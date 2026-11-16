# -*- coding: utf-8 -*-
"""Recall integration: fixed budgets, legacy isolation and read-only previews."""
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='episode_prompt_import_'))
from newGeYi.memory.repository import MemoryRepository
from newGeYi.memory.episodes import EpisodeMemory
from newGeYi.context.prompt_builder import PromptBuilder
from newGeYi.models import SessionKey
from newGeYi.panel import _ReadStore, _MemoryPreview
from newGeYi.storage.database import SQLiteStore


class EpisodePromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='episode_prompt_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'state.sqlite3'))
        self.key = SessionKey('bot', 'qq', 'onebot', 'private', '', '1')
        self.repo = MemoryRepository(self.store, {'memory_episodes_enabled': True})
        self.msg = SimpleNamespace(session=self.key, sender_id='1', text='面包',
                                   is_group=False, memory_space_id='chat')
        self.data = {'facts': [], 'self': [], 'summaries': [], 'impression': None,
                     'cross_impression': None}
        self.repo.recall = lambda *args, **kwargs: dict(self.data)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_total_budget_includes_episode_and_legacy(self):
        from unittest.mock import patch
        self.data['impression'] = {'summary': '旧' * 1000}
        with patch.object(EpisodeMemory, 'build_block', return_value='新' * 80):
            result = self.repo.build_block(self.msg, max_chars=100)
        self.assertTrue(result.startswith('新' * 80))
        self.assertLessEqual(len(result), 100)
        with patch.object(EpisodeMemory, 'build_block', return_value='新' * 80) as build:
            self.assertEqual(self.repo.build_block(self.msg, max_chars=0), '')
            build.assert_not_called()

    def test_legacy_promises_do_not_compete_with_terminal_records(self):
        self.data['self'] = [{'id': 1, 'kind': 'promise', 'content': '明天烤面包'},
                             {'id': 2, 'kind': 'event', 'content': '答应陪你烤面包'},
                             {'id': 3, 'kind': 'event', 'content': '喜欢麦香'}]
        self.data['summaries'] = [{'content': '约好烤面包'}, {'content': '聊过酵母'}]
        result = self.repo.build_block(self.msg)
        self.assertNotIn('明天烤面包', result)
        self.assertNotIn('答应陪你', result)
        self.assertNotIn('约好烤面包', result)
        self.assertIn('喜欢麦香', result)
        self.assertIn('聊过酵母', result)

    def test_story_excludes_unscoped_legacy_events_and_summaries(self):
        self.msg.memory_space_id = 'story:one'
        self.data['self'] = [{'id': 1, 'kind': 'event', 'content': '旧情节'}]
        self.data['summaries'] = [{'content': '旧摘要'}]
        result = self.repo.build_block(self.msg)
        self.assertNotIn('旧情节', result)
        self.assertNotIn('旧摘要', result)

    def test_history_uses_frozen_space_and_legacy_is_chat(self):
        rows = [{'content': 'legacy'}, {'content': 'chat', 'metadata': {'space_id': 'chat'}},
                {'content': 'story', 'metadata': {'space_id': 'story:one'}}]
        builder = PromptBuilder.__new__(PromptBuilder)
        self.assertEqual([r['content'] for r in builder._space_history(rows, self.msg)],
                         ['legacy', 'chat'])
        self.msg.memory_space_id = 'story:one'
        builder = PromptBuilder.__new__(PromptBuilder)
        self.assertEqual([r['content'] for r in builder._space_history(rows, self.msg)], ['story'])

    def test_preview_only_opens_whitelisted_reads(self):
        view = _ReadStore(self.store)
        self.assertEqual(view.list_episode_records(self.key.as_text(), '1'), [])
        with self.assertRaises(Exception):
            view._conn()
        repo = _MemoryPreview(view, {'memory_episodes_enabled': True})
        # The normal builder may read episode rows, but cannot refresh hits or create a story.
        before = self.store._conn().total_changes
        repo.build_block(self.msg)
        self.assertEqual(self.store._conn().total_changes, before)


if __name__ == '__main__':
    unittest.main()
