"""F12 frozen predecessor history; F13 lock-free judge and forget races."""
from __future__ import annotations
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_relation_import_'))
from newGeYi.models import IncomingMessage, SessionKey
from newGeYi.relationship.service import RelationshipService
from newGeYi.storage.database import SQLiteStore
from newGeYi.tests.test_batch2_voice import HeldExecutor

VERDICT = {'delta': 1, 'tag': 'care', 'advance_level': 0, 'note': '', 'mood': ''}


class RelationshipQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_relationship_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'state.sqlite3'))
        self.service = RelationshipService({'galgame_enabled': True, 'galgame_shadow': False}, self.store)
        self.service._pool.shutdown(wait=True)
        self.pool = HeldExecutor()
        self.service._pool = self.pool
        self.session = SessionKey('h', 'qq', 'onebot', 'private', '-', '10001')
        self.msg = IncomingMessage(session=self.session, message_id='1', sender_id='10001',
                                  sender_name='测试', text='今天给你准备了一盘苹果派', raw_text='', is_group=False,
                                  mentioned_bot=False, event=None, proc=None, arrival_seq=1,
                                  sender_is_bot=False)
    def tearDown(self):
        self.service.shutdown()
        self.store.close()
        self.tmp.cleanup()
    def test_explicit_snapshot_ignores_later_same_text_and_does_not_mutate(self):
        seen = []
        self.service._judge = lambda rel, history, *a: seen.append(history) or VERDICT
        history = [{'role': 'user', 'content': '前天在公园看到了小鹿', 'created_at': 1}]
        self.service.after_reply(self.msg, '谢谢你', history=history, now=100)
        history[0]['content'] = '调用方后来修改了列表'
        # Future repetition used to contaminate old worker's latest[:-2].
        for _ in range(3):
            self.store.add_turn(self.session.as_text(), 'user', self.msg.text, '10001')
            self.store.add_turn(self.session.as_text(), 'assistant', '未来的回答')
        self.pool.run()
        self.assertEqual(seen, [['对方：前天在公园看到了小鹿']])
        self.assertEqual(self.service.recent_log('10001', 1)[0]['tag'], 'care')
    def test_legacy_enqueue_snapshots_before_future_turns(self):
        key = self.session.as_text()
        self.store.add_turn(key, 'user', '前一条真实消息', '10001')
        self.store.add_turn(key, 'assistant', '前一答')
        self.store.add_turn(key, 'user', self.msg.text, '10001')
        self.store.add_turn(key, 'assistant', '谢谢你')
        seen = []
        self.service._judge = lambda rel, history, *a: seen.append(history) or VERDICT
        self.service.after_reply(self.msg, '谢谢你', now=100)
        self.store.add_turn(key, 'user', self.msg.text, '10001')
        self.store.add_turn(key, 'assistant', '未来一答')
        self.pool.run()
        self.assertEqual(seen, [['对方：前一条真实消息', '歌伊：前一答']])
    def _blocking_run(self, judge):
        try:
            return self.service.process_interaction('10001', '测试', self.session.as_text(),
                                                    self.msg.text, '谢谢你', history=(), now=100, judge=judge)
        finally:
            self.store.close()
    def test_slow_judge_releases_user_lock_and_db_and_reloads_latest(self):
        entered, release = threading.Event(), threading.Event()
        def judge(*a):
            entered.set()
            self.assertTrue(release.wait(5))
            return dict(VERDICT, delta=0, tag='neutral')
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self._blocking_run, judge)
            self.assertTrue(entered.wait(5))
            def local_update():
                try:
                    with self.service._lock_for('10001'), self.store.transaction():
                        rel = self.service.load('10001')
                        rel.affection = 8.0
                        self.service.save(rel)
                    self.store.set_setting('parallel_write', 'ok')
                    return self.service.describe('10001')
                finally:
                    self.store.close()
            try:
                text = pool.submit(local_update).result(2)
                self.assertTrue(text)
                self.assertFalse(future.done())
            finally:
                release.set()
            future.result(5)
        self.assertGreaterEqual(self.service.load('10001').affection, 8.0)
        self.assertEqual(self.store.get_setting('parallel_write'), 'ok')
    def test_forget_during_judge_discards_completed_result(self):
        entered, release = threading.Event(), threading.Event()
        def judge(*a):
            entered.set()
            self.assertTrue(release.wait(5))
            return VERDICT
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._blocking_run, judge)
            self.assertTrue(entered.wait(5))
            try:
                self.store.purge_user_facts(self.session.as_text(), '10001')
                self.service.forget('10001')
                self.assertFalse(future.done())
            finally:
                release.set()
            self.assertEqual(future.result(5), {'discarded': True, 'reason': 'forgotten'})
        self.assertEqual(self.store._conn().execute('SELECT COUNT(*) FROM user_relationship').fetchone()[0], 0)
        self.assertEqual(self.service.recent_log('10001'), [])

if __name__ == '__main__':
    unittest.main()
