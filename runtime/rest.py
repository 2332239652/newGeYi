# -*- encoding: utf-8 -*-
"""自我保护静默（rest）：允许歌伊自己判断「这个群我待不下去了」，安静一段时间。

为什么要有它（2026-10）：
模型是能判断语境的，遇到持续辱骂、过当调戏、针对角色人格的侮辱、被同一个人
连续纠缠追问时，以前她只能硬扛，或者干脆把整轮输出吞掉（表现为「她突然不说话，
但你不知道是坏了还是不想说」）。现在给她一个明确的退场方式：

- 输出 `[REST:简短原因]`，系统把**那个会话**静默 N 分钟（默认 10）；
- 同时私聊通知骰主，带上触发者、原因、原话，方便事后判断是误判还是真越界；
- 到期自动恢复，不需要任何人操作；骰主也可以发「解除静默」提前解除。

状态存在 settings 表里（`rest:<session_key>`），所以重载插件不会丢。
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger('newGeYi.rest')

_KEY_PREFIX = 'rest:'


class RestGuard:
    """按会话（群/私聊）记录静默截止时间。"""

    def __init__(self, store, config: dict):
        self.store = store
        self.config = config

    # ---------- 配置 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get('rest_enabled', True))

    @property
    def minutes(self) -> float:
        try:
            value = float(self.config.get('rest_minutes', 10))
        except (TypeError, ValueError):
            return 10.0
        return value if value > 0 else 10.0

    # ---------- 状态 ----------
    def _key(self, session_key: str) -> str:
        return _KEY_PREFIX + str(session_key or '')

    def _read(self, session_key: str) -> dict:
        if not session_key:
            return {}
        try:
            raw = self.store.get_setting(self._key(session_key))
            data = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 - 状态坏了当作没静默
            logger.exception('读取静默状态失败')
            return {}
        return data if isinstance(data, dict) else {}

    def until(self, session_key: str) -> float:
        try:
            return float(self._read(session_key).get('until') or 0)
        except (TypeError, ValueError):
            return 0.0

    def is_resting(self, session_key: str) -> bool:
        return self.until(session_key) > time.time()

    def remaining(self, session_key: str) -> int:
        """剩余秒数（向上取整）；不在静默中返回 0。"""
        left = self.until(session_key) - time.time()
        return int(left + 0.999) if left > 0 else 0

    def reason(self, session_key: str) -> str:
        return str(self._read(session_key).get('reason') or '')

    def start(self, session_key: str, reason: str = '', by: str = '',
              minutes: float | None = None) -> float:
        """开始静默，返回截止时间戳。"""
        span = float(minutes) if minutes else self.minutes
        until = time.time() + span * 60.0
        payload = {
            'until': until,
            'reason': str(reason or '').strip()[:40],
            'by': str(by or '').strip()[:40],
            'started_at': time.time(),
        }
        try:
            self.store.set_setting(self._key(session_key),
                                   json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 写不进去也不能让回复流程炸掉
            logger.exception('写入静默状态失败')
        return until

    def clear(self, session_key: str) -> bool:
        """提前解除静默，返回原本是否在静默中。"""
        had = self.is_resting(session_key)
        try:
            self.store.set_setting(self._key(session_key), '')
        except Exception:  # noqa: BLE001
            logger.exception('清除静默状态失败')
        return had
