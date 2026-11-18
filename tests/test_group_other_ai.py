# -*- encoding: utf-8 -*-
"""群聊「其他 AI」防互触发回归：名单读写、入口过滤、指令权限、渲染断词。

全部离线：临时 SQLite + 假 event，不碰平台、不碰模型、不联网。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import types
import unittest

# 与其它 runtime 测试一样：必须在 import newGeYi 之前把状态目录指到临时目录
os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_other_ai_data_'))

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.presentation.renderer import SEND_AT, Renderer  # noqa: E402
from newGeYi.routing import group_other_ai  # noqa: E402
from newGeYi.routing.commands import CommandRouter  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402

GROUP_KEY = 'bot:qq:onebot:group:-:7770001'
OTHER_GROUP_KEY = 'bot:qq:onebot:group:-:7770002'
PRIVATE_KEY = 'bot:qq:onebot:private:-:111'


class TokenStore:
    """最小 store：只验证 helper 自身的读写与 fail-open，不落盘。"""

    def __init__(self):
        self.data = {}
        self.fail = False

    def get_setting(self, key, default=None):
        if self.fail:
            raise RuntimeError('db down')
        return self.data.get(key, default)

    def set_setting(self, key, value):
        if self.fail:
            raise RuntimeError('db down')
        self.data[key] = value


def make_msg(text='', *, session_key=GROUP_KEY, uid='111', name='群友', is_group=True):
    parts = session_key.split(':')
    session = SessionKey(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
    return IncomingMessage(session=session, message_id='m1', sender_id=uid, sender_name=name,
                           text=text, raw_text=text, is_group=is_group,
                           mentioned_bot=False, event=None, proc=None)


class RegistryTest(unittest.TestCase):
    """名单存取：session 隔离、QQ 精确匹配、upsert/删除/清空、脏数据 fail-open。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_other_ai_registry_',
                                               ignore_cleanup_errors=True)
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(self.store.close)

    def test_add_load_and_persist(self):
        self.assertEqual(group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星'), 'added')
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY),
                         [{'qq': '123456789', 'nickname': '小星'}])
        # 换一个连接（模拟重载）也还在：确实是落在 settings 表里
        again = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(again.close)
        self.assertEqual(group_other_ai.load_entries(again, GROUP_KEY),
                         [{'qq': '123456789', 'nickname': '小星'}])
        # 只存 QQ + 昵称，别的字段不进库
        raw = again.get_setting(group_other_ai.registry_key(GROUP_KEY))
        self.assertEqual(raw, '[{"qq": "123456789", "nickname": "小星"}]')

    def test_session_isolation(self):
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        self.assertEqual(group_other_ai.load_entries(self.store, OTHER_GROUP_KEY), [])
        self.assertEqual(group_other_ai.load_entries(self.store, PRIVATE_KEY), [])
        self.assertFalse(group_other_ai.is_registered(self.store, OTHER_GROUP_KEY, '123456789'))
        self.assertFalse(group_other_ai.is_registered(self.store, PRIVATE_KEY, '123456789'))

    def test_qq_exact_match(self):
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        self.assertTrue(group_other_ai.is_registered(self.store, GROUP_KEY, '123456789'))
        for near in ('12345678', '1234567890', '0123456789', '123456789000'):
            self.assertFalse(group_other_ai.is_registered(self.store, GROUP_KEY, near), near)

    def test_upsert_by_qq(self):
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        self.assertEqual(group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星星'), 'updated')
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY),
                         [{'qq': '123456789', 'nickname': '小星星'}])

    def test_delete_and_clear(self):
        self.assertEqual(group_other_ai.remove_entry(self.store, GROUP_KEY, '123456789'), 'missing')
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        group_other_ai.add_entry(self.store, GROUP_KEY, '987654321', '阿蓝')
        self.assertEqual(group_other_ai.remove_entry(self.store, GROUP_KEY, '123456789'), 'removed')
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY),
                         [{'qq': '987654321', 'nickname': '阿蓝'}])
        self.assertEqual(group_other_ai.clear_entries(self.store, GROUP_KEY), 1)
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY), [])
        self.assertEqual(group_other_ai.clear_entries(self.store, GROUP_KEY), 0)

    def test_dirty_json_fails_open(self):
        key = group_other_ai.registry_key(GROUP_KEY)
        for broken in ('{not json', '[1, 2', '"字符串"', '{"qq": "1"}', ''):
            self.store.set_setting(key, broken)
            self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY), [], broken)
            self.assertFalse(group_other_ai.is_registered(self.store, GROUP_KEY, '123456789'), broken)
            # 写坏数据之后要能自愈：新登记直接覆盖成合法 JSON
            self.assertEqual(group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星'), 'added')
            self.assertTrue(group_other_ai.is_registered(self.store, GROUP_KEY, '123456789'))

    def test_storage_error_fails_open(self):
        store = TokenStore()
        store.fail = True
        self.assertEqual(group_other_ai.load_entries(store, GROUP_KEY), [])
        self.assertFalse(group_other_ai.is_registered(store, GROUP_KEY, '123456789'))
        self.assertIsNone(group_other_ai.add_entry(store, GROUP_KEY, '123456789', '小星'))

    def test_bad_entries_are_dropped(self):
        self.store.set_setting(group_other_ai.registry_key(GROUP_KEY), (
            '[{"qq": "123456789", "nickname": "小星"},'
            ' {"qq": "1234", "nickname": "短号"},'
            ' {"qq": "987654321", "nickname": "单"},'
            ' {"qq": "987654322", "nickname": "带\\n换行"},'
            ' {"qq": "123456789", "nickname": "重复号"},'
            ' "不是字典"]'
        ))
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY),
                         [{'qq': '123456789', 'nickname': '小星'}])

    def test_normalizers(self):
        self.assertEqual(group_other_ai.normalize_qq(' 123456789 '), '123456789')
        for bad in ('', '1234', '1234567890123', '12345abc', '１２３４５６７８９', '-1'):
            self.assertEqual(group_other_ai.normalize_qq(bad), '', bad)
        self.assertEqual(group_other_ai.normalize_nickname('  小星  '), '小星')
        for bad in ('', '星', 'x' * 25, '小\n星', '小\t星', '小\u200b星'):
            self.assertEqual(group_other_ai.normalize_nickname(bad), '', repr(bad))


