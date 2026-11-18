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
from newGeYi.memory.worldbook import is_story_mode, set_story_mode  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402


def message(text='', uid='111', target='777'):
    return IncomingMessage(
        session=SessionKey('bot', 'qq', 'onebot', 'group', '-', target),
        message_id='m1', sender_id=uid, sender_name='test', text=text, raw_text=text,
        is_group=True, mentioned_bot=False, event=None, proc=None,
    )


def private_message(text='', uid='111', bot='bot'):
    return IncomingMessage(
        session=SessionKey(bot, 'qq', 'onebot', 'private', '', uid),
        message_id='m2', sender_id=uid, sender_name='test', text=text, raw_text=text,
        is_group=False, mentioned_bot=False, event=None, proc=None,
    )


class EpisodeCommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_episode_commands_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.key = message().session.as_text()
        self.replies = []
        # 群聊里切换共享篇章要群管/骰主；老用例多数直接发剧情指令，统一按管理员跑，
        # 权限本身由 StoryCommandPermissionTest 单独覆盖。
        self.is_master = False
        self.is_admin = True
        self.runtime = types.SimpleNamespace(
            store=self.store, config={},
            permissions=types.SimpleNamespace(is_master=lambda msg: self.is_master,
                                              is_group_admin=lambda msg: self.is_admin),
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
        # 单 QQ 重置只动这个 QQ 的私聊，绝不误伤操作者当前所在群聊（2026-11-17 定稿）
        private_key = private_message(uid='111').session.as_text()
        new_story(self.store, private_key, '私聊篇章')
        set_story_mode(self.store, private_key, '111', True)
        group_story = new_story(self.store, self.key, '群里的篇章')
        set_story_mode(self.store, self.key, '111', True)
        self.router._cmd_story_mode_reset(message(), '111')
        self.assertEqual(current_space(self.store, private_key), 'chat')
        self.assertFalse(is_story_mode(self.store, private_key, ''))
        self.assertEqual(current_space(self.store, self.key), group_story)
        self.assertTrue(is_story_mode(self.store, self.key, ''))

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


GROUP_STORY_DENY = ('群里的剧情篇章是大家共用的，要群管或骰主才能开启、切换或暂停哦～'
                    '想一个人开新篇章的话，可以私聊我试试。')


class StoryCommandPermissionTest(unittest.TestCase):
    """群聊里的共享篇章：只读随便看，切换要群管/骰主；私聊本人自由。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_story_perm_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.replies = []
        self.is_master = False
        self.is_admin = False
        self.runtime = types.SimpleNamespace(
            store=self.store, config={},
            worldbook=types.SimpleNamespace(entries=[]),
            permissions=types.SimpleNamespace(is_master=lambda msg: self.is_master,
                                              is_group_admin=lambda msg: self.is_admin),
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(self.runtime)
        self.group_key = message(target='777').session.as_text()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self, msg):
        self.assertTrue(self.router.try_handle(msg))
        return self.replies[-1]

    def test_plain_member_cannot_switch_group_space(self):
        for cmd in ('新建剧情 随便开', '重开剧情 随便开', '续接剧情', '剧情模式开', '剧情模式关',
                    '开启探索模式', '退出剧情模式'):
            self.assertEqual(self.send(message(cmd)), GROUP_STORY_DENY, cmd)
            self.assertIsNone(self.store.get_setting('memory.space:' + self.group_key), cmd)

    def test_group_admin_and_master_can_switch(self):
        self.is_admin = True
        self.assertIn('已新开', self.send(message('新建剧情 管理员开的')))
        self.assertNotEqual(current_space(self.store, self.group_key), 'chat')
        self.is_admin = False
        self.is_master = True
        self.assertIn('已切回日常', self.send(message('剧情模式关')))
        self.assertEqual(current_space(self.store, self.group_key), 'chat')

    def test_plain_member_can_read_group_story_commands(self):
        for cmd in ('剧情篇章', '剧情模式', '世界书状态'):
            self.assertNotEqual(self.send(message(cmd)), GROUP_STORY_DENY, cmd)
        self.assertIsNone(self.store.get_setting('memory.space:' + self.group_key))

    def test_private_owner_switches_without_permission(self):
        self.assertIn('已新开', self.send(private_message('新建剧情 只有我看')))
        pkey = private_message().session.as_text()
        self.assertNotEqual(current_space(self.store, pkey), 'chat')
        self.assertIn('已切回日常', self.send(private_message('剧情模式关')))
        self.assertEqual(current_space(self.store, pkey), 'chat')
        self.assertIn('已新开', self.send(private_message('重开剧情 再来一遍')))


class StoryIsolationTest(unittest.TestCase):
    """跨会话隔离：一个人开剧情不能污染别的群聊/私聊。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_story_iso_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_private_story_does_not_leak_into_uninitialized_group(self):
        from newGeYi.memory.spaces import snapshot_space
        from newGeYi.memory.worldbook import is_story_mode
        from newGeYi.runtime.app import Runtime

        pkey = private_message(uid='10001').session.as_text()
        gkey = message(target='55555').session.as_text()
        set_story_mode(self.store, pkey, '10001', True)
        new_story(self.store, pkey, '雨夜')
        self.assertNotEqual(current_space(self.store, pkey), 'chat')
        # 用户级遗留键不会再让群聊跟着变剧情
        self.assertFalse(is_story_mode(self.store, gkey, '10001'))
        ensure_space(self.store, gkey, is_story_mode(self.store, gkey, ''))
        self.assertEqual(current_space(self.store, gkey), 'chat')
        self.assertEqual(list_stories(self.store, gkey), [])
        # Runtime 的冻结入口同样只读本会话自己的开关
        runtime = types.SimpleNamespace(store=self.store)
        frozen = Runtime._freeze_message_space(runtime, message('在吗', uid='10001', target='55555'))
        self.assertEqual(frozen.memory_space_id, 'chat')
        self.assertEqual(current_space(self.store, gkey), 'chat')
        self.assertEqual(list_stories(self.store, gkey), [])
        self.assertEqual(snapshot_space(self.store, gkey)['space_id'], 'chat')

    def test_group_story_does_not_leak_into_private(self):
        from newGeYi.memory.worldbook import is_story_mode
        gkey = message(target='55555').session.as_text()
        set_story_mode(self.store, gkey, '20001', True)
        new_story(self.store, gkey, '群里的故事')
        pkey = private_message(uid='20001').session.as_text()
        self.assertEqual(current_space(self.store, pkey), 'chat')
        self.assertFalse(is_story_mode(self.store, pkey, '20001'))
        ensure_space(self.store, pkey, is_story_mode(self.store, pkey, ''))
        self.assertEqual(current_space(self.store, pkey), 'chat')
        self.assertEqual(list_stories(self.store, pkey), [])

    def test_two_private_sessions_are_independent(self):
        a = private_message(uid='10001', bot='bot1').session.as_text()
        b = private_message(uid='10001', bot='bot2').session.as_text()
        set_story_mode(self.store, a, '10001', True)
        new_story(self.store, a, 'bot1 的篇章')
        self.assertNotEqual(current_space(self.store, a), 'chat')
        self.assertEqual(current_space(self.store, b), 'chat')
        self.assertEqual(list_stories(self.store, b), [])

    def test_legacy_user_key_is_ignored_when_session_key_given(self):
        from newGeYi.memory.worldbook import is_story_mode
        gkey = message(target='55555').session.as_text()
        self.store.set_setting('story_mode_user:10001', '1')
        self.assertFalse(is_story_mode(self.store, gkey, '10001'))
        self.assertTrue(is_story_mode(self.store, '', '10001'))
        set_story_mode(self.store, gkey, '10001', True)
        self.assertEqual(self.store.get_setting('story_mode:' + gkey), '1')
        self.assertEqual(self.store.get_setting('story_mode_user:10001'), '1')
        # 正常关闭不得清掉别人的遗留键，只有显式清理（enabled=None）才动用户级键
        set_story_mode(self.store, gkey, '10001', False)
        self.assertEqual(self.store.get_setting('story_mode:' + gkey), '')
        self.assertEqual(self.store.get_setting('story_mode_user:10001'), '1')
        set_story_mode(self.store, '', '10001', None)
        self.assertEqual(self.store.get_setting('story_mode_user:10001'), '')

    def test_group_and_private_switches_do_not_share_keys(self):
        from newGeYi.memory.worldbook import is_story_mode
        gkey = message(target='55555').session.as_text()
        pkey = private_message(uid='10001').session.as_text()
        set_story_mode(self.store, gkey, '10001', True)
        self.assertTrue(is_story_mode(self.store, gkey, ''))
        self.assertFalse(is_story_mode(self.store, pkey, ''))
        set_story_mode(self.store, pkey, '10001', True)
        set_story_mode(self.store, gkey, '10001', False)
        self.assertTrue(is_story_mode(self.store, pkey, ''))
        self.assertFalse(is_story_mode(self.store, gkey, ''))


class ReadOnlyStoryCommandTest(unittest.TestCase):
    """只读命令零副作用：查一眼不能在会话里建出篇章。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_story_readonly_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.replies = []
        self.runtime = types.SimpleNamespace(
            store=self.store, config={},
            worldbook=types.SimpleNamespace(entries=[]),
            permissions=types.SimpleNamespace(is_master=lambda msg: False, is_group_admin=lambda msg: True),
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(self.runtime)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_status_and_episode_commands_do_not_create_space(self):
        fresh = message(target='99999').session.as_text()
        for cmd in ('剧情模式', '探索模式', '共同经历', '我的约定', '世界书状态'):
            self.assertTrue(self.router.try_handle(message(cmd, target='99999')))
            self.assertIsNone(self.store.get_setting('memory.space:' + fresh), cmd)
            self.assertEqual(list_stories(self.store, fresh), [], cmd)

    def test_status_reports_story_space_even_without_flag(self):
        key = message(target='99998').session.as_text()
        new_story(self.store, key, '雨夜')
        self.store.set_setting('story_mode:' + key, '')
        out = self.router._cmd_story_mode_status(message(target='99998'))
        self.assertIn('当前记忆空间：雨夜', out)
        self.assertIn('当前状态：开启', out)


class StoryModeAdminCommandTest(unittest.TestCase):
    """主人专用：剧情模式列表 / 剧情模式重置。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_story_admin_')
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.replies = []
        self.runtime = types.SimpleNamespace(
            store=self.store, config={},
            permissions=types.SimpleNamespace(is_master=lambda msg: True, is_group_admin=lambda msg: True),
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(self.runtime)
        self.group_key = message(target='777').session.as_text()
        self.private_key = private_message(uid='10001').session.as_text()
        self.other_private = private_message(uid='10002').session.as_text()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self, msg):
        self.assertTrue(self.router.try_handle(msg))
        return self.replies[-1]

    def test_list_shows_sessions_instead_of_qq_numbers(self):
        self.assertIn('目前没有会话开启', self.send(message('剧情模式列表')))
        set_story_mode(self.store, self.private_key, '10001', True)
        new_story(self.store, self.private_key, '私聊篇章')
        new_story(self.store, self.group_key, '群聊篇章')
        out = self.send(message('剧情模式列表'))
        self.assertIn('已开启会话（共 2 个）', out)
        self.assertIn(f'私聊 10001 [{self.private_key}]', out)
        self.assertIn(f'群聊 777 [{self.group_key}]', out)

    def test_reset_single_qq_only_touches_that_private_session(self):
        new_story(self.store, self.private_key, '私聊篇章')
        set_story_mode(self.store, self.private_key, '10001', True)
        new_story(self.store, self.other_private, '别人的篇章')
        set_story_mode(self.store, self.other_private, '10002', True)
        # 群里由管理员用「剧情模式开」进入剧情：群开关落在会话级键上
        new_story(self.store, self.group_key, '操作者当前群篇章')
        set_story_mode(self.store, self.group_key, '999', True)
        self.store.set_setting('story_mode_user:10001', '1')
        out = self.send(message('剧情模式重置 10001', target='777'))
        self.assertIn('已重置 QQ 10001 的私聊剧情模式（1 个私聊会话已回到日常模式）', out)
        self.assertEqual(current_space(self.store, self.private_key), 'chat')
        self.assertFalse(is_story_mode(self.store, self.private_key, ''))
        self.assertEqual(self.store.get_setting('story_mode_user:10001'), '')
        # 绝对不误伤：别人的私聊和操作者当前所在群都还在剧情里
        self.assertNotEqual(current_space(self.store, self.other_private), 'chat')
        self.assertTrue(is_story_mode(self.store, self.other_private, ''))
        self.assertTrue(current_space(self.store, self.group_key).startswith('story:'))
        self.assertEqual(self.store.get_setting('story_mode:' + self.group_key), '1')

    def test_reset_all_returns_every_session_to_chat(self):
        new_story(self.store, self.private_key, '私聊篇章')
        new_story(self.store, self.other_private, '别人的篇章')
        new_story(self.store, self.group_key, '群聊篇章')
        # 只有真正开过开关的会话才有 story_mode: 键；重置后应为空串，其余保持没写过
        set_story_mode(self.store, self.private_key, '10001', True)
        self.store.set_setting('story_mode_user:10001', '1')
        self.store.set_setting('story_mode_user:10002', '1')
        out = self.send(message('剧情模式重置 全部'))
        self.assertIn('已重置全部 3 个会话的剧情模式', out)
        self.assertIn('清理了 2 条旧用户开关记录', out)
        for key in (self.private_key, self.other_private, self.group_key):
            self.assertEqual(current_space(self.store, key), 'chat')
            self.assertFalse(is_story_mode(self.store, key), key)
        self.assertEqual(self.store.get_setting('story_mode:' + self.private_key), '')
        # 其余会话本来没有开关记录，重置不应凭空造出空串记录
        self.assertFalse(self.store.get_setting('story_mode:' + self.other_private))
        self.assertFalse(self.store.get_setting('story_mode:' + self.group_key))
        self.assertEqual(self.store.get_setting('story_mode_user:10001'), '')
        self.assertEqual(self.store.get_setting('story_mode_user:10002'), '')


if __name__ == '__main__':
    unittest.main()
