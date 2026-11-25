"""
Playwright 浏览器无头录制模块（真实实现，不依赖本地 FFmpeg）。

录制方案：浏览器端 MediaRecorder 采集 canvas.captureStream 视频轨 + WebAudio 音频轨，
合成含音轨的 webm，再把 base64 回传 Python 写入文件。全程不调用系统 ffmpeg。
"""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Optional

from config.settings import Settings, settings
from src.question_generator import Question

# 系统 Chrome / Chromium 兜底路径（当 Playwright 捆绑内核不可用时使用）
FALLBACK_BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


class BrowserRecorder:
    """浏览器无头录制器。"""

    def __init__(self, cfg: Optional[Settings] = None):
        self.cfg = cfg or settings

    # ------------------------------------------------------------------
    def record(
        self,
        question: Question,
        audio_path: str | Path,
        output_video_path: str | Path,
    ) -> Path:
        """录制一道题的视频，返回输出视频路径（.webm）。"""
        template_path = self.cfg.template_path(question.question_type)
        if not template_path.exists():
            raise FileNotFoundError(f"题型模板不存在：{template_path}")

        audio_src = self._audio_to_data_url(audio_path)
        out = Path(output_video_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            executable = self._resolve_executable(p)
            browser = p.chromium.launch(
                executable_path=executable,
                headless=True,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--use-fake-ui-for-media-stream",
                    "--enable-features=MediaRecorder",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = browser.new_context(
                    viewport={"width": self.cfg.video.width, "height": self.cfg.video.height},
                    device_scale_factor=1,
                    record_video_dir=None,  # 不使用 Playwright 内置录制（无音频），用页面内 MediaRecorder
                )
                page = context.new_page()
                page.set_default_timeout(120000)
                page.goto(template_path.resolve().as_uri())

                payload = question.to_stage_payload()
                page.evaluate(
                    "async (p) => { await window.Stage.loadQuestion(p); }", payload
                )

                opts = {
                    "countdown": self.cfg.video.countdown_seconds,
                    "audioSrc": audio_src,
                    "revealDelay": self.cfg.video.reveal_delay_seconds * 1000,
                    "revealHold": self.cfg.video.reveal_hold_seconds * 1000,
                    "answer": question.answer,
                    "explain": question.explain,
                }
                result = page.evaluate(
                    "async (o) => { return await window.Stage.runFlow(o); }", opts
                )

                data_url = (result or {}).get("base64", "")
                if not data_url:
                    raise RuntimeError("浏览器录制未返回视频数据（MediaRecorder 可能未成功）")
                video_bytes = self._decode_data_url(data_url)
                if len(video_bytes) < 1024:
                    raise RuntimeError(f"录制产物过小（{len(video_bytes)} bytes），疑似录制失败")
                out.write_bytes(video_bytes)
                context.close()
            finally:
                browser.close()

        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_executable(p) -> Optional[str]:
        """优先使用 Playwright 捆绑 Chromium；不存在则回退系统 Chrome。"""
        try:
            bundled = p.chromium.executable_path
            if bundled and Path(bundled).exists():
                return bundled
        except Exception:
            pass
        for cand in FALLBACK_BROWSERS:
            if Path(cand).exists():
                return cand
        return None  # 让 Playwright 自行尝试（可能报错）

    @staticmethod
    def _audio_to_data_url(audio_path: str | Path) -> str:
        p = Path(audio_path)
        if not p.exists() or p.stat().st_size == 0:
            return ""
        mime = mimetypes.guess_type(str(p))[0] or "audio/mpeg"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _decode_data_url(data_url: str) -> bytes:
        # data:<mime>;base64,<payload>  —— 注意 mime 可能含逗号（如 codecs=vp9,opus），
        # 必须按 ";base64," 切分，不能按第一个逗号切。
        sep = ";base64,"
        idx = data_url.find(sep)
        payload = data_url[idx + len(sep):] if idx != -1 else data_url
        payload = "".join(payload.split())  # 去空白
        payload += "=" * (-len(payload) % 4)  # 补 padding
        return base64.b64decode(payload)
