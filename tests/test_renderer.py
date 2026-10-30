# -*- encoding: utf-8 -*-
"""渲染层回归：模型的「内心草稿 / 英文前言」一个字都不许漏进群。

背景（2026-10-30 线上）：小群 1130455496 收到一条完整的条列提纲——

    赛博刀子：
    - 夸歌伊厉害，但安抚熠暗…
    歌伊的反应：
    - 长度约 40-60 字。一条一段。
    - 别长篇大论。不用 Markdown。那当然啦～（得意地昂了昂下巴）…

以前的规则只认 `【…】` 方括号草稿和 `<think>`，这种纯文本提纲一条都拦不住。
同一天还漏过一次行内英文前言（`I should be straightforward. 诶？…`）。

跑法（在 plugin/app 目录下）：

    python -m unittest discover -s newGeYi/tests -t . -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

# 与 test_config 一样：必须在 import newGeYi 之前把状态目录指到临时目录
os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_render_tests_'))

from newGeYi.presentation.renderer import Renderer  # noqa: E402

# 线上原样（去掉了群号等无关内容）
LEAK = (
    '赛博刀子：\n'
    '- 夸歌伊厉害，但安抚熠暗“你们的赛道暂时不相交”\n'
    '- 说明刀子在玩梗/在解释两人的角色定位（一个修女AI，一个MC凋零骷髅人设/或者别的Bot/跑团角色）\n'
    '歌伊的反应：\n'
    '- 听到刀子夸自己很受用，骄傲\n'
    '- 对“赛道不相交”好奇或调侃\n'
    '- “赛道？什么赛道嘛，搞得像我们要比赛跑一样～不过放心啦…”\n'
    '- 语气轻松自然，符合性格。\n'
    '- 长度约 40-60 字。一条一段。\n'
    '- 是否发立绘？之前还没发过，可以来一个或者不发。先保持文字自然。也可以加一个。\n'
    '- 别长篇大论。不用 Markdown。那当然啦～（得意地昂了昂下巴）不过什么赛道不赛道的，'
    '听起来像要赛跑一样！放心啦，只要熠暗不突然拔出大剑砍我，我的净化术绝不乱扔滴'
)

TAIL = '那当然啦～（得意地昂了昂下巴）不过什么赛道不赛道的，听起来像要赛跑一样！'


class OutlineDraftTest(unittest.TestCase):
    def setUp(self):
        self.renderer = Renderer({})

    def test_outline_draft_is_stripped(self):
        out = self.renderer._strip_thinking_process(LEAK)
        self.assertIn(TAIL, out)
        for artifact in ('赛博刀子', '歌伊的反应', '长度约', '是否发立绘', '一条一段'):
            self.assertNotIn(artifact, out, f'草稿残留: {artifact}')

    def test_draft_only_is_dropped(self):
        draft_only = '赛博刀子：\n- 夸她厉害\n歌伊的反应：\n- 长度约 40 字。一条一段。'
        self.assertEqual(self.renderer._strip_thinking_process(draft_only), '')

    def test_english_inline_preamble_is_stripped(self):
        # ① 句号收尾（2026-10-30 小群）
        raw = 'I should be straightforward. 【引用消息】诶？今天是万圣节吗？'
        out = self.renderer._strip_thinking_process(raw)
        self.assertFalse(out.startswith('I should'), out)
        self.assertIn('诶？今天是万圣节吗？', out)

        # ② 冒号 + 引号收尾（2026-10-30 大群 1013491196，原文照抄）
        raw2 = ('Here\'s what I\'ll say: "哼哼，这种事情不用搜也是刻在直觉里的嘛！'
                '而且歌靡难道不觉得现在的我才是最棒的吗～"')
        out2 = self.renderer._strip_thinking_process(raw2)
        self.assertTrue(out2.startswith('哼哼'), out2)
        self.assertNotIn("Here's what", out2)
        self.assertNotIn('"', out2)

    def test_normal_replies_untouched(self):
        samples = [
            '（歪头）诶？真的假的',
            '哈哈，那当然啦～今天天气怪好的',
            '我是说：你先别急，我看看骰子',
            '（得意地昂了昂下巴）不过什么赛道不赛道的，听起来像要赛跑一样！',
            '长度约 40 字的短句。',
            'Token Stamp 做圆形棋子超顺手的！直接把立绘拖进去套个框就行啦～',
            '- 这个我会！（举手）',
        ]
        for sample in samples:
            self.assertEqual(self.renderer._strip_thinking_process(sample), sample,
                             f'正常回复被误改: {sample}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
