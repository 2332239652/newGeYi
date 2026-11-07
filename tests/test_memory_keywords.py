# -*- encoding: utf-8 -*-
"""记忆的「信号密度」：视频降噪 + 写入端关键词 + 容量与视频封顶。

骰主 2026-11-05 的两条要求：
1. 「存更多的同时做最后一条」——容量放大到 50/3000，靠**写入时给关键词**来保召回；
2. 「分享视频特别容易进记忆，记忆里只能看到 bv 号，意义不大」——入库前把编号洗掉，
   纯分享整条不记，视频类事实单独封顶。

为什么这些必须有测试：这两件事坏掉都不会报错，只会让记忆慢慢变脏/变傻。
"""

from __future__ import annotations

import os
import tempfile
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_kw_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.memory.curator import clean_video_noise  # noqa: E402
from newGeYi.memory.repository import relevance_score  # noqa: E402
from newGeYi.storage.database import (  # noqa: E402
    SQLiteStore, normalize_keywords, split_keywords,
)


class VideoNoiseTest(unittest.TestCase):
    def test_bv_only_message_is_dropped_as_pure_share(self):
        # "歌伊看看BV1xx"：去掉编号后只剩「歌伊看看」这种指令词 → 整条不记（本来也没信息量）
        text, pure = clean_video_noise('歌伊看看BV1cPuAzDEZP')
        self.assertTrue(pure)
        self.assertEqual(text, '')

    def test_bare_link_is_dropped_entirely(self):
        for raw in ('BV1cPuAzDEZP', 'https://b23.tv/abcDEF12', 'https://www.bilibili.com/video/BV1xx411c7mD'):
            text, pure = clean_video_noise(raw)
            self.assertTrue(pure, raw)
            self.assertEqual(text, '')

    def test_link_with_no_content_is_dropped(self):
        text, pure = clean_video_noise('歌伊快看这个 https://b23.tv/abcDEF12')
        self.assertTrue(pure, '去掉指令词和链接后没有实质内容，应整条丢弃')
        self.assertEqual(text, '')

    def test_link_plus_real_sentence_is_kept(self):
        text, pure = clean_video_noise('今天好累啊，但还是想给你看 BV1cPuAzDEZP 这个')
        self.assertFalse(pure)
        self.assertNotIn('BV1cPuAzDEZP', text)
        self.assertIn('今天好累啊', text)

    def test_normal_message_untouched(self):
        text, pure = clean_video_noise('今天晚饭吃了拉面')
        self.assertFalse(pure)
        self.assertEqual(text, '今天晚饭吃了拉面')

    def test_op_codes_are_stripped(self):
        text, pure = clean_video_noise('[OP:at,id=123] 今天去看了海')
        self.assertFalse(pure)
        self.assertNotIn('OP:at', text)
        self.assertIn('今天去看了海', text)


class KeywordWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='geyi_kw_db_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'kw.sqlite3'))
        self.store.set_setting('facts.keywords_backfilled', '1')   # 别在用例里回填全库

    def tearDown(self):
        self.store.close()

    def test_model_keywords_are_stored(self):
        self.store.add_user_fact('h:qq:onebot:group:-:1', '10086', '喜欢跑团',
                                 category='偏好', keywords=['TRPG', '桌游', 'coc'])
        row = self.store.list_user_facts('h:qq:onebot:group:-:1', '10086', limit=5)[0]
        self.assertEqual(split_keywords(row['keywords']), ['TRPG', '桌游', 'coc'])

    def test_no_local_fallback_for_keywords(self):
        # 本地分词灌出来的二元组（"喜欢/和牛/的小"）会把"喜欢"这种词变成万能命中，
        # 所以 keywords 只收模型给的；没有就留空，交给 fact 原文匹配。
        self.store.add_user_fact('h:qq:onebot:group:-:1', '10086', '家里养了一只猫叫米粒')
        row = self.store.list_user_facts('h:qq:onebot:group:-:1', '10086', limit=5)[0]
        self.assertEqual(row['keywords'], '')

    def test_keywords_are_deduped_and_capped(self):
        many = ['a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7', 'a8', 'a9', 'a10', 'a1']
        self.assertEqual(len(split_keywords(normalize_keywords(many))), 8)

    def test_facts_matching_finds_by_keyword(self):
        sk = 'h:qq:onebot:group:-:1'
        self.store.add_user_fact(sk, '10086', '喜欢玩桌游', keywords=['跑团', 'TRPG'])
        self.store.add_user_fact(sk, '10086', '在准备考研', keywords=['考试', '研究生'])
        hits = self.store.facts_matching(sk, '10086', ['trpg'], limit=10)
        self.assertEqual(len(hits), 1)
        self.assertIn('桌游', hits[0]['fact'])

    def test_relevance_prefers_keyword_hit(self):
        # 原话里没有「桌游」两个字，但关键词里有 → 仍然应该判为高相关
        score_with = relevance_score('今晚跑团吗', '喜欢玩桌游', '跑团,TRPG')
        score_without = relevance_score('今晚跑团吗', '喜欢玩桌游', '')
        self.assertGreater(score_with, score_without)
        self.assertGreaterEqual(score_with, 0.9)


class VideoShareCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='geyi_kw_cap_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'cap.sqlite3'))
        self.sk = 'h:qq:onebot:group:-:1'
        self.uid = '10086'

    def tearDown(self):
        self.store.close()

    def _add(self, fact, category='', source='inferred'):
        return self.store.add_user_fact(self.sk, self.uid, fact, category=category, source=source)

    def test_video_facts_are_capped_regardless_of_total(self):
        for i in range(5):
            self._add('向歌伊安利了B站视频《第%d弹》' % i, category='bili_share', source='explicit')
        # 总量远没到上限，但视频要单独封顶到 3 条
        removed = self.store.trim_user_facts(self.sk, self.uid, max_per_user=50,
                                             max_per_group=0, video_share_keep=3)
        self.assertEqual(removed, 2)
        rows = self.store.list_user_facts(self.sk, self.uid, limit=10)
        self.assertEqual(len(rows), 3)
        self.assertIn('第4弹', ''.join(r['fact'] for r in rows), '应保留最新的几条')

    def test_handwritten_fact_is_still_protected(self):
        for i in range(3):
            self._add('普通推断事实%d' % i)
        self._add('记住：他生日是 3 月 5 日', source='explicit')
        self.store.trim_user_facts(self.sk, self.uid, max_per_user=2, max_per_group=0)
        rows = self.store.list_user_facts(self.sk, self.uid, limit=10)
        self.assertIn('生日', ''.join(r['fact'] for r in rows), '手工记的不该被淘汰')


class CuratorKeywordFlowTest(unittest.TestCase):
    """提炼模型的 keywords 要真的落进库（这是"存更多"能保召回的最后一米）。"""

    def setUp(self):
        from newGeYi.memory.curator import MemoryCurator
        self.tmp = tempfile.mkdtemp(prefix='geyi_kw_flow_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'flow.sqlite3'))
        self.store.set_setting('facts.keywords_cleanup', '1')
        self.config = {'assistant_name': '歌伊', 'memory_max_facts_per_user': 50,
                       'memory_max_facts_per_group': 3000, 'memory_video_share_keep': 3,
                       'memory_cross_group_enabled': True}
        self.curator = MemoryCurator(self.config, self.store, None)
        self.sk = 'h:qq:onebot:group:-:1'

    def tearDown(self):
        self.store.close()

    def test_model_keywords_are_persisted(self):
        data = {'user_facts': [{
            'user_id': '10086', 'fact': '喜欢跑团', 'category': '偏好', 'importance': 0.7,
            'scope': 'group', 'keywords': 'TRPG,桌游,coc',
        }]}
        stats = self.curator._store_results(self.sk, data, {'10086'}, last_id=1)
        self.assertEqual(stats['facts'], 1)
        row = self.store.list_user_facts(self.sk, '10086', limit=5)[0]
        self.assertEqual(split_keywords(row['keywords']), ['TRPG', '桌游', 'coc'])

    def test_fact_without_keywords_still_saved(self):
        data = {'user_facts': [{'user_id': '10086', 'fact': '在准备考研', 'importance': 0.6}]}
        self.curator._store_results(self.sk, data, {'10086'}, last_id=1)
        row = self.store.list_user_facts(self.sk, '10086', limit=5)[0]
        self.assertEqual(row['fact'], '在准备考研')
        self.assertEqual(row['keywords'], '')


class BufferVideoSkipTest(unittest.TestCase):
    def setUp(self):
        from newGeYi.memory.curator import MemoryCurator
        self.tmp = tempfile.mkdtemp(prefix='geyi_kw_buf_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'buf.sqlite3'))
        self.curator = MemoryCurator({'assistant_name': '歌伊'}, self.store, None)

    def tearDown(self):
        self.store.close()

    @staticmethod
    def _msg(text):
        import types

        class _S:
            def as_text(self_inner):
                return 'h:qq:onebot:group:-:1'

        return types.SimpleNamespace(text=text, sender_id='10086', sender_name='测试',
                                     sender_is_bot=False, session=_S())

    def test_pure_video_share_never_enters_buffer(self):
        self.assertEqual(self.curator.buffer_user(self._msg('BV1cPuAzDEZP')), 0)
        self.assertEqual(self.store.buffer_pending('h:qq:onebot:group:-:1', 0), 0)

    def test_video_with_speech_is_buffered_without_bv(self):
        self.curator.buffer_user(self._msg('今天好累，想给你看 BV1cPuAzDEZP'))
        rows = self.store.buffer_fetch('h:qq:onebot:group:-:1', 0, limit=5)
        self.assertEqual(len(rows), 1)
        self.assertNotIn('BV1cPuAzDEZP', rows[0]['content'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
