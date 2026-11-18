# -*- encoding: utf-8 -*-
"""私聊主动搭话（PRD v2.11 §5）：开关、熔断、静默、安静时段、记忆挂钩。

全部离线：没有网络、没有真实 API、没有真实 SQLite（用内存 FakeStore）。
"""

import os
import sys
import tempfile
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_private_active_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from newGeYi.routing.private_active import (  # noqa: E402
    KEY_BUSY, KEY_COLD, KEY_DAY, KEY_HOOK, KEY_SEEN, KEY_SEND, TEMPLATES,
    PrivateActivePlanner, PrivateActiveService,
)

SESSION = '00000000000000000000000000000000:qq:onebot:private:-:1000000002'
UID = '1000000002'
HOUR = 3600.0
DAY = 86400.0
# 固定「本地时间中午 12 点」，避开默认安静时段（23:00–08:00），让判定与机器时区无关。
BASE = __import__('time').mktime((2026, 5, 6, 12, 0, 0, 0, 1, -1))


class FakeStore:
    """够用的 settings 表：只实现 get_setting / set_setting。"""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting(self, key, default=None):
        value = self.settings.get(key)
        return default if value is None else value

    def set_setting(self, key, value):
        self.settings[key] = str(value)


class FakeEpisodes:
    def __init__(self, events=None, commitments=None):
        self.events = list(events or [])
        self.commitments = list(commitments or [])

    def list_for_user(self, session_key, user_id, space_id='chat', kind='all', limit=20):
        return {'events': list(self.events), 'commitments': list(self.commitments)}


def event_row(row_id=1, summary='一起去镇口买面包', updated_at=1000):
    return {'id': row_id, 'summary': summary, 'evidence_type': 'co_enacted',
            'status': '', 'updated_at': updated_at}


def commitment_row(row_id=2, content='明天一起烤面包', status='open', updated_at=1000):
    return {'id': row_id, 'content': content, 'status': status, 'promisor': 'assistant',
            'evidence_type': '', 'condition': '', 'due_text': '', 'result': '',
            'updated_at': updated_at}


def plan(config=None, episodes=None, store=None):
    cfg = {'private_active_enabled': True}
    cfg.update(config or {})
    store = store or FakeStore()
    return PrivateActivePlanner(store, cfg, episodes if episodes is not None else FakeEpisodes()), store


class PlannerConfigTest(unittest.TestCase):
    def test_defaults_match_design_doc(self):
        planner, _store = plan()
        self.assertTrue(planner.enabled())
        self.assertEqual(6.0, planner.min_silence_hours())
        self.assertEqual(1, planner.daily_limit())
        self.assertEqual(23, planner.quiet_start())
        self.assertEqual(8, planner.quiet_end())

    def test_switch_defaults_off(self):
        planner = PrivateActivePlanner(FakeStore(), {}, FakeEpisodes())
        self.assertFalse(planner.enabled())
        self.assertEqual((None, 'disabled', ''),
                         planner.decide(SESSION, UID, 'chat', 3))

    def test_default_numbers_when_config_missing(self):
        planner = PrivateActivePlanner(FakeStore(), {}, FakeEpisodes())
        self.assertEqual(6.0, planner.min_silence_hours())
        self.assertEqual(1, planner.daily_limit())
        self.assertEqual(23, planner.quiet_start())
        self.assertEqual(8, planner.quiet_end())

    def test_bad_config_values_fall_back(self):
        planner, _ = plan({'private_active_min_silence_hours': 'abc',
                           'private_active_daily_limit': None,
                           'private_active_quiet_start': 'x',
                           'private_active_quiet_end': 'y'})
        self.assertEqual(6.0, planner.min_silence_hours())
        self.assertEqual(1, planner.daily_limit())
        self.assertEqual(23, planner.quiet_start())
        self.assertEqual(8, planner.quiet_end())


