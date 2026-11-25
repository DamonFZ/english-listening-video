"""
音频后处理工具（基于 imageio-ffmpeg 捆绑的 ffmpeg 二进制，不依赖系统 ffmpeg）。

用途：
  - 生成指定时长的静音片段；
  - 把「听力音频 + 静音 + 听力音频」拼接成「朗读两遍、中间停顿」的完整听力音频；
  - （ffmpeg 子进程执行器同时供 video_encoder 复用）。
"""
from __future__ import annotations

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
