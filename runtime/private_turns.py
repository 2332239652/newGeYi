# -*- encoding: utf-8 -*-
"""Rule-based private-chat turn coalescing and reply priority."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..models import IncomingMessage


_DIRECT_RE = re.compile(
    r'(?:歌伊|小歌伊|阿歌伊).{0,12}(?:在吗|听我|帮我|陪我|告诉我|回答|看看|想想|觉得|解释|建议|好吗|可以吗)'
    r'|(?:帮我|陪我|告诉我|回答我|给我建议|听我说|和我聊聊|跟我聊聊)'
    r'|(?:你觉得呢|你怎么看|你会怎么|能不能.{0,8}(?:帮|说|看|想)|可以.{0,8}(?:帮|说|看))',
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r'(?:[?？]|吗[？?]?$|呢[？?]?$|怎么(?:办|看|做|样)|为什么|是不是|要不要|能否|是否)')
_EMOTION_RE = re.compile(r'累|难过|伤心|委屈|害怕|焦虑|崩溃|开心|高兴|想你|孤单|烦|压力|撑不住|哭')
_ACK_RE = re.compile(r'^(?:嗯+|哦+|好(?:的|哦)?|行|收到|知道了|可以|对|是的|哈哈+|笑死|谢(?:谢|啦)|晚安|早安)[。！!～~…]*$')
_MEDIA_RE = re.compile(r'^(?:\【(?:图片|表情|语音|视频)\】|\[CQ:(?:image|face|record|video)[^]]*\])+$', re.IGNORECASE)


@dataclass
class _PendingTurn:
    messages: list[IncomingMessage] = field(default_factory=list)
    timer: threading.Timer | None = None
    first_at: float = field(default_factory=time.monotonic)
    mode: str = 'natural'


def _append_message(messages: list[IncomingMessage]) -> IncomingMessage:
    """Keep one current turn; each original message is already retained in L0 buffer."""
    if len(messages) == 1:
        return messages[0]
    latest = messages[-1]
    text = '\n'.join(str(msg.text or '').strip() for msg in messages if str(msg.text or '').strip())
    return IncomingMessage(
        session=latest.session, message_id=latest.message_id,
        sender_id=latest.sender_id, sender_name=latest.sender_name,
        text=text, raw_text=text, is_group=False, mentioned_bot=any(m.mentioned_bot for m in messages),
        event=latest.event, proc=latest.proc, arrival_seq=latest.arrival_seq,
        sender_is_bot=latest.sender_is_bot, memory_space_id=latest.memory_space_id,
        space_generation=latest.space_generation, memory_record_id=latest.memory_record_id,
        history_pre_recorded=True, coalesced_messages=tuple(messages),
    )


def strip_current_turn(history, msg: IncomingMessage) -> tuple[dict, ...]:
    """从"前序历史"里去掉本轮自己（到达即落库造成的自我污染）。

    私聊的用户轮在**收到的那一刻**就写进 turns 了（见 `RuntimeApp.handle_message`），
    所以 `process_job` 里再取 `recent_turns`，"当前这句话"已经在历史里。
    旧代码注释以为快照发生在写入之前，于是复读判定 `is_repeat` 拿这句话和它自己比，
    相似度恒为 1.0 -> 每一条私聊都被判成刷屏扣分，好感度只跌不涨、评审模型从不被调用。

    这里只从**尾部**按文本精确匹配剥掉本轮的那些行（合轮的每一句各占一行），
    剥掉的条数不超过本轮实际的消息数，因此不会误删上一轮真正重复过的同一句话。
    """
    rows = [dict(turn) for turn in (history or ()) if isinstance(turn, dict)]
    if not rows:
        return ()
    coalesced = tuple(getattr(msg, 'coalesced_messages', ()) or ())
    wanted = {str(getattr(item, 'text', '') or '').strip() for item in coalesced}
    joined = str(getattr(msg, 'text', '') or '').strip()
    if joined:
        wanted.add(joined)
    wanted.discard('')
    if not wanted:
        return tuple(rows)
    budget = max(1, len(coalesced))
    dropped = 0
    while rows and dropped < budget:
        last = rows[-1]
        if str(last.get('role') or '') != 'user':
            break
        if str(last.get('content') or '').strip() not in wanted:
            break
        rows.pop()
        dropped += 1
    return tuple(rows)


def is_direct_invitation(text: str) -> bool:
    return bool(_DIRECT_RE.search(str(text or '')))


def classify_private_turn(runtime, msg: IncomingMessage, messages: list[IncomingMessage], mode: str) -> tuple[bool, str, int]:
    """Return (reply, reason, score) using local text and recent-turn cues only."""
    text = '\n'.join(str(item.text or '').strip() for item in messages)
    if is_direct_invitation(text):
        return True, 'explicit_invitation', 100
    if mode == 'focused':
        triggered = bool(msg.mentioned_bot)
        try:
            triggered = triggered or runtime.router._private_gate().hit_trigger(text)
        except Exception:
            pass
        return triggered, 'focused_mode', 100 if triggered else 0

    score = 0
    if _QUESTION_RE.search(text):
        score += 2
    if _EMOTION_RE.search(text):
        score += 2
    if len(text) >= 18:
        score += 1
    if _ACK_RE.fullmatch(text.strip()):
        score -= 4
    if _MEDIA_RE.fullmatch(text.strip()):
        score -= 4
    if len(messages) > 1:
        score += 1

    # A direct answer to the character's latest question is a strong continuation cue.
    try:
        prior = runtime.store.recent_turns(msg.session.as_text(), limit=4)
        last_assistant = next((row.get('content', '') for row in reversed(prior)
                               if row.get('role') == 'assistant'), '')
        if last_assistant and _QUESTION_RE.search(str(last_assistant)):
            score += 2
    except Exception:
        pass

    threshold = 1 if mode == 'warm' else 2
    return score >= threshold, 'local_priority', score


class PrivateTurnCoordinator:
    """Debounce rapid private messages into one serialized reply task."""

    def __init__(self, runtime, flush: Callable[[list[IncomingMessage]], None]):
        self.runtime = runtime
        self.flush_callback = flush
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingTurn] = {}
        self._closed = False

    def offer(self, msg: IncomingMessage) -> None:
        key = msg.session.as_text()
        session_mode = self.runtime.store.get_setting('private_companion:mode:' + key)
        mode = str(session_mode or self.runtime.config.get('private_companion_mode', 'natural') or 'natural')
        if self.runtime.config.get('private_trigger_only'):
            mode = 'focused'
        immediate = bool(_DIRECT_RE.search(str(msg.text or '')))
        delay_key = 'private_companion_invite_delay_seconds' if immediate else 'private_companion_debounce_seconds'
        try:
            delay = float(self.runtime.config.get(delay_key, 0.6 if immediate else 2.5))
        except (TypeError, ValueError):
            delay = 0.6 if immediate else 2.5
        delay = max(0.05, min(8.0, delay))

        with self._lock:
            if self._closed:
                return
            state = self._pending.get(key)
            if state is None:
                state = _PendingTurn(mode=mode)
                self._pending[key] = state
            state.messages.append(msg)
            if state.timer is not None:
                state.timer.cancel()
            elapsed = time.monotonic() - state.first_at
            max_wait = max(delay, min(8.0, float(self.runtime.config.get('private_companion_max_wait_seconds', 5.0) or 5.0)))
            wait = min(delay, max(0.05, max_wait - elapsed))
            timer = threading.Timer(wait, self._flush, args=(key, state))
            timer.daemon = True
            state.timer = timer
            timer.start()

    def _flush(self, key: str, expected: _PendingTurn) -> None:
        with self._lock:
            if self._closed or self._pending.get(key) is not expected:
                return
            self._pending.pop(key, None)
            messages = list(expected.messages)
        if not messages:
            return
        msg = messages[-1]
        quiet_key = 'private_companion:quiet:' + key
        try:
            quiet = str(self.runtime.store.get_setting(quiet_key) or '') == '1'
        except Exception:
            quiet = False
        if quiet:
            reason, score = 'quiet_mode', 0
            reply = False
        else:
            mode = expected.mode
            reply, reason, score = classify_private_turn(self.runtime, msg, messages, mode)
        logger = getattr(self.runtime, 'log', None)
        if callable(logger):
            try:
                logger(2, f'私聊回合判定 mode={mode if not quiet else "quiet"} n={len(messages)} reply={reply} reason={reason} score={score} | {key[-16:]}')
            except Exception:
                pass
        if reply:
            self.flush_callback(messages)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for state in self._pending.values():
                if state.timer is not None:
                    state.timer.cancel()
            self._pending.clear()