class TemplateTest(unittest.TestCase):
    def test_five_templates_use_the_expected_slots(self):
        from string import Formatter

        slots = {1: ['commitment'], 2: ['episode'], 3: ['topic', 'episode'],
                 4: ['commitment'], 5: ['episode', 'topic']}
        self.assertEqual(set(slots), set(TEMPLATES))
        for number, template in TEMPLATES.items():
            self.assertEqual(slots[number], [
                field for _, field, _, _ in Formatter().parse(template) if field
            ])

    def test_template_one_fills_commitment(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        number, text, hook = planner.decide(SESSION, UID, 'chat', 2, now=BASE + 8 * HOUR,
                                            silence=8 * HOUR)
        self.assertEqual(1, number)
        self.assertIn('明天一起烤面包', text)
        self.assertEqual(2, hook)
        self.assertNotIn('{', text)

    def test_template_two_fills_episode(self):
        planner, _ = plan(episodes=FakeEpisodes(events=[event_row()]))
        number, text, hook = planner.decide(SESSION, UID, 'chat', 2, now=BASE + 30 * HOUR,
                                            silence=30 * HOUR)
        self.assertEqual(2, number)
        self.assertIn('一起去镇口买面包', text)
        self.assertEqual(1, hook)

    def test_template_four_for_high_stage_commitment(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        number, text, _hook = planner.decide(SESSION, UID, 'chat', 5, now=BASE + 30 * HOUR,
                                             silence=30 * HOUR)
        self.assertEqual(4, number)
        self.assertIn('明天一起烤面包', text)

    def test_template_five_for_long_absence(self):
        planner, _ = plan(episodes=FakeEpisodes(events=[event_row()]))
        number, text, _hook = planner.decide(SESSION, UID, 'chat', 3, now=BASE + 72 * HOUR,
                                            silence=72 * HOUR, topic='磨坊的风车')
        self.assertEqual(5, number)
        self.assertIn('一起去镇口买面包', text)
        self.assertIn('磨坊的风车', text)

    def test_template_three_for_topic_with_episode(self):
        planner, _ = plan(episodes=FakeEpisodes(events=[event_row()]))
        number, text, _hook = planner.decide(SESSION, UID, 'chat', 2, now=BASE + 30 * HOUR,
                                            silence=30 * HOUR, topic='新来的面包师')
        self.assertEqual(3, number)
        self.assertIn('新来的面包师', text)

    def test_slot_content_is_clipped(self):
        long_text = '一起' * 80
        planner, _ = plan(episodes=FakeEpisodes(events=[event_row(summary=long_text)]))
        _number, text, _hook = planner.decide(SESSION, UID, 'chat', 1, now=BASE + 30 * HOUR,
                                             silence=30 * HOUR)
        self.assertLess(len(text), 80)


class VetoTest(unittest.TestCase):
    def test_no_memory_means_no_send(self):
        planner, _ = plan()
        self.assertEqual((None, 'no_memory', ''),
                         planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR, silence=30 * HOUR))

    def test_only_closed_commitment_is_not_a_hook(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row(status='fulfilled')]))
        self.assertEqual('no_memory',
                         planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR,
                                        silence=30 * HOUR)[1])

    def test_proposed_commitment_counts_as_hook(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row(status='proposed')]))
        self.assertIsNotNone(
            planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR, silence=30 * HOUR)[0])

    def test_stage_zero_never_sends(self):
        for stage in (0, -1):
            planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
            self.assertEqual('stage0',
                             planner.decide(SESSION, UID, 'chat', stage, now=BASE + 30 * HOUR,
                                            silence=30 * HOUR)[1])

    def test_unknown_stage_never_sends(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        self.assertEqual('stage0',
                         planner.decide(SESSION, UID, 'chat', None, now=BASE + 30 * HOUR,
                                        silence=30 * HOUR)[1])

    def test_silence_below_threshold_blocks(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        self.assertEqual('silence',
                         planner.decide(SESSION, UID, 'chat', 2, now=BASE, silence=5.9 * HOUR)[1])

    def test_silence_at_threshold_allows(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        self.assertIsNotNone(
            planner.decide(SESSION, UID, 'chat', 2, now=BASE, silence=6.0 * HOUR)[0])

    def test_silence_measured_from_last_turn_when_not_given(self):
        store = FakeStore({KEY_SEEN + SESSION: str(BASE)})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertEqual('silence', planner.decide(SESSION, UID, 'chat', 2, now=BASE + HOUR)[1])
        self.assertIsNotNone(planner.decide(SESSION, UID, 'chat', 2, now=BASE + 7 * HOUR)[0])

    def test_silence_measured_from_last_send_too(self):
        store = FakeStore({KEY_SEEN + SESSION: str(BASE), KEY_SEND + SESSION: str(BASE + 5 * HOUR)})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertEqual('silence', planner.decide(SESSION, UID, 'chat', 2, now=BASE + 6 * HOUR)[1])

    def test_pending_event_blocks(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        self.assertEqual('pending_event',
                         planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR,
                                        silence=30 * HOUR, pending_event='A', event_turns=2)[1])

    def test_pending_event_without_turns_does_not_block(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        self.assertIsNotNone(
            planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR, silence=30 * HOUR,
                           pending_event='A', event_turns=0)[0])

    def test_daily_limit_blocks_second_send(self):
        now = BASE + 30 * HOUR
        store = FakeStore({KEY_DAY + SESSION: '%s:1' % _day(now)})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertEqual('daily_limit', planner.decide(SESSION, UID, 'chat', 2, now=now,
                                                       silence=30 * HOUR)[1])

    def test_daily_counter_resets_next_day(self):
        now = BASE + 30 * HOUR
        store = FakeStore({KEY_DAY + SESSION: '%s:1' % _day(now - DAY)})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertIsNotNone(planner.decide(SESSION, UID, 'chat', 2, now=now, silence=30 * HOUR)[0])


class CircuitBreakTest(unittest.TestCase):
    def test_two_unanswered_sends_trip_the_breaker(self):
        store = FakeStore({KEY_COLD + SESSION: '2'})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertEqual('circuit',
                         planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR,
                                        silence=30 * HOUR)[1])

    def test_one_unanswered_send_still_allows(self):
        store = FakeStore({KEY_COLD + SESSION: '1'})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertIsNotNone(planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR,
                                            silence=30 * HOUR)[0])

    def test_user_message_clears_the_breaker(self):
        planner, store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        planner.note_send(SESSION, 2, now=BASE)
        planner.note_send(SESSION, 2, now=BASE + HOUR)
        self.assertEqual(2, planner.cold_count(SESSION))
        self.assertEqual('circuit',
                         planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR,
                                        silence=30 * HOUR)[1])
        planner.note_user_message(SESSION, '我回来了', now=BASE + 2 * HOUR)
        self.assertEqual(0, planner.cold_count(SESSION))
        # 熔断解除后不再以 circuit 为由拦截（当日额度已用完是另一条规则）
        self.assertNotEqual('circuit',
                            planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR,
                                           silence=30 * HOUR)[1])

    def test_note_send_increments_and_counts_the_day(self):
        planner, _store = plan()
        now = BASE + 5 * HOUR
        planner.note_send(SESSION, 7, now=now)
        self.assertEqual(1, planner.cold_count(SESSION))
        self.assertEqual(1, planner.used_today(SESSION, now))
        self.assertEqual(int(now), int(planner.last_send(SESSION)))
        self.assertEqual((7, True), planner.hook_reference(SESSION, now + HOUR))


