"""
英语听力短视频生成器 - 主入口 CLI 脚手架。

子命令 / 参数：
    --init-check          环境与配置自检（Python 环境、依赖、Playwright 内核、配置校验）
    --input PATH          待处理题图或素材目录（占位，后续流水线使用）
    --level LEVEL         学段标识，如 primary_g4（默认取 .env 的 DEFAULT_LEVEL）
    --type QUESTION_TYPE  题型标识，如 image_ab_choice（默认取 .env 的 DEFAULT_TYPE）

自检结果分级：
    PASS  通过
    WARN  骨架阶段可暂不满足（如 LLM/TTS 未配置、Playwright 内核未安装），不阻断自检
    FAIL  必须修复（如 Python 版本过低、核心依赖缺失、学段/模板配置缺失、output 不可写）

本阶段先完成骨架与自检，真实生成流水线（生成文案 -> TTS -> 录制）后续接入。
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Literal

from config.settings import ROOT_DIR, settings

CheckStatus = Literal["PASS", "WARN", "FAIL"]
Check = tuple[CheckStatus, str]


def _check_python() -> Check:
    ok = sys.version_info >= (3, 10)
    ver = ".".join(map(str, sys.version_info[:3]))
    return ("PASS" if ok else "FAIL"), f"Python {ver}（要求 >= 3.10）"


def _check_imports() -> list[Check]:
    results: list[Check] = []
    for mod in ("dotenv", "requests", "playwright"):
        try:
            importlib.import_module(mod)
            results.append(("PASS", f"依赖可导入：{mod}"))
        except Exception as exc:  # noqa: BLE001
            results.append(("FAIL", f"依赖缺失：{mod}（{exc}）"))
    return results


def _check_playwright_browsers() -> Check:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return "WARN", f"playwright 未安装，无法检测浏览器内核（{exc}）"
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            exists = bool(exe) and Path(exe).exists()
            if exists:
                return "PASS", f"Chromium 内核已安装：{exe}"
            return "WARN", f"Chromium 内核未安装，请执行：playwright install chromium（{exe}）"
    except Exception as exc:  # noqa: BLE001
        return "WARN", f"检测 Chromium 内核失败：{exc}"


def _check_env_file() -> Check:
    env_path = ROOT_DIR / ".env"
    example_path = ROOT_DIR / ".env.example"
    if env_path.exists():
        return "PASS", f".env 已存在：{env_path}"
    if example_path.exists():
        return "WARN", f".env 不存在，请复制模板：cp .env.example .env（模板存在：{example_path}）"
    return "FAIL", ".env 与 .env.example 均不存在"


def _check_config() -> list[Check]:
    results: list[Check] = []
    for e in settings.validate():
        results.append(("FAIL", e))
    if not any(msg.startswith("配置校验通过") for _, msg in results):
        results.append(("PASS", "配置校验通过（默认学段/题型/视频参数合法）"))

    try:
        curriculum = settings.load_curriculum()
        results.append((
            "PASS",
            f"学段配置加载成功：{curriculum.get('name', settings.default_level)}"
            f"（{len(curriculum.get('units', []))} 个单元）",
        ))
    except Exception as exc:  # noqa: BLE001
        results.append(("FAIL", f"学段配置加载失败：{exc}"))

    results.append((
        "PASS" if settings.llm.is_configured() else "WARN",
        "多模态 LLM 已配置" if settings.llm.is_configured()
        else "多模态 LLM 未配置（骨架阶段可暂不填，生成文案前需配置 LLM_API_KEY 等）",
    ))
    results.append((
        "PASS" if settings.volc_tts.is_configured() else "WARN",
        "火山引擎 TTS 已配置" if settings.volc_tts.is_configured()
        else "火山引擎 TTS 未配置（骨架阶段可暂不填，合成语音前需配置 VOLC_*）",
    ))

    levels = settings.list_levels()
    results.append(("PASS", f"当前可用学段：{', '.join(levels) if levels else '（无）'}"))
    return results


def _check_output_dir() -> Check:
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.output_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return "PASS", f"output 目录可写：{settings.output_dir}"
    except Exception as exc:  # noqa: BLE001
        return "FAIL", f"output 目录不可写：{exc}"


def run_init_check() -> int:
    print("=" * 64)
    print("  环境与配置自检 (--init-check)")
    print("=" * 64)

    checks: list[Check] = []
    checks.append(_check_python())
    checks.extend(_check_imports())
    checks.append(_check_env_file())
    checks.extend(_check_config())
    checks.append(_check_playwright_browsers())
    checks.append(_check_output_dir())

    n_pass = sum(1 for s, _ in checks if s == "PASS")
    n_warn = sum(1 for s, _ in checks if s == "WARN")
    n_fail = sum(1 for s, _ in checks if s == "FAIL")

    for status, msg in checks:
        print(f"[{status}] {msg}")

    print("-" * 64)
    print(f"自检完成：PASS {n_pass} / WARN {n_warn} / FAIL {n_fail}")
    if n_fail:
        print("存在 FAIL 项，请根据上方提示修复后重试。")
        return 1
    if n_warn:
        print("无 FAIL 项；WARN 项为骨架阶段可选配置，可在对应功能接入前补齐。")
    else:
        print("全部通过，环境就绪。")
    print("视频参数：{}x{} @ {}fps，默认朗读 {} 遍".format(
        settings.video.width, settings.video.height, settings.video.fps, settings.video.read_times
    ))
    print(f"默认学段：{settings.default_level}，默认题型：{settings.default_type}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="英语听力短视频生成器（骨架阶段）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--init-check",
        action="store_true",
        help="运行环境与配置自检（Python、依赖、Playwright 内核、配置校验）",
    )
    parser.add_argument("--input", metavar="PATH", help="待处理题图或素材目录（占位）")
    parser.add_argument("--level", metavar="LEVEL", help="学段标识，如 primary_g4（占位）")
    parser.add_argument("--type", dest="question_type", metavar="TYPE", help="题型标识，如 image_ab_choice（占位）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.init_check:
        return run_init_check()

    print("英语听力短视频生成器（骨架阶段）")
    print(f"  input : {args.input or '(未指定)'}")
    print(f"  level : {args.level or settings.default_level}（默认）")
    print(f"  type  : {args.question_type or settings.default_type}（默认）")
    print("提示：尚未实现完整生成流水线，请先运行 python main.py --init-check 完成环境自检。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
