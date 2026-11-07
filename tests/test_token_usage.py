# -*- encoding: utf-8 -*-
"""token 台账：总量、按用途、按群聊/私聊。

骰主 2026-11-08：「把群聊私聊、提炼、判断之类的 token 消耗都集中起来，
在『歌伊状态』里显示今天共消耗多少 token」。

这里锁三件事，都是"数字错了不会报错、只会让人做出错误判断"的那类：
  1. `total_tokens` = 输入 + 输出（不是只有输出，也不是只有输入）；
  2. 按用途拆账必须带输出——记忆提炼是输入巨大/输出很小，只看输入会把它算成头号开销；
  3. 按场景拆账能分清群聊和私聊——`kind` 分不出来（两类共用 passive）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_tokens_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.storage.database import SQLiteStore  # noqa: E402

GROUP = 'h:qq:onebot:group:1001:2001'
PRIVATE = 'h:qq:onebot:private:-:2001'


class TokenLedgerTest(unittest.TestCase):
    def setUp(self):
        # 每个测试一个独立库：`add_api_call` 按「今天」聚合，同一个文件复用会把
        # 上一个测试写进去的行算进来，最后报的错看起来像 SQL 写错了。
        self.dir = tempfile.mkdtemp(prefix='geyi_tokens_case_', dir=_SCRATCH)
        self.store = SQLiteStore(os.path.join(self.dir, 'tokens.sqlite3'))

    def add(self, kind, in_tokens, out_tokens, session_key=GROUP, ok=True, cached=0):
        self.store.add_api_call(session_key, kind, 'm', in_tokens, out_tokens,
                                120, ok, '', cached)

    def test_total_is_input_plus_output(self):
        self.add('passive', 1000, 200)
        self.add('memory_curate', 4000, 100)
        stats = self.store.today_stats()
        self.assertEqual(stats['calls'], 2)
        self.assertEqual(stats['input_tokens'], 5000)
        self.assertEqual(stats['output_tokens'], 300)
        self.assertEqual(stats['total_tokens'], 5300)

    def test_ok_and_failed_counts(self):
        self.add('passive', 100, 20)
        self.add('passive', 0, 0, ok=False)
        stats = self.store.today_stats()
        self.assertEqual(stats['ok_calls'], 1)
        self.assertEqual(stats['failed_calls'], 1)

    def test_by_kind_carries_output(self):
        self.add('memory_curate', 4000, 100)
        self.add('affection', 300, 60)
        rows = {r['kind']: r for r in self.store.today_stats_by_kind()}
        self.assertEqual(rows['memory_curate']['total_tokens'], 4100)
        self.assertEqual(rows['memory_curate']['output_tokens'], 100)
        self.assertEqual(rows['affection']['total_tokens'], 360)
        # 按合计降序：提炼虽然输出小，但合计仍然是第一
        self.assertEqual(self.store.today_stats_by_kind()[0]['kind'], 'memory_curate')

    def test_by_scope_splits_group_and_private(self):
        self.add('passive', 1000, 100, session_key=GROUP)
        self.add('passive', 500, 50, session_key=PRIVATE)
        self.add('affection', 200, 20, session_key='')            # 评审不属于任何会话
        rows = {r['scope']: r for r in self.store.today_stats_by_scope()}
        self.assertEqual(rows['group']['total_tokens'], 1100)
        self.assertEqual(rows['private']['total_tokens'], 550)
        self.assertEqual(rows['other']['total_tokens'], 220)
        self.assertEqual(sum(r['total_tokens'] for r in rows.values()), 1870)

    def test_empty_day_is_all_zero(self):
        stats = self.store.today_stats()
        self.assertEqual(stats['total_tokens'], 0)
        self.assertEqual(stats['calls'], 0)
        self.assertEqual(stats['failed_calls'], 0)
        self.assertEqual(self.store.today_stats_by_kind(), [])
        self.assertEqual(self.store.today_stats_by_scope(), [])

    def test_range_stats_accumulates(self):
        self.add('passive', 100, 10)
        span = self.store.range_stats(30)
        self.assertEqual(span['total_tokens'], 110)
        self.assertEqual(span['days'], 30)


class StatusLineTest(unittest.TestCase):
    """状态行 / 用量命令的文字回归：数字必须真的出现在那条命令里。"""

    def test_fmt_tokens_groups_thousands(self):
        from newGeYi.routing.commands import fmt_tokens
        self.assertEqual(fmt_tokens(1234567), '1,234,567')
        self.assertEqual(fmt_tokens(0), '0')
        self.assertEqual(fmt_tokens(None), '0')
        self.assertEqual(fmt_tokens('bad'), 'bad')

    def test_kind_labels_cover_real_kinds(self):
        """四个真实 kind 都要有中文名，否则状态里会露出 `affection` 这种裸标识符。"""
        from newGeYi.routing.commands import KIND_LABELS
        for kind in ('passive', 'active', 'memory_curate', 'affection'):
            self.assertIn(kind, KIND_LABELS)

    def test_status_token_line(self):
        """「歌伊状态」里那一行的实际文字：**合计在最前面**，因为骰主要的就是这个数。"""
        from newGeYi.routing.commands import CommandRouter
        stats = {'total_tokens': 754618, 'input_tokens': 704017,
                 'output_tokens': 50601, 'cached_tokens': 0}
        # _token_line 不读 self，可以直接用未绑定的方式调，省掉造一个 runtime
        line = CommandRouter._token_line(None, stats)
        self.assertIn('754,618', line, '合计必须出现且带千分位')
        self.assertIn('704,017', line)
        self.assertIn('50,601', line)
        self.assertTrue(line.startswith('今日 token: 754,618'), '合计要排最前面')

    def test_status_token_line_handles_empty_day(self):
        """零点刚过、库里还没有任何行时不能除零：命中率显示 0%，不是异常。"""
        from newGeYi.routing.commands import CommandRouter
        line = CommandRouter._token_line(None, {})
        self.assertIn('今日 token: 0', line)
        self.assertIn('0%', line)


if __name__ == '__main__':
    unittest.main()
