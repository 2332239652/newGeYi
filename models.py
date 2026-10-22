# -*- encoding: utf-8 -*-
"""运行时数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SessionKey:
    bot_hash: str
    platform: str
    sdk: str
    session_type: str
    host_id: str = ''
    target_id: str = ''

    def as_text(self) -> str:
        parts = (self.bot_hash, self.platform, self.sdk, self.session_type, self.host_id, self.target_id)
        return ':'.join(part or '-' for part in parts)


@dataclass(frozen=True)
class IncomingMessage:
    session: SessionKey
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    raw_text: str
    is_group: bool
    mentioned_bot: bool
    event: Any = field(compare=False, hash=False, repr=False)
    proc: Any = field(compare=False, hash=False, repr=False)
    # 由 Runtime 分配的进程内到达序号，用于实时上下文的因果截断
    arrival_seq: int = 0
    # 发送者是否为机器人/其他插件账号（防「机器人互相引用」的刷屏循环）
    sender_is_bot: bool = False


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, str]]
    model: str
    temperature: float = 0.7
    max_tokens: int = 500
    request_id: str = ''


@dataclass(frozen=True)
class ModelResult:
    ok: bool
    text: str = ''
    model: str = ''
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error_code: str = ''
    error_message: str = ''


@dataclass(frozen=True)
class RenderedReply:
    text: str
    emote: str = ''
    mention_current_user: bool = False
