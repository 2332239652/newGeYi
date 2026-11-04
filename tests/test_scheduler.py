# -*- encoding: utf-8 -*-
"""调度器 / 日志桥的回归测试（离线、无网络、不碰线上状态）。

锁住 2026-11-01 那次「@她完全不理，日志一片干净」的故障教训：

1. 日志桥曾经把 WARNING 映射成 `Proc.log(1, …)` —— 实测本机 OlivOS **只有档位 2 会落进
   日志文件**，于是所有告警（额度满 / 模型报错 / 会话任务失败）全被静默吞掉。
2. 调度器有三条完全静默的丢消息路径：队列满、worker 标记卡死、drain 找不到会话状态。
3. `incomplete_tails: []` 被 `[] or DEFAULT` 吃掉，「按住 8 秒」这个开关关不掉。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import unittest

# ⚠️ 必须在 import newGeYi 之前：包 __init__ 会执行 main.py 里的 Runtime()
_SCRATCH = tempfile.mkdtemp(prefix='geyi_sched_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.runtime.scheduler import (  # noqa: E402
    DEFAULT_INCOMPLETE_TAILS,
    STALE_DRAIN_SECONDS,
    SessionScheduler,
    _SessionState,
    looks_incomplete,
)

SESSION = SessionKey(bot_hash='hash', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='10001')


def make_msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        session=SESSION, message_id='1', sender_id='2', sender_name='测试',
        text=text, raw_text=text, is_group=True, mentioned_bot=True,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


class FakeRuntime:
    """只实现调度器用得到的那点接口。"""

    def __init__(self, config=None, block=None):
        self.config = dict(config or {})
        self.processed: list[tuple[str, str]] = []
        self.block = block          # threading.Event：非空时 process_job 会一直等它
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def process_job(self, msg, kind):
        self.entered.set()
        if self.block is not None:
            self.block.wait(10)
        with self._lock:
            self.processed.append((msg.text, kind))


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class IncompleteTailTest(unittest.TestCase):
    def test_default_tails_still_work(self):
        self.assertTrue(looks_incomplete('对了'))
        self.assertTrue(looks_incomplete('我跟你讲。'))
        self.assertFalse(looks_incomplete('猫'))

    def test_empty_tails_means_off(self):
        """配置里写 [] 就是「关掉」——不能掉回默认词表。"""
        rt = FakeRuntime({'incomplete_hold_seconds': 8, 'incomplete_tails': []})
        sch = SessionScheduler(rt)
        self.assertEqual(sch._hold_seconds(make_msg('然后'), 'passive'), 0.0)
        self.assertEqual(sch._hold_seconds(make_msg('话说'), 'passive'), 0.0)

    def test_missing_tails_uses_defaults(self):
        rt = FakeRuntime({'incomplete_hold_seconds': 8})
        sch = SessionScheduler(rt)
        self.assertEqual(sch._hold_seconds(make_msg('然后'), 'passive'), 8.0)
        self.assertIn('然后', DEFAULT_INCOMPLETE_TAILS)

    def test_active_never_holds(self):
        rt = FakeRuntime({'incomplete_hold_seconds': 8})
        sch = SessionScheduler(rt)
        self.assertEqual(sch._hold_seconds(make_msg('然后'), 'active'), 0.0)


class QueueVisibilityTest(unittest.TestCase):
    def test_full_queue_warns_instead_of_silence(self):
        """队列满了要留一行告警：以前是 return False 一句话都没有。"""
        gate = threading.Event()
        rt = FakeRuntime({'max_pending_per_session': 2, 'incomplete_hold_seconds': 0}, block=gate)
        sch = SessionScheduler(rt)
        try:
            self.assertTrue(sch.submit(make_msg('第一条'), 'passive'))
            self.assertTrue(rt.entered.wait(5))
            self.assertTrue(sch.submit(make_msg('第二条'), 'passive'))
            self.assertTrue(sch.submit(make_msg('第三条'), 'passive'))
            with self.assertLogs('newGeYi.scheduler', level='WARNING') as captured:
                self.assertFalse(sch.submit(make_msg('第四条'), 'passive'))
            joined = '\n'.join(captured.output)
            self.assertIn('队列已满', joined)
            self.assertIn('第四条', joined)
        finally:
            gate.set()
            sch.shutdown()

    def test_drain_without_state_warns(self):
        sch = SessionScheduler(FakeRuntime({'incomplete_hold_seconds': 0}))
        try:
            with self.assertLogs('newGeYi.scheduler', level='WARNING') as captured:
                sch._drain('不存在的会话')
            self.assertIn('找不到会话状态', '\n'.join(captured.output))
        finally:
            sch.shutdown()

    def test_pending_snapshot(self):
        gate = threading.Event()
        rt = FakeRuntime({'max_pending_per_session': 6, 'incomplete_hold_seconds': 0}, block=gate)
        sch = SessionScheduler(rt)
        try:
            sch.submit(make_msg('一条'), 'passive')
            self.assertTrue(rt.entered.wait(5))
            sch.submit(make_msg('两条'), 'passive')
            snap = sch.pending_snapshot()
            self.assertEqual(snap['total'], 1)
            self.assertEqual(len(snap['sessions']), 1)
            self.assertFalse(snap['executor_down'])
        finally:
            gate.set()
            sch.shutdown()


class WedgedQueueTest(unittest.TestCase):
    """worker 标记挂着却没有 drain 在跑：必须自愈，不能永远沉默。"""

    def test_stale_worker_is_rebuilt(self):
        rt = FakeRuntime({'max_pending_per_session': 6, 'incomplete_hold_seconds': 0})
        sch = SessionScheduler(rt)
        key = SESSION.as_text()
        state = _SessionState()
        state.pending.append((make_msg('积压的一条'), 'passive'))
        state.worker_active = True
        state.last_progress_at = time.time() - (STALE_DRAIN_SECONDS + 30)
        sch._states[key] = state
        try:
            with self.assertLogs('newGeYi.scheduler', level='WARNING') as captured:
                self.assertTrue(sch.submit(make_msg('新的一条'), 'passive'))
            self.assertIn('疑似卡死', '\n'.join(captured.output))
            self.assertTrue(wait_for(lambda: len(rt.processed) == 2))
            self.assertEqual([text for text, _ in rt.processed], ['积压的一条', '新的一条'])
        finally:
            sch.shutdown()

    def test_supersede_is_logged(self):
        """被后一条取代（丢掉）也要留一行，否则看起来就像「她没收到这条」。"""
        rt = FakeRuntime({'incomplete_hold_seconds': 5, 'incomplete_tails': ['然后']})
        sch = SessionScheduler(rt)
        state = _SessionState()
        first = make_msg('然后')
        state.pending.append((first, 'passive'))

        def _arrive_later():
            time.sleep(0.3)
            with sch._lock:
                state.pending.append((make_msg('接着说'), 'passive'))

        threading.Thread(target=_arrive_later, daemon=True).start()
        with self.assertLogs('newGeYi.scheduler', level='INFO') as captured:
            self.assertTrue(sch._wait_or_superseded(state, first, 'passive'))
        self.assertIn('被后一条取代', '\n'.join(captured.output))
        sch.shutdown()


class LogBridgeTest(unittest.TestCase):
    """日志桥：所有级别都必须落到 `Proc.log(2, …)`，否则线上一条都看不见。"""

    def _handler(self):
        from newGeYi.runtime.app import _ProcLogHandler

        class Recorder:
            def __init__(self):
                self.rows = []

            def log(self, level, text):
                self.rows.append((level, text))

        rt = Recorder()
        return _ProcLogHandler(rt), rt

    def _record(self, level, msg='', args=(), exc_info=None, name='newGeYi.scheduler'):
        return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)

    def test_warning_goes_out_at_level_two(self):
        handler, rt = self._handler()
        handler.emit(self._record(logging.WARNING, '队列已满 %s', ('x',)))
        self.assertEqual(len(rt.rows), 1)
        level, text = rt.rows[0]
        self.assertEqual(level, 2, 'OlivOS 只有档位 2 会写进日志文件')
        self.assertIn('[WARN]', text)
        self.assertIn('队列已满 x', text)

    def test_info_and_error_tags(self):
        handler, rt = self._handler()
        handler.emit(self._record(logging.INFO, '正常进展'))
        handler.emit(self._record(logging.ERROR, '出错了'))
        self.assertEqual([row[0] for row in rt.rows], [2, 2])
        # INFO 保持老格式（老日志的 grep 还认它），告警才加标记
        self.assertNotIn('[INFO]', rt.rows[0][1])
        self.assertIn('[scheduler] 正常进展', rt.rows[0][1])
        self.assertIn('[ERROR]', rt.rows[1][1])

    def test_exception_traceback_is_kept(self):
        handler, rt = self._handler()
        try:
            raise ValueError('炸了')
        except ValueError:
            info = sys.exc_info()
        handler.emit(self._record(logging.ERROR, '会话任务失败', exc_info=info))
        level, text = rt.rows[0]
        self.assertEqual(level, 2)
        self.assertIn('ValueError', text)
        self.assertIn('炸了', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
