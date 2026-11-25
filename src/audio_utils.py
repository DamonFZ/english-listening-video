"""
音频后处理工具（基于 imageio-ffmpeg 捆绑的 ffmpeg 二进制，不依赖系统 ffmpeg）。

用途：
  - 生成指定时长的静音片段；
  - 把「听力音频 + 静音 + 听力音频」拼接成「朗读两遍、中间停顿」的完整听力音频；
  - （ffmpeg 子进程执行器同时供 video_encoder 复用）。
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Union

PathLike = Union[str, Path]


class MediaProcessError(RuntimeError):
    """ffmpeg 执行失败。"""


def get_ffmpeg_exe() -> str:
    """返回 imageio-ffmpeg 捆绑的 ffmpeg 可执行文件路径（首次调用会自动下载）。"""
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args: Sequence[str], *, timeout: Optional[float] = 300.0) -> None:
    """执行 ffmpeg 子进程，非零退出则抛 MediaProcessError（含 stderr 尾部）。"""
    cmd = [get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise MediaProcessError(f"未找到 ffmpeg 可执行文件：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProcessError(f"ffmpeg 执行超时（>{timeout}s）") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise MediaProcessError(
            f"ffmpeg 失败（exit {proc.returncode}）：\n" + "\n".join(tail)
        )


# ----------------------------------------------------------------------
def make_silence(out_path: PathLike, duration_ms: int, sample_rate: int = 24000) -> Path:
    """生成一段静音音频（mp3），用于两遍朗读之间的停顿。"""
    if duration_ms <= 0:
        raise ValueError("duration_ms 必须 > 0")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    seconds = duration_ms / 1000.0
    run_ffmpeg([
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{seconds:.3f}",
        "-c:a", "libmp3lame", "-ar", str(sample_rate), "-ac", "1", "-q:a", "4",
        "-y", str(out),
    ])
    return out


def concat_audio(parts: Sequence[PathLike], out_path: PathLike,
                 sample_rate: int = 24000) -> Path:
    """
    顺序拼接多段音频为一个 mp3 文件。

    使用 concat demuxer + 重编码，避免各段编码参数不一致导致拼接失败。
    """
    if not parts:
        raise ValueError("parts 不能为空")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # concat demuxer 需要一个列出所有输入文件的清单文件
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        list_path = tf.name
        for p in parts:
            # ffmpeg concat 清单里的路径需转义单引号
            safe = str(Path(p).resolve()).replace("'", r"'\''")
            tf.write(f"file '{safe}'\n")

    try:
        run_ffmpeg([
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c:a", "libmp3lame", "-ar", str(sample_rate), "-ac", "1", "-q:a", "4",
            "-y", str(out),
        ])
    finally:
        try:
            Path(list_path).unlink(missing_ok=True)
        except OSError:
            pass
    return out


# ----------------------------------------------------------------------
def measure_mean_volume(path: PathLike) -> float:
    """用 ffmpeg volumedetect 测量音频平均响度（dB）；无法识别时返回 -30.0。"""
    src = Path(path)
    if not src.exists():
        return -30.0
    cmd = [
        get_ffmpeg_exe(), "-hide_banner", "-i", str(src),
        "-af", "volumedetect", "-vn", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -30.0
    text = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    return float(m.group(1)) if m else -30.0


def _normalize_to_wav(src: PathLike, dst: Path, sample_rate: int,
                      volume_db: float = 0.0) -> Path:
    """把任意音频重采样为 16-bit PCM 单声道 WAV，并可叠加音量增益（dB）。"""
    af = "aformat=sample_fmts=s16:sample_rates={0}:channel_layouts=mono".format(sample_rate)
    if abs(volume_db) > 1e-6:
        af = "volume={0:.4f}dB,".format(volume_db) + af
    run_ffmpeg([
        "-i", str(Path(src)),
        "-af", af,
        "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le",
        "-y", str(dst),
    ])
    return dst


def prepend_intro_sfx(
    speech_path: PathLike,
    out_path: PathLike,
    intro_path: PathLike,
    pause_sec: float = 0.5,
    target_sample_rate: int = 44100,
    cue_boost_db: float = 4.0,
) -> Path:
    """
    在听力语音前拼接 [开场提示音] -> [pause_sec 静音] -> [语音]，输出统一的 mp3。

    关键处理（避免变调/爆音/响度失衡）：
      1. 先把提示音与语音都重采样为 44100Hz / 16-bit / 单声道 WAV，统一采样率与声道；
      2. 用 volumedetect 测量两段平均响度，把提示音校准到「语音 + cue_boost_db」
         （钳制在合理区间），确保提示音清晰起提示作用但不盖过人声；
      3. 生成等采样率的静音停顿；
      4. 用 concat demuxer + 重编码拼接，结尾平滑，无咔哒爆音。
    返回 out_path（mp3）。
    """
    speech = Path(speech_path)
    intro = Path(intro_path)
    out = Path(out_path)
    if not intro.exists():
        raise FileNotFoundError(f"开场提示音文件不存在：{intro}")
    if not speech.exists():
        raise FileNotFoundError(f"语音文件不存在：{speech}")
    out.parent.mkdir(parents=True, exist_ok=True)

    speech_db = measure_mean_volume(speech)
    intro_db = measure_mean_volume(intro)
    # 目标提示音响度 = 语音 + 增强量，钳制在 [-23, -11] dB，避免过响刺耳或过轻听不见
    target_db = max(-23.0, min(-11.0, speech_db + cue_boost_db))
    gain = max(-10.0, min(6.0, target_db - intro_db))

    with tempfile.TemporaryDirectory(prefix="elv_intro_") as td:
        td_path = Path(td)
        intro_wav = td_path / "intro.wav"
        pause_wav = td_path / "pause.wav"
        speech_wav = td_path / "speech.wav"

        _normalize_to_wav(intro, intro_wav, target_sample_rate, volume_db=gain)
        # 静音停顿：等采样率单声道 s16
        run_ffmpeg([
            "-f", "lavfi",
            "-i", f"anullsrc=r={target_sample_rate}:cl=mono",
            "-t", f"{max(0.0, pause_sec):.3f}",
            "-ar", str(target_sample_rate), "-ac", "1", "-c:a", "pcm_s16le",
            "-y", str(pause_wav),
        ])
        _normalize_to_wav(speech, speech_wav, target_sample_rate, volume_db=0.0)

        concat_audio([intro_wav, pause_wav, speech_wav], out, sample_rate=target_sample_rate)
    return out


# ----------------------------------------------------------------------
def probe_duration(path: PathLike) -> float:
    """
    探测音频/视频时长（秒），用捆绑 ffmpeg 解析 stderr 中的 Duration 行，
    不依赖系统 ffprobe。无法识别时返回 0.0。
    """
    src = Path(path)
    if not src.exists():
        return 0.0
    cmd = [get_ffmpeg_exe(), "-hide_banner", "-i", str(src)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    # ffmpeg 会把输入信息打到 stderr：Duration: 00:00:10.20, start: 0.000000, bitrate: ...
    text = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
    if not m:
        return 0.0
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(ss)
