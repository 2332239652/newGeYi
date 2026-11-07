# -*- encoding: utf-8 -*-
"""私聊闸门：触发词 / 防连发 / 每日限额。

骰主 2026-11-05：「私聊多了并发好像有点扛不住了……加一个选项，调整私聊只有在触发关键词
的时候才回应，并且写个防连发和每日次数限额吧」。

为什么这些必须有测试：全是"少回一句"的负向逻辑，坏了不会报错——
只会表现为「她突然不理人」或「限额形同虚设」，两种都很难在生产里复现。
"""

from __future__ import annotations

import itertools
import os
import tempfile
import time
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_pgate_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.runtime.private_gate import PrivateGate  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402

_SEQ = itertools.count(3100001)

BASE_CFG = {
    'triggers': ['歌伊'],
    'assistant_name': '歌伊',
    'private_trigger_extra': [],
    'private_trigger_only': False,
    'private_min_interval': 0,
    'private_burst_window': 0,
    'private_burst_max': 0,
    'private_daily_quota': 0,
    'private_daily_quota_global': 0,
}


def make_private_msg(uid: str, text: str = '在吗'):
    session = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                         session_type='private', host_id='', target_id=uid)
    return IncomingMessage(
        session=session, message_id=str(next(_SEQ)), sender_id=uid, sender_name='测试',
        text=text, raw_text=text, is_group=False, mentioned_bot=False,
        event=types.SimpleNamespace(send=lambda *a, **k: None), proc=None,
        arrival_seq=next(_SEQ), sender_is_bot=False,
    )


class PrivateGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='geyi_pgate_db_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'gate.sqlite3'))
        self.config = dict(BASE_CFG)
        self.gate = PrivateGate(self.store, self.config)

    def tearDown(self):
        self.store.close()

    # ---- 触发词 ----
    def test_trigger_only_off_always_allows(self):
        msg = make_private_msg('1111', '随便说点什么')
        self.assertEqual(self.gate.allow(msg), (True, ''))

    def test_trigger_only_blocks_without_keyword(self):
        self.config['private_trigger_only'] = True
        msg = make_private_msg('1111', '今天好累啊')
        self.assertEqual(self.gate.allow(msg), (False, 'trigger'))

    def test_trigger_only_passes_on_keyword(self):
        self.config['private_trigger_only'] = True
        self.assertEqual(self.gate.allow(make_private_msg('1111', '歌伊在吗')), (True, ''))

    def test_extra_keywords_work(self):
        self.config['private_trigger_only'] = True
        self.config['private_trigger_extra'] = ['在吗']
        self.assertEqual(self.gate.allow(make_private_msg('1111', '在吗')), (True, ''))

    # ---- 防连发 ----
    def test_min_interval_blocks_second_message(self):
        self.config['private_min_interval'] = 60
        msg = make_private_msg('1111')
        self.assertTrue(self.gate.allow(msg)[0])
        self.gate.commit(msg.session.as_text())
        self.assertEqual(self.gate.allow(msg), (False, 'interval'))

    def test_interval_expires(self):
        self.config['private_min_interval'] = 30
        key = 'h:qq:onebot:private:-:1111'
        self.store.set_setting('priv.last:' + key, str(time.time() - 31))
        self.assertTrue(self.gate.allow(make_private_msg('1111'))[0])

    def test_burst_window_blocks_flood(self):
        self.config['private_burst_window'] = 60
        self.config['private_burst_max'] = 3
        msg = make_private_msg('1111')
        for _ in range(3):
            self.assertTrue(self.gate.allow(msg)[0])
            self.gate.commit(msg.session.as_text())
        self.assertEqual(self.gate.allow(msg), (False, 'burst'))

    def test_burst_window_rolls_over(self):
        self.config['private_burst_window'] = 60
        self.config['private_burst_max'] = 1
        key = 'h:qq:onebot:private:-:1111'
        self.store.set_setting('priv.burst:' + key, '9:%d' % int(time.time() - 120))
        self.assertTrue(self.gate.allow(make_private_msg('1111'))[0])

    # ---- 每日限额 ----
    def test_user_daily_quota(self):
        self.config['private_daily_quota'] = 2
        msg = make_private_msg('1111')
        self.gate.commit(msg.session.as_text())
        self.gate.commit(msg.session.as_text())
        self.assertEqual(self.gate.allow(msg), (False, 'quota_user'))

    def test_global_daily_quota_blocks_other_users(self):
        self.config['private_daily_quota_global'] = 1
        a = make_private_msg('1111')
        b = make_private_msg('2222')
        self.gate.commit(a.session.as_text())
        self.assertEqual(self.gate.allow(b), (False, 'quota_global'))

    def test_counter_resets_next_day(self):
        key = 'h:qq:onebot:private:-:1111'
        self.store.set_setting('priv.use:' + key, '19700101:99')
        self.config['private_daily_quota'] = 1
        self.assertEqual(self.gate.used_today(key), 0)
        self.assertTrue(self.gate.allow(make_private_msg('1111'))[0])

    def test_usage_text_mentions_mode_and_quota(self):
        self.config['private_daily_quota'] = 5
        text = self.gate.usage_text('h:qq:onebot:private:-:1111')
        self.assertIn('全体私聊今日', text)
        self.assertIn('限额 5', text)

    # ---- 拥挤收紧（2026-11-07）----
    def test_squeeze_lowers_user_quota_when_global_busy(self):
        """全局用到 70% 之后，单人上限从 60 压到 35——保证晚来的人还有得聊。"""
        self.config.update({'private_daily_quota': 60,
                            'private_daily_quota_global': 10,
                            'private_quota_squeeze_at': 0.5,
                            'private_quota_squeeze': 3})
        self.assertEqual(self.gate._effective_user_quota(), 60)
        self.assertFalse(self.gate.squeezed())
        for uid in ('9001', '9002', '9003', '9004', '9005'):
            self.gate.commit(make_private_msg(uid).session.as_text())   # 全局用到 5 条 = 50%
        self.assertTrue(self.gate.squeezed())
        self.assertEqual(self.gate._effective_user_quota(), 3)
        self.assertIn('拥挤', self.gate.usage_text())

    def test_squeeze_disabled_when_ratio_zero(self):
        self.config.update({'private_daily_quota': 60,
                            'private_daily_quota_global': 4,
                            'private_quota_squeeze_at': 0,
                            'private_quota_squeeze': 3})
        for uid in ('9001', '9002', '9003', '9004'):
            self.gate.commit(make_private_msg(uid).session.as_text())
        self.assertFalse(self.gate.squeezed())
        self.assertEqual(self.gate._effective_user_quota(), 60)

    def test_squeeze_enforced_in_allow(self):
        self.config.update({'private_daily_quota': 60,
                            'private_daily_quota_global': 4,
                            'private_quota_squeeze_at': 0.25,
                            'private_quota_squeeze': 1})
        # 全局先被占 2 条（50% > 25%）→ 进入拥挤期，单人上限变 1
        self.gate.commit(make_private_msg('9001').session.as_text())
        self.gate.commit(make_private_msg('9002').session.as_text())
        msg = make_private_msg('7777')
        self.gate.commit(msg.session.as_text())                       # 他用掉唯一那次
        self.assertEqual(self.gate.allow(msg), (False, 'quota_user'))

    def test_squeeze_never_raises_quota(self):
        """squeeze 比正常限额还大时不应该反而放宽。"""
        self.config.update({'private_daily_quota': 10,
                            'private_daily_quota_global': 2,
                            'private_quota_squeeze_at': 0.5,
                            'private_quota_squeeze': 99})
        self.gate.commit(make_private_msg('9001').session.as_text())
        self.assertTrue(self.gate.squeezed())
        self.assertEqual(self.gate._effective_user_quota(), 10)

    # ---- 支持者（爱发电赞助）----
    def test_sponsor_gets_higher_quota(self):
        self.config.update({'private_daily_quota': 60,
                            'private_daily_quota_sponsor': 120,
                            'sponsor_ids': ['8888']})
        self.assertTrue(self.gate.is_sponsor('8888'))
        self.assertFalse(self.gate.is_sponsor('9999'))
        self.assertEqual(self.gate._effective_user_quota('8888'), 120)
        self.assertEqual(self.gate._effective_user_quota('9999'), 60)

    def test_sponsor_ignores_squeeze(self):
        """赞助者不受拥挤期收紧影响（人家出钱是为了稳定）。"""
        self.config.update({'private_daily_quota': 60,
                            'private_daily_quota_sponsor': 120,
                            'sponsor_ids': ['8888'],
                            'private_daily_quota_global': 4,
                            'private_quota_squeeze_at': 0.25,
                            'private_quota_squeeze': 10})
        for uid in ('9001', '9002', '9003'):
            self.gate.commit(make_private_msg(uid).session.as_text())
        self.assertTrue(self.gate.squeezed())
        self.assertEqual(self.gate._effective_user_quota('9999'), 10, '普通用户被收紧')
        self.assertEqual(self.gate._effective_user_quota('8888'), 120, '赞助者不收紧')

    def test_sponsor_quota_off_by_default(self):
        self.config['sponsor_ids'] = ['8888']
        self.assertEqual(self.gate._effective_user_quota('8888'), 0,
                         'private_daily_quota_sponsor 为 0 时功能不启用')

    def test_sponsor_tolerates_dirty_list(self):
        for bad in (None, '', 123, {'8888': 1}):
            self.config['sponsor_ids'] = bad
            self.assertEqual(self.gate.sponsor_ids(), set(), f'{bad!r} 应被忽略')
            self.assertFalse(self.gate.is_sponsor('8888'))

    def test_sponsor_usage_text_marks_sponsor(self):
        self.config.update({'private_daily_quota': 60,
                            'private_daily_quota_sponsor': 120,
                            'sponsor_ids': ['8888']})
        text = self.gate.usage_text('h:qq:onebot:private:-:8888')
        self.assertIn('赞助者', text)
        self.assertIn('限额 120', text)


