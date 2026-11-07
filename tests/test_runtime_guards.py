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

import itertools
import json
import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_guards_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

# 会话号计数器：模块级，跨测试类唯一（见 _RuntimeCase.setUp 的注释）
_SEQ = itertools.count(1)

from newGeYi.models import IncomingMessage, ModelResult, SessionKey  # noqa: E402

GROUP = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                   session_type='group', host_id='-', target_id='10001')
PRIVATE = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                     session_type='private', host_id='', target_id='2332239652')


def make_msg(session, text='你好呀', seq=1):
    """返回 (msg, sent)：IncomingMessage 是 frozen dataclass，出口记在 sent 里。"""
    sent = []

    def send(*args, **kwargs):
        sent.append(args)

    event = types.SimpleNamespace(send=send)
    msg = IncomingMessage(
        session=session, message_id=str(seq), sender_id='2332239652', sender_name='歌靡',
        text=text, raw_text=text, is_group=session.session_type == 'group',
        mentioned_bot=True, event=event, proc=None, arrival_seq=seq, sender_is_bot=False,
    )
    return msg, sent


class _RuntimeCase(unittest.TestCase):
    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')
        # ⚠️ 每个用例换一个会话号：整个模块共用一个临时数据目录，
        # 复用同一个 session_key 会让上一个用例的 turns 混进来（踩过两次）。
        # 计数器必须是**模块级**的 —— 之前写成 `type(self)._seq`，每个子类各数各的，
        # 于是 RestGuardTest 的第 1 个用例和 VoiceRecordTest 的第 1 个用例撞在
        # 同一个群号上（新增两条静默用例后才暴露出来）。
        suffix = '%06d' % next(_SEQ)
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

    # ----- 2026-11-04：私聊的骚扰退场改无限期，只认骰主「解除静默」 -----
    def test_private_rest_is_indefinite_until_released(self):
        self.rt._notify_owner = lambda *a, **k: True
        session = self.private.as_text()
        msg, sent = make_msg(self.private, '一直缠着她说')
        self.rt._enter_rest(msg, '被同一个人连续纠缠')

        self.assertTrue(self.rt.rest.is_forever(session), '私聊退场不是无限期')
        self.assertEqual(self.rt.rest.remaining(session), -1)
        self.assertTrue(self.rt.is_resting(session))
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.calls, [], '无限期静默期内还是调了模型')
        self.assertEqual(sent, [], '无限期静默期内还是发了消息')

        out = self.rt.commands._cmd_clear_rest(msg)
        self.assertIn('解除', out)
        self.assertFalse(self.rt.is_resting(session), '「解除静默」没真的放她出来')
        self.assertEqual(self.rt.rest.remaining(session), 0)

    def test_group_rest_stays_finite(self):
        self.rt._notify_owner = lambda *a, **k: True
        session = self.group.as_text()
        msg, _sent = make_msg(self.group, '滚')
        self.rt._enter_rest(msg, '被辱骂')
        self.assertFalse(self.rt.rest.is_forever(session), '群聊不该被永久封口')
        self.assertGreater(self.rt.rest.remaining(session), 0)

    def test_forever_ignores_until_expiry(self):
        """无限期不能靠 until 时间戳实现——它必须是状态里的一个标志。"""
        session = self.private.as_text()
        self.rt.store.set_setting(
            'rest:' + session, json.dumps({'until': 1, 'forever': True, 'reason': '骚扰'}))
        self.assertTrue(self.rt.is_resting(session))
        self.assertEqual(self.rt.rest.remaining(session), -1)

    def test_legacy_payload_without_forever_still_works(self):
        session = self.group.as_text()
        self.rt.store.set_setting(
            'rest:' + session, json.dumps({'until': 2 ** 31, 'reason': '旧格式'}))
        self.assertTrue(self.rt.is_resting(session))
        self.assertFalse(self.rt.rest.is_forever(session))
        self.assertGreater(self.rt.rest.remaining(session), 0)

    # ----- 无限期静默必须能"从外面"解除：列出来 + 按号码解除 -----
    def test_rest_list_and_release_by_id(self):
        self.rt._notify_owner = lambda *a, **k: True
        # 陌生人私聊骚扰 → 无限期静默（这个会话只有对方能"在里面发命令"）
        stranger = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                              session_type='private', host_id='', target_id='9333111')
        msg, _sent = make_msg(stranger, '再叫一声')
        self.rt._enter_rest(msg, '被反复骚扰')

        listing = self.rt.commands._cmd_rest_list(msg)
        self.assertIn('9333111', listing)
        self.assertIn('无限期', listing)

        owner_msg, _ = make_msg(self.private, '解除静默 9333111')
        out = self.rt.commands._cmd_clear_rest_any(owner_msg, '9333111')
        self.assertIn('已解除', out)
        self.assertFalse(self.rt.is_resting(stranger.as_text()),
                         '按号码也没法解除 → 陌生人私聊被永久封口')

    def test_release_unknown_id_is_explained(self):
        msg, _ = make_msg(self.private)
        out = self.rt.commands._cmd_clear_rest_any(msg, '1000000099')
        self.assertIn('没找到', out)


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


