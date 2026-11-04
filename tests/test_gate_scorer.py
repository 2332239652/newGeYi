# -*- encoding: utf-8 -*-
"""主动搭话调度的回归测试（离线、无网络、不碰线上状态）。

历史：2026-11-02 之前是「每群 180~720s 冷却 + 全局分位阈值」，产出率从
**0.75 句/百条**（111 条/小时的大群）到 **18.9 句/百条**（12 条/天的小群）——
节奏按时间算、目标按消息量算，群越大越哑。

v2.8.7 改成 `required = (100/目标) × accept` 的产出闭环。机制是对的，但 accept
量的是**模型服从度**：同一段代码 deepseek 79%、gemini-low 17%、gemini-tiered 9%。
实测 11-02 22:20~11-03 12:40：尝试 13.67 次/百条、产出 0.75 句/百条、token 涨 4 倍。

v2.8.9 把「什么时候花这次钱」和「这次要多强硬」拆开：

    欠账    D += λ / 条消息，开口 -1；λ = 目标率/100 ÷ conv
    升级    一个周期内 L0 → L1 → L2 逐级加强，最多 3 次调用
    暴击    周期起手用几档，由干旱计数经 PRD 决定（routing/prd.py）
    自愈    某档实测没用（样本够、接受率低）就砍掉

这个文件钉的就是：产出率只看目标率和群消息量、**不看模型脸色**，
以及「每句主动搭话要调几次模型」必须显著低于 1/p。
"""

from __future__ import annotations

import math
import os
import random
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_gate_tests_'))

from newGeYi.config import DEFAULT_CONFIG  # noqa: E402
from newGeYi.routing.attention import AttentionGate  # noqa: E402
from newGeYi.routing.likelihood import Features  # noqa: E402
from newGeYi.storage.database import SQLiteStore  # noqa: E402

T0 = 1700000000


def make_gate(**overrides):
    store = SQLiteStore(os.path.join(tempfile.mkdtemp(prefix='geyi_gate_'), 'g.sqlite3'))
    config = dict(DEFAULT_CONFIG)
    config.update({
        'active_enabled': True,
        'active_shadow': False,
        'active_quiet_hours': '',
        'daily_call_budget': 999999,
        'group_quota_enabled': False,
        'gate_scorer_enabled': True,
    })
    config.update(overrides)
    return AttentionGate(store, config)


def feed(gate, session, n, start=T0, step=30, features=None, responded=False):
    """喂 n 条群消息，返回每次 evaluate 的决策列表。"""
    decisions = []
    for i in range(n):
        now = start + i * step
        gate.observe_message(session, responded=responded, now=now)
        decisions.append(gate.evaluate(session, now=now, features=features or Features()))
    return decisions


def messages_for_cycle(gate, session='s1') -> int:
    return int(math.ceil(gate.cycle_messages(session)))


def run_cycle(gate, session, now, probs, rnd):
    """模拟「一个周期」：按计划逐级调模型，返回 (是否开口, 调用次数, 用掉的档位)。"""
    plan = gate.pending_plan(session)
    calls = 0
    for level in plan.levels:
        calls += 1
        if rnd.random() < probs.get(level, probs.get(0, 0.0)):
            gate.note_attempt(session, level, True, 'stub', now=now)
            gate.end_cycle(session, True)
            return True, calls, level
        gate.note_attempt(session, level, False, 'stub', now=now)
    gate.end_cycle(session, False)
    return False, calls, -1


def simulate(gate, session, total, probs, seed=7, step=30, features=None):
    """喂 total 条消息，返回 (开口数, 调用次数, 总条数)。"""
    rnd = random.Random(seed)
    speaks = calls = 0
    for i in range(total):
        now = T0 + i * step
        gate.observe_message(session, responded=False, now=now)
        decision = gate.evaluate(session, now=now, features=features or Features(recent_count_5m=3))
        if not decision.allow:
            continue
        gate.note_evaluated(session, now)
        spoke, used, _level = run_cycle(gate, session, now, probs, rnd)
        calls += used
        speaks += 1 if spoke else 0
    return speaks, calls, total


