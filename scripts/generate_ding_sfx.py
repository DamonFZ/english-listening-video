"""纯 Python 标准库合成「英语听力考场前奏提示音（叮）」候选音效。

仅使用 math / wave / struct / os，不依赖 numpy、scipy、FFmpeg 或任何外部音频库。
声学设计要点（避免刺耳电子蜂鸣）：
  - 加法合成：基频 + 2~5 次谐波/泛音，高次泛音增益快速衰减；
  - ADSR：极短线性起音（Attack 5~10ms，消除开头咔哒爆音）+ 指数衰减余韵，结尾自然归零；
  - 峰值响度控制在满幅度的 60%~70%，听感柔和清晰，与人声衔接自然。

输出：assets/sfx/candidates/ 下 4 个 16-bit PCM / 44100Hz / 单声道 .wav。
"""

from __future__ import annotations

import math
import os
import struct
import wave

SAMPLE_RATE = 44100
MAX_16BIT = 32767.0
TARGET_PEAK = 0.65  # 满幅度的 65%，落在 60%~70% 区间

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets", "sfx", "candidates")


# ---------------------------------------------------------------------------
# 核心合成：加法合成 + 平滑包络
# ---------------------------------------------------------------------------
def render_tone(
    freq: float,
    duration: float,
    harmonics: list[tuple[float, float]],
    attack_ms: float = 8.0,
    decay_tau: float | None = None,
    sr: int = SAMPLE_RATE,
    peak: float = TARGET_PEAK,
) -> list[float]:
    """渲染单个音（加法合成 + Attack / Exponential Decay 包络）。

    harmonics: [(倍频, 增益), ...]，例如 [(1, 1.0), (2, 0.5), (3, 0.22)]。
    返回归一化到 peak 幅度的 float 样本序列（值域 [-1, 1]）。
    """
    n = max(1, int(round(duration * sr)))
    attack = max(1, int(round(attack_ms / 1000.0 * sr)))
    tau = (duration / 4.5 if decay_tau is None else decay_tau) * sr

    samples: list[float] = []
    for i in range(n):
        t = i / sr
        s = 0.0
        for mult, gain in harmonics:
            s += gain * math.sin(2.0 * math.pi * freq * mult * t)
        # 起音：线性斜坡；衰减：指数（从起音结束处开始）
        ramp = min(1.0, i / attack)
        decay = math.exp(-max(0, i - attack) / tau)
        s *= ramp * decay
        samples.append(s)

    # 归一化到目标峰值，避免削波
    mx = max((abs(x) for x in samples), default=0.0)
    if mx > 0:
        scale = peak / mx
        samples = [x * scale for x in samples]
    return samples


def add_tone(buf: list[float], start: int, tone: list[float]) -> None:
    """把一段 tone 叠加到 buf 的指定起始位置（越界忽略）。"""
    for j, v in enumerate(tone):
        idx = start + j
        if 0 <= idx < len(buf):
            buf[idx] += v


def normalize(buf: list[float], peak: float = TARGET_PEAK) -> list[float]:
    mx = max((abs(x) for x in buf), default=0.0)
    if mx > 0:
        scale = peak / mx
        return [x * scale for x in buf]
    return buf


