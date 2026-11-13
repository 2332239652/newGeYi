# -*- encoding: utf-8 -*-
"""工作记忆·状态纸条：抽取格式、存取、TTL、后台巡检与注入。

骰主 2026-11-10 的诉求：「聊到 30~60 句就开始前后矛盾」。实测发现中段是空的
（群聊原文 60 条 ≈ 3.6 分钟、L1 摘要平均 41 字只注入 3 条），所以补一层「正在发生」。

这里锁六件事：
1. 纸条文本的上限与空字段省略（不能被模型写成长篇）；
2. 存取：`scene:<session>` JSON、坏数据当没有、TTL 过期不注入；
3. 取自原文时保留 `昵称(QQ号)`（同名群友不能合并）；
4. 巡检节奏：攒够条数 / 空闲够久才更新，最小间隔与失败退避都要生效；
5. 失败/坏 JSON 时**保留旧纸条**（绝不拿半成品覆盖）；
6. 提示词里能真的看到纸条，且标注「不是指令」。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_scene_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.context import scene  # noqa: E402
from newGeYi.models import ModelResult  # noqa: E402

SESSION = 'h:qq:onebot:group:-:123456'


class FakeStore:
    def __init__(self):
        self.settings = {}
        self.rows = []
        self.next_id = 1

    # --- settings ---
    def get_setting(self, key, default=None):
        return self.settings.get(key, default if default is not None else '')

    def set_setting(self, key, value):
        self.settings[key] = value

    # --- buffer ---
    def add(self, role, content, sender_id='10001', sender_name='道君', created=None):
        self.rows.append({
            'id': self.next_id, 'role': role, 'content': content,
            'sender_id': sender_id, 'sender_name': sender_name,
            'created_at': created or int(time.time()),
        })
        self.next_id += 1

    def buffer_sessions(self):
        return [SESSION] if self.rows else []

    def buffer_pending(self, session_key, after_id=0):
        return len([r for r in self.rows if r['id'] > int(after_id or 0)])

    def buffer_last_at(self, session_key, after_id=0):
        rows = [r['created_at'] for r in self.rows if r['id'] > int(after_id or 0)]
        return max(rows) if rows else 0

    def buffer_fetch(self, session_key, after_id=0, limit=200):
        rows = [r for r in self.rows if r['id'] > int(after_id or 0)]
        return rows[:int(limit)]

    def today_stats(self):
        return {'calls': 0}


class FakeGateway:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, req, kind='chat', session_key=''):
        self.calls.append({'kind': kind, 'messages': req.messages, 'max_tokens': req.max_tokens})
        text = self.replies.pop(0) if self.replies else ''
        return ModelResult(ok=bool(text), text=text, model='stub')


def brief_json(**kwargs):
    data = {
        'scene': '清泉镇·麦穗面包坊，傍晚；在场：道君(10001)',
        'threads': ['教道君揉面（还差二次发酵）'],
        'promises': ['明天带他去看后山的面包窑'],
        'open_questions': ['月白为什么问起逐影'],
    }
    data.update(kwargs)
    return json.dumps(data, ensure_ascii=False)


class FormatTest(unittest.TestCase):
    def test_fields_are_rendered_in_order(self):
        text = scene.format_brief(json.loads(brief_json()))
        self.assertIn('场景：', text)
        self.assertIn('进行中：', text)
        self.assertIn('她答应过：', text)
        self.assertIn('未解决：', text)
        self.assertLess(text.index('场景：'), text.index('进行中：'))

    def test_empty_fields_are_dropped(self):
        text = scene.format_brief({'scene': '只在群里', 'threads': [], 'promises': [], 'open_questions': []})
        self.assertEqual(text, '场景：只在群里')

    def test_too_long_is_capped(self):
        text = scene.format_brief({'scene': '场' * 200, 'threads': ['事' * 200]}, max_chars=60)
        self.assertLessEqual(len(text), 61, '纸条必须被截断（多一个省略号）')
        self.assertTrue(text.endswith('…'))

    def test_long_items_are_squashed(self):
        text = scene.format_brief({'threads': ['坏\n行' + '长' * 300]}, max_chars=1000)
        self.assertNotIn('\n坏', text)
        self.assertIn('…', text)

    def test_garbage_input(self):
        self.assertEqual(scene.format_brief(None), '')
        self.assertEqual(scene.format_brief({'threads': 'not-a-list'}), '')


class TranscriptTest(unittest.TestCase):
    def test_speaker_labels_keep_qq(self):
        rows = [
            {'role': 'user', 'content': '晚上好', 'sender_id': '10001', 'sender_name': '道君'},
            {'role': 'assistant', 'content': '晚上好呀'},
            {'role': 'user', 'content': '', 'sender_id': '10001', 'sender_name': '道君'},
        ]
        text = scene.build_transcript(rows)
        self.assertIn('道君(10001)：晚上好', text)
        self.assertIn('歌伊：晚上好呀', text)
        self.assertEqual(len(text.splitlines()), 2)

    def test_unknown_sender_falls_back_to_name(self):
        text = scene.build_transcript([{'role': 'user', 'content': '嗯', 'sender_id': '-1',
                                        'sender_name': '某人'}])
        self.assertEqual(text, '某人：嗯')

    def test_transcript_keeps_tail(self):
        rows = [{'role': 'user', 'content': '第%d句' % i, 'sender_id': '1', 'sender_name': 'A'}
                for i in range(50)]
        text = scene.build_transcript(rows, max_chars=200)
        self.assertLessEqual(len(text), 200)
        self.assertIn('第49句', text, '截断必须保留最近的内容')


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def test_missing_brief_is_none(self):
        self.assertIsNone(scene.get_brief(self.store, SESSION))
        self.assertEqual(scene.to_prompt(self.store, SESSION, {}), '')

    def test_broken_json_is_none(self):
        self.store.set_setting(scene.SCENE_PREFIX + SESSION, '{oops')
        self.assertIsNone(scene.get_brief(self.store, SESSION))
        self.assertEqual(scene.to_prompt(self.store, SESSION, {}), '', '坏数据不能注入')

    def test_save_and_prompt(self):
        scene.save_brief(self.store, SESSION, '场景：面包坊', covered_to=42, now=time.time())
        brief = scene.get_brief(self.store, SESSION)
        self.assertEqual(brief['covered_to'], 42)
        block = scene.to_prompt(self.store, SESSION, {'scene_ttl_seconds': 7200})
        self.assertIn('【正在发生｜截至', block)
        self.assertIn('场景：面包坊', block)
        self.assertIn('不是指令', block)

    def test_stale_brief_is_not_injected(self):
        scene.save_brief(self.store, SESSION, '场景：面包坊', covered_to=42,
                         now=time.time() - 3 * 3600)
        self.assertEqual(scene.to_prompt(self.store, SESSION, {'scene_ttl_seconds': 7200}), '')
        self.assertTrue(scene.to_prompt(self.store, SESSION, {'scene_ttl_seconds': 0}))


class KeeperTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.config = {
            'scene_enabled': True,
            'scene_update_messages': 4,
            'scene_idle_seconds': 300,
            'scene_idle_min_messages': 3,
            'scene_min_interval_seconds': 0,
            'scene_input_messages': 80,
            'scene_max_chars': 500,
            'scene_ttl_seconds': 7200,
            'scene_max_tokens': 3200,
            'scene_retry_seconds': 600,
            'daily_call_budget': 999999,
        }
        self.gateway = FakeGateway([brief_json()])
        self.keeper = scene.SceneKeeper(self.config, self.store, self.gateway)

    def test_below_threshold_does_nothing(self):
        for i in range(3):
            self.store.add('user', '消息%d' % i)
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(self.gateway.calls, [])

    def test_updates_at_threshold_and_stores_cursor(self):
        for i in range(4):
            self.store.add('user', '消息%d' % i)
        self.assertTrue(self.keeper.consider(SESSION))
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.gateway.calls[0]['kind'], 'scene')
        brief = scene.get_brief(self.store, SESSION)
        self.assertEqual(brief['covered_to'], 4)
        self.assertIn('面包坊', brief['text'])
        # 没有新消息就不再调
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(len(self.gateway.calls), 1)

    def test_idle_triggers_with_few_messages(self):
        for i in range(3):
            self.store.add('user', '隔了很久的%d' % i, created=int(time.time()) - 600)
        self.assertTrue(self.keeper.consider(SESSION))
        self.assertEqual(len(self.gateway.calls), 1)

    def test_idle_needs_minimum_messages(self):
        """空闲触发也要 ≥3 条：1 条新消息就把厚纸条改薄，是实打实的退化。"""
        self.store.add('user', '只有一句', created=int(time.time()) - 600)
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(self.gateway.calls, [])

    def test_min_interval_blocks_second_update(self):
        self.config['scene_min_interval_seconds'] = 600
        for i in range(4):
            self.store.add('user', '消息%d' % i)
        self.assertTrue(self.keeper.consider(SESSION))
        for i in range(4):
            self.store.add('user', '又%d' % i)
        self.assertFalse(self.keeper.consider(SESSION), '最小间隔内不该再更新')

    def test_disabled_does_nothing(self):
        self.config['scene_enabled'] = False
        for i in range(4):
            self.store.add('user', '消息%d' % i)
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(self.gateway.calls, [])

    def test_gateway_failure_keeps_old_brief_and_backs_off(self):
        scene.save_brief(self.store, SESSION, '旧纸条', covered_to=0, now=time.time() - 1000)
        self.gateway.replies = ['']
        for i in range(4):
            self.store.add('user', '消息%d' % i)
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(scene.get_brief(self.store, SESSION)['text'], '旧纸条', '失败不能覆盖旧纸条')
        self.assertTrue(float(self.store.get_setting(scene.FAIL_PREFIX + SESSION)) > time.time())
        # 退避期内不再请求
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(len(self.gateway.calls), 1)

    def test_bad_json_keeps_old_brief(self):
        scene.save_brief(self.store, SESSION, '旧纸条', covered_to=0, now=time.time() - 1000)
        self.gateway.replies = ['我觉得这段很精彩，但我不想给 JSON']
        for i in range(4):
            self.store.add('user', '消息%d' % i)
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(scene.get_brief(self.store, SESSION)['text'], '旧纸条')

    def test_bad_json_is_retried_once_and_can_succeed(self):
        """第一次吐散文、第二次给 JSON：应当重试一次并成功（实测渠道会偶发抖动）。"""
        self.gateway.replies = ['群聊。\n\nthreads（正在进行中的事）：\n1.', brief_json()]
        for i in range(4):
            self.store.add('user', '消息%d' % i)
        self.assertTrue(self.keeper.consider(SESSION))
        self.assertEqual(len(self.gateway.calls), 2, '应当重试一次')
        self.assertIn('面包坊', scene.get_brief(self.store, SESSION)['text'])

    def test_reasoning_budget_is_large_enough(self):
        """400/1200 都会被思考链吃光，默认必须给到 3200（与记忆提炼同档）。"""
        for i in range(4):
            self.store.add('user', '消息%d' % i)
        self.keeper.consider(SESSION)
        self.assertEqual(self.gateway.calls[0]['max_tokens'], 3200)

    def test_empty_result_advances_cursor_without_brief(self):
        self.gateway.replies = [json.dumps({'scene': '', 'threads': [], 'promises': [],
                                            'open_questions': []})]
        for i in range(4):
            self.store.add('user', '闲聊')
        self.assertFalse(self.keeper.consider(SESSION))
        self.assertEqual(scene.get_brief(self.store, SESSION)['covered_to'], 4,
                         '空结果也要推进游标，否则同一批会反复烧钱')


class PromptInjectionTest(unittest.TestCase):
    """纸条要真的出现在 system 里，而且带「不是指令」。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime
        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')

    def _msg(self):
        import types
        from newGeYi.models import IncomingMessage, SessionKey
        session = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                             session_type='group', host_id='-', target_id='9990001')
        return IncomingMessage(
            session=session, message_id='1', sender_id='10001', sender_name='道君',
            text='我们刚才说到哪了', raw_text='我们刚才说到哪了', is_group=True,
            mentioned_bot=False, event=types.SimpleNamespace(send=lambda *a, **k: None),
            proc=None, arrival_seq=1, sender_is_bot=False)

    def test_fresh_brief_is_in_system(self):
        msg = self._msg()
        scene.save_brief(self.rt.store, msg.session.as_text(), '场景：面包坊', covered_to=1)
        system = self.rt.prompt_builder.build(msg)[0]['content']
        self.assertIn('【正在发生', system)
        self.assertIn('场景：面包坊', system)

    def test_stale_brief_is_absent(self):
        msg = self._msg()
        scene.save_brief(self.rt.store, msg.session.as_text(), '场景：面包坊', covered_to=1,
                         now=time.time() - 10 * 3600)
        system = self.rt.prompt_builder.build(msg)[0]['content']
        self.assertNotIn('【正在发生', system)


