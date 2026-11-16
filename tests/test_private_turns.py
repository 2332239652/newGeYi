# -*- encoding: utf-8 -*-
"""私聊回合合并、本地优先级与安静控制的回归测试（离线、无网络）。"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace

_SCRATCH = tempfile.mkdtemp(prefix='geyi_private_turns_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.commands import CommandRouter  # noqa: E402
from newGeYi.routing.message_router import MessageRouter  # noqa: E402
from newGeYi.runtime.app import Runtime  # noqa: E402
from newGeYi.runtime.private_turns import (  # noqa: E402
    PrivateTurnCoordinator, classify_private_turn, strip_current_turn)

_SEQ = 0


def _next_uid() -> str:
    global _SEQ
    _SEQ += 1
    return f'8800{_SEQ:04d}'


def make_private_msg(uid: str, text: str, *, mentioned_bot: bool = False,
                     seq: int = 1, replies: list | None = None) -> IncomingMessage:
    session = SessionKey(bot_hash='bot1', platform='qq', sdk='onebot',
                         session_type='private', host_id='-', target_id=uid)
    sent = replies if replies is not None else []
    event = types.SimpleNamespace(
        send=lambda *a, **k: sent.append((a, k)),
        reply=lambda msg: sent.append(msg),
    )
    return IncomingMessage(
        session=session, message_id=f'm{seq}', sender_id=uid, sender_name='测试用户',
        text=text, raw_text=text, is_group=False, mentioned_bot=mentioned_bot,
        event=event, proc=None, arrival_seq=seq, sender_is_bot=False,
    )


class PrivateTurnsTest(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，测试用人设。')
        self.rt.config.update({
            'private_enabled': True,
            'private_trigger_only': False,
            'private_companion_mode': 'natural',
            'private_companion_debounce_seconds': 0.12,
            'private_companion_invite_delay_seconds': 0.05,
            'private_companion_max_wait_seconds': 0.30,
            'private_min_interval': 0,
            'private_burst_window': 0,
            'private_burst_max': 0,
            'private_daily_quota': 0,
            'private_daily_quota_global': 0,
        })
        self.uid = _next_uid()
        self.submitted: list[tuple[IncomingMessage, str]] = []
        self.submit_event = threading.Event()

        def fake_submit(msg, kind):
            self.submitted.append((msg, kind))
            self.submit_event.set()
            return True

        self.rt.scheduler.submit = fake_submit

    def tearDown(self):
        self.rt.private_turns.shutdown()
        self.rt.scheduler.shutdown()
        self.rt.voice_manager._executor.shutdown(wait=False)
        self.rt.relationship.shutdown()
        self.rt.store.close()

    def test_direct_invitation_replies_even_in_focused_mode(self):
        self.rt.config['private_companion_mode'] = 'focused'
        msg = make_private_msg(self.uid, '歌伊在吗，帮我看看这个')
        self.rt.router.route(msg)
        self.assertTrue(self.submit_event.wait(1.0))
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(self.submitted[0][1], 'passive')

    def test_natural_vs_warm_mode_scoring(self):
        msg = make_private_msg(self.uid, '今天去买了杯热美式，路上风挺大的，差点吹跑了')
        reply_natural, _, score_natural = classify_private_turn(self.rt, msg, [msg], 'natural')
        reply_warm, _, score_warm = classify_private_turn(self.rt, msg, [msg], 'warm')
        self.assertEqual(score_natural, 1)
        self.assertFalse(reply_natural)
        self.assertEqual(score_warm, 1)
        self.assertTrue(reply_warm)

    def test_ack_and_media_stay_silent_in_natural_mode(self):
        for text in ('嗯', '好的', '哈哈', '【图片】'):
            msg = make_private_msg(self.uid, text)
            reply, _, _ = classify_private_turn(self.rt, msg, [msg], 'natural')
            self.assertFalse(reply, f'{text!r} 不应单独触发回复')

    def test_batch_coalesces_to_single_enqueue_and_preserves_turns(self):
        m1 = make_private_msg(self.uid, '我今天加班到九点', seq=1)
        m2 = make_private_msg(self.uid, '好累啊，刚到家', seq=2)
        for m in (m1, m2):
            self.rt._remember_user_turn(m)
            from dataclasses import replace
            self.rt.router.route(replace(m, history_pre_recorded=True))
        self.assertTrue(self.submit_event.wait(1.0))
        self.assertEqual(len(self.submitted), 1)
        turns = self.rt.store.recent_turns(m1.session.as_text(), limit=10)
        user_turns = [t['content'] for t in turns if t['role'] == 'user']
        self.assertEqual(user_turns, ['我今天加班到九点', '好累啊，刚到家'])

    def test_max_wait_caps_continuous_messages(self):
        self.rt.config['private_companion_debounce_seconds'] = 0.15
        self.rt.config['private_companion_max_wait_seconds'] = 0.25
        for i in range(4):
            self.rt.router.route(make_private_msg(self.uid, f'第{i}句，我好累啊', seq=i + 1))
            time.sleep(0.08)
        self.assertTrue(self.submit_event.wait(1.0))
        self.assertEqual(len(self.submitted), 1)

    def test_quiet_command_silences_and_resume_restores(self):
        replies: list = []
        cmd_quiet = make_private_msg(self.uid, '暂时安静', replies=replies)
        self.assertTrue(self.rt.commands.try_handle(cmd_quiet))
        self.assertIn('安静', str(replies[-1]))

        self.rt.router.route(make_private_msg(self.uid, '歌伊在吗，帮我看看'))
        time.sleep(0.20)
        self.assertEqual(self.submitted, [])

        cmd_status = make_private_msg(self.uid, '陪聊状态', replies=replies)
        self.assertTrue(self.rt.commands.try_handle(cmd_status))
        self.assertIn('安静', str(replies[-1]))

        cmd_resume = make_private_msg(self.uid, '恢复陪聊', replies=replies)
        self.assertTrue(self.rt.commands.try_handle(cmd_resume))

        self.rt.router.route(make_private_msg(self.uid, '歌伊在吗，帮我看看'))
        self.assertTrue(self.submit_event.wait(1.0))
        self.assertEqual(len(self.submitted), 1)

    def test_mode_command_persists_per_session(self):
        replies: list = []
        self.assertTrue(self.rt.commands.try_handle(make_private_msg(self.uid, '陪聊模式 专注', replies=replies)))
        self.assertIn('专注', str(replies[-1]))
        self.rt.router.route(make_private_msg(self.uid, '我今天好累啊，想找人聊聊'))
        time.sleep(0.20)
        self.assertEqual(self.submitted, [])

    def test_quota_committed_once_after_selection(self):
        self.rt.config['private_daily_quota'] = 1
        self.rt.router.route(make_private_msg(self.uid, '我今天好累啊'))
        self.rt.router.route(make_private_msg(self.uid, '压力好大'))
        self.assertTrue(self.submit_event.wait(1.0))
        time.sleep(0.05)
        self.assertEqual(len(self.submitted), 1)
        session_key = make_private_msg(self.uid, '').session.as_text()
        self.assertEqual(self.rt.private_gate.used_today(session_key), 1)

    def test_shutdown_cancels_pending_timers(self):
        flushed: list = []
        coord = PrivateTurnCoordinator(self.rt, lambda msgs: flushed.append(msgs))
        coord.offer(make_private_msg(self.uid, '歌伊在吗，帮我看看'))
        coord.shutdown()
        time.sleep(0.15)
        self.assertEqual(flushed, [])

    def test_clear_context_works_for_private_and_group_admins(self):
        replies: list[str] = []
        self.rt.reply = lambda msg, text: replies.append(text)
        self.rt.config['owner_ids'] = ['10000']
        priv_msg = make_private_msg(self.uid, '清空上下文')
        priv_key = priv_msg.session.as_text()
        self.rt.store.add_turn(priv_key, 'user', '前文一')
        self.assertTrue(self.rt.commands.try_handle(priv_msg))
        self.assertEqual(self.rt.store.recent_turns(priv_key, limit=10), [])
        self.assertIn('已清空当前会话上下文', replies[-1])

        group_msg = IncomingMessage(
            SessionKey('bot', 'qq', 'onebot', 'group', target_id='30001'),
            'm-g1', '900001', '普通群员', '清空上下文', '清空上下文', True, False, None, None,
        )
        group_key = group_msg.session.as_text()
        self.rt.store.add_turn(group_key, 'user', '群前文')
        self.rt.live_context.append_user(group_msg)
        self.assertTrue(self.rt.commands.try_handle(group_msg))
        self.assertEqual(len(self.rt.store.recent_turns(group_key, limit=10)), 1)
        self.assertIn('群管或骰主', replies[-1])

        owner_group_msg = IncomingMessage(
            SessionKey('bot', 'qq', 'onebot', 'group', target_id='30001'),
            'm-g2', '10000', '主人', '清空上下文', '清空上下文', True, False, None, None,
        )
        self.assertTrue(self.rt.commands.try_handle(owner_group_msg))
        self.assertEqual(self.rt.store.recent_turns(group_key, limit=10), [])
        self.assertEqual(self.rt.live_context.snapshot(group_key, before_source_seq=999), [])


class StripCurrentTurnTest(unittest.TestCase):
    """复读判定的历史快照必须排除「本轮自己」（到达即落库造成的自我污染）。"""

    def test_single_message_drops_only_current_row(self):
        msg = make_private_msg('9100777', '晚安歌伊', seq=2)
        history = (
            {'role': 'user', 'content': '今天好累呀'},
            {'role': 'assistant', 'content': '那就早点睡～'},
            {'role': 'user', 'content': '晚安歌伊'},
        )
        kept = strip_current_turn(history, msg)
        self.assertEqual([row['content'] for row in kept], ['今天好累呀', '那就早点睡～'])

    def test_previous_identical_message_is_kept(self):
        msg = make_private_msg('9100778', '晚安歌伊', seq=3)
        history = (
            {'role': 'user', 'content': '晚安歌伊'},
            {'role': 'assistant', 'content': '晚安～'},
            {'role': 'user', 'content': '晚安歌伊'},
        )
        kept = strip_current_turn(history, msg)
        self.assertEqual([row['content'] for row in kept], ['晚安歌伊', '晚安～'])

    def test_coalesced_turn_drops_every_message_of_this_turn(self):
        first = make_private_msg('9100779', '在吗', seq=4)
        second = make_private_msg('9100779', '今天好累呀', seq=5)
        msg = replace(second, text='在吗\n今天好累呀', coalesced_messages=(first, second),
                      history_pre_recorded=True)
        history = (
            {'role': 'assistant', 'content': '刚睡醒～'},
            {'role': 'user', 'content': '在吗'},
            {'role': 'user', 'content': '今天好累呀'},
        )
        kept = strip_current_turn(history, msg)
        self.assertEqual([row['content'] for row in kept], ['刚睡醒～'])

    def test_row_that_is_not_this_turn_is_left_alone(self):
        msg = make_private_msg('9100780', '晚安歌伊', seq=6)
        history = (
            {'role': 'user', 'content': '晚安歌伊'},
            {'role': 'assistant', 'content': '晚安～'},
        )
        self.assertEqual(len(strip_current_turn(history, msg)), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
