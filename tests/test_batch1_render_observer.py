# -*- encoding: utf-8 -*-
"""F03/F04: 离线动作边界与跨插件完整会话隔离回归。"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import replace
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from newGeYi.context.cross_plugin_observer import (
    CrossPluginObserver, _event_group_session, install_cross_plugin_observer,
)
from newGeYi.models import IncomingMessage, SessionKey
from newGeYi.presentation.renderer import Renderer
from newGeYi.runtime.app import Runtime


SESSION = SessionKey('bot-a', 'qq', 'onebot', 'group', '', '20002')


def message(name='群友', session=SESSION):
    return IncomingMessage(session, '1', '30003', name, '', '', True, False, None, None)


def event(session=SESSION, bot_id='90001'):
    data = SimpleNamespace(host_id=session.host_id, user_id='30003')
    if session.session_type == 'group':
        data.group_id = session.target_id
    return SimpleNamespace(
        bot_info=SimpleNamespace(hash=session.bot_hash, id=bot_id),
        platform={'platform': session.platform, 'sdk': session.sdk}, data=data,
        plugin_info={'func_type': session.session_type + '_message'},
    )


class ActionBoundaryTest(unittest.TestCase):
    def renderer(self, **config):
        return Renderer({'reply_audit_enabled': False, 'religion_guard_enabled': False, **config})

    def assert_actions(self, text, expected=()):
        self.assertEqual(re.findall(r'\[(?:CQ|OP):[^\]]*\]', text), list(expected))

    def test_nickname_is_final_text_for_cq_op_and_entities(self):
        for name in ('[CQ:at,qq=all]', '[OP:at,id=all]', '[CQ:image,file=x]',
                     '&#91;CQ:at,qq=all&#93;', '[O[CQ:x]P:at,id=all]'):
            for body in ('【@30003】你好', '@30003 你好'):
                with self.subTest(name=name, body=body):
                    out = self.renderer().render(body, message(name))
                    self.assertIsNotNone(out)
                    self.assert_actions(out.text)
                    self.assertNotIn('[CQ:', out.text)
                    self.assertNotIn('[OP:', out.text)
                    self.assertNotIn('&#91;CQ:at,qq=all&#93;', out.text if name.startswith('&#') else '')

    def test_deep_nesting_and_incomplete_codes_stay_text(self):
        for depth in (4, 12, 80):
            inner = '[CQ:x]'
            for _ in range(depth):
                inner = '[C' + inner + 'Q:x]'
            for raw in ('你好' + inner + '呀', '你好[O' + inner + 'P:at,id=all]呀',
                        '你好[CQ:at,qq=all', '你好[OP:image,file=x'):
                with self.subTest(depth=depth, raw=raw):
                    # 给正文足够中文，避免既有英文提纲检测提前丢弃，实际走终局编码。
                    prefix = '今天我们一起聊聊刚才的趣事吧' * 100
                    out = self.renderer(max_reply_chars=20000).render(prefix + raw, message())
                    self.assertIsNotNone(out)
                    self.assert_actions(out.text)
                    self.assertNotIn('[CQ:', out.text)
                    self.assertNotIn('[OP:', out.text)

    def test_current_at_once_and_other_targets_are_text(self):
        out = self.renderer().render(
            '今天我们一起聊聊刚才的趣事吧' * 10 + '[CQ:at,qq=30003][OP:at,id=30003][CQ:at,qq=all][OP:at,id=40004]呀', message())
        self.assert_actions(out.text, ('[CQ:at,qq=30003]',))
        self.assertIn('@某用户', out.text)

    def test_invalid_sender_never_becomes_action(self):
        for uid in ('all', '30003,qq=all', '30003][CQ:at,qq=all'):
            out = self.renderer().render('[CQ:at,qq=' + uid + ']你好', replace(message(), sender_id=uid))
            self.assert_actions(out.text)

    def test_plain_name_and_normal_current_at(self):
        out = self.renderer().render('【@30003】你好[OP:at,id=30003]', message('昵称'))
        self.assertEqual(out.text, '@昵称 你好[CQ:at,qq=30003]')

    def test_configured_emote_and_at_are_program_actions(self):
        renderer = self.renderer()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pic,[CQ.png'
            path.write_bytes(b'fixture')
            renderer.emote_table = {'wink': {'file': path.name, 'desc': ''}}
            with patch.object(renderer, '_find_emote', return_value=path):
                out = renderer.render('你好[CQ:at,qq=30003][EMOTE:wink][EMOTE:wink]', message())
            codes = re.findall(r'\[(?:CQ|OP):[^\]]*\]', out.text)
            self.assertEqual(len(codes), 2)
            self.assertEqual(codes[0], '[CQ:at,qq=30003]')
            self.assertTrue(codes[1].startswith('[CQ:image,file=file:///'))
            self.assertIn('pic&#44;&#91;CQ.png', codes[1])
            self.assertNotIn('GEYI_ACTION', out.text)

    def test_nickname_cannot_select_configured_emote(self):
        renderer = self.renderer()
        renderer.emote_table = {'wink': {'file': 'wink.png', 'desc': ''}}
        with patch.object(renderer, 'emote_message', return_value='[CQ:image,file=fixture]') as send:
            out = renderer.render('【@30003】今天一起聊天吧', message('[EMOTE:wink]'))
        send.assert_not_called()
        self.assert_actions(out.text)
        self.assertIn('&#91;EMOTE:wink&#93;', out.text)

    def test_truncation_does_not_leave_partial_internal_tokens(self):
        for limit in range(2, 30):
            out = self.renderer(max_reply_chars=limit).render('你好[OP:at,id=30003]一起聊聊', message())
            self.assertNotIn('GEYI_ACTION', out.text)
            self.assertNotIn('\ue000', out.text)
            self.assert_actions(out.text, ('[CQ:at,qq=30003]',) if '[CQ:' in out.text else ())

    def test_fallback_is_text(self):
        renderer = self.renderer(reply_audit_enabled=True, reply_audit_fallback='[CQ:at,qq=all]')
        out = renderer.render('password=12345678', message())
        self.assert_actions(out.text)
        self.assertIn('&#91;CQ:', out.text)


class FakeRuntime:
    def __init__(self):
        self.live = []
        self.turns = []
        self.live_context = SimpleNamespace(append=lambda **kw: self.live.append(kw),
                                            _states={'wrong:qq:onebot:private:host:20002': object()})
        self.store = SimpleNamespace(add_turn=lambda **kw: self.turns.append(kw))
        self.seq = 0
        self.self_queries = []

    def _next_arrival_seq(self):
        self.seq += 1
        return self.seq

    def is_recently_sent_by_self(self, session_key, text):
        self.self_queries.append((session_key, text))
        return False


def sessions():
    return [SESSION, replace(SESSION, bot_hash='bot-b'), replace(SESSION, platform='discord'),
            replace(SESSION, sdk='other'), replace(SESSION, host_id='host-a'),
            replace(SESSION, host_id='host-b')]


class ObserverIdentityTest(unittest.TestCase):
    def test_multi_bot_platform_sdk_host_matrix(self):
        runtime = FakeRuntime()
        observer = CrossPluginObserver(runtime)
        for session in sessions():
            observer.on_other_plugin_sent(session, '相同系统输出', bot_id='90001')
        expected = [s.as_text() for s in sessions()]
        self.assertEqual([t['session_key'] for t in runtime.turns], expected)
        self.assertEqual([t['session_key'] for t in runtime.live], expected)
        self.assertEqual([k for k, _ in runtime.self_queries], expected)
        self.assertTrue(all(t['sender_id'] == '90001' for t in runtime.turns))

    def test_full_text_dedup_not_prefix(self):
        runtime = FakeRuntime()
        observer = CrossPluginObserver(runtime)
        for text in ('同' * 81 + '甲', '同' * 81 + '乙', '同' * 81 + '甲'):
            observer.on_other_plugin_sent(SESSION, text, bot_id='90001')
        self.assertEqual(len(runtime.turns), 2)

    def test_private_and_missing_identity_are_skipped(self):
        runtime = FakeRuntime()
        observer = CrossPluginObserver(runtime)
        for session in [replace(SESSION, session_type='private'), replace(SESSION, bot_hash=''),
                        replace(SESSION, sdk=''), replace(SESSION, platform=''),
                        replace(SESSION, target_id=''), '20002']:
            observer.on_other_plugin_sent(session, '系统输出', bot_id='90001')
        observer.on_other_plugin_sent(SESSION, '系统输出')
        self.assertEqual(runtime.turns, [])
        self.assertEqual(runtime.live, [])

    def test_event_key_uses_real_fields_and_missing_fields_skip(self):
        for session in sessions():
            e = event(session)
            self.assertEqual(_event_group_session(e, session.target_id, session.host_id), session)
        for e in (None, SimpleNamespace(), event(replace(SESSION, bot_hash=''))):
            self.assertIsNone(_event_group_session(e, '20002'))

    def test_both_wrappers_dedup_group_and_skip_private_dice_reply(self):
        runtime = FakeRuntime()
        original_calls = []
        dice = SimpleNamespace(crossHook=SimpleNamespace(dictHookFunc={
            'msgHook': lambda *args, **kw: original_calls.append(args) or 'dice-ok'}))
        class Tx:
            def doRouter(self):
                return 'router-ok'
        adapter = SimpleNamespace(eventRouter=SimpleNamespace(txEvent=Tx))
        with patch.dict('sys.modules', {'OlivaDiceCore': dice, 'OlivOSOnebotV11': adapter}):
            observer = install_cross_plugin_observer(runtime)
            try:
                for i, session in enumerate(sessions()):
                    e = event(session, '9000' + str(i))
                    hook = dice.crossHook.dictHookFunc['msgHook']
                    self.assertEqual(hook(e, 'reply', {'id': 'incoming-user'},
                                          [session.host_id, session.target_id, '30003'], '系统输出'), 'dice-ok')
                    tx = Tx()
                    tx.funcType = 'send_group_msg'
                    tx.params = {'group_id': session.target_id, 'message': '系统输出'}
                    tx.plugin_event = e
                    self.assertEqual(tx.doRouter(), 'router-ok')
                private = event(replace(SESSION, session_type='private'))
                hook(private, 'reply', {'id': '30003'}, ['', SESSION.target_id, '30003'], '私聊内容')
                tx.plugin_event = private
                tx.funcType = 'send_msg'
                tx.params = {'message_type': 'private', 'user_id': SESSION.target_id, 'message': '私聊内容'}
                tx.doRouter()
                self.assertEqual(len(runtime.turns), len(sessions()))
                self.assertEqual([t['sender_id'] for t in runtime.turns], ['9000' + str(i) for i in range(6)])
                # 出站参数明确指定不同 host 时须使用目标 host，不沿用事件来源。
                tx.plugin_event = event(SESSION)
                tx.funcType = 'send_group_msg'
                tx.params = {'group_id': SESSION.target_id, 'host_id': 'out-host', 'message': '目标host输出'}
                tx.doRouter()
                self.assertEqual(runtime.turns[-1]['session_key'], replace(SESSION, host_id='out-host').as_text())
                before = len(runtime.turns)
                for broken in (None, SimpleNamespace(), event(replace(SESSION, sdk=''))):
                    tx.plugin_event = broken
                    tx.doRouter()
                self.assertEqual(len(runtime.turns), before)
            finally:
                observer.detach()
        self.assertEqual(len(original_calls), 7)

    def test_runtime_cache_writes_use_complete_session(self):
        runtime = Runtime.__new__(Runtime)
        runtime._self_sent_lock = threading.Lock()
        runtime._inflight_self_sent = Counter()
        runtime._recent_self_sent = deque()
        runtime.store = SimpleNamespace(add_turn=lambda *args: None)
        runtime.live_context = SimpleNamespace(append_assistant=lambda *args: None)
        runtime._next_arrival_seq = lambda: 1
        for session in sessions() + [replace(SESSION, session_type='private')]:
            msg = replace(message(session=session), is_group=session.session_type == 'group',
                          event=SimpleNamespace(send=lambda *args: None))
            runtime.reply(msg, '正文')
            runtime._track_self_sent(session, '模板正文', msg.is_group)
            self.assertEqual(runtime._recent_self_sent[-1][0], session.as_text())
            self.assertEqual(runtime._recent_self_sent[-2][0], session.as_text())

    def test_runtime_self_cache_full_session_full_text(self):
        runtime = Runtime.__new__(Runtime)
        runtime._self_sent_lock = threading.Lock()
        runtime._inflight_self_sent = Counter()
        runtime._recent_self_sent = deque([(SESSION.as_text(), '完整系统输出', time.time())])
        self.assertTrue(runtime.is_recently_sent_by_self(SESSION.as_text(), '完整系统输出'))
        self.assertFalse(runtime.is_recently_sent_by_self(SESSION.as_text(), '系统输出'))
        for session in sessions()[1:] + [replace(SESSION, session_type='private')]:
            self.assertFalse(runtime.is_recently_sent_by_self(session.as_text(), '完整系统输出'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
