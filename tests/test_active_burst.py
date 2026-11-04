# -*- encoding: utf-8 -*-
"""主动搭话「升级重试」在 process_job 里的集成测试（离线、假模型、不碰线上状态）。

锁住的是 v2.8.9 的核心改动：一次闸门放行 → **最多 3 次调用**逐级加强，
而不是「被拒了就等下一轮」。三条不变量：

1. L0 被拒 → 立刻用 L1 重试（同一段上下文），L2 同理；
2. 任何一档点头就停，不再多花一次调用；
3. 网络/额度失败**不算**「她不想说」——立刻收手（不能拿故障去喂档位统计）。
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_burst_tests_')
os.environ.setdefault('GEYI_DATA_DIR', _SCRATCH)

from newGeYi.models import IncomingMessage, ModelResult, SessionKey  # noqa: E402

SKIP = '【歌伊忽略】'
SESSION = SessionKey(bot_hash='hash', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='20002')


def make_msg(seq=9):
    sent = []

    def send(*args, **kwargs):
        sent.append(args)

    event = types.SimpleNamespace(send=send)
    msg = IncomingMessage(
        session=SESSION, message_id=str(seq), sender_id='30003', sender_name='群友',
        text='你们在聊什么呢', raw_text='你们在聊什么呢', is_group=True,
        mentioned_bot=False, event=event, proc=None, arrival_seq=seq, sender_is_bot=False,
    )
    return msg, sent


def level_of(req) -> int:
    """从 system 的**最后一段**判断这次用的是哪一档。"""
    system = str(req.messages[0]['content'])
    tail = system[-400:]
    if '很想开口' in tail:
        return 2
    if '大多数群聊消息都不需要你出现' in tail:
        return 0
    return 1


class ActiveBurstTest(unittest.TestCase):
    def setUp(self):
        from newGeYi.runtime.app import Runtime

        self.rt = Runtime()
        self.rt.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')
        # 把「暴击起手」压到几乎不可能：PRD 本来有 8.5% 的概率直接从 L1 开始，
        # 断言 [0,1,2] 就变成掷骰子（整套跑的时候偶发失败过一次）。
        self.rt.config['active_prd_crit_rate'] = 0.001
        self.calls = []
        # 档位计数是**全局**的（跨会话共享），所以断言一律看增量，不看绝对值
        gate = self.rt.attention
        self.before = [(gate.level_samples(l), gate.level_speaks(l)) for l in (0, 1, 2)]

    def _delta(self, level):
        gate = self.rt.attention
        base_n, base_s = self.before[level]
        return gate.level_samples(level) - base_n, gate.level_speaks(level) - base_s

    def _stub(self, speak_at, error_at=None):
        def fake_chat(req, kind='chat', session_key=''):
            level = level_of(req)
            self.calls.append((kind, level))
            if error_at is not None and len(self.calls) >= error_at:
                return ModelResult(ok=False, model='stub', error_code='connection_error')
            text = '（歪头）这句我接一下～' if level >= speak_at else SKIP
            return ModelResult(ok=True, text=text, model='stub',
                               input_tokens=9000, output_tokens=20, latency_ms=5)
        self.rt.gateway.chat = fake_chat

    def test_escalates_until_spoken(self):
        self._stub(speak_at=2)
        msg, sent = make_msg()
        self.rt.process_job(msg, 'active')
        self.assertEqual([level for _kind, level in self.calls], [0, 1, 2])
        self.assertEqual(len(sent), 1, '开口了就该发出去')
        gate = self.rt.attention
        key = SESSION.as_text()
        self.assertEqual(self._delta(0), (1, 0))
        self.assertEqual(self._delta(1), (1, 0))
        self.assertEqual(self._delta(2), (1, 1))
        # 周期兑现了：干旱归零、兑现率计数 s+1
        self.assertEqual(gate.rate_info(key)['drought'], 0)
        self.assertEqual(gate._get_float('active_cyc_s', key), 1)

    def test_stops_at_first_yes(self):
        self._stub(speak_at=0)
        msg, sent = make_msg()
        self.rt.process_job(msg, 'active')
        self.assertEqual([level for _kind, level in self.calls], [0])
        self.assertEqual(len(sent), 1)

    def test_all_levels_refused_end_cycle(self):
        self._stub(speak_at=99)
        msg, sent = make_msg()
        self.rt.process_job(msg, 'active')
        self.assertEqual([level for _kind, level in self.calls], [0, 1, 2])
        self.assertEqual(sent, [])
        gate = self.rt.attention
        key = SESSION.as_text()
        self.assertEqual(gate.rate_info(key)['drought'], 1)
        self.assertEqual(gate._get_float('active_cyc_n', key), 1)
        self.assertEqual(gate._get_float('active_cyc_s', key), 0)

    def test_network_error_aborts_escalation(self):
        self._stub(speak_at=99, error_at=2)
        msg, sent = make_msg()
        self.rt.process_job(msg, 'active')
        # 第二次就报错：不该继续试 L2，也不该把这次算成「她拒了」
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(sent, [])
        gate = self.rt.attention
        self.assertEqual(self._delta(2)[0], 0, 'L2 根本没被调用，不该有样本')
        self.assertEqual(gate.rate_info(SESSION.as_text())['drought'], 1)

    def test_burst_can_be_limited_to_one_attempt(self):
        self.rt.config = dict(self.rt.config)
        self.rt.config['active_burst_max_attempts'] = 1
        self.rt.attention.config = self.rt.config
        self._stub(speak_at=99)
        msg, sent = make_msg()
        self.rt.process_job(msg, 'active')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
