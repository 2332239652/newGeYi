# -*- encoding: utf-8 -*-
"""自定义称呼：歌伊怎么叫一个人。

骰主 2026-11-09 的需求原话：「让用户不单单被识别为 QQ 昵称，而是可以自定义歌伊的称呼」。
定下的口径：**全局一个**（按 QQ 号存，不分群）、**本人设置、骰主只能重置**。

这里锁四件事：
1. 校验（标签结构字符、纯数字、保留词、长度）——称呼会直接进 `昵称(QQ号)：内容` 这个标签，
   放开了就等于让群友伪造「另一个人」的发言；
2. 存/取/清（`settings` 的 `alias:<QQ号>`，空的 = 回 QQ 昵称）；
3. 运行时接线：入口把 `sender_name` 换成称呼，下游（提示词标签、记忆缓冲）跟着走；
4. 指令：`叫我 <名字>` / `我的称呼` / `称呼重置`，且「叫我起床」这种正常说话不能被吃掉。
"""

from __future__ import annotations

import itertools
import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_alias_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.context import aliases  # noqa: E402
from newGeYi.memory.repository import MemoryRepository  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.routing.commands import CommandRouter  # noqa: E402
from newGeYi.routing.permissions import Permissions  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402

UID = '7770001'
OTHER = '7770002'
OWNER = '9990001'

# Runtime 相关用例共用一个临时状态目录，QQ 号必须逐例唯一，
# 否则上一个用例设的称呼会漏进下一个（本文件第一版就踩了）。
_SEQ = itertools.count(1)


def _msg(text='你好', sender=UID, name='QQ昵称', is_group=False) -> IncomingMessage:
    session = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                         session_type='group' if is_group else 'private',
                         host_id='-' if is_group else '',
                         target_id='8880001' if is_group else sender)
    return IncomingMessage(
        session=session, message_id='1', sender_id=sender, sender_name=name,
        text=text, raw_text=text, is_group=is_group, mentioned_bot=False,
        event=types.SimpleNamespace(send=lambda *a, **k: None), proc=None,
        arrival_seq=1, sender_is_bot=False,
    )


class SanitizeTest(unittest.TestCase):
    def test_normal_names_pass(self):
        for raw in ('小明', '阿花', 'Alice', '  小 明  ', '面包屑'):
            text, error = aliases.sanitize(raw)
            self.assertEqual(error, '', '%r 不该被拒：%s' % (raw, error))
            self.assertTrue(text)

    def test_whitespace_is_collapsed(self):
        self.assertEqual(aliases.sanitize('  小   明  ')[0], '小 明')

    def test_empty_is_rejected(self):
        for raw in ('', '   ', None):
            self.assertTrue(aliases.sanitize(raw)[1])

    def test_too_long_is_rejected(self):
        text, error = aliases.sanitize('一二三四五六七八九十十一十二')
        self.assertEqual(text, '')
        self.assertIn('12', error)

    def test_label_structure_chars_are_rejected(self):
        """称呼会拼进 `昵称(QQ号)：内容`，这些字符能伪造发言人。"""
        for raw in ('小明(123)', '小:明', '小：明', '小【明】', '[OP:at,id=1]', '小@明'):
            text, error = aliases.sanitize(raw)
            self.assertEqual(text, '', '%r 应该被拒（会破坏标签）' % raw)
            self.assertTrue(error)

    def test_pure_digits_rejected(self):
        self.assertEqual(aliases.sanitize('123456')[0], '')

    def test_reserved_words_rejected(self):
        for raw in ('主人', '骰主', '系统', '管理员'):
            self.assertEqual(aliases.sanitize(raw)[0], '', '%r 不该能当称呼' % raw)

    def test_bot_name_rejected(self):
        self.assertEqual(aliases.sanitize('歌伊')[0], '')
        self.assertEqual(aliases.sanitize('歌伊的小猫')[0], '')


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='geyi_alias_db_', dir=_SCRATCH)
        self.store = SQLiteStore(os.path.join(self.dir, 'alias.sqlite3'))

    def tearDown(self):
        self.store.close()


class StoreTest(_StoreCase):
    def test_empty_by_default(self):
        self.assertEqual(aliases.get(self.store, UID), '')
        self.assertEqual(aliases.display(self.store, UID, 'QQ昵称'), 'QQ昵称')

    def test_set_get_clear(self):
        aliases.set_alias(self.store, UID, '小明')
        self.assertEqual(aliases.get(self.store, UID), '小明')
        self.assertEqual(aliases.display(self.store, UID, 'QQ昵称'), '小明')
        self.assertEqual(aliases.listing(self.store), [(UID, '小明')])
        aliases.set_alias(self.store, UID, None)
        self.assertEqual(aliases.get(self.store, UID), '', '清除后应回到 QQ 昵称')
        self.assertEqual(aliases.listing(self.store), [], '清空的键不该出现在列表里')

    def test_aliases_are_per_user(self):
        aliases.set_alias(self.store, UID, '小明')
        self.assertEqual(aliases.get(self.store, OTHER), '')


