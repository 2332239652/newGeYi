"""隔离SQLite回放：共同经历、承诺证据及在途删除。无网络/真实API。"""
from __future__ import annotations

import copy
import os
import sqlite3
import tempfile
import threading
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_episode_data_'))

from newGeYi.memory.episodes import EpisodeMemory, _RESIDENT_MARK
from newGeYi.storage.database import SQLiteStore

SESSION = 'bot:qq:onebot:group:-:100'
UID = '200'


class EpisodeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_episode_test_')
        self.path = os.path.join(self.tmp.name, 'test.sqlite3')
        self.store = SQLiteStore(self.path)
        self.episodes = EpisodeMemory(self.store, {})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, role, content, uid=UID, space='chat', accepted=True, session=SESSION, **meta):
        metadata = {'space_id': space, 'message_id': 'message-1', **meta}
        if role == 'assistant':
            metadata.update(recipient_id=uid, delivery_level='accepted' if accepted else 'queued')
        rid = self.store.buffer_append(session, role, content,
                                       sender_id=uid if role == 'user' else '', metadata=metadata)
        return self.store.buffer_fetch(session, rid - 1, 1)[0]

    def pair(self, space='chat', uid=UID):
        return [self.append('user', '我和你约好明天一起烤面包', uid, space),
                self.append('assistant', '我答应你，我和你约好明天一起烤面包', uid, space)]

    @staticmethod
    def sources(rows):
        return [{'record_id': r['id'], 'quote': r['content']} for r in rows]

    def event(self, rows, **updates):
        return {'user_id': UID, 'summary': '我和你约好明天一起烤面包', 'evidence_type': 'co_enacted',
                'sources': self.sources(rows), **updates}

    def commitment(self, rows, **updates):
        return {'user_id': UID, 'content': '明天一起烤面包', 'status': 'open',
                'sources': self.sources(rows), **updates}

    def apply(self, rows, events=None, changes=None, snap=None):
        return self.episodes.validate_and_apply(SESSION, {'shared_events': events or [],
                                                         'commitment_changes': changes or []}, rows, snap)

    def test_metadata_legacy_and_turn_round_trip(self):
        rid = self.store.buffer_append(SESSION, 'user', '旧消息', UID)
        self.assertEqual({}, self.store.buffer_fetch(SESSION, rid - 1)[0]['metadata'])
        self.store.add_turn(SESSION, 'user', '剧情', metadata={'space_id': 'story:a'})
        self.assertEqual('story:a', self.store.recent_turns(SESSION)[0]['metadata']['space_id'])
        with self.assertRaises(ValueError):
            self.store.buffer_append(SESSION, 'user', 'bad', metadata=[])

    def test_additive_migration_preserves_old_rows(self):
        path = os.path.join(self.tmp.name, 'legacy.sqlite3')
        conn = sqlite3.connect(path)
        conn.executescript('CREATE TABLE memory_buffer(id INTEGER PRIMARY KEY AUTOINCREMENT,session_key TEXT,'
                           'role TEXT,sender_id TEXT,sender_name TEXT,content TEXT,created_at INTEGER);'
                           "INSERT INTO memory_buffer VALUES(1,'s','user','u','','legacy',1);")
        conn.close()
        legacy = SQLiteStore(path)
        try:
            self.assertEqual('legacy', legacy.buffer_fetch('s')[0]['content'])
            self.assertEqual({}, legacy.buffer_fetch('s')[0]['metadata'])
        finally:
            legacy.close()

    def test_create_event_and_commitment_with_durable_evidence(self):
        rows = self.pair(space='story:a')
        result = self.apply(rows, [self.event(rows)], [self.commitment(rows)])
        known = self.episodes.list_for_user(SESSION, UID, 'story:a')
        self.assertEqual(1, len(known['events']))
        self.assertEqual('open', known['commitments'][0]['status'])
        self.assertEqual(1, known['commitments'][0]['version'])
        self.assertEqual(result['events'][0], known['events'][0]['id'])
        self.store._conn().execute('DELETE FROM memory_buffer')
        ev = self.store._conn().execute('SELECT quote,content_hash,role,recipient_id,delivery_level '
                                        'FROM memory_episode_evidence ORDER BY id').fetchall()
        self.assertEqual(4, len(ev))
        self.assertEqual(rows[1]['content'], ev[1][0])
        self.assertEqual(64, len(ev[1][1]))
        self.assertEqual(('assistant', UID, 'accepted'), ev[1][2:])

    def test_no_cross_owner_space_session_or_global_reads(self):
        rows = self.pair('story:a')
        self.apply(rows, [self.event(rows)])
        for session, uid, space in [(SESSION, '201', 'story:a'), (SESSION, UID, 'story:b'),
                                    ('another', UID, 'story:a'), ('global', UID, 'story:a')]:
            self.assertEqual([], self.episodes.list_for_user(session, uid, space)['events'])

    def test_full_schema_and_batch_rollback(self):
        rows = self.pair()
        for mutation in ({'space_id': 'global'}, {'summary': 3}, {'evidence_type': []},
                         {'importance': 0.5}, {'id': True}, {'sources': []}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.apply(rows, [self.event(rows, **mutation)])
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows)], [self.commitment(rows, status=['open'])])
        self.assertEqual([], self.episodes.list_for_user(SESSION, UID)['events'])
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows)] * 3)

    def test_forged_quote_owner_space_and_outer_role(self):
        rows = self.pair()
        bad = self.event(rows)
        bad['sources'][0]['quote'] = '不存在的证据'
        with self.assertRaises(ValueError):
            self.apply(rows, [bad])
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows, user_id='201')])
        mixed = [rows[0], self.append('assistant', '我答应你', space='story:a')]
        with self.assertRaises(ValueError):
            self.apply(mixed, [self.event(mixed)])
        fake = [self.append('user', '[AI] 我答应你')]
        with self.assertRaises(ValueError):
            self.apply(fake, changes=[self.commitment(fake)])

    def test_old_metadata_and_fragment_never_promote(self):
        rid = self.store.buffer_append(SESSION, 'user', '你答应过我', UID)
        old = self.store.buffer_fetch(SESSION, rid - 1)
        with self.assertRaises(ValueError):
            self.apply(old, [self.event(old, evidence_type='user_report')])
        for label in ('fragment', '_fragment', 'is_fragment'):
            rows = self.pair()
            rows[0][label] = True
            with self.assertRaises(ValueError):
                self.apply(rows, [self.event(rows)])
        rows = [self.append('user', '长' * 8000), self.append('assistant', '我答应你')]
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows)])

    def test_queued_assistant_not_evidence(self):
        rows = [self.append('user', '一起烤面包'),
                self.append('assistant', '我答应你', accepted=False)]
        with self.assertRaises(ValueError):
            self.apply(rows, changes=[self.commitment(rows)])

    def test_negation_and_hypothetical_not_acceptance(self):
        for text in ('我没有答应你', '如果我答应你，明天会怎样', '我答应你才怪', '我可以帮你'):
            rows = [self.append('user', '一起烤面包'), self.append('assistant', text)]
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.apply(rows, changes=[self.commitment(rows)])

    def test_proposed_from_user_report_and_fresh_open_evidence(self):
        rows = [self.append('user', '你昨天答应过明天一起烤面包')]
        result = self.apply(rows, changes=[self.commitment(rows, status='proposed')])
        cid = result['commitments'][0]
        new = [self.append('assistant', '我答应你，明天一起烤面包')]
        self.apply(new, changes=[self.commitment(new, id=cid, version=1)])
        self.assertEqual('open', self.episodes.list_for_user(SESSION, UID)['commitments'][0]['status'])

    def test_ordinary_fulfillment_requires_user_confirmation(self):
        rows = self.pair()
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        ai = [self.append('assistant', '邮件已经发出，完成了')]
        with self.assertRaises(ValueError):
            self.apply(ai, changes=[self.commitment(ai, id=cid, version=1, status='fulfilled', result='邮件发出')])
        unrelated = [self.append('user', '邮件收到，完成了')]
        with self.assertRaises(ValueError):
            self.apply(unrelated, changes=[self.commitment(unrelated, id=cid, version=1, status='fulfilled', result='邮件收到')])
        user = [self.append('user', '烤面包已经完成，面包收到')]
        self.apply(user, changes=[self.commitment(user, id=cid, version=1, status='fulfilled', result='面包收到')])
        self.assertEqual('fulfilled', self.episodes.list_for_user(SESSION, UID)['commitments'][0]['status'])
        again = self.pair()
        with self.assertRaises(ValueError):
            self.apply(again, changes=[self.commitment(again, id=cid, version=2)])

    def test_negative_user_result_not_completion(self):
        rows = self.pair()
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        rows = [self.append('user', '还没完成，邮件没收到')]
        bad = self.commitment(rows, id=cid, version=1, status='fulfilled', result='收到')
        bad['sources'][0]['quote'] = '收到'
        with self.assertRaises(ValueError):
            self.apply(rows, changes=[bad])

    def test_story_fulfillment_accepts_enacted_assistant(self):
        rows = self.pair('story:a')
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        done = [self.append('assistant', '（把烤好的面包递给你）这次烤好了', space='story:a')]
        self.apply(done, changes=[self.commitment(done, id=cid, version=1, status='fulfilled', result='烤好的面包')])
        self.assertEqual('fulfilled', self.episodes.list_for_user(SESSION, UID, 'story:a')['commitments'][0]['status'])

    def test_version_cas_and_idempotency(self):
        rows = self.pair()
        snap = self.episodes.snapshot(SESSION, rows)
        data = {'commitment_changes': [self.commitment(rows)]}
        first = self.episodes.validate_and_apply(SESSION, data, rows, snap)
        second = self.episodes.validate_and_apply(SESSION, data, rows, snap)
        self.assertEqual(first['commitments'], second['commitments'])
        self.assertEqual(1, second['idempotent'])
        cid = first['commitments'][0]
        rows = self.pair()
        snap = self.episodes.snapshot(SESSION, rows)
        data = {'commitment_changes': [self.commitment(rows, id=cid, version=1)]}
        plan = self.episodes.validate(SESSION, data, rows, snap)
        self.store._conn().execute('UPDATE memory_commitments SET version=2 WHERE id=?', (cid,))
        with self.assertRaises(ValueError):
            self.episodes.apply(SESSION, plan)

    def test_idempotency_conflict_and_duplicate_sources(self):
        rows = self.pair()
        self.apply(rows, changes=[self.commitment(rows)])
        with self.assertRaises(ValueError):
            self.apply(rows, changes=[self.commitment(rows, content='同一来源不同内容')])
        bad = self.event(rows)
        bad['sources'].append(dict(bad['sources'][0]))
        with self.assertRaises(ValueError):
            self.apply(rows, [bad])

    def test_apply_failure_rolls_back_entire_batch_inside_outer_transaction(self):
        rows = self.pair()
        snap = self.episodes.snapshot(SESSION, rows)
        data = {'shared_events': [self.event(rows)], 'commitment_changes': [self.commitment(rows)]}
        plan = self.episodes.validate(SESSION, data, rows, snap)
        self.store._conn().execute("CREATE TRIGGER test_failure BEFORE INSERT ON memory_commitments "
                                   "BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.store.transaction():
            with self.assertRaises(sqlite3.IntegrityError):
                self.episodes.apply(SESSION, plan)
            self.store.set_setting('outer_survives', '1')
        self.assertEqual([], self.episodes.list_for_user(SESSION, UID)['events'])
        self.assertEqual('1', self.store.get_setting('outer_survives'))

    def test_superseding_uses_programmatic_bidirectional_cas(self):
        rows = self.pair()
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        rows = [self.append('user', '改为后天烤面包吧'), self.append('assistant', '我答应你，后天烤面包')]
        new = self.apply(rows, changes=[self.commitment(rows, content='后天烤面包', supersedes_id=cid)])['commitments'][0]
        known = {r['id']: r for r in self.episodes.list_for_user(SESSION, UID)['commitments']}
        self.assertEqual('superseded', known[cid]['status'])
        self.assertEqual(new, known[cid]['superseded_by'])
        self.assertEqual(cid, known[new]['supersedes_id'])

    def test_delete_inflight_and_restart_no_resurrection(self):
        rows = self.pair()
        eid = self.apply(rows, [self.event(rows)])['events'][0]
        snap = self.episodes.snapshot(SESSION, rows)
        self.store.add_group_summary(SESSION, '旧面包摘要')
        self.store.add_self_memory(SESSION, '旧面包约定', 'promise')
        self.store.set_setting('scene:' + SESSION, '旧纸条')
        self.assertTrue(self.episodes.delete_for_user(SESSION, UID, 'event', eid))
        self.assertEqual([], self.store.recent_group_summaries(SESSION))
        self.assertEqual([], self.store.list_self_memories(SESSION))
        self.assertIsNone(self.store.get_setting('scene:' + SESSION))
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows)], snap=snap)
        self.store.close()
        self.store = SQLiteStore(self.path)
        self.episodes = EpisodeMemory(self.store)
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows)])
        tomb = self.store._conn().execute('SELECT * FROM memory_episode_tombstones').fetchall()
        self.assertEqual(1, len(tomb))
        self.assertEqual(0, self.store._conn().execute('SELECT COUNT(*) FROM memory_episode_evidence').fetchone()[0])
        fresh = self.pair()
        self.apply(fresh, [self.event(fresh)])
        self.assertEqual(1, len(self.episodes.list_for_user(SESSION, UID)['events']))

    def test_forget_user_preserves_other_owner_and_clears_shared_derivatives(self):
        rows = self.pair()
        self.apply(rows, [self.event(rows)], [self.commitment(rows)])
        other = self.pair(uid='201')
        self.apply(other, [self.event(other, user_id='201')])
        self.store.add_group_summary(SESSION, '混合内容')
        self.store.purge_user_facts(SESSION, UID)
        self.assertEqual([], self.episodes.list_for_user(SESSION, UID)['events'])
        self.assertEqual([], self.episodes.list_for_user(SESSION, UID)['commitments'])
        self.assertEqual(1, len(self.episodes.list_for_user(SESSION, '201')['events']))
        self.assertEqual([], self.store.recent_group_summaries(SESSION))
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows)])

    def test_management_atomic_owner_space_and_manual_cas(self):
        rows = self.pair('story:a')
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        self.assertFalse(self.episodes.delete_for_user(SESSION, UID, 'commitment', cid))
        self.assertFalse(self.episodes.correct_for_user(SESSION, '201', 'commitment', cid, '修改', space_id='story:a'))
        self.assertTrue(self.episodes.correct_for_user(SESSION, UID, 'commitment', cid, '后天烤面包',
                                                      expected_version=1, space_id='story:a'))
        with self.assertRaises(ValueError):
            self.episodes.set_status_for_user(SESSION, UID, 'commitment', cid, 'fulfilled',
                                              expected_version=1, space_id='story:a')
        self.assertTrue(self.episodes.set_status_for_user(SESSION, UID, 'commitment', cid, 'cancelled',
                                                         expected_version=2, space_id='story:a'))
        self.assertEqual('cancelled', self.episodes.list_for_user(SESSION, UID, 'story:a')['commitments'][0]['status'])

    def test_cross_connection_concurrent_version_update(self):
        rows = self.pair()
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        rows = self.pair()
        snap = self.episodes.snapshot(SESSION, rows)
        plan = self.episodes.validate(SESSION, {'commitment_changes': [self.commitment(rows, id=cid, version=1)]}, rows, snap)
        failures = []
        def worker():
            try:
                with self.store.transaction() as conn:
                    conn.execute('UPDATE memory_commitments SET version=2 WHERE id=?', (cid,))
            except Exception as exc:
                failures.append(exc)
            finally:
                self.store.close()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        with self.assertRaises(ValueError):
            self.episodes.apply(SESSION, plan)

    def test_snapshot_generation_and_live_evidence_recheck(self):
        rows = self.pair()
        snap = self.episodes.snapshot(SESSION, rows)
        plan = self.episodes.validate(SESSION, {'shared_events': [self.event(rows)]}, rows, snap)
        self.store.set_setting('scene.generation:' + SESSION, '1')
        with self.assertRaises(ValueError):
            self.episodes.apply(SESSION, plan)
        self.store.set_setting('scene.generation:' + SESSION, '0')
        self.store._conn().execute('UPDATE memory_buffer SET content=? WHERE id=?', ('changed', rows[0]['id']))
        with self.assertRaises(ValueError):
            self.episodes.apply(SESSION, plan)

    def test_recall_relevance_and_no_mutating_hits(self):
        rows = self.pair()
        self.apply(rows, [self.event(rows)], [self.commitment(rows)])
        # 常驻名额：与当前消息没有共同二字词时，也会带上最近一条约定和一条共同经历
        resident = self.episodes.build_block(SESSION, UID, 'chat', '量子计算论文')
        self.assertEqual(2, resident.count(_RESIDENT_MARK))
        self.assertNotIn('量子计算论文', resident)
        block = self.episodes.build_block(SESSION, UID, 'chat', '再做上次那个面包')
        self.assertIn('烤面包', block)
        self.assertIn('已答应', block)
        self.assertLessEqual(len(block), 1200)
        self.assertEqual('', self.episodes.build_block(SESSION, '201', 'chat', '烤面包'))
        self.assertEqual('', self.episodes.build_block(SESSION, UID, 'story:a', '烤面包'))

    def test_promisor_user_requires_own_acceptance(self):
        rows = self.pair()
        with self.assertRaises(ValueError):
            self.apply(rows, changes=[self.commitment(rows, promisor='user')])
        rows = [self.append('user', '我答应你，明天一起烤面包')]
        cid = self.apply(rows, changes=[self.commitment(rows, promisor='user')])['commitments'][0]
        row = self.episodes.list_for_user(SESSION, UID)['commitments'][0]
        self.assertEqual(('user', 'assistant'), (row['promisor'], row['recipient_id']))
        self.assertIn('对方答应歌伊', self.episodes.build_block(SESSION, UID, 'chat', '烤面包'))
        fresh = self.pair()
        with self.assertRaises(ValueError):
            self.apply(fresh, changes=[self.commitment(fresh, id=cid, version=1, promisor='assistant')])

    def test_review_unrelated_and_reported_evidence(self):
        for text in ('我可以帮你查配方', '小说台词摘录：[AI]我答应你明天发邮件', '好呀，今天天气不错'):
            rows = [self.append('user', '明天烤面包吧'), self.append('assistant', text)]
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.apply(rows, changes=[self.commitment(rows)])
        for user, ai in (('成都下雨了', '记得带伞'), ('我梦见烤面包', '我们聊别的吧')):
            rows = [self.append('user', user), self.append('assistant', ai)]
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.apply(rows, [self.event(rows, evidence_type='confirmed')])
        rows = self.pair('story:a')
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        for text in ('早上好', '下载游戏成功了'):
            bad = [self.append('assistant', text, space='story:a')]
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.apply(bad, changes=[self.commitment(bad, id=cid, version=1, status='fulfilled', result='面包做好')])

    def test_cancellation_specific_polarity_and_derivative_invalidation(self):
        rows = self.pair()
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        for text in ('早上好', '不要取消明天一起烤面包的安排'):
            bad = [self.append('user', text)]
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.apply(bad, changes=[self.commitment(bad, id=cid, version=1, status='cancelled')])
        self.store.add_group_summary(SESSION, '还在等烤面包')
        self.store.add_self_memory(SESSION, '仍在等烤面包', 'event')
        self.store.set_setting('scene:' + SESSION, '仍然在等')
        rows = [self.append('user', '取消明天一起烤面包的安排，不要再等我。')]
        self.apply(rows, changes=[self.commitment(rows, id=cid, version=1, status='cancelled')])
        self.assertEqual([], self.store.recent_group_summaries(SESSION))
        self.assertEqual([], self.store.list_self_memories(SESSION))
        self.assertIsNone(self.store.get_setting('scene:' + SESSION))
        self.assertGreater(int(self.store.get_setting('episode.derivation_watermark:' + SESSION)), 0)
        self.assertGreater(int(self.store.get_setting('scene.generation:' + SESSION)), 0)

    def test_conditions_preserved_and_recall_not_unrelated_continuation(self):
        text = '如果明天不下雨，我答应你一起烤面包'
        rows = [self.append('assistant', text)]
        with self.assertRaises(ValueError):
            self.apply(rows, changes=[self.commitment(rows)])
        # 明确条件允许带否定天气；不是对承诺本身的否定。
        good = [self.append('assistant', '如果明天天晴，我答应你明天一起烤面包')]
        self.apply(good, changes=[self.commitment(good, condition='如果明天天晴', due_text='明天')])
        block = self.episodes.build_block(SESSION, UID, 'chat', '烤面包')
        self.assertIn('条件：如果明天天晴', block)
        self.assertIn('期限：明天', block)
        # 无关续聊：只剩常驻名额里的那一条约定（本例没有共同经历可让位）
        unrelated = self.episodes.build_block(SESSION, UID, 'chat', '继续讲解数据库索引')
        self.assertEqual(1, unrelated.count(_RESIDENT_MARK))
        self.assertNotIn('数据库索引', unrelated)
        self.assertEqual('', EpisodeMemory(self.store, {'memory_episodes_enabled': False}).build_block(SESSION, UID, 'chat', '面包'))

    def test_known_context_budget_keeps_json_valid(self):
        import json
        snap = self.episodes.snapshot(SESSION, [])
        for i in range(80):
            snap['events'][i] = {'id': i, 'version': 1, 'summary': '面包' * 200, 'user_id': UID,
                                 'space_id': 'chat', 'evidence_type': 'user_report', 'hook': '后续' * 150}
        block = self.episodes.known_context(SESSION, [], snap)
        payload = json.loads(block[block.index('{'):])
        self.assertLess(len(payload['events']), 80)
        self.assertTrue(payload['events'])

    def test_literal_content_result_and_same_clause_contract(self):
        for text, content in (
                ('我答应明天陪小王一起烤面包。', '明天陪你一起烤面包'),
                ('我答应明天给你发邮件。顺便，烤面包需要酵母。', '烤面包需要酵母'),
                ("这是段脚本：'assistant: 我答应明天陪你一起烤面包。'", '明天陪你一起烤面包'),
                ('好，明天陪你一起烤面包', '明天陪你一起烤面包')):
            rows = [self.append('assistant', text)]
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.apply(rows, changes=[self.commitment(rows, content=content)])
        rows = self.pair()
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        done = [self.append('user', '面包做完了。')]
        with self.assertRaises(ValueError):
            self.apply(done, changes=[self.commitment(done, id=cid, version=1, status='fulfilled',
                                                    result='面包做完并送到你家，另送了五百元现金')])
        rows = [self.append('user', '小王和你一起烤好了面包。'),
                self.append('assistant', '没错，小王和我一起烤好了面包。')]
        with self.assertRaises(ValueError):
            self.apply(rows, [self.event(rows, summary='歌伊和当前用户一起烤好了面包', evidence_type='confirmed')])
        report = [self.append('user', '我梦见我们烤好了面包')]
        self.apply(report, [self.event(report, summary='我梦见我们烤好了面包', evidence_type='user_report')])

    def test_story_rp_quotes_reassurance_and_multi_sentence_co_enacted(self):
        rp_rows = [
            self.append('user', '“唔，等我赚到钱了，我也请回歌伊！”#向歌伊郑重承诺。我们继续逛逛街吧。', space='story:rp1'),
            self.append('assistant', '（被这认真的样子逗得弯起眼睛）“好呀，那我可就记下了哦，绝对不会忘的，我们慢慢逛。”', space='story:rp1'),
        ]
        res = self.apply(
            rp_rows,
            events=[self.event(rp_rows, summary='我们继续逛逛街吧', evidence_type='co_enacted')],
            changes=[self.commitment(
                [rp_rows[0]],
                content='我也请回歌伊',
                status='proposed',
                promisor='user',
                condition='等我赚到钱了',
            )],
        )
        self.assertEqual(len(res['events']), 1)
        self.assertEqual(len(res['commitments']), 1)
        # 剧情中共演：一方提出共同行动、另一方短应答“好哦~”并跟随后，也应允许通过（不放宽普通chat）
        pair_story = [
            self.append('assistant', '先跟我回教会吧', space='story:rp1'),
            self.append('user', '“好哦~”＃跟着歌伊', space='story:rp1'),
        ]
        self.assertEqual(
            len(self.apply(pair_story, events=[self.event(pair_story, summary='先跟我回教会吧', evidence_type='co_enacted')])['events']),
            1,
        )
        # 含第三人称舞台描写的普通chat仍拒绝，而剧情中剥离括号动作后允许
        story_stage = [
            self.append('user', '（路边摊主递来木勺）把雕刻好的木雕给歌伊', space='story:rp1'),
            self.append('assistant', '（双手捧过小木雕，眼里弯起笑意）哇，这木雕我超级喜欢！', space='story:rp1'),
        ]
        self.assertEqual(
            len(self.apply(story_stage, events=[self.event(story_stage, summary='把雕刻好的木雕给歌伊', evidence_type='co_enacted')])['events']),
            1,
        )
        # 肯定句伴随问句分句时，不因无关问句误杀
        reassure = [self.append('assistant', '好呀，去吃早茶吗？我答应明天陪你去吃早茶，我不会忘的。')]
        self.assertEqual(
            len(self.apply(reassure, changes=[self.commitment(reassure, content='明天陪你去吃早茶')])['commitments']),
            1,
        )

    def test_terminal_exact_replay_and_outer_role_change(self):
        rows = self.pair()
        cid = self.apply(rows, changes=[self.commitment(rows)])['commitments'][0]
        done = [self.append('user', '烤面包完成了，面包收到')]
        snap = self.episodes.snapshot(SESSION, done)
        plan = self.episodes.validate(SESSION, {'commitment_changes': [self.commitment(
            done, id=cid, version=1, status='fulfilled', result='面包收到')]}, done, snap)
        self.episodes.apply(SESSION, plan)
        self.assertEqual(1, self.episodes.apply(SESSION, plan)['idempotent'])
        fresh = self.pair()
        plan = self.episodes.validate(SESSION, {'shared_events': [self.event(fresh)]}, fresh,
                                      self.episodes.snapshot(SESSION, fresh))
        self.store._conn().execute("UPDATE memory_buffer SET role='assistant' WHERE id=?", (fresh[0]['id'],))
        with self.assertRaises(ValueError):
            self.episodes.apply(SESSION, plan)

    def test_legacy_self_memory_not_migrated(self):
        self.store.add_self_memory(SESSION, '我答应过用户烤面包', 'promise')
        self.assertEqual([], self.episodes.list_for_user(SESSION, UID)['commitments'])
        self.assertEqual('', self.episodes.build_block(SESSION, UID, 'chat', '面包'))


if __name__ == '__main__':
    unittest.main()
