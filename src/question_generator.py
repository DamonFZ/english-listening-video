"""
素材分析 + 分学段听力文案 / 答案生成模块。

职责：
1. 接收一张 A/B 对比图（复合图）或两张独立图，以及学段配置。
2. 调用多模态大模型 API 识别 A 与 B 的视觉差异，结合学段教材/考纲
   （config/curriculum/<level>.json）生成符合该学段水平的英语听力文案、
   正确选项（A/B）、中文翻译与解析。
3. 输出结构化 Question；当未配置 LLM 时走确定性兜底，保证全链路可跑通。
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from config.settings import LLMConfig, Settings, settings


ImageInput = Union[str, Path, bytes]


@dataclass
class ChoiceOption:
    """单个选项（A 或 B）。"""

    key: str  # "A" / "B"
    image: str = ""  # 图片路径或 dataURL
    description: str = ""


@dataclass
class Question:
    """一道听力题的统一数据结构。"""

    question_type: str
    level: str
    level_badge: str = ""
    title: str = ""

    image: str = ""  # 复合图（A/B 同图）路径或 dataURL；优先使用
    subtitle_en: str = ""  # 英文听力题干（朗读文本）
    subtitle_zh: str = ""  # 中文释义
    options: List[ChoiceOption] = field(default_factory=list)
    answer: str = ""  # "A" / "B"
    explain: str = ""  # 英文解析

    audio_text: str = ""  # 默认等于 subtitle_en
    read_times: int = 2
    source: str = "llm"  # "llm" 或 "fallback"，便于调试

    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def to_stage_payload(self) -> Dict[str, Any]:
        """转换为 templates/image_ab_choice.html 的 window.Stage.loadQuestion 入参。"""
        payload: Dict[str, Any] = {
            "levelBadge": self.level_badge,
            "title": self.title,
            "image": self.image,
            "subtitleEn": self.subtitle_en,
            "subtitleZh": self.subtitle_zh,
            "answer": self.answer,
            "explain": self.explain,
        }
        for opt in self.options:
            if opt.key.upper() == "A":
                payload["imageA"] = opt.image
            elif opt.key.upper() == "B":
                payload["imageB"] = opt.image
        return payload


def _image_to_data_url(image: ImageInput) -> str:
    """把图片（路径或字节）转成 base64 data URL。"""
    if isinstance(image, (bytes, bytearray)):
        data = bytes(image)
        mime = "image/jpeg"
    else:
        p = Path(image)
        if not p.exists():
            raise FileNotFoundError(f"图片不存在：{p}")
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


class QuestionGenerator:
    """素材分析 + 分学段文案生成器。"""

    SYSTEM_PROMPT = (
        "You are an expert English teacher for K-12. You analyze an A/B comparison image "
        "(panel A on the left, panel B on the right) and write ONE listening comprehension "
        "question suitable for the given grade level. Always reply with a single JSON object "
        "and nothing else (no markdown, no code fences)."
    )

    def __init__(self, llm_config: Optional[LLMConfig] = None, cfg: Optional[Settings] = None):
        self.cfg = cfg or settings
        self.llm = llm_config or self.cfg.llm

    # ------------------------------------------------------------------
    def generate(
        self,
        image: ImageInput,
        image_b: Optional[ImageInput] = None,
        level: Optional[str] = None,
        question_type: Optional[str] = None,
        save_debug: Optional[Union[str, Path]] = None,
    ) -> Question:
        """
        主入口：分析图片生成一道听力题。

        参数：
            image: 复合图路径（含 A/B 两栏），或图 A 的路径/字节。
            image_b: 可选，图 B；若提供则视为 A/B 两张独立图。
            level: 学段标识，默认 .env DEFAULT_LEVEL。
            question_type: 题型，默认 .env DEFAULT_TYPE。
            save_debug: 若提供路径，把原始模型响应 / 兜底信息写入该 JSON 文件。
        """
        target_level = level or self.cfg.default_level
        target_type = question_type or self.cfg.default_type
        curriculum = self.cfg.load_curriculum(target_level)

        image_data_url = _image_to_data_url(image)
        image_b_data_url = _image_to_data_url(image_b) if image_b else ""

        base = Question(
            question_type=target_type,
            level=target_level,
            level_badge=curriculum.get("name", target_level),
            title="看图二选一",
            image=image_data_url,
            options=[
                ChoiceOption(key="A", image=image_data_url),
                ChoiceOption(key="B", image=image_b_data_url or image_data_url),
            ],
            read_times=int(curriculum.get("tts", {}).get("read_times", self.cfg.video.read_times)),
        )

        if self.llm.is_configured():
            question = self._generate_with_llm(base, curriculum, image_data_url, image_b_data_url)
        else:
            question = self._generate_fallback(base, curriculum, image)

        if not question.audio_text:
            question.audio_text = question.subtitle_en
        self._validate_question(question)

        if save_debug:
            self._save_debug(save_debug, question, curriculum)
        return question

    # ------------------------------------------------------------------
    def _build_curriculum_context(self, curriculum: Dict[str, Any]) -> str:
        """把学段配置压缩成提示词上下文。"""
        lines: List[str] = []
        lines.append(f"Grade level: {curriculum.get('name', curriculum.get('level'))}")
        units = curriculum.get("units", []) or []
        if units:
            lines.append("Units / topics:")
            for u in units[:6]:
                vocab = (u.get("vocabulary") or [])[:8]
                sents = (u.get("sentences") or [])[:2]
                lines.append(
                    f"- {u.get('title')} ({u.get('topic')}): vocab={vocab}; "
                    f"example sentences={sents}"
                )
        core_vocab = (curriculum.get("core_vocabulary") or [])[:15]
        if core_vocab:
            lines.append(f"Core vocabulary: {core_vocab}")
        dims = curriculum.get("contrast_dimensions") or []
        if dims:
            lines.append(
                "Contrast dimensions to consider: "
                + ", ".join(d.get("dimension", "") for d in dims)
            )
        return "\n".join(lines)

    def _build_user_prompt(self, curriculum: Dict[str, Any], has_image_b: bool) -> str:
        side_hint = (
            "The image contains two panels: A on the LEFT, B on the RIGHT."
            if not has_image_b
            else "Two separate images are provided: the first is A, the second is B."
        )
        schema = (
            'Return ONLY a JSON object with these keys: '
            '"correct_option" (value "A" or "B"), '
            '"listening_script" (a short natural English sentence the student hears, '
            'matching the grade level, e.g. "Which picture is red?"), '
            '"cn_translation" (Chinese translation of the listening script), '
            '"reason" (a short English explanation why that option is correct), '
            '"title" (optional Chinese/English short title for the slide).'
        )
        return (
            f"{self._build_curriculum_context(curriculum)}\n\n"
            f"{side_hint} Decide which panel (A or B) matches the target, "
            f"then write a listening question. {schema}"
        )

    # ------------------------------------------------------------------
    def _generate_with_llm(
        self,
        base: Question,
        curriculum: Dict[str, Any],
        image_a_url: str,
        image_b_url: str,
    ) -> Question:
        """调用 OpenAI 兼容多模态接口生成结构化结果。"""
        prompt = self._build_user_prompt(curriculum, has_image_b=bool(image_b_url))
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.append({"type": "image_url", "image_url": {"url": image_a_url}})
        if image_b_url:
            content.append({"type": "image_url", "image_url": {"url": image_b_url}})

        payload: Dict[str, Any] = {
            "model": self.llm.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.3,
            "max_tokens": 600,
        }
        headers = {
            "Authorization": f"Bearer {self.llm.api_key}",
            "Content-Type": "application/json",
        }
        url = self.llm.base_url.rstrip("/") + "/chat/completions"

        resp = requests.post(url, headers=headers, json=payload, timeout=self.llm.timeout)
        resp.raise_for_status()
        body = resp.json()
        raw = body["choices"][0]["message"]["content"]

        data = self._parse_json(raw)
        base.answer = str(data.get("correct_option", "")).strip().upper()[:1]
        base.subtitle_en = str(data.get("listening_script", "")).strip()
        base.subtitle_zh = str(data.get("cn_translation", "")).strip()
        base.explain = str(data.get("reason", "")).strip()
        if data.get("title"):
            base.title = str(data["title"]).strip()
        base.source = "llm"
        base.extra["raw_response"] = raw
        return base

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        """从模型输出中稳健解析 JSON（容忍 markdown 代码块与前后缀文本）。"""
        if not raw:
            return {}
        text = raw.strip()
        # 去除 ```json ... ``` 围栏
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        else:
            # 截取第一个 { 到最后一个 }
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start : end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------
    def _generate_fallback(
        self, base: Question, curriculum: Dict[str, Any], image: ImageInput
    ) -> Question:
        """
        未配置 LLM 时的确定性兜底：基于文件名/学段生成可用样题，保证全链路可跑。
        不进行真实图像识别，answer 由文件名哈希决定（便于复现）。
        """
        name = Path(image).stem if isinstance(image, (str, Path)) else "sample"
        topic = self._topic_from_filename(name)
        scripts = {
            "shopping": ("How much is the bag?", "这个包多少钱？", "Picture B shows the price."),
            "colors": ("Which picture is red?", "哪张图片是红色的？", "Picture A is red."),
            "numbers": ("How many apples are there?", "有多少个苹果？", "Picture B has three apples."),
            "animal": ("Which picture shows a cat?", "哪张图片是一只猫？", "Picture A is a cat."),
        }
        en, zh, reason = scripts.get(
            topic,
            ("Listen and choose. Which one is correct?", "听一听，选一选。哪一个是正确的？",
             "The correct option matches the listening script."),
        )
        # 基于文件名哈希稳定决定答案 A/B
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()
        answer = "A" if int(digest[0], 16) % 2 == 0 else "B"

        base.subtitle_en = en
        base.subtitle_zh = zh
        base.explain = reason
        base.answer = answer
        base.source = "fallback"
        base.extra["topic_guess"] = topic
        base.extra["note"] = (
            "LLM 未配置，使用确定性兜底样例；填入 LLM_API_KEY 后将进行真实图像识别。"
        )
        return base

    @staticmethod
    def _topic_from_filename(name: str) -> str:
        n = name.lower()
        for key in ("shopping", "price", "bag", "money", "yuan"):
            if key in n:
                return "shopping"
        for key in ("color", "red", "blue", "green"):
            if key in n:
                return "colors"
        for key in ("number", "count", "apple", "many"):
            if key in n:
                return "numbers"
        for key in ("cat", "dog", "animal", "bird"):
            if key in n:
                return "animal"
        return "general"

    # ------------------------------------------------------------------
    def _save_debug(
        self, path: Union[str, Path], question: Question, curriculum: Dict[str, Any]
    ) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # dataURL 太长，调试文件里只保留占位
        d = question.to_dict()
        if d.get("image", "").startswith("data:"):
            d["image"] = f"<data_url omitted, {len(question.image)} chars>"
        for opt in d.get("options", []):
            if opt.get("image", "").startswith("data:"):
                opt["image"] = "<data_url omitted>"
        payload = {
            "question": d,
            "curriculum_name": curriculum.get("name"),
            "curriculum_level": curriculum.get("level"),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def _validate_question(self, question: Question) -> None:
        keys = {o.key.upper() for o in question.options}
        if question.answer and question.answer.upper() not in keys:
            raise ValueError(f"答案 {question.answer!r} 不在选项 {keys} 中")
        if not question.subtitle_en:
            raise ValueError("subtitle_en 不能为空")
