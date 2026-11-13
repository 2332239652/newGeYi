# -*- encoding: utf-8 -*-
"""Prompt 构建的回归测试（离线、无网络、不碰线上状态）。

专门锁 2026-11-01 那次「只回第一句，之后全哑」的故障：
`strip_image_marks()` 用到的 `IMAGE_MARK_RE` 在回滚时被弄丢，只要上下文里**有历史**
就抛 `NameError`，而重载后第一句话的旁观窗口是空的，所以永远只有第一句能回。

⚠️ 这就是为什么下面的集成测试**必须塞历史进去**：只测空上下文的话，这个 bug 一直测不出来
（当时离线复现「通过」就是这么被骗过去的）。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

_SCRATCH = tempfile.mkdtemp(prefix='geyi_prompt_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.context import prompt_builder as PB  # noqa: E402
from newGeYi.context.prompt_builder import strip_image_marks  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402

SESSION = SessionKey(bot_hash='hash', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='20002')


def make_msg(text: str, seq: int, sender_id='30003', sender_name='群友') -> IncomingMessage:
    return IncomingMessage(
        session=SESSION, message_id=str(seq), sender_id=sender_id, sender_name=sender_name,
        text=text, raw_text=text, is_group=True, mentioned_bot=False,
        event=None, proc=None, arrival_seq=seq, sender_is_bot=False,
    )


class ImageMarkTest(unittest.TestCase):
    def test_constant_exists(self):
        """常量真的定义了——以前它整个不见了，只有用它的时候才炸。"""
        self.assertTrue(hasattr(PB, 'IMAGE_MARK_RE'))
        self.assertIsInstance(PB.IMAGE_MARK_RE, re.Pattern)

    def test_strips_all_placeholder_shapes(self):
        cases = {
            '【图片】': '',
            '【图片：立绘】': '',
            '【图片:立绘】': '',
            '（图片）': '',
            '【表情】': '',
            '[发送了表情包: 开心]': '',
            '[OP:image,file=abc.jpg,url=https://x]': '',
            '[CQ:image,file=abc.image]': '',
            '【图片】（附图）': '（附图）',
            '看看这个【图片】好看吗': '看看这个好看吗',
        }
        for raw, expected in cases.items():
            self.assertEqual(strip_image_marks(raw), expected, f'输入 {raw!r}')

    def test_plain_text_untouched(self):
        for text in ('歌伊今天好可爱', '【资料】这是正文', '100% 好评'):
            self.assertEqual(strip_image_marks(text), text)


class PromptWithHistoryTest(unittest.TestCase):
    """有历史时 build() 必须正常返回（这一条就是当时缺的）。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')

    def test_build_does_not_raise_with_history(self):
        live = self.rt.live_context
        live.append_user(make_msg('【图片】', seq=1))
        live.append_user(make_msg('刚才那张图好好笑', seq=2))
        live.append_assistant(SESSION.as_text(), 3, '（歪头）哪里好笑呀～')
        live.append_user(make_msg('歌伊你看', seq=4))

        messages = self.rt.prompt_builder.build(make_msg('歌伊', seq=5), is_active=False)

        self.assertTrue(messages)
        self.assertEqual(messages[0]['role'], 'system')
        parts = []
        for message in messages:
            content = message['content']
            if isinstance(content, list):
                parts.extend(str(part.get('text', '')) for part in content if isinstance(part, dict))
            else:
                parts.append(str(content))
        joined = '\n'.join(parts)
        # 历史里的图片占位痕迹必须被清掉，不能留给模型模仿
        self.assertNotIn('【图片】', joined)
        self.assertIn('刚才那张图好好笑', joined)

    def test_build_active_does_not_raise_with_history(self):
        live = self.rt.live_context
        live.append_user(make_msg('【图片】【图片】', seq=1))
        messages = self.rt.prompt_builder.build(make_msg('随便聊聊', seq=2), is_active=True)
        self.assertTrue(messages)


