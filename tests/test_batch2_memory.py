# -*- encoding: utf-8 -*-
"""F06/F07/F08: isolated SQLite, controlled gateways and injected SQL failures."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_batch2_import_'))

from newGeYi.memory.curator import MemoryCurator
from newGeYi.models import ModelResult
from newGeYi.storage.database import SQLiteStore


class ControlledGateway:
    def __init__(self, data=None):
        self.data = data if data is not None else {}
        self.prompts = []
        self.callback = None

    def chat(self, request, **kwargs):
        self.prompts.append(request.messages[1]['content'])
        if self.callback:
            self.callback()
        if isinstance(self.data, str):
            return ModelResult(ok=False, error_code=self.data)
        return ModelResult(ok=True, text=json.dumps(self.data, ensure_ascii=False))

    def records(self, index=-1):
        return self.prompts[index].split('聊天记录：\n', 1)[1][:-1]


class Batch2MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_batch2_memory_')
        self.path = os.path.join(self.tmp.name, 'memory.sqlite3')
        self.store = SQLiteStore(self.path)
        self.session = 'bot:qq:onebot:group:-:100'
        self.gateway = ControlledGateway()
        self.config = {'memory_curate_known_facts': 5, 'memory_cross_group_enabled': False,
                       'memory_buffer_retention_seconds': 7200,
                       'memory_fuzzy_dedup_threshold': 1.0}
        self.curator = MemoryCurator(self.config, self.store, self.gateway)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, text, uid='1', session=None, role='user'):
        return self.store.buffer_append(session or self.session, role, text, uid, '群友')

    def snapshot(self):
        tables = ('group_summaries', 'user_facts', 'user_facts_archive', 'self_memories',
                  'user_impressions', 'user_impressions_history', 'memory_buffer', 'forget_state')
        result = {table: self.store._conn().execute('SELECT * FROM ' + table).fetchall()
                  for table in tables}
        result['cursor'] = self.curator.cursor(self.session)
        result['fragment'] = self.curator._fragment_state(self.session)
        return result

    def rich_data(self):
        return {'group_summary': '大家约定周末聚餐',
                'user_impressions': [{'user_id': '1', 'summary': '说话温和又很喜欢研究茶点'}],
                'user_facts': [{'user_id': '1', 'fact': '喜欢喝乌龙茶', 'scope': 'group',
                                'importance': 0.8, 'keywords': ['乌龙茶', '茶饮']}],
                'self_memories': [{'content': '答应周末准备点心', 'kind': 'promise'}]}

    def test_interleaved_global_ids_do_not_trim_sparse_session(self):
        first = self.append('A第一条')
        for _ in range(301):
            self.append('B流量', session='other')
        second = self.append('A第二条')
        self.store.buffer_trim(self.session, keep=300)
        self.assertEqual([row['id'] for row in self.store.buffer_fetch(self.session)], [first, second])

    def test_pending_over_soft_cap_is_retained_and_reported(self):
        for _ in range(8):
            self.append('积压')
        with self.assertLogs('newGeYi.database', level='WARNING') as logs:
            self.store.buffer_trim(self.session, keep=3)
        self.assertIn('rows=8 keep=3', logs.output[0])
        self.assertEqual(self.store.buffer_pending(self.session), 8)

    def test_trim_requires_consumption_and_retention_even_when_over_cap(self):
        ids = [self.append('原文') for _ in range(6)]
        self.curator.set_cursor(self.session, ids[3])
        self.store._conn().execute('UPDATE memory_buffer SET created_at=1 WHERE id IN (?, ?, ?)',
                                   (ids[0], ids[1], ids[4]))
        with patch('newGeYi.storage.database.time.time', return_value=10000):
            with self.assertLogs('newGeYi.database', level='WARNING'):
                self.store.buffer_trim(self.session, keep=1, retention_seconds=7200)
        self.assertEqual([r['id'] for r in self.store.buffer_fetch(self.session)], ids[2:])
        # 保留期=0只删除已消费；即使过期，后面的pending仍留着。
        self.store.buffer_trim(self.session, keep=300, retention_seconds=0)
        self.assertEqual([r['id'] for r in self.store.buffer_fetch(self.session)], ids[4:])

    def test_whole_records_budget_and_speaker_set_follow_selected_rows(self):
        first = self.append('甲' * 4400)
        second = self.append('尾部标记' + '乙' * 4400, uid='2')
        self.gateway.data = {'user_facts': [{'user_id': '2', 'fact': '未进入请求的用户事实'}]}
        result = self.curator.run_once(self.session)
        self.assertEqual(result['error'], '')
        self.assertEqual(result['facts'], 0)
        self.assertNotIn('尾部标记', self.gateway.records())
        self.assertEqual(self.curator.cursor(self.session), first)
        self.assertEqual(self.store.buffer_pending(self.session, first), 1)
        self.gateway.data = {}
        self.curator.run_once(self.session)
        self.assertIn('尾部标记', self.gateway.records())
        self.assertEqual(self.curator.cursor(self.session), second)

    def test_exact_record_budget_boundary(self):
        prefix = len(self.curator._build_records([{'content': 'x', 'role': 'user',
                                                 'sender_id': '1', 'sender_name': '群友'}])[0]) - 1
        first = self.append('甲' * (8000 - prefix))
        second = self.append('NEXT')
        self.curator.run_once(self.session)
        self.assertEqual(len(self.gateway.records()), 8000)
        self.assertEqual(self.curator.cursor(self.session), first)
        self.assertEqual(self.curator._fragment_state(self.session), (0, 0))
        self.curator.run_once(self.session)
        self.assertEqual(self.curator.cursor(self.session), second)

    def test_long_single_record_restarts_and_reassembles_without_loss(self):
        original = '头\n' + ''.join(chr(0x4e00 + i % 2000) for i in range(24000)) + '\n尾'
        first = self.append(original)
        second = self.append('NEXT_ROW', uid='2')
        rendered = self.curator._build_records(self.store.buffer_fetch(self.session, limit=1))[0]
        pieces = []
        for iteration in range(10):
            result = self.curator.run_once(self.session)
            self.assertEqual(result['error'], '')
            body = self.gateway.records()
            self.assertLessEqual(len(body), 8000)
            self.assertNotIn('NEXT_ROW', body)
            self.assertTrue(body.startswith('[记录ID='))
            pieces.append(body.split('\n', 1)[1])
            if self.curator.cursor(self.session) == first:
                break
            self.assertEqual(self.curator.cursor(self.session), 0)
            row_id, offset = self.curator._fragment_state(self.session)
            self.assertEqual(row_id, first)
            self.assertEqual(offset, sum(map(len, pieces)))
            self.store.close()
            self.store = SQLiteStore(self.path)
            self.curator = MemoryCurator(self.config, self.store, self.gateway)
        else:
            self.fail('long row did not advance within bounded chunks')
        self.assertGreater(iteration, 1)
        self.assertEqual(''.join(pieces), rendered)
        self.assertEqual(self.curator._fragment_state(self.session), (0, 0))
        self.curator.run_once(self.session)
        self.assertEqual(self.curator.cursor(self.session), second)
        self.assertIn('NEXT_ROW', self.gateway.records())

    def test_assistant_fragment_offset_survives_name_configuration_change(self):
        content = '助手原文' * 5000
        row_id = self.append(content, role='assistant')
        self.curator.run_once(self.session)
        first = self.gateway.records().split('\n', 1)[1]
        offset = self.curator._fragment_state(self.session)[1]
        self.assertEqual(offset, len(first))
        self.curator = MemoryCurator(dict(self.config, assistant_name='重启后特别长的名字'),
                                     self.store, self.gateway)
        pieces = [first]
        for _ in range(5):
            self.curator.run_once(self.session)
            pieces.append(self.gateway.records().split('\n', 1)[1])
            if self.curator.cursor(self.session) == row_id:
                break
        self.assertEqual(''.join(pieces), '[AI] ' + content)
        self.assertEqual(self.curator.cursor(self.session), row_id)

    def test_failed_fragment_retries_same_bytes_without_advancing(self):
        row_id = self.append('长' * 17000)
        self.curator.run_once(self.session)
        progress = self.curator._fragment_state(self.session)
        self.gateway.data = 'fixture_failure'
        self.assertEqual(self.curator.run_once(self.session)['error'], 'fixture_failure')
        failed = self.gateway.records()
        self.assertEqual(self.curator._fragment_state(self.session), progress)
        self.gateway.data = {}
        self.curator.run_once(self.session)
        self.assertEqual(self.gateway.records(), failed)
        self.assertGreater(self.curator._fragment_state(self.session)[1], progress[1])
        self.assertEqual(self.curator.cursor(self.session), 0)
        self.assertEqual(self.store.buffer_pending(self.session), 1)
        self.assertEqual(self.curator._fragment_state(self.session)[0], row_id)

    def test_complete_schema_rejects_bad_types_before_writes(self):
        bad_fields = [
            ('group_summary', 4), ('user_facts', 42), ('user_facts', [42]),
            ('user_facts', [{'user_id': 1, 'fact': 'x'}]),
            ('user_facts', [{'user_id': '1', 'fact': 'x', 'keywords': {'bad': 'type'}}]),
            ('user_facts', [{'user_id': '1', 'fact': 'x', 'importance': float('nan')}]),
            ('user_facts', [{'user_id': '1', 'fact': 'x', 'importance': 10 ** 1000}]),
            ('user_impressions', [{'user_id': '1', 'summary': []}]),
            ('self_memories', [{'content': 42}]), ('expired_fact_ids', '123'),
            ('expired_fact_ids', [True]), ('self_memories', None),
        ]
        self.append('原始内容')
        before = self.snapshot()
        for field, value in bad_fields:
            with self.subTest(field=field, value=value):
                data = self.rich_data()
                data[field] = value
                self.gateway.data = data
                self.assertEqual(self.curator.run_once(self.session)['error'], 'bad_schema')
                self.assertEqual(self.snapshot(), before)
        # 写入条数上限外的坏项也必须在写入第一条前发现。
        data = self.rich_data()
        data['user_facts'] *= 20
        data['user_facts'].append({'user_id': '1', 'fact': []})
        self.gateway.data = data
        self.assertEqual(self.curator.run_once(self.session)['error'], 'bad_schema')
        self.assertEqual(self.snapshot(), before)

    def test_every_sql_failure_rolls_back_batch_then_retry_commits_once(self):
        self.append('我喜欢乌龙茶，周末约聚餐')
        old_id = self.store.add_user_fact(self.session, '1', '计划去远足', importance=0.1)
        self.config['memory_max_facts_per_user'] = 1
        self.config['memory_buffer_retention_seconds'] = 0
        data = self.rich_data()
        self.gateway.data = data
        failures = [
            'BEFORE INSERT ON user_impressions_history',
            'BEFORE INSERT ON user_facts',
            'BEFORE INSERT ON user_facts_archive',
            'BEFORE DELETE ON user_facts',
            'BEFORE INSERT ON self_memories',
            "BEFORE INSERT ON settings WHEN NEW.key LIKE 'mem.cursor:%'",
            'BEFORE DELETE ON memory_buffer',
            "BEFORE INSERT ON settings WHEN NEW.key LIKE 'mem.fragment:%'",
            "BEFORE INSERT ON settings WHEN NEW.key LIKE 'mem.fail:%'",
        ]
        for clause in failures:
            with self.subTest(clause=clause):
                before = self.snapshot()
                self.store._conn().execute('CREATE TRIGGER fail_batch ' + clause +
                                           " BEGIN SELECT RAISE(ABORT, 'controlled failure'); END")
                result = self.curator.run_once(self.session)
                self.assertEqual(result['error'], 'IntegrityError')
                self.assertEqual(result['summary'], 0)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.store._conn().in_transaction)
                self.store._conn().execute('DROP TRIGGER fail_batch')
        result = self.curator.run_once(self.session)
        self.assertEqual(result['error'], '')
        self.assertEqual(len(self.store.recent_group_summaries(self.session)), 1)
        self.assertEqual(len(self.store.impression_history('1')), 1)
        self.assertEqual(len(self.store.list_self_memories(self.session)), 1)
        self.assertEqual(self.store.count_user_facts(self.session), 1)
        self.assertEqual(self.store.archived_facts(self.session)[0]['fact'], '计划去远足')
        self.assertEqual(self.store.archived_facts(self.session)[0]['id'], 1)
        count = len(self.gateway.prompts)
        self.curator.run_once(self.session)
        self.assertEqual(len(self.gateway.prompts), count)
        self.assertEqual(len(self.store.recent_group_summaries(self.session)), 1)
        self.assertEqual(len(self.store.archived_facts(self.session)), 1)
        self.assertGreater(old_id, 0)

    def test_commit_failure_rolls_back_active_transaction_before_retry(self):
        self.append('聚会原文')
        self.gateway.data = self.rich_data()
        before = self.snapshot()
        conn = self.store._conn()

        def deny_commit(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        # 快照事务已完成、模型返回前才安装，精确打到最终整批COMMIT。
        self.gateway.callback = lambda: conn.set_authorizer(deny_commit)
        self.assertEqual(self.curator.run_once(self.session)['error'], 'DatabaseError')
        conn.set_authorizer(None)
        self.assertFalse(conn.in_transaction)
        self.assertEqual(self.snapshot(), before)
        self.gateway.callback = None
        self.assertEqual(self.curator.run_once(self.session)['error'], '')
        self.assertEqual(len(self.store.recent_group_summaries(self.session)), 1)
        self.assertEqual(len(self.store.impression_history('1')), 1)

    def test_expiration_and_summary_trim_errors_are_not_swallowed(self):
        self.append('新内容')
        old = self.store.add_user_fact(self.session, '1', '旧事实')
        self.store.add_group_summary(self.session, '旧摘要')
        self.gateway.data = dict(self.rich_data(), expired_fact_ids=[old])
        for clause in ('BEFORE UPDATE ON user_facts', 'BEFORE DELETE ON group_summaries'):
            with self.subTest(clause=clause):
                before = self.snapshot()
                self.config['memory_summary_keep'] = 1
                self.store._conn().execute('CREATE TRIGGER fail_batch ' + clause +
                                           " BEGIN SELECT RAISE(ABORT, 'controlled failure'); END")
                self.assertEqual(self.curator.run_once(self.session)['error'], 'IntegrityError')
                self.assertEqual(self.snapshot(), before)
                self.store._conn().execute('DROP TRIGGER fail_batch')

    def test_fragment_commit_sql_failure_preserves_offset_and_retry_once(self):
        self.append('长' * 17000)
        self.gateway.data = {'group_summary': '分片事件'}
        self.curator.run_once(self.session)
        before = self.snapshot()
        self.store._conn().execute("CREATE TRIGGER fail_batch BEFORE INSERT ON settings "
                                   "WHEN NEW.key LIKE 'mem.fragment:%' "
                                   "BEGIN SELECT RAISE(ABORT, 'controlled failure'); END")
        self.assertEqual(self.curator.run_once(self.session)['error'], 'IntegrityError')
        failed = self.gateway.records()
        self.assertEqual(self.snapshot(), before)
        self.store._conn().execute('DROP TRIGGER fail_batch')
        self.assertEqual(self.curator.run_once(self.session)['error'], '')
        self.assertEqual(self.gateway.records(), failed)
        self.assertEqual(len(self.store.recent_group_summaries(self.session)), 2)

    def test_two_curators_same_snapshot_commit_once(self):
        self.append('聚会内容')
        self.gateway.data = self.rich_data()
        other = MemoryCurator(self.config, self.store, ControlledGateway(self.rich_data()))
        self.gateway.callback = lambda: other.run_once(self.session)
        self.assertEqual(self.curator.run_once(self.session)['error'], 'stale_batch')
        self.assertEqual(len(self.store.recent_group_summaries(self.session)), 1)
        self.assertEqual(len(self.store.impression_history('1')), 1)

    def test_two_curators_same_fragment_commit_once(self):
        self.append('长' * 17000)
        data = {'group_summary': '分片事件'}
        self.gateway.data = data
        other = MemoryCurator(self.config, self.store, ControlledGateway(data))
        self.gateway.callback = lambda: other.run_once(self.session)
        self.assertEqual(self.curator.run_once(self.session)['error'], 'stale_batch')
        self.assertEqual(len(self.store.recent_group_summaries(self.session)), 1)
        self.assertEqual(self.curator.cursor(self.session), 0)
        self.assertGreater(self.curator._fragment_state(self.session)[1], 0)

    def test_forget_during_fragment_keeps_epoch_guard_and_drops_old_offset(self):
        self.append('旧秘密' * 6000)
        self.curator.run_once(self.session)
        self.gateway.data = self.rich_data()
        self.gateway.callback = lambda: self.store.purge_user_facts(self.session, '1')
        self.assertEqual(self.curator.run_once(self.session)['error'], 'forgotten')
        self.assertEqual(self.store.forget_epoch(), 1)
        self.assertEqual(self.store.count_user_facts(self.session), 0)
        self.gateway.callback = None
        new_id = self.append('新一代内容')
        self.gateway.data = {}
        self.assertEqual(self.curator.run_once(self.session)['error'], '')
        self.assertEqual(self.curator.cursor(self.session), new_id)
        self.assertEqual(self.curator._fragment_state(self.session), (0, 0))

    def test_direct_store_results_validates_and_rolls_back(self):
        bad = dict(self.rich_data(), user_facts=42)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.curator._store_results(self.session, bad, {'1'}, 1)
        self.assertEqual(self.snapshot(), before)
        self.store._conn().execute('CREATE TRIGGER fail_batch BEFORE INSERT ON self_memories '
                                   "BEGIN SELECT RAISE(ABORT, 'controlled failure'); END")
        with self.assertRaises(Exception):
            self.curator._store_results(self.session, self.rich_data(), {'1'}, 1)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
