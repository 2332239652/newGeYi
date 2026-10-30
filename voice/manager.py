# -*- encoding: utf-8 -*-
"""歌伊伴生原声语音：核心调度器。

负责：
1. 语音网关通信与健康监控；
2. 主动语音请求意图识别（用户要求发语音时必应并跳过CD）；
3. 类似表情包的轻量频控（单群冷却CD、避免连续连发）；
4. 后台线程池异步合成与消息派发（先文字后语音，零阻塞）；
5. 戳一戳互动伴生语音彩蛋。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import re
import threading
import time
from typing import Any, Callable

from .client import VoiceClient
from .presets import (
    SUPPORTED_EMOTIONS,
    get_random_poke_voice,
    get_random_preset,
    normalize_emotion,
)

logger = logging.getLogger("newGeYi.voice")

# 主动索要语音的常用中文意图正则
_VOICE_INTENT_RE = re.compile(
    r'(?:(?:发|来|录|发个|来个|说句|用|发条|发段)语音|说两句|说句话|说点什么|我想听你声音|听听你的声音|想听歌伊的声音|语音回复)',
    re.I,
)


class VoiceManager:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.enabled: bool = bool(cfg.get("voice_enabled", True))
        self.gateway_url: str = str(cfg.get("voice_gateway_url", "http://127.0.0.1:9885/v1/voice/synthesize"))
        self.health_url: str = str(cfg.get("voice_health_url", "http://127.0.0.1:9885/health"))
        self.auth_token: str = str(cfg.get("voice_auth_token", ""))
        self.timeout: float = float(cfg.get("voice_timeout", 10.0))
        self.group_cooldown: float = float(cfg.get("voice_group_cooldown", 45))
        self.poke_rate: float = float(cfg.get("voice_poke_rate", 0.25))
        self.speed: float = float(cfg.get("voice_speed", 1.0))
        self.onebot_http_url: str = str(cfg.get("voice_onebot_http_url", "http://127.0.0.1:15991/send_msg"))

        self.client = VoiceClient(
            gateway_url=self.gateway_url,
            health_url=self.health_url,
            auth_token=self.auth_token,
            timeout=self.timeout,
            speed=self.speed,
        )

        # 频控状态维护
        self._lock = threading.Lock()
        self._group_last_voice: dict[str, float] = {}  # target_id -> timestamp
        self._session_consecutive: dict[str, bool] = {}  # session_key -> 上一轮是否刚发过语音

        # 熔断状态：网关掉线时**只记日志**，不发通知（见 _note_health 的说明）
        self._healthy_seen: bool | None = None
        self._probe_inflight: bool = False

        # 异步合成线程池（极轻量，最大 2 个并发线程）
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="GeYiVoice")

    def reload(self, config: dict[str, Any]) -> None:
        """重载语音配置。"""
        with self._lock:
            self.enabled = bool(config.get("voice_enabled", True))
            self.gateway_url = str(config.get("voice_gateway_url", self.gateway_url))
            self.health_url = str(config.get("voice_health_url", self.health_url))
            self.auth_token = str(config.get("voice_auth_token", self.auth_token))
            self.timeout = float(config.get("voice_timeout", 10.0))
            self.group_cooldown = float(config.get("voice_group_cooldown", 45))
            self.poke_rate = float(config.get("voice_poke_rate", 0.25))
            self.speed = float(config.get("voice_speed", 1.0))
            self.onebot_http_url = str(config.get("voice_onebot_http_url", self.onebot_http_url))

            self.client.update_config(
                gateway_url=self.gateway_url,
                health_url=self.health_url,
                auth_token=self.auth_token,
                timeout=self.timeout,
                speed=self.speed,
            )
        logger.info("语音子系统配置已重载: enabled=%s, url=%s", self.enabled, self.gateway_url)

    # ---------------- 熔断状态 ----------------

    def note_probe_result(self, healthy: bool, reason: str = '') -> None:
        """外部（运维指令 / 手动测试）拿到探测结果后回报，用于状态迁移。"""
        self._note_health(healthy, reason)

    def _note_health(self, healthy: bool, reason: str = '') -> None:
        """记录一次健康判定：**只写日志，不发任何通知**。

        TTS 跑在骰主自己的电脑上，晚上关机是常态 —— 以前这里会私聊骰主
        「歌伊语音降级」，等于每天半夜给主人发一条已知的噪声（2026-10-30 骰主明确要求去掉）。
        语音本来就是可选增强：网关不在，文字照常，`语音状态` 指令里能查到熔断详情，
        日志里按状态跃变留一行就够。
        """
        prev = self._healthy_seen
        self._healthy_seen = healthy
        if healthy:
            if prev is False:
                logger.info('语音网关已恢复可用')
            return
        if prev is False:
            return                       # 已经是离线状态，不重复刷日志
        logger.info('语音网关不可用，静默降级（文字照常，仅语音暂停）：%s', reason or '未知原因')

    def _refresh_health_async(self) -> None:
        """后台探一次网关。回复路径上绝不发网络请求（老版本这里会阻塞 2 秒）。"""
        with self._lock:
            if self._probe_inflight:
                return
            self._probe_inflight = True

        def _job():
            try:
                healthy, reason, _ = self.client.check_health(force=True)
                self._note_health(healthy, reason)
            except Exception as ex:  # noqa: BLE001
                logger.debug('语音健康探测异常: %s', ex)
            finally:
                with self._lock:
                    self._probe_inflight = False

        try:
            self._executor.submit(_job)
        except RuntimeError:  # 线程池已关闭（插件正在卸载）
            with self._lock:
                self._probe_inflight = False

    # ---------------- 意图与频控 ----------------

    def is_user_requesting_voice(self, text: str) -> bool:
        """识别用户本轮输入中是否包含明确的索要语音意图。"""
        if not text:
            return False
        return bool(_VOICE_INTENT_RE.search(text))

    def can_send_voice(self, session_key: str, target_id: str, is_active_request: bool = False) -> tuple[bool, str]:
        """类似表情包的频控策略：

        1. 用户明确主动要求：无条件放行（豁免冷却与连发限制，熔断也绕过一次）；
        2. 主机熔断保护期内：快速跳过，**绝不在这里发网络请求**；
        3. 上一轮刚刚发过语音（连续连发）：拦截（保持自然对话间隔）；
        4. 群冷却 CD 未到（默认 45 秒）：拦截。

        2026-10-30 改：以前第 2 步调的是 check_health(force=False)，缓存一过期就
        同步发一次 GET（2 秒超时）。主力机一关机，等于每隔 45 秒就有一条回复
        平白多等 2 秒——用户体感就是「歌伊怎么突然卡一下」。现在探测全走后台线程。
        """
        if not self.enabled:
            return False, "语音功能已关闭"

        # 只读缓存判断，不做任何网络 IO
        healthy, reason, _ = self.client.health_cached()
        if not healthy:
            if is_active_request:
                # 用户明确要语音：无视熔断硬试一次（合成在后台线程里），并清零窗口
                self.client.reset_offline_window()
                logger.info("用户主动索要语音，忽略熔断重试一次（%s）", reason)
                return True, "用户主动触发，忽略熔断重试"
            if self.client.is_offline_window():
                return False, f"语音熔断中: {reason}"
            # 保护期已过：后台探一次，本轮先跳过
            self._refresh_health_async()
            return False, "语音网关重连中，本轮跳过"

        # 用户主动索要语音，直接放行
        if is_active_request:
            return True, "用户主动触发，豁免冷却"

        now = time.time()
        with self._lock:
            # 连续性检查：上一轮刚发过语音则当前轮自觉间隔
            if self._session_consecutive.get(session_key, False):
                return False, "连发间隔保护（上一轮刚发过语音）"

            # 群冷却检查
            last_t = self._group_last_voice.get(target_id, 0.0)
            passed = now - last_t
            if passed < self.group_cooldown:
                remain = int(self.group_cooldown - passed)
                return False, f"群冷却中（剩余 {remain} 秒）"

        return True, "可以发送"

    def mark_voice_sent(self, session_key: str, target_id: str) -> None:
        """记录已发送语音，更新冷却与连发状态。"""
        now = time.time()
        with self._lock:
            self._group_last_voice[target_id] = now
            self._session_consecutive[session_key] = True

    def mark_turn_without_voice(self, session_key: str) -> None:
        """当歌伊进行了一次没有发语音的普通对话时，重置连发状态。"""
        with self._lock:
            self._session_consecutive[session_key] = False

    # ---------------- 异步发送调度 ----------------

    def send_voice_message(self, session_type: str, target_id: str | int, base64_audio: str, event: Any = None) -> bool:
        """投递 Base64 语音：优先走本地 OneBot HTTP 接口（彻底杜绝 OlivOS OP 码 url='None' 导致的未知文件路径报错），若失败则退回 event.send。"""
        tid = str(target_id).strip()
        b64 = (base64_audio or "").strip()
        if b64.startswith("base64://"):
            b64_clean = b64[9:]
        else:
            b64_clean = b64

        if not b64_clean:
            return False

        # 方式 1: 直接通过 OneBot 11 HTTP API 投递标准 JSON（彻底杜绝 url="None" 异常与 [程心自定义] 标签）
        if self.onebot_http_url:
            try:
                import json
                import requests
                target_key = "group_id" if session_type == "group" else "user_id"
                payload = {
                    "message_type": session_type,
                    target_key: int(tid),
                    "message": [
                        {
                            "type": "record",
                            "data": {
                                "file": f"base64://{b64_clean}"
                            }
                        }
                    ]
                }
                resp = requests.post(self.onebot_http_url, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("retcode") == 0 or data.get("status") == "ok":
                        logger.info("OneBot HTTP 语音直发成功 -> %s(%s)", session_type, tid)
                        return True
                    else:
                        logger.warning("OneBot HTTP 语音接口返回错误: %s", data)
            except Exception as e:
                logger.debug("OneBot HTTP 语音投递异常，尝试退回平台接口: %s", e)

        # 方式 2: 退回平台 event 接口
        cq_code = f"[CQ:record,file=base64://{b64_clean}]"
        if event is not None:
            try:
                if hasattr(event, "send"):
                    event.send(session_type, int(tid), cq_code)
                    return True
                elif hasattr(event, "reply"):
                    event.reply(cq_code)
                    return True
            except Exception as ex:
                logger.warning("平台接口发送语音异常: %s", ex)

        return False

    def dispatch_voice(
        self,
        event: Any,
        session_type: str,
        target_id: str | int,
        session_key: str,
        emotion: str,
        japanese_text: str,
        is_active_request: bool = False,
        group_id: str = "",
        group_name: str = "",
        sender_name: str = "",
        on_success: Callable[[str, str, str], None] | None = None,
    ) -> None:
        """在后台线程异步请求本地 TTS 并发送语音消息。

        参数:
            event: OlivOS 事件对象 (具有 send(session_type, target_id, payload) 方法)
            session_type: 'group' 或 'private'
            target_id: 目标群号或用户 QQ
            session_key: 会话标识字符串
            emotion: 情绪名 (8大维度之一)
            japanese_text: 日语台词
            is_active_request: 是否为用户主动求语音
            group_id: 统计用群号（可选）
            group_name: 统计用群名（可选）
            sender_name: 统计用发送者昵称（可选）
            on_success: 发送成功回调 (session_key, emotion, japanese_text)，用于上下文回写
        """
        can, reason = self.can_send_voice(session_key, str(target_id), is_active_request)
        if not can:
            logger.debug("跳过语音合成 [%s]: %s", session_key, reason)
            return

        norm_emotion = normalize_emotion(emotion)

        def _worker():
            try:
                cq_code, info = self.client.synthesize(
                    japanese_text,
                    norm_emotion,
                    group_id=group_id,
                    group_name=group_name,
                    sender_name=sender_name,
                    # 用户明确索要时无视熔断硬试一次
                    force=is_active_request,
                )
                b64 = info.get("base64")
                if not b64 and cq_code:
                    m = re.search(r'base64://([A-Za-z0-9+/=]+)', cq_code)
                    if m:
                        b64 = m.group(1)

                if not b64:
                    # 合成失败：如果是「网关连不上」，顺手把降级通知发出去（按跃变 + 冷却，不刷屏）
                    if not self.client.health_cached()[0]:
                        self._note_health(False, str(info.get("error") or "合成失败"))
                    logger.debug("语音合成未产出有效音频数据: %s", info.get("error", "未知"))
                    return
                self._note_health(True)

                # 调用平台底层接口发送 Base64 语音气泡
                sent = self.send_voice_message(session_type, target_id, b64, event=event)
                if not sent:
                    logger.warning("所有语音投递渠道均失败 -> %s(%s)", session_type, target_id)
                    return

                # 记录冷却
                self.mark_voice_sent(session_key, str(target_id))
                logger.info(
                    "成功发送原声语音 [%s|%s]: %s (耗时: %sms)",
                    norm_emotion,
                    japanese_text,
                    session_key,
                    info.get("latency_ms", 0),
                )

                # 回调持久化上下文记忆
                if on_success:
                    try:
                        on_success(session_key, norm_emotion, japanese_text)
                    except Exception as ex:
                        logger.warning("语音发送成功后回调异常: %s", ex)

            except Exception as e:
                logger.warning("异步发送语音异常: %s", e)

        self._executor.submit(_worker)

    def report_reply_telemetry(self) -> None:
        """在后台线程异步上报文本回复事件，点亮本地控制台的语音触发率指标。"""
        if not self.enabled:
            return
        self._executor.submit(self.client.report_reply_telemetry)

    # ---------------- 戳一戳互动彩蛋 ----------------

    def maybe_dispatch_poke_voice(
        self,
        event: Any,
        session_type: str,
        target_id: str | int,
        session_key: str,
        on_success: Callable[[str, str, str], None] | None = None,
    ) -> bool:
        """戳一戳时以指定概率触发伴生语音彩蛋。"""
        import random

        if not self.enabled or self.poke_rate <= 0:
            return False

        if random.random() > self.poke_rate:
            return False

        can, reason = self.can_send_voice(session_key, str(target_id), is_active_request=False)
        if not can:
            return False

        emotion, phrase = get_random_poke_voice()
        self.dispatch_voice(
            event=event,
            session_type=session_type,
            target_id=target_id,
            session_key=session_key,
            emotion=emotion,
            japanese_text=phrase,
            is_active_request=False,
            group_id=str(target_id) if session_type == "group" else "",
            sender_name="戳一戳互动",
            on_success=on_success,
        )
        return True

    # ---------------- 运维状态汇报 ----------------

    def get_status_report(self) -> str:
        """生成供中文运维指令查看的健康与状态报告（含 v2.1 统计数据）。"""
        healthy, status_str, latency = self.client.check_health(force=True)
        # 人工查状态本身就是一次有效探测：同步状态迁移，但**不要再发私信**（人就在这儿）
        self._healthy_seen = healthy
        status_icon = "🟢 在线" if healthy else "🔴 离线"

        lines = [
            "【歌伊伴生语音系统状态】",
            f"• 整体开关: {'开启' if self.enabled else '关闭'}",
            f"• 本地算力主机: {status_icon} ({status_str})",
            f"• 探测延迟: {latency} ms",
        ]
        if healthy:
            lines.append("• 熔断状态: 🟢 正常")
        else:
            fails, window = self.client.offline_stats()
            remain = int(self.client.next_probe_in())
            lines.append(
                f"• 熔断状态: 🟠 连续失败 {fails} 次 | 退避窗口 {window} 秒 | {remain} 秒后才重探"
            )
            lines.append("• 降级表现: 文字照常，语音静默（用户主动索要时会硬试一次）")
        lines += [
            f"• 网关地址: {self.gateway_url}",
            f"• 请求硬超时: {self.timeout} 秒",
            f"• 单群冷却时间: {self.group_cooldown} 秒",
            f"• 戳一戳语音率: {int(self.poke_rate * 100)}%",
            f"• 支持情绪维度: {' / '.join(SUPPORTED_EMOTIONS)}",
        ]

        if healthy:
            stats = self.client.get_remote_stats()
            if stats and isinstance(stats, dict):
                today_syn = stats.get("today_synthesized", 0)
                today_rep = stats.get("today_replies", 0)
                rate = stats.get("trigger_rate", 0.0)
                avg_lat = stats.get("avg_latency", 0.0)
                engine = stats.get("engine", "Genie-TTS")
                character = stats.get("character", "Megumin")
                lines.append("")
                lines.append("【本地算力端 v2.1 遥测统计】")
                lines.append(f"• 当前模型: {character} ({engine})")
                lines.append(f"• 今日合成语音: {today_syn} 条 | 今日文本回复: {today_rep} 条")
                lines.append(f"• 语音触发率: {rate:.1f}% | 平均生成耗时: {avg_lat:.2f} 秒")

        return "\n".join(lines)