class QuietHoursTest(unittest.TestCase):
    def test_night_hours_are_quiet(self):
        planner, _ = plan()
        self.assertTrue(planner._in_quiet(_at_hour(23)))
        self.assertTrue(planner._in_quiet(_at_hour(2)))
        self.assertTrue(planner._in_quiet(_at_hour(7)))

    def test_day_hours_are_not_quiet(self):
        planner, _ = plan()
        self.assertFalse(planner._in_quiet(_at_hour(8)))
        self.assertFalse(planner._in_quiet(_at_hour(15)))
        self.assertFalse(planner._in_quiet(_at_hour(22)))

    def test_quiet_hours_block_sending(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        quiet = _at_hour(2)
        self.assertEqual('quiet', planner.decide(SESSION, UID, 'chat', 3, now=quiet,
                                                 silence=30 * HOUR)[1])

    def test_configurable_window(self):
        planner, _ = plan({'private_active_quiet_start': 1, 'private_active_quiet_end': 3})
        self.assertTrue(planner._in_quiet(_at_hour(2)))
        self.assertFalse(planner._in_quiet(_at_hour(4)))

    def test_equal_start_and_end_means_never_quiet(self):
        planner, _ = plan({'private_active_quiet_start': 5, 'private_active_quiet_end': 5})
        for hour in (0, 5, 12, 23):
            self.assertFalse(planner._in_quiet(_at_hour(hour)))


class NegativeClosingTest(unittest.TestCase):
    def test_negative_last_turn_blocks(self):
        planner, _ = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        self.assertEqual('negative',
                         planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR,
                                        silence=30 * HOUR, last_user_text='别烦我')[1])

    def test_busy_words_block_for_a_day(self):
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]))
        planner.note_user_message(SESSION, '我先去忙了，晚安', now=BASE + 30 * HOUR)
        self.assertEqual('busy',
                         planner.decide(SESSION, UID, 'chat', 3, now=BASE + 31 * HOUR,
                                        silence=30 * HOUR)[1])
        # 超过 24 小时就该解禁
        self.assertIsNotNone(
            planner.decide(SESSION, UID, 'chat', 3, now=BASE + 30 * HOUR + DAY, silence=30 * HOUR)[0])

    def test_normal_message_does_not_set_busy(self):
        planner, _store = plan()
        planner.note_user_message(SESSION, '今天天气不错', now=BASE)
        self.assertEqual(0.0, planner.busy_until(SESSION))


