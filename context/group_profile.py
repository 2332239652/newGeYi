# -*- encoding: utf-8 -*-
"""群档案：拿群人数，用来给每个群算一个合理的每日额度。

为什么需要它：歌伊是公开骰子，谁都能把她拉进新群。只有**全局**日预算的话，
一个 5 人的小群可以疯狂 @ 她，把额度吃光、把账单顶上去，而正经的大群反而没得用。
按群人数给每个群一个上限，就把「恶意小群」的天花板压到很低。

接口来源（实测确认，不是猜的）：
- OlivOS 官方的 GROUP 类型只有 `max_member_count`（群**容量**上限，如 500），
  **不是**当前人数——拿它当人数会把 3 人群也当成 500 人群；
- 实际人数只能靠 `plugin_event.get_group_member_list(group_id)` 数 `data` 的长度
  （服务器上的 jrlp 插件就是这么用的，返回结构是 {'active': bool, 'data': [...]}）。

取人数要发一次接口调用，而且 500 人群的返回很大，所以：
- 结果缓存进 settings（默认 12 小时）；
- **在后台线程里取**，绝不阻塞消息处理（取不到就先用默认档）。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger('newGeYi.group')

CACHE_TTL = 12 * 3600


class GroupProfile:
    """按群缓存人数，并提供「按人数算额度」的纯函数。"""

    def __init__(self, store, config: dict):
        self.store = store
        self.config = config
        self._inflight: set = set()
        self._lock = threading.Lock()

    # ==================== 缓存 ====================
    def _count_key(self, session_key: str) -> str:
        return f'group.members:{session_key}'

    def _at_key(self, session_key: str) -> str:
        return f'group.members_at:{session_key}'

    def cached(self, session_key: str) -> int:
        """已缓存的人数；没有或过期返回 0（0 = 未知，调用方用默认档）。"""
        try:
            count = int(self.store.get_setting(self._count_key(session_key)) or 0)
        except (TypeError, ValueError):
            return 0
        if count <= 0:
            return 0
        try:
            fetched = float(self.store.get_setting(self._at_key(session_key)) or 0)
        except (TypeError, ValueError):
            return 0
        ttl = float(self.config.get('group_profile_ttl_seconds', CACHE_TTL) or CACHE_TTL)
        if time.time() - fetched > ttl:
            return 0
        return count

    def save(self, session_key: str, count: int) -> None:
        if count <= 0:
            return
        self.store.set_setting(self._count_key(session_key), str(int(count)))
        self.store.set_setting(self._at_key(session_key), str(time.time()))

    # ==================== 取人数 ====================
    def refresh_async(self, session_key: str, event, group_id) -> None:
        """后台取一次群人数；同一群同时只跑一个，绝不阻塞调用方。"""
        if self.cached(session_key):
            return
        with self._lock:
            if session_key in self._inflight:
                return
            self._inflight.add(session_key)
        thread = threading.Thread(
            target=self._fetch_worker, args=(session_key, event, group_id),
            name='geyi-group-profile', daemon=True,
        )
        thread.start()

    def _fetch_worker(self, session_key: str, event, group_id) -> None:
        try:
            count = self.fetch(event, group_id)
            if count:
                self.save(session_key, count)
                logger.info('群人数已缓存：%s = %d 人', session_key[-12:], count)
        except Exception as exc:  # noqa: BLE001 - 取不到就用默认档，不影响任何功能
            logger.warning('取群人数失败（%s）：%s', session_key[-12:], type(exc).__name__)
        finally:
            with self._lock:
                self._inflight.discard(session_key)

    def fetch(self, event, group_id) -> int:
        """同步取群人数（只在后台线程里调）。取不到返回 0。"""
        if event is None:
            return 0
        try:
            res = event.get_group_member_list(group_id)
        except Exception:  # noqa: BLE001
            return 0
        if not isinstance(res, dict) or not res.get('active'):
            return 0
        data = res.get('data')
        return len(data) if isinstance(data, list) else 0

    # ==================== 按人数算额度 ====================
    def quota_for(self, session_key: str) -> dict:
        """这个群今天的调用额度。

        公式：base + 人数 × factor，再夹在 floor~cap 之间。
        默认值（30 + 人数×0.4，夹在 40~250）算出来的效果：
            10 人群 → 40 次/天      50 人群 → 50 次/天
            100 人群 → 70 次/天     500 人群 → 230 次/天
        人数未知时用 default（60）——宁可给保守值，等后台取到了再放开。
        """
        cfg = self.config
        members = self.cached(session_key)
        if members <= 0:
            return {'members': 0, 'quota': int(cfg.get('group_quota_default', 60) or 60),
                    'known': False}
        base = float(cfg.get('group_quota_base', 30) or 0)
        factor = float(cfg.get('group_quota_per_member', 0.4) or 0)
        floor = float(cfg.get('group_quota_min', 40) or 0)
        cap = float(cfg.get('group_quota_max', 250) or 0)
        quota = max(floor, min(cap, base + members * factor))
        return {'members': members, 'quota': int(quota), 'known': True}

    def used_today(self, session_key: str) -> int:
        try:
            return int(self.store.count_calls_today(session_key))
        except Exception:  # noqa: BLE001 - 统计读不到就不拦
            return 0

    def allow(self, session_key: str) -> tuple:
        """这个群今天还能不能调模型。返回 (是否允许, 说明)。"""
        if not self.config.get('group_quota_enabled', True):
            return True, ''
        info = self.quota_for(session_key)
        used = self.used_today(session_key)
        if used >= info['quota']:
            return False, '本群今日额度用完（%d/%d%s）' % (
                used, info['quota'], '' if info['known'] else '，人数未知按默认档')
        return True, ''

    def status_text(self, session_key: str) -> str:
        info = self.quota_for(session_key)
        used = self.used_today(session_key)
        people = ('%d 人' % info['members']) if info['known'] else '未知（后台正在取）'
        return '本群: %s | 今日调用 %d/%d 次' % (people, used, info['quota'])
