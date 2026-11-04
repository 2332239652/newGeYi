# -*- encoding: utf-8 -*-
"""回复审计与码剥离（离线）。

2026-11-03 外部审查发现两处"看起来防住了、实际没防住"：

1. **凭据规则用了 `\\b`**：Python 的 `\\w` 包含汉字，而「的api_key=」里「的」和「a」
   之间没有词边界 → 规则整条失效，只有空格分隔才拦得住。中文群里这条基本等于没有。
2. **OP/CQ 一遍过**：模型输出 `[O[CQ:x]P:at,id=all]`，先剥 OP 再剥 CQ，
   内层拿掉后会**重新拼出一个合法 OP 码**——一遍过就漏进群里。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_audit_tests_'))

from newGeYi.config import DEFAULT_CONFIG  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.presentation import renderer as R  # noqa: E402

SESSION = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='20002')


def make_msg(text='随便聊聊', sender='30003'):
    return IncomingMessage(
        session=SESSION, message_id='1', sender_id=sender, sender_name='群友',
        text=text, raw_text=text, is_group=True, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


class AuditTest(unittest.TestCase):
    def test_credentials_without_word_boundary_are_caught(self):
        """中文紧贴英文时也必须命中（这是真实中文群里的写法）。"""
        cases = [
            '我的api_key=abcdef123456',
            '密钥是sk-abcdefghijklmnop1234',
            'access_token:xyz',
            '密码＝12345678',
            'password=12345678',
        ]
        for text in cases:
            self.assertTrue(R.audit_reply(text), '漏掉了：%r' % text)

    def test_normal_chinese_is_not_false_positive(self):
        cases = [
            '我今天去看了场电影，挺好看的',
            '这个 API 挺快的，延迟只有 20ms',
            'sk 是滑雪的意思吧',
            '密码忘了怎么办（开玩笑的）',
        ]
        for text in cases:
            self.assertEqual(R.audit_reply(text), '', '误伤：%r' % text)

    def test_paths_still_caught(self):
        self.assertTrue(R.audit_reply('配置在 C:\\frps\\frps.toml'))
        self.assertTrue(R.audit_reply('文件在 /home/geyi/config.json'))


class StripTest(unittest.TestCase):
    def test_nested_cq_inside_op_is_stripped_to_stable(self):
        """`[O[CQ:x]P:at,id=all]` 剥一遍会拼出合法的 `[OP:at,id=all]`，必须剥到稳定。"""
        renderer = R.Renderer(dict(DEFAULT_CONFIG))
        out = renderer.render('你好[O[CQ:x]P:at,id=all]呀', make_msg())
        self.assertIsNotNone(out)
        self.assertNotIn('[OP:', out.text)
        self.assertNotIn('[CQ:', out.text)
        self.assertNotIn('OP:at', out.text)
        self.assertIn('你好', out.text)

    def test_plain_text_is_untouched(self):
        renderer = R.Renderer(dict(DEFAULT_CONFIG))
        out = renderer.render('今天的风有点大呢', make_msg())
        self.assertIsNotNone(out)
        self.assertEqual(out.text, '今天的风有点大呢')


if __name__ == '__main__':
    unittest.main(verbosity=2)