class DeficitScheduleTest(unittest.TestCase):
    def test_not_enough_deficit_is_blocked(self):
        gate = make_gate()
        gate.observe_message('s1', responded=False, now=T0)
        decision = gate.evaluate('s1', now=T0, features=Features())
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, 'rate')

    def test_cycle_messages_matches_formula(self):
        gate = make_gate()
        target = float(DEFAULT_CONFIG['active_rate_target_per_100'])
        conv = float(DEFAULT_CONFIG['active_conv_init'])
        expected = (100.0 / target) * conv          # gain 初值 1.0
        self.assertAlmostEqual(gate.cycle_messages('s1'), expected, places=4)
        # 目标 3 句/百条、兑现率初值 0.15（宁可低估）→ 每 5 条消息一个周期，然后自己收敛
        self.assertGreater(gate.cycle_messages('s1'), 4)
        self.assertLess(gate.cycle_messages('s1'), 6)

    def test_cycle_spacing_is_bounded(self):
        """再极端也不许密到「每 1 条消息试一次」，也不许稀疏到几年不搭话。"""
        gate = make_gate(active_conv_min=0.001, active_rate_gain_max=99.0)
        self.assertGreaterEqual(gate.cycle_messages('s1'), 3.0 - 1e-6)
        gate2 = make_gate(active_rate_gain_min=0.001)
        gate2._set('active_cyc_n', 's1', 2000)
        gate2._set('active_cyc_s', 's1', 2000)       # conv ≈ 1.0
        gate2._set('active_rate_gain', 's1', 0.001)
        self.assertLessEqual(gate2.cycle_messages('s1'), 400.0 + 1e-6)

    def test_passes_once_deficit_filled_then_resets(self):
        gate = make_gate()
        need = messages_for_cycle(gate)
        decisions = feed(gate, 's1', need + 2, step=30)
        passed = [d for d in decisions if d.allow]
        self.assertEqual(len(passed), 1, [(d.allow, d.reason) for d in decisions])
        # 通过之后欠账扣 1、计数清零：紧接着的下一条又不够
        info = gate.rate_info('s1')
        self.assertLess(info['deficit'], 1.0)
        self.assertLessEqual(info['counter'], 2)

    def test_deficit_is_capped(self):
        """死群攒了一堆账也不许一次连发。"""
        gate = make_gate()
        for i in range(400):
            gate.observe_message('s1', responded=False, now=T0 + i)
        self.assertLessEqual(gate.deficit('s1'), float(DEFAULT_CONFIG['active_deficit_cap']))

    def test_lower_conversion_shortens_interval(self):
        """兑现率越低，周期越密（产量回到目标率）。"""
        gate = make_gate()
        before = gate.cycle_messages('s1')
        for _ in range(8):
            gate._bump_conversion('s1', False)
        after = gate.cycle_messages('s1')
        self.assertLess(after, before)
        floor = float(DEFAULT_CONFIG['active_conv_min'])
        target = float(DEFAULT_CONFIG['active_rate_target_per_100'])
        self.assertGreaterEqual(after, (100.0 / target) * floor - 1e-6)

    def test_higher_conversion_lengthens_interval(self):
        gate = make_gate()
        before = gate.cycle_messages('s1')
        for _ in range(6):
            gate._bump_conversion('s1', True)
        self.assertGreater(gate.cycle_messages('s1'), before)

    def test_min_gap_blocks_burst(self):
        gate = make_gate(active_rate_min_gap=45)
        gate._add_deficit('s1', 3.0)
        gate.note_evaluated('s1', T0)
        decision = gate.evaluate('s1', now=T0 + 5, features=Features())
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, 'gap')

    def test_attempt_cap_per_10min(self):
        gate = make_gate(active_rate_max_attempts_10min=2, active_rate_min_gap=0,
                         active_base_cooldown=0, active_min_self_gap=0)
        gate._add_deficit('s1', 3.0)
        gate.note_attempt('s1', 0, False, 'stub', now=T0 - 10)
        gate.note_attempt('s1', 0, False, 'stub', now=T0 - 20)
        decision = gate.evaluate('s1', now=T0, features=Features())
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, 'attempt_cap')


class ScoreFloorTest(unittest.TestCase):
    """打分从「配额」退成「噪声地板」：只挡明显不该接的，且**不许动兑现率**。"""

    def test_floor_blocks_hopeless_message(self):
        gate = make_gate()
        gate._add_deficit('s1', 2.0)
        decision = gate.evaluate(
            's1', now=T0,
            features=Features(media_only=True, at_other=True, recent_count_5m=200))
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, 'probability')

    def test_floor_rejection_does_not_touch_conversion(self):
        """v2.8.7 的真 bug：本地挡掉一条也记一次「模型没开口」，
        accept 越挡越小 → 间隔越算越短 → 尝试率跑到 13.7 次/百条。"""
        gate = make_gate()
        gate._add_deficit('s1', 2.0)
        before_conv = gate.conversion('s1')
        before_deficit = gate.deficit('s1')
        for _ in range(5):
            gate.evaluate('s1', now=T0,
                          features=Features(media_only=True, at_other=True, recent_count_5m=200))
        self.assertAlmostEqual(gate.conversion('s1'), before_conv, places=4)
        self.assertAlmostEqual(gate.deficit('s1'), before_deficit, places=4)

    def test_quantile_off_by_default(self):
        gate = make_gate()
        self.assertEqual(gate.gate_summary('s1')['keep'], 1.0)


