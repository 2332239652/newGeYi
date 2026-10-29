# -*- encoding: utf-8 -*-
"""歌伊伴生原声语音：轻量 HTTP 客户端。

连接部署在主力电脑的本地算力端（Genie-TTS ONNX 极速合成服务）。
具备严格的超时熔断、平滑降级与健康缓存能力，保护低配云服务器性能。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
import requests

logger = logging.getLogger("newGeYi.voice")


def _optimize_wav_audio(wav_bytes: bytes) -> bytes:
    """对原始音频进行极速优化：

    1. 剥离头部无声死区（GPT-SoVITS 默认生成约 0.6s 前置静音，消除“前面空着点”的体感迟滞）；
    2. 重采样至标准 24000Hz（对齐 QQ NT / Silk V3 官方最高保真采样率，杜绝不发声或解码卡死）。
    """
    try:
        import audioop
        import io
        import struct
        import wave

        with wave.open(io.BytesIO(wav_bytes), 'rb') as w_in:
            nchannels = w_in.getnchannels()
            sampwidth = w_in.getsampwidth()
            framerate = w_in.getframerate()
            nframes = w_in.getnframes()
            frames = w_in.readframes(nframes)

        # 1. 重采样至 24000Hz
        if framerate != 24000:
            frames, _ = audioop.ratecv(frames, sampwidth, nchannels, framerate, 24000, None)
            framerate = 24000
            nframes = len(frames) // (sampwidth * nchannels)

        # 2. 剥离头部无声死区 (保留 30ms 呼吸头防止吃爆破音)
        if sampwidth == 2 and nchannels == 1:
            samples = struct.unpack(f'<{nframes}h', frames)
            start_idx = 0
            for idx, s in enumerate(samples):
                if abs(s) >= 600:
                    start_idx = idx
                    break
            pad_samples = int(framerate * 0.03)
            start_idx = max(0, start_idx - pad_samples)
            if start_idx > 0:
                samples = samples[start_idx:]
                frames = struct.pack(f'<{len(samples)}h', *samples)

        out_io = io.BytesIO()
        with wave.open(out_io, 'wb') as w_out:
            w_out.setnchannels(nchannels)
            w_out.setsampwidth(sampwidth)
            w_out.setframerate(framerate)
            w_out.writeframes(frames)
        return out_io.getvalue()
    except Exception as e:
        logger.debug("WAV 音频流优化异常，保留原音频: %s", e)
        return wav_bytes


class VoiceClient:
    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:9885/v1/voice/synthesize",
        health_url: str = "http://127.0.0.1:9885/health",
        auth_token: str = "",
        timeout: float = 10.0,
        speed: float = 1.0,
    ):
        self.gateway_url = gateway_url.strip()
        self.health_url = health_url.strip() if health_url else ""
        self.auth_token = auth_token.strip()
        self.timeout = max(1.0, float(timeout))
        self.speed = float(speed)

        # 健康状态轻量缓存（避免主机离线时每次对话都白等超时）
        self._last_health_check_time: float = 0.0
        self._is_healthy: bool = True
        self._health_ttl: float = 45.0  # 健康状态缓存 45 秒
        self._last_latency_ms: int = 0
        self._last_error: str = ""

    def _base_url(self) -> str:
        """从 gateway_url 提取基础路径 (例如 http://127.0.0.1:9885)。"""
        if not self.gateway_url:
            return "http://127.0.0.1:9885"
        from urllib.parse import urlparse
        u = urlparse(self.gateway_url)
        return f"{u.scheme}://{u.netloc}" if u.netloc else self.gateway_url.rstrip("/")

    def update_config(
        self,
        gateway_url: str | None = None,
        health_url: str | None = None,
        auth_token: str | None = None,
        timeout: float | None = None,
        speed: float | None = None,
    ) -> None:
        """热更新客户端网络参数。"""
        if gateway_url is not None:
            self.gateway_url = gateway_url.strip()
        if health_url is not None:
            self.health_url = health_url.strip()
        if auth_token is not None:
            self.auth_token = auth_token.strip()
        if timeout is not None:
            self.timeout = max(1.0, float(timeout))
        if speed is not None:
            self.speed = float(speed)
        # 重置健康缓存
        self._last_health_check_time = 0.0

    def check_health(self, force: bool = False) -> tuple[bool, str, int]:
        """探测本地语音主机健康状态。

        返回: (is_healthy, message, latency_ms)
        """
        now = time.time()
        if not force and (now - self._last_health_check_time < self._health_ttl):
            return self._is_healthy, ("在线" if self._is_healthy else self._last_error), self._last_latency_ms

        target_url = self.health_url or self.gateway_url
        if not target_url:
            self._is_healthy = False
            self._last_error = "未配置网关地址"
            return False, self._last_error, 0

        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        start_t = time.perf_counter()
        try:
            resp = requests.get(target_url, headers=headers, timeout=min(2.0, self.timeout))
            latency = int((time.perf_counter() - start_t) * 1000)
            self._last_latency_ms = latency
            self._last_health_check_time = now

            if resp.status_code == 200:
                self._is_healthy = True
                self._last_error = ""
                return True, "在线", latency
            else:
                self._is_healthy = False
                self._last_error = f"HTTP {resp.status_code}"
                return False, self._last_error, latency
        except requests.Timeout:
            latency = int((time.perf_counter() - start_t) * 1000)
            self._is_healthy = False
            self._last_latency_ms = latency
            self._last_health_check_time = now
            self._last_error = f"探测超时 ({self.timeout}s)"
            return False, self._last_error, latency
        except Exception as e:
            latency = int((time.perf_counter() - start_t) * 1000)
            self._is_healthy = False
            self._last_latency_ms = latency
            self._last_health_check_time = now
            self._last_error = f"连接失败 ({type(e).__name__})"
            return False, self._last_error, latency

    def synthesize(
        self,
        text: str,
        emotion: str = "日常",
        speed: float | None = None,
        group_id: str = "",
        group_name: str = "",
        sender_name: str = "",
    ) -> tuple[str | None, dict[str, Any]]:
        """向本地主机请求日语原声语音合成。

        返回: (cq_code_or_none, extra_info)
        成功返回: ("[CQ:record,file=base64://...]", {"inference_time_ms": ..., "duration_est": ...})
        失败返回: (None, {"error": ...})
        """
        clean_text = (text or "").strip()
        # 1. 彻底清除违背歌伊人设的 "先生" / "せんせい" 称呼，回归歌伊原生设定
        clean_text = re.sub(r'(?:先生|せんせい|センセイ)', '', clean_text)
        # 2. 清除 ASCII 颜文字、动作括号与异常方括号，保持纯净日语音素输入
        clean_text = re.sub(r'[（(][^）)]*[）)]', '', clean_text)
        clean_text = re.sub(r'[<>[\]{}【】]+', '', clean_text)
        # 3. 剥离句首可能导致 TTS G2P 插入静音 pause 标记的标点和符号
        clean_text = re.sub(r'^[「『（(\[【\s…、，,。！？!?~～]+', '', clean_text).strip()
        # 4. 清除装饰符号（保留全角波浪号 〜 和破折号 …，用于触发极逼真的微颤气声与拖音）
        clean_text = re.sub(r'[☆★※♪♫♬#@$^*_+=\\/`]+', '', clean_text).strip()
        # 5. 黄金长度控制（10~25 字符最佳，硬截断上限 35 字符，确保 2~4 秒自然呼吸节奏）
        if len(clean_text) > 35:
            split_pos = max(
                clean_text[:30].rfind('！'),
                clean_text[:30].rfind('。'),
                clean_text[:30].rfind('？'),
                clean_text[:30].rfind('…'),
                clean_text[:30].rfind('〜'),
            )
            if split_pos >= 8:
                clean_text = clean_text[:split_pos+1]
            else:
                clean_text = clean_text[:28].rstrip('！!?。、…〜') + '！'
        if not clean_text:
            return None, {"error": "空文本"}

        if not self.gateway_url:
            return None, {"error": "未配置网关地址"}

        # 若处于离线缓存周期内，静默跳过，避免无效超时阻塞
        now = time.time()
        if not self._is_healthy and (now - self._last_health_check_time < self._health_ttl):
            return None, {"error": f"本地主机处于离线保护期: {self._last_error}"}

        headers = {
            "Content-Type": "application/json",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        payload: dict[str, Any] = {
            "text": clean_text,
            "emotion": emotion,
            "speed": float(speed) if speed is not None else self.speed,
            "format": "base64",
        }
        if group_id:
            payload["group_id"] = str(group_id)
        if group_name:
            payload["group_name"] = str(group_name)
        if sender_name:
            payload["sender_name"] = str(sender_name)

        start_t = time.perf_counter()
        try:
            resp = requests.post(
                self.gateway_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            self._last_latency_ms = latency_ms

            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0 and data.get("base64"):
                    b64 = data["base64"].strip()
                    # 极速流优化：切除头部约 0.6s 迟滞静音 + 重采样至 QQ 官方 24000Hz 高清 Silk 规格
                    try:
                        import base64
                        raw_wav = base64.b64decode(b64)
                        optimized_wav = _optimize_wav_audio(raw_wav)
                        b64 = base64.b64encode(optimized_wav).decode("ascii")
                    except Exception as ex:
                        logger.debug("音频流优化失败，降级使用原音频: %s", ex)

                    self._is_healthy = True
                    self._last_error = ""
                    self._last_health_check_time = now
                    cq_code = f"[CQ:record,file=base64://{b64}]"
                    return cq_code, {
                        "base64": b64,
                        "latency_ms": latency_ms,
                        "duration_est": data.get("duration_est", 0.0),
                        "metrics": data.get("metrics", {}),
                    }
                else:
                    msg = data.get("message", "未知错误")
                    logger.warning("语音网关返回业务错误: code=%s, msg=%s", data.get("code"), msg)
                    return None, {"error": msg, "latency_ms": latency_ms}
            else:
                logger.warning("语音网关返回非 200: %s %s", resp.status_code, resp.text[:200])
                self._last_error = f"HTTP {resp.status_code}"
                return None, {"error": self._last_error, "latency_ms": latency_ms}

        except requests.Timeout:
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            logger.info("本地语音合成请求超时 (%.1fs)，平滑静默降级", self.timeout)
            self._is_healthy = False
            self._last_error = f"超时 ({self.timeout}s)"
            self._last_health_check_time = now
            return None, {"error": self._last_error, "latency_ms": latency_ms}

        except requests.RequestException as e:
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            logger.info("本地语音网络异常 (%s)，平滑静默降级", type(e).__name__)
            self._is_healthy = False
            self._last_error = f"连接异常: {type(e).__name__}"
            self._last_health_check_time = now
            return None, {"error": self._last_error, "latency_ms": latency_ms}

        except Exception as e:
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            logger.warning("语音合成未捕获异常: %s", e)
            return None, {"error": str(e), "latency_ms": latency_ms}

    def report_reply_telemetry(self) -> None:
        """异步向本地网关上报一条文本回复，点亮控制台触发率统计（失败静默吞掉）。"""
        base = self._base_url()
        if not base or not self._is_healthy:
            return
        url = f"{base}/v1/voice/telemetry/reply"
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        try:
            requests.post(url, json={}, headers=headers, timeout=1.0)
        except Exception:
            pass

    def get_remote_stats(self) -> dict[str, Any] | None:
        """拉取本地算力网关 v2.1 统计报表（只读自查）。"""
        base = self._base_url()
        if not base:
            return None
        url = f"{base}/v1/voice/stats"
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        try:
            resp = requests.get(url, headers=headers, timeout=2.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None