class CommandsTest(unittest.TestCase):
    """管理指令：权限、私聊指路、增删列表清空、参数校验、空名单提示。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_other_ai_commands_',
                                               ignore_cleanup_errors=True)
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(self.store.close)
        self.replies = []
        self.is_master = True
        self.is_admin = True
        runtime = types.SimpleNamespace(
            store=self.store,
            config={},
            permissions=types.SimpleNamespace(
                is_master=lambda msg: self.is_master,
                is_group_admin=lambda msg: self.is_admin,
            ),
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(runtime)

    def run_text(self, text, *, is_group=True):
        msg = make_msg(text, session_key=GROUP_KEY if is_group else PRIVATE_KEY,
                       is_group=is_group)
        return self.router.try_handle(msg)

    def last(self):
        return self.replies[-1] if self.replies else ''

    def test_admin_menu_lists_command(self):
        menu = self.router._cmd_admin_menu(make_msg())
        self.assertIn('[本群] ai过滤 高/低/关', menu)
        self.assertIn('[本群] 本群其他AI 添加 <QQ> <昵称> | 删除 <QQ> | 列表 | 清空', menu)

    def test_ordinary_member_denied(self):
        self.is_master = False
        self.is_admin = False
        for text in ('本群其他AI 添加 123456789 小星', '本群其他AI 列表',
                     '本群其他AI 删除 123456789', '本群其他AI 清空', '本群其他AI'):
            self.assertTrue(self.run_text(text), text)
            self.assertIn('只有骰主或主人', self.last(), text)
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY), [])

    def test_group_admin_can_add_and_list(self):
        self.is_master = False
        self.is_admin = True
        self.assertTrue(self.run_text('本群其他AI 添加 123456789 小星'))
        self.assertIn('已登记其他AI：小\u200b星（123456789）', self.last())
        self.assertTrue(self.run_text('本群其他AI 列表'))
        self.assertIn('小\u200b星（123456789）', self.last())
        # 同一个 QQ 再添加 = 改昵称，不新增
        self.assertTrue(self.run_text('本群其他AI 添加 123456789 小星星'))
        self.assertIn('已更新其他AI昵称：小\u200b星\u200b星（123456789）', self.last())
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY),
                         [{'qq': '123456789', 'nickname': '小星星'}])

    def test_receipt_masks_nicknames_that_resemble_emote_markers(self):
        nickname = '【EMOTE:小星】'
        self.assertTrue(self.run_text(f'本群其他AI 添加 123456789 {nickname}'))
        self.assertNotIn(nickname, self.last())
        self.assertIn(group_other_ai.ZWSP.join(nickname), self.last())
        self.assertTrue(self.run_text('本群其他AI 列表'))
        self.assertIn(group_other_ai.ZWSP.join(nickname), self.last())

    def test_low_mode_receipts_share_nickname_cooldown(self):
        self.assertTrue(self.run_text('ai过滤 低'))
        self.assertTrue(self.run_text('本群其他AI 添加 123456789 小星'))
        self.assertIn('小星（123456789）', self.last())
        self.assertTrue(self.run_text('本群其他AI 列表'))
        self.assertIn('小\u200b星（123456789）', self.last())
        self.assertTrue(self.run_text('本群其他AI 添加 123456789 小星'))
        self.assertIn('已更新其他AI昵称：小\u200b星（123456789）', self.last())

    def test_off_mode_receipts_escape_nicknames_without_masking(self):
        self.assertTrue(self.run_text('ai过滤 关'))
        self.assertTrue(self.run_text('本群其他AI 添加 123456789 小星'))
        self.assertIn('小星（123456789）', self.last())
        nickname = '[CQ:at,qq=all]'
        self.assertTrue(self.run_text(f'本群其他AI 添加 123456789 {nickname}'))
        self.assertIn('已更新其他AI昵称：&#91;CQ:at,qq=all&#93;（123456789）', self.last())
        self.assertTrue(self.run_text('本群其他AI 列表'))
        self.assertIn('&#91;CQ:at,qq=all&#93;（123456789）', self.last())
        self.assertNotIn(nickname, self.last())

    def test_group_admin_delete_and_clear(self):
        self.is_master = False
        self.is_admin = True
        self.run_text('本群其他AI 添加 123456789 小星')
        self.assertTrue(self.run_text('本群其他AI 删除 111111111'))
        self.assertIn('未登记 111111111', self.last())
        self.assertTrue(self.run_text('本群其他AI 删除 123456789'))
        self.assertIn('已删除', self.last())
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY), [])
        self.run_text('本群其他AI 添加 123456789 小星')
        self.run_text('本群其他AI 添加 987654321 阿蓝')
        self.assertTrue(self.run_text('本群其他AI 清空'))
        self.assertIn('已清空', self.last())
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY), [])
        self.assertTrue(self.run_text('本群其他AI 清空'))
        self.assertIn('本来就是空的', self.last())

    def test_empty_list_hint(self):
        self.run_text('本群其他AI 列表')
        self.assertIn('名单是空的', self.last())
        self.run_text('本群其他AI')
        self.assertIn('名单是空的', self.last())

    def test_private_chat_gets_group_hint(self):
        for master in (True, False):
            self.is_master = master
            self.is_admin = False
            self.replies.clear()
            for text in ('本群其他AI 添加 123456789 小星', '本群其他AI 列表',
                         '本群其他AI 删除 123456789', '本群其他AI 清空', '本群其他AI'):
                self.assertTrue(self.run_text(text, is_group=False), text)
                self.assertIn('请在群聊中使用', self.last(), text)
        self.assertEqual(group_other_ai.load_entries(self.store, PRIVATE_KEY), [])

    def test_bad_qq_and_nickname_rejected(self):
        for text in ('本群其他AI 添加 1234 小星',
                     '本群其他AI 添加 1234567890123 小星',
                     '本群其他AI 添加 12345abc 小星'):
            self.assertTrue(self.run_text(text), text)
            self.assertIn('位数字', self.last(), text)
        self.assertTrue(self.run_text('本群其他AI 添加 小星'))
        self.assertIn('格式：', self.last())
        self.assertTrue(self.run_text('本群其他AI 删除 1234'))
        self.assertIn('格式：', self.last())
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY), [])
        for nickname in ('星', 'x' * 25, '小\n星'):
            self.run_text(f'本群其他AI 添加 123456789 {nickname}')
            self.assertIn('昵称要', self.last(), nickname)
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY), [])


class EntryFilterTest(unittest.TestCase):
    """入口过滤：只拦「本群登记的 QQ 的群消息」，而且必须早于命令/上下文/记忆。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime
        self.rt = Runtime()
        self.seen = []
        self.rt.group_profile.refresh_async = lambda *a, **k: None
        self.rt.commands.try_handle = lambda msg: self.seen.append('command') or False
        self.rt.live_context.append_user = lambda msg: self.seen.append('context')
        self.rt.images.remember = lambda msg: self.seen.append('image')

        def _buffer(*args, **kwargs):
            self.seen.append('memory')
            return 0

        self.rt.curator.buffer_user = _buffer
        self.rt.router.route = lambda msg: self.seen.append('route')
        self.addCleanup(self.rt.store.close)

    def event(self, text, *, uid='123456789', name='小星', group='7770001'):
        data = types.SimpleNamespace(
            message=text, group_id=group, host_id='-', user_id=uid, message_id='m1',
            sender={'name': name, 'role': 'member'}, _geyi_handled=False,
        )
        return types.SimpleNamespace(
            data=data,
            bot_info=types.SimpleNamespace(hash='bot', id='999'),
            platform={'platform': 'qq', 'sdk': 'onebot'},
            base_info={'self_id': '999'},
            send=lambda *a, **k: None,
        )

    def group_key(self, group='7770001'):
        return self.rt._session_key(self.event('你好', group=group), True).as_text()

    def test_registered_group_sender_is_dropped_before_everything(self):
        group_other_ai.add_entry(self.rt.store, self.group_key(), '123456789', '小星')
        self.rt.handle_message(self.event('你好呀，一起打本吗？'), None)
        self.assertEqual(self.seen, [])

    def test_registered_sender_text_is_not_a_command(self):
        group_other_ai.add_entry(self.rt.store, self.group_key(), '123456789', '小星')
        self.rt.handle_message(self.event('歌伊菜单'), None)
        self.assertEqual(self.seen, [])

    def test_private_same_qq_is_unaffected(self):
        group_other_ai.add_entry(self.rt.store, self.group_key(), '123456789', '小星')
        event = self.event('你好呀')
        event.data.group_id = None  # 私聊
        self.rt.handle_message(event, None)
        self.assertIn('route', self.seen)

    def test_unregistered_qq_is_unaffected(self):
        group_other_ai.add_entry(self.rt.store, self.group_key(), '123456789', '小星')
        self.rt.handle_message(self.event('你好呀', uid='222333444'), None)
        self.assertIn('command', self.seen)
        self.assertIn('route', self.seen)

    def test_registration_is_per_group(self):
        group_other_ai.add_entry(self.rt.store, self.group_key('7770001'), '123456789', '小星')
        self.rt.handle_message(self.event('你好呀', group='7770002'), None)
        self.assertIn('route', self.seen)

    def test_dirty_json_does_not_swallow_messages(self):
        self.rt.store.set_setting(group_other_ai.registry_key(self.group_key()), '{broken json')
        self.rt.handle_message(self.event('你好呀'), None)
        self.assertIn('command', self.seen)
        self.assertIn('route', self.seen)


