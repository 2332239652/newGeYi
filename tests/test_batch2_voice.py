"""F20 bounded admission and lifecycle tests; no HTTP/TTS, no sleeps."""
from __future__ import annotations
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import Mock, patch
from newGeYi.voice.manager import VoiceManager
from newGeYi.voice.presets import normalize_emotion


class HeldExecutor:
    def __init__(self):
        self.jobs = []
    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((fn, args, future))
        return future
    def run(self):
        fn, args, future = self.jobs.pop(0)
        try:
            future.set_result(fn(*args))
        except BaseException as ex:
            future.set_exception(ex)
            raise
    def shutdown(self, wait=False):
        pass


class VoiceQueueTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {'voice_max_pending': 2, 'voice_max_pending_per_session': 1,
                    'voice_queue_ttl': 30, 'voice_group_cooldown': 45}
        self.manager = VoiceManager(self.cfg)
        self.manager._executor.shutdown(wait=True)
        self.pool = HeldExecutor()
        self.manager._executor = self.pool
        self.now = 100.0
        self.manager._clock = lambda: self.now
        self.client = Mock()
        self.client.health_cached.return_value = (True, 'ok', 0)
        self.client.synthesize.return_value = ('', {'base64': 'YQ=='})
        self.manager.client = self.client
        self.manager.send_voice_message = Mock(return_value=True)
        self.success = Mock()
    def tearDown(self):
        self.manager.shutdown()
    def dispatch(self, key='one', target='100', active=False):
        return self.manager.dispatch_voice(None, 'group', target, key, 'happy', 'よし',
                                           active, on_success=self.success)
    def test_same_session_atomic_and_global_bound_even_active(self):
        barrier = threading.Barrier(8)
        def attempt(_):
            barrier.wait(5)
            return self.dispatch(active=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            admitted = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(admitted), 1)
        self.assertTrue(self.dispatch('two', active=True))
        self.assertFalse(self.dispatch('three', active=True))
        self.assertFalse(self.manager._submit_aux(Mock()))
        self.assertEqual(len(self.manager._pending), 2)
    def test_expired_queue_releases_without_tts_then_can_retry(self):
        self.assertTrue(self.dispatch())
        self.now += 31
        self.pool.run()
        self.client.synthesize.assert_not_called()
        self.success.assert_not_called()
        self.assertFalse(self.manager._pending)
        self.assertTrue(self.dispatch())
    def test_failed_tts_and_send_release_without_cooldown_or_fact(self):
        for result in (('', {'error': 'bad'}), ('', {'base64': 'YQ=='})):
            self.client.synthesize.return_value = result
            self.manager.send_voice_message.return_value = False
            self.assertTrue(self.dispatch())
            self.pool.run()
            self.assertFalse(self.manager._pending)
            self.assertEqual(self.manager._group_last_voice, {})
            self.success.assert_not_called()
        self.manager.send_voice_message.return_value = True
        self.assertTrue(self.dispatch())
        self.pool.run()
        self.success.assert_called_once_with('one', normalize_emotion('happy'), 'よし')
    def test_worker_rechecks_cooldown_and_active_keeps_product_exemption(self):
        self.assertTrue(self.dispatch())
        self.manager.mark_voice_sent('other', '100')
        self.pool.run()
        self.client.synthesize.assert_not_called()
        self.assertTrue(self.dispatch(active=True))
        self.pool.run()
        self.success.assert_called_once()
    def test_health_rechecked_before_execution(self):
        self.assertTrue(self.dispatch())
        self.client.health_cached.return_value = (False, 'offline', 0)
        self.pool.run()
        self.client.synthesize.assert_not_called()
        self.assertFalse(self.manager._pending)
    def test_post_synthesis_ttl_recheck(self):
        def synthesize(*args, **kwargs):
            self.now += 31
            return '', {'base64': 'YQ=='}
        self.client.synthesize.side_effect = synthesize
        self.assertTrue(self.dispatch())
        self.pool.run()
        self.manager.send_voice_message.assert_not_called()
        self.assertFalse(self.manager._pending)
    def test_unrelated_reload_preserves_and_voice_reload_discards(self):
        self.assertTrue(self.dispatch())
        self.manager.reload(dict(self.cfg, memory_enabled=False))
        self.pool.run()
        self.success.assert_called_once()
        self.now += 50
        self.manager.mark_turn_without_voice('one')
        self.assertTrue(self.dispatch())
        self.manager.reload(dict(self.cfg, voice_enabled=False))
        self.pool.run()
        self.assertEqual(self.success.call_count, 1)
        self.assertFalse(self.manager._pending)
    def test_reload_during_synthesis_drops_old_client_audio(self):
        entered, release = threading.Event(), threading.Event()
        def synthesize(*a, **k):
            entered.set()
            self.assertTrue(release.wait(5))
            return '', {'base64': 'YQ=='}
        self.client.synthesize.side_effect = synthesize
        self.assertTrue(self.dispatch())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.pool.run)
            self.assertTrue(entered.wait(5))
            try:
                self.manager.reload(dict(self.cfg, voice_gateway_url='http://fixture/new'))
                self.assertEqual(len(self.manager._pending), 1)
            finally:
                release.set()
            future.result(5)
        self.manager.send_voice_message.assert_not_called()
        self.assertFalse(self.manager._pending)
    def test_cooldown_rechecked_after_synthesis(self):
        def synthesize(*a, **k):
            self.manager.mark_voice_sent('other', '100')
            return '', {'base64': 'YQ=='}
        self.client.synthesize.side_effect = synthesize
        self.assertTrue(self.dispatch())
        self.pool.run()
        self.manager.send_voice_message.assert_not_called()
        self.success.assert_not_called()
        self.assertFalse(self.manager._pending)
    def test_http_explicit_error_overrides_ok_and_response_closes(self):
        responses = [({'retcode': 0, 'status': 'ok'}, True),
                     ({'retcode': 100, 'status': 'ok'}, False),
                     ({'retcode': 0, 'status': 'failed'}, False),
                     ({'status': 'ok'}, True), ({'status': 'failed'}, False),
                     ({'retcode': 0, 'active': False}, False)]
        for data, expected in responses:
            with self.subTest(data=data):
                response = Mock(status_code=200)
                response.json.return_value = data
                with patch('requests.post', return_value=response):
                    accepted = VoiceManager.send_voice_message(
                        self.manager, 'group', '100', 'YQ==', send_url='http://fixture/send')
                self.assertEqual(accepted, expected)
                response.close.assert_called_once()
        response = Mock(status_code=503)
        event = Mock()
        event.send.return_value = None
        with patch('requests.post', return_value=response):
            self.assertTrue(VoiceManager.send_voice_message(
                self.manager, 'group', '100', 'YQ==', event, send_url='http://fixture/send'))
        response.close.assert_called_once()
        event.send.assert_called_once()
    def test_closed_executor_submit_failure_releases(self):
        self.pool.submit = Mock(side_effect=RuntimeError('closed'))
        self.assertFalse(self.dispatch(active=True))
        self.assertFalse(self.manager._pending)
    def test_shutdown_discards_queued(self):
        self.assertTrue(self.dispatch())
        self.manager.shutdown()
        self.pool.run()
        self.client.synthesize.assert_not_called()
        self.assertFalse(self.dispatch(active=True))
    def test_adapter_false_and_error_dict_not_sent(self):
        real = VoiceManager.send_voice_message
        for response in (False, {'retcode': 1}, {'status': 'failed'}, {'active': False}):
            event = Mock()
            event.send.return_value = response
            self.assertFalse(real(self.manager, 'group', '100', 'YQ==', event, send_url=''))
        event.send.return_value = None
        self.assertTrue(real(self.manager, 'group', '100', 'YQ==', event, send_url=''))

if __name__ == '__main__':
    unittest.main()
