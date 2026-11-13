# -*- encoding: utf-8 -*-
"""神职头衔削弱回归：使用合成文本验证替换与保留规则。

合成场景：角色在带有教会背景的对话中自称
「本教皇」「本大主教」。测试同时覆盖多个头衔、多个命中和开关关闭，
确认替换次数、替换结果以及渲染链路中的最终文本。

口径必须钉死：**只削头衔，不删背景**。修女／教会／祈祷／礼拜堂／净化术一个字都不许动，
那是她的职业与住处；去掉就不是歌伊了。所以这里的断言有一半是「不许改」。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_religion_tests_'))

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.presentation.renderer import (  # noqa: E402
    Renderer,
    sanitize_religion,
)

SESSION = SessionKey(bot_hash='hash', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='123456789')


def fake_msg() -> IncomingMessage:
    return IncomingMessage(
        session=SESSION, message_id='1', sender_id='1000000001', sender_name='测试用户',
        text='大主教大人，请检查骑士训练的测试报告',
        raw_text='', is_group=True, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


# 合成自称样本，保留本大主教和得力骑士以验证替换结果。
ONLINE_SELF_TITLE = '本大主教的得力骑士，请完成这轮合成测试。'


class SelfTitleTest(unittest.TestCase):
    """她自称神职高位：一律换成世俗自称，中二感保留。"""

    def test_all_self_titles_replaced(self):
        for raw, want in (('本大主教', '本小姐'), ('本教皇', '本小姐'),
                          ('本主教', '本小姐'), ('本圣女', '本小姐'),
                          ('本祭司', '本小姐'), ('本先知', '本小姐')):
            text, hits = sanitize_religion(f'{raw}宣布一件事')
            self.assertEqual(text, f'{want}宣布一件事', f'{raw} 没换掉')
            self.assertEqual(hits, 1)

    def test_online_sample(self):
        text, hits = sanitize_religion(ONLINE_SELF_TITLE)
        self.assertEqual(hits, 1)
        self.assertNotIn('本大主教', text)
        self.assertIn('本小姐的得力骑士', text)

    def test_multiple_hits_counted(self):
        text, hits = sanitize_religion('本大主教开始测试。本教皇检查结果。')
        self.assertEqual(hits, 2)
        self.assertEqual(text, '本小姐开始测试。本小姐检查结果。')


class AddressTitleTest(unittest.TestCase):
    """她称呼对方「XX大人」：头衔摘掉，敬称留着。"""

    def test_address_titles(self):
        for raw in ('大主教大人', '教皇大人', '主教大人', '圣女大人', '祭司大人'):
            text, hits = sanitize_religion(f'{raw}晚安啦')
            self.assertEqual(text, '大人晚安啦', f'{raw} 没换掉')
            self.assertEqual(hits, 1)

    def test_longer_pattern_wins(self):
        """`大主教大人` 包含 `主教大人`，长的必须在前，否则会替成「大大人」。"""
        text, _ = sanitize_religion('大主教大人')
        self.assertEqual(text, '大人')
        self.assertNotIn('大大', text)


class BackgroundKeptTest(unittest.TestCase):
    """人设底线：教会的底色一个都不许删。"""

    def test_church_vocabulary_untouched(self):
        for sentence in ('测试故事中的修女在教会整理祈祷手册，',
                         '测试场景里的礼拜堂陈列着彩绘玻璃，',
                         '测试设定保留治愈粒子、净化术和圣职背景。'):
            text, hits = sanitize_religion(sentence)
            self.assertEqual(text, sentence)
            self.assertEqual(hits, 0)

    def test_plain_text_noop(self):
        text, hits = sanitize_religion('这轮合成测试已经准备就绪')
        self.assertEqual(hits, 0)
        self.assertEqual(text, '这轮合成测试已经准备就绪')

    def test_empty(self):
        self.assertEqual(sanitize_religion(''), ('', 0))


class RendererIntegrationTest(unittest.TestCase):
    def test_render_replaces_title(self):
        renderer = Renderer({})
        reply = renderer.render(ONLINE_SELF_TITLE, fake_msg())
        self.assertIsNotNone(reply)
        self.assertNotIn('本大主教', reply.text)
        self.assertIn('本小姐', reply.text)

    def test_switch_off_keeps_original(self):
        renderer = Renderer({'religion_guard_enabled': False})
        reply = renderer.render(ONLINE_SELF_TITLE, fake_msg())
        self.assertIsNotNone(reply)
        self.assertIn('本大主教', reply.text)

    def test_church_talk_survives_full_render(self):
        """整条渲染链走完，「教会/修女/祈祷」这些日常说法照样发得出去。"""
        renderer = Renderer({})
        reply = renderer.render('测试故事中的教会正在整理晨祷记录', fake_msg())
        self.assertIsNotNone(reply)
        self.assertIn('教会', reply.text)
        self.assertIn('晨祷', reply.text)


if __name__ == '__main__':
    unittest.main()
