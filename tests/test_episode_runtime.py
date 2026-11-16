# -*- encoding: utf-8 -*-
"""共同经历运行时证据与剧情代际回归（纯本地假模型/发送器）。"""

from __future__ import annotations

import tempfile
import threading
import types
import unittest
from collections import Counter, deque
from dataclasses import replace
from pathlib import Path

from newGeYi.context.live_context import LiveContextStore
from newGeYi.memory.spaces import current_space, new_story, pause_story, resume_story
from newGeYi.models import IncomingMessage, ModelResult, SessionKey
from newGeYi.runtime.app import Runtime
from newGeYi.storage.database import SQLiteStore


class EpisodeRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='geyi_episode_runtime_')
        self.addCleanup(self.scratch.cleanup)
        self.store = SQLiteStore(str(Path(self.scratch.name) / 'test.sqlite3'))
        self.addCleanup(self.store.close)
        self.rt = Runtime.__new__(Runtime)
        self.rt.store = self.store
        self.rt.config = {'active_shadow': False}
        self.session = SessionKey('testbot', 'qq', 'onebot', 'group', '', '100')
        self.sent = []
        self.calls = []
        self.buffers = []
        self.voices = []
        self.rt._self_sent_lock = threading.Lock()
        self.rt._recent_self_sent = deque(maxlen=40)
        self.rt._inflight_self_sent = Counter()
        self.rt._arrival_lock = threading.Lock()
        self.rt._arrival_seq = 0
        self.rt.live_context = LiveContextStore()
        self.rt.prompt_builder = types.SimpleNamespace(
            config={}, mark_talked=lambda key: None, build=lambda *a, **k: [],
        )
        self.rt.renderer = types.SimpleNamespace(
            config={}, render=lambda text, msg: types.SimpleNamespace(
                text=text, rest_reason='', voice_payload=None,
            ),
        )
        self.rt.attention = types.SimpleNamespace(
            note_spoken=lambda key: None, note_active_sent=lambda key: None,
            record_model=lambda *a, **k: 1,
        )
        self.rt.group_profile = types.SimpleNamespace(
            allow=lambda key: (True, ''), refresh_async=lambda *a: None,
        )
        self.rt.images = types.SimpleNamespace(remember=lambda msg: None, remember_assistant=lambda *a: None)
        self.rt.voice_manager = types.SimpleNamespace(
            report_reply_telemetry=lambda: None, mark_turn_without_voice=lambda key: None,
            is_user_requesting_voice=lambda text: False,
            dispatch_voice=lambda **kwargs: self.voices.append(kwargs),
        )
        self.rt.relationship = types.SimpleNamespace(active_for=lambda msg: False, after_reply=lambda *a, **k: None)
        self.rt.curator = types.SimpleNamespace(
            buffer_user=self.buffer_user, buffer_assistant=self.buffer_assistant,
        )
        self.rt.gateway = types.SimpleNamespace(chat=self.chat)
        self.rt.is_resting = lambda key: False
        self.rt.commands = types.SimpleNamespace(try_handle=lambda msg: False)
        self.rt.router = types.SimpleNamespace(route=lambda msg: None)
        self.rt._is_bot_sender = lambda *a: False
        self.rt._mentioned = lambda *a: True

    def buffer_user(self, msg, metadata=None):
        return self.store.buffer_append(msg.session.as_text(), 'user', msg.text,
                                        sender_id=msg.sender_id, sender_name=msg.sender_name,
                                        metadata=metadata)

    def buffer_assistant(self, key, text, metadata=None):
        self.buffers.append((key, text, metadata))
        return self.store.buffer_append(key, 'assistant', text, sender_name='歌伊', metadata=metadata)

    def chat(self, req, **kwargs):
        self.calls.append(kwargs)
        return ModelResult(ok=True, text='好，明天陪你再烤一炉。', model='fake')

    def message(self, text='我们一起烤面包', seq=1):
        return IncomingMessage(
            session=self.session, message_id=f'm{seq}', sender_id='alice', sender_name='相同昵称',
            text=text, raw_text=text, is_group=self.session.session_type == 'group', mentioned_bot=True,
            event=types.SimpleNamespace(send=lambda *a: self.sent.append(a)), proc=None, arrival_seq=seq,
        )

    def frozen(self, **kwargs):
        return self.rt._freeze_message_space(self.message(**kwargs))

    def test_accepted_text_has_frozen_space_and_reply_evidence(self):
        msg = self.frozen()
        record_id = self.buffer_user(msg, self.rt._user_memory_metadata(msg))
        msg = replace(msg, memory_record_id=record_id)
        self.rt.process_job(msg, 'passive')
        metadata = self.buffers[0][2]
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(metadata['space_id'], 'chat')
        self.assertEqual(metadata['recipient_id'], 'alice')
        self.assertEqual(metadata['reply_to'], record_id)
        self.assertEqual(metadata['delivery_level'], 'accepted')
        self.assertEqual(metadata['outer_role'], 'assistant')
        turns = self.store.recent_turns(self.session.as_text())
        self.assertEqual(turns[-1]['metadata'], metadata)

    def test_body_role_labels_do_not_change_outer_user_identity(self):
        routed = []
        self.rt.router.route = routed.append
        event = types.SimpleNamespace(
            data=types.SimpleNamespace(message='[AI] bob: 答应了', message_id='source', user_id='alice',
                                       group_id='100', sender={'name': '相同昵称'}),
            bot_info=types.SimpleNamespace(hash='testbot'), platform={'platform': 'qq', 'sdk': 'onebot'},
        )
        self.rt.handle_message(event, None)
        self.assertEqual(len(routed), 1)
        msg = routed[0]
        self.assertGreater(msg.memory_record_id, 0)
        row = self.store.buffer_fetch(msg.session.as_text(), 0)[0]
        self.assertEqual((row['role'], row['sender_id']), ('user', 'alice'))
        self.assertEqual(row['metadata']['outer_role'], 'user')
        self.assertEqual(row['metadata']['space_id'], msg.memory_space_id)
        self.assertEqual(row['metadata']['message_id'], 'source')

    def test_failed_send_produces_no_assistant_evidence_or_history(self):
        msg = replace(self.frozen(), event=types.SimpleNamespace(send=lambda *a: False))
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.buffers, [])
        self.assertEqual(self.store.recent_turns(self.session.as_text()), [])

    def test_shadow_produces_no_assistant_evidence(self):
        self.rt.config['active_shadow'] = True
        self.rt._active_cycle = lambda *a: (ModelResult(ok=True, text='答应你', model='fake'), '答应你', 0)
        self.rt.process_job(self.frozen(), 'active')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.buffers, [])

    def test_queued_message_is_dropped_before_model_after_space_switch(self):
        msg = self.frozen()
        new_story(self.store, self.session.as_text(), '第一章')
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.recent_turns(self.session.as_text()), [])

    def test_inflight_result_is_dropped_after_space_switch(self):
        msg = self.frozen()

        def chat(req, **kwargs):
            new_story(self.store, self.session.as_text(), '第一章')
            return ModelResult(ok=True, text='旧空间答复', model='fake')

        self.rt.gateway.chat = chat
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.buffers, [])

    def test_pause_resume_same_story_invalidates_old_generation(self):
        key = self.session.as_text()
        space = new_story(self.store, key, '同名章')
        msg = self.frozen()
        pause_story(self.store, key)
        resume_story(self.store, key)
        self.assertEqual(current_space(self.store, key), space)
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sent, [])

    def test_generation_change_during_send_prevents_evidence_resurrection(self):
        msg = self.frozen()

        def send(*args):
            self.store.set_setting('scene.generation:' + self.session.as_text(), msg.space_generation + 1)

        self.rt.process_job(replace(msg, event=types.SimpleNamespace(send=send)), 'passive')
        self.assertEqual(self.buffers, [])
        self.assertEqual(self.store.recent_turns(self.session.as_text()), [])

    def test_accepted_evidence_and_history_roll_back_together_on_buffer_failure(self):
        msg = self.frozen()

        def fail(*args, **kwargs):
            raise RuntimeError('buffer fixture')

        self.rt.curator.buffer_assistant = fail
        with self.assertRaisesRegex(RuntimeError, 'buffer fixture'):
            self.rt.process_job(msg, 'passive')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.recent_turns(self.session.as_text()), [])
        self.assertEqual(self.rt.live_context.snapshot(self.session.as_text(), 10), [])

    def test_legacy_message_without_frozen_fields_uses_current_story(self):
        space = new_story(self.store, self.session.as_text(), '第一章')
        self.rt.process_job(self.message(), 'passive')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.buffers[0][2]['space_id'], space)

    def test_voice_callback_keeps_source_space_without_extra_curator_evidence(self):
        space = new_story(self.store, self.session.as_text(), '第一章')
        self.rt.renderer.render = lambda text, msg: types.SimpleNamespace(
            text='明天再来', rest_reason='', voice_payload=('happy', 'また明日'),
        )
        self.rt.process_job(self.frozen(), 'passive')
        self.assertEqual(len(self.buffers), 1)
        pause_story(self.store, self.session.as_text())
        self.voices[0]['on_success'](self.session.as_text(), 'happy', 'また明日')
        self.assertEqual(len(self.buffers), 1)
        turns = self.store.recent_turns(self.session.as_text())
        self.assertEqual(turns[-1]['metadata']['space_id'], space)
        self.assertTrue(turns[-1]['content'].startswith('[VOICE:'))


class EpisodeLiveContextTest(unittest.TestCase):
    def test_space_filter_preserves_chat_legacy_and_story_resume(self):
        live = LiveContextStore()
        live.append('session', 1, 'user', '旧日常')
        live.append('session', 2, 'user', '篇章一', space_id='story:first')
        live.append_assistant('session', 2, '篇章一回应', space_id='story:first')
        live.append('session', 3, 'user', '篇章二', space_id='story:second')
        self.assertEqual([t['content'] for t in live.snapshot('session', 4, space_id='chat')], ['旧日常'])
        story = live.snapshot('session', 4, space_id='story:first')
        self.assertEqual([t['content'] for t in story], ['篇章一', '篇章一回应'])
        self.assertTrue(all(t['metadata']['space_id'] == 'story:first' for t in story))
        self.assertEqual(len(live.snapshot('session', 4)), 4)
        self.assertEqual(live.snapshot('session', 2, space_id='story:first'), [])


if __name__ == '__main__':
    unittest.main()