class PrivateGateRoutingTest(unittest.TestCase):
    """走真实 route()：确认被闸门拦住时**根本不会提交任务**（这才是省并发的地方）。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime
        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')
        self.rt.config['private_enabled'] = True      # 私聊总开关（默认是关的）
        # ⚠️ 整个测试进程共用一个数据目录，上一轮跑留下的 priv.last/priv.gday 会串进来。
        # 这里把间隔和额度显式归零，用例只测自己想测的那一条。
        self.rt.config['private_min_interval'] = 0
        self.rt.config['private_burst_window'] = 0
        self.rt.config['private_burst_max'] = 0
        self.rt.config['private_daily_quota'] = 0
        self.rt.config['private_daily_quota_global'] = 0
        self.uid = '9%06d' % next(_SEQ)
        self.submitted = []
        self.rt.scheduler.submit = lambda msg, kind: (self.submitted.append(kind), True)[1]

    def _route(self, text, uid=None):
        from newGeYi.routing.message_router import MessageRouter
        # 每个用例用独立 QQ 号：闸门的计数是按会话键存的，复用会互相污染
        MessageRouter(self.rt).route(make_private_msg(uid or self.uid, text))

    def test_trigger_only_silences_untriggered_private(self):
        self.rt.config['private_trigger_only'] = True
        self._route('今天好累')
        self.assertEqual(self.submitted, [])
        self._route('歌伊在吗')
        self.assertEqual(self.submitted, ['passive'])

    def test_daily_quota_stops_submitting(self):
        self.rt.config['private_trigger_only'] = False
        self.rt.config['private_daily_quota'] = 1
        self.rt.config['private_min_interval'] = 0
        self._route('第一句')
        self._route('第二句')
        self.assertEqual(len(self.submitted), 1)

    def test_group_chat_is_not_affected(self):
        from newGeYi.routing.message_router import MessageRouter
        self.rt.config['private_trigger_only'] = True
        self.rt.config['private_daily_quota'] = 0
        session = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                             session_type='group', host_id='-', target_id='700111')
        msg = IncomingMessage(
            session=session, message_id='1', sender_id='900111', sender_name='测试',
            text='歌伊在吗', raw_text='歌伊在吗', is_group=True, mentioned_bot=False,
            event=types.SimpleNamespace(send=lambda *a, **k: None), proc=None,
            arrival_seq=1, sender_is_bot=False,
        )
        MessageRouter(self.rt).route(msg)
        self.assertEqual(len(self.submitted), 1, '群聊不该被私聊闸门影响')


if __name__ == '__main__':
    unittest.main(verbosity=2)
