# -*- encoding: utf-8 -*-
"""会话调度器：同一会话 FIFO 有序，不同会话并行。

- 每个会话一个状态（pending 队列 + worker_active 标记）
- 只有队列为空时才释放 worker，保证同一会话同时最多一个任务在执行
- 全局线程池限制总并发，网络调用在队列外执行（不占用会话锁）
- 队列超过 max_pending_per_session 时丢弃新任务（防堆积）
- 「话没说完」时先按住几秒再回（见 _hold_seconds 的说明）

⚠️ 2026-11-01：这个模块以前有三条**完全静默**的路径——队列满丢消息、worker 标记卡住、
_drain 找不到会话状态。线上表现就是「@她完全不理，日志一片干净、api_calls 一条都没有」，
事后只能靠数据库和 py-spy 反推。现在每一条都留日志，并且加了看门狗：
worker 的 Future 已结束或缺失且标记失效时恢复；耗时长但仍存活的任务只告警，不并发接管。
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

# 看门狗阈值只用于告警或恢复无 Future 的遗留标记，不能用时长推断线程死亡。
STALE_DRAIN_SECONDS = 300.0

# 队列满的告警节流：同一个会话 60 秒内只喊一次，别把日志刷爆
_FULL_WARN_INTERVAL = 60.0


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
    __slots__ = ('pending', 'worker_active', 'last_active', 'last_progress_at', 'warned_full_at', 'future', 'generation')

    def __init__(self):
        self.pending: collections.deque = collections.deque()
        self.worker_active = False
        self.last_active = time.time()
        self.future = None
        self.generation = 0
        # 上一次任务起步时间仅供诊断
        self.last_progress_at = time.time()
        # 「队列满」告警的节流时间
        self.warned_full_at = 0.0


class SessionScheduler:
    def __init__(self, runtime, max_workers: int = 6):
        self.runtime = runtime
        self.max_workers = int(max_workers)
        self._executor = self._new_executor()
        self._states: dict[str, _SessionState] = {}
        self._lock = threading.Lock()
        self._closed = False

    def _new_executor(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix='geyi')

    # ---------- 提交 ----------
    def submit(self, msg: IncomingMessage, kind: str) -> bool:
        key = msg.session.as_text()
        job = (msg, kind)
        now = time.time()
        with self._lock:
            if self._closed:
                logger.warning('调度器已关闭，本条不再接收 | %s', key[-24:])
                return False
            state = self._states.get(key)
            if state is None:
                state = _SessionState()
                self._states[key] = state
            state.last_active = now
            if state.worker_active:
                future = state.future
                if (future is not None and future.done()) or (
                    future is None and now - state.last_progress_at > STALE_DRAIN_SECONDS
                ):
                    logger.warning('会话队列疑似卡死：任务已结束或缺失，恢复 drain | %s', key[-24:])
                    state.worker_active = False
                    state.future = None
                elif now - state.last_progress_at > STALE_DRAIN_SECONDS:
                    # Future 仍在运行或等待线程：不能启动第二个 drain。
                    if now - state.warned_full_at > _FULL_WARN_INTERVAL:
                        state.warned_full_at = now
                        logger.warning('会话任务耗时较长，保留现有 drain 等待返回 | %s', key[-24:])
            max_pending = int(self.runtime.config.get('max_pending_per_session', 6))
            if len(state.pending) >= max_pending:
                # 容量拒绝不应让一次 executor 提交失败留下的已接受积压永远休眠。
                if state.pending and not state.worker_active:
                    self._kick_locked(key, state)
                if now - state.warned_full_at > _FULL_WARN_INTERVAL:
                    state.warned_full_at = now
                    logger.warning(
                        '会话队列已满（%d/%d），本条被丢弃 | %s | %r',
                        len(state.pending), max_pending, key[-24:], (msg.text or '')[:30],
                    )
                return False
            state.pending.append(job)
            if not state.worker_active:
                self._prune_locked()
                if not self._kick_locked(key, state):
                    state.pending.remove(job)
                    return False
        return True

    def _kick_locked(self, key: str, state: _SessionState) -> bool:
        """在同一把锁内保留所有权并记录 Future，消除提交窗口与退出交接竞态。"""
        if self._closed or state.worker_active:
            return False
        state.generation += 1
        generation = state.generation
        state.worker_active = True
        state.last_progress_at = time.time()
        try:
            state.future = self._executor.submit(self._drain, key, generation)
            return True
        except Exception:  # noqa: BLE001
            logger.exception('提交调度任务失败 | %s', key[-24:])
            state.worker_active = False
            state.future = None
            return False

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
        # 配置里写 [] 必须是「关掉」，不能掉回默认词表——以前 `[] or DEFAULT` 让这个
        # 开关形同虚设：明明关了，每条以「然后/就是/话说」结尾的消息还是被按住 8 秒，
        # 忙群里再被后一条顶掉，回复就直接消失了。
        tails = self.runtime.config.get('incomplete_tails')
        if tails is None:
            tails = DEFAULT_INCOMPLETE_TAILS
        if not isinstance(tails, (list, tuple, set)):
            tails = DEFAULT_INCOMPLETE_TAILS
        if not looks_incomplete(getattr(msg, 'text', ''), tails):
            return 0.0
        return hold

    def _wait_or_superseded(self, state: _SessionState, msg, kind: str) -> bool:
        """按住几秒，看对方会不会接着说。

        返回 True = 本条被后一条取代，调用方应该丢弃它。
        为什么可以丢：后一条的上下文是从数据库现取的，**已经包含前一条**，
        回一次就够；两条都回反而像抢话、像逐条应答的机器人。
        （丢弃会留一行 INFO，以前是纯静默的，排查时看起来就像「她没收到这条」。）
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
                    if pending[1][1] == 'passive':
                        logger.info('「话没说完」：本条被后一条取代，不再单独回 | %r',
                                    (getattr(msg, 'text', '') or '')[:30])
                        return True
                    return False
        return False

    def _prune_locked(self) -> None:
        """清理超过 1 小时未活动且队列为空的会话状态。"""
        now = time.time()
        for key in list(self._states.keys()):
            st = self._states[key]
            if not st.pending and not st.worker_active and now - st.last_active > 3600:
                del self._states[key]

    def _drain(self, key: str, generation=None) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is not None and generation is None:
                generation = state.generation
        if state is None:
            # 以前这里静默 return，等于把这个会话的下一条消息永远埋了
            logger.warning('drain 找不到会话状态（已被清理？），本轮丢弃 | %s', key[-24:])
            return
        try:
            while True:
                with self._lock:
                    if self._closed or state.generation != generation or not state.pending:
                        return
                    msg, kind = state.pending[0]
                    # 每起步一条任务就算一次「有进展」，看门狗据此判断死活
                    state.last_progress_at = time.time()
                    state.worker_active = True
                # 未完待续：按住几秒。被后一条取代就直接丢，不重复回话。
                if self._wait_or_superseded(state, msg, kind):
                    with self._lock:
                        if state.pending and state.pending[0][0] is msg:
                            state.pending.popleft()
                    # 私聊消息被取代时补写进 SQLite turns，防止私聊上下文永久丢失
                    if not getattr(msg, 'is_group', True) and hasattr(self.runtime, 'store'):
                        try:
                            self.runtime.store.add_turn(
                                msg.session.as_text(), 'user', msg.text, msg.sender_id, msg.sender_name
                            )
                        except Exception:
                            pass
                    continue
                with self._lock:
                    if self._closed or state.generation != generation or not state.pending:
                        return
                    msg, kind = state.pending.popleft()
                try:
                    # 开工/收工各留一行：没有这两行的话，「她没回」到底是没进队列、
                    # 还是卡在生成里，日志上完全分不出来（2026-11-01 排障时最缺的就是这个）。
                    logger.info('任务开始 kind=%s | %s | %r',
                                kind, key[-16:], (msg.text or '')[:30])
                    self.runtime.process_job(msg, kind)
                    # 「开始了」但没有 api_calls、也没有「LLM error」→ 说明是 process_job
                    # 内部的静默早退，现在那些分支都各自留了 warning，可以对上号。
                    logger.info('任务返回 kind=%s | %s', kind, key[-16:])
                except Exception:  # noqa: BLE001 - 单个任务失败不影响会话继续
                    # 现在会连栈一起进日志文件了（见 _ProcLogHandler），以前整条栈是丢的
                    logger.exception('会话任务失败（kind=%s）| %s', kind, key[-24:])
        finally:
            # 所有权只在这里释放；新提交与剩余积压的交接都在同一锁内完成。
            with self._lock:
                if state.generation == generation:
                    state.worker_active = False
                    state.future = None
                    if state.pending and not self._closed:
                        logger.warning('drain 退出时仍有 %d 条积压，重新排队 | %s',
                                       len(state.pending), key[-24:])
                        self._kick_locked(key, state)

    # ---------- 诊断 ----------
    def pending_snapshot(self) -> dict:
        """给「歌伊状态」用：现在有多少条任务在排队、worker 标记是否还挂着。"""
        with self._lock:
            busy = {key: len(st.pending) for key, st in self._states.items() if st.pending}
            workers = sum(1 for st in self._states.values() if st.worker_active)
            executor_down = self._closed or bool(getattr(self._executor, '_shutdown', False))
            return {
                'sessions': busy,
                'total': sum(busy.values()),
                'workers_active': workers,
                'executor_down': executor_down,
            }

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for state in self._states.values():
                state.pending.clear()
                if state.future is not None and state.future.cancel():
                    state.worker_active = False
                    state.future = None
        try:
            self._executor.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - 关闭失败不该影响插件卸载
            pass
