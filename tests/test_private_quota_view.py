# -*- encoding: utf-8 -*-
"""私聊「今日额度」：用户自己看用量 + 额度用尽时的提示。

骰主 2026-11-10：「大家希望私信可以查看今日额度次数」。
以前只有骰主的「私聊用量」能看到，普通用户被限额挡住时只表现为"她不说话"。
"""

from __future__ import annotations

import itertools
import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_quota_view_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.message_router import MessageRouter  # noqa: E402
from newGeYi.runtime.app import Runtime  # noqa: E402

_SEQ = itertools.count(4100001)


def make_msg(text, uid, is_group=False):
    session = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='-' if is_group else '', target_id=uid)
    sent = []
    event = types.SimpleNamespace(send=lambda *a, **k: sent.append(a))
    msg = IncomingMessage(
        session=session, message_id=str(next(_SEQ)), sender_id=uid, sender_name='测试',
        text=text, raw_text=text, is_group=is_group, mentioned_bot=False,
        event=event, proc=None, arrival_seq=next(_SEQ), sender_is_bot=False,
    )
    return msg, sent


class QuotaViewTest(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime()
        self.uid = str(next(_SEQ))
        self.rt.config.update({
            'private_enabled': True,
            'private_trigger_only': False,
            'private_min_interval': 0,
            'private_burst_window': 0,
            'private_burst_max': 0,
            'private_daily_quota': 30,
            'private_daily_quota_global': 0,
        })
        self.rt.prompt_builder.set_role_prompt('你是歌伊，测试人设。')

    def tearDown(self):
        self.rt.scheduler.shutdown()
        self.rt.voice_manager._executor.shutdown(wait=False)
        self.rt.relationship.shutdown()
        self.rt.store.close()

    def _say(self, text, is_group=False):
        msg, sent = make_msg(text, self.uid, is_group=is_group)
        handled = self.rt.commands.try_handle(msg)
        return handled, (sent[0][2] if sent else '')

    def _session(self):
        return SessionKey('h', 'qq', 'onebot', 'private', '', self.uid).as_text()

    def test_private_shows_used_and_remaining(self):
        self.rt.config['private_daily_quota'] = 30
        for _ in range(3):
            self.rt.private_gate.commit(self._session())
        handled, text = self._say('今日额度')
        self.assertTrue(handled)
        self.assertIn('已用 3 / 30', text)
        self.assertIn('还能聊 27', text)
        self.assertIn('0 点重置', text)

    def test_exhausted_state_is_spelled_out(self):
        self.rt.config['private_daily_quota'] = 2
        for _ in range(2):
            self.rt.private_gate.commit(self._session())
        _handled, text = self._say('我的额度')
        self.assertIn('已用 2 / 2', text)
        self.assertIn('省点 token', text, '用完时要说明是为了省主人的 token')

    def test_no_quota_configured(self):
        self.rt.config['private_daily_quota'] = 0
        _handled, text = self._say('额度')
        self.assertIn('没有设置每日上限', text)

    def test_group_points_to_private(self):
        handled, text = self._say('今日额度', is_group=True)
        self.assertTrue(handled)
        self.assertIn('私聊里才能看', text)

    def test_sponsor_tier_is_shown(self):
        self.rt.config.update({
            'private_daily_quota': 30,
            'private_daily_quota_sponsor_tier1': 120,
            'sponsor_ids': [self.uid],
        })
        _handled, text = self._say('今日额度')
        self.assertIn('已用 0 / 120', text)
        self.assertIn('赞助', text)


class QuotaHintTest(unittest.TestCase):
    """额度用尽时给一次提示（每天每会话只提示一次），别让人以为她坏了。"""

    def setUp(self):
        self.rt = Runtime()
        self.uid = str(next(_SEQ))
        self.rt.config.update({
            'private_enabled': True,
            'private_trigger_only': False,
            'private_min_interval': 0,
            'private_burst_window': 0,
            'private_burst_max': 0,
            'private_daily_quota': 1,
            'private_daily_quota_global': 0,
        })
        self.rt.prompt_builder.set_role_prompt('你是歌伊，测试人设。')
        self.submitted = []
        self.rt.scheduler.submit = lambda msg, kind: (self.submitted.append(kind), True)[1]
        self.router = MessageRouter(self.rt)

    def tearDown(self):
        self.rt.scheduler.shutdown()
        self.rt.voice_manager._executor.shutdown(wait=False)
        self.rt.relationship.shutdown()
        self.rt.store.close()

    def test_hint_once_per_day_per_session(self):
        msg1, _sent1 = make_msg('第一句', self.uid)
        self.router.route(msg1)
        self.assertEqual(self.submitted, ['passive'])

        msg2, sent2 = make_msg('第二句', self.uid)
        self.router.route(msg2)
        self.assertEqual(len(sent2), 1, '额度用尽时应当提示一次')
        self.assertIn('token', sent2[0][2], '措辞要点明是在给主人省 token')

        msg3, sent3 = make_msg('第三句', self.uid)
        self.router.route(msg3)
        self.assertEqual(sent3, [], '同一天不重复提示')

    def test_global_quota_has_its_own_wording(self):
        self.rt.config['private_daily_quota'] = 0
        self.rt.config['private_daily_quota_global'] = 1
        self.rt.private_gate.commit('other:session')
        msg, sent = make_msg('在吗', self.uid)
        self.router.route(msg)
        self.assertEqual(len(sent), 1)
        self.assertIn('人有点多', sent[0][2])


if __name__ == '__main__':
    unittest.main(verbosity=2)
