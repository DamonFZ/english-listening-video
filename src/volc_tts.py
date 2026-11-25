"""
火山引擎 TTS 语音合成调用模块（V3 seed-tts-2.0 大模型接口）。

接口：https://openspeech.bytedance.com/api/v3/tts/unidirectional （HTTP Chunked 单向流式）
鉴权：新版控制台「API Key 快速接入」，请求头 X-Api-Key + X-Api-Resource-Id。
      旧版 V1 的 APPID + Access Token（Bearer/HMAC256）鉴权已废弃，本模块不再使用。

请求体（不含 app 节点）：
  {
    "user": {"uid": "..."},
    "req_params": {
      "text": "...",
      "speaker": "<V3 兼容音色>",
      "audio_params": {"format": "mp3", "sample_rate": 24000, "speech_rate": 15},
      "additions": "<json string, e.g. {\"silence_duration\": 500}>"
    }
  }
注意：additions 在 V3 协议中是 jsonstring（字符串），不是对象，否则服务端会报
  "cannot unmarshal object into ... req_params.additions of type string"。

响应：HTTP chunked 流式，每行一段 JSON，含 code/message/data（base64 音频分片）。
      逐行解析并聚合所有 data 字段，最终 base64 解码拼接成完整音频。

语速/停顿：
  - speed_ratio 由学段 curriculum.tts.speed_ratio 控制（范围 0.5~2.0），
    换算为 V3 speech_rate（范围 [-50,100]，0=1.0x，100=2.0x，-50=0.5x）。
  - read_times > 1 时，把文本重复 read_times 次、用 ". " 连接（利用句末自然停顿），
    并通过 additions.silence_duration 增大句尾停顿。
"""
from __future__ import annotations

import base64
import json
import math
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from config.settings import Settings, VolcTTSConfig, settings

TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
# V3 speech_rate 取值范围：[-50, 100]，100=2.0x，-50=0.5x，0=1.0x
SPEED_RATE_MIN = -50
SPEED_RATE_MAX = 100


class VolcTTSError(RuntimeError):
    """火山 TTS 调用失败（含鉴权失败、参数错误、无音频返回等）。"""


@dataclass
class TTSParams:
    """TTS 请求参数（学段配置覆盖默认值）。"""

    voice_type: str
    speed_ratio: float = 1.0
    pause_ms: int = 500
    volume: float = 1.0
    pitch: float = 1.0
    encoding: str = "mp3"
    sample_rate: int = 24000
    read_times: int = 2

    @classmethod
    def from_curriculum(
        cls,
        curriculum: Dict[str, Any],
        default_config: VolcTTSConfig,
        default_read_times: int = 2,
    ) -> "TTSParams":
        tts = curriculum.get("tts", {}) or {}
        speed = float(tts.get("speed_ratio", 1.0))
        speed = max(0.5, min(2.0, speed))  # 语义范围，越界钳制避免换算异常
        return cls(
            voice_type=tts.get("voice_type") or default_config.voice_type,
            speed_ratio=speed,
            pause_ms=int(tts.get("pause_ms", 500)),
            read_times=int(tts.get("read_times", default_read_times)),
        )


