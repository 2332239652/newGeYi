# -*- encoding: utf-8 -*-
"""F02: persistent forget watermarks and deterministic in-flight races (no models)."""
from __future__ import annotations

import os
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_batch1_import_'))

from newGeYi.memory.curator import MemoryCurator
from newGeYi.models import IncomingMessage, SessionKey
from newGeYi.relationship.service import RelationshipService
from newGeYi.storage.database import SQLiteStore


class ForgetGenerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_batch1_forget_')
        self.path = os.path.join(self.tmp.name, 'state.sqlite3')
        self.store = SQLiteStore(self.path)
        self.group = 'h:qq:onebot:group:-:100'
        self.other = 'h:qq:onebot:group:-:200'
        self.private = 'h:qq:onebot:private:-:10001'
        self.uid = '10001'
        self.config = {'memory_cross_group_enabled': True, 'memory_impression_enabled': True,
                       'galgame_enabled': True, 'galgame_shadow': False}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _data(self, scope='group', uid=None):
        return {'user_facts': [{'user_id': uid or self.uid, 'fact': '偏爱肉桂苹果卷',
                                'scope': scope}],
                'user_impressions': [{'user_id': uid or self.uid, 'summary': '说话温和而且很喜欢甜点'}],
                'group_summary': '公共聚会', 'self_memories': [{'content': '答应准备点心'}]}

    def _curate(self, session, data):
        cur = MemoryCurator(self.config, self.store, None)
        cur._request_and_parse = lambda *args: data
        return cur.run_once(session)

    def _count(self, table, uid=None):
        return self.store._conn().execute('SELECT COUNT(*) FROM ' + table + ' WHERE user_id=?',
                                          (uid or self.uid,)).fetchone()[0]

    def test_inflight_curator_forget_returns_before_model_and_old_result_dropped(self):
        self.store.buffer_append(self.group, 'user', '旧秘密：偏爱肉桂苹果卷', self.uid)
        self.store.buffer_append(self.group, 'assistant', '我记住你的旧秘密了')
        entered, release = threading.Event(), threading.Event()
        cur = MemoryCurator(self.config, self.store, None)
        def model(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('model release timed out')
            return self._data('global')
        cur._request_and_parse = model
        def run():
            try:
                return cur.run_once(self.group)
            finally:
                self.store.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            self.assertTrue(entered.wait(5))
            try:
                self.store.purge_user_facts(self.group, self.uid)
                self.assertFalse(future.done())
            finally:
                release.set()
            self.assertEqual(future.result(5)['error'], 'forgotten')
        for table in ('user_facts', 'user_facts_archive', 'user_impressions', 'user_impressions_history'):
            self.assertEqual(self._count(table), 0, table)
        self.assertEqual(self.store._conn().execute('SELECT COUNT(*) FROM self_memories').fetchone()[0], 0)
        # 剩下的旧 assistant 引文不能给模型伪造 user_id 的机会。
        result = self._curate(self.group, self._data())
        self.assertEqual(result['facts'], 0)
        self.assertEqual(self._count('user_facts'), 0)
        self.assertEqual(cur.cursor(self.group), self.store.buffer_max_id(self.group))

    def test_archive_clear_scope_global_and_other_user_isolation(self):
        for session in (self.group, 'global', self.other):
            for uid in (self.uid, '10002'):
                self.store.add_user_fact(session, uid, '归档的旧事实', importance=0.1)
                self.store.add_user_fact(session, uid, '当前有效事实', importance=0.9)
                self.store.trim_user_facts(session, uid, max_per_user=1)
                self.store.buffer_append(session, 'user', '原始内容', uid)
        self.store.set_user_impression(self.uid, '这是私聊印象', scope='private')
        self.store.set_user_impression(self.uid, '这是群聊印象', scope='group')
        self.store._conn().execute('INSERT INTO memories(user_key,fact,created_at,last_used_at) VALUES(?,?,1,1)',
                                   (self.group + '|u|' + self.uid, '旧格式事实'))
        rel = RelationshipService(self.config, self.store)
        rel.force_stage(self.uid, 2)
        rel._log(self.uid, delta_raw=1, delta_applied=1, tag='test', level=0, aff_before=0,
                 aff_after=1, stage_before=0, stage_after=0, shadow=False, note='',
                 user_text='旧文本', reply_text='旧回复')
        rel.shutdown()
        self.store.purge_user_facts(self.group, self.uid)
        for table in ('user_facts', 'user_facts_archive'):
            rows = self.store._conn().execute('SELECT session_key FROM ' + table + ' WHERE user_id=?',
                                              (self.uid,)).fetchall()
            self.assertEqual(rows, [(self.other,)], table)
            self.assertEqual(self._count(table, '10002'), 3)
        for table in ('user_impressions', 'user_impressions_history', 'user_relationship', 'relationship_log'):
            self.assertEqual(self._count(table), 0, table)
        self.assertEqual(self.store.buffer_fetch(self.group)[0]['sender_id'], '10002')
        self.assertEqual(len(self.store.buffer_fetch(self.other)), 2)
        self.assertEqual(self.store._conn().execute('SELECT COUNT(*) FROM memories').fetchone()[0], 0)

    def test_other_group_old_text_cannot_restore_global_or_shared_impression_after_restart(self):
        old_id = self.store.buffer_append(self.other, 'user', '偏爱肉桂苹果卷', self.uid)
        self.store.purge_user_facts(self.group, self.uid)
        self.store.close()
        self.store = SQLiteStore(self.path)
        self.assertGreaterEqual(self.store.forget_watermark('global', self.uid), old_id)
        result = self._curate(self.other, self._data('global'))
        self.assertEqual(result['facts'], 0)
        self.assertEqual(self._count('user_impressions'), 0)
        self.assertEqual(len(self.store.buffer_fetch(self.other)), 1)
        # 新一代消息仍可正常形成新记忆。
        self.store.buffer_append(self.other, 'user', '我重新告诉你：偏爱肉桂苹果卷', self.uid)
        result = self._curate(self.other, self._data('global'))
        self.assertEqual(result['facts'], 1)
        self.assertEqual(self._count('user_impressions'), 1)

    def test_other_user_old_rows_still_curate_after_forget(self):
        self.store.buffer_append(self.group, 'user', '旧秘密', self.uid)
        self.store.buffer_append(self.group, 'user', '我喜欢苹果卷', '10002')
        self.store.purge_user_facts(self.group, self.uid)
        # 假模型即使从旧摘要/assistant引文输出被遗忘者，也只允许本批有效说话人。
        data = self._data(uid='10002')
        data['user_facts'] += self._data()['user_facts']
        data['user_impressions'] += self._data()['user_impressions']
        result = self._curate(self.group, data)
        self.assertEqual(result['facts'], 1)
        self.assertEqual(self._count('user_facts'), 0)
        self.assertEqual(self._count('user_facts', '10002'), 1)

    def test_single_delete_invalidates_inflight_curator_and_keeps_other_facts(self):
        fact_id = self.store.add_user_fact('global', self.uid, '偏爱肉桂苹果卷')
        self.store.add_user_fact('global', self.uid, '从事天文学研究')
        self.store.buffer_append(self.group, 'user', '偏爱肉桂苹果卷', self.uid)
        entered, release = threading.Event(), threading.Event()
        cur = MemoryCurator(self.config, self.store, None)
        def model(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return self._data('global')
        cur._request_and_parse = model
        def run():
            try:
                return cur.run_once(self.group)
            finally:
                self.store.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            self.assertTrue(entered.wait(5))
            try:
                self.assertFalse(self.store.delete_user_fact_by_id(fact_id, '10002'))
                self.assertTrue(self.store.delete_user_fact_by_id(fact_id, self.uid, self.group))
            finally:
                release.set()
            self.assertEqual(future.result(5)['error'], 'forgotten')
        self.assertEqual([r['fact'] for r in self.store.list_user_facts('global', self.uid)], ['从事天文学研究'])

    def test_concurrent_forget_is_atomic_and_separate_users_survive(self):
        for uid in (self.uid, '10002', '10003'):
            self.store.add_user_fact(self.group, uid, '各自的事实')
            self.store.buffer_append(self.group, 'user', '各自的原文', uid)
        barrier = threading.Barrier(2)
        def purge(uid):
            try:
                barrier.wait(5)
                self.store.purge_user_facts(self.group, uid)
            finally:
                self.store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(purge, uid) for uid in (self.uid, '10002')]
            for future in futures:
                future.result(5)
        self.assertEqual(self.store.forget_epoch(), 2)
        self.assertEqual(self._count('user_facts'), 0)
        self.assertEqual(self._count('user_facts', '10002'), 0)
        self.assertEqual(self._count('user_facts', '10003'), 1)
        self.assertEqual([r['sender_id'] for r in self.store.buffer_fetch(self.group)], ['10003'])

    def test_forget_rollback_keeps_data_and_generation_together(self):
        self.store.add_user_fact(self.group, self.uid, '旧事实')
        self.store.buffer_append(self.group, 'user', '旧原文', self.uid)
        self.store._conn().execute("CREATE TRIGGER fail_forget BEFORE DELETE ON user_impressions "
                                   "BEGIN SELECT RAISE(ABORT, 'injected forget failure'); END")
        self.store.set_user_impression(self.uid, '有效的旧印象')
        with self.assertRaisesRegex(Exception, 'injected forget failure'):
            self.store.purge_user_facts(self.group, self.uid)
        self.assertEqual(self.store.forget_epoch(), 0)
        self.assertEqual(self.store.forget_generation(self.group, self.uid), 0)
        self.assertEqual(self._count('user_facts'), 1)
        self.assertEqual(len(self.store.buffer_fetch(self.group)), 1)

    def test_relationship_inflight_and_queued_jobs_do_not_resurrect(self):
        service = RelationshipService(self.config, self.store)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def judge(*args):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(5))
            return {'delta': 2, 'tag': 'care', 'advance_level': 0, 'note': '', 'mood': '开心'}
        service._judge = judge
        session = SessionKey('h', 'qq', 'onebot', 'private', '-', self.uid)
        msg = IncomingMessage(session=session, message_id='1', sender_id=self.uid,
                              sender_name='测试', text='今天给你准备了苹果派', raw_text='', is_group=False,
                              mentioned_bot=False, event=None, proc=None, arrival_seq=1,
                              sender_is_bot=False)
        futures = []
        # 保持真实单worker队列，用wrapper收集future并关闭thread-local连接。
        original_submit = service._pool.submit
        def submit(fn, job):
            def run():
                try:
                    return fn(job)
                finally:
                    self.store.close()
            future = original_submit(run)
            futures.append(future)
            return future
        service._pool.submit = submit
        try:
            service.after_reply(msg, '谢谢你的点心')
            self.assertTrue(entered.wait(5))
            service.after_reply(msg, '第二条排队回复')
            self.store.purge_user_facts(self.private, self.uid)
            service.forget(self.uid)
            self.assertFalse(futures[0].done())
        finally:
            release.set()
            service._pool.shutdown(wait=True)
        for future in futures:
            future.result(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._count('user_relationship'), 0)
        self.assertEqual(self._count('relationship_log'), 0)
        # 新任务捕获新generation，可以继续建立关系。
        service.process_interaction(self.uid, '测试', self.private, '给你准备新的苹果派', '谢谢', judge=judge)
        self.assertEqual(self._count('user_relationship'), 1)


if __name__ == '__main__':
    unittest.main()
