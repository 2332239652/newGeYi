# -*- encoding: utf-8 -*-
"""测试「忘了我」二次确认与「我的记忆」序号标记及精确删除功能。"""

from __future__ import annotations

import os
import tempfile
import time
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_forget_del_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.runtime.app import Runtime  # noqa: E402
from newGeYi.context import aliases  # noqa: E402


class ForgetAndDeleteMemoryTest(unittest.TestCase):
    def setUp(self):
        import shutil
        from newGeYi.storage.database import SQLiteStore
        self.tmp = tempfile.mkdtemp(prefix='geyi_forget_del_')
        db_path = os.path.join(self.tmp, 'test.sqlite3')
        self.rt = Runtime()
        self.rt.store = SQLiteStore(db_path)
        self.rt.commands.r = self.rt
        self.rt.memory.store = self.rt.store
        self.rt.relationship.store = self.rt.store
        self.uid = '200001'
        self.session = SessionKey(
            bot_hash='h', platform='qq', sdk='onebot',
            session_type='private', host_id='-', target_id=self.uid,
        )

    def tearDown(self):
        import shutil
        try:
            self.rt.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _msg(self, text: str, uid: str | None = None) -> IncomingMessage:
        user_id = str(uid or self.uid)
        sess = SessionKey(
            bot_hash='h', platform='qq', sdk='onebot',
            session_type='private', host_id='-', target_id=user_id,
        )
        return IncomingMessage(
            session=sess, message_id='1', sender_id=user_id,
            sender_name='道君', text=text, raw_text=text,
            is_group=False, mentioned_bot=True,
            event=types.SimpleNamespace(send=lambda *a, **k: None),
            proc=None, arrival_seq=1, sender_is_bot=False,
        )

    def say(self, text: str, uid: str | None = None) -> tuple[bool, str]:
        msg = self._msg(text, uid=uid)
        replies = []
        orig_reply = self.rt.reply
        self.rt.reply = lambda m, resp: replies.append(resp)
        try:
            handled = self.rt.commands.try_handle(msg)
        finally:
            self.rt.reply = orig_reply
        return handled, replies[0] if replies else ''

    def test_view_memory_shows_numbered_list(self):
        # 写入3条事实
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '喜欢吃肉桂苹果卷', source='explicit')
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '是一名冒险家', source='explicit')
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '不爱吃胡萝卜', source='explicit')

        handled, text = self.say('我的记忆')
        self.assertTrue(handled)
        self.assertIn('1.', text)
        self.assertIn('2.', text)
        self.assertIn('3.', text)
        self.assertIn('删除记忆 <序号>', text)

    def test_delete_memory_by_index_success(self):
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '第一条事实', source='explicit', importance=0.9)
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '第二条事实', source='explicit', importance=0.8)

        # 删除第 1 条
        handled, text = self.say('删除记忆 1')
        self.assertTrue(handled)
        self.assertIn('已为你删除第 1 条记忆', text)
        self.assertIn('第一条事实', text)

        # 再次查看，应该只剩一条，且重新编号为 1
        handled, view_text = self.say('我的记忆')
        self.assertNotIn('第一条事实', view_text)
        self.assertIn('1. 第二条事实', view_text)
        self.assertNotIn('2.', view_text)

    def test_delete_memory_invalid_arguments(self):
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '事实A', source='explicit')

        # 非数字
        handled, text = self.say('删除记忆 abc')
        self.assertTrue(handled)
        self.assertIn('例如「删除记忆 1」', text)

        # 序号越界
        handled, text = self.say('删除记忆 0')
        self.assertTrue(handled)
        self.assertIn('找不到序号为 0', text)

        handled, text = self.say('删除记忆 99')
        self.assertTrue(handled)
        self.assertIn('找不到序号为 99', text)

    def test_delete_memory_user_isolation(self):
        other_uid = '200002'
        self.rt.store.add_user_fact(self.session.as_text(), other_uid, '别人的秘密', source='explicit')

        # 当前用户没有记忆，尝试删除应该提示没有记忆
        handled, text = self.say('删除记忆 1')
        self.assertTrue(handled)
        self.assertIn('没有关于你的记忆', text)

        # 别人的记忆依然存在
        other_facts = self.rt.store.list_user_facts(self.session.as_text(), other_uid)
        self.assertEqual(len(other_facts), 1)

    def test_forget_me_two_step_confirmation(self):
        # 准备数据：事实、称呼、关系好感度
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '喜欢苹果')
        aliases.set_alias(self.rt.store, self.uid, '小道君')
        self.rt.relationship.force_stage(self.uid, 2)

        # 第一次发「忘了我」：应当被拦截，返回警告
        handled, text = self.say('忘了我')
        self.assertTrue(handled)
        self.assertIn('重要确认', text)
        self.assertIn('私聊好感度与关系阶段', text)
        self.assertIn('确认忘了我', text)

        # 数据尚未被删除
        self.assertEqual(aliases.get(self.rt.store, self.uid), '小道君')
        self.assertEqual(self.rt.relationship.load(self.uid).stage, 2)
        facts = self.rt.store.list_user_facts(self.session.as_text(), self.uid)
        self.assertEqual(len(facts), 1)

        # 发送「确认忘了我」：真正执行清空
        handled, text = self.say('确认忘了我')
        self.assertTrue(handled)
        self.assertIn('已彻底重置关于你的全部档案', text)
        self.assertIn('私聊好感度与关系阶段已重置为「陌生」', text)

        # 数据已清空
        self.assertEqual(aliases.get(self.rt.store, self.uid), '')
        self.assertEqual(self.rt.relationship.load(self.uid).stage, 0)
        facts = self.rt.store.list_user_facts(self.session.as_text(), self.uid)
        self.assertEqual(len(facts), 0)

    def test_forget_me_double_call_confirms(self):
        # 连发两次「忘了我」也能成功确认
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '喜欢苹果')
        handled, text1 = self.say('忘了我')
        self.assertIn('重要确认', text1)

        handled, text2 = self.say('忘了我')
        self.assertIn('已彻底重置关于你的全部档案', text2)
        facts = self.rt.store.list_user_facts(self.session.as_text(), self.uid)
        self.assertEqual(len(facts), 0)

    def test_forget_me_timeout_cancels(self):
        self.rt.store.add_user_fact(self.session.as_text(), self.uid, '喜欢苹果')
        self.say('忘了我')
        # 模拟超时（61 秒后）
        confirm_key = f'{self.session.as_text()}:{self.uid}'
        self.rt.commands._pending_forget[confirm_key] = time.time() - 65

        # 再次发「忘了我」，应该重新进入第一步确认警告
        handled, text = self.say('忘了我')
        self.assertIn('重要确认', text)
        # 数据依然未被清空
        facts = self.rt.store.list_user_facts(self.session.as_text(), self.uid)
        self.assertEqual(len(facts), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
