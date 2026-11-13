"""Independent F16-F19 review regressions. Offline, event controlled I/O only."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
if 'newGeYi' not in sys.modules:
    package = types.ModuleType('newGeYi')
    package.__path__ = [str(ROOT / 'newGeYi')]
    sys.modules['newGeYi'] = package
os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_gateway_review_'))

from newGeYi.providers.llm_gateway import LLMGateway
from newGeYi.models import ModelRequest
from newGeYi.tests.test_gateway_failover import build


class IndependentGatewayReview(unittest.TestCase):
    def test_inflight_probe_must_not_override_new_all_down_hold_or_soft_state(self):
        for wait in (0, 300):
            with self.subTest(wait=wait):
                gw, store, _, notes = build(retry_before_hold_seconds=wait)
                gw._current_name = 'p2'
                dict((p.name, st) for p, st in gw._pairs)['p1'].healthy = False
                entered, release = threading.Event(), threading.Event()
                result = []

                def probe(provider):
                    entered.set()
                    if not release.wait(1):
                        raise AssertionError('fixture release missing')
                    return True

                gw._probe_provider = probe
                thread = threading.Thread(target=lambda: result.append(gw.probe_once()))
                thread.start()
                self.assertTrue(entered.wait(1))
                try:
                    gw._handle_all_down('http_500')
                    before = (gw._current_name, gw._hold_name, gw._pending_hold_at,
                              copy.deepcopy(store.settings))
                finally:
                    release.set()
                    thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual((gw._current_name, gw._hold_name, gw._pending_hold_at,
                                  store.settings), before)
                self.assertEqual(result, [[]])

    def test_route_persist_sql_error_rolls_back_and_later_success_rebuilds(self):
        from newGeYi.storage.database import SQLiteStore
        template, _, _, _ = build(retry_before_hold_seconds=300)
        with tempfile.TemporaryDirectory(prefix='geyi_gw_review_sql_') as scratch:
            store = SQLiteStore(str(Path(scratch) / 'gateway.sqlite'))
            gw = LLMGateway(store, template.config)
            try:
                gw.switch_to('p2')
                conn = store._conn()
                before = list(conn.execute('SELECT key,value FROM settings ORDER BY key'))
                conn.execute("""CREATE TEMP TRIGGER fail_pending BEFORE UPDATE ON settings
                    WHEN NEW.key='gw.pending_retry'
                    BEGIN SELECT RAISE(ABORT, 'review pending failure'); END""")
                with self.assertLogs('newGeYi.gateway', level='ERROR') as captured:
                    gw._handle_all_down('http_500')
                self.assertIn('渠道状态持久化失败', '\n'.join(captured.output))
                self.assertFalse(conn.in_transaction)
                self.assertEqual(list(conn.execute('SELECT key,value FROM settings ORDER BY key')), before)
                # In-memory degraded operation remains available despite failed persistence.
                self.assertEqual(gw._current_name, 'official-chat')
                self.assertGreater(gw.pending_retry_at(), 0)
                rebuilt = LLMGateway(store, template.config)
                self.assertEqual(rebuilt._current_name, 'p2')
                self.assertEqual(rebuilt.pending_retry_at(), 0)
                conn.execute('DROP TRIGGER fail_pending')
                with gw._lock:
                    gw._persist_locked()
                rebuilt = LLMGateway(store, template.config)
                self.assertEqual(rebuilt._current_name, 'official-chat')
                self.assertEqual(rebuilt._anchor_name, 'p2')
                self.assertEqual(rebuilt.pending_retry_at(), gw.pending_retry_at())
                self.assertEqual(rebuilt._pending_error, 'http_500')
            finally:
                store._conn().close()

    def test_deadline_return_does_not_wait_for_metrics_write(self):
        gw, store, _, _ = build(total_timeout_seconds=0.02)
        io_release = threading.Event()
        metric_entered, metric_release, returned = (threading.Event() for _ in range(3))
        results = []

        class Session:
            def post(self, *args, **kwargs):
                if not io_release.wait(1):
                    raise AssertionError('fixture release missing')
                raise RuntimeError('late I/O fixture')

        def record(**kwargs):
            metric_entered.set()
            if not metric_release.wait(1):
                raise AssertionError('fixture metric release missing')

        def caller():
            try:
                results.append(gw.chat(ModelRequest(messages=[], model='x')))
            finally:
                returned.set()

        gw._get_session = lambda: Session()
        store.add_api_call = record
        thread = threading.Thread(target=caller)
        thread.start()
        try:
            self.assertTrue(returned.wait(0.2),
                            'expired deadline still waits synchronously for SQLite metrics')
            self.assertFalse(metric_entered.is_set(),
                             'budget-only outcome must avoid synchronous SQLite writes')
        finally:
            metric_release.set()
            io_release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].error_code, 'budget_exhausted')

    def test_deadline_worker_cap_across_reloads_and_late_failures(self):
        gw, store, _, notes = build(total_timeout_seconds=0.02)
        gw._semaphore = threading.Semaphore(1)
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        calls = []

        class Session:
            def post(self, *args, **kwargs):
                calls.append(1)
                entered.set()
                try:
                    if not release.wait(1):
                        raise AssertionError('fixture release missing')
                    raise RuntimeError('late I/O failure')
                finally:
                    exited.set()

        gw._get_session = lambda: Session()
        req = ModelRequest(messages=[{'role': 'user', 'content': 'hi'}], model='x')
        started = time.monotonic()
        try:
            self.assertEqual(gw.chat(req).error_code, 'budget_exhausted')
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(entered.is_set())
            semaphore = gw._semaphore
            cfg = copy.deepcopy(gw.config)
            cfg['global_concurrency'] = 8
            gw.reload(cfg)
            self.assertIs(gw._semaphore, semaphore)
            snapshot = (gw._current_name, gw._hold_name, copy.deepcopy(store.settings))
            self.assertEqual(gw.chat(req).error_code, 'budget_exhausted')
            self.assertEqual(calls, [1])
        finally:
            release.set()
        self.assertTrue(exited.wait(1))
        self.assertTrue(gw._semaphore.acquire(timeout=1))
        gw._semaphore.release()
        self.assertEqual((gw._current_name, gw._hold_name, store.settings), snapshot)
        self.assertEqual(notes, [])


if __name__ == '__main__':
    unittest.main()
