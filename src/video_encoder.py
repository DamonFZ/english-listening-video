"""
视频转码：把浏览器录制产出的 webm（vp9/opus）转换为标准兼容的 mp4（H.264 + AAC）。

使用 imageio-ffmpeg 捆绑的 ffmpeg 二进制，无需系统级安装 ffmpeg。
转码时强制 yuv420p 像素格式 + faststart，确保主流播放器与视频号可直接播放。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from src.audio_utils import MediaProcessError, run_ffmpeg

PathLike = Union[str, Path]


def transcode_to_mp4(
    webm_path: PathLike,
    mp4_path: PathLike,
    *,
    fps: int = 30,
    crf: int = 20,
    preset: str = "veryfast",
) -> Path:
    """
    将 webm 转码为 mp4。

    参数：
        webm_path: 输入 webm 文件（含 vp9/opus 音视频轨）。
        mp4_path:  输出 mp4 文件路径。
        fps:       输出帧率（与录制一致，默认 30）。
        crf:       视频质量（越小越清晰，18~23 常用）。
        preset:    编码速度预设。
    """
    src = Path(webm_path)
    if not src.exists() or src.stat().st_size == 0:
        raise MediaProcessError(f"输入 webm 不存在或为空：{src}")
    out = Path(mp4_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    run_ffmpeg([
        "-i", str(src),
        # 视频：H.264，yuv420p 保证兼容性，faststart 便于流式播放
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-movflags", "+faststart",
        # 音频：AAC
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        # 取较短轨（音视频时长一致时无影响）
        "-shortest",
        "-y", str(out),
    ], timeout=600.0)

    if not out.exists() or out.stat().st_size < 1024:
        raise MediaProcessError(f"转码产物异常：{out}")
    return out


def is_mp4_ok(path: PathLike, min_size: int = 1024) -> bool:
    p = Path(path)
    return p.exists() and p.stat().st_size >= min_size
