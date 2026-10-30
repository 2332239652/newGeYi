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


# audioop 缺失只提醒一次（Python 3.13 起该模块已被移除，PEP 594）
_AUDIOOP_MISSING_WARNED = False


def _resample_pcm16(samples: list[int], src_rate: int, dst_rate: int) -> list[int]:
    """纯 Python 线性插值重采样（16bit 单声道样本表）。

    只在 `audioop` 不可用时走这条路。速度比 C 实现慢一两个数量级，
    但一条 2~4 秒的语音也就是十几万点，放在语音线程池里完全够用。
    """
    if src_rate == dst_rate or not samples:
        return samples
    ratio = src_rate / float(dst_rate)
    out_len = max(1, int(len(samples) / ratio))
    last = len(samples) - 1
    out: list[int] = []
    for i in range(out_len):
        pos = i * ratio
        left = int(pos)
        if left > last:
            break
        frac = pos - left
        right = left + 1 if left < last else left
        out.append(int(samples[left] + (samples[right] - samples[left]) * frac))
    return out


def _interleave_pcm16(channels: list[list[int]]) -> bytes:
    import struct

    if not channels:
        return b''
    if len(channels) == 1:
        return struct.pack(f'<{len(channels[0])}h', *channels[0])
    length = min(len(ch) for ch in channels)
    merged: list[int] = []
    for i in range(length):
        for ch in channels:
            merged.append(ch[i])
    return struct.pack(f'<{len(merged)}h', *merged)


