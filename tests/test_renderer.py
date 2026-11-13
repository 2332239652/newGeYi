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

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.presentation.renderer import (  # noqa: E402
    Renderer,
    is_silent_reply,
    looks_like_plan,
    op_codes_to_plain,
)

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

        # ② 冒号 + 引号收尾（2026-10-30 大群 9000000002，原文照抄）
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

    def test_silent_mark_without_brackets(self):
        """内部哨兵被裸写出去：线上真发过 11 次 `歌伊忽略`（10-30~10-31，4 个群）。"""
        silent = ['【歌伊忽略】', '歌伊忽略', '[歌伊忽略]', '（歌伊忽略）',
                  '【歌伊忽略】。', ' **歌伊忽略** ', '歌伊忽略！', '[NO_REPLY]', '不回复']
        for sample in silent:
            self.assertTrue(is_silent_reply(sample), f'哨兵漏判: {sample!r}')

    def test_normal_text_is_not_silent(self):
        samples = [
            '（歪头）诶？真的假的',
            '哈哈，那当然啦～今天天气怪好的',
            '歌伊忽略了这条消息',
            '你别忽略我说话啊',
            '这条我不回复你了，哼',
        ]
        for sample in samples:
            self.assertFalse(is_silent_reply(sample), f'正常回复被判成哨兵: {sample!r}')


# 2026-11-01 线上原样（小群 957309761，B 站愚人节剧情视频触发的主动搭话）：
# 模型把「写给自己的写稿备忘」整段发了出去
PLAN_LEAK = (
    '（或者顺着刚才聊的边狱话题发出来的），作为歌伊，可以结合愚人节剧情被创飞的梗、'
    '或者画风突变女仆卡戎/马娘那些离谱镜像世界来吐槽或搭话。\n'
    '- 风格：口语、30~70字以内，自然，不换行，可带一个动作或[EMOTE]。\n'
    '- 注意情绪立绘：惊讶/死鱼眼/晕乎乎等。选“惊讶”或“死鱼眼”都很贴切。\n'
    '- 构思：这个愚人节剧情简直精神污染啊，看完感觉整个人都要被卡戎的车来回碾两遍了'
)

SESSION = SessionKey(bot_hash='hash', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='957309761')


def fake_msg() -> IncomingMessage:
    return IncomingMessage(
        session=SESSION, message_id='1', sender_id='1000000001', sender_name='赛博刀子',
        text='【[零协汉化]仲春夜之梦——2023边狱公司愚人节特别活动剧情】',
        raw_text='', is_group=True, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


class PlanLeakTest(unittest.TestCase):
    """模型把「写稿备忘」当台词发进群——线上漏过一次，整条不许发。"""

    def setUp(self):
        self.renderer = Renderer({})

    def test_leaked_brief_is_detected(self):
        self.assertTrue(looks_like_plan(PLAN_LEAK), '线上那条原样没认出来')

    def test_leaked_brief_is_dropped_by_render(self):
        self.assertIsNone(self.renderer.render(PLAN_LEAK, fake_msg()))

    def test_single_label_line_is_detected(self):
        for sample in ('构思：这剧情太创了',
                       '- 风格：萌一点',
                       '- 注意情绪立绘：惊讶',
                       '**字数：** 40 字',
                       '方向：吐槽愚人节剧情'):
            self.assertTrue(looks_like_plan(sample), f'漏判: {sample!r}')

    def test_instruction_voice_without_labels(self):
        """没有提纲标签、但通篇是「作为歌伊，可以…」的指令口吻，也算泄露。"""
        sample = '作为歌伊，可以结合刚才的话题吐槽两句'
        self.assertTrue(looks_like_plan(sample))
        self.assertIsNone(self.renderer.render(sample, fake_msg()))

    def test_normal_replies_still_go_through(self):
        samples = [
            '（探头）在呢在呢！主人怎么啦～',
            '这首歌的风格：很复古诶，听着像老动画',
            '长度约 40 字的短句。',
            '我是说：你先别急，我看看骰子',
            '- 这个我会！（举手）',
            '见解：这剧情就是精神污染啊',
            '（歪头）构思这种东西不是应该先告诉我嘛',
        ]
        for sample in samples:
            self.assertFalse(looks_like_plan(sample), f'正常台词被误判: {sample!r}')
            rendered = self.renderer.render(sample, fake_msg())
            self.assertIsNotNone(rendered, f'正常台词被整条丢掉: {sample!r}')
            self.assertTrue(rendered.text.strip(), f'正常台词被清空: {sample!r}')


class AtCodeTest(unittest.TestCase):
    """@ 不许再变成一串数字。

    2026-11-04 线上原话：`【@2309394954】我刚才明明讲了流血电弧和毒震波呀！`
    —— 模型是照着上下文里 `【@<id>】` 的旧格式学的：`op_codes_to_plain` 以前把
    `[OP:at,id=…,name=梦语熙]` 里的昵称丢掉，只吐 id。
    """

    def setUp(self):
        self.renderer = Renderer({})

    # ---- 入参：OP / CQ 的 @ 码 → @名字 ----
    def test_op_at_uses_name(self):
        self.assertEqual(op_codes_to_plain('[OP:at,id=2933419998,name=歌伊] 你好'),
                         '@歌伊 你好')

    def test_cq_at_uses_name(self):
        self.assertEqual(op_codes_to_plain('[CQ:at,qq=123,name=小明] 在吗'),
                         '@小明 在吗')

    def test_at_without_name_is_not_a_number(self):
        self.assertEqual(op_codes_to_plain('[OP:at,id=123] 在吗'), '@某用户 在吗')
        self.assertEqual(op_codes_to_plain('[OP:at,id=123]', names={'123': '阿花'}), '@阿花')

    def test_quoted_message_still_tagged(self):
        self.assertEqual(
            op_codes_to_plain('[OP:reply,id=1][OP:at,id=1000000001,name=测试用户] 继续'),
            '【引用消息】@测试用户 继续')

    # ---- 输出：模型自己写的【@数字】要收口 ----
    def test_render_numeric_at_becomes_sender_name(self):
        out = self.renderer.render('【@1000000001】我错了', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@赛博刀子 我错了')

    def test_render_unknown_numeric_at(self):
        out = self.renderer.render('【@999999】？', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@某用户？')

    def test_render_bare_numeric_at(self):
        out = self.renderer.render('@1000000001 我错了', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@赛博刀子 我错了')

    def test_render_bracket_at_with_name(self):
        out = self.renderer.render('【@赛博刀子】好呀', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@赛博刀子 好呀')

    def test_plain_text_is_untouched(self):
        out = self.renderer.render('（探头）在呢！', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '（探头）在呢！')


if __name__ == '__main__':
    unittest.main(verbosity=2)
