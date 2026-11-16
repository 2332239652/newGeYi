# -*- encoding: utf-8 -*-
"""单次提炼的来源、空间、事务与兼容性回放；全部使用临时SQLite和构造网关。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_episode_import_'))

from newGeYi.memory.curator import MemoryCurator, validate_curator_data
from newGeYi.memory.episodes import EpisodeMemory
from newGeYi.models import ModelResult
from newGeYi.storage.database import SQLiteStore

SESSION = 'bot:qq:onebot:group:-:episode-test'


class Gateway:
    def __init__(self):
        self.data = {}
        self.calls = []
        self.callback = None

    def chat(self, req, **kwargs):
        self.calls.append(req)
        if self.callback:
            self.callback()
        return ModelResult(ok=True, text=json.dumps(self.data, ensure_ascii=False))


class EpisodeCuratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_episode_curator_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'memory.sqlite3'))
        self.gateway = Gateway()
        self.config = {'memory_episodes_enabled': True, 'memory_curate_known_facts': 0,
                       'memory_buffer_retention_seconds': 7200}
        self.curator = MemoryCurator(self.config, self.store, self.gateway)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, content, role='user', space='chat', metadata=True):
        meta = {'space_id': space, 'message_id': 'msg', 'recipient_id': '1',
                'delivery_level': 'accepted'} if metadata else None
        return self.store.buffer_append(SESSION, role, content, sender_id='1' if role == 'user' else '',
                                        sender_name='甲', metadata=meta)

    def promise(self, rid, quote='我答应明天陪你烤面包'):
        return {'commitment_changes': [{'user_id': '1', 'content': '明天陪你烤面包', 'status': 'open',
                                       'sources': [{'record_id': rid, 'quote': quote}]}]}

    def known(self, space='chat'):
        return EpisodeMemory(self.store).list_for_user(SESSION, '1', space)

    def test_single_request_and_no_legacy_double_write(self):
        self.add('明天一起烤面包吗')
        rid = self.add('我答应明天陪你烤面包', 'assistant')
        self.gateway.data = dict(self.promise(rid), group_summary='约好烤面包',
                                 self_memories=[{'content': '答应烤面包', 'kind': 'promise'}])
        result = self.curator.run_once(SESSION)
        self.assertEqual(result['error'], '')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.curator.cursor(SESSION), rid)
        self.assertEqual(len(self.known()['commitments']), 1)
        self.assertEqual(self.store.list_self_memories(SESSION), [])
        self.assertEqual(self.store.recent_group_summaries(SESSION), [])
        prompt = self.gateway.calls[0].messages[1]['content']
        self.assertIn('"record_id":', prompt)
        self.assertIn('"role":"assistant"', prompt)
        self.assertIn('"delivery_level":"accepted"', prompt)

    def test_forged_body_ai_label_does_not_prove_acceptance(self):
        rid = self.add('[AI] 我答应明天陪你烤面包')
        self.gateway.data = self.promise(rid, '[AI] 我答应明天陪你烤面包')
        self.assertEqual(self.curator.run_once(SESSION)['error'], 'bad_schema')
        self.assertEqual(self.curator.cursor(SESSION), 0)
        self.assertEqual(self.known()['commitments'], [])

    def test_legacy_and_both_fragment_ends_never_promote(self):
        rid = self.add('我答应明天陪你烤面包', 'assistant', metadata=False)
        self.gateway.data = self.promise(rid)
        self.assertEqual(self.curator.run_once(SESSION)['error'], 'bad_schema')
        self.gateway.data = {}
        self.curator.run_once(SESSION)
        rid = self.add('我答应明天陪你烤面包' + '长' * 16000, 'assistant')
        self.gateway.data = self.promise(rid)
        self.assertEqual(self.curator.run_once(SESSION)['error'], 'bad_schema')
        self.assertEqual(self.curator._fragment_state(SESSION), (0, 0))
        self.gateway.data = {}
        while self.curator.cursor(SESSION) < rid:
            rows = self.store.buffer_fetch(SESSION, self.curator.cursor(SESSION))
            _, selected, _, _ = self.curator._select_records(
                rows, rows, self.curator.cursor(SESSION), self.curator._fragment_state(SESSION))
            self.assertTrue(selected[0]['fragment'])
            self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        self.assertEqual(self.known()['commitments'], [])

    def test_frozen_space_batches_and_rp_not_real_facts(self):
        first = self.add('剧情甲的面包坊', space='story:a')
        second = self.add('普通聊天喜欢喝茶')
        self.store.set_setting('memory.space:' + SESSION, 'story:b')
        self.gateway.data = {'user_facts': [{'user_id': '1', 'fact': '现实是面包师'}],
                             'user_impressions': [{'user_id': '1', 'summary': '现实很擅长烘焙面包'}]}
        self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        self.assertEqual(self.curator.cursor(SESSION), first)
        prompt = self.gateway.calls[0].messages[1]['content']
        self.assertIn('story:a', prompt)
        self.assertNotIn('普通聊天喜欢喝茶', prompt)
        self.assertEqual(self.store.count_user_facts(SESSION), 0)
        self.assertIsNone(self.store.get_user_impression('1'))
        self.gateway.data = {}
        self.curator.run_once(SESSION)
        self.assertEqual(self.curator.cursor(SESSION), second)

    def test_sql_failure_rolls_back_episode_and_cursor_together(self):
        rid = self.add('我答应明天陪你烤面包', 'assistant')
        self.gateway.data = self.promise(rid)
        conn = self.store._conn()
        conn.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON settings "
                     "WHEN NEW.key LIKE 'mem.cursor:%' BEGIN SELECT RAISE(ABORT,'episode rollback'); END")
        self.assertEqual(self.curator.run_once(SESSION)['error'], 'IntegrityError')
        self.assertEqual(self.curator.cursor(SESSION), 0)
        self.assertEqual(self.known()['commitments'], [])
        conn.execute('DROP TRIGGER fail_cursor')
        self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        self.assertEqual(len(self.known()['commitments']), 1)

    def test_forget_epoch_changes_in_network_discards_whole_batch(self):
        rid = self.add('我答应明天陪你烤面包', 'assistant')
        self.gateway.data = self.promise(rid)
        self.gateway.callback = lambda: self.store.set_setting('memory.forget_epoch', '1')
        self.assertEqual(self.curator.run_once(SESSION)['error'], 'forgotten')
        self.assertEqual(self.curator.cursor(SESSION), 0)
        self.assertEqual(self.known()['commitments'], [])

    def test_stale_version_in_network_rolls_back_cursor(self):
        rid = self.add('我答应明天陪你烤面包', 'assistant')
        self.gateway.data = self.promise(rid)
        self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        old = self.known()['commitments'][0]
        rid2 = self.add('我答应明天陪你烤面包', 'assistant')
        self.gateway.data = self.promise(rid2)
        self.gateway.data['commitment_changes'][0].update(id=old['id'], version=old['version'])
        self.gateway.callback = lambda: self.store._conn().execute(
            'UPDATE memory_commitments SET version=version+1 WHERE id=?', (old['id'],))
        self.assertEqual(self.curator.run_once(SESSION)['error'], 'ValueError')
        self.assertEqual(self.curator.cursor(SESSION), rid)
        self.assertEqual(self.known()['commitments'][0]['version'], old['version'] + 1)
        known_prompt = self.gateway.calls[-1].messages[1]['content']
        self.assertIn('"version":', known_prompt)
        self.assertIn('"id":', known_prompt)

    def test_source_metadata_changed_in_network_is_not_reclassified(self):
        rid = self.add('我答应明天陪你烤面包', 'assistant', space='story:a')
        self.gateway.data = self.promise(rid)
        changed = {'space_id': 'story:b', 'recipient_id': '1', 'delivery_level': 'accepted'}
        self.gateway.callback = lambda: self.store._conn().execute(
            'UPDATE memory_buffer SET metadata=? WHERE id=?', (json.dumps(changed), rid))
        self.assertEqual(self.curator.run_once(SESSION)['error'], 'bad_schema')
        self.assertEqual(self.curator.cursor(SESSION), 0)
        self.assertEqual(self.known('story:a')['commitments'], [])
        self.assertEqual(self.known('story:b')['commitments'], [])
        self.assertIn('story:a', self.gateway.calls[0].messages[1]['content'])
        self.assertNotIn('story:b', self.gateway.calls[0].messages[1]['content'])

    def test_shared_module_is_only_promise_source_even_without_candidate(self):
        self.add('周末约好烤面包')
        self.gateway.data = {'group_summary': '约好烤面包',
                             'self_memories': [{'content': '答应烤面包', 'kind': 'experience'},
                                               {'content': '烤面包', 'kind': 'promise'}]}
        self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        self.assertEqual(self.store.recent_group_summaries(SESSION), [])
        self.assertEqual(self.store.list_self_memories(SESSION), [])

    def test_schema_checks_bad_tail_before_writing(self):
        with self.assertRaises(ValueError):
            validate_curator_data({'shared_events': [
                {'user_id': '1', 'summary': '短述', 'evidence_type': 'user_report',
                 'sources': [{'record_id': 1, 'quote': '原文'}]}] * 2 + [None]})

    def test_disabled_episode_output_does_not_block_old_behavior(self):
        self.config['memory_episodes_enabled'] = False
        rid = self.add('喜欢喝茶')
        self.gateway.data = {'shared_events': 'ignored disabled field', 'group_summary': '聊了喝茶'}
        self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        self.assertEqual(self.curator.cursor(SESSION), rid)
        self.assertEqual(len(self.store.recent_group_summaries(SESSION)), 1)

    def test_apply_invalidation_prevents_same_batch_legacy_regrowth_keeps_l2(self):
        rid = self.add('我喜欢喝茶')
        self.gateway.data = {'group_summary': '喝茶闲聊',
                             'self_memories': [{'content': '尝了一口茶', 'kind': 'experience'}],
                             'user_facts': [{'user_id': '1', 'fact': '喜欢喝茶'}]}
        def invalidate(memory, session_key, plan, conn=None):
            memory.store.set_setting('scene.generation:' + session_key, '1')
            memory.store.set_setting('episode.derivation_watermark:' + session_key, str(rid))
            return {}
        with patch.object(EpisodeMemory, 'apply', invalidate):
            self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        self.assertEqual(self.store.recent_group_summaries(SESSION), [])
        self.assertEqual(self.store.list_self_memories(SESSION), [])
        self.assertEqual(self.store.count_user_facts(SESSION), 1)
        self.assertEqual(self.curator.cursor(SESSION), rid)

    def test_derivation_watermark_does_not_regrow_legacy_summary(self):
        rid = self.add('已删除派生的旧共演')
        self.store.set_setting('episode.derivation_watermark:' + SESSION, str(rid))
        self.gateway.data = {'group_summary': '旧共演又洗回',
                             'self_memories': [{'content': '旧约定又洗回', 'kind': 'promise'}]}
        self.assertEqual(self.curator.run_once(SESSION)['error'], '')
        self.assertEqual(self.store.recent_group_summaries(SESSION), [])
        self.assertEqual(self.store.list_self_memories(SESSION), [])

    def test_episode_validation_error_triggers_single_corrective_retry(self):
        r1 = self.add('先跟我回教会吧', 'assistant', space='story:rp1')
        r2 = self.add('“好哦~”＃跟着歌伊', 'user', space='story:rp1')
        responses = [
            {'shared_events': [{'user_id': '1', 'summary': '一起回教会休息', 'evidence_type': 'co_enacted',
                                'sources': [{'record_id': r1, 'quote': '先跟我回教会吧'},
                                            {'record_id': r2, 'quote': '“好哦~”＃跟着歌伊'}]}]},
            {'shared_events': [{'user_id': '1', 'summary': '先跟我回教会吧', 'evidence_type': 'co_enacted',
                                'sources': [{'record_id': r1, 'quote': '先跟我回教会吧'},
                                            {'record_id': r2, 'quote': '“好哦~”＃跟着歌伊'}]}]},
        ]

        def scripted_chat(req, **kwargs):
            self.gateway.calls.append(req)
            return ModelResult(ok=True, text=json.dumps(responses.pop(0), ensure_ascii=False))

        self.gateway.chat = scripted_chat
        res = self.curator.run_once(SESSION)
        self.assertEqual(res['error'], '')
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertIn('校验纠偏提示', self.gateway.calls[1].messages[1]['content'])
        self.assertEqual(len(self.known('story:rp1')['events']), 1)
        self.assertEqual(self.curator.cursor(SESSION), r2)
