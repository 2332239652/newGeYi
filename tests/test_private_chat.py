# -*- encoding: utf-8 -*-
"""私聊模式的回归测试（离线、无网络、不碰线上状态）。

2026-11-01 线上暴露的三个坑，全部锁在这里：

1. **私聊只要不含「歌伊」就整条不回**：`directed` 以前只看 @/触发词，私聊里
   '发语音啦'、'狗，修，金，sama~' 直接被丢掉（对方只好补一句「歌伊发语音正式一点啦」）。
2. **私聊当成群聊**：她在私聊里说过「这里是群诶，大家都看见了」——因为当前消息既没有
   场景说明，记忆块标题还写着「这个群最近发生过什么」。
3. **私聊没有发言人标签 → 主人判定失效**：`昵称(QQ号)：` 只有群聊才带，
   于是【主人】那条「只有 2332239652 是主人」在私聊里完全无从判定。
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_private_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.message_router import MessageRouter  # noqa: E402

BOT = 'bot-test'
OWNER = '2332239652'
STRANGER = '1000000002'


def make_msg(text, is_group, target, sender_id=STRANGER, sender_name='Oracle'):
    session = SessionKey(bot_hash=BOT, platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='-' if not is_group else 'host',
                         target_id=target)
    return IncomingMessage(
        session=session, message_id='1', sender_id=sender_id, sender_name=sender_name,
        text=text, raw_text=text, is_group=is_group, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


class FakeStore:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key):
        return self.settings.get(key)


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


class FakeRuntime:
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

    def is_resting(self, session_key):
        return False


class PrivateDirectedTest(unittest.TestCase):
    """私聊里每一句都算「对我说」，不需要靠触发词点名。"""

    def test_private_message_without_trigger_still_replies(self):
        rt = FakeRuntime()
        router = MessageRouter(rt)
        for text in ('发语音啦', '狗，修，金，sama~', '嗯', '【图片】'):
            rt.scheduler.kinds.clear()
            router.route(make_msg(text, is_group=False, target=STRANGER))
            self.assertEqual(rt.scheduler.kinds, ['passive'], f'私聊被吞: {text!r}')

    def test_private_message_with_mention_still_replies_once(self):
        rt = FakeRuntime()
        router = MessageRouter(rt)
        router.route(make_msg('歌伊在吗', is_group=False, target=STRANGER))
        self.assertEqual(rt.scheduler.kinds, ['passive'])

    def test_group_message_without_trigger_is_not_a_passive_reply(self):
        """群聊的克制口径不能跟着改：没人叫她就不该抢话。"""
        rt = FakeRuntime(active_allow=False)
        router = MessageRouter(rt)
        router.route(make_msg('今天好热啊', is_group=True, target='123456789'))
        self.assertEqual(rt.scheduler.kinds, [])

    def test_private_disabled_stays_silent(self):
        rt = FakeRuntime(private_enabled=False)
        router = MessageRouter(rt)
        router.route(make_msg('在吗', is_group=False, target=STRANGER))
        self.assertEqual(rt.scheduler.kinds, [])


class PrivatePromptTest(unittest.TestCase):
    """prompt 结构：私聊必须带发言人标签 + 场景说明，且不出现「群」的措辞。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')

    def test_private_current_message_carries_speaker_label(self):
        msg = make_msg('我是你主人', is_group=False, target=STRANGER)
        messages = self.rt.prompt_builder.build(msg)
        current = messages[-1]['content']
        self.assertTrue(current.startswith('Oracle(1000000002)：'), current)
        self.assertIn('这是私聊', current)
        self.assertIn('只有你们两个人', current)

    def test_group_current_message_has_no_private_note(self):
        msg = make_msg('歌伊在吗', is_group=True, target='123456789',
                       sender_id=OWNER, sender_name='歌靡')
        messages = self.rt.prompt_builder.build(msg)
        current = messages[-1]['content']
        self.assertTrue(current.startswith('歌靡(2332239652)：'), current)
        self.assertNotIn('这是私聊', current)

    def test_private_memory_headers_avoid_group_wording(self):
        """记忆块标题不能写「这个群 / 群友」——那是「她以为在群里」的直接来源。"""
        from newGeYi.memory.repository import MemoryRepository

        repo = MemoryRepository(self.rt.store, self.rt.config)
        repo.recall = lambda msg: {
            'facts': [], 'self': [],
            'summaries': [{'content': '昨天你们聊了晚饭吃什么', 'created_at': 0, 'id': 1}],
            'impression': {'summary': '爱开玩笑的跑团玩家'},
        }
        repo._touch = lambda data: None
        private_block = repo.build_block(make_msg('在吗', is_group=False, target=STRANGER))
        group_block = repo.build_block(make_msg('在吗', is_group=True, target='123456789'))
        self.assertIn('你和对方最近聊过什么', private_block)
        self.assertNotIn('这个群最近发生过什么', private_block)
        self.assertIn('【对方的印象卡片', private_block)
        self.assertNotIn('群友印象卡片', private_block)
        # 群聊保持原样
        self.assertIn('这个群最近发生过什么', group_block)
        self.assertIn('【群友印象卡片', group_block)

    def test_private_time_note_does_not_say_group(self):
        msg = make_msg('在吗', is_group=False, target=STRANGER)
        note = self.rt.prompt_builder.time_note(msg)
        self.assertNotIn('你们去哪啦', note)
        self.assertIn('在忙吗', note)


if __name__ == '__main__':
    unittest.main(verbosity=2)
