# -*- encoding: utf-8 -*-
"""神职头衔削弱回归（2026-11 新增，防 QQ 内容风控）。

背景：v2.9.0 世界书上线后她身边全是教会设定，跟群友玩「封官过家家」时开始自称
「本教皇」「本大主教」。线上实测（turns 表 715 条回复）自称神职高位 44 条 = 6.2%，
而且集中在同一分钟连发十几条——这类「自称宗教领袖」是 QQ 内容风控最敏感的一类。

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
                     session_type='group', host_id='-', target_id='1013491196')


def fake_msg() -> IncomingMessage:
    return IncomingMessage(
        session=SESSION, message_id='1', sender_id='1000000001', sender_name='小特派员',
        text='大主教大人，今晚赏我块蛋糕吧',
        raw_text='', is_group=True, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


# 线上原样（2026-11-05，群 1013491196 那一串「本大主教」）
ONLINE_SELF_TITLE = (
    '（赞许地拍拍你的肩膀）干得太漂亮了！不愧是本大主教的得力骑士，'
    '顺便把他的牢门彻底焊死，明早草莓蛋糕端上来之前谁也不许放他出来！'
)


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
        text, hits = sanitize_religion('本大主教准了。本教皇今天心情好。')
        self.assertEqual(hits, 2)
        self.assertEqual(text, '本小姐准了。本小姐今天心情好。')


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
        for sentence in ('我在清泉镇的教会当修女，每天清晨要祈祷，',
                         '礼拜堂的彩绘玻璃是我擦的，',
                         '治愈粒子和净化术都会一点，圣职也算干过。'):
            text, hits = sanitize_religion(sentence)
            self.assertEqual(text, sentence)
            self.assertEqual(hits, 0)

    def test_plain_text_noop(self):
        text, hits = sanitize_religion('今天天气真好呀，要不要去泉边走走')
        self.assertEqual(hits, 0)
        self.assertEqual(text, '今天天气真好呀，要不要去泉边走走')

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
        reply = renderer.render('我在镇上的教会帮忙，今天晨祷差点又睡着了', fake_msg())
        self.assertIsNotNone(reply)
        self.assertIn('教会', reply.text)
        self.assertIn('晨祷', reply.text)


if __name__ == '__main__':
    unittest.main()
