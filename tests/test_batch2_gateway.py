"""F16-F19 controlled offline regressions; no live provider calls."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

# Standalone runner bypasses the OlivOS host entrypoint, as the audit repro does.
ROOT = Path(__file__).resolve().parents[2]
if 'newGeYi' not in sys.modules:
    package = types.ModuleType('newGeYi')
    package.__path__ = [str(ROOT / 'newGeYi')]
    sys.modules['newGeYi'] = package
_TEMP = tempfile.TemporaryDirectory(prefix='geyi_batch2_gateway_')
os.environ['GEYI_DATA_DIR'] = _TEMP.name

import requests
from newGeYi.models import ModelRequest
from newGeYi.providers import llm_gateway as mod
from newGeYi.tests.test_gateway_failover import build, FakeResp

REQ = ModelRequest(messages=[{'role': 'user', 'content': 'hi'}], model='x')


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class InlineThread:
    """Run fake I/O synchronously so virtual time does not depend on scheduling."""
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class GoodResp(FakeResp):
    def __init__(self):
        super().__init__(200)

    def json(self):
        return {'choices': [{'message': {'content': 'ok'}}]}


class GatewayBatch2Test(unittest.TestCase):
    def snapshot(self, gw, store):
        return (gw._current_name, gw._anchor_name, gw._hold_name,
                gw._pending_hold_at, copy.deepcopy(store.settings),
                [(p.name, st.healthy, st.fail_count, st.last_error,
                  st.last_ok_at, st.last_probe_at, st.probe_ok_streak,
                  st.cooldown_until) for p, st in gw._pairs])

    def test_budget_request_timeout_and_retry_share_deadline(self):
        gw, store, _, notes = build(total_timeout_seconds=1)
        cfg = copy.deepcopy(gw.config)
        cfg['max_retries'] = 3
        gw.reload(cfg)
        clock = Clock()
        timeouts = []

        class Session:
            def post(self, *args, timeout, **kwargs):
                timeouts.append(timeout)
                clock.now += sum(timeout)
                raise requests.Timeout('fixture')

        gw._get_session = lambda: Session()
        before = self.snapshot(gw, store)
        with patch.object(mod.time, 'monotonic', clock.monotonic), \
             patch.object(mod.time, 'sleep', clock.sleep), \
             patch.object(mod.threading, 'Thread', InlineThread):
            result = gw.chat(REQ)
        self.assertEqual(result.error_code, 'budget_exhausted')
        self.assertEqual(timeouts, [(0.5, 0.5)])
        self.assertEqual(clock.now, 1001)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(before, self.snapshot(gw, store))
        self.assertEqual(notes, [])

    def test_backoff_is_clamped_and_no_next_attempt_starts(self):
        gw, store, _, _ = build(total_timeout_seconds=0.3)
        cfg = copy.deepcopy(gw.config)
        cfg['max_retries'] = 2
        gw.reload(cfg)
        clock = Clock()
        calls = []

        class Session:
            def post(self, *args, timeout, **kwargs):
                calls.append(timeout)
                return FakeResp(500)

        gw._get_session = lambda: Session()
        with patch.object(mod.time, 'monotonic', clock.monotonic), \
             patch.object(mod.time, 'sleep', clock.sleep), \
             patch.object(mod.threading, 'Thread', InlineThread):
            result = gw.chat(REQ)
        self.assertEqual(result.error_code, 'budget_exhausted')
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(clock.sleeps[0], 0.3)
        self.assertTrue(all(st.healthy for _, st in gw._pairs))

    def test_semaphore_wait_consumes_budget_without_request(self):
        gw, store, session, _ = build(total_timeout_seconds=1)
        clock = Clock()
        waits = []

        class Busy:
            def acquire(self, timeout=None):
                waits.append(timeout)
                clock.now += timeout
                return False

        gw._semaphore = Busy()
        with patch.object(mod.time, 'monotonic', clock.monotonic):
            result = gw.chat(REQ)
        self.assertEqual(result.error_code, 'budget_exhausted')
        self.assertEqual(waits, [1])
        self.assertEqual(session.posts, [])
        self.assertEqual(gw._hold_name, '')

    def test_deadline_is_shared_across_channels_and_ignores_wall_clock(self):
        gw, store, _, _ = build(total_timeout_seconds=1)
        clock = Clock()
        timeouts = []

        class Session:
            def post(self, *args, timeout, **kwargs):
                timeouts.append(timeout)
                clock.now += 0.6 if len(timeouts) == 1 else 0.4
                return FakeResp(500)

        gw._get_session = lambda: Session()
        with patch.object(mod.time, 'monotonic', clock.monotonic), \
             patch.object(mod.time, 'time', lambda: -123456), \
             patch.object(mod.threading, 'Thread', InlineThread):
            result = gw.chat(REQ)
        self.assertEqual(result.error_code, 'budget_exhausted')
        self.assertEqual(len(timeouts), 2)
        self.assertAlmostEqual(sum(timeouts[1]), 0.4)
        self.assertEqual(gw._hold_name, '')
        self.assertTrue(gw._current_name != 'official-chat')

    def test_late_io_keeps_slot_and_never_commits(self):
        gw, store, _, notes = build(total_timeout_seconds=0.05)
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        gw._semaphore = threading.Semaphore(1)

        class Session:
            def post(self, *args, **kwargs):
                entered.set()
                try:
                    if not release.wait(2):
                        raise AssertionError('fixture release missing')
                    return GoodResp()
                finally:
                    exited.set()

        gw._get_session = lambda: Session()
        before = self.snapshot(gw, store)
        try:
            result = gw.chat(REQ)
            self.assertTrue(entered.is_set())
            self.assertEqual(result.error_code, 'budget_exhausted')
            self.assertFalse(gw._semaphore.acquire(blocking=False))
            self.assertEqual(before, self.snapshot(gw, store))
            self.assertEqual(notes, [])
        finally:
            release.set()
        self.assertTrue(exited.wait(1))
        self.assertTrue(gw._semaphore.acquire(timeout=1))
        gw._semaphore.release()
        self.assertEqual(before, self.snapshot(gw, store))

    def test_background_exemption_never_enters_global_failover(self):
        for error in (requests.Timeout, requests.ConnectionError):
            for wait in (0, 300):
                with self.subTest(error=error, wait=wait):
                    gw, store, _, notes = build(retry_before_hold_seconds=wait)

                    class Session:
                        def post(self, *args, **kwargs):
                            raise error('fixture')

                    gw._get_session = lambda: Session()
                    result = gw.chat(REQ, kind='memory_curate')
                    self.assertFalse(result.ok)
                    self.assertEqual(gw._current_name, 'p1')
                    self.assertEqual(gw._hold_name, '')
                    self.assertEqual(gw.pending_retry_at(), 0)
                    self.assertTrue(all(st.healthy and st.fail_count == 0 for _, st in gw._pairs))
                    self.assertEqual(notes, [])

    def test_background_auth_still_counts_and_other_kinds_keep_policy(self):
        gw, _, _, _ = build(status=401)
        gw.chat(REQ, kind='memory_curate')
        self.assertEqual(gw._hold_name, 'official-chat')
        for kind in ('affection', 'scene', 'gate'):
            with self.subTest(kind=kind):
                gw, _, _, _ = build()

                class Session:
                    def post(self, *args, **kwargs):
                        raise requests.Timeout('fixture')

                gw._get_session = lambda: Session()
                gw.chat(REQ, kind=kind)
                self.assertEqual(gw._hold_name, 'official-chat')

    def test_soft_degrade_roundtrip_preserves_reason_deadline_and_recovery(self):
        gw, store, _, notes = build(retry_before_hold_seconds=300)
        gw.switch_to('p2')
        with patch.object(mod.time, 'time', lambda: 2000):
            gw.chat(REQ)
        pending = json.loads(store.settings['gw.pending_retry'])
        self.assertEqual(pending['reason'], 'all_down_soft')
        self.assertEqual(pending['retry_at'], 2300)
        self.assertEqual(pending['anchor'], 'p2')
        rebuilt = mod.LLMGateway(store, gw.config)
        self.assertEqual(rebuilt.pending_retry_at(), 2300)
        self.assertEqual(rebuilt._pending_error, 'http_500')
        self.assertEqual(rebuilt._current_name, 'official-chat')
        self.assertEqual(rebuilt.probe_once(), [])
        rebuilt._probe_provider = lambda p: p.name == 'p2'
        self.assertEqual(rebuilt.retry_pending_once(), 'recovered')
        self.assertEqual(rebuilt._current_name, 'p2')
        self.assertEqual(json.loads(store.settings['gw.pending_retry']), {})
        self.assertEqual(notes, [])

    def test_hand_selected_manual_only_stays_selected_after_rebuild(self):
        gw, store, _, _ = build(retry_before_hold_seconds=300)
        gw.chat(REQ)
        gw.switch_to('official-chat')
        rebuilt = mod.LLMGateway(store, gw.config)
        self.assertEqual(rebuilt.pending_retry_at(), 0)
        self.assertEqual(rebuilt.probe_once(), [])
        self.assertEqual(rebuilt._current_name, 'official-chat')
        self.assertEqual(rebuilt._anchor_name, 'official-chat')

    def interleave_probe(self, method, change, outcome=True, repair=False):
        gw, store, _, _ = build(retry_before_hold_seconds=300,
                                probe_repair_per_round=1 if repair else 0)
        if method == 'retry_pending_once':
            gw._handle_all_down('timeout')
        else:
            gw._current_name = 'p1' if repair else 'p2'
            state = dict((p.name, st) for p, st in gw._pairs)['p2' if repair else 'p1']
            state.healthy = False
        entered, release = threading.Event(), threading.Event()
        results = []

        def probe(provider):
            entered.set()
            if not release.wait(2):
                raise AssertionError('fixture release missing')
            return outcome

        gw._probe_provider = probe
        thread = threading.Thread(target=lambda: results.append(getattr(gw, method)()))
        thread.start()
        self.assertTrue(entered.wait(1))
        if change == 'manual':
            gw.switch_to('official-chat')
        elif change == 'hold':
            gw._escalate_to_last_resort('concurrent_chat')
        elif change == 'soft':
            gw._handle_all_down('concurrent_chat')
        elif change == 'route':
            with gw._lock:
                gw._current_name = 'p3'
                gw._persist_locked()
        else:
            cfg = copy.deepcopy(gw.config)
            gw.invalidate_config()
            cfg['providers'][0]['model'] = 'new-model'
            gw.reload(cfg)
        after_change = self.snapshot(gw, store)
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(after_change, self.snapshot(gw, store))
        self.assertEqual(results, ['cancelled'] if method == 'retry_pending_once' else [[]])

    def test_probe_and_repair_cannot_override_manual_or_reload(self):
        for change in ('manual', 'config'):
            for repair in (False, True):
                for ok in (False, True):
                    with self.subTest(change=change, repair=repair, ok=ok):
                        self.interleave_probe('probe_once', change, ok, repair)

    def test_probe_and_repair_cannot_override_concurrent_hold_pending_or_route(self):
        for change in ('hold', 'soft', 'route'):
            for repair in (False, True):
                for ok in (False, True):
                    with self.subTest(change=change, repair=repair, ok=ok):
                        self.interleave_probe('probe_once', change, ok, repair)

    def test_notification_callback_does_not_hold_gateway_lock(self):
        gw, _, _, _ = build()
        acquired = []

        def callback(text):
            def contender():
                with gw._lock:
                    acquired.append(True)
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())

        gw._notify_cb = callback
        gw.chat(REQ)
        self.assertEqual(acquired, [True])

    def test_pending_probe_cannot_override_manual_or_reload(self):
        for change in ('manual', 'config'):
            for ok in (False, True):
                with self.subTest(change=change, ok=ok):
                    self.interleave_probe('retry_pending_once', change, ok)

    def test_chat_late_result_cannot_mutate_after_manual_or_config_change(self):
        for change in ('manual', 'config'):
            for status in (200, 500):
                with self.subTest(change=change, status=status):
                    gw, store, _, _ = build()
                    entered, release = threading.Event(), threading.Event()

                    class Session:
                        def post(self, *args, **kwargs):
                            entered.set()
                            if not release.wait(2):
                                raise AssertionError('fixture release missing')
                            return GoodResp() if status == 200 else FakeResp(status)

                    gw._get_session = lambda: Session()
                    thread = threading.Thread(target=lambda: gw.chat(REQ))
                    thread.start()
                    self.assertTrue(entered.wait(1))
                    if change == 'manual':
                        gw.switch_to('official-chat')
                    else:
                        cfg = copy.deepcopy(gw.config)
                        gw.invalidate_config()
                        cfg['providers'][0]['model'] = 'new-model'
                        gw.reload(cfg)
                    after_change = self.snapshot(gw, store)
                    calls_after_change = copy.deepcopy(store.api_calls)
                    release.set()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(after_change, self.snapshot(gw, store))
                    self.assertEqual(store.api_calls, calls_after_change)

    def test_pending_state_write_is_atomic_on_real_sqlite_failure(self):
        from newGeYi.storage.database import SQLiteStore
        with tempfile.TemporaryDirectory(prefix='gateway_sqlite_') as folder:
            store = SQLiteStore(str(Path(folder) / 'state.db'))
            gw, _, _, _ = build(retry_before_hold_seconds=300)
            gw.store = store
            try:
                with gw._lock:
                    gw._persist_locked()
                before = dict(store._conn().execute('SELECT key,value FROM settings').fetchall())
                original = store.set_setting

                def fail_on_pending(key, value):
                    if key == 'gw.pending_retry':
                        raise OSError('fixture interrupted multi-setting write')
                    original(key, value)

                store.set_setting = fail_on_pending
                with patch.object(mod.logger, 'exception') as failure_log:
                    gw._handle_all_down('timeout')
                failure_log.assert_called_once()
                after = dict(store._conn().execute('SELECT key,value FROM settings').fetchall())
                self.assertEqual(before, after)
                store.set_setting = original
                with gw._lock:
                    gw._persist_locked()
                rebuilt = mod.LLMGateway(store, gw.config)
                self.assertEqual(rebuilt.pending_retry_at(), gw.pending_retry_at())
                self.assertEqual(rebuilt._current_name, 'official-chat')
            finally:
                store.close()

    def test_config_is_snapshot_and_same_dictionary_hook_invalidates(self):
        gw, store, _, _ = build()
        cfg = copy.deepcopy(gw.config)
        gw.reload(cfg)
        with gw._lock:
            token = gw._operation_token_locked()
        gw.invalidate_config()
        cfg['providers'][0]['extra_body'] = {'new': ['value']}
        self.assertNotIn('extra_body', gw.config['providers'][0])
        gw.reload(cfg)
        cfg['providers'][0]['extra_body']['new'].append('mutated')
        self.assertEqual(gw.config['providers'][0]['extra_body'], {'new': ['value']})
        with gw._lock:
            self.assertFalse(gw._operation_valid_locked(token))


if __name__ == '__main__':
    unittest.main(verbosity=2)
