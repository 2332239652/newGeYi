# -*- encoding: utf-8 -*-
"""菜单拆分（2026-11-12）：公开菜单只留用户指令，管理指令整体挪进「歌伊管理菜单」。

为什么要有测试：菜单是**唯一**自己介绍自己的地方，拆错的表现不是报错，
而是「普通人被刷半屏管理指令」或者「群管找不到开关」——两种都要等有人抱怨才会发现。
"""

from __future__ import annotations

import copy
import os
import tempfile
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_menu_split_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.routing.commands import CommandRouter  # noqa: E402

CONFIG = {
    'triggers': ['歌伊'], 'worldbook_enabled': True, 'active_enabled': True,
    'active_shadow': True, 'memory_curator_enabled': True,
    'image_enabled': True, 'voice_enabled': True, 'private_enabled': True,
    'sponsor_tier1_ids': ['111'], 'sponsor_tier2_ids': [],
}


class _Store:
    def get_setting(self, key, default=None):
        return default


class _Perms:
    def __init__(self, master=False, admin=False):
        self.master = master
        self.admin = admin

    def is_master(self, msg):
        return self.master

    def is_group_admin(self, msg):
        return self.admin


class _Session:
    def as_text(self):
        return 'h:qq:onebot:group:-:1'


class _Msg:
    def __init__(self, text='', is_group=True, sender_id='10001'):
        self.text = text
        self.is_group = is_group
        self.sender_id = sender_id
        self.session = _Session()


class _Runtime:
    def __init__(self, perms):
        self.config = copy.deepcopy(CONFIG)
        self.gateway = None
        self.store = _Store()
        self.permissions = perms
        self.replies: list[str] = []
        self.video_context = None

    def reply(self, msg, text):
        self.replies.append(text)


class MenuSplitTest(unittest.TestCase):
    def _router(self, **perms):
        return CommandRouter(_Runtime(_Perms(**perms)))

    def _send(self, text, **perms):
        runtime = _Runtime(_Perms(**perms))
        CommandRouter(runtime).try_handle(_Msg(text))
        return '\n'.join(runtime.replies)

    # ---------- 公开菜单 ----------
    def test_public_menu_lists_user_commands(self):
        text = self._router()._cmd_menu(None)
        for word in ('歌伊状态', '今日用量', '我的记忆', '忘了我', '叫我', '关系',
                     '今日额度', '剧情模式', '世界书状态'):
            self.assertIn(word, text, '公开菜单少了用户指令：%s' % word)

    def test_public_menu_has_no_admin_commands(self):
        text = self._router()._cmd_menu(None)
        for word in ('渠道状态', '切换渠道', '设置key', '静默列表', '重载设定',
                     '赞助加', '重置上下文', '休眠时间', '导出记忆', '本群被动'):
            self.assertNotIn(word, text, '管理指令不该出现在公开菜单：%s' % word)

    def test_public_menu_points_to_admin_menu(self):
        self.assertIn('歌伊管理菜单', self._router()._cmd_menu(None))

    # ---------- 管理菜单 ----------
    def test_admin_menu_lists_admin_commands(self):
        text = self._router()._cmd_admin_menu(None)
        for word in ('本群被动', '本群主动', '设置群背景', '渠道状态', '切换渠道',
                     '设置key', '静默列表', '赞助名单', '休眠时间', '导出记忆',
                     '剧情模式列表', '当前渠道'):
            self.assertIn(word, text, '管理菜单少了指令：%s' % word)

    def test_admin_menu_has_no_thanks(self):
        """鸣谢只属于公开菜单：管理菜单是骰主自己看的，不需要。"""
        self.assertNotIn('鸣谢', self._router()._cmd_admin_menu(None))

    # ---------- 权限 ----------
    def test_normal_user_cannot_see_admin_menu(self):
        text = self._send('歌伊管理菜单')
        self.assertIn('群管和骰主', text)
        self.assertNotIn('渠道状态', text)

    def test_group_admin_can_see_admin_menu(self):
        text = self._send('管理菜单', admin=True)
        self.assertIn('本群被动', text)
        self.assertIn('渠道状态', text)

    def test_master_can_see_admin_menu(self):
        text = self._send('管理员菜单', master=True)
        self.assertIn('本群被动', text)

    def test_public_menu_is_open_to_everyone(self):
        text = self._send('歌伊菜单')
        self.assertIn('歌伊菜单', text)
        self.assertNotIn('群管和骰主才', text)

    def test_private_message_can_open_admin_menu_as_master(self):
        runtime = _Runtime(_Perms(master=True))
        CommandRouter(runtime).try_handle(_Msg('歌伊管理菜单', is_group=False))
        self.assertIn('本群被动', '\n'.join(runtime.replies))


if __name__ == '__main__':
    unittest.main()