class RuntimeWiringTest(unittest.TestCase):
    """入口把 sender_name 换成称呼，下游全跟着走。"""

    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')
        self.rt.group_profile.refresh_async = lambda *a, **k: None
        self.captured = []
        self.rt.router.route = lambda msg: self.captured.append(msg)
        self.uid = '777%04d' % next(_SEQ)

    def event(self, raw='晚上好', name='AAA平板批发商', user=None):
        data = types.SimpleNamespace(message=raw, group_id='8880001',
                                     user_id=user or self.uid,
                                     message_id='1', sender={'name': name}, host_id='-')
        return types.SimpleNamespace(
            data=data,
            bot_info=types.SimpleNamespace(hash='h', id='999'),
            platform={'platform': 'qq', 'sdk': 'onebot'},
            base_info={'self_id': '999'},
            send=lambda *a, **k: None,
        )

    def test_no_alias_keeps_qq_nickname(self):
        self.rt.handle_message(self.event(), None)
        self.assertEqual(self.captured[0].sender_name, 'AAA平板批发商')

    def test_alias_replaces_sender_name_everywhere(self):
        aliases.set_alias(self.rt.store, self.uid, '小明')
        self.rt.handle_message(self.event(), None)
        msg = self.captured[0]
        self.assertEqual(msg.sender_name, '小明', '入口没把称呼换上')
        # 记忆缓冲里也应当是称呼（提炼时看到的正是这一列）
        rows = self.rt.store.buffer_fetch(msg.session.as_text(), 0, limit=5)
        self.assertTrue(rows)
        self.assertEqual(rows[-1]['sender_name'], '小明')

    def test_prompt_carries_alias_note_and_label(self):
        aliases.set_alias(self.rt.store, self.uid, '小明')
        msg = _msg('晚上好', sender=self.uid, name='小明', is_group=True)
        messages = self.rt.prompt_builder.build(msg)
        system = messages[0]['content']
        self.assertIn('「小明」', system, 'system 里应当有称呼说明')
        self.assertIn('称呼', system)
        current = messages[-1]['content']
        self.assertIn('小明(%s)' % self.uid, current, '当前消息的标签应当是 称呼(QQ号)')

    def test_prompt_without_alias_has_no_note(self):
        msg = _msg('晚上好', sender=self.uid, name='QQ昵称', is_group=True)
        system = self.rt.prompt_builder.build(msg)[0]['content']
        self.assertNotIn('对方希望你叫他', system)


class AliasCommandTest(_StoreCase):
    def setUp(self):
        super().setUp()
        self.replies: list[str] = []
        cfg = {'memory_private_enabled': True, 'memory_impression_enabled': True}
        self.rt = types.SimpleNamespace(
            config=cfg,
            store=self.store,
            memory=MemoryRepository(self.store, cfg),
            permissions=Permissions({'owner_ids': [OWNER]}, owner_ids_env=''),
            relationship=None,
            reply=lambda msg, text: self.replies.append(text),
        )
        self.router = CommandRouter(self.rt)

    def say(self, text, sender=UID, name='QQ昵称', is_group=False):
        self.replies.clear()
        handled = self.router._dispatch(_msg(text, sender=sender, name=name, is_group=is_group))
        return handled, (self.replies[-1] if self.replies else '')

    def test_set_then_show_then_reset(self):
        handled, text = self.say('叫我 小明')
        self.assertTrue(handled)
        self.assertIn('小明', text)
        self.assertEqual(aliases.get(self.store, UID), '小明')
        handled, text = self.say('我的称呼')
        self.assertIn('小明', text)
        handled, text = self.say('称呼重置')
        self.assertIn('改回', text)
        self.assertEqual(aliases.get(self.store, UID), '')

    def test_show_without_alias_names_qq_nickname(self):
        handled, text = self.say('我的称呼', name='QQ昵称')
        self.assertTrue(handled)
        self.assertIn('QQ昵称', text)

    def test_invalid_alias_is_rejected_and_not_stored(self):
        handled, text = self.say('叫我 主人')
        self.assertTrue(handled)
        self.assertIn('称呼', text)
        self.assertEqual(aliases.get(self.store, UID), '', '非法称呼不该被存下来')

    def test_casual_talk_is_not_swallowed(self):
        """「叫我起床」是正常说话，不能变成把称呼设成"起床"。"""
        handled, _ = self.say('叫我起床')
        self.assertFalse(handled, '不带分隔符的「叫我…」应当继续走聊天')
        self.assertEqual(aliases.get(self.store, UID), '')

    def test_full_width_colon_works(self):
        handled, _ = self.say('叫我：阿花')
        self.assertTrue(handled)
        self.assertEqual(aliases.get(self.store, UID), '阿花')

    def test_owner_list_and_reset(self):
        aliases.set_alias(self.store, UID, '小明')
        handled, text = self.say('称呼列表', sender=OWNER)
        self.assertTrue(handled)
        self.assertIn(UID, text)
        self.assertIn('小明', text)
        handled, text = self.say('称呼重置 %s' % UID, sender=OWNER)
        self.assertTrue(handled)
        self.assertEqual(aliases.get(self.store, UID), '')

    def test_non_owner_cannot_list_or_reset_others(self):
        aliases.set_alias(self.store, UID, '小明')
        handled, text = self.say('称呼列表', sender=OTHER)
        self.assertTrue(handled)
        self.assertIn('骰主', text)
        handled, text = self.say('称呼重置 %s' % UID, sender=OTHER)
        self.assertTrue(handled)
        self.assertIn('骰主', text)
        self.assertEqual(aliases.get(self.store, UID), '小明', '非骰主不该清掉别人的称呼')

    def test_forget_me_clears_alias(self):
        aliases.set_alias(self.store, UID, '小明')
        # 第一次触发二次确认警告
        handled, text = self.say('忘了我')
        self.assertTrue(handled)
        self.assertIn('重要确认', text)
        self.assertEqual(aliases.get(self.store, UID), '小明', '未确认前不应清除称呼')
        # 确认后清除称呼
        handled, text = self.say('确认忘了我')
        self.assertTrue(handled)
        self.assertIn('称呼', text)
        self.assertEqual(aliases.get(self.store, UID), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
