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


# ---------------------------------------------------------------------------
# 竖屏模式辅助：把横向 A/B 复合图沿中间白缝切成左右两张独立卡片
# ---------------------------------------------------------------------------

def split_ab_image_white_gap(
    src: Path,
    out_dir: Path,
    white_threshold: int = 245,
    scan_ratio: tuple[float, float] = (0.35, 0.65),
    min_strip_width: int = 16,
    safe_pad: int = 10,
) -> tuple[Path, Path]:
    """把横向 A/B 复合图沿最宽的近纯白纵向列带中心切分为左右两张卡片。

    参数:
        src: 横向 A/B 复合图路径。
        out_dir: 切分后的两张图的输出目录（会自动创建）。
        white_threshold: RGB 通道值 >= 阈值视为白像素（默认 245）。
        scan_ratio: 扫描白缝的水平区间，默认宽度的 35%~65%（避开两侧卡片边缘）。
        min_strip_width: 判定为有效白缝的最小列数（像素），避免误切卡片内部小空隙。
        safe_pad: 切分后每张卡片四周额外保留的安全内边距（像素），
                  保证左上角 A/B 圆形徽标与圆角不被裁切。

    返回: (left_path, right_path) — 左图 A、右图 B 两张切好并分别裁过白边的 PNG 路径。

    若 Pillow 不可用或任何步骤失败，则回退到中线切分，保证流水线不中断。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    left_path = out_dir / f"{src.stem}_A.png"
    right_path = out_dir / f"{src.stem}_B.png"

    if not _PIL_AVAILABLE or not src.exists():
        # 回退：直接复制原图到两个输出（后续模板 contain 缩放下不会报错）
        shutil.copyfile(src, left_path); shutil.copyfile(src, right_path)
        return left_path, right_path

    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            left_bound = int(w * scan_ratio[0])
            right_bound = int(w * scan_ratio[1])
            left_bound = max(left_bound, 1)
            right_bound = min(right_bound, w - 1)
            if right_bound - left_bound < min_strip_width:
                # 扫描区间太窄 → 回退到中线
                split_x = w // 2
            else:
                split_x = _find_whitest_strip_center(
                    im, left_bound, right_bound, h,
                    white_threshold=white_threshold,
                    min_strip_width=min_strip_width,
                )

            # 切分（split_x 是左图的右边界，右图的左边界）
            left_card = im.crop((0, 0, split_x, h))
            right_card = im.crop((split_x, 0, w, h))

        # 分别裁掉各自外围多余白边（复用 trim_white_border），再补安全内边距
        _save_card(left_card, left_path, safe_pad)
        _save_card(right_card, right_path, safe_pad)
        return left_path, right_path

    except Exception:  # noqa: BLE001
        # 回退：中线切分，不裁白边
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                w, h = im.size
                left_card = im.crop((0, 0, w // 2, h))
                right_card = im.crop((w // 2, 0, w, h))
            left_card.save(left_path, format="PNG")
            right_card.save(right_path, format="PNG")
        except Exception:  # noqa: BLE001
            shutil.copyfile(src, left_path)
            shutil.copyfile(src, right_path)
        return left_path, right_path


def _find_whitest_strip_center(
    im: "Image.Image",
    left_x: int,
    right_x: int,
    height: int,
    white_threshold: int,
    min_strip_width: int,
) -> int:
    """在 [left_x, right_x] 区间内找最宽的连续白列带，返回其中心 X。

    列白度定义：该列中 >= white_threshold 的行占全高比例 >= 0.95 即视为白列。
    """
    data = im.load()
    row_stride = 1 if height <= 2000 else max(1, height // 2000)  # 大图降采样加速
    col_white = [True] * (right_x - left_x + 1)

    for ci in range(right_x - left_x + 1):
        cx = left_x + ci
        white_count = 0
        checked = 0
        for cy in range(0, height, row_stride):
            checked += 1
            r, g, b = data[cx, cy]
            if r >= white_threshold and g >= white_threshold and b >= white_threshold:
                white_count += 1
        if checked == 0 or white_count / checked < 0.95:
            col_white[ci] = False

    # 扫描最长连续 True 段
    best_start = 0; best_len = 0
    cur_start = 0; cur_len = 0
    for ci in range(len(col_white)):
        if col_white[ci]:
            if cur_len == 0: cur_start = ci
            cur_len += 1
            if cur_len > best_len: best_len = cur_len; best_start = cur_start
        else:
            cur_len = 0

    if best_len < min_strip_width:
        # 没找到足够宽的白缝 → 回退到中线
        return (left_x + right_x) // 2

    center_ci = best_start + best_len // 2
    return left_x + center_ci


def _save_card(card: "Image.Image", dst: Path, safe_pad: int) -> None:
    """保存单张卡片：先裁掉内部多余白边，再给四周补 safe_pad 纯白内边距。"""
    # 裁内部白边
    bg = Image.new("RGB", card.size, (255, 255, 255))
    diff = ImageChops.difference(card, bg)
    r, g, b = diff.split()
    max_diff = ImageChops.lighter(ImageChops.lighter(r, g), b)
    tol = max(0, 255 - 248)
    mask = max_diff.point(lambda p: 255 if p > tol else 0)
    bbox = mask.getbbox()
    if bbox is not None:
        card = card.crop(bbox)
    # 补四周安全内边距（纯白），保证 A/B 徽标与圆角完整
    w, h = card.size
    out = Image.new("RGB", (w + safe_pad * 2, h + safe_pad * 2), (255, 255, 255))
    out.paste(card, (safe_pad, safe_pad))
    out.save(dst, format="PNG")