class VolcTTS:
    """火山引擎 TTS V3 客户端（seed-tts-2.0）。"""

    def __init__(self, config: Optional[VolcTTSConfig] = None, cfg: Optional[Settings] = None):
        self.cfg = cfg or settings
        self.config = config or self.cfg.volc_tts
        self._session = requests.Session()

    # ------------------------------------------------------------------
    def synthesize(
        self,
        text: str,
        output_path: str | Path,
        level: Optional[str] = None,
        params: Optional[TTSParams] = None,
    ) -> Path:
        """把 text 合成为音频文件写入 output_path，返回路径。失败抛 VolcTTSError。"""
        if not self.config.is_configured():
            raise VolcTTSError(
                "火山引擎 TTS V3 未配置：请在 .env 中填写 VOLC_API_KEY 与 VOLC_RESOURCE_ID"
                "（语音技术控制台 -> 大模型语音合成 -> API Key 快速接入）。"
            )

        curriculum: Dict[str, Any] = {}
        if level:
            curriculum = self.cfg.load_curriculum(level)
        if params is None:
            params = TTSParams.from_curriculum(
                curriculum, self.config, default_read_times=self.cfg.video.read_times
            )

        final_text = self._repeat_text(text, params.read_times)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        audio_bytes = self._request(final_text, params)
        out.write_bytes(audio_bytes)
        return out

    # ------------------------------------------------------------------
    def _request(self, text: str, params: TTSParams) -> bytes:
        # V3 语速换算：speed_ratio(0.5~2.0) -> speech_rate(-50~100)，0 对应 1.0x
        speech_rate = round((params.speed_ratio - 1.0) * 100)
        speech_rate = max(SPEED_RATE_MIN, min(SPEED_RATE_MAX, speech_rate))

        additions = {
            "silence_duration": min(30000, max(0, int(params.pause_ms))),
        }
        body: Dict[str, Any] = {
            "user": {"uid": "english_listening_video"},
            "req_params": {
                "text": text,
                "speaker": params.voice_type,
                "audio_params": {
                    "format": params.encoding,
                    "sample_rate": params.sample_rate,
                    "speech_rate": speech_rate,
                },
                # additions 在 V3 协议中是 jsonstring，必须序列化为字符串
                "additions": json.dumps(additions, ensure_ascii=False),
            },
        }

        headers = {
            "X-Api-Key": self.config.api_key,
            "X-Api-Resource-Id": self.config.resource_id,
            "X-Api-Request-Id": self._gen_request_id(),
            "Content-Type": "application/json",
        }

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self._session.post(
                    TTS_URL,
                    headers=headers,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    stream=True,
                    timeout=60,
                )
                break
            except requests.RequestException as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise VolcTTSError(f"火山 TTS V3 网络请求失败：{last_exc}")

        if r.status_code != 200:
            raise VolcTTSError(
                f"火山 TTS V3 调用失败：HTTP {r.status_code}, "
                f"logid={r.headers.get('X-Tt-Logid')}, body={r.text[:300]!r}"
            )

        # 流式逐行解析，聚合所有 data 分片的 base64 音频
        audio_chunks: list[bytes] = []
        last_code: Any = 0
        last_message: str = ""
        last_logid: Optional[str] = r.headers.get("X-Tt-Logid")
        raw_preview: list[str] = []

        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            if len(raw_preview) < 2:
                raw_preview.append(line[:160])
            try:
                chunk = json.loads(line)
            except (ValueError, TypeError):
                # 非 JSON 行忽略（极少数服务端可能混入心跳）
                continue
            if "code" in chunk:
                last_code = chunk.get("code")
            if "message" in chunk:
                last_message = chunk.get("message") or last_message
            data = chunk.get("data")
            if isinstance(data, str) and data:
                try:
                    audio_chunks.append(base64.b64decode(data))
                except Exception as e:  # pragma: no cover - 防御性
                    raise VolcTTSError(f"火山 TTS V3 返回的音频分片 base64 解码失败：{e}") from e

        if not audio_chunks:
            raise VolcTTSError(
                "火山 TTS V3 未返回任何音频数据："
                f"code={last_code!r}, message={last_message!r}, "
                f"logid={last_logid}, preview={raw_preview}"
            )

        return b"".join(audio_chunks)

    # ------------------------------------------------------------------
    @staticmethod
    def _repeat_text(text: str, read_times: int) -> str:
        n = max(1, int(read_times))
        if n == 1:
            return text
        # 重复朗读：用句点+空格连接，依赖句末自然停顿 + silence_duration
        return ". ".join([text.strip().rstrip(".") for _ in range(n)]) + "."

    @staticmethod
    def _gen_request_id() -> str:
        return f"elv-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


# ----------------------------------------------------------------------
# 本地兜底音频生成（仅当火山 TTS 不可用时使用，保证录制管线可跑通）
# 生成一段带振幅包络的正弦波 WAV（非语音，但为有效音频），无任何第三方依赖。
# ----------------------------------------------------------------------
def generate_tone_wav(output_path: str | Path, duration: float = 3.0, sample_rate: int = 24000) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = int(duration * sample_rate)
    freq = 440.0
    frames = bytearray()
    for i in range(n):
        t = i / sample_rate
        # 简单的两音高交替 + 淡入淡出，听感不那么单调
        f = freq if (i // (sample_rate // 2)) % 2 == 0 else freq * 1.5
        env = min(1.0, t / 0.05) * min(1.0, (duration - t) / 0.1)
        env = max(0.0, env)
        val = 0.3 * env * math.sin(2 * math.pi * f * t)
        frames += struct.pack("<h", int(val * 32767))
    # WAV header
    data_size = len(frames)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", data_size,
    )
    out.write_bytes(header + frames)
    return out
