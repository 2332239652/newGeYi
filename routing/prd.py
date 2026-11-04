# -*- encoding: utf-8 -*-
"""伪随机分布（PRD, pseudo-random distribution）：把「连着失败」压成有上限的干旱。

来源：《魔兽争霸 3》的暴击/重击。面板写 25%，如果每次独立掷骰，玩家一定会遇到
「连着 12 次不出」这种体感事故。改成

    第 1 次概率 C，第 2 次 2C，第 3 次 3C …… 一旦成功就归零

再把 C 反解成「长期命中率正好等于面板值」，副产品是**失败次数方差小得多**：

    p = 0.50 → C ≈ 0.3021，E[N] = 2.00，Var[N] = 0.66（独立掷骰是 2.00）
    p = 0.25 → C ≈ 0.0847，E[N] = 4.00，Var[N] = 3.60（独立掷骰是 12.0）
    p = 0.10 → C ≈ 0.0148，E[N] = 10.0，Var[N] = 25.6（独立掷骰是 90.0）

也就是说：均值不变、**尾巴变短**。这不只是游戏手感问题，对我们正好是需要的性质。

用在哪（2026-11-03，主动搭话 v2.8.9）：
不是暴击伤害，而是**升级档位的起手**。每「一个周期」她可以连着试 1~3 档提示词
（L0 严格 → L1 中性 → L2 软化），一个周期整个失败算一次「没暴击」。
`hit(S)` 每失败一次就更容易命中，于是：

  · 一直不肯开口的群，一定会被推到软化档（干旱有上限，不会永远不说话）；
  · 刚开口过的群归零，不会连着强推（不吵、也不浪费 token）。

和「按 1/p 固定间隔重试」的区别：那条路的间隔是几何分布，长尾一样难看；
PRD 只在成功时才把计数归零，失败次数被压在均值附近，**产出节奏更稳**。

本模块只做纯数学，不碰 IO、不读写状态，方便单测。
"""

from __future__ import annotations

import random

# 已知反解值（游戏圈里流传的那张表），单测拿它对照
KNOWN_CONSTANTS = {
    0.50: 0.30210,
    0.25: 0.08474,
    0.20: 0.05570,
    0.15: 0.03222,
    0.10: 0.01475,
    0.05: 0.00380,
}


def expected_trials(constant: float, limit: int = 400) -> float:
    """在这个 C 下，第一次成功平均要几次（E[N]）。

    用生存函数展开，不用模拟：E[N] = Σ_{n≥0} P(N > n)，
    P(N > n) = Π_{k=1..n} (1 - min(1, k·C))。
    """
    c = float(constant)
    if c <= 0:
        return float(limit)
    if c >= 1:
        return 1.0
    product = 1.0
    total = 1.0                      # n = 0 那一项，恒为 1
    for k in range(1, max(2, limit)):
        product *= max(0.0, 1.0 - k * c)
        total += product
        if product < 1e-12:
            break
    return total


def prd_constant(rate: float, low: float = 1e-6, high: float = 1.0,
                 rounds: int = 90) -> float:
    """反解 C：让长期命中率 ≈ `rate`（即 E[N] ≈ 1/rate）。

    E[N] 对 C 单调递减，直接二分。`rate` 越接近 1，C 越接近 1
    （rate=1 时必然是每次命中）。
    """
    try:
        p = float(rate)
    except (TypeError, ValueError):
        return high
    if p <= 0:
        return low
    if p >= 1:
        return high
    want = 1.0 / p
    lo, hi = float(low), float(high)
    for _ in range(max(8, rounds)):
        mid = (lo + hi) / 2.0
        if expected_trials(mid) > want:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def chance(streak: int, constant: float) -> float:
    """第 `streak + 1` 次的命中概率 = min(1, (streak+1)·C)。

    `streak` = 已经连续失败的次数（0 表示还没失败过 = 第一次尝试）。
    """
    n = int(streak) + 1
    if n <= 0:
        n = 1
    return max(0.0, min(1.0, n * float(constant)))


def hit(streak: int, constant: float, roll: float | None = None) -> bool:
    """掷一次：这次算不算「暴击」。`roll` 传进来是为了单测可复现。"""
    value = random.random() if roll is None else float(roll)
    return value < chance(streak, constant)


def variance(constant: float, limit: int = 400) -> float:
    """N 的方差，给单测/文档对照用（PRD 的核心卖点就是它比几何分布小）。"""
    c = float(constant)
    if c <= 0:
        return float(limit) ** 2
    product = 1.0
    total = 0.0
    for n in range(1, max(2, limit)):
        product_prev = product
        product *= max(0.0, 1.0 - n * c)
        # P(N = n) = P(N>n-1) - P(N>n)
        total += n * n * (product_prev - product)
        if product < 1e-12:
            break
    mean = expected_trials(c, limit)
    return max(0.0, total - mean * mean)


def describe(rate: float) -> str:
    """一行人类可读的解释，日志/文档用。"""
    c = prd_constant(rate)
    return '名义 %.2f → C=%.5f，平均每 %.2f 次命中一次' % (rate, c, expected_trials(c))
