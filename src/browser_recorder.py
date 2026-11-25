"""
Playwright 浏览器无头录制模块（骨架）。

职责：
1. 根据 question_type 加载对应 HTML 模板（templates/<question_type>.html）。
2. 启动无头 Chromium，设置 viewport 为竖屏分辨率（默认 1080x1920）。
3. 通过 window.Stage 接口驱动舞台状态切换：loadQuestion -> countdown -> playAudio -> revealAnswer。
4. 同步音频与画面状态，录制页面生成视频文件（不依赖本地 FFmpeg）。
5. 导出 mp4/webm 到 output 目录。

当前阶段：定义流程骨架与关键 TODO，暂不实现完整录制细节。

录制方案说明（Playwright 两种可行路线，后续二选一或结合）：
- 路线 A：Playwright 内置 record_video（context.new_context(record_video_dir=..., record_video_size=...)），
  导出 webm，再用浏览器 MediaRecorder 或服务端转封装。注意：内置录制不包含音频，需另行处理音轨。
- 路线 B：使用 Chrome DevTools Protocol (CDP) 的 Page.startScreencast 逐帧采集 + Web Audio 采集，
  自行合成（纯 Python 合成较复杂）；或在页面内用 MediaRecorder 录制 canvas+audio stream 并通过
  page.evaluate 传回，导出 webm。
- 音频同步：优先在页面内用 <audio> 播放，并在录制的 media stream 中纳入 audio track；
  或在 Python 侧用 ffmpeg（不允许本地 FFmpeg 时排除）。本项目约定不依赖本地 FFmpeg，
  故采用浏览器端 MediaRecorder 录制含音轨的 webm 路线。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from config.settings import Settings, settings
from src.question_generator import Question


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
        """
        录制一道题的视频，返回输出视频路径。

        流程：
            1. 校验模板存在：self.cfg.template_path(question.question_type)
            2. 启动无头 Chromium，viewport = (video.width, video.height)
            3. 打开模板 HTML（file:// 绝对路径）
            4. page.evaluate("window.Stage.loadQuestion(...)") 注入题目数据
            5. 开始录制（见模块顶部方案说明）
            6. await page.evaluate("window.Stage.startCountdown(n)")
            7. 注入音频并播放：设置 audio.src，或调用 window.Stage.playAudio(src)
            8. 等待 reveal_delay 后 window.Stage.revealAnswer(...)
            9. 停止录制，保存到 output_video_path
            10. 关闭浏览器

        TODO（真实实现）：
        - 录制方案选型与实现（建议路线 B：页面内 MediaRecorder 录制 canvas+audio）。
        - 音画同步：确保 audio.play() 与录制 start 时间对齐，必要时用 performance.now() 打点。
        - 等待音频播放结束：监听 window.Stage 状态回调或 audio 'ended' 事件。
        - 错误处理与超时：页面加载、音频加载失败时的兜底。
        - 清理临时文件。
        """
        template_path = self.cfg.template_path(question.question_type)
        if not template_path.exists():
            raise FileNotFoundError(f"题型模板不存在：{template_path}")

        out = Path(output_video_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # ---- 占位：真实录制逻辑待接入 ----
        # TODO: 接入 Playwright（from playwright.sync_api import sync_playwright）
        # 此处仅创建空文件占位，避免流程中断（正式接入后删除）。
        if not out.exists():
            out.write_bytes(b"")

        return out

    # ------------------------------------------------------------------
    def _stage_run_flow_code(self, question: Question, audio_src: str) -> str:
        """
        生成在浏览器端执行的 JS：调用 window.Stage.runFlow 完成单题流水线。
        返回 JS 字符串，供 page.evaluate 使用。
        （分步控制录制起止时，也可拆成多次 evaluate。）
        """
        payload = question.to_stage_payload()
        import json

        return (
            "window.Stage.loadQuestion(" + json.dumps(payload, ensure_ascii=False) + ");"
            " return window.Stage.runFlow({"
            f"countdown: {self.cfg.video.countdown_seconds},"
            f" audioSrc: {json.dumps(audio_src, ensure_ascii=False)},"
            f" revealDelay: {self.cfg.video.reveal_delay_seconds * 1000},"
            f" answer: {json.dumps(question.answer, ensure_ascii=False)},"
            f" explain: {json.dumps(question.explain, ensure_ascii=False)}"
            " });"
        )
