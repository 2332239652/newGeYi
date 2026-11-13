# -*- encoding: utf-8 -*-
"""私聊「群聊记忆共享」：会话级选择 + 全局默认。

为什么要这个（2026-11-09 骰主反馈）：`memory_private_enabled` 原来是**唯一总闸**，
于是「这个私聊不想被带进群聊里的事」只能去改全局配置——一改就是所有人的私聊一起变。
而且「忘了我」在私聊只清得掉当前会话（+ 跨群偏好），群里那份照旧被召回，
用户会觉得"说了忘记还照样提"。

现在：
1. 每个私聊可以自己开/关（settings 键 `mempriv:<session_key>` = '1'/'0'）；
2. 没设过才看 `memory_private_enabled`（它退化成**默认值**）；
3. 群聊那一侧仍然只由 `memory_private_to_group` 统一决定（不提供会话级开关）；
4. 显式「开」也要过关系阶段门——否则陌生人一私聊就能把群里的你摸清。
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_privshare_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.memory.repository import (  # noqa: E402
    SHARE_OVERRIDE_PREFIX, MemoryRepository, cross_scope_enabled,
)
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.commands import CommandRouter  # noqa: E402
from newGeYi.routing.permissions import Permissions  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402

UID = '6660001'
OTHER = '6660002'
GID = '8880001'
OWNER = '9990001'

GROUP_KEY = 'h:qq:onebot:group:-:%s' % GID
PRIVATE_KEY = 'h:qq:onebot:private:-:%s' % UID


def _msg(is_group: bool, text: str = '在吗', sender: str = UID) -> IncomingMessage:
    session = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='-' if is_group else '',
                         target_id=GID if is_group else sender)
    return IncomingMessage(
        session=session, message_id='1', sender_id=sender, sender_name='测试',
        text=text, raw_text=text, is_group=is_group, mentioned_bot=False,
        event=types.SimpleNamespace(send=lambda *a, **k: None), proc=None,
        arrival_seq=1, sender_is_bot=False,
    )


class OverrideFlagTest(unittest.TestCase):
    """纯函数：会话覆盖与全局默认的优先级。"""

    BASE = {'memory_private_enabled': True, 'memory_private_to_group': False}

    def test_no_override_follows_default(self):
        self.assertTrue(cross_scope_enabled(self.BASE, is_group=False))
        cfg = dict(self.BASE, memory_private_enabled=False)
        self.assertFalse(cross_scope_enabled(cfg, is_group=False))
        self.assertFalse(cross_scope_enabled(cfg, is_group=False, override=None))

    def test_session_can_opt_out_of_default_on(self):
        self.assertFalse(cross_scope_enabled(self.BASE, is_group=False, override=False))

    def test_session_can_opt_in_despite_default_off(self):
        cfg = dict(self.BASE, memory_private_enabled=False)
        self.assertTrue(cross_scope_enabled(cfg, is_group=False, override=True))

    def test_stage_gate_still_applies_to_explicit_on(self):
        cfg = dict(self.BASE, memory_private_enabled=False)
        self.assertFalse(cross_scope_enabled(cfg, is_group=False, override=True,
                                             relation={'cross': False}))
        self.assertTrue(cross_scope_enabled(cfg, is_group=False, override=True,
                                            relation={'cross': True}))

    def test_group_side_ignores_override(self):
        cfg = dict(self.BASE, memory_private_to_group=False)
        self.assertFalse(cross_scope_enabled(cfg, is_group=True, override=True))
        cfg_rev = dict(self.BASE, memory_private_to_group=True)
        self.assertTrue(cross_scope_enabled(cfg_rev, is_group=True, override=False))


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='geyi_privshare_db_', dir=_SCRATCH)
        self.store = SQLiteStore(os.path.join(self.tmp, 'share.sqlite3'))
        self.store.add_user_fact(GROUP_KEY, UID, '群里爱吃辣', importance=0.8)
        self.store.add_user_fact(PRIVATE_KEY, UID, '私聊说讨厌胡萝卜', importance=0.8)
        self.store.set_user_impression(UID, '群里的活跃群友', scope='group')
        self.store.set_user_impression(UID, '私聊里聊得来的人', scope='private')
        self.cfg = {
            'memory_enabled': True,
            'memory_impression_enabled': True,
            'memory_cross_group_enabled': True,
            'memory_inject_facts': 12,
            'memory_inject_self': 6,
            'memory_inject_summaries': 3,
            'memory_max_chars': 4000,
            'memory_private_enabled': True,
            'memory_private_to_group': False,
        }

    def tearDown(self):
        self.store.close()

    def repo(self):
        return MemoryRepository(self.store, dict(self.cfg))

    def block(self, is_group=False, relation=None):
        return self.repo().build_block(_msg(is_group), relation=relation)


class OverrideStorageTest(_StoreCase):
    def test_write_read_clear(self):
        repo = self.repo()
        self.assertIsNone(repo.share_override(PRIVATE_KEY), '默认应当是"没设过"')
        repo.set_share_override(PRIVATE_KEY, False)
        self.assertEqual(self.store.get_setting(SHARE_OVERRIDE_PREFIX + PRIVATE_KEY), '0')
        self.assertFalse(repo.share_override(PRIVATE_KEY))
        repo.set_share_override(PRIVATE_KEY, True)
        self.assertTrue(repo.share_override(PRIVATE_KEY))
        repo.set_share_override(PRIVATE_KEY, None)
        self.assertIsNone(repo.share_override(PRIVATE_KEY), '清除后应当回到"没设过"')

    def test_status_reports_source_and_stage(self):
        repo = self.repo()
        info = repo.share_status(PRIVATE_KEY)
        self.assertEqual(info['source'], 'default')
        self.assertTrue(info['enabled'])
        repo.set_share_override(PRIVATE_KEY, False)
        info = repo.share_status(PRIVATE_KEY)
        self.assertEqual(info['source'], 'user')
        self.assertFalse(info['enabled'])
        self.assertFalse(info['stage_blocked'])
        # 显式开着、但关系阶段没到 → 未生效且标记原因
        repo.set_share_override(PRIVATE_KEY, True)
        info = repo.share_status(PRIVATE_KEY, relation={'cross': False})
        self.assertFalse(info['enabled'])
        self.assertTrue(info['stage_blocked'])


class OverrideRecallTest(_StoreCase):
    def test_opt_out_hides_group_fact_and_impression(self):
        self.repo().set_share_override(PRIVATE_KEY, False)
        block = self.block(is_group=False)
        self.assertNotIn('群里爱吃辣', block, '设了"关"就不该把群里的记忆带进私聊')
        self.assertNotIn('群里的活跃群友', block)
        self.assertIn('私聊说讨厌胡萝卜', block, '自己这个私聊的记忆当然要留着')

    def test_opt_in_works_when_default_off(self):
        self.cfg['memory_private_enabled'] = False
        self.repo().set_share_override(PRIVATE_KEY, True)
        block = self.block(is_group=False)
        self.assertIn('群里爱吃辣', block)

    def test_group_side_still_isolated(self):
        self.repo().set_share_override(PRIVATE_KEY, True)
        block = self.block(is_group=True)
        self.assertIn('群里爱吃辣', block)
        self.assertNotIn('私聊说讨厌胡萝卜', block, '会话级开关不能把群聊那一侧打开')

    def test_stage_gate_wins_over_opt_in(self):
        self.repo().set_share_override(PRIVATE_KEY, True)
        block = self.block(is_group=False, relation={'cross': False})
        self.assertNotIn('群里爱吃辣', block)


class ShareCommandTest(_StoreCase):
    """QQ 指令：自己改、骰主查/清。"""

    def setUp(self):
        super().setUp()
        self.replies: list[str] = []
        self.rt = types.SimpleNamespace(
            config=self.cfg,
            store=self.store,
            memory=MemoryRepository(self.store, self.cfg),
            permissions=Permissions({'owner_ids': [OWNER]}, owner_ids_env=''),
            relationship=None,
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(self.rt)

    def say(self, text, is_group=False, sender=UID):
        self.replies.clear()
        msg = _msg(is_group, text, sender)
        self.assertTrue(self.router._dispatch(msg), '命令没被识别：%s' % text)
        return self.replies[-1]

    def test_status_shows_default(self):
        text = self.say('群聊记忆')
        self.assertIn('开启', text)
        self.assertIn('默认', text)

    def test_off_then_status_then_default(self):
        self.assertIn('关闭', self.say('群聊记忆关'))
        self.assertEqual(self.store.get_setting(SHARE_OVERRIDE_PREFIX + PRIVATE_KEY), '0')
        text = self.say('群聊记忆')
        self.assertIn('关闭', text)
        self.assertIn('你设的', text)
        text = self.say('群聊记忆默认')
        self.assertIn('默认', text)
        self.assertIsNone(self.rt.memory.share_override(PRIVATE_KEY))

    def test_group_chat_refuses(self):
        text = self.say('群聊记忆关', is_group=True)
        self.assertIn('私聊', text)
        self.assertIsNone(self.rt.memory.share_override(GROUP_KEY))

    def test_owner_can_list_and_reset(self):
        self.rt.memory.set_share_override(PRIVATE_KEY, False)
        text = self.say('群聊记忆列表', sender=OWNER)
        self.assertIn(UID, text)
        self.assertIn('关', text)
        self.say('群聊记忆重置 %s' % UID, sender=OWNER)
        self.assertIsNone(self.rt.memory.share_override(PRIVATE_KEY))

    def test_non_owner_cannot_list(self):
        text = self.say('群聊记忆列表', sender=OTHER)
        self.assertIn('骰主', text)

    def test_forget_me_mentions_share_when_on(self):
        self.rt.memory.set_share_override(PRIVATE_KEY, True)
        self.say('忘了我')
        text = self.say('确认忘了我')
        self.assertIn('群聊记忆', text, '开着共享时应当提醒"群里的事仍会想起来"')

    def test_forget_me_quiet_when_off(self):
        self.rt.memory.set_share_override(PRIVATE_KEY, False)
        self.say('忘了我')
        text = self.say('确认忘了我')
        self.assertNotIn('群聊记忆', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
