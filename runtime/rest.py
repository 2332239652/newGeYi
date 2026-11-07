# -*- encoding: utf-8 -*-
"""自我保护静默（rest）：允许歌伊自己判断「这个群我待不下去了」，安静一段时间。

为什么要有它（2026-10）：
模型是能判断语境的，遇到持续辱骂、过当调戏、针对角色人格的侮辱、被同一个人
连续纠缠追问时，以前她只能硬扛，或者干脆把整轮输出吞掉（表现为「她突然不说话，
但你不知道是坏了还是不想说」）。现在给她一个明确的退场方式：

- 输出 `[REST:简短原因]`，系统把**那个会话**静默 N 分钟（默认 10）；
- **私聊例外**（2026-11-04 起）：私聊里的骚扰退场是**无限期**的，只有骰主在
  那个私聊窗口发「解除静默」才会恢复。群里是公共场合，静默到期自己回来；
  而私聊是一对一的"追着她说"，等她自动回来只会让同一套话术再来一次 ——
  所以这里让骰主一个人决定什么时候放她出来（`rest_private_forever`）。
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
FOREVER = -1.0          # remaining() 的哨兵值：无限期静默


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

    @property
    def private_forever(self) -> bool:
        """私聊里的退场是否无限期（默认开）。"""
        return bool(self.config.get('rest_private_forever', True))

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

    def is_forever(self, session_key: str) -> bool:
        """这个会话是不是"无限期静默"（私聊骚扰退场）。"""
        return bool(self._read(session_key).get('forever'))

    def is_resting(self, session_key: str) -> bool:
        if self.is_forever(session_key):
            return True
        return self.until(session_key) > time.time()

    def remaining(self, session_key: str) -> int:
        """剩余秒数（向上取整）；无限期返回 -1；不在静默中返回 0。"""
        if self.is_forever(session_key):
            return int(FOREVER)
        left = self.until(session_key) - time.time()
        return int(left + 0.999) if left > 0 else 0

    def reason(self, session_key: str) -> str:
        return str(self._read(session_key).get('reason') or '')

    def start(self, session_key: str, reason: str = '', by: str = '',
              minutes: float | None = None, forever: bool = False) -> float:
        """开始静默，返回截止时间戳（无限期时返回 0，靠 `forever` 字段判断）。"""
        span = float(minutes) if not forever and minutes else self.minutes
        until = 0.0 if forever else time.time() + span * 60.0
        payload = {
            'until': until,
            'forever': bool(forever),
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

    def resting_sessions(self) -> list:
        """列出所有仍在静默里的会话 → [(session_key, remaining, reason, forever)]。

        为什么需要它：私聊的退场是**无限期**的，而解除命令只作用在"发命令的那个
        会话"。陌生人私聊骚扰她就等于被永久静默，骰主在自己的窗口里根本碰不到那个
        会话 —— 必须能按 QQ 号点名解除，也就必须先能枚举。
        """
        out = []
        try:
            rows = self.store.settings_with_prefix(_KEY_PREFIX)
        except Exception:  # noqa: BLE001 - 枚举失败当作没有
            logger.exception('枚举静默状态失败')
            return out
        now = time.time()
        for key, raw in rows:
            session_key = str(key)[len(_KEY_PREFIX):]
            try:
                data = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict) or not data:
                continue
            forever = bool(data.get('forever'))
            try:
                left = -1 if forever else int(float(data.get('until') or 0) - now)
            except (TypeError, ValueError):
                continue
            if not forever and left <= 0:
                continue
            out.append((session_key, int(FOREVER) if forever else left,
                        str(data.get('reason') or ''), forever))
        return out
