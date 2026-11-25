"""
火山引擎 TTS 语音合成调用模块（真实实现）。

鉴权（二选一，自动尝试）：
  1. Bearer Token：Authorization: Bearer;<access_token>，body.app.token 同值。
  2. HMAC256 签名：Authorization: HMAC256; access_token="<AK>"; mac="<sig>"; h="Host"。

语速/停顿：
  - speed_ratio 直接由学段 curriculum.tts.speed_ratio 控制（范围 0.5~2.0）。
  - read_times > 1 时，把文本重复 read_times 次、用 ". " 连接（利用句末自然停顿），
    并设置 silence_duration 增大句间停顿，避免 SSML break 标签在部分音色下不兼容。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
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

TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"
TTS_HOST = "openspeech.bytedance.com"


class VolcTTSError(RuntimeError):
    """火山 TTS 调用失败（含鉴权失败）。"""


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
        speed = max(0.5, min(2.0, speed))  # 火山允许范围，越界钳制避免报错
        return cls(
            voice_type=tts.get("voice_type") or default_config.voice_type,
            speed_ratio=speed,
            pause_ms=int(tts.get("pause_ms", 500)),
            read_times=int(tts.get("read_times", default_read_times)),
        )


class VolcTTS:
    """火山引擎 TTS 客户端。"""

    def __init__(self, config: Optional[VolcTTSConfig] = None, cfg: Optional[Settings] = None):
        self.cfg = cfg or settings
        self.config = config or self.cfg.volc_tts

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
                "火山引擎 TTS 未配置：请在 .env 中填写 VOLC_ACCESS_KEY/VOLC_SECRET_KEY/VOLC_APP_ID "
                "（或 VOLC_API_KEY）。"
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
        body: Dict[str, Any] = {
            "app": {
                "appid": self.config.app_id,
                "cluster": self.config.cluster,
            },
            "user": {"uid": "english_listening_video"},
            "audio": {
                "voice_type": params.voice_type,
                "encoding": params.encoding,
                "speed_ratio": params.speed_ratio,
                "volume_ratio": params.volume,
                "pitch_ratio": params.pitch,
                "rate": params.sample_rate,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "reqid": self._gen_reqid(),
                "text": text,
                "operation": "query",
                "silence_duration": str(min(2000, max(0, params.pause_ms))),
            },
        }

        last_err: Optional[Exception] = None
        # 方式 1：Bearer Token
        try:
            return self._post_bearer(body, params)
        except VolcTTSError as e:
            last_err = e
            # 鉴权类错误才尝试 HMAC256；其它错误（如文本超长）直接抛出
            if "grant" not in str(e).lower() and "3001" not in str(e):
                raise

        # 方式 2：HMAC256（用 AK/SK）
        if self.config.access_key and self.config.secret_key:
            try:
                return self._post_hmac(body, params)
            except VolcTTSError as e:
                last_err = e

        raise VolcTTSError(
            "火山 TTS 鉴权失败（两种方式均返回 grant 不存在 / 3001）。"
            "请确认 .env 中 VOLC_APP_ID 对应的 Access Token 正确："
            "在 语音技术控制台 -> 应用管理 找到该 AppID，复制其 Access Token 填入 VOLC_API_KEY。"
            f" 原始错误：{last_err}"
        )

    def _post_bearer(self, body: Dict[str, Any], params: TTSParams) -> bytes:
        token = self.config.api_key or self.config.access_key
        body["app"]["token"] = token
        headers = {
            "Authorization": f"Bearer;{token}",
            "Content-Type": "application/json",
        }
        return self._do_post(body, headers, params)

    def _post_hmac(self, body: Dict[str, Any], params: TTSParams) -> bytes:
        body_s = json.dumps(body, ensure_ascii=False)
        sts = f"POST /api/v1/tts HTTP/1.1\n{TTS_HOST}\n{body_s}"
        mac = base64.urlsafe_b64encode(
            hmac.new(self.config.secret_key.encode(), sts.encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")
        headers = {
            "Authorization": f'HMAC256; access_token="{self.config.access_key}"; mac="{mac}"; h="Host"',
            "Content-Type": "application/json",
        }
        return self._do_post(body, headers, params, raw_body=body_s)

    def _do_post(
        self,
        body: Dict[str, Any],
        headers: Dict[str, str],
        params: TTSParams,
        raw_body: Optional[str] = None,
    ) -> bytes:
        data = raw_body if raw_body is not None else json.dumps(body, ensure_ascii=False)
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = requests.post(TTS_URL, headers=headers, data=data.encode("utf-8"), timeout=30)
                break
            except requests.RequestException as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise VolcTTSError(f"火山 TTS 网络请求失败：{last_exc}")

        try:
            j = r.json()
        except ValueError:
            raise VolcTTSError(f"火山 TTS 返回非 JSON：HTTP {r.status_code} {r.text[:200]!r}")

        code = j.get("code")
        if r.status_code != 200 or (code not in (0, None, "0")):
            raise VolcTTSError(
                f"火山 TTS 调用失败：HTTP {r.status_code}, code={code}, message={j.get('message')!r}"
            )
        b64 = j.get("data")
        if not b64:
            raise VolcTTSError(f"火山 TTS 返回无音频数据：{json.dumps(j, ensure_ascii=False)[:300]}")
        return base64.b64decode(b64)

    # ------------------------------------------------------------------
    @staticmethod
    def _repeat_text(text: str, read_times: int) -> str:
        n = max(1, int(read_times))
        if n == 1:
            return text
        # 重复朗读：用句点+空格连接，依赖 silence_duration 产生停顿
        return ". ".join([text.strip().rstrip(".") for _ in range(n)]) + "."

    @staticmethod
    def _gen_reqid() -> str:
        return f"elv_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


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
