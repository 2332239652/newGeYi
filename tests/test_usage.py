# -*- encoding: utf-8 -*-
"""用量口径的回归：缓存命中 token 要从各家 usage 方言里正确挖出来。

背景（2026-10-31 实测）：同一个前缀连发两次，
- 智谱 glm-5.3-flash：第二次 `prompt_tokens_details.cached_tokens = 3136/3143`（自动缓存生效）
- ginka 的 gemini-3.8-flash：usage 里连字段都没有（透传丢了，等于拿不到折扣）

也就是说「缓存有没有命中」只能靠记录 usage 观察，不能靠猜；
而观察的前提是解析得对，所以这里把三种方言都钉住。

跑法（在 plugin/app 目录下）：

    python -m unittest discover -s newGeYi/tests -t . -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_usage_tests_'))

from newGeYi.providers.llm_gateway import cached_tokens_from_usage  # noqa: E402


class CachedTokensTest(unittest.TestCase):
    def test_openai_style(self):
        """OpenAI / 智谱：prompt_tokens_details.cached_tokens"""
        usage = {'prompt_tokens': 3143, 'completion_tokens': 3,
                 'prompt_tokens_details': {'cached_tokens': 3136}}
        self.assertEqual(cached_tokens_from_usage(usage), 3136)

    def test_deepseek_style(self):
        """DeepSeek：prompt_cache_hit_tokens"""
        self.assertEqual(cached_tokens_from_usage({'prompt_cache_hit_tokens': 1024}), 1024)

    def test_anthropic_style(self):
        """Anthropic 协议（本地 gemini 反代）：cache_read_input_tokens"""
        self.assertEqual(cached_tokens_from_usage({'cache_read_input_tokens': 2048}), 2048)

    def test_no_cache_fields(self):
        """渠道不透传缓存字段时必须是 0，不能抛异常、也不能瞎报。"""
        for usage in (
            {'prompt_tokens': 3745, 'completion_tokens': 141},   # ginka 的 gemini 实测就长这样
            {'prompt_tokens_details': None},
            {'prompt_tokens_details': {'cached_tokens': 0}},
            {},
            None,
        ):
            self.assertEqual(cached_tokens_from_usage(usage), 0)

    def test_garbage_value(self):
        """脏值不许把整条调用记录带崩。"""
        self.assertEqual(cached_tokens_from_usage({'cached_tokens': 'N/A'}), 0)


if __name__ == '__main__':
    unittest.main()