class RendererMaskTest(unittest.TestCase):
    """渲染断词：只对群聊、肉眼不变、CQ/OP/@/动作标记不被破坏、私聊原文不变。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_other_ai_render_',
                                               ignore_cleanup_errors=True)
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(self.store.close)
        self.renderer = Renderer({'reply_audit_enabled': False, 'religion_guard_enabled': False})
        self.renderer.store = self.store
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')

    def render(self, text, *, is_group=True, uid='111', name='群友'):
        return self.renderer.render(text, make_msg(text, uid=uid, name=name, is_group=is_group))

    def test_group_text_gets_zero_width_breaks(self):
        out = self.render('小星说要不要一起打副本？')
        self.assertIsNotNone(out)
        self.assertIn('\u200b', out.text)
        self.assertNotIn('小星', out.text)
        self.assertEqual(out.text.replace('\u200b', ''), '小星说要不要一起打副本？')
        self.assertEqual(out.text, '小\u200b星说要不要一起打副本？')

    def test_private_text_is_untouched(self):
        out = self.render('小星说要不要一起打副本？', is_group=False)
        self.assertEqual(out.text, '小星说要不要一起打副本？')
        self.assertNotIn('\u200b', out.text)

    def test_unregistered_or_other_group_text_is_untouched(self):
        out = self.render('阿蓝说要不要一起打副本？')
        self.assertNotIn('\u200b', out.text)
        other = make_msg('小星来了', session_key=OTHER_GROUP_KEY)
        out = self.renderer.render('小星来了', other)
        self.assertEqual(out.text, '小星来了')

    def test_cq_and_op_at_tags_are_handled_not_leaked(self):
        # 指向别人的 @：标签归一化成占位文本，不会原样漏进正文
        for raw in ('[CQ:at,qq=987654321] 小星你好', '[OP:at,id=987654321] 小星你好'):
            out = self.render(raw, uid='123456789')
            self.assertNotIn('[CQ:', out.text)
            self.assertNotIn('[OP:', out.text)
            self.assertIn('@某用户', out.text)
            self.assertIn('小\u200b星', out.text)
            self.assertNotIn('987654321', out.text)
        # 指向当前发言人：恢复成程序构造的 at 动作码，昵称不被插入零宽
        out = self.render('[CQ:at,qq=123456789] 小星你好', uid='123456789', name='小明')
        self.assertEqual(out.text.count(SEND_AT % '123456789'), 1)
        self.assertIn('小\u200b星', out.text)
        self.assertNotIn('小\u200b明', out.text)

    def test_at_action_is_not_masked(self):
        # @自己 → 动作标记回填「@小明」；标记文本不能再被零宽插入
        out = self.render('@123456789 小星也来吗', uid='123456789', name='小明')
        self.assertIn('@小明', out.text)
        self.assertIn('小\u200b星', out.text)
        self.assertNotIn('小\u200b明', out.text)

    def test_emote_and_action_markers_are_protected(self):
        masked = group_other_ai.mask_names('【EMOTE:小星】小星来了', ['小星'])
        self.assertIn('【EMOTE:小星】', masked)
        self.assertIn('小\u200b星来了', masked)
        marker = '\ue000GEYI_ACTION0\ue001小星'
        masked = group_other_ai.mask_names(marker, ['小星'])
        self.assertIn('\ue000GEYI_ACTION0\ue001', masked)
        self.assertIn('小\u200b星', masked)

    def test_overlapping_occurrences_are_all_broken(self):
        masked = group_other_ai.mask_names('哈哈哈', ['哈哈'])
        self.assertEqual(masked, '哈\u200b哈\u200b哈')
        self.assertNotIn('哈哈', masked)

    def test_single_char_nickname_is_never_masked(self):
        # 命令层不允许 1 字昵称；就算库里被手工塞了，也不能把常见字全断开
        group_other_ai.save_entries(self.store, GROUP_KEY, [{'qq': '123456789', 'nickname': '星'}])
        out = self.render('星说要不要一起打副本？')
        self.assertNotIn('\u200b', out.text)
        self.assertEqual(out.text, '星说要不要一起打副本？')

    def test_dirty_json_fails_open(self):
        self.store.set_setting(group_other_ai.registry_key(GROUP_KEY), '{broken json')
        out = self.render('小星说要不要一起打副本？')
        self.assertEqual(out.text, '小星说要不要一起打副本？')


class ModeStorageTest(unittest.TestCase):
    """过滤档位：默认高、三档持久化、按群隔离、脏值/存储异常 fail-open。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_other_ai_mode_',
                                               ignore_cleanup_errors=True)
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(self.store.close)

    def test_default_mode_is_high(self):
        self.assertEqual(group_other_ai.get_mode(self.store, GROUP_KEY), group_other_ai.MODE_HIGH)
        self.assertEqual(group_other_ai.get_mode(None, GROUP_KEY), group_other_ai.MODE_HIGH)

    def test_each_mode_round_trips(self):
        for raw, expected in (('高', 'high'), ('低', 'low'), ('关', 'off'),
                              ('HIGH', 'high'), ('Low', 'low'), ('off', 'off')):
            self.assertEqual(group_other_ai.normalize_mode(raw), expected, raw)
            self.assertTrue(group_other_ai.set_mode(self.store, GROUP_KEY, raw))
            self.assertEqual(group_other_ai.get_mode(self.store, GROUP_KEY), expected)
        self.assertEqual(group_other_ai.normalize_mode('中'), '')
        self.assertFalse(group_other_ai.set_mode(self.store, GROUP_KEY, '中'))

    def test_mode_is_per_group(self):
        group_other_ai.set_mode(self.store, GROUP_KEY, '低')
        self.assertEqual(group_other_ai.get_mode(self.store, GROUP_KEY), group_other_ai.MODE_LOW)
        self.assertEqual(group_other_ai.get_mode(self.store, OTHER_GROUP_KEY), group_other_ai.MODE_HIGH)
        self.assertEqual(group_other_ai.get_mode(self.store, PRIVATE_KEY), group_other_ai.MODE_HIGH)

    def test_dirty_mode_falls_back_high(self):
        for junk in ('', 'middle', '2', '{}', '高 '):
            self.store.set_setting(group_other_ai.mode_key(GROUP_KEY), junk)
            expected = group_other_ai.MODE_HIGH
            self.assertEqual(group_other_ai.get_mode(self.store, GROUP_KEY), expected, junk)

    def test_mode_fail_open(self):
        broken = TokenStore()
        broken.fail = True
        self.assertEqual(group_other_ai.get_mode(broken, GROUP_KEY), group_other_ai.MODE_HIGH)
        self.assertFalse(group_other_ai.set_mode(broken, GROUP_KEY, '低'))
        self.assertFalse(group_other_ai.set_mode(None, GROUP_KEY, '低'))

    def test_mode_read_failure_does_not_apply_default_high_filter(self):
        class ModeReadFailure(TokenStore):
            def get_setting(self, key, default=None):
                if key.startswith(group_other_ai.MODE_KEY_PREFIX):
                    raise RuntimeError('mode read failed')
                return super().get_setting(key, default)

        store = ModeReadFailure()
        store.data[group_other_ai.registry_key(GROUP_KEY)] = json.dumps(
            [{'qq': '123456789', 'nickname': '小星'}], ensure_ascii=False)
        self.assertFalse(group_other_ai.should_drop_message(store, GROUP_KEY, '123456789'))
        self.assertEqual(group_other_ai.mask_other_ai_names('小星', store, GROUP_KEY), '小星')

    def test_mode_persists_across_reload(self):
        self.assertTrue(group_other_ai.set_mode(self.store, GROUP_KEY, '低'))
        again = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(again.close)
        self.assertEqual(group_other_ai.get_mode(again, GROUP_KEY), group_other_ai.MODE_LOW)