class MemoryReuseTest(unittest.TestCase):
    def test_same_hook_reused_within_three_days_blocks(self):
        now = BASE + 30 * HOUR
        store = FakeStore({KEY_HOOK + SESSION: '2|%s' % _day(now)})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertEqual('reused', planner.decide(SESSION, UID, 'chat', 2, now=now,
                                                  silence=30 * HOUR)[1])

    def test_other_hook_still_allowed(self):
        now = BASE + 30 * HOUR
        store = FakeStore({KEY_HOOK + SESSION: '99|%s' % _day(now)})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertIsNotNone(planner.decide(SESSION, UID, 'chat', 2, now=now, silence=30 * HOUR)[0])

    def test_old_hook_reference_expires(self):
        now = BASE + 30 * HOUR
        store = FakeStore({KEY_HOOK + SESSION: '2|%s' % _day(now - 5 * DAY)})
        planner, _store = plan(episodes=FakeEpisodes(commitments=[commitment_row()]), store=store)
        self.assertIsNotNone(planner.decide(SESSION, UID, 'chat', 2, now=now, silence=30 * HOUR)[0])


class ServiceTest(unittest.TestCase):
    """`PrivateActiveService`：默认关闭时零动作，开启后真的发出去。"""

    def test_disabled_service_does_not_start_thread(self):
        runtime = FakeRuntime(private_active_enabled=False)
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        service.start()
        self.assertIsNone(service._thread)
        self.assertFalse(service.enabled())
        self.assertEqual([], service.tick())
        self.assertEqual([], runtime.replies)

    def test_remember_is_a_noop_when_disabled(self):
        runtime = FakeRuntime(private_active_enabled=False)
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        service.remember(_msg('你好'))
        self.assertEqual([], service.tick())
        self.assertEqual([], runtime.replies)

    def test_enabled_service_sends_after_silence(self):
        runtime = FakeRuntime(private_active_enabled=True)
        runtime.episodes = FakeEpisodes(commitments=[commitment_row()])
        runtime.relationship = FakeRelationship(stage=2)
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        service.remember(_msg('今天面包烤好了'))
        now = time_now()
        service.planner.note_user_message(SESSION, '嗯', now=now - 8 * HOUR)
        sent = service.tick(now=now)
        self.assertEqual(1, len(sent))
        self.assertIn('明天一起烤面包', sent[0][1])
        self.assertEqual(1, len(runtime.replies))
        self.assertEqual(1, service.planner.cold_count(SESSION))

    def test_service_does_not_send_twice_in_a_day(self):
        runtime = FakeRuntime(private_active_enabled=True)
        runtime.episodes = FakeEpisodes(commitments=[commitment_row()])
        runtime.relationship = FakeRelationship(stage=2)
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        service.remember(_msg('嗯'))
        now = time_now()
        service.planner.note_user_message(SESSION, '嗯', now=now - 8 * HOUR)
        self.assertEqual(1, len(service.tick(now=now)))
        service.planner.note_user_message(SESSION, '睡了', now=now)
        self.assertEqual([], service.tick(now=now + 12 * HOUR))

    def test_rejected_delivery_does_not_consume_quota(self):
        runtime = FakeRuntime(private_active_enabled=True, reply_accepted=False)
        runtime.episodes = FakeEpisodes(commitments=[commitment_row()])
        runtime.relationship = FakeRelationship(stage=2)
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        service.remember(_msg('嗯'))
        now = time_now()
        service.planner.note_user_message(SESSION, '嗯', now=now - 8 * HOUR)
        self.assertEqual([], service.tick(now=now))
        self.assertEqual(0, service.planner.used_today(SESSION, now))
        self.assertEqual(0, service.planner.cold_count(SESSION))

    def test_no_relationship_means_stage_zero_and_no_send(self):
        runtime = FakeRuntime(private_active_enabled=True)
        runtime.episodes = FakeEpisodes(commitments=[commitment_row()])
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        service.remember(_msg('嗯'))
        now = time_now()
        service.planner.note_user_message(SESSION, '嗯', now=now - 8 * HOUR)
        self.assertEqual([], service.tick(now=now))
        self.assertEqual([], runtime.replies)

    def test_session_registry_is_bounded(self):
        runtime = FakeRuntime(private_active_enabled=True)
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        from newGeYi.routing import private_active as module
        for index in range(module.MAX_SESSIONS + 5):
            service.remember(_msg('嗯', target=str(index)))
        self.assertLessEqual(len(service._messages), module.MAX_SESSIONS)


