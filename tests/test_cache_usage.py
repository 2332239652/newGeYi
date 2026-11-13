# -*- encoding: utf-8 -*-
"""缓存命中率的口径与显示。

三件事都是线上真发生过的，而且"错了不会报错、只会让人做出错误判断"：

1. **字段名不统一**：OpenAI/智谱、DeepSeek、Anthropic、Gemini 原生各写各的，
   漏一个就是永远 0%。
2. **`input_tokens` 不含缓存**：Anthropic 协议下命中缓存时会出现「输入 43 / 缓存 5376」
   这种 12500% 的比例（历史库里有 2 行真数据）。命中率的分母必须是**含缓存的全部输入**。
3. **整数地板除法**：`cached * 100 // input` 把真实 0.28% 显示成 0%，
   于是「命中几乎总是 0」有一半是显示造成的。另外还有个别中转把
   `prompt_tokens` 与 `cached_tokens` 报成互斥（cached > prompt），总量必须相加。
"""

from __future__ import annotations

import os
import tempfile
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_cache_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.providers.llm_gateway import (  # noqa: E402
    cached_tokens_from_usage, response_usage, usage_reports_cache, usage_tokens,
)
from newGeYi.routing.commands import CommandRouter, cache_rate_text  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402


class CacheFieldTest(unittest.TestCase):
    """各家 usage 里的缓存字段都要能抓到。"""

    def test_openai_prompt_tokens_details(self):
        usage = {'prompt_tokens': 5000, 'prompt_tokens_details': {'cached_tokens': 3136}}
        self.assertEqual(cached_tokens_from_usage(usage), 3136)

    def test_openai_input_tokens_details(self):
        usage = {'input_tokens_details': {'cached_tokens': 1024}}
        self.assertEqual(cached_tokens_from_usage(usage), 1024)

    def test_deepseek_official(self):
        self.assertEqual(cached_tokens_from_usage({'prompt_cache_hit_tokens': 3200}), 3200)

    def test_anthropic_cache_read(self):
        self.assertEqual(cached_tokens_from_usage({'cache_read_input_tokens': 5376}), 5376)

    def test_gemini_native_camel(self):
        self.assertEqual(cached_tokens_from_usage({'cachedContentTokenCount': 2048}), 2048)

    def test_gemini_native_snake(self):
        self.assertEqual(cached_tokens_from_usage({'cached_content_token_count': 2048}), 2048)

    def test_gemini_under_usage_metadata(self):
        usage = {'usageMetadata': {'cachedContentTokenCount': 4096}}
        self.assertEqual(cached_tokens_from_usage(usage), 4096)

    def test_explicit_zero_is_zero(self):
        self.assertEqual(cached_tokens_from_usage({'prompt_tokens_details': {'cached_tokens': 0}}), 0)

    def test_garbage_values_do_not_raise(self):
        for usage in ({'cached_tokens': 'N/A'}, {}, [], None, {'prompt_tokens_details': 'x'}):
            self.assertEqual(cached_tokens_from_usage(usage), 0)

    def test_response_usage_falls_back_to_gemini_metadata(self):
        """原生 Gemini 响应把用量放在 usageMetadata，不在 usage。"""
        data = {'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 2}}
        self.assertEqual(response_usage(data)['promptTokenCount'], 10)
        self.assertEqual(response_usage({}), {})


class CacheReportedTest(unittest.TestCase):
    """「显式 0」= 渠道有上报但没命中；「没有字段」= 渠道没透传。"""

    def test_explicit_zero_counts_as_reported(self):
        self.assertTrue(usage_reports_cache({'prompt_tokens_details': {'cached_tokens': 0}}))
        self.assertTrue(usage_reports_cache({'cached_tokens': 0}))

    def test_absent_fields_mean_not_reported(self):
        self.assertFalse(usage_reports_cache({'prompt_tokens': 900, 'completion_tokens': 30}))
        self.assertFalse(usage_reports_cache({}))
        self.assertFalse(usage_reports_cache(None))

    def test_nested_metadata_counts_as_reported(self):
        self.assertTrue(usage_reports_cache({'usageMetadata': {'cachedContentTokenCount': 0}}))


class UsageTotalTest(unittest.TestCase):
    """总输入（命中率的分母）必须"含缓存"，否则比例会撒谎。"""

    def test_anthropic_total_adds_cache_read_and_write(self):
        usage = {'input_tokens': 43, 'output_tokens': 113,
                 'cache_read_input_tokens': 5376, 'cache_creation_input_tokens': 100}
        total, output, cached = usage_tokens(usage, 'anthropic')
        self.assertEqual(total, 43 + 5376 + 100)
        self.assertEqual(cached, 5376)
        self.assertEqual(output, 113)
        self.assertLessEqual(cached, total, '命中数不可能超过总输入')

    def test_anthropic_without_cache_fields(self):
        self.assertEqual(usage_tokens({'input_tokens': 800, 'output_tokens': 20}, 'anthropic'),
                         (800, 20, 0))

    def test_openai_subset_keeps_prompt_total(self):
        usage = {'prompt_tokens': 10060, 'completion_tokens': 355,
                 'prompt_tokens_details': {'cached_tokens': 8048}}
        self.assertEqual(usage_tokens(usage, 'openai'), (10060, 355, 8048))

    def test_openai_disjoint_adds_up(self):
        """实测某中转把 prompt_tokens 报成"未命中部分"：2355 / cached 3242。"""
        usage = {'prompt_tokens': 2355, 'completion_tokens': 120, 'cached_tokens': 3242}
        total, output, cached = usage_tokens(usage, 'openai')
        self.assertEqual(total, 2355 + 3242)
        self.assertEqual(cached, 3242)
        self.assertEqual(output, 120)

    def test_gemini_native_counts(self):
        usage = {'promptTokenCount': 5000, 'candidatesTokenCount': 200,
                 'cachedContentTokenCount': 3000}
        self.assertEqual(usage_tokens(usage, 'openai'), (5000, 200, 3000))

    def test_missing_usage_is_all_zero(self):
        for usage in ({}, None, [], 'x'):
            self.assertEqual(usage_tokens(usage, 'openai'), (0, 0, 0))

    def test_garbage_numbers_do_not_raise(self):
        self.assertEqual(usage_tokens({'prompt_tokens': 'N/A', 'completion_tokens': None}), (0, 0, 0))


class CacheRateTextTest(unittest.TestCase):
    """显示层：小数、封顶、除零。"""

    def test_below_one_percent_keeps_one_decimal(self):
        self.assertEqual(cache_rate_text(28, 10000), '0.3%')
        self.assertEqual(cache_rate_text(55799, 19716501), '0.3%')

    def test_very_small_is_stated_as_less_than(self):
        self.assertEqual(cache_rate_text(1, 10000), '<0.1%')

    def test_tens_are_rounded_down_not_up(self):
        self.assertEqual(cache_rate_text(9995, 10000), '99%')
        self.assertEqual(cache_rate_text(120, 1000), '12%')

    def test_never_exceeds_one_hundred(self):
        self.assertEqual(cache_rate_text(2000, 1000), '100%')

    def test_zero_and_empty(self):
        self.assertEqual(cache_rate_text(0, 1000), '0%')
        self.assertEqual(cache_rate_text(0, 0), '0%')
        self.assertEqual(cache_rate_text(5, 0), '0%')


class StatusLineCacheTest(unittest.TestCase):
    """「歌伊状态」里的实际文字。"""

    @staticmethod
    def line(**kwargs):
        today = {'total_tokens': 10100, 'input_tokens': 10000, 'output_tokens': 100,
                 'cached_tokens': 0, 'cache_reported_calls': 0}
        today.update(kwargs)
        return CommandRouter._token_line(None, today)

    def test_fraction_is_not_swallowed_by_floor_division(self):
        self.assertIn('0.3%', self.line(cached_tokens=28, cache_reported_calls=10))

    def test_unreported_channel_is_named(self):
        line = self.line(cached_tokens=0, cache_reported_calls=0)
        self.assertIn('未上报缓存字段', line)

    def test_real_miss_is_not_blamed_on_channel(self):
        line = self.line(cached_tokens=0, cache_reported_calls=80)
        self.assertIn('0%', line)
        self.assertNotIn('未上报', line)

    def test_hit_hides_the_hint(self):
        line = self.line(cached_tokens=4000, cache_reported_calls=80)
        self.assertIn('40%', line)
        self.assertNotIn('未上报', line)


class StoreCacheReportedTest(unittest.TestCase):
    """新列要真的落库、能被 today_stats 统计到（老库靠 _migrate 补列）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='geyi_cache_case_', dir=_SCRATCH)
        self.store = SQLiteStore(os.path.join(self.dir, 'cache.sqlite3'))

    def add(self, cached=0, reported=False):
        self.store.add_api_call('h:qq:onebot:group:1:2', 'passive', 'm', 1000, 100,
                                120, True, '', cached, reported)

    def test_reported_flag_is_counted(self):
        self.add(cached=300, reported=True)
        self.add(cached=0, reported=True)
        self.add(cached=0, reported=False)
        stats = self.store.today_stats()
        self.assertEqual(stats['cache_reported_calls'], 2)
        self.assertEqual(stats['cached_tokens'], 300)
        self.assertEqual(stats['input_tokens'], 3000)

    def test_default_is_not_reported(self):
        self.add()
        self.assertEqual(self.store.today_stats()['cache_reported_calls'], 0)

    def test_old_database_gets_the_column_added(self):
        """生产库是"还没有这一列"的老库，升级时必须自动补列且不丢旧行。"""
        import sqlite3

        path = os.path.join(self.dir, 'old.sqlite3')
        raw = sqlite3.connect(path)
        raw.execute(
            'CREATE TABLE api_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, '
            'ts INTEGER NOT NULL, session_key TEXT DEFAULT "", kind TEXT DEFAULT "", '
            'model TEXT DEFAULT "", in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0, '
            'latency_ms INTEGER DEFAULT 0, ok INTEGER DEFAULT 1, error_code TEXT DEFAULT "")'
        )
        raw.execute("INSERT INTO api_calls(day, ts, session_key, kind, model, in_tokens, out_tokens, "
                    "latency_ms, ok, error_code) VALUES(date('now','localtime'), 1, 'old', 'passive', "
                    "'m', 500, 20, 10, 1, '')")
        raw.commit()
        raw.close()

        store = SQLiteStore(path)          # 打开即迁移
        cols = {row[1] for row in store._conn().execute('PRAGMA table_info(api_calls)')}
        self.assertIn('cache_reported', cols)
        self.assertEqual(store.today_stats()['calls'], 1, '补列不能丢历史行')
        self.assertEqual(store.today_stats()['cache_reported_calls'], 0, '老行按未上报算')
        store.add_api_call('h:qq:onebot:group:1:2', 'passive', 'm', 800, 40, 10, True, '', 300, True)
        stats = store.today_stats()
        self.assertEqual(stats['cached_tokens'], 300)
        self.assertEqual(stats['cache_reported_calls'], 1)


if __name__ == '__main__':
    unittest.main()
