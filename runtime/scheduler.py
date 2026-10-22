# -*- encoding: utf-8 -*-
"""会话调度器：同一会话 FIFO 有序，不同会话并行。

- 每个会话一个状态（pending 队列 + worker_active 标记）
- 只有队列为空时才释放 worker，保证同一会话同时最多一个任务在执行
- 全局线程池限制总并发，网络调用在队列外执行（不占用会话锁）
- 队列超过 max_pending_per_session 时丢弃新任务（防堆积）
- 「话没说完」时先按住几秒再回（见 _hold_seconds 的说明）
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..models import IncomingMessage

logger = logging.getLogger('newGeYi.scheduler')

# 「话没说完」的尾部标记（2026-09 新增）。
# 这些词出现在消息**结尾**时，通常意味着对方还要接着说；这时候抢答会显得很机械，
# 真人会等一下。表参考开源项目 qq-bridge 实战打磨的清单，只留下明显不完整的那些。
#
# 注意是**尾部**匹配而不是包含匹配——句中出现「然后」是正常表达，不该按住。
DEFAULT_INCOMPLETE_TAILS = (
    '你知道', '我跟你讲', '我跟你说', '其实吧', '但是', '所以说',
    '然后', '那个', '就是', '对了', '等我', '等等', '等一下', '等下',
    '我看看', '还有', '再说', '另外', '话说', '说起来', '那个啥',
)

# 按住期间的轮询间隔：够短才反应得过来，够长才不空转
_HOLD_TICK = 0.4


def looks_incomplete(text: str, tails=DEFAULT_INCOMPLETE_TAILS) -> bool:
    """消息是不是「话还没说完」。

    只看结尾：先去掉结尾的标点和空白（「你知道，」和「你知道」是一回事），
    再看剩下的是不是以某个未完标记收尾。
    """
    t = str(text or '').strip()
    if not t:
        return False
    t = t.rstrip(' \t\r\n。，、．.,!！?？~～;；:：')
    if not t:
        return False
    return any(t.endswith(str(tail)) for tail in tails if tail)


class _SessionState:
    __slots__ = ('pending', 'worker_active', 'last_active')

    def __init__(self):
        self.pending: collections.deque = collections.deque()
        self.worker_active = False
        self.last_active = time.time()


class SessionScheduler:
    def __init__(self, runtime, max_workers: int = 6):
        self.runtime = runtime
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='geyi'
        )
        self._states: dict[str, _SessionState] = {}
        self._lock = threading.Lock()

    def submit(self, msg: IncomingMessage, kind: str) -> bool:
        key = msg.session.as_text()
        job = (msg, kind)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _SessionState()
                self._states[key] = state
            state.last_active = time.time()
            max_pending = int(self.runtime.config.get('max_pending_per_session', 6))
            if len(state.pending) >= max_pending:
                return False
            state.pending.append(job)
            if state.worker_active:
                return True
            state.worker_active = True
            self._prune_locked()
        self._executor.submit(self._drain, key)
        return True

    # ---------- 话没说完时先按住 ----------
    def _hold_seconds(self, msg: IncomingMessage, kind: str) -> float:
        """这条任务该按住多久（0 = 不按）。

        只按**被动回复**：主动搭话本来就过了四道闸门，再按住只是徒增延迟。
        """
        if kind != 'passive':
            return 0.0
        try:
            hold = float(self.runtime.config.get('incomplete_hold_seconds', 8) or 0)
        except (TypeError, ValueError):
            return 0.0
        if hold <= 0:
            return 0.0
        tails = self.runtime.config.get('incomplete_tails') or DEFAULT_INCOMPLETE_TAILS
        if not looks_incomplete(getattr(msg, 'text', ''), tails):
            return 0.0
        return hold

    def _wait_or_superseded(self, state: _SessionState, msg, kind: str) -> bool:
        """按住几秒，看对方会不会接着说。

        返回 True = 本条被后一条取代，调用方应该丢弃它。
        为什么可以丢：后一条的上下文是从数据库现取的，**已经包含前一条**，
        回一次就够；两条都回反而像抢话、像逐条应答的机器人。
        """
        hold = self._hold_seconds(msg, kind)
        if hold <= 0:
            return False
        deadline = time.time() + hold
        while time.time() < deadline:
            time.sleep(_HOLD_TICK)
            with self._lock:
                pending = state.pending
                if not pending:
                    return False          # 队列空了（异常情况），照常处理
                if len(pending) > 1:
                    # 后面确实又来了一条。只有也是被动回复时才取代本条；
                    # 如果排的是主动搭话，那跟本条无关，别把人家的话吞了。
                    return pending[1][1] == 'passive'
        return False

    def _prune_locked(self) -> None:
        """清理超过 1 小时未活动且队列为空的会话状态。"""
        now = time.time()
        for key in list(self._states.keys()):
            st = self._states[key]
            if not st.pending and not st.worker_active and now - st.last_active > 3600:
                del self._states[key]

    def _drain(self, key: str) -> None:
        state = self._states.get(key)
        if state is None:
            return
        try:
            while True:
                with self._lock:
                    if not state.pending:
                        state.worker_active = False
                        return
                    msg, kind = state.pending[0]
                # 未完待续：按住几秒。被后一条取代就直接丢，不重复回话。
                if self._wait_or_superseded(state, msg, kind):
                    with self._lock:
                        if state.pending and state.pending[0][0] is msg:
                            state.pending.popleft()
                    continue
                with self._lock:
                    if not state.pending:
                        state.worker_active = False
                        return
                    msg, kind = state.pending.popleft()
                try:
                    self.runtime.process_job(msg, kind)
                except Exception:  # noqa: BLE001 - 单个任务失败不影响会话继续
                    logger.exception('session %s job failed', key)
        finally:
            with self._lock:
                if not state.pending:
                    state.worker_active = False

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
