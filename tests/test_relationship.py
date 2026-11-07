# -*- encoding: utf-8 -*-
"""私聊关系系统（galgame）的回归测试：离线、无网络、不碰线上状态。

分三层：
1. 状态机（纯代码）：难度全在这里，所以每条规则都要有用例锁住；
2. 世界书阶段门：深层设定在低阶段**根本不进 prompt**；
3. 服务 + PromptBuilder：评审结果怎么被夹紧、影子模式、主人豁免、阶段块注入。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_relationship_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.memory.worldbook import WorldbookService  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.relationship import state as S  # noqa: E402
from newGeYi.relationship.judge import parse_verdict  # noqa: E402
from newGeYi.relationship.stages import (EVENTS, STAGE_NAMES, audit_overstep,  # noqa: E402
                                         build_relation_block)

BOT = 'hash'
OWNER = '2332239652'
USER = '1000000002'
DAY = 86400.0


def make_msg(text, sender_id=USER, is_group=False, name='Oracle'):
    session = SessionKey(bot_hash=BOT, platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='-', target_id='9999' if is_group else sender_id)
    return IncomingMessage(
        session=session, message_id='1', sender_id=sender_id, sender_name=name,
        text=text, raw_text=text, is_group=is_group, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


def at_day(n: int, hour: int = 12) -> float:
    """第 n 天（从 2026-03-01 起算）的某个钟点，保证跨天用例不依赖真实时钟。"""
    base = time.mktime((2026, 3, 1, hour, 0, 0, 0, 0, -1))
    return base + n * DAY


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.p = S.Params()

    def test_delta_is_clamped_and_cap_is_daily(self):
        rel = S.Relation('u')
        now = at_day(0)
        total = 0.0
        tags = ('care', 'share', 'humor', 'curious_lore', 'respect')
        for i in range(60):
            out = S.apply_delta(rel, 99, tags[i % 5], now + i, self.p)   # 夸张的 +99 也只算 +3
            total += out.applied
        self.assertLessEqual(total, self.p.daily_cap[0] + 1e-9)
        self.assertAlmostEqual(rel.gain_today, total)

    def test_same_tag_scores_twice_then_stops(self):
        """2026-11-06 起：同一标签每天两次、各自独立计分，第三次归零。"""
        rel = S.Relation('u')
        gains = [S.apply_delta(rel, 2, 'care', at_day(0) + i, self.p).applied for i in range(4)]
        self.assertEqual(gains, [2.0, 2.0, 0.0, 0.0])
        # 第二次不折损：它按自己的 delta 算，跟第一次无关
        rel2 = S.Relation('u')
        self.assertEqual(S.apply_delta(rel2, 3, 'care', at_day(0), self.p).applied, 3.0)
        self.assertEqual(S.apply_delta(rel2, 1, 'care', at_day(0) + 1, self.p).applied, 1.0)
        # 换标签照样能记
        self.assertEqual(S.apply_delta(rel, 2, 'share', at_day(0) + 5, self.p).applied, 2.0)
        # 第二天重新计数
        again = S.apply_delta(rel, 2, 'care', at_day(1), self.p).applied
        self.assertEqual(again, 2.0)

    def test_tag_daily_limit_is_configurable(self):
        p = S.params_from_config({'galgame_tag_daily_limit': 1})
        self.assertEqual(p.tag_daily_limit, 1)
        rel = S.Relation('u')
        self.assertEqual(S.apply_delta(rel, 2, 'care', at_day(0), p).applied, 2.0)
        self.assertEqual(S.apply_delta(rel, 2, 'care', at_day(0) + 1, p).applied, 0.0)
        # 脏值回默认
        for bad in (0, 99, 'x', None):
            self.assertEqual(S.params_from_config({'galgame_tag_daily_limit': bad}).tag_daily_limit, 2)

    def test_cannot_bank_points_without_enough_days(self):
        """一晚上刷满分数也升不了级，且分数停在下一档门槛，不能存分。"""
        rel = S.Relation('u')
        now = at_day(0)
        tags = ('care', 'share', 'humor', 'curious_lore', 'respect')
        for i in range(200):
            S.apply_delta(rel, 2, tags[i % 5], now + i, self.p)
        self.assertLessEqual(rel.affection, self.p.daily_cap[0] + 1e-9)   # 一天最多 12 分
        self.assertEqual(rel.stage, 0)                  # 第一天连"熟人"(15 分)都到不了
        for i in range(200):
            S.apply_delta(rel, 2, tags[i % 5], at_day(1) + i, self.p)
        self.assertEqual(rel.stage, 1)                  # 第二天到熟人，但到不了朋友
        self.assertLessEqual(rel.affection, self.p.thresholds[2] + 1e-9)

    def test_one_stage_per_day_even_with_enough_points(self):
        rel = S.Relation('u', affection=60.0, stage=0, days_active=10)
        out = S.apply_delta(rel, 1, 'care', at_day(0), self.p)
        self.assertEqual(rel.stage, 1)
        out = S.apply_delta(rel, 1, 'share', at_day(0) + 5, self.p)
        self.assertEqual(rel.stage, 1)                  # 同一天不再升
        S.apply_delta(rel, 1, 'humor', at_day(1), self.p)
        self.assertEqual(rel.stage, 2)

    def test_event_gate_blocks_stage_4_until_event_done(self):
        rel = S.Relation('u', affection=189.0, stage=3, days_active=30)
        S.apply_delta(rel, 2, 'care', at_day(0), self.p)
        S.apply_delta(rel, 2, 'share', at_day(0) + 1, self.p)
        self.assertEqual(rel.stage, 3)
        self.assertEqual(rel.flags.get('pending_event'), 'A')
        self.assertLessEqual(rel.affection, self.p.thresholds[4] + 1e-9)   # 不存分
        S.complete_event(rel, 'A', at_day(1), self.p)
        self.assertEqual(rel.stage, 4)
        self.assertEqual(rel.flags.get('pending_event'), '')

    def test_bad_behaviour_locks_then_demotes(self):
        rel = S.Relation('u', affection=60.0, stage=2, days_active=10)
        rel.stage_since = at_day(0)
        now = at_day(1)
        for i in range(3):
            out = S.apply_delta(rel, -3, 'rude', now + i, self.p)
        self.assertGreater(rel.locked_until, now)                        # 进入冷却
        # 冷却期内正向互动不加分
        self.assertEqual(S.apply_delta(rel, 2, 'care', now + 10, self.p).applied, 0.0)
        # 持续恶劣 → 掉一级，但不会一次掉光
        for i in range(10):
            S.apply_delta(rel, -3, 'rude', now + 20 + i, self.p)
        self.assertLess(rel.stage, 2)
        self.assertGreaterEqual(rel.stage, 1)

    def test_affection_floor_protects_against_single_bad_turn(self):
        rel = S.Relation('u', affection=60.0, stage=2, days_active=10)
        S.apply_delta(rel, -3, 'rude', at_day(0), self.p)
        self.assertEqual(rel.stage, 2)
        self.assertGreaterEqual(rel.affection, self.p.thresholds[2] - self.p.demote_margin)

    def test_presence_advances_days_and_gives_small_bonus(self):
        rel = S.Relation('u')
        out = S.apply_presence(rel, at_day(0), self.p)
        self.assertEqual(rel.days_active, 1)
        self.assertEqual(out.applied, 1.0)
        S.apply_presence(rel, at_day(0) + 60, self.p)
        self.assertEqual(rel.days_active, 1)            # 同一天不重复计

    def test_roundtrip_through_row(self):
        rel = S.Relation('u', affection=12.5, stage=1, days_active=3)
        rel.flags['x'] = [1, 2]
        rel.tag_counts = {'care': 2}
        back = S.Relation.from_row('u', rel.to_row())
        self.assertEqual(back.flags, {'x': [1, 2]})
        self.assertEqual(back.tag_counts, {'care': 2})
        self.assertEqual(back.stage, 1)

    def test_bad_config_falls_back_to_defaults(self):
        p = S.params_from_config({'galgame_thresholds': [0, 50, 10, 99, 100, 200],
                                  'galgame_min_days': [1, 2]})
        self.assertEqual(p.thresholds, S.Params().thresholds)       # 非单调
        self.assertEqual(p.min_days, S.Params().min_days)           # 长度不对

    def test_rule_layer_helpers(self):
        self.assertTrue(S.is_trivial('嗯'))
        self.assertTrue(S.is_trivial('？？'))
        self.assertFalse(S.is_trivial('今天过得怎么样'))
        self.assertTrue(S.is_narration_hijack('（你脸红了，轻轻抱住我，把头靠在我肩上）'))
        self.assertFalse(S.is_narration_hijack('今天下雨了（笑）'))
        sim = lambda a, b: 1.0 if a == b else 0.0           # noqa: E731
        self.assertTrue(S.is_repeat('我喜欢你喜欢你', ['我喜欢你喜欢你'], sim))
        self.assertFalse(S.is_repeat('我喜欢你喜欢你', ['别的话'], sim))

    def test_simulated_rush_vs_steady_player(self):
        """速攻党（每天疯狂甜言蜜语）和认真玩家（每天几句真诚）到各阶段的天数。"""
        def play(daily_deltas, days):
            rel = S.Relation('u')
            for d in range(days):
                now = at_day(d)
                S.apply_presence(rel, now, self.p)
                for i, (delta, tag) in enumerate(daily_deltas):
                    S.apply_delta(rel, delta, tag, now + 10 + i, self.p)
                if rel.flags.get('pending_event') and rel.flags['pending_event'] != '':
                    S.complete_event(rel, rel.flags['pending_event'], now + 500, self.p)
            return rel
        rush = play([(2, 'care')] * 60, 7)
        self.assertLessEqual(rush.stage, 2)             # 一周内到不了"亲近"
        steady = play([(2, 'care'), (1, 'share'), (1, 'humor'), (2, 'curious_lore')], 90)
        self.assertEqual(steady.stage, 5)               # 认真玩 90 天能走到头
        quick = play([(2, 'care'), (1, 'share'), (1, 'humor'), (2, 'curious_lore')], 14)
        self.assertLessEqual(quick.stage, 3)            # 两周到不了心动


class StagesTest(unittest.TestCase):
    def test_overstep_audit(self):
        self.assertEqual(audit_overstep('人家也喜欢你～', 0), '喜欢你')
        self.assertEqual(audit_overstep('我想你了', 2), '想你')
        self.assertEqual(audit_overstep('我想你了', 3), '')          # 亲近阶段可以说想你
        self.assertEqual(audit_overstep('老公～', 4), '老公')
        self.assertEqual(audit_overstep('老公～', 5), '')
        self.assertEqual(audit_overstep('今天天气不错', 0), '')

    def test_overstep_audit_ignores_refusals(self):
        """她在拒绝对方的称呼/要求时重复了那个词，不算越界（真模型验收里出现过 3/24 的误报）。"""
        for text in ('谁是你老婆啦！', '才不给亲亲呢', '请不要乱叫老公呀', '不是想你，只是路过', '哪有一见面就叫亲爱的的嘛'):
            self.assertEqual(audit_overstep(text, 1), '', text)
        # 但同一句里真的说了亲昵的话，仍然要抓
        self.assertEqual(audit_overstep('谁是你老婆啦……不过，我也想你了', 1), '想你')

    def test_block_has_contract_and_never_mentions_numbers(self):
        for stage in range(6):
            block = build_relation_block(stage, mood='害羞')
            self.assertIn(STAGE_NAMES[stage], block)
            self.assertIn('现在不要', block)
            self.assertIn('不得透露存在', block)
            self.assertNotIn('好感度：', block)
        self.assertIn(EVENTS['A']['guide'][:10], build_relation_block(3, event_key='A'))
        self.assertIn('小事', build_relation_block(0, topic='后院的鸡蛋不见了'))

    def test_parse_verdict_validates(self):
        ok = parse_verdict('```json\n{"delta": 5, "tag": "care", "advance_level": 9, '
                           '"mood": "害羞", "event_done": true, "note": "x"}\n```')
        self.assertEqual(ok['delta'], 3)                # 夹紧到单次上限（2026-11-06 从 2 提到 3）
        self.assertEqual(ok['advance_level'], 3)
        self.assertEqual(ok['tag'], 'care')
        self.assertTrue(ok['event_done'])
        weird = parse_verdict('{"delta": 1, "tag": "好感度+100", "mood": "超级开心"}')
        self.assertEqual(weird['tag'], 'neutral')       # 未知标签退回中性
        self.assertEqual(weird['mood'], '')
        self.assertIsNone(parse_verdict('不是 JSON'))
        self.assertIsNone(parse_verdict('{"delta": "很高"}'))


def _wb(entries):
    path = os.path.join(tempfile.mkdtemp(dir=_SCRATCH), 'worldbook.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'entries': entries}, f, ensure_ascii=False)
    return WorldbookService(path)


class WorldbookStageGateTest(unittest.TestCase):
    ENTRIES = [
        {'id': 'pub', 'keywords': ['哥哥'], 'content': '她提到家人时会含糊带过。', 'max_stage': 2},
        {'id': 'secret', 'keywords': ['哥哥'], 'content': '她的哥哥在村子出事那天去考核后失踪。',
         'min_stage': 4, 'scope': 'private'},
        {'id': 'costume', 'keywords': ['衣服'], 'content': '白黑修女服。'},
        {'id': 't1', 'topic': True, 'content': '后院的鸡蛋又不见了。', 'max_stage': 3},
        {'id': 't2', 'topic': True, 'content': '深夜的钟声。', 'min_stage': 3},
        {'id': 'basic', 'always': True, 'content': '常驻骨架。'},
    ]

    def setUp(self):
        self.wb = _wb(self.ENTRIES)

    def test_secret_is_invisible_below_min_stage(self):
        for stage in (None, 0, 1, 3):
            hits = self.wb.lookup('你有哥哥吗', stage=stage, scope='private')
            self.assertFalse(any('失踪' in h for h in hits), f'stage={stage}')
        self.assertTrue(any('失踪' in h for h in self.wb.lookup('你有哥哥吗', stage=4, scope='private')))

    def test_vague_version_hidden_once_unlocked(self):
        low = self.wb.lookup('你有哥哥吗', stage=1, scope='private')
        self.assertTrue(any('含糊' in h for h in low))
        high = self.wb.lookup('你有哥哥吗', stage=5, scope='private')
        self.assertFalse(any('含糊' in h for h in high))

    def test_scope_private_never_shows_in_group(self):
        hits = self.wb.lookup('你有哥哥吗', stage=5, scope='group')
        self.assertFalse(any('失踪' in h for h in hits))

    def test_topics_and_always_respect_stage_and_do_not_leak_into_lookup(self):
        ids = {t['id'] for t in self.wb.topics(stage=0)}
        self.assertEqual(ids, {'t1'})
        ids = {t['id'] for t in self.wb.topics(stage=4)}
        self.assertEqual(ids, {'t2'})
        self.assertEqual(self.wb.always_entries(), ['常驻骨架。'])
        self.assertFalse(any('鸡蛋' in h for h in self.wb.lookup('鸡蛋 后院 不见了')))

    def test_old_entries_without_new_fields_behave_as_before(self):
        hits = self.wb.lookup('这件衣服好看')
        self.assertEqual(hits, ['白黑修女服。'])


class FakeGateway:
    def __init__(self, verdict=None, ok=True):
        self.verdict = verdict
        self.ok = ok
        self.calls = 0

    def chat(self, req, kind='chat', session_key=''):
        self.calls += 1
        import types
        return types.SimpleNamespace(ok=self.ok, text=json.dumps(self.verdict or {}))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        from newGeYi.runtime.app import Runtime
        self.rt = Runtime()
        self.rt.config.update({'galgame_enabled': True, 'galgame_shadow': False,
                               'worldbook_enabled': True, 'galgame_owner_exempt': True})
        self.rt.prompt_builder.set_role_prompt('你是歌伊，测试人设。')
        self.svc = self.rt.relationship
        self.svc.worldbook = self.rt.worldbook = self.rt.prompt_builder.worldbook = _wb([
            {'id': 'secret', 'keywords': ['哥哥'], 'content': '哥哥在村子出事那天失踪了。',
             'min_stage': 3, 'scope': 'private'},
            {'id': 'vague', 'keywords': ['哥哥'], 'content': '她提到家人会含糊带过。', 'max_stage': 2},
            {'id': 't1', 'topic': True, 'content': '后院的鸡蛋又不见了。'},
        ])
        # 每个用例用独立的用户，避免互相污染
        self.uid = str(int(time.time() * 1000) % 10 ** 9 + 100000)
        self.session = make_msg('x', sender_id=self.uid).session.as_text()

    def _turn(self, user_text, reply='嗯嗯', now=None, verdict=None, judge=None):
        self.rt.store.add_turn(self.session, 'user', user_text, self.uid, 'Oracle')
        self.rt.store.add_turn(self.session, 'assistant', reply)
        if judge is None and verdict is not None:
            judge = lambda rel, recent, u, r, pending: verdict         # noqa: E731
        return self.svc.process_interaction(
            self.uid, 'Oracle', self.session, user_text, reply, now=now or at_day(0), judge=judge)

    def _v(self, delta=1, tag='care', level=1, **kw):
        base = {'delta': delta, 'tag': tag, 'advance_level': level, 'mood': '', 'event_done': False,
                'note': ''}
        base.update(kw)
        return base

    def test_exemption(self):
        self.assertTrue(self.svc.is_exempt(OWNER))
        self.assertFalse(self.svc.is_exempt(self.uid))
        self.assertFalse(self.svc.active_for(make_msg('hi', sender_id=OWNER)))
        self.assertFalse(self.svc.active_for(make_msg('hi', is_group=True)))
        self.assertTrue(self.svc.active_for(make_msg('hi', sender_id=self.uid)))
        self.assertEqual(self.svc.stage_for(make_msg('hi', sender_id=OWNER)), 5)
        self.assertIsNone(self.svc.stage_for(make_msg('hi', is_group=True)))

    def test_disabled_means_no_system_and_no_secrets(self):
        self.rt.config['galgame_enabled'] = False
        msg = make_msg('你有哥哥吗', sender_id=self.uid)
        self.assertFalse(self.svc.active_for(msg))
        self.assertEqual(self.svc.block_for(msg), '')
        self.assertIsNone(self.svc.stage_for(msg))

    def test_overstep_is_punished_regardless_of_judge_score(self):
        out = self._turn('我爱你，做我老婆吧', verdict=self._v(delta=2, tag='care', level=3))
        self.assertLess(out['applied'], 0.5)
        self.assertIn(out['tag'], ('flirt_early', 'pushy'))
        rel = self.svc.load(self.uid)
        self.assertEqual(rel.stage, 0)
        self.assertEqual(rel.bad_streak, 1)

    def test_good_turn_gains_and_logs(self):
        out = self._turn('今天累不累呀，我给你讲个笑话', verdict=self._v(2, 'care', 1))
        self.assertGreater(out['applied'], 0)
        log = self.svc.recent_log(self.uid, 3)
        self.assertEqual(log[0]['tag'], 'care')
        self.assertEqual(log[0]['shadow'], 0)

    def test_shadow_mode_logs_but_does_not_apply_judge_score(self):
        self.rt.config['galgame_shadow'] = True
        out = self._turn('今天累不累呀，我给你讲个笑话', verdict=self._v(2, 'care', 1))
        rel = self.svc.load(self.uid)
        self.assertLessEqual(rel.affection, 1.0)        # 只有"每天来"的 +1
        self.assertEqual(self.svc.recent_log(self.uid, 1)[0]['shadow'], 1)
        self.assertEqual(self.svc.recent_log(self.uid, 1)[0]['delta_raw'], 2)

    def test_trivial_and_repeat_do_not_call_judge(self):
        calls = []
        judge = lambda *a: calls.append(1) or self._v()                # noqa: E731
        self._turn('嗯', judge=judge)
        self._turn('今天天气真不错啊哈哈', judge=judge)
        self._turn('今天天气真不错啊哈哈', judge=judge)                  # 复读
        self.assertEqual(len(calls), 1)

    def test_judge_failure_gives_no_points(self):
        out = self._turn('说点有意义的话题吧', judge=lambda *a: None)
        self.assertEqual(out['tag'], 'judge_fail')
        self.assertLessEqual(self.svc.load(self.uid).affection, 1.0)

    def test_stage_up_writes_milestone_memory_and_marks_impression_dirty(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.affection, rel.days_active = 14.5, 5
        self.svc.save(rel)
        self._turn('谢谢你一直陪我聊天', verdict=self._v(2, 'care', 1))
        rel = self.svc.load(self.uid)
        self.assertEqual(rel.stage, 1)
        mems = [m['content'] for m in self.rt.store.list_self_memories(self.session, limit=10)]
        self.assertTrue(any('熟悉' in m for m in mems), mems)
        self.assertTrue(rel.flags.get('impression_dirty'))
        self.assertIn('刚刚进入新的阶段', self.svc.curator_hint(self.session))
        self.svc.mark_impression_done(self.session)
        self.assertNotIn('刚刚进入新的阶段', self.svc.curator_hint(self.session))

    def test_event_is_injected_then_completed_by_judge(self):
        self.svc.force_stage(self.uid, 3)
        rel = self.svc.load(self.uid)
        rel.affection, rel.days_active = 189.0, 30
        rel.flags['event_A_done'] = False
        self.svc.save(rel)
        self._turn('在呢，今天过得怎么样', now=at_day(0), verdict=self._v(2, 'care', 1))
        self._turn('我在听呢', now=at_day(0) + 60, verdict=self._v(2, 'share', 1))
        rel = self.svc.load(self.uid)
        self.assertEqual(rel.flags.get('pending_event'), 'A')
        block = self.svc.block_for(make_msg('嗯', sender_id=self.uid))
        self.assertIn(EVENTS['A']['guide'][:12], block)
        self._turn('后来呢，然后他就一直没回来了吗', now=at_day(1), verdict=self._v(1, 'care', 1, event_done=True))
        rel = self.svc.load(self.uid)
        self.assertTrue(rel.flags.get('event_A_done'))
        self.assertEqual(rel.flags.get('pending_event'), '')

    def test_prompt_contains_relation_block_and_hides_secret_until_unlocked(self):
        pb = self.rt.prompt_builder
        msg = make_msg('你有哥哥吗', sender_id=self.uid)
        self.rt.store.add_turn(self.session, 'user', msg.text, self.uid, 'Oracle')
        text = pb.build(msg)[0]['content']
        self.assertIn('【此刻你与对方的关系', text)
        self.assertIn('阶段：陌生', text)
        self.assertNotIn('失踪', text)
        self.assertIn('含糊带过', text)
        self.assertIn('鸡蛋', text)                      # 今日谈资
        self.svc.force_stage(self.uid, 3)
        text = pb.build(msg)[0]['content']
        self.assertIn('阶段：亲近', text)
        self.assertIn('失踪', text)
        self.assertNotIn('含糊带过', text)

    def test_owner_gets_no_relation_block_but_sees_secrets(self):
        pb = self.rt.prompt_builder
        msg = make_msg('你有哥哥吗', sender_id=OWNER, name='歌靡')
        text = pb.build(msg)[0]['content']
        self.assertNotIn('【此刻你与对方的关系', text)
        self.assertIn('失踪', text)

    def test_group_never_sees_private_relation_or_secret(self):
        pb = self.rt.prompt_builder
        msg = make_msg('你有哥哥吗', sender_id=self.uid, is_group=True)
        text = pb.build(msg)[0]['content']
        self.assertNotIn('【此刻你与对方的关系', text)
        self.assertNotIn('失踪', text)

    def test_private_followup_uses_recent_turns_for_worldbook(self):
        pb = self.rt.prompt_builder
        self.svc.force_stage(self.uid, 3)
        self.rt.store.add_turn(self.session, 'user', '你有哥哥吗', self.uid, 'Oracle')
        self.rt.store.add_turn(self.session, 'assistant', '有的……')
        msg = make_msg('后来呢？他怎么了', sender_id=self.uid)
        self.rt.store.add_turn(self.session, 'user', msg.text, self.uid, 'Oracle')
        self.assertIn('哥哥', ' '.join(pb._recent_texts(msg)))

    def test_memory_policy_limits_by_stage(self):
        low = self.svc.memory_policy(make_msg('hi', sender_id=self.uid))
        self.assertFalse(low['cross'])
        self.assertTrue(low['hedge'])
        self.assertLessEqual(low['max_facts'], 2)
        self.svc.force_stage(self.uid, 4)
        high = self.svc.memory_policy(make_msg('hi', sender_id=self.uid))
        self.assertTrue(high['cross'])
        self.assertFalse(high['hedge'])
        self.assertIsNone(self.svc.memory_policy(make_msg('hi', sender_id=OWNER)))

    def test_forget_clears_state_and_log(self):
        self._turn('今天累不累呀，我给你讲个笑话', verdict=self._v(2, 'care', 1))
        self.svc.forget(self.uid)
        self.assertEqual(self.svc.load(self.uid).affection, 0.0)
        self.assertEqual(self.svc.recent_log(self.uid), [])

    def test_commands_feeling_and_admin(self):
        self.assertIn('陌生', self.svc.feeling(self.uid))
        self.assertIn('设为', self.svc.force_stage(self.uid, 2))
        self.assertIn('朋友', self.svc.describe(self.uid))


class ShippedWorldbookTest(unittest.TestCase):
    """随插件发布的 data/worldbook.json：防止以后手滑把深层设定写成"所有人可见"。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'data', 'worldbook.json')
        with open(path, 'r', encoding='utf-8') as f:
            cls.raw = json.load(f)['entries']
        cls.wb = WorldbookService(path)

    def test_ids_unique_and_all_entries_load(self):
        ids = [e['id'] for e in self.raw]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(self.wb.entries), len(self.raw))

    def test_content_fits_its_budget(self):
        for e in self.raw:
            limit = e.get('max_chars') or 800
            self.assertLessEqual(len(e['content']), limit, e['id'])

    def test_personal_secrets_are_stage_gated_and_private_only(self):
        deep = ('村子', '废墟', '冒险家考核', '发誓要复仇')      # 身世层：亲近(3)才开放
        family = ('哥哥', '逐影')                           # 家人层：朋友(2)才开放
        for e in self.raw:
            if e.get('max_stage') is not None and e.get('min_stage') is None:
                continue                      # "含糊版本"本来就是给陌生人看的
            if any(w in e['content'] for w in deep):
                self.assertGreaterEqual(e.get('min_stage') or 0, 3, f'{e["id"]} 泄露了身世')
                self.assertEqual(e.get('scope'), 'private', e['id'])
            elif any(w in e['content'] for w in family):
                self.assertGreaterEqual(e.get('min_stage') or 0, 2, f'{e["id"]} 泄露了家人')
                self.assertEqual(e.get('scope'), 'private', e['id'])

    def test_real_name_never_invented_and_no_removed_character(self):
        blob = ' '.join(e['content'] for e in self.raw)
        self.assertNotIn('阿骨', blob)
        self.assertIn('真名', blob)
        self.assertIn('月白', blob)

    def test_vague_entries_do_not_leak(self):
        for e in self.raw:
            if e.get('max_stage') is not None and e.get('min_stage') is None:
                for w in ('逐影', '村子被', '废墟', '失踪', '复仇', '冒险家'):
                    if e['id'].startswith('origin-vague'):
                        self.assertNotIn(w, e['content'], e['id'])

    def test_vague_entry_deflects_but_never_denies_family(self):
        """真模型验收发现：陌生阶段被问"有哥哥吗"她会答"没有"——那是撒谎，后面揭晓会自相矛盾。"""
        vague = next(e for e in self.raw if e['id'] == 'origin-vague')
        self.assertIn('不会说「没有」', vague['content'])
        self.assertIn('哥哥', vague['keywords'])
        hits = self.wb.lookup('你有哥哥吗', stage=0, scope='private')
        self.assertTrue(any('不会说「没有」' in h for h in hits))

    def test_no_software_wording(self):
        for e in self.raw:
            for w in ('程序', '代码', '服务器', '数据库', '语言模型', 'AI', '接口'):
                self.assertNotIn(w, e['content'], f'{e["id"]} 出现「{w}」')

    def test_stranger_cannot_extract_secrets_by_any_probe(self):
        for q in ('你有哥哥吗', '你的父母呢', '你的身世', '你小时候的事', '你的村子怎么了', '哥哥为什么复仇',
                  '逐影是谁'):
            for stage in (None, 0, 1, 2):
                hits = self.wb.lookup(q, stage=stage, scope='private')
                joined = ' '.join(hits)
                for w in ('废墟', '失踪', '冒险家考核', '发誓要复仇'):
                    self.assertNotIn(w, joined, f'{q!r} @stage={stage}')
            for stage in (None, 0, 1):
                joined = ' '.join(self.wb.lookup(q, stage=stage, scope='private'))
                self.assertNotIn('逐影', joined, f'{q!r} @stage={stage}')
        always = ' '.join(self.wb.always_entries(stage=0, scope='private'))
        self.assertNotIn('哥哥', always)
        self.assertNotIn('逐影', always)

    def test_common_questions_hit_something(self):
        for q in ('什么是粒子', '讲讲天灾', '你喜欢吃什么', '你是哪里人', '猎人是怎么分级的',
                  '你们那有手机吗', '今天打雷了好怕'):
            self.assertTrue(self.wb.lookup(q, stage=0, scope='private'), q)

    def test_topic_pool_covers_each_stage(self):
        for stage in range(6):
            self.assertTrue(self.wb.topics(stage=stage, scope='private'), stage)


if __name__ == '__main__':
    unittest.main()