class LowModeEntryTest(unittest.TestCase):
    """低档入口限流：每个 QQ 30 分钟 1 条；三档各自行为。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime
        self.rt = Runtime()
        self.seen = []
        self.rt.group_profile.refresh_async = lambda *a, **k: None
        self.rt.commands.try_handle = lambda msg: self.seen.append('command') or False
        self.rt.live_context.append_user = lambda msg: self.seen.append('context')
        self.rt.images.remember = lambda msg: self.seen.append('image')
        self.rt.curator.buffer_user = lambda *a, **k: self.seen.append('memory') or 0
        self.rt.router.route = lambda msg: self.seen.append('route')
        self.addCleanup(self.rt.store.close)
        self.test_group = 'ai-filter-' + self.id().rsplit('.', 1)[-1]
        self.key = self.rt._session_key(self.event('你好', group=self.test_group), True).as_text()
        self.assertEqual(group_other_ai.add_entry(self.rt.store, self.key, '123456789', '小星'), 'added')

    def event(self, text, *, uid='123456789', group=None):
        group = group or self.test_group
        data = types.SimpleNamespace(
            message=text, group_id=group, host_id='-', user_id=uid, message_id='m1',
            sender={'name': '小星', 'role': 'member'}, _geyi_handled=False,
        )
        return types.SimpleNamespace(
            data=data,
            bot_info=types.SimpleNamespace(hash='bot', id='999'),
            platform={'platform': 'qq', 'sdk': 'onebot'},
            base_info={'self_id': '999'},
            send=lambda *a, **k: None,
        )

    def send(self, text, *, uid='123456789', group=None):
        self.seen.clear()
        self.rt.handle_message(self.event(text, uid=uid, group=group), None)
        return list(self.seen)

    def set_mode(self, mode):
        self.assertTrue(group_other_ai.set_mode(self.rt.store, self.key, mode))

    def expire_msg_window(self, uid='123456789'):
        key = group_other_ai.msg_window_key(self.key, uid)
        self.rt.store.set_setting(key, str(time.time() - group_other_ai.WINDOW_SECONDS - 1))

    def test_low_allows_one_then_drops(self):
        self.set_mode('低')
        self.assertIn('route', self.send('第一条'))
        self.assertEqual(self.send('第二条'), [])
        self.assertEqual(self.send('第三条'), [])

    def test_low_window_expires(self):
        self.set_mode('低')
        self.assertIn('route', self.send('第一条'))
        self.assertEqual(self.send('第二条'), [])
        self.expire_msg_window()
        self.assertIn('route', self.send('第三条'))

    def test_low_is_per_qq(self):
        group_other_ai.add_entry(self.rt.store, self.key, '222333444', '阿蓝')
        self.set_mode('低')
        self.assertIn('route', self.send('小星第一条'))
        self.assertIn('route', self.send('阿蓝第一条', uid='222333444'))
        self.assertEqual(self.send('小星第二条'), [])
        self.assertEqual(self.send('阿蓝第二条', uid='222333444'), [])

    def test_high_mode_drops_every_message(self):
        self.set_mode('高')
        for _ in range(3):
            self.assertEqual(self.send('随便'), [])

    def test_off_mode_never_drops(self):
        self.set_mode('关')
        for _ in range(3):
            self.assertIn('route', self.send('随便'))

    def test_unknown_sender_still_passes(self):
        self.set_mode('高')
        self.assertIn('route', self.send('普通人说话', uid='222333444'))


class CooldownStateTest(unittest.TestCase):
    """冷却时间戳：fail-open、时钟回拨、并发只放行一条、重载仍生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_other_ai_cool_',
                                               ignore_cleanup_errors=True)
        self.path = os.path.join(self.tmp.name, 'test.sqlite3')
        self.store = SQLiteStore(self.path)
        self.addCleanup(self.store.close)
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        self.assertTrue(group_other_ai.set_mode(self.store, GROUP_KEY, '低'))

    def test_window_persists_across_reload(self):
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))
        again = SQLiteStore(self.path)
        self.addCleanup(again.close)
        self.assertEqual(group_other_ai.get_mode(again, GROUP_KEY), group_other_ai.MODE_LOW)
        self.assertTrue(group_other_ai.should_drop_message(again, GROUP_KEY, '123456789'))

    def test_expired_window_allows_again(self):
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))
        key = group_other_ai.msg_window_key(GROUP_KEY, '123456789')
        self.store.set_setting(key, str(time.time() - group_other_ai.WINDOW_SECONDS - 1))
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))

    def test_clock_rollback_is_treated_as_expired(self):
        key = group_other_ai.msg_window_key(GROUP_KEY, '123456789')
        self.store.set_setting(key, str(time.time() + 3600))
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))
        # 回拨后重新计时：时间戳被改写成当前时刻
        self.assertLess(abs(float(self.store.get_setting(key)) - time.time()), 5)

    def test_write_failure_allows_message(self):
        store = TokenStore()
        store.data[group_other_ai.registry_key(GROUP_KEY)] = json.dumps(
            [{'qq': '123456789', 'nickname': '小星'}], ensure_ascii=False)
        store.data[group_other_ai.mode_key(GROUP_KEY)] = 'low'

        def _fail(key, value):
            raise RuntimeError('readonly')

        store.set_setting = _fail
        self.assertFalse(group_other_ai.should_drop_message(store, GROUP_KEY, '123456789'))

    def test_timestamp_read_failure_allows_message(self):
        class TimestampReadFailure(TokenStore):
            def get_setting(self, key, default=None):
                if key.startswith(group_other_ai.MSG_WINDOW_PREFIX):
                    raise RuntimeError('read-only timestamp')
                return super().get_setting(key, default)

        store = TimestampReadFailure()
        store.data[group_other_ai.registry_key(GROUP_KEY)] = json.dumps(
            [{'qq': '123456789', 'nickname': '小星'}], ensure_ascii=False)
        store.data[group_other_ai.mode_key(GROUP_KEY)] = 'low'
        self.assertFalse(group_other_ai.should_drop_message(store, GROUP_KEY, '123456789'))

    def test_storage_failure_never_drops(self):
        broken = TokenStore()
        broken.fail = True
        self.assertFalse(group_other_ai.should_drop_message(broken, GROUP_KEY, '123456789'))

    def test_concurrent_low_messages_only_one_passes(self):
        store = TokenStore()
        store.data[group_other_ai.registry_key(GROUP_KEY)] = json.dumps(
            [{'qq': '123456789', 'nickname': '小星'}], ensure_ascii=False)
        store.data[group_other_ai.mode_key(GROUP_KEY)] = 'low'
        results = []
        guard = threading.Lock()
        start = threading.Event()

        def worker():
            start.wait()
            dropped = group_other_ai.should_drop_message(store, GROUP_KEY, '123456789')
            with guard:
                results.append(dropped)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 5)
        self.assertEqual(results.count(False), 1)  # 并发下也只有 1 条被放行

    def test_remove_and_readd_resets_window(self):
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))
        self.assertTrue(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))
        self.assertEqual(group_other_ai.remove_entry(self.store, GROUP_KEY, '123456789'), 'removed')
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))

    def test_clear_resets_all_windows(self):
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))
        self.assertEqual(group_other_ai.clear_entries(self.store, GROUP_KEY), 1)
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        self.assertFalse(group_other_ai.should_drop_message(self.store, GROUP_KEY, '123456789'))


