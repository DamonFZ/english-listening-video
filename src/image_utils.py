"""图片预处理：渲染前自动裁切原图外围多余的纯白空边。

仅依赖 Pillow（无 numpy），用 ImageChops 取三通道最大差值定位非白内容边界，
保留极小安全边距后裁剪为 PNG，使 A/B 卡片在 1920x1080 画面中尽可能大、留白克制。
"""
from __future__ import annotations

import shutil
from pathlib import Path

try:
    from PIL import Image, ImageChops
    _PIL_AVAILABLE = True
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False


def trim_white_border(
    src: Path,
    dst: Path,
    white_threshold: int = 248,
    margin_ratio: float = 0.012,
) -> Path:
    """裁掉 src 外围接近纯白的空白边界，保存为 PNG 到 dst，返回 dst。

    参数:
        src: 原图路径。
        dst: 裁剪后 PNG 输出路径。
        white_threshold: RGB 通道值 >= 该阈值即视为纯白背景（默认 248，兼容轻微灰白）。
        margin_ratio: 裁切后四周额外保留的安全边距，占原图短边比例（默认 1.2%）。

    若 Pillow 不可用、图像全白或裁切失败，则把原图原样复制到 dst 并返回，
    保证流水线不中断。
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not _PIL_AVAILABLE or not src.exists():
        shutil.copyfile(src, dst)
        return dst

    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            # 与纯白背景做差，取三通道最大差值 → 任何一个通道非白即判定为内容
            bg = Image.new("RGB", im.size, (255, 255, 255))
            diff = ImageChops.difference(im, bg)
            r, g, b = diff.split()
            max_diff = ImageChops.lighter(ImageChops.lighter(r, g), b)
            tol = max(0, 255 - int(white_threshold))
            mask = max_diff.point(lambda p: 255 if p > tol else 0)
            bbox = mask.getbbox()
            if bbox is None:
                shutil.copyfile(src, dst)
                return dst

            margin = int(min(w, h) * float(margin_ratio))
            left = max(0, bbox[0] - margin)
            top = max(0, bbox[1] - margin)
            right = min(w, bbox[2] + margin)
            bottom = min(h, bbox[3] + margin)
            if right - left < 8 or bottom - top < 8:
                shutil.copyfile(src, dst)
                return dst
            cropped = im.crop((left, top, right, bottom))
            cropped.save(dst, format="PNG")
            return dst
    except Exception:  # noqa: BLE001
        shutil.copyfile(src, dst)
        return dst


def image_size(path: Path) -> tuple[int, int] | None:
    """返回 (宽, 高)，失败返回 None。"""
    if not _PIL_AVAILABLE:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None
