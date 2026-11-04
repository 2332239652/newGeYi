# -*- encoding: utf-8 -*-
"""PRD（伪随机分布）的数学回归。

PRD 是 v2.8.9 主动搭话「暴击起手」的数学底座：连续失败时命中率递增、成功归零。
它只有一个硬指标——**长期命中率必须等于名义值**（不然闭环校准就无从谈起），
以及一个卖点：失败次数的方差比独立掷骰小（尾巴短 = 不会长久干旱/连续暴击）。
"""

from __future__ import annotations

import unittest

from newGeYi.routing import prd


class PrdMathTest(unittest.TestCase):
    def test_known_constants(self):
        """游戏圈流传的那张表：反解出来的 C 必须对得上（小数点后 4 位）。"""
        for rate, known in prd.KNOWN_CONSTANTS.items():
            self.assertAlmostEqual(prd.prd_constant(rate), known, places=4,
                                   msg=f'p={rate} 反解出的 C 不对')

    def test_expected_trials_matches_nominal_rate(self):
        for rate in (0.05, 0.10, 0.25, 0.50, 0.75):
            constant = prd.prd_constant(rate)
            expected = 1.0 / rate
            self.assertAlmostEqual(prd.expected_trials(constant), expected,
                                   delta=expected * 0.01)

    def test_variance_smaller_than_geometric(self):
        """PRD 的全部价值：均值一样，尾巴更短。"""
        for rate in (0.10, 0.25, 0.50):
            constant = prd.prd_constant(rate)
            geometric = (1.0 - rate) / (rate * rate)
            self.assertLess(prd.variance(constant), geometric * 0.75,
                            f'p={rate} 的方差没有明显小于几何分布')

    def test_constant_monotone(self):
        self.assertLess(prd.prd_constant(0.10), prd.prd_constant(0.25))
        self.assertLess(prd.prd_constant(0.25), prd.prd_constant(0.50))
        self.assertEqual(prd.prd_constant(0.0), 1e-6)
        self.assertEqual(prd.prd_constant(1.0), 1.0)

    def test_chance_grows_then_caps(self):
        constant = prd.prd_constant(0.25)
        values = [prd.chance(n, constant) for n in range(0, 14)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in values))
        self.assertAlmostEqual(values[0], constant, places=6)
        self.assertEqual(values[-1], 1.0)          # (n+1)·C 超过 1 就是必中

    def test_hit_accepts_roll(self):
        constant = prd.prd_constant(0.25)          # C ≈ 0.0847
        self.assertTrue(prd.hit(0, constant, roll=0.0))
        self.assertFalse(prd.hit(0, constant, roll=0.5))
        self.assertTrue(prd.hit(11, constant, roll=0.999))   # 12C > 1，必中

    def test_hit_rate_matches_nominal(self):
        """跑一遍真随机：命中频率要贴近名义值（允许 1.5 个百分点误差）。"""
        import random
        rnd = random.Random(20261103)
        rate = 0.25
        constant = prd.prd_constant(rate)
        streak = hits = 0
        trials = 20000
        for _ in range(trials):
            if prd.hit(streak, constant, roll=rnd.random()):
                hits += 1
                streak = 0
            else:
                streak += 1
        actual = hits / float(trials)
        self.assertAlmostEqual(actual, rate, delta=0.015, msg=f'实测命中率 {actual:.3f}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
