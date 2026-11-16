# -*- encoding: utf-8 -*-
"""场景在途遗忘/剧情切换/并发CAS回放；不访问真实模型或生产DB。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_episode_scene_import_'))

from newGeYi.context import scene
from newGeYi.models import ModelResult
from newGeYi.storage.database import SQLiteStore

SESSION = 'bot:qq:onebot:group:-:episode-scene'


class Gateway:
    def __init__(self):
        self.callback = None
        self.calls = []
        self.data = {'scene': '面包坊', 'threads': ['正在揉面'], 'promises': ['明天陪他烤面包']}

    def chat(self, req, **kwargs):
        self.calls.append(req)
        if self.callback:
            self.callback()
        return ModelResult(ok=True, text=json.dumps(self.data, ensure_ascii=False))


class EpisodeSceneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_episode_scene_')
        self.path = os.path.join(self.tmp.name, 'memory.sqlite3')
        self.store = SQLiteStore(self.path)
        self.gateway = Gateway()
        self.config = {'scene_update_messages': 1, 'scene_min_interval_seconds': 0,
                       'scene_idle_seconds': 0, 'scene_retry_seconds': 0}
        self.keeper = scene.SceneKeeper(self.config, self.store, self.gateway)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, content='正在揉面', space='chat', uid='1'):
        return self.store.buffer_append(SESSION, 'user', content, sender_id=uid,
                                        metadata={'space_id': space})

    def test_promises_are_not_rendered_new_or_old(self):
        self.assertNotIn('明天', scene.format_brief(self.gateway.data))
        scene.save_brief(self.store, SESSION, '场景：面包坊\n她答应过：明天陪他烤面包', 1)
        self.assertNotIn('她答应过', scene.to_prompt(self.store, SESSION, {}))
        self.assertNotIn('明天', scene.get_brief(self.store, SESSION)['text'])
        # 升级前持久化的自由承诺同样不能注入。
        self.store.set_setting(scene.SCENE_PREFIX + SESSION, json.dumps({
            'text': '场景：面包坊\n她答应过：旧承诺', 'covered_to': 1, 'updated_at': 9999999999}))
        self.assertNotIn('旧承诺', scene.to_prompt(self.store, SESSION, {}))

    def test_hidden_promises_in_scene_and_threads_are_filtered(self):
        text = scene.format_brief({'scene': '面包坊', 'threads': ['约好明天烤面包'],
                                   'open_questions': ['面粉在哪']})
        self.assertNotIn('约好', text)
        self.assertIn('面包坊', text)
        self.assertIn('面粉在哪', text)

    def test_forget_during_network_does_not_resurrect_after_restart(self):
        self.add()
        def forget():
            self.store.set_setting('memory.forget_epoch', '1')
            self.store._conn().execute('DELETE FROM settings WHERE key=?', (scene.SCENE_PREFIX + SESSION,))
        self.gateway.callback = forget
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertIsNone(scene.get_brief(self.store, SESSION))
        self.store.close()
        self.store = SQLiteStore(self.path)
        self.assertEqual(scene.to_prompt(self.store, SESSION, {}), '')

    def test_space_switch_during_network_discards_old_scene(self):
        self.add(space='story:a')
        self.store.set_setting('memory.space:' + SESSION, 'story:a')
        def switch():
            self.store.set_setting('memory.space:' + SESSION, 'story:b')
            self.store.set_setting('scene.generation:' + SESSION, '1')
        self.gateway.callback = switch
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertIsNone(scene.get_brief(self.store, SESSION))

    def test_newer_covered_to_wins_while_model_runs(self):
        rid = self.add()
        self.gateway.callback = lambda: scene.save_brief(self.store, SESSION, '更晚纸条', rid + 1)
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(scene.get_brief(self.store, SESSION)['text'], '更晚纸条')

    def test_same_covered_to_concurrent_result_cas(self):
        rid = self.add()
        self.gateway.callback = lambda: scene.save_brief(self.store, SESSION, '其他提炼已提交', rid)
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(scene.get_brief(self.store, SESSION)['text'], '其他提炼已提交')

    def test_input_only_current_frozen_space_and_after_derivation_watermark(self):
        old = self.add('已删旧原文', space='story:a')
        self.add('其他篇章原文', space='story:b')
        self.add('当前新原文', space='story:a')
        self.store.set_setting('memory.space:' + SESSION, 'story:a')
        self.store.set_setting('episode.derivation_watermark:' + SESSION, str(old))
        self.assertTrue(self.keeper.consider(SESSION))
        prompt = self.gateway.calls[0].messages[1]['content']
        self.assertIn('当前新原文', prompt)
        self.assertNotIn('已删旧原文', prompt)
        self.assertNotIn('其他篇章原文', prompt)

    def test_old_space_backlog_advances_without_spending_model_calls(self):
        self.config['scene_input_messages'] = 4
        for _ in range(9):
            self.add('旧篇章存量', space='story:old')
        new = self.add('新篇章揉面', space='story:new')
        self.store.set_setting('memory.space:' + SESSION, 'story:new')
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(self.gateway.calls, [])
        self.assertTrue(self.keeper.consider(SESSION))
        self.assertEqual(scene.get_brief(self.store, SESSION)['covered_to'], new)
        self.assertNotIn('旧篇章存量', self.gateway.calls[0].messages[1]['content'])

    def test_network_call_is_outside_transaction(self):
        self.add()
        observed = []
        self.gateway.callback = lambda: observed.append(self.store._conn().in_transaction)
        self.assertTrue(self.keeper.consider(SESSION))
        self.assertEqual(observed, [False])

    def test_prompt_requires_explicit_frozen_space_not_current_story(self):
        self.store.set_setting('memory.space:' + SESSION, 'story:new')
        scene.save_brief(self.store, SESSION, '场景：新篇章', 1)
        self.assertEqual(scene.to_prompt(self.store, SESSION, {}, space_id='story:old'), '')
        self.assertEqual(scene.to_prompt(self.store, SESSION, {}, space_id='chat'), '')
        self.assertIn('新篇章', scene.to_prompt(self.store, SESSION, {}, space_id='story:new'))
        self.assertIn('新篇章', scene.to_prompt(self.store, SESSION, {}))

    def test_old_scene_hidden_on_epoch_or_generation_or_space_change(self):
        for setting, value in [('memory.forget_epoch', '1'), ('scene.generation:' + SESSION, '1'),
                               ('memory.space:' + SESSION, 'story:new')]:
            with self.subTest(setting=setting):
                scene.save_brief(self.store, SESSION, '场景：旧面包坊', 1)
                self.store.set_setting(setting, value)
                self.assertEqual(scene.to_prompt(self.store, SESSION, {}), '')