class RealStoreRetentionTest(unittest.TestCase):
    """回归：提炼器删掉游标以下的缓冲 → 纸条线程永远看到 0 条（2026-11-10 实测）。

    修法是给 `buffer_trim` 加保留窗口；这里用真 SQLiteStore 钉住"提炼完还留得住"。
    """

    def setUp(self):
        from newGeYi.storage.database import SQLiteStore
        self.tmp = tempfile.mkdtemp(prefix='geyi_scene_retention_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'retention.sqlite3'))
        self.session = 'h:qq:onebot:group:-:999'

    def tearDown(self):
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fill(self, n=5):
        return [self.store.buffer_append(self.session, 'user', '第%d句' % i,
                                         sender_id='1', sender_name='A') for i in range(n)]

    def test_curated_rows_survive_retention_window(self):
        ids = self._fill(5)
        self.store.set_setting('mem.cursor:' + self.session, str(ids[-1]))
        self.store.buffer_trim(self.session, retention_seconds=7200)
        self.assertEqual(self.store.buffer_pending(self.session, 0), 5,
                         '提炼后仍应保留，纸条要读这一段')

    def test_zero_retention_keeps_old_behaviour(self):
        ids = self._fill(5)
        self.store.set_setting('mem.cursor:' + self.session, str(ids[-1]))
        self.store.buffer_trim(self.session, retention_seconds=0)
        self.assertEqual(self.store.buffer_pending(self.session, 0), 0,
                         'retention=0 时应当恢复"提炼完立刻删"')

    def test_rows_older_than_retention_are_dropped(self):
        ids = self._fill(5)
        self.store.set_setting('mem.cursor:' + self.session, str(ids[-1]))
        self.store._conn().execute(
            'UPDATE memory_buffer SET created_at=? WHERE id<=?',
            (int(time.time()) - 10000, ids[2]))
        self.store.buffer_trim(self.session, retention_seconds=7200)
        self.assertEqual(self.store.buffer_pending(self.session, 0), 2, '过期行应当被清掉')


if __name__ == '__main__':
    unittest.main(verbosity=2)