class RateGainTest(unittest.TestCase):
    """外环：每 `active_update_messages` 条消息按**实际产出**修正，不让护栏偷走产量。

    它是安全网，不是主力：主力是 conv（模型服从度）。回放里真正要命的不是护栏，
    而是 conv 初值给太高（0.70）导致「周期太稀 → 样本太少 → conv 收敛不了 →
    周期还是太稀」的死循环——所以 `active_conv_init` 现在默认 0.15（宁可多试）。
    """

    def test_gain_rises_when_window_produces_nothing(self):
        gate = make_gate()
        for i in range(int(DEFAULT_CONFIG['active_update_messages'])):
            gate.observe_message('s1', responded=False, now=T0 + i)
        self.assertGreater(gate.rate_gain('s1'), 1.0)

    def test_gain_falls_when_window_is_too_chatty(self):
        gate = make_gate()
        for i in range(100):
            gate.observe_message('s1', responded=False, now=T0 + i)
            if i % 12 == 0:
                gate.end_cycle('s1', True)             # 一窗口里说了 9 句，远超目标 3 句
        self.assertLess(gate.rate_gain('s1'), 1.0)

    def test_gain_is_clamped(self):
        gate = make_gate()
        for _ in range(int(DEFAULT_CONFIG['active_update_messages']) * 20):
            gate.observe_message('s1', responded=False, now=T0)
        self.assertLessEqual(gate.rate_gain('s1'),
                             float(DEFAULT_CONFIG['active_rate_gain_max']) + 1e-9)

    def test_gain_shortens_interval(self):
        """外环起来了，周期就变密（欠账攒得更快）——这是它唯一的作用方式。"""
        gate = make_gate()
        # 构造 conv = 0.30：(11.4 + 0.15×4) / (36 + 4) = 0.30
        gate._set('active_cyc_n', 's1', 36)
        gate._set('active_cyc_s', 's1', 11.4)
        before = gate.cycle_messages('s1')
        gate._set('active_rate_gain', 's1', 2.0)
        after = gate.cycle_messages('s1')
        self.assertLess(after, before)
        self.assertAlmostEqual(after, before / 2.0, places=4)


class EscalationLadderTest(unittest.TestCase):
    """升级档位：暴击起手 / 逐级加强 / 没用的档位自动下架。"""

    def test_default_plan_is_three_levels(self):
        gate = make_gate(active_prd_crit_rate=0.01)
        plan = gate.cycle_plan('s1')
        self.assertEqual(plan.levels, (0, 1, 2))
        self.assertEqual(plan.start_level, 0)

    def test_crit_starts_higher(self):
        gate = make_gate(active_prd_crit_rate=1.0, active_prd_strong_at=4)
        self.assertEqual(gate.cycle_plan('s1').start_level, 1)
        gate._set('active_drought', 's1', 4)
        self.assertEqual(gate.cycle_plan('s1').start_level, 2)
        self.assertEqual(gate.cycle_plan('s1').levels, (2,))

    def test_burst_can_be_disabled(self):
        gate = make_gate(active_burst_max_attempts=1, active_prd_crit_rate=1.0)
        self.assertEqual(gate.cycle_plan('s1').levels, (0,))

    def test_useless_level_is_trimmed(self):
        """L1 试了 80 次一次都没成 → 判定白花钱，不再升级到它（也就不再升到 L2）。"""
        gate = make_gate()
        for _ in range(80):
            gate.note_attempt('s1', 1, False, 'stub')
        self.assertLess(gate.level_upper_bound(1),
                        float(DEFAULT_CONFIG['active_level_min_accept']))
        self.assertEqual(gate.effective_levels(), (0,))

    def test_trim_needs_evidence(self):
        """20 次里只成了 1 次也**不许**判死：样本少时必误杀（第一版就这么把落地率打回 2.0）。"""
        gate = make_gate()
        for _ in range(19):
            gate.note_attempt('s1', 1, False, 'stub')
        gate.note_attempt('s1', 1, True, 'stub')
        self.assertEqual(gate.effective_levels(), (0, 1, 2))

    def test_good_level_is_kept(self):
        gate = make_gate()
        for _ in range(80):
            gate.note_attempt('s1', 1, True, 'stub')
        gate.note_attempt('s1', 2, True, 'stub')
        self.assertEqual(gate.effective_levels(), (0, 1, 2))

    def test_level_acceptance_is_per_level(self):
        gate = make_gate()
        for _ in range(4):
            gate.note_attempt('s1', 0, False, 'stub')
            gate.note_attempt('s1', 2, True, 'stub')
        self.assertGreater(gate.level_accept(2), gate.level_accept(0))
        self.assertEqual(gate.level_samples(0), 4)
        self.assertEqual(gate.level_speaks(2), 4)


