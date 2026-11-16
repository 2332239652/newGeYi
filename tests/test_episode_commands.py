# -*- encoding: utf-8 -*-
"""共同记忆本人管理与完整会话剧情空间：纯本地临时SQLite，无平台/模型调用。"""

from __future__ import annotations

import os
import tempfile
import types
import unittest
from unittest.mock import patch

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_episode_command_data_'))

from newGeYi.memory.spaces import (  # noqa: E402
    current_space, ensure_space, list_stories, new_story, pause_story, resume_story,
    snapshot_space, space_boundary,
)
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.commands import CommandRouter  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402


def message(text='', uid='111', target='777'):
    return IncomingMessage(
        session=SessionKey('bot', 'qq', 'onebot', 'group', '-', target),
        message_id='m1', sender_id=uid, sender_name='test', text=text, raw_text=text,
        is_group=True, mentioned_bot=False, event=None, proc=None,
    )


class EpisodeCommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_episode_commands_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.key = message().session.as_text()
        self.replies = []
        self.runtime = types.SimpleNamespace(
            store=self.store, config={},
            permissions=types.SimpleNamespace(is_master=lambda msg: False, is_group_admin=lambda msg: False),
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(self.runtime)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, uid='111', space='chat', key=None, text='一起烤面包'):
        return self.store._conn().execute(
            'INSERT INTO memory_events(session_key,space_id,user_id,summary,evidence_type,created_at,updated_at) '
            'VALUES(?,?,?,?,?,1,1)',
            (key or self.key, space, uid, text, 'co_enacted'),
        ).lastrowid

    def commitment(self, uid='111', space='chat', status='open'):
        return self.store._conn().execute(
            'INSERT INTO memory_commitments(session_key,space_id,user_id,recipient_id,content,status,created_at,'
            'updated_at) VALUES(?,?,?,?,?,?,1,1)',
            (self.key, space, uid, uid, '明天再做一炉', status),
        ).lastrowid

    def run_command(self, text, uid='111', target='777'):
        self.assertTrue(self.router.try_handle(message(text, uid, target)), text)
        return self.replies[-1]

    def test_public_owner_only_reads_current_user_session_and_space(self):
        eid = self.event(text='本人经历')
        self.event('222', text='他人秘密')
        self.event(space=new_story(self.store, self.key), text='剧情秘密')
        self.event(key=message(target='888').session.as_text(), text='另群秘密')
        pause_story(self.store, self.key)
        result = self.run_command('共同经历')
        self.assertIn(f'{eid}：本人经历', result)
        for forbidden in ('他人秘密', '剧情秘密', '另群秘密'):
            self.assertNotIn(forbidden, result)

    def test_nonowner_can_correct_delete_own_item(self):
        eid = self.event()
        self.assertIn('已纠正', self.run_command(f'纠正经历 {eid} 上周一起烤面包'))
        self.assertIn('上周一起', self.run_command('共同经历'))
        self.assertIn('已删除', self.run_command(f'删除经历 {eid}'))
        self.assertIn('暂无记录', self.run_command('共同经历'))

    def test_other_owner_unknown_and_other_space_identical_feedback(self):
        other = self.event('222')
        hidden = self.event(space='story:00000000-0000-0000-0000-000000000001')
        replies = [self.run_command(f'删除经历 {eid}') for eid in (other, hidden, 99999)]
        self.assertEqual(len(set(replies)), 1)
        self.assertEqual(self.store._conn().execute('SELECT COUNT(*) FROM memory_events').fetchone()[0], 2)

    def test_complete_cancel_and_terminal_state_cannot_reopen(self):
        first, second = self.commitment(), self.commitment()
        self.assertIn('已完成', self.run_command(f'完成约定 {first}'))
        self.assertIn('已取消', self.run_command(f'取消约定 {second}'))
        self.assertIn('状态已改变', self.run_command(f'取消约定 {first}'))
        rows = self.store._conn().execute('SELECT status FROM memory_commitments ORDER BY id').fetchall()
        self.assertEqual(rows, [('fulfilled',), ('cancelled',)])

    def test_text_cq_op_and_encoded_brackets_are_escaped(self):
        self.event(text='[CQ:at,qq=all] [OP:image,file=a] &#91;CQ:poke&#93;')
        output = self.run_command('共同经历')
        self.assertNotIn('[CQ:', output)
        self.assertNotIn('[OP:', output)
        self.assertIn('&amp;#91;', output)
        output = self.run_command('新建剧情 [OP:at,id=all]')
        self.assertNotIn('[OP:', output)
        self.assertIn('&#91;', output)

    def test_bad_arguments_do_not_call_management_or_fallthrough(self):
        for cmd in ('删除经历', '删除经历 -1', '删除经历 1 extra', '纠正约定 1', '完成约定 abc',
                    '纠正经历 1 ' + '字' * 401):
            self.assertIn('格式', self.run_command(cmd))
        self.assertFalse(self.router.try_handle(message('普通聊天')))

    def test_story_command_pause_resume_and_restart(self):
        self.run_command('新建剧情 厨房')
        first = current_space(self.store, self.key)
        self.run_command('剧情模式关')
        self.assertEqual(current_space(self.store, self.key), 'chat')
        self.run_command('续接剧情')
        self.assertEqual(current_space(self.store, self.key), first)
        self.run_command('重开剧情 厨房')
        self.assertNotEqual(current_space(self.store, self.key), first)
        self.assertIn('1. 厨房', self.run_command('剧情篇章'))
        self.run_command('续接剧情 1')
        self.assertEqual(current_space(self.store, self.key), first)
        self.run_command('剧情模式关')
        self.run_command('续接剧情')
        self.assertEqual(current_space(self.store, self.key), first)

    def test_admin_reset_returns_same_session_to_chat(self):
        new_story(self.store, self.key)
        self.router._cmd_story_mode_reset(message(), '111')
        self.assertEqual(current_space(self.store, self.key), 'chat')

    def test_ensure_migrates_once_and_session_scope(self):
        first = ensure_space(self.store, self.key, True)
        snap = snapshot_space(self.store, self.key)
        self.assertTrue(first.startswith('story:'))
        self.assertEqual(ensure_space(self.store, self.key, True), first)
        self.assertEqual(snapshot_space(self.store, self.key), snap)
        pause_story(self.store, self.key)
        self.assertEqual(ensure_space(self.store, self.key, True), 'chat')
        second = ensure_space(self.store, message(target='888').session.as_text(), True)
        self.assertNotEqual(first, second)

    def test_switch_invalidates_scene_and_preserves_raw_history(self):
        self.store.add_turn(self.key, 'user', '保留别人消息', sender_id='222')
        self.store.buffer_append(self.key, 'user', '保留原始消息', sender_id='222', sender_name='他人')
        self.store.set_setting('scene:' + self.key, 'old')
        self.store.set_setting('scene.fail_until:' + self.key, '999')
        story = new_story(self.store, self.key)
        self.assertIsNone(self.store.get_setting('scene:' + self.key))
        self.assertIsNone(self.store.get_setting('scene.fail_until:' + self.key))
        self.assertEqual(space_boundary(self.store, self.key, story)['source_seq'], 1)
        self.assertGreater(space_boundary(self.store, self.key, story)['buffer_id'], 0)
        self.assertEqual(len(self.store.recent_turns(self.key)), 1)
        self.assertEqual(len(self.store.buffer_fetch(self.key, 0)), 1)

    def test_switch_rolls_back_all_settings_on_failure(self):
        ensure_space(self.store, self.key, False)
        before = snapshot_space(self.store, self.key)
        self.store.set_setting('scene:' + self.key, 'survive')
        original = self.store.set_setting

        def fail_generation(key, value):
            if key.startswith('scene.generation:'):
                raise RuntimeError('injected disk failure')
            original(key, value)

        with patch.object(self.store, 'set_setting', side_effect=fail_generation):
            with self.assertRaises(RuntimeError):
                new_story(self.store, self.key, 'rollback')
        self.assertEqual(snapshot_space(self.store, self.key), before)
        self.assertEqual(list_stories(self.store, self.key), [])
        self.assertEqual(self.store.get_setting('scene:' + self.key), 'survive')

    def test_duplicate_titles_select_by_number_and_persist_across_restart(self):
        first = new_story(self.store, self.key, '同名')
        new_story(self.store, self.key, '同名')
        with self.assertRaises(ValueError):
            resume_story(self.store, self.key, '同名')
        resume_story(self.store, self.key, '1')
        self.store.close()
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.assertEqual(current_space(self.store, self.key), first)
        self.assertEqual(len(list_stories(self.store, self.key)), 2)


if __name__ == '__main__':
    unittest.main()
