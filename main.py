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
def run_pipeline(
    input_path: str,
    level: str | None,
    question_type: str | None,
    script: str | None = None,
    speed: float | None = None,
    read_times: int | None = None,
    orientation: str | None = None,
) -> int:
    from src.question_generator import PURE_LISTENING, QuestionGenerator
    from src.volc_tts import TTSParams, VolcTTS, VolcTTSError, generate_tone_wav
    from src.browser_recorder import BrowserRecorder
    from src.video_encoder import MediaProcessError, transcode_to_mp4

    img = Path(input_path)
    if not img.is_absolute():
        img = (Path.cwd() / img).resolve()
    if not img.exists():
        print(f"[ERROR] 输入图片不存在：{img}"); return 2

    target_level = level or settings.default_level
    target_type = question_type or settings.default_type

    # 画幅方向：CLI > .env > 默认 landscape
    target_orientation = (orientation or settings.video.orientation or "landscape").lower()
    if target_orientation not in ("landscape", "portrait"):
        print(f"[ERROR] orientation 必须是 landscape 或 portrait，当前值：{target_orientation!r}"); return 2

    # 加载画幅预设并覆盖 settings.video 宽高（browser_recorder / 转码器自动生效）
    layout = settings.apply_layout(target_orientation)

    # 纯听力模式：指定了 --script 或 --type pure_listening
    is_pure = (target_type == PURE_LISTENING) or bool(script)
    if is_pure:
        target_type = PURE_LISTENING
        if not script:
            print("[ERROR] 纯听力模式（pure_listening）必须通过 --script 提供英文听力稿"); return 2

    out_dir = settings.output_dir / target_level / img.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_path = out_dir / "question_debug.json"

    print("=" * 64)
    orient_label = "横屏 16:9" if target_orientation == "landscape" else "竖屏 9:16"
    print(f"  英语听力短视频生成 - 端到端流水线（{orient_label}）")
    print("=" * 64)
    print(f"输入图片 : {img}")
    print(f"学段     : {target_level}")
    print(f"题型     : {target_type}")
    print(f"画幅     : {target_orientation} → {settings.video.width}x{settings.video.height} @ {settings.video.fps}fps")
    print(f"输出目录 : {out_dir}")
    print("-" * 64)

    # 0) 图片预处理
    from src.image_utils import image_size, trim_white_border, split_ab_image_white_gap
    stem = img.stem
    orient_suffix = f"_{target_orientation}"   # 竖屏文件命名后缀：pep_g4_u4_cat_portrait.mp4
    print(f"[0/4] 图片预处理 ...")

    if target_orientation == "portrait":
        # 竖屏模式：先裁掉原图外围多余白边，再沿白缝切为左右两张独立卡片
        full_render = trim_white_border(img, out_dir / f"{stem}_trim.png")
        imgA_path, imgB_path = split_ab_image_white_gap(full_render, out_dir)
        print(f"  原图尺寸 : {image_size(img)} → 裁切后 {image_size(full_render)}")
        print(f"  白缝切图 : {imgA_path.name} {image_size(imgA_path)} + {imgB_path.name} {image_size(imgB_path)}")
        render_img = full_render   # 复合图仍传给单一图场景兜底（如 image_ab_choice 模板）
    else:
        # 横屏模式：保持不变，只裁外围多余白边
        render_img = trim_white_border(img, out_dir / f"{stem}_render.png")
        imgA_path = imgB_path = None
        print(f"  原图尺寸 : {image_size(img)} → 渲染尺寸 : {image_size(render_img)}")

    # 1) 出题（纯听力模式用指定稿件，不调用 LLM；否则看图出题）
    print("[1/4] 生成听力文案 ...")
    gen = QuestionGenerator()
    if is_pure:
        if target_orientation == "portrait" and imgA_path and imgB_path:
            question = gen.generate_pure(script, imgA_path, image_b=imgB_path,
                                          level=target_level, save_debug=debug_path)
        else:
            question = gen.generate_pure(script, render_img, level=target_level, save_debug=debug_path)
        print(f"  模式     : 纯听力（pure_listening，画面不展示答案/翻译/解析）")
        print(f"  英文听力稿: {question.audio_text}")
    else:
        question = gen.generate(render_img, level=target_level, question_type=target_type,
                                save_debug=debug_path)
        print(f"  文案来源 : {question.source}")
        print(f"  情境引导 : {question.context_en}")
        print(f"  英文听力稿: {question.audio_text}")
        print(f"  中文翻译 : {question.subtitle_zh}")
        print(f"  正确答案 : {question.answer}")
        print(f"  解析     : {question.explain}")
    print(f"  调试信息 : {debug_path}")

    # 2) TTS 合成听力音频（语速/遍数可被 CLI 覆盖），再按需拼接开场提示音
    print("[2/4] 合成听力音频 ...")
    from src.audio_utils import probe_duration, prepend_intro_sfx

    intro_cfg = settings.intro_sfx
    intro_on = intro_cfg.is_available()
    # 音频不分方向（内容相同），用 stem 即可
    final_audio = out_dir / f"{stem}.mp3"
    speech_audio: Path | None = None

    try:
        tts = VolcTTS()
        if is_pure:
            # 纯听力默认语速 0.85（实测该稿件 ≈10.2s），单遍朗读
            speed_ratio = speed if speed is not None else 0.85
            rt = read_times if read_times is not None else 1
            params = TTSParams(
                voice_type=settings.volc_tts.voice_type,
                speed_ratio=max(0.5, min(2.0, speed_ratio)),
                pause_ms=0, read_times=max(1, rt),
            )
        else:
            curriculum = settings.load_curriculum(target_level)
            params = TTSParams.from_curriculum(
                curriculum, settings.volc_tts, default_read_times=settings.video.read_times
            )
            if speed is not None:
                params.speed_ratio = max(0.5, min(2.0, speed))
            if read_times is not None:
                params.read_times = max(1, read_times)
        # 启用提示音时先把纯语音写到中间文件，再拼接；否则直接写入最终文件
        speech_audio = (out_dir / f"{stem}_speech.mp3") if intro_on else final_audio
        tts.synthesize_full(question.audio_text, speech_audio, level=question.level, params=params)
        print(f"  火山 TTS 成功 : {speech_audio} ({speech_audio.stat().st_size} bytes)")
        print(f"  语速设置 : speed_ratio={params.speed_ratio}（学段 {target_level}）")
        print(f"  朗读遍数 : {params.read_times} 遍")
    except VolcTTSError as e:
        print(f"  [WARN] 火山 TTS 失败，使用本地兜底音频：{e}")
        wav_path = out_dir / f"{stem}.wav"
        duration = max(2.0, min(15.0, len(question.audio_text or question.subtitle_en) * 0.06 + question.read_times * 1.2))
        generate_tone_wav(wav_path, duration=duration)
        speech_audio = wav_path
        if not intro_on:
            final_audio = wav_path
        print(f"  兜底音频生成 : {wav_path} ({wav_path.stat().st_size} bytes)")

    # 2b) 拼接开场提示音：[intro_ding] -> [pause] -> [语音]，统一采样率/声道/响度
    intro_dur = 0.0
    if intro_on:
        prepend_intro_sfx(
            speech_audio, final_audio,
            intro_cfg.resolved_path, pause_sec=intro_cfg.pause_sec,
        )
        intro_dur = probe_duration(intro_cfg.resolved_path)
        print(f"  开场提示音 : 已启用 {intro_cfg.resolved_path.name}（{intro_dur:.2f}s）"
              f" + 停顿 {intro_cfg.pause_sec:.2f}s 后开始朗读")
    else:
        print(f"  开场提示音 : 未启用（ENABLE_INTRO_SFX=False 或提示音文件不存在）")

    speech_dur = probe_duration(speech_audio) if speech_audio else 0.0
    total_dur = probe_duration(final_audio)
    print(f"  语音时长   : {speech_dur:.2f}s")
    print(f"  总音频时长 : {total_dur:.2f}s（提示音 + 停顿 + 语音）")

    # 3) 浏览器无头录制 webm（音画同录；webm 按方向分文件，避免同目录覆盖）
    print("[3/4] 浏览器无头录制（Canvas + WebAudio → webm）...")
    webm_path = out_dir / f"{stem}{orient_suffix}.webm"
    rec = BrowserRecorder()
    rec.record(question, final_audio, webm_path, orientation=target_orientation)
    print(f"  录制完成 webm : {webm_path} ({webm_path.stat().st_size} bytes)")

    # 4) 转码为正式 mp4（H.264 + AAC，含完整音轨；文件名带方向后缀）
    print("[4/4] 转码为正式 MP4（H.264/AAC，imageio-ffmpeg 捆绑二进制）...")
    mp4_path = out_dir / f"{stem}{orient_suffix}.mp4"
    final_video = mp4_path
    try:
        transcode_to_mp4(webm_path, mp4_path, fps=settings.video.fps)
        print(f"  MP4 已生成   : {mp4_path} ({mp4_path.stat().st_size} bytes)")
    except MediaProcessError as e:
        print(f"  [WARN] MP4 转码失败，回退使用 webm：{e}")
        final_video = webm_path

    print("-" * 64)
    print("完成。本次生成内容：")
    print(f"  模式     : {'纯听力（不展示答案/翻译/解析）' if is_pure else '看图二选一'}")
    print(f"  英文听力稿: {question.audio_text}")
    if not is_pure:
        print(f"  情境引导 : {question.context_en}")
        print(f"  中文翻译 : {question.subtitle_zh}")
        print(f"  正确答案 : {question.answer}")
        print(f"  解析     : {question.explain}")
    from src.audio_utils import probe_duration as _pd
    print(f"  实际音频时长 : {_pd(final_audio):.2f}s")
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
    p.add_argument("--type", dest="question_type", metavar="TYPE",
                   help="题型标识，如 image_ab_choice / pure_listening")
    p.add_argument("--script", metavar="TEXT",
                   help="纯听力模式：指定英文听力稿（自动启用 pure_listening，画面不展示答案/翻译）")
    p.add_argument("--speed", type=float, metavar="RATIO",
                   help="覆盖语速倍率（如 0.85），范围 0.5~2.0")
    p.add_argument("--read-times", type=int, dest="read_times", metavar="N",
                   help="覆盖朗读遍数")
    p.add_argument("--orientation", choices=["landscape", "portrait"],
                   help="画幅方向（覆盖 .env VIDEO_ORIENTATION）；默认读取 .env 或 landscape")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.init_check:
        return run_init_check()
    if args.input:
        return run_pipeline(
            args.input, args.level, args.question_type,
            script=args.script, speed=args.speed, read_times=args.read_times,
            orientation=args.orientation,
        )
    build_parser().print_help()
    print("\n示例：python main.py --input input/test_ab_bag.jpeg --level primary_g4")
    print("纯听力：python main.py --input input/xxx.jpeg --level primary_g4 --type pure_listening "
          "--script \"Look at...\"")
    print("竖屏测试：python main.py --input input/xxx.png --level primary_g4 --orientation portrait "
          "--script \"...\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