class ClosedLoopSimulationTest(unittest.TestCase):
    """把闭环跑起来：喂消息 → 按各档接受率决定说/不说 → 产出率应当收敛到目标。"""

    # 实测口径（11-01~11-03）：tiered 9% / low 17% / deepseek 79%
    PICKY = {0: 0.09, 1: 0.25, 2: 0.55}
    CHATTY = {0: 0.35, 1: 0.50, 2: 0.70}
    HOPELESS = {0: 0.09, 1: 0.09, 2: 0.09}

    def test_converges_near_target_for_picky_model(self):
        gate = make_gate()
        speaks, calls, total = simulate(gate, 's1', 1200, self.PICKY)
        rate = 100.0 * speaks / total
        self.assertGreater(rate, 2.0, f'产出率 {rate:.2f} 太低（speaks={speaks} calls={calls}）')
        self.assertLess(rate, 4.5, f'产出率 {rate:.2f} 太高（speaks={speaks} calls={calls}）')

    def test_cost_per_speak_is_far_below_one_over_p(self):
        """核心：p=9% 的模型，旧机制要 11 次调用换 1 句，现在只要个位数。"""
        gate = make_gate()
        speaks, calls, _total = simulate(gate, 's2', 1200, self.PICKY)
        per_speak = calls / float(max(1, speaks))
        self.assertLess(per_speak, 6.0, f'每句 {per_speak:.1f} 次调用，太贵')
        self.assertGreater(per_speak, 1.5)

    def test_different_models_land_on_the_same_rate(self):
        """不同服从度的模型，落地率必须接近（成本可以不同）。"""
        picky = simulate(make_gate(), 's3', 1200, self.PICKY, seed=11)
        chatty = simulate(make_gate(), 's4', 1200, self.CHATTY, seed=12)
        picky_rate = 100.0 * picky[0] / picky[2]
        chatty_rate = 100.0 * chatty[0] / chatty[2]
        self.assertLess(abs(picky_rate - chatty_rate), 1.3,
                        f'不同模型落地率差太多：{picky_rate:.2f} vs {chatty_rate:.2f}')

    def test_rate_is_independent_of_message_volume(self):
        slow = simulate(make_gate(), 's5', 400, self.PICKY, step=600)
        fast = simulate(make_gate(), 's6', 1600, self.PICKY, step=20)
        slow_rate = 100.0 * slow[0] / slow[2]
        fast_rate = 100.0 * fast[0] / fast[2]
        self.assertLess(abs(slow_rate - fast_rate), 1.3,
                        f'群活跃度不该改变产出率：安静 {slow_rate:.2f} vs 热闹 {fast_rate:.2f}')

    def test_hopeless_prompt_costs_the_same_as_single_level(self):
        """软化完全无效时，升级阶梯**省不了钱**——这是个诚实的物理下限：

        各档接受率都等于 p 时，无论阶梯多长，每次开口的调用数都 ≈ 1/p。
        阶梯真正省钱的前提是「更软的提示词真的更容易说服她」（见上面
        `test_cost_per_speak_is_far_below_one_over_p`）。
        """
        gate = make_gate()
        speaks, calls, total = simulate(gate, 's7', 1200, self.HOPELESS)
        rate = 100.0 * speaks / total
        per_speak = calls / float(max(1, speaks))
        self.assertLess(per_speak, 1.0 / self.HOPELESS[0] * 1.25,
                        f'每句 {per_speak:.1f} 次调用，比 1/p 还差')
        self.assertGreater(rate, 2.0)
        self.assertLess(rate, 4.5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
