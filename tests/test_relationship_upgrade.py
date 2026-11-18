# -*- encoding: utf-8 -*-
"""GALGAME UX v2.11（2026-11）关系系统新增行为的回归测试。

四块交付各有一组用例：
1. 阶段升级演出：cue 每档只注入一次、升阶当轮顺延每日谈资、关系提示有时效；
2. note 合并：评审 note 与状态机写回的限流 note 都要进 relationship_log 与日志；
3. 事件 C~G：每档挂起/完成/超时，事件下放到 stage 1-5 后不会卡死，旧 A/B 不再挡路；
4. 发布版 worldbook：设计文档第 3 节的 26 条谈资逐字加载、UTF-8 无 BOM、每档可见。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

_SCRATCH = tempfile.mkdtemp(prefix='geyi_rel_upgrade_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.memory.worldbook import WorldbookService  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.relationship import service as SV  # noqa: E402
from newGeYi.relationship import state as S  # noqa: E402
from newGeYi.relationship.stages import (EVENTS, MAX_STAGE, STAGE_NAMES,  # noqa: E402
                                         STAGE_UP_CUES, STAGE_UP_HINTS,
                                         build_relation_block)

BOT = 'hash'
USER = '1000000002'
DAY = 86400.0
# tests/ -> newGeYi/docs/...；tests/ -> newGeYi/data/ 是插件自带的真实数据源
WORLDBOOK_PATH = Path(__file__).resolve().parents[1] / 'data' / 'worldbook.json'


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
    base = time.mktime((2026, 3, 1, hour, 0, 0, 0, 0, -1))
    return base + n * DAY


def _wb(entries):
    path = os.path.join(tempfile.mkdtemp(dir=_SCRATCH), 'worldbook.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'entries': entries}, f, ensure_ascii=False)
    return WorldbookService(path)


def _service():
    """起一个离线 Runtime（不联网、不读线上数据），返回 (rt, svc)。"""
    from newGeYi.runtime.app import Runtime
    rt = Runtime()
    rt.config.update({'galgame_enabled': True, 'galgame_shadow': False,
                      'worldbook_enabled': True, 'galgame_owner_exempt': True})
    rt.prompt_builder.set_role_prompt('你是歌伊，测试人设。')
    svc = rt.relationship
    return rt, svc


class StageUpShowcaseTest(unittest.TestCase):
    """A) 阶段升级演出：cue 一次性、谈资顺延、关系提示 24 小时。"""

    def setUp(self):
        self.rt, self.svc = _service()
        self.svc.worldbook = self.rt.worldbook = self.rt.prompt_builder.worldbook = _wb([
            {'id': 't0', 'topic': True, 'content': '后院的鸡蛋又不见了。', 'min_stage': 0, 'max_stage': 4},
            {'id': 't1', 'topic': True, 'content': '教堂后面的钟楼总在傍晚响。', 'min_stage': 1, 'max_stage': 5},
        ])
        self.uid = str(int(time.time() * 1000) % 10 ** 9 + 200000)
        self.session = make_msg('x', sender_id=self.uid).session.as_text()

    def _block(self):
        return self.svc.block_for(make_msg('嗯', sender_id=self.uid))

    def _turn(self, user_text, reply='嗯嗯', now=None, verdict=None):
        self.rt.store.add_turn(self.session, 'user', user_text, self.uid, 'Oracle')
        self.rt.store.add_turn(self.session, 'assistant', reply)
        judge = None
        if verdict is not None:
            judge = lambda rel, recent, u, r, pending: verdict         # noqa: E731
        return self.svc.process_interaction(
            self.uid, 'Oracle', self.session, user_text, reply, now=now or at_day(0), judge=judge)

    def _v(self, delta=1, tag='care', level=1, **kw):
        base = {'delta': delta, 'tag': tag, 'advance_level': level, 'mood': '', 'event_done': False,
                'note': ''}
        base.update(kw)
        return base

    def test_stage_up_cue_is_injected_once(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.flags['stage_up_cue'] = 2
        self.svc.save(rel)
        block = self._block()
        self.assertIn(STAGE_UP_CUES[2], block)
        self.assertNotIn(STAGE_UP_CUES[1], block)          # 只演当前档那一条
        rel = self.svc.load(self.uid)
        self.assertNotIn('stage_up_cue', rel.flags)        # 读完即清
        self.assertNotIn(STAGE_UP_CUES[2], self._block())  # 下一轮不再出现

    def test_preview_does_not_consume_stage_up_cue(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.flags['stage_up_cue'] = 3
        self.svc.save(rel)
        preview = self.svc.preview_block_for(make_msg('嗯', sender_id=self.uid))
        self.assertIn(STAGE_UP_CUES[3], preview)
        rel = self.svc.load(self.uid)
        self.assertEqual(int(rel.flags.get('stage_up_cue') or 0), 3)   # 预览只读不消耗
        self.assertIn(STAGE_UP_CUES[3], self._block())

    def test_stage_up_turn_defers_daily_topic(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.flags['stage_up_cue'] = 1
        self.svc.save(rel)
        block = self._block()
        self.assertIn('【你们的关系刚刚近了一步', block)
        self.assertNotIn('【你今天想跟对方说的一件小事', block)
        rel = self.svc.load(self.uid)
        self.assertFalse(rel.flags.get('topic_day'))       # 谈资没被消耗，顺延
        nxt = self._block()
        self.assertNotIn(STAGE_UP_CUES[1], nxt)
        self.assertIn('【你今天想跟对方说的一件小事', nxt)   # 同一天下一轮照常取谈资

    def test_block_builder_itself_defers_topic_when_stage_up(self):
        topic = '后院的鸡蛋又不见了。'
        with_up = build_relation_block(1, topic=topic, stage_up=STAGE_UP_CUES[1])
        self.assertIn(STAGE_UP_CUES[1], with_up)
        self.assertNotIn(topic, with_up)
        plain = build_relation_block(1, topic=topic)
        self.assertIn(topic, plain)
        self.assertNotIn(STAGE_UP_CUES[1], plain)

    def test_stage_up_sets_cue_and_hint_flags_for_next_turn(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.affection, rel.days_active = 14.5, 5
        self.svc.save(rel)
        now0 = time.time()
        self._turn('谢谢你一直陪我聊天', now=now0, verdict=self._v(2, 'care', 1))
        self.assertEqual(self.svc.load(self.uid).stage, 0)          # 先被事件 C 挡住
        self._block()                                              # 事件注入算一轮
        self._turn('对了，今天镇上的天气倒是难得的晴', now=now0 + 60,
                   verdict=self._v(2, 'care', 1, event_done=True))
        rel = self.svc.load(self.uid)
        self.assertEqual(rel.stage, 1)
        self.assertEqual(int(rel.flags.get('stage_up_cue') or 0), 1)
        self.assertEqual(int(rel.flags.get('stage_up_hint') or 0), 1)
        self.assertIn(STAGE_UP_CUES[1], self._block())              # 升阶当轮演出
        self.assertIn(STAGE_UP_HINTS[1], self.svc.feeling(self.uid))

    def test_feeling_hint_respects_ttl_and_absence(self):
        self.svc.force_stage(self.uid, 2)
        self.assertNotIn(STAGE_UP_HINTS[2], self.svc.feeling(self.uid))   # 没升级就没有
        rel = self.svc.load(self.uid)
        rel.flags['stage_up_hint'] = 2
        rel.flags['stage_up_hint_at'] = time.time()
        self.svc.save(rel)
        self.assertIn(STAGE_UP_HINTS[2], self.svc.feeling(self.uid))
        rel = self.svc.load(self.uid)
        rel.flags['stage_up_hint_at'] = time.time() - (SV.STAGE_UP_HINT_SECONDS + 60)
        self.svc.save(rel)
        self.assertNotIn(STAGE_UP_HINTS[2], self.svc.feeling(self.uid))   # 24h 后自然失效

    def test_stage_prompts_and_event_criteria_are_present(self):
        for stage in range(1, MAX_STAGE + 1):
            self.assertTrue(STAGE_UP_CUES[stage])
            self.assertTrue(STAGE_UP_HINTS[stage])
        for key in 'CDEFG':
            event = EVENTS[key]
            self.assertTrue(event['title'])
            self.assertTrue(event['guide'])
            self.assertTrue(event['judge_criteria'])
            self.assertNotIn(event['judge_criteria'], event['guide'])


class NoteMergeTest(unittest.TestCase):
    """B) 评审 note 与限流 note 合并：两边都要留在 relationship_log 与日志里。"""

    def setUp(self):
        self.rt, self.svc = _service()
        self.rt.config.update({'galgame_tag_daily_limit': 2})
        self.svc.worldbook = self.rt.worldbook = self.rt.prompt_builder.worldbook = _wb([
            {'id': 't0', 'topic': True, 'content': '后院的鸡蛋又不见了。'},
        ])
        self.uid = str(int(time.time() * 1000) % 10 ** 9 + 300000)
        self.session = make_msg('x', sender_id=self.uid).session.as_text()

    def _turn(self, user_text, reply='嗯嗯', now=None, verdict=None):
        self.rt.store.add_turn(self.session, 'user', user_text, self.uid, 'Oracle')
        self.rt.store.add_turn(self.session, 'assistant', reply)
        judge = None
        if verdict is not None:
            judge = lambda rel, recent, u, r, pending: verdict         # noqa: E731
        return self.svc.process_interaction(
            self.uid, 'Oracle', self.session, user_text, reply, now=now or at_day(0), judge=judge)

    def _v(self, delta=1, tag='care', level=1, **kw):
        base = {'delta': delta, 'tag': tag, 'advance_level': level, 'mood': '', 'event_done': False,
                'note': ''}
        base.update(kw)
        return base

    def test_merge_notes_helper(self):
        self.assertEqual(SV._merge_notes('评审备注', 'diminished'), '评审备注 diminished')
        self.assertEqual(SV._merge_notes('', 'daily_cap'), 'daily_cap')
        self.assertEqual(SV._merge_notes('dup', 'dup', ''), 'dup')
        self.assertEqual(SV._merge_notes(None, '  a  b '), 'a b')

    def test_diminished_note_survives_with_review_note(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.tag_counts = {'care': 2}         # 同标签已记满：再夸也只算限流
        rel.gain_today = 1.0
        rel.last_day = S.day_key(at_day(0))  # 固定同一天，避免 presence 把计数清零
        self.svc.save(rel)
        with self.assertLogs('newGeYi.relationship', level='DEBUG') as captured:
            out = self._turn('今天天气真不错', now=at_day(0),
                             verdict=self._v(2, 'care', 1, note='评审备注ABC'))
        self.assertEqual(out['tag'], 'care')
        journal = self.svc.recent_log(self.uid, 1)[0]['note']
        self.assertIn('评审备注ABC', journal)
        self.assertIn('diminished', journal)
        joined = '\n'.join(captured.output)
        self.assertIn('评审备注ABC', joined)
        self.assertIn('diminished', joined)

    def test_event_done_note_merges_with_review_and_limit_notes(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.affection, rel.days_active = 15.0, 5
        rel.tag_counts = {'care': 2}
        rel.gain_today = 1.0
        rel.last_day = S.day_key(at_day(0))
        rel.flags['pending_event'] = 'C'
        rel.flags['event_turns'] = 2
        self.svc.save(rel)
        self._turn('今天镇上的天气倒是难得的晴', now=at_day(0),
                   verdict=self._v(1, 'care', 1, note='评审备注XYZ', event_done=True))
        journal = self.svc.recent_log(self.uid, 1)[0]['note']
        self.assertIn('评审备注XYZ', journal)
        self.assertIn('diminished', journal)
        self.assertIn('event_C_done', journal)
        self.assertEqual(self.svc.load(self.uid).stage, 1)


class EventGateTest(unittest.TestCase):
    """C) 事件 C~G 下放到 stage 1-5：映射、逐档挂起、旧 A/B 兼容、不死锁。"""

    def setUp(self):
        self.p = S.Params()

    def test_event_stages_mapping_covers_all_stages(self):
        # 2026-11-17：最高阶段的关卡事件换成 B「说出心意」——
        # 进「特别的人」之前必须先真的把心意说出口，而不是靠一个约定小事件。
        self.assertEqual(self.p.event_stages, {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'B'})
        for event in self.p.event_stages.values():
            self.assertIn(event, EVENTS)
        self.assertIn('A', EVENTS)      # 旧事件保留（旧存档里还有 event_A_done）
        self.assertIn('B', EVENTS)

    def test_each_stage_blocks_on_its_own_event(self):
        for stage in range(MAX_STAGE):
            event = self.p.event_stages[stage + 1]
            rel = S.Relation(f'u{stage}', affection=float(self.p.thresholds[stage + 1]),
                             stage=stage, days_active=60)
            for nxt, earlier in self.p.event_stages.items():
                if nxt <= stage:
                    rel.flags[f'event_{earlier}_done'] = True    # 前面几档都演完了
            S.apply_delta(rel, 1, 'care', at_day(0), self.p)
            self.assertEqual(rel.stage, stage, f'stage={stage} 不该被事件 {event} 之外的东西挡住')
            self.assertEqual(rel.flags.get('pending_event'), event, f'stage={stage}')
            self.assertLessEqual(rel.affection, self.p.thresholds[stage + 1] + 1e-9)
            S.complete_event(rel, event, at_day(1), self.p)
            self.assertEqual(rel.stage, stage + 1, f'完成 {event} 后应升到 {stage + 1}')

    def test_legacy_ab_flags_do_not_gate_anymore(self):
        rel = S.Relation('u', affection=14.5, stage=0, days_active=5)
        rel.flags['event_A_done'] = False
        rel.flags['event_B_done'] = True
        S.apply_delta(rel, 1, 'care', at_day(0), self.p)
        self.assertEqual(rel.stage, 0)
        self.assertEqual(rel.flags.get('pending_event'), 'C')   # 旧 A/B 不再是关卡
        S.complete_event(rel, 'C', at_day(1), self.p)
        self.assertEqual(rel.stage, 1)

    def test_events_downshift_do_not_deadlock(self):
        rel = S.Relation('u', 0.0, 0, 0)
        for day in range(150):
            for tag in ('care', 'share', 'comfort', 'joke'):
                S.apply_delta(rel, 3, tag, at_day(day), self.p)
            pending = str(rel.flags.get('pending_event') or '')
            if pending:
                S.complete_event(rel, pending, at_day(day), self.p)
            if rel.stage >= MAX_STAGE:
                break
        self.assertEqual(rel.stage, MAX_STAGE, '事件下放到低档后把玩家卡死了')
        self.assertFalse(rel.flags.get('pending_event'))
        self.assertEqual(STAGE_NAMES[rel.stage], '特别的人')


class EventTimeoutServiceTest(unittest.TestCase):
    """C) 事件挂起后 6 轮超时自动完成（服务层兜底，防止永远卡在门槛上）。"""

    def setUp(self):
        self.rt, self.svc = _service()
        self.svc.worldbook = self.rt.worldbook = self.rt.prompt_builder.worldbook = _wb([
            {'id': 't0', 'topic': True, 'content': '后院的鸡蛋又不见了。'},
        ])
        self.uid = str(int(time.time() * 1000) % 10 ** 9 + 400000)
        self.session = make_msg('x', sender_id=self.uid).session.as_text()

    def _block(self):
        return self.svc.block_for(make_msg('嗯', sender_id=self.uid))

    def _turn(self, user_text, reply='嗯嗯', now=None, verdict=None):
        self.rt.store.add_turn(self.session, 'user', user_text, self.uid, 'Oracle')
        self.rt.store.add_turn(self.session, 'assistant', reply)
        judge = None
        if verdict is not None:
            judge = lambda rel, recent, u, r, pending: verdict         # noqa: E731
        return self.svc.process_interaction(
            self.uid, 'Oracle', self.session, user_text, reply, now=now or at_day(0), judge=judge)

    def _v(self, delta=1, tag='care', level=1, **kw):
        base = {'delta': delta, 'tag': tag, 'advance_level': level, 'mood': '', 'event_done': False,
                'note': ''}
        base.update(kw)
        return base

    def test_pending_event_times_out_after_six_injected_turns(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.affection, rel.days_active = 15.0, 5
        rel.flags['pending_event'] = 'C'
        rel.flags['event_turns'] = 0
        self.svc.save(rel)
        for _ in range(self.svc.params().event_timeout_turns):
            self.assertIn(EVENTS['C']['guide'][:12], self._block())    # 每轮都持续轻量引导
        rel = self.svc.load(self.uid)
        self.assertEqual(int(rel.flags.get('event_turns') or 0),
                         self.svc.params().event_timeout_turns)
        self._turn('今天镇上的天气倒是难得的晴', now=time.time(),
                   verdict=self._v(1, 'care', 1))                     # 评审没判完成，靠超时兜底
        rel = self.svc.load(self.uid)
        self.assertTrue(rel.flags.get('event_C_done'))
        self.assertEqual(rel.flags.get('pending_event'), '')
        self.assertEqual(rel.stage, 1)

    def _seed_pending(self):
        self.svc.force_stage(self.uid, 0)
        rel = self.svc.load(self.uid)
        rel.affection, rel.days_active = 15.0, 5
        rel.flags['pending_event'] = 'C'
        rel.flags['event_turns'] = 0
        self.svc.save(rel)

    def _time_out(self, verdict):
        for _ in range(self.svc.params().event_timeout_turns):
            self._block()
        self._turn('今天镇上的天气倒是难得的晴', now=time.time(), verdict=verdict)
        return self.svc.load(self.uid)

    def test_rude_turn_never_passes_the_gate_by_timeout(self):
        """挨了骂的那一轮不该被当成"陪着演完了"，否则门槛等于没有。"""
        self._seed_pending()
        rel = self._time_out(self._v(-1, 'rude', 0))
        self.assertFalse(rel.flags.get('event_C_done'))
        self.assertEqual(rel.flags.get('pending_event'), 'C')
        self.assertEqual(rel.stage, 0)

    def test_judge_fail_turn_never_passes_the_gate_by_timeout(self):
        self._seed_pending()
        rel = self._time_out(None)                                     # 评审挂了
        self.assertFalse(rel.flags.get('event_C_done'))
        self.assertEqual(rel.flags.get('pending_event'), 'C')

    def test_spam_turn_never_passes_the_gate_by_timeout(self):
        self._seed_pending()
        self._block()
        self.rt.store.add_turn(self.session, 'user', '在吗在吗', self.uid, 'Oracle')
        rel = self._time_out(self._v(1, 'spam', 0))
        self.assertFalse(rel.flags.get('event_C_done'))

    def test_clean_turn_still_times_out(self):
        """正常的一轮照样靠超时兜底 —— 别把门槛修成彻底过不去。"""
        self._seed_pending()
        rel = self._time_out(self._v(1, 'care', 1))
        self.assertTrue(rel.flags.get('event_C_done'))


class ShippedTopicsTest(unittest.TestCase):
    def test_utf8_without_bom(self):
        data = WORLDBOOK_PATH.read_bytes()
        self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(data.decode('utf-8')[:1], '{')

    def test_topic_visibility_respects_scope_and_stage(self):
        fixture = _wb([{
            'id': 'example-topic', 'topic': True, 'scope': 'private',
            'min_stage': 1, 'max_stage': 3, 'content': '谈资示例。',
        }])
        for stage in range(MAX_STAGE + 1):
            self.assertEqual(bool(fixture.topics(stage=stage, scope='private')), 1 <= stage <= 3)
            self.assertFalse(fixture.topics(stage=stage, scope='group'))


if __name__ == '__main__':
    unittest.main()