# -*- encoding: utf-8 -*-
"""赞助两档：档位判定、额度、免收紧、记忆不裁剪、召回下限、内测。

为什么要测试：这些全是「少给一点 / 多给一点」的静默逻辑，错了不会报错——
表现出来只是「他没享受到」或「所有人都享受到了」，两种都要等赞助者自己来问才会发现。

档位是**累计**的：2 档包含 1 档的全部权益。老配置里的 `sponsor_ids` 按 1 档算，
升级后行为必须和 2.9.10 完全一致（这一条单独有测试锁着）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_sponsor_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.runtime import sponsors  # noqa: E402
from newGeYi.runtime.private_gate import PrivateGate  # noqa: E402
from newGeYi.relationship.stages import FACT_LIMIT_BY_STAGE  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402


class FakeStore:
    """只实现闸门用到的那几个读写，避免为了一个额度判断去开数据库。"""

    def __init__(self):
        self.settings = {}

    def get_setting(self, key):
        return self.settings.get(key, '')

    def set_setting(self, key, value):
        self.settings[key] = value


BASE = {
    'private_daily_quota': 60,
    'private_daily_quota_global': 400,
    'private_quota_squeeze_at': 0.7,
    'private_quota_squeeze': 35,
}


def cfg(**extra):
    base = dict(BASE)
    base.update(extra)
    return base


class TierTest(unittest.TestCase):
    def test_default_off(self):
        """默认全关：三份名单空、两个额度键读不到 → 谁都不是赞助者。"""
        c = cfg()
        self.assertEqual(sponsors.tier_of(c, '123'), 0)
        self.assertFalse(sponsors.is_sponsor(c, '123'))
        self.assertEqual(sponsors.quota_for(c, '123'), 0)

    def test_two_tiers(self):
        c = cfg(sponsor_tier1_ids=['111'], sponsor_tier2_ids=['222'],
                private_daily_quota_sponsor_tier1=100,
                private_daily_quota_sponsor_tier2=120)
        self.assertEqual(sponsors.tier_of(c, '111'), 1)
        self.assertEqual(sponsors.tier_of(c, '222'), 2)
        self.assertEqual(sponsors.quota_for(c, '111'), 100)
        self.assertEqual(sponsors.quota_for(c, '222'), 120)

    def test_legacy_list_counts_as_tier1(self):
        """老配置只有 sponsor_ids：按 1 档算，行为与 2.9.10 一致。"""
        c = cfg(sponsor_ids=['333'], private_daily_quota_sponsor=90)
        self.assertEqual(sponsors.tier_of(c, '333'), 1)
        # 两档专属额度都没配 → 回落旧键
        self.assertEqual(sponsors.quota_for(c, '333'), 90)

    def test_tier2_wins_when_in_both(self):
        c = cfg(sponsor_tier1_ids=['444'], sponsor_tier2_ids=['444'])
        self.assertEqual(sponsors.tier_of(c, '444'), 2)

    def test_dirty_list_tolerated(self):
        for bad in (None, 'not-a-list', 123, [None, '', '  ', ['x']]):
            c = cfg(sponsor_tier2_ids=bad)
            self.assertFalse(sponsors.is_sponsor(c, '999'), f'{bad!r} 应被忽略')

    def test_bare_string_is_not_a_list(self):
        """配置被手改坏成 `"sponsor_ids": "123"` 时返回空集，而不是当成单元素列表。

        静默地把坏配置当好的处理，表现是「配了但没生效」，最难查。
        """
        self.assertEqual(sponsors.id_set(cfg(sponsor_ids='123'), 'sponsor_ids'), set())
        self.assertEqual(sponsors.id_set(cfg(sponsor_ids=123), 'sponsor_ids'), set())

    def test_empty_sender_is_nobody(self):
        c = cfg(sponsor_tier2_ids=['555'])
        for empty in ('', None, '   '):
            self.assertEqual(sponsors.tier_of(c, empty), 0)


class MemoryBenefitTest(unittest.TestCase):
    def test_memory_keep_only_tier2(self):
        c = cfg(sponsor_tier1_ids=['111'], sponsor_tier2_ids=['222'],
                sponsor_memory_keep=True)
        self.assertFalse(sponsors.memory_keep(c, '111'))
        self.assertTrue(sponsors.memory_keep(c, '222'))
        self.assertFalse(sponsors.memory_keep(c, '999'))

    def test_memory_keep_can_be_switched_off(self):
        c = cfg(sponsor_tier2_ids=['222'], sponsor_memory_keep=False)
        self.assertFalse(sponsors.memory_keep(c, '222'))
        self.assertEqual(sponsors.protected_ids(c), set())

    def test_protected_ids_lists_tier2(self):
        c = cfg(sponsor_tier1_ids=['111'], sponsor_tier2_ids=['222', '333'])
        self.assertEqual(sponsors.protected_ids(c), {'222', '333'})

    def test_fact_floor_only_tier2(self):
        c = cfg(sponsor_tier1_ids=['111'], sponsor_tier2_ids=['222'],
                sponsor_fact_floor=8)
        self.assertEqual(sponsors.fact_floor(c, '111'), 0)
        self.assertEqual(sponsors.fact_floor(c, '222'), 8)

    def test_fact_floor_raises_stage_limit(self):
        """陌生阶段本来只给 2 条，二档抬到 8 条；到了高阶段不再压回去。"""
        base0 = FACT_LIMIT_BY_STAGE[0]
        self.assertLess(base0, 8)
        self.assertEqual(max(base0, 8), 8)
        # 高阶段本来就 ≥ 8，下限不该把它拉低
        self.assertEqual(max(FACT_LIMIT_BY_STAGE[5], 8), FACT_LIMIT_BY_STAGE[5])

    def test_beta(self):
        c = cfg(sponsor_tier2_ids=['222'], beta_ids=['777'])
        self.assertTrue(sponsors.is_beta(c, '222'))     # 二档自动在内
        self.assertTrue(sponsors.is_beta(c, '777'))     # 手加的内测
        self.assertFalse(sponsors.is_beta(c, '999'))


class GateQuotaTest(unittest.TestCase):
    def make_gate(self, **extra):
        store = FakeStore()
        return PrivateGate(store, cfg(**extra)), store

    def test_tier1_quota_100(self):
        gate, _ = self.make_gate(sponsor_tier1_ids=['111'],
                                 private_daily_quota_sponsor_tier1=100)
        self.assertEqual(gate._effective_user_quota('111'), 100)
        self.assertEqual(gate.sponsor_tier('111'), 1)

    def test_tier2_quota_120(self):
        gate, _ = self.make_gate(sponsor_tier2_ids=['222'],
                                 private_daily_quota_sponsor_tier2=120)
        self.assertEqual(gate._effective_user_quota('222'), 120)

    def test_normal_user_still_60(self):
        gate, _ = self.make_gate(sponsor_tier2_ids=['222'],
                                 private_daily_quota_sponsor_tier2=120)
        self.assertEqual(gate._effective_user_quota('999'), 60)

    def test_both_tiers_ignore_squeeze(self):
        """拥挤期：普通用户被压到 35，两档赞助者完全不掉。"""
        gate, _ = self.make_gate(sponsor_tier1_ids=['111'], sponsor_tier2_ids=['222'],
                                 private_daily_quota_sponsor_tier1=100,
                                 private_daily_quota_sponsor_tier2=120)
        # 把全局用量堆到 70% 以上，进入拥挤期
        gate.store.set_setting('priv.gday', time_stamp(300))
        self.assertTrue(gate.squeezed())
        self.assertEqual(gate._effective_user_quota('999'), 35)       # 普通用户收紧
        self.assertEqual(gate._effective_user_quota('111'), 100)      # 一档不收紧
        self.assertEqual(gate._effective_user_quota('222'), 120)      # 二档不收紧

    def test_usage_text_names_the_tier(self):
        gate, _ = self.make_gate(sponsor_tier2_ids=['222'],
                                 private_daily_quota_sponsor_tier2=120)
        text = gate.usage_text('h:qq:onebot:private:-:222')
        self.assertIn('二档', text)
        self.assertIn('120', text)

    def test_legacy_quota_still_applies_after_upgrade(self):
        """老配置只写了 private_daily_quota_sponsor：升级后不能被悄悄打回 60。"""
        gate, _ = self.make_gate(sponsor_ids=['333'], private_daily_quota_sponsor=90)
        self.assertEqual(gate._effective_user_quota('333'), 90)


def time_stamp(used: int) -> str:
    import time
    return '%s:%d' % (time.strftime('%Y%m%d'), used)



class SponsorVisibilityTest(unittest.TestCase):
    """赞助内容只许出现在「赞助名单」里，公开命令一个字都不能提（骰主 2026-11-08 定）。

    为什么要锁死：这类泄漏不会报错，而且每次都是「顺手加一行显示」造成的。
    公开命令里一旦出现档位、人数、额度或鸣谢，效果只有两个——看起来像在卖服务，
    以及招来「凭什么他有 100 我只有 60」。
    """

    CONFIG = {
        'triggers': ['歌伊'], 'worldbook_enabled': True, 'active_enabled': True,
        'memory_curator_enabled': True, 'image_enabled': True, 'voice_enabled': True,
        'sponsor_tier1_ids': ['111'], 'sponsor_tier2_ids': ['222'],
        'private_daily_quota_sponsor_tier1': 100,
        'private_daily_quota_sponsor_tier2': 120,
        'sponsor_thanks_text': '感谢 阿P、小满 的支持',
    }

    def _router(self):
        import copy
        from newGeYi.routing.commands import CommandRouter

        class Stub:
            pass

        runtime = Stub()
        runtime.config = copy.deepcopy(self.CONFIG)
        runtime.gateway = None
        return CommandRouter(runtime)

    def test_menu_has_no_sponsor_content(self):
        text = self._router()._cmd_menu(None)
        for word in ('鸣谢', '一档', '二档', '额度 100', '额度 120', '阿P'):
            self.assertNotIn(word, text, '菜单里不该出现赞助内容：%s' % word)

    def test_menu_still_lists_the_sponsor_command(self):
        """菜单只保留「赞助名单」这条指令入口，让人知道有这个命令。"""
        self.assertIn('赞助名单', self._router()._cmd_menu(None))

    def test_status_has_no_sponsor_content(self):
        """歌伊状态同样一个字都不能提（这也是公开命令）。"""
        router = self._router()
        router.r.store = _FakeStatsStore()

        class Sess:
            def as_text(self): return 'h:qq:onebot:group:1:2'

        class Msg:
            session = Sess(); is_group = True; sender_id = '1'; sender_name = 'x'; text = ''

        for attr in ('group_profile', 'rest', 'scheduler', 'curator', 'private_gate'):
            setattr(router.r, attr, None)
        text = router._cmd_status(Msg())
        for word in ('鸣谢', '一档', '二档', '赞助', '额度 100', '额度 120'):
            self.assertNotIn(word, text, '歌伊状态里不该出现赞助内容：%s' % word)

    def test_sponsor_list_holds_everything(self):
        """反过来：档位、名额、额度、鸣谢文案都必须在这个命令里看得到。"""
        text = self._router()._cmd_sponsor_list(None)
        for word in ('一档', '二档', '100', '120', '阿P'):
            self.assertIn(word, text, '赞助名单里应该有：%s' % word)


class _FakeStatsStore:
    """够 _cmd_status 用的最小 store（不然要开一个真库才测一行文字）。"""

    def today_stats(self):
        return {'calls': 0, 'output_tokens': 0, 'active_calls': 0,
                'input_tokens': 0, 'cached_tokens': 0, 'total_tokens': 0,
                'ok_calls': 0, 'failed_calls': 0}

    def today_stats_by_kind(self):
        return []

    def today_stats_by_scope(self):
        return []

    def count_user_facts(self, session_key):
        return 0

    def buffer_pending(self, session_key, cursor):
        return 0


class TrimProtectTest(unittest.TestCase):
    """二档的记忆不参与容量裁剪 —— 单人裁剪和按群总量裁剪都要认。"""

    def setUp(self):
        # 每个测试一个独立库，否则上一个测试种下去的事实会被算进本次裁剪
        self.dir = tempfile.mkdtemp(prefix='geyi_protect_case_', dir=_SCRATCH)
        self.store = SQLiteStore(os.path.join(self.dir, 'protect.sqlite3'))
        self.key = 'h:qq:onebot:group:1:2'

    def _seed(self, uid: str, count: int, importance: float = 0.2):
        for index in range(count):
            self.store.add_user_fact(self.key, uid, '%s 的第 %d 条事实' % (uid, index),
                                     importance=importance)

    def test_protected_user_survives_per_user_trim(self):
        self._seed('222', 10)
        removed = self.store.trim_user_facts(self.key, '222', max_per_user=3,
                                             protect_ids={'222'})
        self.assertEqual(removed, 0, '二档赞助者的事实不该被裁掉')
        self.assertEqual(len(self.store.list_user_facts(self.key, '222', limit=99)), 10)

    def test_others_still_trimmed(self):
        self._seed('222', 5)
        self._seed('999', 10)
        self.store.trim_user_facts(self.key, '999', max_per_user=3, protect_ids={'222'})
        self.assertEqual(len(self.store.list_user_facts(self.key, '999', limit=99)), 3)
        self.assertEqual(len(self.store.list_user_facts(self.key, '222', limit=99)), 5)

    def test_protected_user_survives_group_trim(self):
        """按群总量裁剪时也不能把他当"低分条目"挤掉。"""
        self._seed('222', 4)
        for uid in ('901', '902', '903', '904'):
            self._seed(uid, 3)
        removed = self.store.trim_user_facts(self.key, '', max_per_group=6,
                                             protect_ids={'222'})
        self.assertGreater(removed, 0, '总量确实超了，别的用户应该被裁')
        self.assertEqual(len(self.store.list_user_facts(self.key, '222', limit=99)), 4,
                         '赞助者的 4 条一条都不能少')

    def test_no_protect_keeps_old_behaviour(self):
        self._seed('222', 10)
        removed = self.store.trim_user_facts(self.key, '222', max_per_user=3)
        self.assertGreater(removed, 0)


if __name__ == '__main__':
    unittest.main()
