"""
统一配置中心：读取 .env、校验必填项、提供全局默认参数，并按学段加载教材/考纲配置。

设计原则：
- 不写死单一学段：学段通过 level 标识映射到 config/curriculum/<level>.json。
- 不写死单一题型：题型通过 question_type 映射到 templates/<question_type>.html。
- 不写死单一画幅：画幅通过 orientation 标识映射到 config/layouts.json 的预设
  （仅支持 landscape / portrait 二选一；不提供 both 模式）。
- 所有可调参数集中在此，便于后续扩展到小学其他年级 / 初中 / 高中。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# 项目根目录（本文件位于 <root>/config/settings.py）
ROOT_DIR: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = ROOT_DIR / "config"
CURRICULUM_DIR: Path = CONFIG_DIR / "curriculum"
LAYOUTS_JSON: Path = CONFIG_DIR / "layouts.json"
TEMPLATES_DIR: Path = ROOT_DIR / "templates"
INPUT_DIR: Path = ROOT_DIR / "input"

# 仅支持二选一：严禁扩展出 both 模式
SUPPORTED_ORIENTATIONS = ("landscape", "portrait")

# 加载 .env（若存在）；不覆盖已存在的环境变量，方便 CI/命令行注入
load_dotenv(ROOT_DIR / ".env", override=False)


def _get_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError as exc:  # pragma: no cover - 配置错误时给出明确提示
        raise ValueError(f"环境变量 {name} 必须是整数，当前值：{val!r}") from exc


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError as exc:  # pragma: no cover - 配置错误时给出明确提示
        raise ValueError(f"环境变量 {name} 必须是数字，当前值：{val!r}") from exc


def _get_orientation(name: str, default: str = "landscape") -> str:
    """读取方向配置，非法值时报错提示可选范围。"""
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    v = val.strip().lower()
    if v not in SUPPORTED_ORIENTATIONS:
        raise ValueError(
            f"环境变量 {name} 必须是 {' 或 '.join(SUPPORTED_ORIENTATIONS)} 之一，当前值：{val!r}"
        )
    return v


def _is_real_value(val: str) -> bool:
    """判断配置值是否为真实值（排除空串与模板占位符，如 your_xxx_here）。"""
    if not val:
        return False
    v = val.strip().lower()
    if not v:
        return False
    # 常见占位模式：your_xxx_here / xxx_here / your-xxx-here
    if v.startswith("your_") or v.startswith("your-") or v.endswith("_here") or v.endswith("-here"):
        return False
    if "placeholder" in v or v in {"xxx", "todo", "changeme", "change_me"}:
        return False
    return True


@dataclass
class LLMConfig:
    """多模态大模型（素材识别 + 分学段文案生成）配置。"""

    api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o"))
    timeout: int = field(default_factory=lambda: _get_int("LLM_TIMEOUT", 60))

    def is_configured(self) -> bool:
        return _is_real_value(self.api_key)


@dataclass
class VolcTTSConfig:
    """火山引擎 TTS 配置（V3 seed-tts-2.0 大模型接口，默认值可被学段配置覆盖）。"""

    # V3 新版鉴权：API Key + Resource-Id（语音技术控制台 -> 大模型语音合成 -> API Key）
    api_key: str = field(default_factory=lambda: os.getenv("VOLC_API_KEY", ""))
    resource_id: str = field(default_factory=lambda: os.getenv("VOLC_RESOURCE_ID", "seed-tts-2.0"))
    # V3 兼容音色（必须是 seed-tts-2.0 音色列表中的发音人）
    voice_type: str = field(
        default_factory=lambda: os.getenv(
            "VOLC_VOICE_TYPE", "zh_female_yingyujiaoxue_uranus_bigtts"
        )
    )
    # 以下字段为旧版 V1 鉴权遗留，V3 已废弃，仅保留读取以兼容旧 .env
    access_key: str = field(default_factory=lambda: os.getenv("VOLC_ACCESS_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("VOLC_SECRET_KEY", ""))
    app_id: str = field(default_factory=lambda: os.getenv("VOLC_APP_ID", "") or os.getenv("VOLC_TTS_APPID", ""))
    cluster: str = field(default_factory=lambda: os.getenv("VOLC_CLUSTER", "volcano_tts"))

    def is_configured(self) -> bool:
        # V3 只需要 API Key + Resource-Id；旧版 APPID/Token 鉴权已废弃
        return _is_real_value(self.api_key) and _is_real_value(self.resource_id)


@dataclass
class VideoConfig:
    """视频默认参数（宽高由当前 orientation 决定；fps 等参数固定）。"""

    orientation: str = field(default_factory=lambda: _get_orientation("VIDEO_ORIENTATION", "landscape"))
    width: int = 0   # 运行时由 load_layout 覆盖；0 占位
    height: int = 0  # 运行时由 load_layout 覆盖；0 占位
    fps: int = field(default_factory=lambda: _get_int("VIDEO_FPS", 30))
    read_times: int = field(default_factory=lambda: _get_int("DEFAULT_READ_TIMES", 2))
    countdown_seconds: int = field(default_factory=lambda: _get_int("COUNTDOWN_SECONDS", 3))
    reveal_delay_seconds: int = field(default_factory=lambda: _get_int("REVEAL_DELAY_SECONDS", 1))
    reveal_hold_seconds: int = field(default_factory=lambda: _get_int("REVEAL_HOLD_SECONDS", 6))


@dataclass
class IntroSfxConfig:
    """全局开场提示音配置：朗读前自动拼接「叮」+ 停顿，模拟真实考场节奏。"""

    enable: bool = field(default_factory=lambda: _get_bool("ENABLE_INTRO_SFX", True))
    path: str = field(default_factory=lambda: os.getenv("INTRO_SFX_PATH", "assets/sfx/intro_ding.wav"))
    pause_sec: float = field(default_factory=lambda: _get_float("INTRO_SFX_PAUSE_SEC", 0.5))

    @property
    def resolved_path(self) -> Path:
        """返回提示音绝对路径（相对路径基于项目根解析）。"""
        p = Path(self.path)
        return p if p.is_absolute() else ROOT_DIR / p

    def is_available(self) -> bool:
        return bool(self.enable) and self.resolved_path.exists()


@dataclass
class Settings:
    """全局设置聚合。"""

    default_level: str = field(default_factory=lambda: os.getenv("DEFAULT_LEVEL", "primary_g4"))
    default_type: str = field(default_factory=lambda: os.getenv("DEFAULT_TYPE", "image_ab_choice"))
    output_dir: Path = field(
        default_factory=lambda: ROOT_DIR / os.getenv("OUTPUT_DIR", "output")
    )
    llm: LLMConfig = field(default_factory=LLMConfig)
    volc_tts: VolcTTSConfig = field(default_factory=VolcTTSConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    intro_sfx: IntroSfxConfig = field(default_factory=IntroSfxConfig)

    # ---------- 校验 ----------
    def validate(self) -> List[str]:
        """返回缺失/不合法配置的提示列表；空列表表示通过。"""
        errors: List[str] = []

        # 画幅方向必须是受支持的二选一
        if self.video.orientation not in SUPPORTED_ORIENTATIONS:
            errors.append(
                f"VIDEO_ORIENTATION 必须是 {' 或 '.join(SUPPORTED_ORIENTATIONS)} 之一，"
                f"当前值：{self.video.orientation!r}"
            )

        # layouts.json 必须存在且包含当前 orientation
        if not LAYOUTS_JSON.exists():
            errors.append(f"画幅预设文件不存在：{LAYOUTS_JSON}")
        else:
            try:
                with LAYOUTS_JSON.open("r", encoding="utf-8") as f:
                    layouts = json.load(f)
                if self.video.orientation not in layouts:
                    errors.append(
                        f"config/layouts.json 中缺少 {self.video.orientation!r} 预设"
                    )
            except json.JSONDecodeError as exc:
                errors.append(f"config/layouts.json 解析失败：{exc}")

        # 学段配置文件必须存在
        if not self.curriculum_path(self.default_level).exists():
            errors.append(
                f"默认学段 default_level={self.default_level!r} 对应的配置文件不存在："
                f"config/curriculum/{self.default_level}.json"
            )

        # 题型模板必须存在
        if not self.template_path(self.default_type).exists():
            errors.append(
                f"默认题型 default_type={self.default_type!r} 对应的模板不存在："
                f"templates/{self.default_type}.html"
            )

        # 视频参数合理性
        # 注意：width/height 为 0 表示尚未调用 apply_layout()，不算错
        if 0 < self.video.width <= 0 or 0 < self.video.height <= 0:
            errors.append("VIDEO_WIDTH / VIDEO_HEIGHT 必须为正整数")
        if not (1 <= self.video.fps <= 120):
            errors.append("VIDEO_FPS 建议在 1~120 之间")
        if self.video.read_times < 1:
            errors.append("DEFAULT_READ_TIMES 必须 >= 1")

        return errors

    # ---------- 画幅布局加载 ----------
    def load_layout(self, orientation: Optional[str] = None) -> Dict[str, Any]:
        """从 config/layouts.json 读取指定 orientation 的布局预设，返回完整 dict。"""
        target = orientation or self.video.orientation
        if target not in SUPPORTED_ORIENTATIONS:
            raise ValueError(
                f"orientation 必须是 {' 或 '.join(SUPPORTED_ORIENTATIONS)} 之一，当前值：{target!r}"
            )
        if not LAYOUTS_JSON.exists():
            raise FileNotFoundError(f"画幅预设文件不存在：{LAYOUTS_JSON}")
        with LAYOUTS_JSON.open("r", encoding="utf-8") as f:
            layouts = json.load(f)
        if target not in layouts:
            raise KeyError(f"config/layouts.json 中缺少 {target!r} 预设")
        return layouts[target]

    def apply_layout(self, orientation: Optional[str] = None) -> Dict[str, Any]:
        """读取并应用画幅预设到 self.video（覆盖 width/height），返回预设 dict。"""
        target = orientation or self.video.orientation
        layout = self.load_layout(target)
        self.video.orientation = target
        self.video.width = int(layout["width"])
        self.video.height = int(layout["height"])
        return layout

    # ---------- 路径辅助 ----------
    def curriculum_path(self, level: str) -> Path:
        return CURRICULUM_DIR / f"{level}.json"

    def template_path(self, question_type: str) -> Path:
        return TEMPLATES_DIR / f"{question_type}.html"

    # ---------- 学段配置加载 ----------
    def load_curriculum(self, level: Optional[str] = None) -> Dict[str, Any]:
        """
        加载指定学段的教材/考纲配置。
        若 level 为空则使用 DEFAULT_LEVEL。
        返回解析后的 dict；文件不存在时抛出 FileNotFoundError。
        """
        target = level or self.default_level
        path = self.curriculum_path(target)
        if not path.exists():
            raise FileNotFoundError(f"学段配置文件不存在：{path}")
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # 兜底：保证关键子结构存在，避免下游 KeyError
        data.setdefault("level", target)
        data.setdefault("tts", {})
        data.setdefault("units", [])
        data.setdefault("core_vocabulary", [])
        data.setdefault("core_sentences", [])
        data.setdefault("contrast_dimensions", [])
        return data

    def list_levels(self) -> List[str]:
        """列出当前可用的所有学段标识（基于 curriculum 目录下的 json 文件）。"""
        if not CURRICULUM_DIR.exists():
            return []
        return sorted(p.stem for p in CURRICULUM_DIR.glob("*.json"))


# 全局单例
settings = Settings()