def _optimize_wav_audio(wav_bytes: bytes) -> bytes:
    """对原始音频做两件事：

    1. 重采样至 24000Hz（QQ Silk V3 只支持 8k/12k/16k/24k，其它采样率会不发声）；
    2. 裁掉首部静音（GPT-SoVITS 默认有约 0.6s 前置空白，听起来像"没声音"）。

    优先用 `audioop`（C 实现）。**Python 3.13 起 audioop 被移除**（PEP 594），
    所以这里有一条纯 Python 兜底路径，并且**兜底触发时打 warning**——
    以前外层只 `logger.debug`，而日志桥接的级别是 INFO，等于坏了无声：
    QQ 端表现就是"语音没声 / 前面空 0.6 秒"，日志里一个字都查不到。
    """
    global _AUDIOOP_MISSING_WARNED
    try:
        import io
        import struct
        import wave

        with wave.open(io.BytesIO(wav_bytes), 'rb') as w_in:
            nchannels = w_in.getnchannels()
            sampwidth = w_in.getsampwidth()
            framerate = w_in.getframerate()
            frames = w_in.readframes(w_in.getnframes())

        try:
            import audioop  # noqa: PLC0415 - 可选加速路径
            audioop_missing = False
        except Exception:  # noqa: BLE001 - 3.13+ 没有这个模块
            audioop_missing = True
            if not _AUDIOOP_MISSING_WARNED:
                _AUDIOOP_MISSING_WARNED = True
                logger.warning(
                    '当前解释器没有 audioop（Python 3.13 起已移除），'
                    '改用纯 Python 重采样：语音照常发送，只是每条多花一两百毫秒'
                )

        # ---- 1. 重采样至 24000Hz ----
        if framerate != 24000:
            if not audioop_missing:
                frames, _ = audioop.ratecv(frames, sampwidth, nchannels, framerate, 24000, None)
                framerate = 24000
            elif sampwidth == 2:
                total = len(frames) // 2
                flat = struct.unpack(f'<{total}h', frames)
                channels = [list(flat[ch::nchannels]) for ch in range(nchannels)]
                channels = [_resample_pcm16(ch, framerate, 24000) for ch in channels]
                frames = _interleave_pcm16(channels)
                framerate = 24000

        # ---- 2. 剥离头部无声死区（保留 30ms 呼吸头，防止吃爆破音）----
        if sampwidth == 2 and nchannels == 1:
            count = len(frames) // 2
            samples = list(struct.unpack(f'<{count}h', frames))
            start_idx = 0
            for idx, value in enumerate(samples):
                if abs(value) >= 600:
                    start_idx = idx
                    break
            start_idx = max(0, start_idx - int(framerate * 0.03))
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
    except Exception as e:  # noqa: BLE001 - 优化失败也要把原音频发出去
        # 从 debug 提到 warning：这条一旦出现，语音很可能"能发但没声"
        logger.warning('WAV 音频流优化失败，回退原始音频（可能无声或延迟）: %s', e)
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
        # 熔断退避：网关真挂掉时探测窗口逐次翻倍（60s → 120s → … → 300s）。
        # 老版本固定 45 秒探一次，而探测是**同步**跑在回复路径上的（2 秒超时），
        # 等于主力机一关机，每隔 45 秒就有一次回复平白多等 2 秒（2026-10 复盘）。
        self._offline_ttl_min: float = 60.0
        self._offline_ttl_max: float = 300.0
        self._offline_ttl: float = 0.0
        self._consecutive_fail: int = 0
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
        # 重置健康缓存与熔断计数（换了地址就该重新判一次）
        self._last_health_check_time = 0.0
        self._offline_ttl = 0.0
        self._consecutive_fail = 0

    # ---------------- 熔断状态（回复路径只读，绝不做网络 IO） ----------------
    def health_cached(self) -> tuple[bool, str, int]:
        """只读缓存，绝不发网络请求。回复路径只许调这个。"""
        if self._is_healthy:
            return True, "在线", self._last_latency_ms
        return False, self._last_error or "离线", self._last_latency_ms

    def is_offline_window(self) -> bool:
        """是否还处在熔断保护期（此时连探测都不该做）。"""
        if self._is_healthy:
            return False
        window = self._offline_ttl or self._health_ttl
        return (time.time() - self._last_health_check_time) < window

    def next_probe_in(self) -> float:
        """距离允许下一次探测还有多少秒（健康时为 0）。"""
        if self._is_healthy:
            return 0.0
        window = self._offline_ttl or self._health_ttl
        return max(0.0, window - (time.time() - self._last_health_check_time))

    def offline_stats(self) -> tuple[int, int]:
        """(连续失败次数, 当前熔断窗口秒数)，给「语音状态」用。"""
        return self._consecutive_fail, int(self._offline_ttl or self._health_ttl)

    def reset_offline_window(self) -> None:
        """手动索要语音时用：清零熔断窗口，允许立刻重试一次。"""
        self._last_health_check_time = 0.0
        self._offline_ttl = 0.0

    def _note_online(self, latency_ms: int = 0) -> None:
        self._is_healthy = True
        self._last_error = ""
        self._consecutive_fail = 0
        self._offline_ttl = 0.0
        self._last_latency_ms = latency_ms
        self._last_health_check_time = time.time()

    def _note_offline(self, error: str) -> None:
        """记一次离线：失败次数 +1，探测窗口翻倍（60s → 120s → … → 300s 封顶）。"""
        self._is_healthy = False
        self._last_error = error
        self._consecutive_fail += 1
        if self._offline_ttl <= 0:
            self._offline_ttl = self._offline_ttl_min
        else:
            self._offline_ttl = min(self._offline_ttl_max, self._offline_ttl * 2)
        self._last_health_check_time = time.time()
        logger.info(
            "语音网关离线（连续 %d 次，熔断 %d 秒）: %s",
            self._consecutive_fail, int(self._offline_ttl), error,
        )

    def check_health(self, force: bool = False) -> tuple[bool, str, int]:
        """探测本地语音主机健康状态。

        返回: (is_healthy, message, latency_ms)

        ⚠️ 这个函数会发网络请求，**只许在后台线程 / 运维指令里调**；
        回复路径一律用 health_cached()，否则主力机一关机，回复就跟着卡。
        """
        now = time.time()
        window = self._health_ttl if self._is_healthy else (self._offline_ttl or self._health_ttl)
        if not force and (now - self._last_health_check_time < window):
            return self._is_healthy, ("在线" if self._is_healthy else self._last_error), self._last_latency_ms

        target_url = self.health_url or self.gateway_url
        if not target_url:
            self._note_offline("未配置网关地址")
            return False, self._last_error, 0

        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        start_t = time.perf_counter()
        try:
            resp = requests.get(target_url, headers=headers, timeout=min(2.0, self.timeout))
            latency = int((time.perf_counter() - start_t) * 1000)
            if resp.status_code == 200:
                self._note_online(latency)
                return True, "在线", latency
            self._last_latency_ms = latency
            self._note_offline(f"HTTP {resp.status_code}")
            return False, self._last_error, latency
        except requests.Timeout:
            latency = int((time.perf_counter() - start_t) * 1000)
            self._last_latency_ms = latency
            self._note_offline(f"探测超时 ({min(2.0, self.timeout):.0f}s)")
            return False, self._last_error, latency
        except Exception as e:
            latency = int((time.perf_counter() - start_t) * 1000)
            self._last_latency_ms = latency
            self._note_offline(f"连接失败 ({type(e).__name__})")
            return False, self._last_error, latency

    def synthesize(
        self,
        text: str,
        emotion: str = "日常",
        speed: float | None = None,
        group_id: str = "",
        group_name: str = "",
        sender_name: str = "",
        force: bool = False,
    ) -> tuple[str | None, dict[str, Any]]:
        """向本地主机请求日语原声语音合成。

        返回: (cq_code_or_none, extra_info)
        成功返回: ("[CQ:record,file=base64://...]", {"inference_time_ms": ..., "duration_est": ...})
        失败返回: (None, {"error": ...})

        force=True 表示「用户明确索要 / 运维手动测试」：无视熔断窗口硬试一次，
        否则用户喊「发个语音」，而熔断窗口还剩两分钟，就会莫名其妙一声不吭。
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

        # 熔断保护期内静默跳过，避免每次回复都白等一次超时（主动索要时 force=True 绕过）
        if not force and self.is_offline_window():
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

                    self._note_online(latency_ms)
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
                # 非 200 也要计入熔断：以前只写 _last_error 不置不可用，
                # 网关稳定返回 5xx 时会每次都陪它走一遍超时
                self._note_offline(f"HTTP {resp.status_code}")
                return None, {"error": self._last_error, "latency_ms": latency_ms}

        except requests.Timeout:
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            logger.info("本地语音合成请求超时 (%.1fs)，平滑静默降级", self.timeout)
            self._note_offline(f"超时 ({self.timeout:.0f}s)")
            return None, {"error": self._last_error, "latency_ms": latency_ms}

        except requests.RequestException as e:
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            logger.info("本地语音网络异常 (%s)，平滑静默降级", type(e).__name__)
            self._note_offline(f"连接异常: {type(e).__name__}")
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
