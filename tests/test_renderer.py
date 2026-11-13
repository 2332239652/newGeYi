# -*- encoding: utf-8 -*-
"""渲染层回归：模型的「内心草稿 / 英文前言」一个字都不许漏进群。

合成场景：回复中混入了标题、条列提纲和末尾台词。
标题与列表应剥除，末尾的自然回复应保留。

以前的规则只认 `【…】` 方括号草稿和 `<think>`，这种纯文本提纲一条都拦不住。
同时覆盖行内英文前言（`I should be straightforward. 测试开始啦！`）。

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

# 合成多行提纲样本，保留测试需要的标题、列表和末尾台词。
TAIL = '那当然啦～（得意地昂了昂下巴）测试已经准备好了！'
LEAK = (
    '测试用户：\n'
    '- 说明测试话题，并准备简短回复\n'
    '歌伊的反应：\n'
    '- 用轻松自然的语气回复\n'
    '- 长度约 40-60 字。一条一段。\n'
    '- 是否发立绘？可以省略。\n'
    '- 别长篇大论。不用 Markdown。' + TAIL
)


class OutlineDraftTest(unittest.TestCase):
    def setUp(self):
        self.renderer = Renderer({})

    def test_outline_draft_is_stripped(self):
        out = self.renderer._strip_thinking_process(LEAK)
        self.assertIn(TAIL, out)
        for artifact in ('测试用户', '歌伊的反应', '长度约', '是否发立绘', '一条一段'):
            self.assertNotIn(artifact, out, f'草稿残留: {artifact}')

    def test_draft_only_is_dropped(self):
        draft_only = '测试用户：\n- 夸她厉害\n歌伊的反应：\n- 长度约 40 字。一条一段。'
        self.assertEqual(self.renderer._strip_thinking_process(draft_only), '')

    def test_english_inline_preamble_is_stripped(self):
        # ① 句号收尾的合成样本。
        raw = 'I should be straightforward. 【引用消息】测试已经开始了吗？'
        out = self.renderer._strip_thinking_process(raw)
        self.assertFalse(out.startswith('I should'), out)
        self.assertIn('测试已经开始了吗？', out)

        # 冒号加引号收尾的合成样本。
        raw2 = ('Here\'s what I\'ll say: "哼哼，今天的测试很顺利！'
                '测试用户觉得这个回复怎么样～"')
        out2 = self.renderer._strip_thinking_process(raw2)
        self.assertTrue(out2.startswith('哼哼'), out2)
        self.assertNotIn("Here's what", out2)
        self.assertNotIn('"', out2)

    def test_normal_replies_untouched(self):
        samples = [
            '（歪头）这个测试通过了吗',
            '哈哈，这轮测试进行得很顺利',
            '我是说：等一下，我检查测试结果',
            '（举起测试报告）这次的检查结果已经全部准备好啦！',
            '长度约 40 字的短句。',
            'Token Stamp 的测试图已经做好了！把素材拖进边框就能检查效果啦～',
            '- 这项测试交给我！（举手）',
        ]
        for sample in samples:
            self.assertEqual(self.renderer._strip_thinking_process(sample), sample,
                             f'正常回复被误改: {sample}')

    def test_silent_mark_without_brackets(self):
        """合成静默标记覆盖裸写、括号、标点及 Markdown 修饰形式。"""
        silent = ['【歌伊忽略】', '歌伊忽略', '[歌伊忽略]', '（歌伊忽略）',
                  '【歌伊忽略】。', ' **歌伊忽略** ', '歌伊忽略！', '[NO_REPLY]', '不回复']
        for sample in silent:
            self.assertTrue(is_silent_reply(sample), f'哨兵漏判: {sample!r}')

    def test_normal_text_is_not_silent(self):
        samples = [
            '（歪头）这个测试通过了吗',
            '哈哈，这轮测试进行得很顺利',
            '歌伊忽略了这条消息',
            '你别忽略我说话啊',
            '这条我不回复你了，哼',
        ]
        for sample in samples:
            self.assertFalse(is_silent_reply(sample), f'正常回复被判成哨兵: {sample!r}')


# 合成写稿备忘回归样本，保留标签与多行格式。
PLAN_LEAK = (
    '（合成的写稿备忘），作为歌伊，可以结合测试话题回复。\n'
    '- 风格：口语、30~70字以内，自然，不换行。\n'
    '- 注意情绪立绘：惊讶或死鱼眼。\n'
    '- 构思：用简短语句回应测试话题。'
)

SESSION = SessionKey(bot_hash='hash', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='123456789')


def fake_msg() -> IncomingMessage:
    return IncomingMessage(
        session=SESSION, message_id='1', sender_id='1000000001', sender_name='测试用户',
        text='合成的测试话题',
        raw_text='', is_group=True, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


class PlanLeakTest(unittest.TestCase):
    """合成写稿备忘：检测命中后应整条丢弃。"""

    def setUp(self):
        self.renderer = Renderer({})

    def test_leaked_brief_is_detected(self):
        self.assertTrue(looks_like_plan(PLAN_LEAK), '合成样本未被识别')

    def test_leaked_brief_is_dropped_by_render(self):
        self.assertIsNone(self.renderer.render(PLAN_LEAK, fake_msg()))

    def test_single_label_line_is_detected(self):
        for sample in ('构思：简短说明测试结果',
                       '- 风格：语气轻松一点',
                       '- 注意情绪立绘：惊讶',
                       '**字数：** 40 字',
                       '方向：讨论测试流程'):
            self.assertTrue(looks_like_plan(sample), f'漏判: {sample!r}')

    def test_instruction_voice_without_labels(self):
        """没有提纲标签、但通篇是「作为歌伊，可以…」的指令口吻，也算泄露。"""
        sample = '作为歌伊，可以结合测试结果说明两句'
        self.assertTrue(looks_like_plan(sample))
        self.assertIsNone(self.renderer.render(sample, fake_msg()))

    def test_normal_replies_still_go_through(self):
        samples = [
            '（探头）测试用户，结果已经准备好了～',
            '这张测试图的风格：配色明亮，线条简洁',
            '长度约 40 字的短句。',
            '我是说：等一下，我检查测试结果',
            '- 这项测试交给我！（举手）',
            '见解：这项测试的步骤很清楚',
            '（歪头）构思这一步要先检查测试目标吧',
        ]
        for sample in samples:
            self.assertFalse(looks_like_plan(sample), f'正常台词被误判: {sample!r}')
            rendered = self.renderer.render(sample, fake_msg())
            self.assertIsNotNone(rendered, f'正常台词被整条丢掉: {sample!r}')
            self.assertTrue(rendered.text.strip(), f'正常台词被清空: {sample!r}')


class AtCodeTest(unittest.TestCase):
    """数字提及回归：合成示例 `【@123456789】你好` 应显示昵称。"""

    def setUp(self):
        self.renderer = Renderer({})

    # ---- 入参：OP / CQ 的 @ 码 → @名字 ----
    def test_op_at_uses_name(self):
        self.assertEqual(op_codes_to_plain('[OP:at,id=123456789,name=测试用户] 你好'),
                         '@测试用户 你好')

    def test_cq_at_uses_name(self):
        self.assertEqual(op_codes_to_plain('[CQ:at,qq=123,name=测试用户] 在吗'),
                         '@测试用户 在吗')

    def test_at_without_name_is_not_a_number(self):
        self.assertEqual(op_codes_to_plain('[OP:at,id=123] 在吗'), '@某用户 在吗')
        self.assertEqual(op_codes_to_plain('[OP:at,id=123]', names={'123': '测试用户'}), '@测试用户')

    def test_quoted_message_still_tagged(self):
        self.assertEqual(
            op_codes_to_plain('[OP:reply,id=1][OP:at,id=1000000001,name=测试用户] 继续'),
            '【引用消息】@测试用户 继续')

    # ---- 输出：模型自己写的【@数字】要收口 ----
    def test_render_numeric_at_becomes_sender_name(self):
        out = self.renderer.render('【@1000000001】测试完成了', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@测试用户 测试完成了')

    def test_render_unknown_numeric_at(self):
        out = self.renderer.render('【@999999】？', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@某用户？')

    def test_render_bare_numeric_at(self):
        out = self.renderer.render('@1000000001 测试完成了', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@测试用户 测试完成了')

    def test_render_bracket_at_with_name(self):
        out = self.renderer.render('【@测试用户】好呀', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '@测试用户 好呀')

    def test_plain_text_is_untouched(self):
        out = self.renderer.render('（探头）测试就绪！', fake_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '（探头）测试就绪！')


if __name__ == '__main__':
    unittest.main(verbosity=2)
