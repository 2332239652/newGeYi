# -*- encoding: utf-8 -*-
"""歌伊跨插件上下文感知系统。

功能：
捕获同一机器人实例下其他插件（如 OlivaDice 骰娘、jrlp 今日运势/签到、漂流瓶等）发出的群聊消息，
自动将这些系统输出作为事实沉淀至歌伊的 live_context 与 SQLite turns 对话历史中。
防止歌伊因“看不见自己其他插件说了什么”而对群友的骰点反应、签到反馈等产生理解错误。
"""

from __future__ import annotations

from functools import wraps
import logging
import re
import threading
import time
from typing import Any

from ..presentation.renderer import op_codes_to_plain

logger = logging.getLogger("newGeYi.cross_plugin")

# 骰娘输出特征识别
_DICE_KEYWORDS = (
    "检定", "掷骰", "1D", "1d", "D100", "d100", "2D", "2d",
    "成功！", "失败！", "大成功", "大失败", "困难成功", "极难成功",
    "对抗", "理智", "狂气", "暗骰", "SAN", "san", "COC", "coc", "DND", "dnd",
    "克苏鲁", "疯狂发作", "幕间成长",
)

# 运势/签到输出特征识别
_FORTUNE_KEYWORDS = (
    "今日运势", "运势", "吉神", "求签", "签到", "金币", "人品",
)

# 漂流瓶输出特征识别
_BOTTLE_KEYWORDS = (
    "漂流瓶", "捞起了一个瓶子", "扔出了一个瓶子", "海风送来",
)


def guess_plugin_sender_label(text: str) -> str:
    """根据输出内容智能推断发信插件的身份标签。"""
    if any(kw in text for kw in _DICE_KEYWORDS):
        return "歌伊(骰娘系统)"
    if any(kw in text for kw in _FORTUNE_KEYWORDS):
        return "歌伊(今日运势)"
    if any(kw in text for kw in _BOTTLE_KEYWORDS):
        return "歌伊(漂流瓶)"
    return "歌伊(系统功能)"


class CrossPluginObserver:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._lock = threading.Lock()
        # 去重缓冲：(group_id, text_prefix) -> timestamp (TTL 10s)
        self._seen_messages: dict[tuple[str, str], float] = {}
        # 卸载钩子用的回调（install 时登记，save 时反向执行）
        self._detachers: list = []
        self._last_warn = 0.0

    def _warn(self, text: str, exc: Any = None) -> None:
        """钩子内部异常：限流后打 **warning**。

        以前这里是 `logger.debug`，而 newGeYi 的日志桥接级别是 INFO —— 钩子坏掉
        在线上完全无声（跨插件感知悄悄失效，谁也不知道）。改成 warning 并限流，
        避免一次异常风暴把日志刷满。
        """
        now = time.time()
        if now - self._last_warn < 60.0:
            return
        self._last_warn = now
        if exc is None:
            logger.warning('跨插件感知: %s', text)
        else:
            logger.warning('跨插件感知: %s: %s', text, exc)

    def add_detacher(self, fn) -> None:
        """登记一个"卸载钩子"的回调（插件 save 时执行）。"""
        self._detachers.append(fn)

    def detach(self) -> None:
        """还原所有被替换的钩子。

        为什么要还原：这两个钩子是**直接替换别人的模块属性**（猴补丁）。不还原的话，
        同一进程里每次重载都会再套一层——旧 runtime 被包在新闭包里，重复记账 + 泄漏。
        本机靠"重载=重启插件进程"兜住了，但那是运气，不是设计。
        """
        while self._detachers:
            fn = self._detachers.pop()
            try:
                fn()
            except Exception as ex:  # noqa: BLE001 - 还原失败也不能拦着退出
                logger.debug('还原跨插件钩子失败: %s', ex)

    def _cleanup_seen(self, now: float) -> None:
        for k in list(self._seen_messages.keys()):
            if now - self._seen_messages[k] > 10.0:
                self._seen_messages.pop(k, None)

    def on_other_plugin_sent(self, group_id: str, raw_message: Any, bot_id: str = "") -> None:
        """当捕获到其他插件向群聊发信时触发记录。"""
        if not group_id or not raw_message:
            return

        gid = str(group_id).strip()
        now = time.time()

        # 归一化消息文本
        text = ""
        if isinstance(raw_message, str):
            text = raw_message
        elif isinstance(raw_message, list):
            # OneBot 数组消息
            parts = []
            for item in raw_message:
                if isinstance(item, dict):
                    data = item.get("data", {})
                    if item.get("type") == "text":
                        parts.append(str(data.get("text", "")))
                    elif item.get("type") == "image":
                        parts.append("【图片】")
                    elif item.get("type") == "record":
                        parts.append("【语音】")
                elif isinstance(item, str):
                    parts.append(item)
            text = "".join(parts)
        else:
            text = str(raw_message)

        text = op_codes_to_plain(text).strip()
        if not text:
            return

        # 检查是否为歌伊 newGeYi 自身刚刚发出的回复（避免自己记自己两次）
        if hasattr(self.runtime, "is_recently_sent_by_self") and self.runtime.is_recently_sent_by_self(gid, text):
            return

        # 幂等去重检查（防止 OneBot 路由层与 OlivaDiceCore 钩子双重触发）
        sig = (gid, text[:80])
        with self._lock:
            self._cleanup_seen(now)
            if sig in self._seen_messages:
                return
            self._seen_messages[sig] = now

        # 获取机器人的 QQ 号
        if not bot_id and hasattr(self.runtime, "_bot_account_ids"):
            bids = self.runtime._bot_account_ids()
            if bids:
                bot_id = next(iter(bids))

        # 定位目标群的活跃 session_key
        session_key = ""
        if hasattr(self.runtime, "live_context") and hasattr(self.runtime.live_context, "_states"):
            for sk in self.runtime.live_context._states.keys():
                if sk.endswith(f":{gid}"):
                    session_key = sk
                    break

        if not session_key:
            session_key = f"default:qq:onebot:group:-:{gid}"

        label = guess_plugin_sender_label(text)
        seq = self.runtime._next_arrival_seq()

        # 写入实时旁观窗口 live_context（供当前实时上下文感知）
        try:
            self.runtime.live_context.append(
                session_key=session_key,
                source_seq=seq,
                role="user",
                content=text,
                sender_name=label,
                sender_id=bot_id,
            )
        except Exception as e:
            logger.debug("写入 live_context 失败: %s", e)

        # 写入持久化 turns 表（供后续历史与记忆沉淀）
        try:
            self.runtime.store.add_turn(
                session_key=session_key,
                role="user",
                content=text,
                sender_id=bot_id,
                sender_name=label,
            )
        except Exception as e:
            logger.debug("写入 turns 失败: %s", e)

        logger.info(
            "[跨插件感知] 已将群 %s 自身插件【%s】输出记入上下文: %s",
            gid,
            label,
            text[:50].replace("\n", " "),
        )


