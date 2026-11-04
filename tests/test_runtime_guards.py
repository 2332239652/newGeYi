# -*- encoding: utf-8 -*-
"""运行时护栏（离线、假模型、临时数据目录）。

2026-11-03 外部审查发现的「真实 bug」，每一条都在这里钉住：

- 主动/被动回复在**静默期**照发（router 只拦新消息，队列里排着的照样出去）；
- 私聊的用户消息在任一早退分支（模型报错/空内容/静默/渲染被拦）都不入历史，
  而私聊上下文只来自 turns → 她"完全不记得你说过什么"；
- 语音没真发出去（冷却/熔断）也把 `[VOICE:…]` 写进历史 → 她记得自己发过语音；
- 私聊内容默认进了记忆缓冲（隐私），关掉私聊也不停；
- `@` 判定用前缀匹配（`[OP:at,id=123456789` 命中 `...=1234567890]`），且 `@全体` 算点名她。
"""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_guards_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.models import IncomingMessage, ModelResult, SessionKey  # noqa: E402

GROUP = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                   session_type='group', host_id='-', target_id='10001')
PRIVATE = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                     session_type='private', host_id='', target_id='1000000001')


def make_msg(session, text='你好呀', seq=1):
    """返回 (msg, sent)：IncomingMessage 是 frozen dataclass，出口记在 sent 里。"""
    sent = []

    def send(*args, **kwargs):
        sent.append(args)

    event = types.SimpleNamespace(send=send)
    msg = IncomingMessage(
        session=session, message_id=str(seq), sender_id='1000000001', sender_name='歌靡',
        text=text, raw_text=text, is_group=session.session_type == 'group',
        mentioned_bot=True, event=event, proc=None, arrival_seq=seq, sender_is_bot=False,
    )
    return msg, sent


class _RuntimeCase(unittest.TestCase):
    _seq = 0

    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')
        # ⚠️ 每个用例换一个会话号：整个模块共用一个临时数据目录，
        # 复用同一个 session_key 会让上一个用例的 turns 混进来（踩过一次）。
        type(self)._seq += 1
        suffix = '%06d' % type(self)._seq
        self.group = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                                session_type='group', host_id='-', target_id='7' + suffix)
        self.private = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                                  session_type='private', host_id='', target_id='9' + suffix)
        self.calls = []

        def fake_chat(req, kind='chat', session_key=''):
            self.calls.append(kind)
            return ModelResult(ok=True, text='（歪头）好呀～', model='stub',
                               input_tokens=10, output_tokens=5, latency_ms=3)

        self.rt.gateway.chat = fake_chat


class MentionTest(unittest.TestCase):
    def setUp(self):
        from newGeYi.runtime.app import Runtime
        self.rt = Runtime()
        self.event = types.SimpleNamespace(
            base_info=types.SimpleNamespace(get=lambda k, d=None: '123456789'))

    def test_exact_id_only(self):
        self.assertFalse(self.rt._mentioned(self.event, '[OP:at,id=1234567890] 在吗'),
                         '前缀匹配把「@别人」当成了「@她」')
        self.assertTrue(self.rt._mentioned(self.event, '[OP:at,id=123456789] 在吗'))
        self.assertTrue(self.rt._mentioned(self.event, '[CQ:at,qq=123456789] 在吗'))

    def test_at_all_is_not_a_mention_by_default(self):
        self.assertFalse(self.rt._mentioned(self.event, '[OP:at,id=all] 公告'))
        self.rt.config['mention_on_at_all'] = True
        self.assertTrue(self.rt._mentioned(self.event, '[OP:at,id=all] 公告'))


class RestGuardTest(_RuntimeCase):
    def test_queued_job_is_dropped_during_rest(self):
        session = self.group.as_text()
        self.rt.rest.start(session, reason='测试静默')
        msg, sent = make_msg(self.group)
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.calls, [], '静默期内还是调了模型')
        self.assertEqual(sent, [], '静默期内还是发了消息')


class PrivateHistoryTest(_RuntimeCase):
    def test_user_turn_kept_when_llm_fails(self):
        session = self.private.as_text()
        self.rt.gateway.chat = lambda *a, **k: ModelResult(ok=False, error_code='connection_error')
        msg, _sent = make_msg(self.private, '我今天很累')
        self.rt.process_job(msg, 'passive')
        turns = self.rt.store.recent_turns(session, limit=10)
        self.assertEqual([t['role'] for t in turns], ['user'])
        self.assertIn('我今天很累', turns[0]['content'])

    def test_user_turn_not_duplicated_on_success(self):
        session = self.private.as_text()
        msg, _sent = make_msg(self.private, '在吗')
        self.rt.process_job(msg, 'passive')
        turns = self.rt.store.recent_turns(session, limit=10)
        self.assertEqual([t['role'] for t in turns], ['user', 'assistant'])

    def test_group_still_writes_once(self):
        session = self.group.as_text()
        msg, _sent = make_msg(self.group, '在吗')
        self.rt.process_job(msg, 'passive')
        turns = self.rt.store.recent_turns(session, limit=10)
        self.assertEqual([t['role'] for t in turns], ['user', 'assistant'])


