# -*- encoding: utf-8 -*-
"""群聊↔私聊记忆的「方向」：单向共享 vs 双向互通。

骰主要的口径（2026-11-05）：
**「仅对私聊单方面共享群聊记忆」**——私聊能接住群里的话题，
但群里不能翻出私聊里的事（否则一群里出现"好几个恋爱对象"）。

所以这里锁三件事：
1. 总闸关 → 两边彻底隔离；
2. 总闸开 + 反向关 → 私聊看得到群聊，**群聊看不到私聊**；
3. 总闸开 + 反向开 → 双向（旧行为）。
外加：私聊侧还要过关系阶段闸（`relation.cross`）。
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_scope_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.memory.repository import MemoryRepository, cross_scope_enabled  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402

UID = '5550001'
GID = '7770001'


def _msg(is_group: bool, text: str = '在吗') -> IncomingMessage:
    session = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='-' if is_group else '',
                         target_id=GID if is_group else UID)
    return IncomingMessage(
        session=session, message_id='1', sender_id=UID, sender_name='测试',
        text=text, raw_text=text, is_group=is_group, mentioned_bot=False,
        event=types.SimpleNamespace(send=lambda *a, **k: None), proc=None,
        arrival_seq=1, sender_is_bot=False,
    )


class DirectionFlagTest(unittest.TestCase):
    """纯函数：三种开关组合的含义。"""

    def test_master_off_isolates_both_sides(self):
        cfg = {'memory_private_enabled': False, 'memory_private_to_group': True}
        self.assertFalse(cross_scope_enabled(cfg, is_group=False))
        self.assertFalse(cross_scope_enabled(cfg, is_group=True))

    def test_one_way_default(self):
        cfg = {'memory_private_enabled': True, 'memory_private_to_group': False}
        self.assertTrue(cross_scope_enabled(cfg, is_group=False), '私聊应该能读到群聊')
        self.assertFalse(cross_scope_enabled(cfg, is_group=True), '群聊不该读到私聊')

    def test_two_way_opt_in(self):
        cfg = {'memory_private_enabled': True, 'memory_private_to_group': True}
        self.assertTrue(cross_scope_enabled(cfg, is_group=True))

    def test_relation_gate_only_affects_private(self):
        cfg = {'memory_private_enabled': True, 'memory_private_to_group': False}
        self.assertFalse(cross_scope_enabled(cfg, is_group=False,
                                             relation={'cross': False}))
        self.assertTrue(cross_scope_enabled(cfg, is_group=False,
                                            relation={'cross': True}))


class DirectionRecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='geyi_scope_db_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'scope.sqlite3'))
        self.grp_key = 'h:qq:onebot:group:-:%s' % GID
        self.priv_key = 'h:qq:onebot:private:-:%s' % UID
        self.store.add_user_fact(self.grp_key, UID, '群里爱吃辣', importance=0.8)
        self.store.add_user_fact(self.priv_key, UID, '私聊说讨厌胡萝卜', importance=0.8)
        self.store.set_user_impression(UID, '群里的活跃群友', scope='group')
        self.store.set_user_impression(UID, '私聊里聊得来的人', scope='private')
        self.cfg = {
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

    def _block(self, is_group: bool, relation=None) -> str:
        repo = MemoryRepository(self.store, dict(self.cfg))
        return repo.build_block(_msg(is_group), relation=relation)

    def test_private_sees_group_memory(self):
        block = self._block(is_group=False)
        self.assertIn('群里爱吃辣', block, '单向共享的核心：私聊要能接住群聊的话题')
        self.assertIn('私聊说讨厌胡萝卜', block)
        self.assertIn('群里的活跃群友', block, '私聊里也能看到群聊印象')

    def test_group_does_not_see_private_memory(self):
        block = self._block(is_group=True)
        self.assertIn('群里爱吃辣', block)
        self.assertNotIn('私聊说讨厌胡萝卜', block, '群聊不该翻出私聊的事实')
        self.assertNotIn('私聊里聊得来的人', block, '群聊不该翻出私聊的印象')

    def test_reverse_switch_opens_group_side(self):
        self.cfg['memory_private_to_group'] = True
        block = self._block(is_group=True)
        self.assertIn('私聊说讨厌胡萝卜', block)

    def test_master_off_isolates(self):
        self.cfg['memory_private_enabled'] = False
        priv = self._block(is_group=False)
        self.assertNotIn('群里爱吃辣', priv)
        grp = self._block(is_group=True)
        self.assertNotIn('私聊说讨厌胡萝卜', grp)

    def test_stage_gate_blocks_private_side(self):
        block = self._block(is_group=False, relation={'cross': False})
        self.assertNotIn('群里爱吃辣', block, '关系阶段没到就不该把群聊记忆带进私聊')


if __name__ == '__main__':
    unittest.main(verbosity=2)
