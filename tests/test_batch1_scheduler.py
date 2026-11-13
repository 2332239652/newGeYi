"""真实并发交错验证会话单执行者及关闭语义，不调用网络。"""
import threading
import time
import unittest
from unittest.mock import patch

from .test_scheduler import FakeRuntime, SESSION, make_msg, wait_for
from newGeYi.runtime.scheduler import SessionScheduler, STALE_DRAIN_SECONDS


class SchedulerLeaseTest(unittest.TestCase):
    def test_long_running_drain_is_not_replaced(self):
        release = threading.Event()
        rt = FakeRuntime({'incomplete_hold_seconds': 0}, block=release)
        sch = SessionScheduler(rt, max_workers=2)
        try:
            self.assertTrue(sch.submit(make_msg('first'), 'passive'))
            self.assertTrue(rt.entered.wait(2))
            state = sch._states[SESSION.as_text()]
            with sch._lock:
                original = state.future
                state.last_progress_at -= STALE_DRAIN_SECONDS + 1
            self.assertTrue(sch.submit(make_msg('second'), 'passive'))
            self.assertIs(state.future, original)
            self.assertEqual(state.generation, 1)
            self.assertEqual(len(state.pending), 1)
            release.set()
            self.assertTrue(wait_for(lambda: len(rt.processed) == 2))
            self.assertEqual([x[0] for x in rt.processed], ['first', 'second'])
        finally:
            release.set()
            sch.shutdown()

    def test_executor_waiting_future_is_not_replaced(self):
        blocker = threading.Event()
        rt = FakeRuntime({'incomplete_hold_seconds': 0})
        sch = SessionScheduler(rt, max_workers=1)
        sch._executor.submit(blocker.wait, 3)
        try:
            sch.submit(make_msg('first'), 'passive')
            state = sch._states[SESSION.as_text()]
            with sch._lock:
                original = state.future
                state.last_progress_at -= STALE_DRAIN_SECONDS + 1
            sch.submit(make_msg('second'), 'passive')
            self.assertFalse(original.running())
            self.assertIs(state.future, original)
            blocker.set()
            self.assertTrue(wait_for(lambda: len(rt.processed) == 2))
        finally:
            blocker.set()
            sch.shutdown()

    def test_drain_exception_releases_lease_and_recovers(self):
        rt = FakeRuntime({'incomplete_hold_seconds': 0})
        sch = SessionScheduler(rt)
        original = sch._wait_or_superseded
        failed = threading.Event()
        def once(*args):
            if not failed.is_set():
                failed.set()
                raise RuntimeError('controlled drain failure')
            return original(*args)
        try:
            with patch.object(sch, '_wait_or_superseded', side_effect=once):
                sch.submit(make_msg('first'), 'passive')
                self.assertTrue(wait_for(lambda: len(rt.processed) == 1))
            self.assertEqual(rt.processed, [('first', 'passive')])
            self.assertTrue(wait_for(lambda: not sch.pending_snapshot()['workers_active']))
        finally:
            sch.shutdown()

    def test_full_backlog_recovers_after_restart_submit_failure(self):
        entered = threading.Event()
        fail_now = threading.Event()
        rt = FakeRuntime({'incomplete_hold_seconds': 0, 'max_pending_per_session': 2})
        sch = SessionScheduler(rt)
        original_wait = sch._wait_or_superseded

        def fail_once(*args):
            if not entered.is_set():
                entered.set()
                if not fail_now.wait(2):
                    raise AssertionError('test did not release the controlled drain')
                raise RuntimeError('controlled drain failure')
            return original_wait(*args)

        try:
            with patch.object(sch, '_wait_or_superseded', side_effect=fail_once):
                self.assertTrue(sch.submit(make_msg('first'), 'passive'))
                self.assertTrue(entered.wait(2))
                self.assertTrue(sch.submit(make_msg('second'), 'passive'))
                state = sch._states[SESSION.as_text()]
                with sch._lock:
                    first_future = state.future
                    self.assertEqual(len(state.pending), 2)
                # 已接受的两条仍在队列中；异常退出的第一次重启提交恰好失败。
                with patch.object(sch._executor, 'submit', side_effect=RuntimeError('controlled submit failure')):
                    fail_now.set()
                    with self.assertRaisesRegex(RuntimeError, 'controlled drain failure'):
                        first_future.result(timeout=2)
                with sch._lock:
                    self.assertFalse(state.worker_active)
                    self.assertIsNone(state.future)
                    self.assertEqual([job[0].text for job in state.pending], ['first', 'second'])
                # 满额的新消息仍被丢弃，但必须唤醒先前已接受的积压。
                self.assertFalse(sch.submit(make_msg('rejected'), 'passive'))
                self.assertTrue(wait_for(lambda: len(rt.processed) == 2))
                self.assertTrue(wait_for(lambda: not sch.pending_snapshot()['workers_active']))
                self.assertEqual(rt.processed, [('first', 'passive'), ('second', 'passive')])
        finally:
            fail_now.set()
            sch.shutdown()

    def test_initial_submit_failure_rejects_only_new_job_and_next_submit_recovers(self):
        rt = FakeRuntime({'incomplete_hold_seconds': 0})
        sch = SessionScheduler(rt)
        try:
            with patch.object(sch._executor, 'submit', side_effect=RuntimeError('controlled submit failure')):
                self.assertFalse(sch.submit(make_msg('rejected'), 'passive'))
            state = sch._states[SESSION.as_text()]
            with sch._lock:
                self.assertFalse(state.worker_active)
                self.assertIsNone(state.future)
                self.assertEqual(len(state.pending), 0)
            self.assertTrue(sch.submit(make_msg('accepted'), 'passive'))
            self.assertTrue(wait_for(lambda: len(rt.processed) == 1))
            self.assertEqual(rt.processed, [('accepted', 'passive')])
        finally:
            sch.shutdown()

    def test_shutdown_does_not_restart_executor_or_run_pending(self):
        release = threading.Event()
        rt = FakeRuntime({'incomplete_hold_seconds': 0}, block=release)
        sch = SessionScheduler(rt)
        try:
            sch.submit(make_msg('first'), 'passive')
            self.assertTrue(rt.entered.wait(2))
            sch.submit(make_msg('second'), 'passive')
            executor = sch._executor
            sch.shutdown()
            self.assertFalse(sch.submit(make_msg('third'), 'passive'))
            self.assertIs(executor, sch._executor)
            release.set()
            self.assertTrue(wait_for(lambda: len(rt.processed) == 1))
            self.assertTrue(wait_for(lambda: not sch.pending_snapshot()['workers_active']))
            self.assertEqual(rt.processed, [('first', 'passive')])
        finally:
            release.set()
            sch.shutdown()

    def test_many_simultaneous_arrivals_keep_fifo_single_worker(self):
        release = threading.Event()
        rt = FakeRuntime({'incomplete_hold_seconds': 0, 'max_pending_per_session': 100}, block=release)
        sch = SessionScheduler(rt, max_workers=6)
        threads = []
        try:
            sch.submit(make_msg('first'), 'passive')
            self.assertTrue(rt.entered.wait(2))
            for i in range(30):
                th = threading.Thread(target=sch.submit, args=(make_msg(str(i)), 'passive'))
                th.start()
                threads.append(th)
            for th in threads:
                th.join(2)
            state = sch._states[SESSION.as_text()]
            self.assertEqual(state.generation, 1)
            expected = ['first'] + [job[0].text for job in state.pending]
            release.set()
            self.assertTrue(wait_for(lambda: len(rt.processed) == 31))
            self.assertEqual([x[0] for x in rt.processed], expected)
        finally:
            release.set()
            sch.shutdown()