class LowModeMaskTest(unittest.TestCase):
    """低档断词：只保留每个昵称的第一次原样提及，其余（含同条后续）断词。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_other_ai_lowmask_',
                                               ignore_cleanup_errors=True)
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(self.store.close)
        self.renderer = Renderer({'reply_audit_enabled': False, 'religion_guard_enabled': False})
        self.renderer.store = self.store
        group_other_ai.add_entry(self.store, GROUP_KEY, '123456789', '小星')
        self.assertTrue(group_other_ai.set_mode(self.store, GROUP_KEY, '低'))

    def render(self, text, *, is_group=True):
        return self.renderer.render(text, make_msg(text, uid='111', name='群友', is_group=is_group))

    def expire_nick(self, nickname='小星'):
        key = group_other_ai.nick_window_key(GROUP_KEY, nickname)
        self.store.set_setting(key, str(time.time() - group_other_ai.WINDOW_SECONDS - 1))

    def test_first_mention_kept_then_cooldown_masks(self):
        self.assertEqual(self.render('小星来了').text, '小星来了')
        self.assertEqual(self.render('小星又来了').text, '小\u200b星又来了')
        self.expire_nick()
        self.assertEqual(self.render('小星回来了').text, '小星回来了')

    def test_same_message_only_first_occurrence_kept(self):
        out = self.render('小星和小星都在')
        self.assertEqual(out.text.count('\u200b'), 1)
        self.assertTrue(out.text.startswith('小星'))
        self.assertEqual(out.text.replace('\u200b', ''), '小星和小星都在')

    def test_two_nicknames_have_independent_windows(self):
        group_other_ai.add_entry(self.store, GROUP_KEY, '222333444', '阿蓝')
        self.assertEqual(self.render('小星和阿蓝').text, '小星和阿蓝')
        second = self.render('小星和阿蓝').text
        self.assertIn('小\u200b星', second)
        self.assertIn('阿\u200b蓝', second)
        self.expire_nick('阿蓝')  # 只放开阿蓝的窗口
        third = self.render('小星和阿蓝').text
        self.assertIn('小\u200b星', third)
        self.assertIn('阿蓝', third)
        self.assertNotIn('阿\u200b蓝', third)

    def test_off_mode_keeps_text_and_registry(self):
        self.assertTrue(group_other_ai.set_mode(self.store, GROUP_KEY, '关'))
        for _ in range(3):
            self.assertEqual(self.render('小星和小星').text, '小星和小星')
        self.assertEqual(group_other_ai.load_entries(self.store, GROUP_KEY),
                         [{'qq': '123456789', 'nickname': '小星'}])

    def test_high_mode_masks_every_occurrence(self):
        self.assertTrue(group_other_ai.set_mode(self.store, GROUP_KEY, '高'))
        out = self.render('小星和小星')
        self.assertNotIn('小星', out.text)
        self.assertEqual(out.text.count('\u200b'), 2)

    def test_low_mode_protected_marker_is_not_counted(self):
        raw = '【EMOTE:小星】小星来了'
        masked = group_other_ai.mask_other_ai_names(raw, self.store, GROUP_KEY)
        self.assertIn('【EMOTE:小星】', masked)
        self.assertTrue(masked.endswith('小星来了'))
        again = group_other_ai.mask_other_ai_names(raw, self.store, GROUP_KEY)
        self.assertIn('【EMOTE:小星】', again)
        self.assertNotIn('小星来了', again)

    def test_duplicate_nicknames_are_limited_once(self):
        group_other_ai.add_entry(self.store, GROUP_KEY, '222333444', '小星')
        first = self.render('小星出现').text
        second = self.render('小星出现').text
        self.assertEqual(first, '小星出现')
        self.assertEqual(second, '小\u200b星出现')

    def test_overlapping_nicknames_have_independent_cooldowns(self):
        group_other_ai.add_entry(self.store, GROUP_KEY, '222333444', '星光')
        first = self.render('小星光').text
        self.assertEqual(first, '小星光')
        second = self.render('小星光').text
        self.assertEqual(second, '小\u200b星\u200b光')
        self.assertIn('星\u200b光', second)

    def test_overlapping_nickname_can_keep_allowed_match(self):
        group_other_ai.add_entry(self.store, GROUP_KEY, '222333444', '星光')
        self.store.set_setting(group_other_ai.nick_window_key(GROUP_KEY, '小星'),
                               str(time.time()))
        out = self.render('小星光').text
        self.assertEqual(out, '小\u200b星光')
        self.assertIn('星光', out)

    def test_private_chat_never_masks_even_in_low(self):
        out = self.render('小星来了', is_group=False)
        self.assertEqual(out.text, '小星来了')


class ModeCommandTest(unittest.TestCase):
    """`ai过滤 高/低/关`：群管权限、私聊指路、菜单、非法参数。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='geyi_other_ai_modecmd_',
                                               ignore_cleanup_errors=True)
        self.store = SQLiteStore(os.path.join(self.tmp.name, 'test.sqlite3'))
        self.addCleanup(self.store.close)
        self.replies = []
        self.is_master = False
        self.is_admin = True
        runtime = types.SimpleNamespace(
            store=self.store,
            config={},
            permissions=types.SimpleNamespace(
                is_master=lambda msg: self.is_master,
                is_group_admin=lambda msg: self.is_admin,
            ),
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(runtime)

    def run_text(self, text, *, is_group=True):
        msg = make_msg(text, session_key=GROUP_KEY if is_group else PRIVATE_KEY,
                       is_group=is_group)
        return self.router.try_handle(msg)

    def last(self):
        return self.replies[-1] if self.replies else ''

    def test_menu_has_filter_mode(self):
        self.assertIn('[本群] ai过滤 高/低/关', self.router._cmd_admin_menu(make_msg()))

    def test_admin_sets_all_modes_and_status(self):
        for text, expected in (('ai过滤 低', '低'), ('ai过滤 关', '关'), ('ai过滤 高', '高')):
            self.assertTrue(self.run_text(text), text)
            self.assertIn(f'已设为：{expected}', self.last(), text)
            mode = group_other_ai.get_mode(self.store, GROUP_KEY)
            self.assertEqual(group_other_ai.MODE_LABELS[mode], expected)
        self.assertTrue(self.run_text('ai过滤'))
        self.assertIn('过滤模式：高', self.last())
        self.assertIn('ai过滤 高/低/关', self.last())

    def test_case_insensitive(self):
        self.assertTrue(self.run_text('AI过滤 低'))
        self.assertEqual(group_other_ai.get_mode(self.store, GROUP_KEY), group_other_ai.MODE_LOW)

    def test_ordinary_member_denied(self):
        self.is_admin = False
        for text in ('ai过滤 高', 'ai过滤 低', 'ai过滤 关', 'ai过滤'):
            self.assertTrue(self.run_text(text), text)
            self.assertIn('只有骰主或主人', self.last(), text)
        self.assertEqual(group_other_ai.get_mode(self.store, GROUP_KEY), group_other_ai.MODE_HIGH)

    def test_private_chat_gets_hint(self):
        self.is_master = True
        self.assertTrue(self.run_text('ai过滤 低', is_group=False))
        self.assertIn('请在群聊中使用', self.last())
        self.assertEqual(group_other_ai.get_mode(self.store, PRIVATE_KEY), group_other_ai.MODE_HIGH)

    def test_invalid_value_usage(self):
        self.assertTrue(self.run_text('ai过滤 中'))
        self.assertIn('用法：ai过滤 高/低/关', self.last())
        self.assertEqual(group_other_ai.get_mode(self.store, GROUP_KEY), group_other_ai.MODE_HIGH)


if __name__ == '__main__':
    unittest.main()