class VoiceRecordTest(_RuntimeCase):
    def _run_with_voice(self, queued: bool) -> str:
        rendered = types.SimpleNamespace(
            text='好的呀', rest_reason='', voice_payload=('happy', 'よし'),
            emote='', mention_current_user=False)
        self.rt.renderer.render = lambda text, msg: rendered
        self.rt.voice_manager.dispatch_voice = lambda *a, **k: queued
        session = self.group.as_text()
        msg, _sent = make_msg(self.group, '说句话嘛')
        self.rt.process_job(msg, 'passive')
        turns = self.rt.store.recent_turns(session, limit=10)
        assistant = [t['content'] for t in turns if t['role'] == 'assistant']
        self.assertEqual(len(assistant), 1, '助手轮数不对：%r' % [t['role'] for t in turns])
        return assistant[0]

    def test_no_voice_fact_when_not_sent(self):
        content = self._run_with_voice(False)
        self.assertNotIn('[VOICE:', content, '语音没发出去却写进了历史')

    def test_voice_fact_when_really_queued(self):
        self.assertIn('[VOICE:', self._run_with_voice(True))


class MemoryTrackingTest(_RuntimeCase):
    def test_private_is_not_tracked_by_default(self):
        msg, _sent = make_msg(self.private, '私聊内容')
        self.assertFalse(self.rt._memory_tracking_on(msg))
        self.rt.config['memory_private_enabled'] = True
        self.assertTrue(self.rt._memory_tracking_on(msg))

    def test_group_is_tracked_unless_memory_is_off(self):
        msg, _sent = make_msg(self.group, '群聊内容')
        self.assertTrue(self.rt._memory_tracking_on(msg))
        self.rt.config['memory_enabled'] = False
        self.assertFalse(self.rt._memory_tracking_on(msg))


class CommandToggleTest(_RuntimeCase):
    """「命令回 ✅、行为却没变」是这次审查最刺眼的一条，这里钉住它真的生效。"""

    def test_toggle_reaches_components_and_disk(self):
        from newGeYi import config as C
        self.rt.commands._cmd_global_active(None, False)
        self.assertFalse(self.rt.attention.config['active_enabled'],
                         '组件还抱着旧字典（命令回 ✅、行为不变）')
        on_disk = json.loads(C.get_config_path().read_text(encoding='utf-8'))
        self.assertFalse(on_disk['active_enabled'], '没有落盘')

    def test_toggle_refuses_and_rolls_back_when_config_is_broken(self):
        from newGeYi import config as C
        path = C.get_config_path()
        original = path.read_text(encoding='utf-8')
        before = self.rt.config['active_enabled']
        try:
            path.write_text('{oops', encoding='utf-8')
            with self.assertRaises(C.ConfigWriteBlocked):
                self.rt.commands._cmd_global_active(None, not before)
            self.assertEqual(self.rt.config['active_enabled'], before,
                             '写盘失败后内存没回滚：状态和磁盘不一致了')
            self.assertEqual(path.read_text(encoding='utf-8'), '{oops',
                             '看不懂的配置文件被覆盖了')
        finally:
            path.write_text(original, encoding='utf-8')


class CuratorGuardTest(unittest.TestCase):
    def setUp(self):
        from newGeYi.memory.curator import MemoryCurator
        from newGeYi.storage.database import SQLiteStore

        self.store = SQLiteStore(os.path.join(tempfile.mkdtemp(prefix='geyi_cur_'), 'c.sqlite3'))
        self.curator = MemoryCurator(
            {'memory_enabled': True, 'memory_curator_enabled': True,
             'memory_curate_messages': 1, 'memory_curate_min_interval': 0},
            self.store, gateway=None)

    def test_extract_json_distinguishes_empty_from_failure(self):
        from newGeYi.memory.curator import extract_json

        self.assertEqual(extract_json('{}'), {})
        self.assertIsNone(extract_json('好的，我整理完了'))
        self.assertIsNone(extract_json(''))
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {'a': 1})

    def test_expired_ids_outside_known_set_are_ignored(self):
        fact_id = self.store.add_user_fact('g1', '1000000001', '喜欢猫')
        self.curator._store_results('g1', {'expired_fact_ids': [fact_id]},
                                    valid_ids={'1000000001'}, last_id=1,
                                    allowed_fact_ids=set())
        self.assertEqual(len(self.store.list_user_facts('g1', '1000000001', limit=5)), 1,
                         '模型幻觉的 ID 把真实事实标成失效了')

        self.curator._store_results('g1', {'expired_fact_ids': [fact_id]},
                                    valid_ids={'1000000001'}, last_id=1,
                                    allowed_fact_ids={fact_id})
        self.assertEqual(self.store.list_user_facts('g1', '1000000001', limit=5), [])

    def test_private_session_does_not_curate_by_default(self):
        key = 'h|qq|onebot|private||1000000001'
        self.store.buffer_append(key, 'user', '私聊的内容', sender_id='1000000001')
        self.assertFalse(self.curator.should_run(key))
        # 第一次调用会把不该留的缓冲整段丢掉（游标推进），所以要看「之后再来的私聊」
        self.store.buffer_append(key, 'user', '又一句私聊', sender_id='1000000001')
        self.curator.config['memory_private_enabled'] = True
        self.assertTrue(self.curator.should_run(key))

    def test_memory_off_stops_curating(self):
        key = 'h|qq|onebot|group|-|10001'
        self.store.buffer_append(key, 'user', '群聊的内容', sender_id='1')
        self.assertTrue(self.curator.should_run(key))
        self.curator.config['memory_enabled'] = False
        self.assertFalse(self.curator.should_run(key))


if __name__ == '__main__':
    unittest.main(verbosity=2)
