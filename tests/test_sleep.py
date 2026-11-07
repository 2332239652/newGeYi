# -*- encoding: utf-8 -*-
"""作息（休眠）回归：睡前提醒、睡着时的缄默、跨午夜。

三个最容易踩的点，各有一条测试钉住：

1. **跨午夜**：23:30-07:00 用 `start <= m < end` 判的话整段不生效，而且不报错——
   半夜她照样秒回，你还以为是开关没保存。
2. **提示只一次**：记日期的话，23:40 提示过、过了午夜换天，00:30 又会再提示一次。
   所以状态记的是"这个睡眠段的起始时间戳"。
3. **主人指令永远不受限**：命令在 `commands.try_handle` 里就返回了，走不到路由层；
   否则半夜想「休眠关」会把自己锁死。
"""

from __future__ import annotations

import os
import tempfile
import time
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_sleep_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.message_router import MessageRouter  # noqa: E402
from newGeYi.runtime.sleep import (  # noqa: E402
    SleepWindow,
    in_window,
    minutes_until,
    parse_window,
)

BOT = '53da3460cc454e12667452872284f47d'
OWNER = '2332239652'
STRANGER = '1000000002'
GROUP = '1013491196'


def ts_at(hour: int, minute: int, day_offset: int = 0) -> float:
    """今天（可偏移若干天）本地时间 HH:MM 的时间戳。"""
    local = time.localtime()
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday + day_offset,
                        hour, minute, 0, 0, 0, -1))