class PrivatePromptDuplicationTest(_RuntimeCase):
    """2026-11-04 线上反馈：私聊里「一句变两遍」。

    根因：v2.8.10 起私聊的用户消息**先落库**（`_remember_user_turn`，防止早退分支丢话），
    而 PromptBuilder 又从这个表读历史 → 同一句既在历史里、又作为当前消息出现一次。
    """

    def _capture(self):
        captured = []

        def fake_chat(req, kind='chat', session_key=''):
            captured.append(req.messages)
            return ModelResult(ok=True, text='好呀～', model='stub',
                               input_tokens=1, output_tokens=1, latency_ms=1)

        self.rt.gateway.chat = fake_chat
        return captured

    @staticmethod
    def _texts(messages):
        return [m['content'] for m in messages if isinstance(m['content'], str)]

    def test_current_message_appears_exactly_once(self):
        captured = self._capture()
        msg, _sent = make_msg(self.private, '今天好累啊')
        self.rt.process_job(msg, 'passive')
        texts = self._texts(captured[0])
        hits = [t for t in texts if '今天好累啊' in t]
        self.assertEqual(len(hits), 1, '同一句在 prompt 里出现了 %d 次: %r' % (len(hits), texts))

    def test_history_keeps_earlier_turns(self):
        captured = self._capture()
        self.rt.process_job(make_msg(self.private, '今天好累啊', seq=1)[0], 'passive')
        captured.clear()
        self.rt.process_job(make_msg(self.private, '你还在吗', seq=2)[0], 'passive')
        texts = self._texts(captured[0])
        self.assertEqual(sum(1 for t in texts if '今天好累啊' in t), 1, texts)
        self.assertEqual(sum(1 for t in texts if '你还在吗' in t), 1, texts)
        self.assertIn('好呀～', texts, '上一轮她的回复不见了')

    def test_user_repeating_the_same_line_still_shows_twice(self):
        captured = self._capture()
        self.rt.process_job(make_msg(self.private, '在吗', seq=1)[0], 'passive')
        captured.clear()
        self.rt.process_job(make_msg(self.private, '在吗', seq=2)[0], 'passive')
        texts = self._texts(captured[0])
        self.assertEqual(sum(1 for t in texts if '在吗' in t), 2, texts)


class PrivateMemoryBufferTest(_RuntimeCase):
    """私聊与群聊均独立缓冲和提炼。仅总开关 memory_enabled 关掉时停止缓冲。"""

    def test_private_buffers_assistant_reply(self):
        """私聊正常缓冲。"""
        session = self.private.as_text()
        msg, _sent = make_msg(self.private, '私聊里的内容')
        self.rt.process_job(msg, 'passive')
        rows = self.rt.store.buffer_fetch(session, 0, limit=10)
        self.assertEqual([r['role'] for r in rows], ['assistant'])

    def test_private_off_memory_enabled_writes_nothing(self):
        session = self.private.as_text()
        self.rt.config['memory_enabled'] = False
        msg, _sent = make_msg(self.private, '总开关关掉')
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.rt.store.buffer_pending(session), 0)

    def test_group_off_writes_nothing(self):
        session = self.group.as_text()
        self.rt.config['memory_enabled'] = False
        msg, _sent = make_msg(self.group, '群里的话')
        self.rt.process_job(msg, 'passive')
        self.assertEqual(self.rt.store.buffer_pending(session), 0)


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
    def test_private_is_tracked_by_default(self):
        msg, _sent = make_msg(self.private, '私聊内容')
        self.assertTrue(self.rt._memory_tracking_on(msg))
        self.rt.config['memory_enabled'] = False
        self.assertFalse(self.rt._memory_tracking_on(msg))

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
        fact_id = self.store.add_user_fact('g1', '2332239652', '喜欢猫')
        self.curator._store_results('g1', {'expired_fact_ids': [fact_id]},
                                    valid_ids={'2332239652'}, last_id=1,
                                    allowed_fact_ids=set())
        self.assertEqual(len(self.store.list_user_facts('g1', '2332239652', limit=5)), 1,
                         '模型幻觉的 ID 把真实事实标成失效了')

        self.curator._store_results('g1', {'expired_fact_ids': [fact_id]},
                                    valid_ids={'2332239652'}, last_id=1,
                                    allowed_fact_ids={fact_id})
        self.assertEqual(self.store.list_user_facts('g1', '2332239652', limit=5), [])

    def test_private_session_curates_independently(self):
        """私聊会话独立触发提炼，不受互通开关状态阻断。"""
        key = 'h:qq:onebot:private:-:2332239652'
        self.store.buffer_append(key, 'user', '私聊的内容', sender_id='2332239652')
        self.curator.config['memory_private_enabled'] = False
        self.assertTrue(self.curator.should_run(key))

    def test_memory_off_stops_curating(self):
        key = 'h|qq|onebot|group|-|10001'
        self.store.buffer_append(key, 'user', '群聊的内容', sender_id='1')
        self.assertTrue(self.curator.should_run(key))
        self.curator.config['memory_enabled'] = False
        self.assertFalse(self.curator.should_run(key))


if __name__ == '__main__':
    unittest.main(verbosity=2)