def install_cross_plugin_observer(runtime: Any) -> CrossPluginObserver:
    """在运行时挂载跨插件感知钩子。

    幂等：已经挂过的钩子带 `_geyi_hooked` 标记，重载时跳过（不套娃）；
    可撤销：每个替换都登记了还原回调，`Runtime.save()` 会调 `observer.detach()`。
    """
    observer = CrossPluginObserver(runtime)

    # 1. 钩入 OlivaDiceCore.crossHook (捕获骰娘专属检定/理智/掷骰等输出)
    try:
        import OlivaDiceCore  # type: ignore

        holder = OlivaDiceCore.crossHook.dictHookFunc
        orig_msg_hook = holder.get("msgHook")
        if getattr(orig_msg_hook, "_geyi_hooked", False):
            logger.info("骰娘消息感知钩子已在位，跳过重复挂载")
        else:
            @wraps(orig_msg_hook)
            def _hooked_dice_msg_hook(event, funcType, sender, dectData, message, *args, **kwargs):
                res = orig_msg_hook(event, funcType, sender, dectData, message, *args, **kwargs) if orig_msg_hook else None
                try:
                    if funcType in ("reply", "send_group") and dectData and len(dectData) >= 2:
                        group_id = str(dectData[1] or "")
                        if group_id:
                            bot_id = str((sender or {}).get("id", ""))
                            observer.on_other_plugin_sent(group_id, message, bot_id=bot_id)
                except Exception as ex:  # noqa: BLE001 - 绝不能打断骰娘自己的发信
                    observer._warn("OlivaDiceCore msgHook 捕获失败", ex)
                return res

            _hooked_dice_msg_hook._geyi_hooked = True  # type: ignore[attr-defined]
            holder["msgHook"] = _hooked_dice_msg_hook
            observer.add_detacher(lambda: holder.__setitem__("msgHook", orig_msg_hook))
            logger.info("已成功装载 OlivaDiceCore 骰娘消息感知钩子")
    except Exception as e:  # noqa: BLE001
        # 从 debug 提到 info：钩子没挂上时，跨插件感知会**悄悄失效**
        logger.info("未装载 OlivaDiceCore 骰娘钩子（模块不存在或结构变化）：%s", e)

    # 2. 钩入 OlivOSOnebotV11.eventRouter.txEvent.doRouter（捕获运势/签到等所有出站消息）
    try:
        import OlivOSOnebotV11  # type: ignore

        tx_event = OlivOSOnebotV11.eventRouter.txEvent
        orig_do_router = tx_event.doRouter
        if getattr(orig_do_router, "_geyi_hooked", False):
            logger.info("OneBot 出站感知钩子已在位，跳过重复挂载")
        else:
            @wraps(orig_do_router)
            def _hooked_do_router(self_arg, *args, **kwargs):
                res = orig_do_router(self_arg, *args, **kwargs)
                try:
                    func_type = getattr(self_arg, "funcType", "")
                    params = getattr(self_arg, "params", {}) or {}
                    group_id = ""
                    if func_type == "send_group_msg":
                        group_id = str(params.get("group_id", "") or "")
                    elif func_type == "send_msg" and params.get("message_type") == "group":
                        group_id = str(params.get("group_id", "") or "")

                    if group_id:
                        bot_id = ""
                        if hasattr(self_arg, "plugin_event") and hasattr(self_arg.plugin_event, "bot_info"):
                            bot_id = str(getattr(self_arg.plugin_event.bot_info, "id", "") or "")
                        msg_content = params.get("message", "")
                        observer.on_other_plugin_sent(group_id, msg_content, bot_id=bot_id)
                except Exception as ex:  # noqa: BLE001 - 绝不能打断出站消息
                    observer._warn("OlivOSOnebotV11 doRouter 捕获失败", ex)
                return res

            _hooked_do_router._geyi_hooked = True  # type: ignore[attr-defined]
            tx_event.doRouter = _hooked_do_router
            observer.add_detacher(lambda: setattr(tx_event, "doRouter", orig_do_router))
            logger.info("已成功装载 OlivOSOnebotV11 全局出站感知钩子")
    except Exception as e:  # noqa: BLE001
        logger.info("未装载 OlivOSOnebotV11 出站钩子（模块不存在或结构变化）：%s", e)

    return observer
