"""
英语听力短视频生成器 - 主入口 CLI。

用法：
    python main.py --init-check
    python main.py --input input/test_ab_bag.jpeg --level primary_g4
    python main.py --input input/test_ab_bag.jpeg --level primary_g4 --type image_ab_choice

全链路：看图出题（多模态 LLM，无 Key 时走兜底） -> 火山 TTS 合成音频
       （失败时本地生成兜底音频）-> Playwright 无头录制导出 webm。
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import List, Literal

from config.settings import ROOT_DIR, settings

CheckStatus = Literal["PASS", "WARN", "FAIL"]
Check = tuple[CheckStatus, str]


# ===================== 环境自检 =====================
def _check_python() -> Check:
    ok = sys.version_info >= (3, 10)
    ver = ".".join(map(str, sys.version_info[:3]))
    return ("PASS" if ok else "FAIL"), f"Python {ver}（要求 >= 3.10）"


def _check_imports() -> List[Check]:
    results: List[Check] = []
    for mod in ("dotenv", "requests", "playwright", "imageio_ffmpeg"):
        try:
            importlib.import_module(mod)
            results.append(("PASS", f"依赖可导入：{mod}"))
        except Exception as exc:  # noqa: BLE001
            results.append(("FAIL", f"依赖缺失：{mod}（{exc}）"))
    return results


def _check_browser() -> Check:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return "WARN", f"playwright 未安装：{exc}"
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            if exe and Path(exe).exists():
                return "PASS", f"Chromium 内核已安装：{exe}"
        # 回退检测系统 Chrome
        for cand in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ):
            if Path(cand).exists():
                return "PASS", f"使用系统浏览器（Playwright 捆绑内核缺失，已自动回退）：{cand}"
        return "WARN", "未找到可用浏览器：请执行 playwright install chromium 或安装 Google Chrome"
    except Exception as exc:  # noqa: BLE001
        return "WARN", f"检测浏览器失败：{exc}"


def _check_env_file() -> Check:
    env_path = ROOT_DIR / ".env"
    example_path = ROOT_DIR / ".env.example"
    if env_path.exists():
        return "PASS", f".env 已存在：{env_path}"
    if example_path.exists():
        return "WARN", f".env 不存在，请复制模板：cp .env.example .env"
    return "FAIL", ".env 与 .env.example 均不存在"


def _check_config() -> List[Check]:
    results: List[Check] = []
    for e in settings.validate():
        results.append(("FAIL", e))
    if not any(m.startswith("配置校验通过") for _, m in results):
        results.append(("PASS", "配置校验通过"))
    try:
        c = settings.load_curriculum()
        results.append(("PASS", f"学段配置加载成功：{c.get('name')}（{len(c.get('units', []))} 个单元）"))
    except Exception as exc:  # noqa: BLE001
        results.append(("FAIL", f"学段配置加载失败：{exc}"))
    results.append((
        "PASS" if settings.llm.is_configured() else "WARN",
        "多模态 LLM 已配置" if settings.llm.is_configured()
        else "多模态 LLM 未配置（将使用确定性兜底样例）",
    ))
    results.append((
        "PASS" if settings.volc_tts.is_configured() else "WARN",
        "火山引擎 TTS 已配置" if settings.volc_tts.is_configured()
        else "火山引擎 TTS 未配置（将使用本地兜底音频）",
    ))
    results.append(("PASS", f"可用学段：{', '.join(settings.list_levels()) or '（无）'}"))
    return results


def _check_output_dir() -> Check:
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.output_dir / ".write_test"
        probe.write_text("ok"); probe.unlink()
        return "PASS", f"output 目录可写：{settings.output_dir}"
    except Exception as exc:  # noqa: BLE001
        return "FAIL", f"output 目录不可写：{exc}"


def run_init_check() -> int:
    print("=" * 64)
    print("  环境与配置自检 (--init-check)")
    print("=" * 64)
    checks: List[Check] = [_check_python(), *_check_imports(), _check_env_file(),
                           *_check_config(), _check_browser(), _check_output_dir()]
    for s, m in checks:
        print(f"[{s}] {m}")
    nf = sum(1 for s, _ in checks if s == "FAIL")
    nw = sum(1 for s, _ in checks if s == "WARN")
    print("-" * 64)
    print(f"自检完成：PASS {len(checks)-nf-nw} / WARN {nw} / FAIL {nf}")
    if nf:
        print("存在 FAIL 项，请修复后重试。"); return 1
    print("视频参数：{}x{} @ {}fps，默认朗读 {} 遍".format(
        settings.video.width, settings.video.height, settings.video.fps, settings.video.read_times))
    return 0


# ===================== 端到端生成 =====================
def run_pipeline(input_path: str, level: str | None, question_type: str | None) -> int:
    from src.question_generator import QuestionGenerator
    from src.volc_tts import VolcTTS, VolcTTSError, generate_tone_wav
    from src.browser_recorder import BrowserRecorder
    from src.video_encoder import MediaProcessError, transcode_to_mp4

    img = Path(input_path)
    if not img.is_absolute():
        img = (Path.cwd() / img).resolve()
    if not img.exists():
        print(f"[ERROR] 输入图片不存在：{img}"); return 2

    target_level = level or settings.default_level
    target_type = question_type or settings.default_type

    out_dir = settings.output_dir / target_level / img.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_path = out_dir / "question_debug.json"

    print("=" * 64)
    print("  英语听力短视频生成 - 端到端流水线（横屏 MP4）")
    print("=" * 64)
    print(f"输入图片 : {img}")
    print(f"学段     : {target_level}")
    print(f"题型     : {target_type}")
    print(f"分辨率   : {settings.video.width}x{settings.video.height} @ {settings.video.fps}fps")
    print(f"输出目录 : {out_dir}")
    print("-" * 64)

    # 1) 看图出题
    print("[1/4] 生成听力文案与答案 ...")
    gen = QuestionGenerator()
    question = gen.generate(img, level=target_level, question_type=target_type,
                            save_debug=debug_path)
    print(f"  文案来源 : {question.source}")
    print(f"  情境引导 : {question.context_en}")
    print(f"  英文听力稿: {question.audio_text}")
    print(f"  中文翻译 : {question.subtitle_zh}")
    print(f"  正确答案 : {question.answer}")
    print(f"  解析     : {question.explain}")
    print(f"  朗读遍数 : {question.read_times} 遍（遍间停顿 {question.read_times and '由学段配置'}）")
    print(f"  调试信息 : {debug_path}")

    # 2) TTS 合成完整听力音频（朗读 N 遍 + 遍间静音拼接）
    print("[2/4] 合成听力音频（多遍 + 停顿）...")
    mp3_path = out_dir / f"{img.stem}.mp3"
    wav_path = out_dir / f"{img.stem}.wav"
    final_audio = mp3_path
    try:
        tts = VolcTTS()
        tts.synthesize_full(question.audio_text, mp3_path, level=question.level)
        print(f"  火山 TTS 成功 : {mp3_path} ({mp3_path.stat().st_size} bytes)")
    except VolcTTSError as e:
        print(f"  [WARN] 火山 TTS 失败，使用本地兜底音频：{e}")
        duration = max(2.0, min(15.0, len(question.audio_text or question.subtitle_en) * 0.06 + question.read_times * 1.2))
        generate_tone_wav(wav_path, duration=duration)
        final_audio = wav_path
        print(f"  兜底音频生成 : {wav_path} ({wav_path.stat().st_size} bytes)")

    # 3) 浏览器无头录制 webm（音画同录）
    print("[3/4] 浏览器无头录制（Canvas + WebAudio → webm）...")
    webm_path = out_dir / f"{img.stem}.webm"
    rec = BrowserRecorder()
    rec.record(question, final_audio, webm_path)
    print(f"  录制完成 webm : {webm_path} ({webm_path.stat().st_size} bytes)")

    # 4) 转码为正式 mp4（H.264 + AAC，含完整音轨）
    print("[4/4] 转码为正式 MP4（H.264/AAC，imageio-ffmpeg 捆绑二进制）...")
    mp4_path = out_dir / f"{img.stem}.mp4"
    final_video = mp4_path
    try:
        transcode_to_mp4(webm_path, mp4_path, fps=settings.video.fps)
        print(f"  MP4 已生成   : {mp4_path} ({mp4_path.stat().st_size} bytes)")
    except MediaProcessError as e:
        print(f"  [WARN] MP4 转码失败，回退使用 webm：{e}")
        final_video = webm_path

    print("-" * 64)
    print("完成。本次生成的完整听力内容：")
    print(f"  情境引导 : {question.context_en}")
    print(f"  英文听力稿: {question.audio_text}")
    print(f"  中文翻译 : {question.subtitle_zh}")
    print(f"  正确答案 : {question.answer}")
    print(f"  解析     : {question.explain}")
    print("关键产物：")
    print(f"  音频 : {final_audio}")
    print(f"  视频 : {final_video}")
    print(f"  文案 : {debug_path}")
    return 0


# ===================== CLI =====================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="英语听力短视频生成器")
    p.add_argument("--init-check", action="store_true", help="环境与配置自检")
    p.add_argument("--input", metavar="PATH", help="输入 A/B 对比图路径，触发端到端生成")
    p.add_argument("--level", metavar="LEVEL", help="学段标识，如 primary_g4")
    p.add_argument("--type", dest="question_type", metavar="TYPE", help="题型标识，如 image_ab_choice")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.init_check:
        return run_init_check()
    if args.input:
        return run_pipeline(args.input, args.level, args.question_type)
    build_parser().print_help()
    print("\n示例：python main.py --input input/test_ab_bag.jpeg --level primary_g4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