class _FakeSleep:
    def __init__(self, phase):
        self._phase = phase

    def phase(self, now=None):
        return self._phase


class DeliveryGateTest(unittest.TestCase):
    """主动搭话也要守「她在休息/睡了」：不能在她安静的时候凭空冒出来。"""

    def _service(self, **extra):
        runtime = FakeRuntime(private_active_enabled=True, **extra)
        runtime.episodes = FakeEpisodes(commitments=[commitment_row()])
        runtime.relationship = FakeRelationship(stage=2)
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        service.remember(_msg('嗯'))
        service.planner.note_user_message(SESSION, '嗯', now=time_now() - 8 * HOUR)
        return runtime, service

    def test_resting_runtime_blocks_active_chat(self):
        runtime, service = self._service()
        runtime.is_resting = lambda session_key: True
        self.assertEqual([], service.tick(now=time_now()))
        self.assertEqual([], runtime.replies)

    def test_asleep_phase_blocks_active_chat(self):
        runtime, service = self._service()
        runtime.sleep = _FakeSleep('asleep')
        self.assertEqual([], service.tick(now=time_now()))
        self.assertEqual([], runtime.replies)

    def test_awake_phase_still_sends(self):
        runtime, service = self._service()
        runtime.is_resting = lambda session_key: False
        runtime.sleep = _FakeSleep('awake')
        self.assertEqual(1, len(service.tick(now=time_now())))
        self.assertEqual(1, len(runtime.replies))

    def test_resting_check_failure_does_not_block(self):
        """守卫本身出错时不能把功能静默关掉。"""
        runtime, service = self._service()

        def _boom(session_key):
            raise RuntimeError('rest fixture')

        runtime.is_resting = _boom
        self.assertEqual(1, len(service.tick(now=time_now())))
        self.assertEqual(1, len(runtime.replies))


