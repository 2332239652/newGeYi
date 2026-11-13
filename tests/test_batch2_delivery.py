"""F11 delivery acceptance, side-effect boundary and deferred VOICE facts."""
from __future__ import annotations
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_delivery_import_'))
from newGeYi.models import IncomingMessage, ModelResult, SessionKey
from newGeYi.runtime.app import Runtime


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_delivery_')
        self.env = patch.dict(os.environ, GEYI_DATA_DIR=self.tmp.name)
        self.env.start()
        self.rt = Runtime()
        self.rt.config['active_shadow'] = False
        self.rt.voice_manager.report_reply_telemetry = Mock()
        self.rt.voice_manager.dispatch_voice = Mock(return_value=True)
        self.rt.relationship.after_reply = Mock()
        self.rt._retry_if_overstep = Mock(return_value=None)
        self.rt.prompt_builder.mark_talked = Mock()
        self.rt.attention.note_spoken = Mock()
        self.rt.attention.note_active_sent = Mock()
        self.rt.images.remember_assistant = Mock()
        self.rt.live_context.append_assistant = Mock()
        self.rt.curator.buffer_assistant = Mock()
        self.rt.group_profile.allow = Mock(return_value=(True, 'ok'))
        self.rendered = types.SimpleNamespace(text='好的呀', rest_reason='', voice_payload=None,
                                               emote='', mention_current_user=False)
        self.rt.renderer.render = Mock(return_value=self.rendered)
        self.result = ModelResult(ok=True, text='好的呀', model='fixture', input_tokens=123,
                                  output_tokens=7, latency_ms=2)
        def generate(msg, kind, level, cfg):
            # Match the real gateway's independent accounting before delivery.
            self.rt.store.add_api_call(msg.session.as_text(), kind, self.result.model,
                                       self.result.input_tokens, self.result.output_tokens, 2, True)
            return self.result, self.result.text
        self.rt._ask = Mock(side_effect=generate)
    def tearDown(self):
        self.rt.save(None)
        self.rt.store.close()
        self.env.stop()
        self.tmp.cleanup()
    def message(self, group=False, response=None, error=None, target='10001'):
        event = Mock()
        event.send.return_value = response
        event.send.side_effect = error
        session = SessionKey('h', 'qq', 'onebot', 'group' if group else 'private', '-', target)
        return IncomingMessage(session=session, message_id='1', sender_id=target, sender_name='测试',
                               text='这是一句测试', raw_text='', is_group=group, mentioned_bot=True,
                               event=event, proc=None, arrival_seq=1, sender_is_bot=False)
    def test_none_return_means_accepted_not_platform_confirmation(self):
        msg = self.message()
        result = self.rt.reply(msg, '好的呀')
        self.assertTrue(result.accepted)
        self.assertFalse(result.confirmed)
        self.assertTrue(self.rt.is_recently_sent_by_self(msg.session.as_text(), '好的呀'))
    def test_explicit_rejection_or_exception_has_no_self_cache(self):
        for response, error in ((False, None), ({'retcode': 1}, None),
                                ({'status': 'failed'}, None), (None, RuntimeError('send fixture'))):
            msg = self.message(response=response, error=error)
            self.assertFalse(self.rt.reply(msg, 'failed').accepted)
            self.assertFalse(self.rt.is_recently_sent_by_self(msg.session.as_text(), 'failed'))
    def test_synchronous_outbound_observer_is_suppressed_without_sent_fact(self):
        from newGeYi.context.cross_plugin_observer import CrossPluginObserver
        observer = CrossPluginObserver(self.rt)
        self.rt.live_context.append = Mock()
        msg = self.message(group=True)
        def send(*args):
            self.assertFalse(self.rt._recent_self_sent)
            observer.on_other_plugin_sent(msg.session, '同步钩子', bot_id='123')
            return False
        msg.event.send.side_effect = send
        self.assertFalse(self.rt.reply(msg, '同步钩子').accepted)
        self.rt.live_context.append.assert_not_called()
        self.assertFalse(self.rt._inflight_self_sent)
        self.assertFalse(self.rt.is_recently_sent_by_self(msg.session.as_text(), '同步钩子'))
    def test_identical_concurrent_send_reservations_do_not_release_each_other(self):
        msg = self.message()
        entered, release = threading.Event(), threading.Event()
        def first_send(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return False
        msg.event.send.side_effect = first_send
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.rt.reply, msg, '相同文本')
            self.assertTrue(entered.wait(5))
            try:
                second = self.message(response=False)
                self.assertFalse(self.rt.reply(second, '相同文本').accepted)
                self.assertTrue(self.rt.is_recently_sent_by_self(msg.session.as_text(), '相同文本'))
                self.assertEqual(self.rt._inflight_self_sent[(msg.session.as_text(), '相同文本')], 1)
            finally:
                release.set()
            self.assertFalse(first.result(5).accepted)
        self.assertFalse(self.rt._inflight_self_sent)
        self.assertFalse(self.rt.is_recently_sent_by_self(msg.session.as_text(), '相同文本'))
    def test_text_failure_gates_assistant_and_all_sent_side_effects(self):
        for group in (False, True):
            msg = self.message(group, error=RuntimeError('send fixture'), target=str(10000 + group))
            self.rt.process_job(msg, 'passive')
            turns = self.rt.store.recent_turns(msg.session.as_text())
            self.assertEqual([t['role'] for t in turns], [] if group else ['user'])
        self.assertEqual(self.rt._ask.call_count, 2)
        usage = self.rt.store._conn().execute('SELECT SUM(in_tokens),SUM(out_tokens) FROM api_calls').fetchone()
        self.assertEqual(usage, (246, 14))  # generation cost survives failed delivery
        for hook in (self.rt.relationship.after_reply, self.rt.voice_manager.report_reply_telemetry,
                     self.rt.voice_manager.dispatch_voice, self.rt.images.remember_assistant,
                     self.rt.live_context.append_assistant, self.rt.attention.note_spoken,
                     self.rt.attention.note_active_sent, self.rt.prompt_builder.mark_talked,
                     self.rt.curator.buffer_assistant):
            hook.assert_not_called()
    def test_success_commits_once_and_carries_pre_user_snapshot(self):
        msg = self.message()
        self.rt.store.add_turn(msg.session.as_text(), 'user', '前一条', msg.sender_id)
        self.rt.store.add_turn(msg.session.as_text(), 'assistant', '前一答')
        self.rt.process_job(msg, 'passive')
        turns = self.rt.store.recent_turns(msg.session.as_text())
        self.assertEqual([t['content'] for t in turns], ['前一条', '前一答', msg.text, '好的呀'])
        kwargs = self.rt.relationship.after_reply.call_args.kwargs
        self.assertEqual([t['content'] for t in kwargs['history']], ['前一条', '前一答'])
        self.rt.voice_manager.report_reply_telemetry.assert_called_once()
        self.rt.attention.note_spoken.assert_called_once()
        row = self.rt.store._conn().execute('SELECT in_tokens,out_tokens FROM turns WHERE role="assistant" ORDER BY seq DESC LIMIT 1').fetchone()
        self.assertEqual(row, (123, 7))
    def test_voice_queue_is_not_fact_and_callback_freezes_turn(self):
        self.rendered.voice_payload = ('happy', 'よし')
        first = self.message(group=True)
        self.rt.process_job(first, 'passive')
        callback = self.rt.voice_manager.dispatch_voice.call_args.kwargs['on_success']
        self.assertEqual([t['content'] for t in self.rt.store.recent_turns(first.session.as_text())],
                         [first.text, '好的呀'])
        # Future message and render changes must not be captured by the old callback.
        self.rendered.voice_payload = None
        second = self.message(group=True, target='10002')
        self.rt.process_job(second, 'passive')
        callback(first.session.as_text(), 'happy', 'よし')
        first_turns = self.rt.store.recent_turns(first.session.as_text())
        self.assertEqual([t['content'] for t in first_turns], [first.text, '好的呀', '[VOICE:happy|よし]'])
        self.assertFalse(any('[VOICE:' in t['content'] for t in self.rt.store.recent_turns(second.session.as_text())))
    def test_failed_active_delivery_never_marked_sent(self):
        self.rt._active_cycle = Mock(return_value=(self.result, self.result.text, 0))
        self.rt.store.mark_active_decision_sent = Mock()
        self.rt.process_job(self.message(group=True, response=False), 'active')
        self.rt.attention.note_active_sent.assert_not_called()
        self.rt.store.mark_active_decision_sent.assert_not_called()
    def test_failed_sleep_notice_not_recorded(self):
        self.rt._track_self_sent = Mock()
        self.rt.send_sleep_notice(self.message(error=RuntimeError('send fixture')))
        self.rt._track_self_sent.assert_not_called()

if __name__ == '__main__':
    unittest.main()