def make_msg(text, is_group, target, sender_id=STRANGER, mentioned=False):
    session = SessionKey(bot_hash=BOT, platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='-' if not is_group else 'host',
                         target_id=target)
    return IncomingMessage(
        session=session, message_id='1', sender_id=sender_id, sender_name='测试',
        text=text, raw_text=text, is_group=is_group, mentioned_bot=mentioned,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


class FakeStore:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = value

    def settings_with_prefix(self, prefix):
        return [(k, v) for k, v in self.settings.items() if k.startswith(prefix)]


class WindowMathTest(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(parse_window('23:30-07:00'), (1410, 420))
        self.assertEqual(parse_window(' 00:00-00:30 '), (0, 30))

    def test_parse_bad(self):
        for raw in ('', 'abc', '23:30', '24:00-07:00', '23:60-07:00', None, '23:30~07:00'):
            self.assertEqual(parse_window(raw), (-1, -1), f'{raw!r} 应该解析失败')

    def test_cross_midnight(self):
        """23:30-07:00：午夜前后都在里面，07:00 整点算起床。"""
        for minute, want in ((1409, False), (1430, True), (1439, True),
                             (0, True), (120, True), (419, True), (420, False),
                             (720, False), (1380, False)):
            self.assertEqual(in_window(minute, 1410, 420), want, f'minute={minute}')

    def test_same_day(self):
        for minute, want in ((0, False), (60, True), (479, True), (480, False)):
            self.assertEqual(in_window(minute, 60, 480), want, f'minute={minute}')

    def test_degenerate(self):
        self.assertFalse(in_window(600, 600, 600), '起止相同应为不睡')
        self.assertFalse(in_window(600, -1, 420), '解析失败应为不睡')

    def test_minutes_until_wraps(self):
        self.assertEqual(minutes_until(1380, 1410), 30)
        self.assertEqual(minutes_until(0, 1410), 1410)
        self.assertEqual(minutes_until(1400, 1410), 10)


class PhaseTest(unittest.TestCase):
    """阶段判定。窗口固定 23:30-07:00，睡前 30 分钟。"""

    def setUp(self):
        self.config = {
            'sleep_enabled': True,
            'sleep_window': '23:30-07:00',
            'sleep_pre_minutes': 30,
        }
        self.window = SleepWindow(FakeStore(), self.config)

    def phase_at(self, hour, minute):
        return self.window.phase(ts_at(hour, minute))

    def test_asleep_across_midnight(self):
        for hour, minute in ((23, 30), (23, 59), (0, 0), (2, 0), (6, 59)):
            self.assertEqual(self.phase_at(hour, minute), 'asleep', f'{hour}:{minute}')

    def test_awake_boundaries(self):
        for hour, minute in ((7, 0), (12, 0), (22, 59)):
            self.assertEqual(self.phase_at(hour, minute), 'awake', f'{hour}:{minute}')

    def test_pre_window(self):
        for hour, minute in ((23, 0), (23, 29)):
            self.assertEqual(self.phase_at(hour, minute), 'pre', f'{hour}:{minute}')

    def test_off_when_disabled(self):
        self.config['sleep_enabled'] = False
        self.assertEqual(self.phase_at(2, 0), 'off')
        self.assertEqual(self.window.pre_note(ts_at(23, 10)), '')

    def test_off_when_window_broken(self):
        self.config['sleep_window'] = '乱写'
        self.assertEqual(self.phase_at(2, 0), 'off')

    def test_minutes_to_sleep(self):
        self.assertEqual(self.window.minutes_to_sleep(ts_at(23, 0)), 30)
        self.assertEqual(self.window.minutes_to_sleep(ts_at(2, 0)), 0)

    def test_pre_note_only_in_pre(self):
        note = self.window.pre_note(ts_at(23, 10))
        self.assertIn('快到睡觉时间了', note)
        self.assertIn('23:30', note)
        self.assertEqual(self.window.pre_note(ts_at(12, 0)), '')

    def test_status_text(self):
        self.assertIn('休眠：开', self.window.status_text(ts_at(12, 0)))
        self.assertIn('正在睡', self.window.status_text(ts_at(2, 0)))
        self.assertIn('分钟就要去睡', self.window.status_text(ts_at(23, 10)))


class NoticeOnceTest(unittest.TestCase):
    """睡着时的"已经睡了"只应一句——跨午夜也不能重复。"""

    def setUp(self):
        self.store = FakeStore()
        self.config = {
            'sleep_enabled': True,
            'sleep_window': '23:30-07:00',
            'sleep_pre_minutes': 30,
            'sleep_notice_once': True,
        }
        self.window = SleepWindow(self.store, self.config)

    def test_once_per_session_per_night(self):
        first = self.window.notice_once_now('sess', ts_at(23, 40))
        second = self.window.notice_once_now('sess', ts_at(23, 45))
        self.assertTrue(first)
        self.assertFalse(second, '同一晚第二次被叫不该再应')

    def test_survives_midnight(self):
        """23:40 提示过，次日 00:30 / 03:00 属于同一个睡眠段，不许再来一次。"""
        self.assertTrue(self.window.notice_once_now('sess', ts_at(23, 40)))
        self.assertFalse(self.window.notice_once_now('sess', ts_at(0, 30, day_offset=1)))
        self.assertFalse(self.window.notice_once_now('sess', ts_at(3, 0, day_offset=1)))

    def test_next_night_resets(self):
        self.assertTrue(self.window.notice_once_now('sess', ts_at(23, 40)))
        self.assertTrue(self.window.notice_once_now('sess', ts_at(23, 40, day_offset=1)),
                        '第二天晚上应该重新提示一次')

    def test_per_session(self):
        self.assertTrue(self.window.notice_once_now('a', ts_at(2, 0)))
        self.assertTrue(self.window.notice_once_now('b', ts_at(2, 0)))

    def test_switch_off_always_notices(self):
        self.config['sleep_notice_once'] = False
        for _ in range(3):
            self.assertTrue(self.window.notice_once_now('sess', ts_at(2, 0)))

    def test_reset(self):
        self.assertTrue(self.window.notice_once_now('sess', ts_at(2, 0)))
        self.window.reset_notice('sess')
        self.assertTrue(self.window.notice_once_now('sess', ts_at(2, 0)))


class FakeScheduler:
    def __init__(self):
        self.kinds = []

    def submit(self, msg, kind):
        self.kinds.append(kind)
        return True


class FakeGate:
    def __init__(self, allow=False):
        self.allow = allow

    def observe_message(self, *a, **k):
        pass

    def evaluate(self, *a, **k):
        return types.SimpleNamespace(allow=self.allow)

    def note_evaluated(self, *a, **k):
        pass


class FakePermissions:
    def __init__(self, owners=()):
        self.owners = set(owners)

    def is_owner(self, msg):
        return str(msg.sender_id) in self.owners


class FakeRuntime:
    def __init__(self, owners=(), **cfg):
        self.config = {
            'passive_enabled': True,
            'private_enabled': True,
            'triggers': ['歌伊'],
            'route_debug': False,
            'active_bare_image_factor': 0.3,
            'sleep_enabled': True,
            'sleep_window': '23:30-07:00',
            'sleep_pre_minutes': 30,
        }
        self.config.update(cfg)
        self.store = FakeStore()
        self.scheduler = FakeScheduler()
        self.attention = FakeGate(False)
        self.live_context = None
        self.images = None
        self.permissions = FakePermissions(owners)
        self.sleep = SleepWindow(self.store, self.config)
        self.notices = []

    def is_resting(self, key):
        return False

    def send_sleep_notice(self, msg):
        self.notices.append(msg.text)


def freeze_window(rt, asleep: bool) -> None:
    """把作息窗口挪到"现在正在睡"或"现在离睡还远"，避免测试依赖真实时间。"""
    now = time.time()
    shift = -10 * 60 if asleep else 6 * 3600
    start_lt = time.localtime(now + shift)
    end_lt = time.localtime(now + shift + 8 * 3600)
    rt.config['sleep_window'] = '%02d:%02d-%02d:%02d' % (
        start_lt.tm_hour, start_lt.tm_min, end_lt.tm_hour, end_lt.tm_min)


class RouterSleepTest(unittest.TestCase):
    def test_private_message_while_asleep_notices_once(self):
        rt = FakeRuntime()
        freeze_window(rt, asleep=True)
        router = MessageRouter(rt)
        router.route(make_msg('在吗', is_group=False, target=STRANGER))
        router.route(make_msg('在吗在吗', is_group=False, target=STRANGER))
        self.assertEqual(rt.scheduler.kinds, [], '睡着时不该排模型任务')
        self.assertEqual(len(rt.notices), 1, '第一次应一句，第二次该缄默')

    def test_group_chatter_while_asleep_is_silent(self):
        rt = FakeRuntime()
        freeze_window(rt, asleep=True)
        router = MessageRouter(rt)
        router.route(make_msg('今天好热啊', is_group=True, target=GROUP))
        self.assertEqual(rt.notices, [], '群里闲聊不该被"睡着了"打断')
        self.assertEqual(rt.scheduler.kinds, [])

    def test_group_mention_while_asleep_notices(self):
        rt = FakeRuntime()
        freeze_window(rt, asleep=True)
        router = MessageRouter(rt)
        router.route(make_msg('歌伊醒醒', is_group=True, target=GROUP, mentioned=True))
        self.assertEqual(len(rt.notices), 1)
        self.assertEqual(rt.scheduler.kinds, [])

    def test_awake_still_replies(self):
        rt = FakeRuntime()
        freeze_window(rt, asleep=False)
        router = MessageRouter(rt)
        router.route(make_msg('在吗', is_group=False, target=STRANGER))
        self.assertEqual(rt.scheduler.kinds, ['passive'])
        self.assertEqual(rt.notices, [])

    def test_owner_exempt_switch(self):
        rt = FakeRuntime(owners=[OWNER], sleep_owner_exempt=True)
        freeze_window(rt, asleep=True)
        router = MessageRouter(rt)
        router.route(make_msg('还没睡呀', is_group=False, target=OWNER, sender_id=OWNER))
        self.assertEqual(rt.scheduler.kinds, ['passive'], '主人豁免开着就该照常回')
        self.assertEqual(rt.notices, [])

    def test_owner_still_asleep_by_default(self):
        rt = FakeRuntime(owners=[OWNER])
        freeze_window(rt, asleep=True)
        router = MessageRouter(rt)
        router.route(make_msg('还没睡呀', is_group=False, target=OWNER, sender_id=OWNER))
        self.assertEqual(rt.scheduler.kinds, [])
        self.assertEqual(len(rt.notices), 1)

    def test_sleep_disabled_never_notices(self):
        rt = FakeRuntime(sleep_enabled=False)
        router = MessageRouter(rt)
        router.route(make_msg('在吗', is_group=False, target=STRANGER))
        self.assertEqual(rt.scheduler.kinds, ['passive'])
        self.assertEqual(rt.notices, [])


class PromptInjectionTest(unittest.TestCase):
    """睡前提醒必须真的进 system（只测字符串不算数）。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')

    def _system(self, msg) -> str:
        messages = self.rt.prompt_builder.build(msg, is_active=False)
        parts = []
        for message in messages:
            content = message.get('content')
            if isinstance(content, list):
                parts.extend(str(p.get('text', '')) for p in content if isinstance(p, dict))
            else:
                parts.append(str(content))
        return '\n'.join(parts)

    def _window(self, phase: str) -> None:
        now = time.time()
        shift = {'asleep': -600, 'pre': 600, 'awake': 6 * 3600}[phase]
        start_lt = time.localtime(now + shift)
        end_lt = time.localtime(now + shift + 8 * 3600)
        self.rt.config['sleep_enabled'] = True
        self.rt.config['sleep_window'] = '%02d:%02d-%02d:%02d' % (
            start_lt.tm_hour, start_lt.tm_min, end_lt.tm_hour, end_lt.tm_min)

    def test_pre_phase_injects_note(self):
        self._window('pre')
        joined = self._system(make_msg('陪我聊会', is_group=False, target=STRANGER))
        self.assertIn('快到睡觉时间了', joined)

    def test_awake_has_no_note(self):
        self._window('awake')
        joined = self._system(make_msg('陪我聊会', is_group=False, target=STRANGER))
        self.assertNotIn('快到睡觉时间了', joined)

    def test_disabled_has_no_note(self):
        self._window('pre')
        self.rt.config['sleep_enabled'] = False
        joined = self._system(make_msg('陪我聊会', is_group=False, target=STRANGER))
        self.assertNotIn('快到睡觉时间了', joined)


if __name__ == '__main__':
    unittest.main()