class TopicHookTest(unittest.TestCase):
    """谈资钩子：优先用关系服务里今天那条（和主对话是同一件小事），再退回旧 settings 键。"""

    def test_topic_comes_from_relationship_snapshot(self):
        runtime = FakeRuntime(private_active_enabled=True)
        runtime.relationship = FakeRelationship(stage=2)
        runtime.relationship.snapshot = lambda uid, log_limit=0: {
            'user_id': uid, 'stage': 2, 'pending_event': '', 'event_turns': 0,
            'topic': '后院的鸡蛋又不见了。'}
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        self.assertEqual('后院的鸡蛋又不见了。', service._topic_of(SESSION))

    def test_falls_back_to_settings_key(self):
        runtime = FakeRuntime(private_active_enabled=True)
        runtime.relationship = None
        runtime.store.set_setting('topic:' + SESSION, '旧谈资还在。')
        service = PrivateActiveService(runtime, runtime.config, runtime.episodes)
        self.assertEqual('旧谈资还在。', service._topic_of(SESSION))


def _day(now):
    import time
    return time.strftime('%Y%m%d', time.localtime(now))


def _at_hour(hour):
    """构造一个「本地时间正好 hour 点」的秒时间戳。"""
    import time
    base = time.mktime((2026, 5, 6, 0, 0, 0, 0, 1, -1))
    return base + hour * HOUR


def time_now():
    return _at_hour(12)


class FakeRelationship:
    def __init__(self, stage=2, pending_event='', event_turns=0):
        self.stage = stage
        self.pending_event = pending_event
        self.event_turns = event_turns

    def snapshot(self, uid, log_limit=0):
        return {'user_id': uid, 'stage': self.stage, 'pending_event': self.pending_event,
                'event_turns': self.event_turns}


class _Msg:
    def __init__(self, text, target):
        from newGeYi.models import IncomingMessage, SessionKey
        self.session = SessionKey(bot_hash='00000000000000000000000000000000', platform='qq',
                                  sdk='onebot', session_type='private', host_id='-', target_id=target)
        self.message_id = '1'
        self.sender_id = target
        self.sender_name = 'Oracle'
        self.text = text
        self.raw_text = text
        self.is_group = False
        self.mentioned_bot = False
        self.event = None
        self.proc = None


def _msg(text, target=UID):
    return _Msg(text, target)


class FakeRuntime:
    def __init__(self, reply_accepted=True, **cfg):
        self.config = {'private_active_enabled': False}
        self.config.update(cfg)
        self.store = FakeStore()
        self.episodes = FakeEpisodes()
        self.relationship = None
        self.replies = []
        self.tracked = []
        self._reply_accepted = reply_accepted

    def reply(self, msg, text):
        import types
        self.replies.append(text)
        return types.SimpleNamespace(accepted=self._reply_accepted, error='')

    def _track_self_sent(self, session, text, is_group, space_id=''):
        self.tracked.append((session.as_text(), text, is_group))


if __name__ == '__main__':
    unittest.main()
