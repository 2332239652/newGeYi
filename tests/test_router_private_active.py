# -*- encoding: utf-8 -*-
"""私聊主动搭话在**路由层**的接线：默认关闭必须零影响，开启后才真的拉起服务。

这一层比 `test_private_active.py` 更靠外：它走真实的 `MessageRouter.route()`，
只把 runtime 换成最小实现，用来锁住两件事：

1. `private_active_enabled` 缺省（以及显式 False）时，路由的每一步都与改动前完全一致：
   不多建属性、不多起线程、不多发消息、被动回复照旧进队列；
2. 打开后，私聊消息会被服务记住（刷新静默计时、解除熔断），群聊消息一律不碰。

全部离线：无网络、无真实 API、无真实 SQLite。
"""

from __future__ import annotations

import os
import tempfile
import threading
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_router_private_active_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.message_router import MessageRouter  # noqa: E402

BOT = '00000000000000000000000000000000'
STRANGER = '1000000002'
PRIVATE_KEY = '%s:qq:onebot:private:-:%s' % (BOT, STRANGER)


def make_msg(text, is_group=False, target=None, sender_id=STRANGER, sender_name='Oracle'):
    if target is None:
        target = 'group-1001' if is_group else STRANGER
    session = SessionKey(bot_hash=BOT, platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='host' if is_group else '-',
                         target_id=target)
    return IncomingMessage(
        session=session, message_id='1', sender_id=sender_id, sender_name=sender_name,
        text=text, raw_text=text, is_group=is_group, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


class FakeStore:
    """单参数老接口：故意不带 default，专门验证新代码兼容旧 fake。"""

    def __init__(self):
        self.settings = {}

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = str(value)


class FakeScheduler:
    def __init__(self):
        self.kinds = []

    def submit(self, msg, kind):
        self.kinds.append(kind)
        return True


class FakeGate:
    def __init__(self, allow=False):
        self.allow = allow
        self.evaluated = 0

    def observe_message(self, *a, **k):
        pass

    def evaluate(self, *a, **k):
        return types.SimpleNamespace(allow=self.allow)

    def note_evaluated(self, *a, **k):
        self.evaluated += 1


class FakePrivateGate:
    """私聊闸门：默认全放行。"""

    def __init__(self, allowed=True, why=''):
        self.allowed = allowed
        self.why = why
        self.commits = 0

    def trigger_only(self):
        return False

    def hit_trigger(self, text):
        return False

    def allow(self, msg):
        return self.allowed, self.why

    def commit(self, session_key):
        self.commits += 1


class FakeRuntime:
    """最小 runtime：只提供 router 真正会碰到的属性。"""

    def __init__(self, active_allow=False, **cfg):
        self.config = {
            'passive_enabled': True,
            'private_enabled': True,
            'triggers': ['歌伊'],
            'route_debug': False,
            'active_bare_image_factor': 0.3,
        }
        self.config.update(cfg)
        self.store = FakeStore()
        self.scheduler = FakeScheduler()
        self.attention = FakeGate(active_allow)
        self.live_context = None
        self.images = None
        self.private_gate = FakePrivateGate()
        self.replies = []

    def is_resting(self, session_key):
        return False

    def reply(self, msg, text):
        self.replies.append(text)
        return types.SimpleNamespace(accepted=True, error='')


class SwitchOffTest(unittest.TestCase):
    """硬要求：`private_active_enabled` 关闭时，行为与改动前完全一致。"""

    def test_default_config_leaves_no_service_and_no_threads(self):
        rt = FakeRuntime()
        router = MessageRouter(rt)
        before = threading.active_count()
        router.route(make_msg('发语音啦'))
        self.assertFalse(hasattr(rt, 'private_active'))
        self.assertEqual(before, threading.active_count())
        self.assertEqual(['passive'], rt.scheduler.kinds)
        self.assertEqual([], rt.replies)

    def test_explicit_false_is_the_same_as_missing(self):
        rt = FakeRuntime(private_active_enabled=False)
        router = MessageRouter(rt)
        router.route(make_msg('嗯'))
        self.assertFalse(hasattr(rt, 'private_active'))
        self.assertEqual(['passive'], rt.scheduler.kinds)
        self.assertEqual({}, rt.store.settings)

    def test_switch_off_writes_no_settings_at_all(self):
        rt = FakeRuntime(private_active_enabled=False)
        router = MessageRouter(rt)
        for text in ('嗯', '我到了', '晚安', '【图片】'):
            router.route(make_msg(text))
        self.assertEqual({}, rt.store.settings)
        self.assertEqual(['passive'] * 4, rt.scheduler.kinds)

    def test_group_message_never_touches_private_active(self):
        rt = FakeRuntime()
        router = MessageRouter(rt)
        router.route(make_msg('随便说点什么', is_group=True))
        self.assertFalse(hasattr(rt, 'private_active'))
        self.assertEqual({}, rt.store.settings)

    def test_switch_off_keeps_quota_hint_path_intact(self):
        rt = FakeRuntime(private_active_enabled=False)
        rt.private_gate = FakePrivateGate(allowed=False, why='quota_user')
        router = MessageRouter(rt)
        router.route(make_msg('在吗'))
        self.assertEqual([], rt.scheduler.kinds)
        self.assertEqual(1, len(rt.replies))
        self.assertIn('今天和我聊的次数用完啦', rt.replies[0])


class SwitchOnTest(unittest.TestCase):
    """打开后：私聊消息被记账，群聊消息完全不碰。"""

    def _service(self, rt):
        from newGeYi.routing.private_active import PrivateActiveService
        runtime_episodes = getattr(rt, 'episodes', None)
        return PrivateActiveService(rt, rt.config, runtime_episodes)

    def test_private_message_is_remembered_when_enabled(self):
        rt = FakeRuntime(private_active_enabled=True)
        router = MessageRouter(rt)
        router.route(make_msg('我回来了'))
        service = getattr(rt, 'private_active', None)
        self.assertIsNotNone(service)
        self.assertIn(PRIVATE_KEY, service._messages)
        self.assertGreater(service.planner.last_seen(PRIVATE_KEY), 0)
        self.assertEqual(['passive'], rt.scheduler.kinds)

    def test_enabled_service_never_starts_a_thread_eagerly_twice(self):
        rt = FakeRuntime(private_active_enabled=True)
        router = MessageRouter(rt)
        router.route(make_msg('嗯'))
        service = rt.private_active
        first = service._thread
        router.route(make_msg('嗯嗯'))
        self.assertIs(service, rt.private_active)
        self.assertIs(first, service._thread)
        service.stop()

    def test_group_message_does_not_register_a_session(self):
        rt = FakeRuntime(private_active_enabled=True)
        router = MessageRouter(rt)
        router.route(make_msg('随便说点什么', is_group=True))
        self.assertFalse(hasattr(rt, 'private_active'))

    def test_remember_clears_the_circuit_breaker(self):
        rt = FakeRuntime(private_active_enabled=True)
        router = MessageRouter(rt)
        router.route(make_msg('嗯'))
        planner = rt.private_active.planner
        planner.note_send(PRIVATE_KEY, 1)
        planner.note_send(PRIVATE_KEY, 1)
        self.assertEqual(2, planner.cold_count(PRIVATE_KEY))
        router.route(make_msg('我回来了'))
        self.assertEqual(0, planner.cold_count(PRIVATE_KEY))
        rt.private_active.stop()

    def test_busy_words_are_recorded_on_the_way_in(self):
        rt = FakeRuntime(private_active_enabled=True)
        router = MessageRouter(rt)
        router.route(make_msg('我先去忙了，晚安'))
        planner = rt.private_active.planner
        self.assertGreater(planner.busy_until(PRIVATE_KEY), 0)
        rt.private_active.stop()


if __name__ == '__main__':
    unittest.main()
