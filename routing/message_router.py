# -*- encoding: utf-8 -*-
"""消息路由：命令 -> 被动回复 -> 主动搭话。"""

from __future__ import annotations

import logging

from ..config import SKIP_ACTIVE  # noqa: F401  （保留导出，别的地方还在用）
from ..models import IncomingMessage
from .attention import AttentionGate

logger = logging.getLogger('newGeYi.router')


class MessageRouter:
    def __init__(self, runtime):
        self.r = runtime

    # ---------- 开关 ----------
    def _gate(self) -> AttentionGate:
        """闸门实例挂在 runtime 上；没有就现建一个（测试用的最小 runtime）。"""
        gate = getattr(self.r, 'attention', None)
        if gate is None:
            gate = AttentionGate(self.r.store, self.r.config)
            self.r.attention = gate
        return gate

    def _group_passive_on(self, msg: IncomingMessage) -> bool:
        val = self.r.store.get_setting(f'group_passive:{msg.session.as_text()}')
        return val != '0'

    # 注：这里曾经还有一个 `_group_active_on()`，跟 attention.py 里
    # `store.get_setting(f'group_active:{session_key}') == '0'` 是同一件事的两份实现，
    # 而且零调用（真正的判断一直在闸门里）。2026-10-30 删除，避免两份实现走样。

    def _debug(self, msg: IncomingMessage, directed: bool,
               passive_allowed: bool, reason: str) -> None:
        """route_debug 打开时，把路由判定写进日志。

        排查「她为什么不回话」时，最难的是分不清「没被点名」「被动关了」
        「发送者被当成机器人」这三种静默分支——它们都不产生任何输出。
        """
        if not self.r.config.get('route_debug'):
            return
        logger.info(
            '路由 | 群=%s 发送者=%s | 机器人=%s 点名=%s 被动放行=%s%s | %r',
            msg.is_group, msg.sender_id, msg.sender_is_bot, directed, passive_allowed,
            f' | {reason}' if reason else '',
            (msg.text or '')[:40],
        )

    def route(self, msg: IncomingMessage) -> None:
        cfg = self.r.config
        # 机器人/其他插件账号发来的消息一律不回应：腾讯系官方机器人被引用时会自动 @ 回复，
        # 与我们这边形成无限循环刷屏。消息仍会进旁观窗口，所以歌伊看得见、但不去接话。
        if msg.sender_is_bot:
            self._debug(msg, False, False, '发送者被判定为机器人账号')
            return
        # 自我保护静默：她判断这个群待不下去，静默期内不接话、也不主动搭话。
        # 消息照常进旁观窗口和记忆缓冲（只是在 route 之前就已经写完了），
        # 所以静默一结束她仍然知道这十分钟里群里发生了什么。
        session_key = msg.session.as_text()
        is_resting = getattr(self.r, 'is_resting', None)
        if callable(is_resting) and is_resting(session_key):
            self._debug(msg, False, False, '歌伊在本群自我保护静默中')
            return
        directed = bool(msg.mentioned_bot or self._trigger_hit(msg.text))
        gate = self._gate() if msg.is_group else None
        if gate is not None:
            # 每一条群消息都要过一遍：有人接话要记账，观察窗口到期要结算退避
            gate.observe_message(msg.session.as_text(), responded=directed)
        if msg.is_group:
            # 被动回复：全局被动 + 本群开关
            passive_allowed = bool(cfg.get('passive_enabled', True)) and self._group_passive_on(msg)
        else:
            passive_allowed = bool(cfg.get('private_enabled', False))
        self._debug(msg, directed, passive_allowed, '')

        # 被点名（@ 或命中触发词）：只走被动回复；
        # 被动被关时保持安静，不把「被点名」当成主动搭话的机会
        if directed:
            if passive_allowed:
                self.r.scheduler.submit(msg, kind='passive')
            return

        # 主动搭话：仅群聊，且与被动开关完全独立（本群被动关不影响主动）
        if msg.is_group:
            factor = 1.0
            images = getattr(self.r, 'images', None)
            if images is not None and images.is_bare_image(msg):
                # 纯表情包只是刷图，不是「有人找你说话」——概率打折，
                # 否则群里图一多她就变成见图就评（线上实测）
                factor = float(cfg.get('active_bare_image_factor', 0.3) or 0.0)
            decision = gate.evaluate(msg.session.as_text(), probability_factor=factor)
            if decision.allow:
                # 冷却从「决定去评估」这一刻开始算：不管模型最后说不说，
                # 都不该在下一条消息上再花一次 token
                gate.note_evaluated(msg.session.as_text())
                self.r.scheduler.submit(msg, kind='active')

    def _trigger_hit(self, text: str) -> bool:
        triggers = self.r.config.get('triggers', []) or ['歌伊']
        return any(kw in text for kw in triggers)