class ActiveInstructionLevelTest(unittest.TestCase):
    """主动搭话的三档强度（v2.8.9）：L0 严格 / L1 中性 / L2 软化。

    只有一句硬约束：**三档都必须提到 SKIP 标记**，否则模型不肯说话时没有出口，
    会退化成硬凑一句（这比不说话更糟）。
    """

    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')

    def test_all_levels_keep_the_skip_marker(self):
        for level in (0, 1, 2):
            self.assertIn(PB.SKIP_ACTIVE, PB.active_instruction(level))

    def test_levels_get_looser(self):
        strict = PB.active_instruction(0)
        neutral = PB.active_instruction(1)
        eager = PB.active_instruction(2)
        self.assertIn('大多数群聊消息都不需要你出现', strict)
        self.assertNotIn('大多数群聊消息都不需要你出现', neutral)
        self.assertIn('很想开口', eager)
        # L0 是回放对比的对照组，一个字都不许动
        self.assertTrue(strict.startswith('你正在旁观群聊，没有人点名找你'))

    def test_level_clamped(self):
        self.assertEqual(PB.active_instruction(-3), PB.active_instruction(0))
        self.assertEqual(PB.active_instruction(99), PB.active_instruction(2))
        self.assertEqual(PB.active_instruction('x'), PB.active_instruction(0))

    def test_build_uses_selected_level(self):
        builder = self.rt.prompt_builder
        system = builder.build(make_msg('随便聊聊', seq=3), is_active=True,
                               active_level=2)[0]['content']
        self.assertIn('很想开口', system)
        self.assertNotIn('大多数群聊消息都不需要你出现', system)
        passive = builder.build(make_msg('随便聊聊', seq=4), is_active=False)[0]['content']
        self.assertNotIn('很想开口', passive)

    def test_anti_fabrication_clause_and_two_assistant_turns_in_recent_texts(self):
        from newGeYi.memory.worldbook import set_story_mode

        self.rt.config.update({'worldbook_enabled': True})
        builder = self.rt.prompt_builder
        uid = '888001'
        priv_session = SessionKey(
            bot_hash='hash', platform='qq', sdk='onebot',
            session_type='private', host_id='-', target_id=uid,
        )
        session_key = priv_session.as_text()
        self.rt.store.add_turn(session_key, 'user', '你好呀', uid, 'Tester')
        self.rt.store.add_turn(session_key, 'assistant', '今天刚和月白一起喝了麦茶～')
        self.rt.store.add_turn(session_key, 'user', '哦？真的吗', uid, 'Tester')
        self.rt.store.add_turn(session_key, 'assistant', '嗯嗯，还在镇口聊了会儿呢！')
        priv_msg = IncomingMessage(
            session=priv_session,
            message_id='10',
            sender_id=uid,
            sender_name='Tester',
            text='她平时喜欢做什么呀？',
            raw_text='她平时喜欢做什么呀？',
            is_group=False,
            mentioned_bot=True,
            event=None,
            proc=None,
            arrival_seq=10,
            sender_is_bot=False,
        )
        self.rt.store.add_turn(session_key, 'user', priv_msg.text, uid, 'Tester')
        recent = builder._recent_texts(priv_msg, limit=6)
        # 保留最近两轮 assistant 回复，保证隔一轮简短附和后追问代词仍能找到「月白」锚点
        self.assertIn('今天刚和月白一起喝了麦茶～', recent)
        self.assertIn('嗯嗯，还在镇口聊了会儿呢！', recent)

        set_story_mode(self.rt.store, session_key, uid, True)
        sys_prompt = builder.build(priv_msg, is_active=False)[0]['content']
        self.assertIn('严禁擅自扩写或临场编造世界书里没写的异世界设定', sys_prompt)
        self.assertIn('严禁为了聊得尽兴而自己编造新地名', sys_prompt)


if __name__ == '__main__':
    unittest.main(verbosity=2)
