# -*- encoding: utf-8 -*-
"""群聊实时上下文。

这是一个短时、内存中的旁观窗口，不是长期记忆：
- 只保留最近一段时间的群消息；
- 同一会话按到达顺序保存；
- 每条消息都记下发送者 QQ 号，避免同名群友在模型眼里变成同一个人；
- Prompt 按当前消息的 arrival_seq 截断，避免把“未来消息”泄露给较早任务；
- 进程重启后自然清空，旧消息不会重新注入群聊上下文。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveTurn:
    order: int
    source_seq: int
    role: str
    content: str
    sender_name: str = ''
    # 真实 QQ 号：记忆按 QQ 号隔离，喂给模型的文本也必须带 QQ 号，
    # 否则两个同昵称的群友在系统里是两个人、在模型眼里却是同一个人（记忆串味）。
    sender_id: str = ''
    created_at: float = 0.0


class LiveContextStore:
    """按 session_key 隔离的短时群聊消息环形缓存。"""

    def __init__(self, max_cached_per_session: int = 120):
        self.max_cached_per_session = max_cached_per_session
        self._states: dict[str, deque[LiveTurn]] = defaultdict(deque)
        self._lock = threading.RLock()
        self._order = 0

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def append_user(self, msg) -> None:
        """收到群消息后立即写入，不等待模型结果。"""
        self.append(
            session_key=msg.session.as_text(),
            source_seq=msg.arrival_seq,
            role='user',
            content=msg.text,
            sender_name=msg.sender_name,
            sender_id=msg.sender_id,
        )

    def append_assistant(self, session_key: str, source_seq: int, content: str) -> None:
        """成功发送回复后写入，让后续群消息能看到歌伊刚才说的话。"""
        self.append(
            session_key=session_key,
            source_seq=source_seq,
            role='assistant',
            content=content,
            sender_name='歌伊',
        )

    def append(self, session_key: str, source_seq: int, role: str, content: str,
               sender_name: str = '', sender_id: str = '') -> None:
        if not content:
            return
        now = time.time()
        with self._lock:
            turn = LiveTurn(
                order=self._next_order(),
                source_seq=source_seq,
                role=role,
                content=str(content),
                sender_name=sender_name or ('歌伊' if role == 'assistant' else '用户'),
                sender_id=str(sender_id or ''),
                created_at=now,
            )
            state = self._states[session_key]
            state.append(turn)
            while len(state) > self.max_cached_per_session:
                state.popleft()
            self._prune_locked(now)

    def snapshot(
        self,
        session_key: str,
        before_source_seq: int,
        window_seconds: int = 1800,
        max_messages: int = 30,
        max_chars: int = 6000,
    ) -> list[dict]:
        """取当前消息之前的实时上下文，返回与 SQLite turns 相同的字典结构。"""
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            state = list(self._states.get(session_key, ()))

        candidates = [
            turn for turn in state
            if turn.source_seq < before_source_seq and now - turn.created_at <= window_seconds
        ]
        # 回复可能晚于下一条用户消息落缓存，按原始消息序号恢复因果顺序。
        # 同一序号内用户消息先于 assistant 回复。
        candidates.sort(key=lambda turn: (turn.source_seq, 0 if turn.role == 'user' else 1, turn.order))
        # 从最新消息向前取，防止较早的大段消息挤掉当前话题
        selected: list[LiveTurn] = []
        used = 0
        for turn in reversed(candidates):
            line_len = len(turn.content) + len(turn.sender_name) + 2
            if selected and (len(selected) >= max_messages or used + line_len > max_chars):
                break
            if not selected and line_len > max_chars:
                selected.append(turn)
                break
            selected.append(turn)
            used += line_len
        selected.reverse()
        return [
            {
                'role': turn.role,
                'sender_id': turn.sender_id,
                'sender_name': turn.sender_name,
                'content': turn.content,
            }
            for turn in selected
        ]

    def _prune_locked(self, now: float) -> None:
        # 只做一个较宽的内存清理阈值，真正的业务窗口由 snapshot 参数控制
        cutoff = now - 2 * 60 * 60
        for key in list(self._states.keys()):
            state = self._states[key]
            while state and state[0].created_at < cutoff:
                state.popleft()
            if not state:
                self._states.pop(key, None)