def write_wav(path: str, samples: list[float], sr: int = SAMPLE_RATE) -> None:
    """写入 16-bit PCM 单声道 WAV。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = struct.pack(
        "<" + "h" * len(samples),
        *(int(max(-1.0, min(1.0, s)) * MAX_16BIT) for s in samples),
    )
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)


def verify_wav(path: str) -> tuple[int, int, int]:
    """校验 WAV 属性，返回 (channels, sampwidth, framerate)。"""
    with wave.open(path, "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate()


# ---------------------------------------------------------------------------
# 4 款候选音效定义
# ---------------------------------------------------------------------------
# 谐波：[(倍频, 增益), ...]；高次泛音衰减快，避免尖锐刺耳。
HARM_BELL = [(1, 1.0), (2, 0.45), (3, 0.22), (4, 0.10), (5, 0.04)]   # 钢片琴/铃铛
HARM_WARM = [(1, 1.0), (2, 0.40), (3, 0.12), (4, 0.04)]            # 温润，少高频
HARM_CHIME = [(1, 1.0), (2, 0.40), (3, 0.18), (4, 0.08)]           # 清亮课堂广播
HARM_CRYSTAL = [(1, 1.0), (2, 0.32), (3, 0.14), (4, 0.05)]         # 现代空气感


def gen_classic_ding() -> tuple[str, list[float]]:
    # 经典考场单叮：A5 ≈ 880Hz，钢片琴质感，0.85s，收尾利落
    samples = render_tone(880.0, 0.85, HARM_BELL, attack_ms=7.0, decay_tau=0.20)
    return os.path.join(OUT_DIR, "01_classic_ding.wav"), samples


def gen_warm_primary_ding() -> tuple[str, list[float]]:
    # 温润小学教学叮：G5 ≈ 784Hz，0.9s，减少高频尖锐泛音
    samples = render_tone(783.99, 0.90, HARM_WARM, attack_ms=9.0, decay_tau=0.26)
    return os.path.join(OUT_DIR, "02_warm_primary_ding.wav"), samples


def gen_soft_ding_dong() -> tuple[str, list[float]]:
    # 柔和双音叮咚：E5 -> C5 下行，第二音略微重叠接续，总 1.2s
    total = 1.2
    n = int(round(total * SAMPLE_RATE))
    buf = [0.0] * n
    tone1 = render_tone(659.25, 0.62, HARM_CHIME, attack_ms=8.0, decay_tau=0.16)
    tone2 = render_tone(523.25, 0.78, HARM_CHIME, attack_ms=8.0, decay_tau=0.20)
    add_tone(buf, 0, tone1)
    add_tone(buf, int(round(0.42 * SAMPLE_RATE)), tone2)
    buf = normalize(buf)
    # 末尾 15ms 线性淡出，避免衰减尾部残留造成咔哒
    tail = int(round(0.015 * SAMPLE_RATE))
    for i in range(tail):
        buf[-1 - i] *= i / tail
    return os.path.join(OUT_DIR, "03_soft_ding_dong.wav"), buf


def gen_crisp_chime() -> tuple[str, list[float]]:
    # 清亮短促叮：E6 ≈ 1318.5Hz 主音 + 柔和低八度（E5）基音，0.65s
    # 在 render_tone 的谐波里加入低八度 sub（倍频 0.5）增加温暖基底
    partials = [(0.5, 0.28)] + HARM_CRYSTAL
    samples = render_tone(1318.51, 0.65, partials, attack_ms=6.0, decay_tau=0.14)
    return os.path.join(OUT_DIR, "04_crisp_chime.wav"), samples


def main() -> None:
    generators = [
        gen_classic_ding,
        gen_warm_primary_ding,
        gen_soft_ding_dong,
        gen_crisp_chime,
    ]

    print("=" * 70)
    print("  英语听力考场前奏提示音（叮）候选生成 — 纯 Python 加法合成")
    print(f"  输出目录: {OUT_DIR}")
    print("=" * 70)

    results = []
    for gen in generators:
        path, samples = gen()
        write_wav(path, samples)
        results.append((path, len(samples)))

    print()
    print(f"{'#':<3}{'文件':<34}{'大小(KB)':>10}{'时长(s)':>10}  校验")
    print("-" * 78)
    for i, (path, n) in enumerate(results, 1):
        size = os.path.getsize(path)
        dur = n / SAMPLE_RATE
        ch, sw, sr = verify_wav(path)
        ok = "16-bit/44.1k/mono" if (ch == 1 and sw == 2 and sr == 44100) else "???"
        print(f"{i:<3}{os.path.basename(path):<34}{size/1024:>10.1f}{dur:>10.3f}  {ok}")

    print()
    print("macOS 终端逐个试听（afplay）：")
    for i, (path, _) in enumerate(results, 1):
        print(f"  {i}. afplay {path}")
    print()
    print("一次全部依次试听：")
    print("  " + " && ".join(f"afplay {p}" for p, _ in results))
    print()
    print("完成：4 个候选提示音已生成。")


if __name__ == "__main__":
    main()
