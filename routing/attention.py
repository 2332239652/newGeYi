# -*- encoding: utf-8 -*-
"""主动搭话决策（Attention Gate）。

旧实现是「每条群消息掷一次 2% 骰子」——把「要不要说话」变成了与内容无关的骰子。
调研里把它列为头号反模式：群里 90% 以上的消息根本不该接，而这个决策的质量
直接决定她是「群友」还是「噪音源」。

现在拆成四道闸门，**从最便宜的开始，前三道零成本**：

  ① 时机闸 —— 她刚说过话吗？（说完马上又插一句会连成两段，很假）
  ② 频率闸 —— 距上次评估够久吗？**冷却按她自己的发言频率自适应拉长**：
              她在这个群说得越多，冷却越长（她已经融进去了，不需要再主动找话）；
              她越安静，冷却越短（主动开口这时才真的有价值）。
  ③ 总量闸 + 退避闸 —— 今天说够了吗？连着几次主动都没人接话吗？
  ④ 质量闸 —— 模型判断「此刻值不值得说」，只有①②③全过了才花 token。

四道全过才调模型；模型回「没什么好说的」就记一条 observe，不发出去。

**2026-11-03 起多了一层「升级」**：①②③过了只是「允许花这一次钱」，真正开口要由
模型点头。可模型的点头率是三成到九成不等的（同一段代码实测差 9 倍），所以现在
不是「被拒就等下一轮」，而是**同一个周期里逐级加强硬提示词，最多三次**（见下面
「欠账 + 暴击升级」一节）。钱花在「把她从沉默里推出来」上，而不是反复掷骰。

影子模式（默认开）下模型照调、决策照记，但消息**不真发**——先观察一两天再开真发。
"""

from __future__ import annotations

import collections
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field

from . import prd
from .likelihood import (Features, explain, resolve_threshold, score,
                         target_ratio, weights_from_config)

logger = logging.getLogger('newGeYi.gate')

# 闸门代号：日志、统计、观察命令都引用这里，别在别处写字面量
REASONS = {
    'disabled': '主动搭话总开关关着',
    'group_off': '本群主动搭话关着',
    'quiet_hours': '安静时段',
    'stopped': '连续没人接话太多次，已停止主动（发「本群主动开」可重置）',
    'backoff': '连续没人接话，冷却中',
    'daily_cap': '今天主动次数用完了',
    'budget': '今日调用总预算用完了',
    'quota': '本群今日额度用完了（按群人数算的）',
    'rate': '欠账还没攒够（消息条数不够）',
    'gap': '距上次尝试太近',
    'attempt_cap': '10 分钟内尝试次数到顶了（成本护栏）',
    'cooldown': '冷却未到',
    'self_gap': '她刚说过话，别紧接着又插一句',
    'probability': '概率没中',
    'model_declined': '模型觉得此刻没什么好说的',
}

# 三档基础概率：接自己的话要比自选发言高得多
DEFAULT_TIER_PROBABILITY = {2: 0.60, 3: 0.30}


def _midnight(now: float) -> int:
    local = time.localtime(now)
    return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)))


def _parse_window(raw: str) -> tuple:
    """把 '01:00-08:00' 解析成 (起始分钟, 结束分钟)；解析不了返回 (-1, -1)。"""
    try:
        left, right = str(raw or '').split('-', 1)
        start_h, start_m = left.strip().split(':')
        end_h, end_m = right.strip().split(':')
        return int(start_h) * 60 + int(start_m), int(end_h) * 60 + int(end_m)
    except (ValueError, AttributeError):
        return -1, -1


@dataclass
class GateResult:
    allow: bool
    reason: str = ''
    tier: int = 3
    probability: float = 0.0
    cooldown: int = 0
    strikes: int = 0
    notes: list = field(default_factory=list)
    # 本次尝试的升级计划（只有 allow=True 才有意义）
    start_level: int = 0
    levels: tuple = ()

    @property
    def reason_text(self) -> str:
        return REASONS.get(self.reason, self.reason)


@dataclass
class CyclePlan:
    """一个「周期」要试哪几档提示词。

    周期 = 欠账攒够 1 次之后的一次兑现尝试。最多 `active_burst_max_attempts` 次调用，
    每被拒一次升一级；整个周期都失败就算一次「干旱」，下一轮更容易暴击起手。
    """

    start_level: int = 0
    levels: tuple = (0,)
    drought: int = 0
    crit: bool = False
    deficit: float = 0.0

    @property
    def attempts(self) -> int:
        return len(self.levels)

    def text(self) -> str:
        return 'L%s%s' % ('→'.join(str(l) for l in self.levels),
                          '（暴击起手）' if self.crit else '')


class AttentionGate:
    def __init__(self, store, config: dict):
        self.store = store
        self.config = config
        self._last_tier: dict = {}
        # 每群一个分数窗口（2026-11-02 改）：以前是**全局一个** deque，
        # 热闹的群把阈值顶高、安静群跟着遭殃 —— 这是「不同群概率不同」的一半原因。
        self._recent_scores: dict[str, collections.deque] = {}
        # 每群最近的尝试时间戳（10 分钟成本护栏用；内存即可，重载丢了不影响正确性）
        self._attempts: dict[str, list] = {}
        # 本轮允许后待执行的升级计划（router 决定 → scheduler 执行，中间不重新掷骰）
        self._plans: dict[str, CyclePlan] = {}
        self._prd_cache: dict = {}
        self._trim_logged: set = set()
        self._score_pass = 0
        self._score_deny = 0

    # ==================== 设置读写 ====================
    @staticmethod
    def _key(name: str, session_key: str) -> str:
        return f'{name}:{session_key}'

    def _get_float(self, name: str, session_key: str, default: float = 0.0) -> float:
        raw = self.store.get_setting(self._key(name, session_key), default)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _set(self, name: str, session_key: str, value) -> None:
        self.store.set_setting(self._key(name, session_key), str(value))

    # ==================== 单条闸门 ====================
    def in_quiet_hours(self, now: float = 0) -> bool:
        start, end = _parse_window(self.config.get('active_quiet_hours', ''))
        if start < 0:
            return False
        local = time.localtime(now or time.time())
        minute = local.tm_hour * 60 + local.tm_min
        if start <= end:
            return start <= minute < end
        return minute >= start or minute < end      # 跨零点

    def self_factor(self, session_key: str, now: float = 0) -> tuple:
        """她的发言频率系数：最近说得越多，冷却越长。返回 (系数, 条数)。

        2026-11-02：加了上限（默认 2.0）。以前最高 4 倍，配上 180s 基础冷却就是
        12 分钟不能说 —— 而 `count_recent_assistant_msgs` 把**被动回复**也算进去了，
        于是"越有人跟她说话，她越不能主动开口"，这是反的。
        """
        now = now or time.time()
        window = float(self.config.get('active_self_window', 1800))
        count = self.store.count_recent_assistant_msgs(session_key, int(now - window))
        cap = float(self.config.get('active_self_factor_cap', 2.0) or 2.0)
        for threshold, factor in ((10, 4.0), (6, 2.5), (3, 1.5)):
            if count >= threshold:
                return min(factor, cap), count
        return 1.0, count

    def cooldown_seconds(self, session_key: str, now: float = 0) -> int:
        base = float(self.config.get('active_base_cooldown', 15))
        factor, _count = self.self_factor(session_key, now)
        return int(base * factor)

    def tier_of(self, session_key: str, now: float = 0) -> int:
        """2 = 接着她自己刚说的话；3 = 群里自己聊（跟她无关）。"""
        now = now or time.time()
        last_self = self._get_float('last_self_talk', session_key)
        if last_self and now - last_self <= float(self.config.get('active_tier2_seconds', 180)):
            return 2
        return 3

    def probability_of(self, tier: int, session_key: str) -> float:
        """分层概率 + 升级公式：一直没轮上说话，想插话的意愿会累积。

        ⚠️ 只在 `evaluate(features=None)` 时才走到（v2.8.9 起路由层总是传 features，
        所以线上走的是打分闸门）。留着是为了幂等回退和单测，别在它上面加新机制——
        真正的"升级"已经是提示词档位（`cycle_plan`），两者叠加会互相打架。
        """
        table = self.config.get('active_tier_probability') or {}
        base = table.get(str(tier), table.get(tier, DEFAULT_TIER_PROBABILITY.get(tier, 0.30)))
        try:
            base = float(base)
        except (TypeError, ValueError):
            base = DEFAULT_TIER_PROBABILITY.get(tier, 0.30)
        misses = self._get_float('active_miss', session_key)
        cap_n = float(self.config.get('active_escalation_max', 8))
        n = min(misses, cap_n)
        escalated = 1 - (1 - base) ** (n + 1)       # n=0 时正好是 base
        cap = float(self.config.get('active_escalation_cap', 0.98))
        # 上限只压「升级出来的部分」：阈值本来就配得比上限高时不该被反而调低
        return min(escalated, max(cap, base))

    # ============ 欠账调度 + 暴击升级（2026-11-03，v2.8.9）============
    # 目标还是「每 100 条群友消息 ≈ 3 句主动搭话，每群都一样」；变的是**靠什么达到**。
    #
    # v2.8.7 的闭环（required = (100/目标) × accept）机制没错，错在 accept 是模型的
    # 「服从度」：同一段代码，deepseek-flash 79%、gemini-low 17%、gemini-tiered 9%。
    # 实测 11-02 22:20 ~ 11-03 12:40（14.4 小时）：尝试 13.67 次/百条、产出 0.75 句/百条、
    # 输入 token 7.11M/天（baseline 1.77M）——**钱多花四倍，产量还掉了**。
    # 还有一个真 bug：打分地板挡掉一条消息也记一次「没开口」，accept 越挡越小、
    # 间隔越算越短，正反馈跑飞（accept 被压到 0.056，正好撞上 0.08 下限）。
    #
    # 现在把两件事拆开——模型只决定后者：
    #   ① 要不要现在花这次钱 → 欠账（deficit），纯记账，跟模型无关
    #   ② 这次要用几级强度才肯开口 → 升级（L0 严格 / L1 中性 / L2 软化）
    #
    #   欠账 D：每条群消息 += λ，开口一次 -= 1，夹在 [0, cap]
    #           λ = 目标率/100 ÷ conv    （conv = 周期兑现率，EWMA）
    #           ⇒ 产出 = λ × conv = 目标率，**与模型服从度无关**
    #   升级 ladder：一个周期内最多 3 次调用、同一段上下文，每被拒一次升一级
    #   干旱 S：整个周期都失败 → S+1；开口 → S=0
    #   暴击：周期**起手**用哪一级，由 S 经过伪随机分布（PRD）决定
    #         p = min(1, (S+1)×C)，C 由名义暴击率反解（见 routing/prd.py）
    #         → 一直不肯开口的群一定会被推到软化档；刚说过话的群不会被强推
    #   自愈：某一级实测接受率低于 floor（样本够）就砍掉，不为「没用的一次升级」付钱
    #
    # 为什么这样省钱（p0=9% 的 tiered，p1/p2 按 25%/55% 估）：
    #   旧：每次尝试独立掷骰 → 1/0.09 ≈ 11 次调用换 1 句
    #   新：L0 拒 → L1 拒 → L2 说，兑现率 69%、平均 2.6 次调用 → 3.7 次/句（省 66%）
    #   最坏（软化完全无效，p1=p2=9%）：兑现率 24.6%、7.8 次/句，仍省 29%；
    #   而且不会像 v2.8.7 那样为了追产量把尝试率推到 13.7 次/百条。
    def _target_per_100(self) -> float:
        try:
            return max(0.0, float(self.config.get('active_rate_target_per_100', 3.0) or 0))
        except (TypeError, ValueError):
            return 3.0

    # ---------- 周期兑现率 ----------
    def conversion(self, session_key: str) -> float:
        """一个周期能把「欠账」兑现成一句话的概率。

        用**计数比 + Beta 先验**，不用 EWMA：λ = 目标/conv 是「除以一个估计值」，
        而 E[1/x] ≥ 1/E[x]——EWMA 的随机起伏会被这条凸性翻译成系统性偏高，
        实测回放里落地率稳定超出目标 11%（3.34 对 3.0）。计数比没有这个偏差，
        而且冷启动由先验兜着（`active_conv_prior` 个「假观测」）。
        """
        cfg = self.config
        init = float(cfg.get('active_conv_init', 0.15) or 0.15)
        low = float(cfg.get('active_conv_min', 0.08) or 0.0)
        high = float(cfg.get('active_conv_max', 1.0) or 1.0)
        n = self._get_float('active_cyc_n', session_key)
        s = self._get_float('active_cyc_s', session_key)
        prior = float(cfg.get('active_conv_prior', 1.0) or 0.0)
        total = n + prior
        value = ((s + init * prior) / total) if total > 0 else init
        return max(low, min(high, value))

    def _bump_conversion(self, session_key: str, success: bool) -> None:
        """记一个周期的结果；窗口满了就整体减半（遗忘），别一辈子被历史绑住。"""
        n = self._get_float('active_cyc_n', session_key) + 1.0
        s = self._get_float('active_cyc_s', session_key) + (1.0 if success else 0.0)
        span = float(self.config.get('active_conv_window', 40) or 0.0)
        if span > 0 and n >= span:
            n *= 0.5
            s *= 0.5
        self._set('active_cyc_n', session_key, round(n, 3))
        self._set('active_cyc_s', session_key, round(s, 3))

    # ---------- 欠账 ----------
    def deficit(self, session_key: str) -> float:
        return self._get_float('active_deficit', session_key)

    def rate_gain(self, session_key: str) -> float:
        """外环修正系数：弥补「闸门吃掉的机会」，让**实际产出**贴住目标。

        为什么还要外环（conv 已经在内环修模型服从度了）：conv 只看得到「一个周期
        兑现没有」，看不到计划外损耗——最小间隔、10 分钟上限、打分地板、日上限。
        这些损耗**与模型无关**，却会系统性地把产出压到目标以下。
        2026-11-03 的真日志回放证明了这一点：burst=1、p=9% 时 conv 收敛到 0.09（正确），
        但落地只有 1.38 句/百条而不是 3.0，差额全被间隔和成本护栏吃掉了。

        外环每 `active_update_messages` 条消息结算一次「实际产出 / 目标」，乘性慢修正。
        它同时也是「换模型后频率不变」的最后一道保险：模型换了、梯子变了、
        护栏位置变了，只要产出偏离目标，这个系数就会把它拉回来。
        """
        default = 1.0
        value = self._get_float('active_rate_gain', session_key, default)
        low = float(self.config.get('active_rate_gain_min', 0.25) or 0.0)
        high = float(self.config.get('active_rate_gain_max', 4.0) or 1.0)
        return max(low, min(high, value or default))

    def _roll_rate_window(self, session_key: str, msgs: int = 0, speaks: int = 0) -> None:
        """按**消息条数**滚动结算实际产出率，修正 gain。

        窗口按消息数而不是时间：目标本身就是「每 100 条」，用时间窗口会让安静的群
        永远结算不了，也永远修不准。
        """
        cfg = self.config
        win_msgs = int(self._get_float('active_win_msgs', session_key)) + int(msgs)
        win_speaks = int(self._get_float('active_win_speaks', session_key)) + int(speaks)
        self._set('active_win_msgs', session_key, win_msgs)
        if speaks:
            self._set('active_win_speaks', session_key, win_speaks)
        span = int(cfg.get('active_update_messages', 100) or 0)
        target = self._target_per_100()
        if span <= 0 or target <= 0 or win_msgs < span:
            return
        realized = 100.0 * win_speaks / float(win_msgs)
        gain = self.rate_gain(session_key)
        # 死区：偏多少以内不动。否则 realized 的随机起伏会被 `ratio ** expo` 的凸性
        # 翻译成单向漂移（控制器不该对噪声出反应）；但死区太宽会让系统停在区间边缘
        # （实测 30% 死区 → 落地 3.52 而不是 3.0），所以默认给 8%。
        band = float(cfg.get('active_gain_deadband', 0.08) or 0.0)
        if band > 0 and abs(realized - target) <= target * band:
            self._set('active_win_msgs', session_key, 0)
            self._set('active_win_speaks', session_key, 0)
            return
        if realized <= 0:
            gain *= float(cfg.get('active_gain_step', 1.6) or 1.6)
        else:
            ratio = max(0.2, min(5.0, target / realized))
            gain *= ratio ** float(cfg.get('active_gain_expo', 0.6) or 0.6)
        gain = max(float(cfg.get('active_rate_gain_min', 0.25) or 0.0),
                   min(float(cfg.get('active_rate_gain_max', 4.0) or 1.0), gain))
        self._set('active_rate_gain', session_key, round(gain, 4))
        self._set('active_win_msgs', session_key, 0)
        self._set('active_win_speaks', session_key, 0)
        logger.info('主动搭话外环结算 | %s | 最近 %d 条实际 %.2f 句/百条（目标 %g）'
                    '→ 修正系数 %.2f', session_key[-16:], span, realized, target, gain)

    def deficit_lambda(self, session_key: str) -> float:
        """每条群消息攒多少欠账 = 目标率/100 ÷ 兑现率 × 外环系数。"""
        target = self._target_per_100()
        if target <= 0:
            return 0.0
        lam = ((target / 100.0) / max(0.05, self.conversion(session_key))
               * self.rate_gain(session_key))
        # 夹住：最密 3 条消息一个周期；最稀 400 条（安静的群也不会几年不搭话）
        lam = min(lam, 1.0 / 3.0)
        lam = max(lam, 1.0 / 400.0)
        return lam

    def cycle_messages(self, session_key: str) -> float:
        """平均多少条群消息攒够一个周期（命令/日志用）。"""
        lam = self.deficit_lambda(session_key)
        return (1.0 / lam) if lam > 0 else 0.0

    def required_messages(self, session_key: str) -> float:
        """兼容旧名：v2.8.7 的 required，现在等于「一个周期消耗多少条消息」。"""
        return self.cycle_messages(session_key)

    def _add_deficit(self, session_key: str, amount: float) -> None:
        try:
            cap = float(self.config.get('active_deficit_cap', 1.0) or 1.0)
        except (TypeError, ValueError):
            cap = 1.0
        value = max(0.0, min(cap, self.deficit(session_key) + float(amount)))
        self._set('active_deficit', session_key, round(value, 4))

    def _spend_deficit(self, session_key: str) -> None:
        self._add_deficit(session_key, -1.0)

    # ---------- 档位与暴击 ----------
    def _level_counter(self, key: str) -> int:
        try:
            return int(float(self.store.get_setting(key) or 0))
        except (TypeError, ValueError):
            return 0

    def level_samples(self, level: int) -> int:
        return self._level_counter('active_level_n%d' % int(level))

    def level_speaks(self, level: int) -> int:
        return self._level_counter('active_level_s%d' % int(level))

    def level_accept(self, level: int) -> float:
        raw = self.store.get_setting('active_level_p%d' % int(level))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value <= 0:
            return float(self.config.get('active_level_init_accept', 0.50) or 0.50)
        return value

    def level_upper_bound(self, level: int, z: float = 1.64) -> float:
        """这一档接受率的 Wilson 90% 上界（样本少时接近 1，不敢下结论）。

        为什么不用 EWMA 直接跟地板比：样本 20 次、真值 0.25 时，EWMA 很容易晃到
        0.08 以下，于是「误差判死」把一个有用的档位砍掉——实测第一版就这样把
        落地率从 3.0 打回 2.0。上界法要求**即使乐观估计也够差**才动手。
        """
        n = self.level_samples(level)
        if n <= 0:
            return 1.0
        p = self.level_speaks(level) / float(n)
        denom = 1.0 + z * z / n
        center = p + z * z / (2.0 * n)
        spread = z * math.sqrt(max(0.0, p * (1.0 - p) / n + z * z / (4.0 * n * n)))
        return min(1.0, (center + spread) / denom)

    def prd_constant(self) -> float:
        try:
            rate = float(self.config.get('active_prd_crit_rate', 0.25) or 0.25)
        except (TypeError, ValueError):
            rate = 0.25
        rate = max(0.01, min(0.99, rate))
        if self._prd_cache.get('rate') != rate:
            self._prd_cache = {'rate': rate, 'c': prd.prd_constant(rate)}
        return self._prd_cache['c']

    def effective_levels(self) -> tuple:
        """这一轮允许用哪几档。软化档实测不管用（样本够了）就砍掉。"""
        cfg = self.config
        try:
            hard = int(cfg.get('active_burst_max_attempts', 3) or 1)
        except (TypeError, ValueError):
            hard = 1
        hard = max(1, min(3, hard)) - 1
        if hard < 1 or not cfg.get('active_escalate_enabled', True):
            return (0,)
        min_samples = int(cfg.get('active_level_min_samples', 60) or 0)
        floor = float(cfg.get('active_level_min_accept', 0.05) or 0.0)
        levels = [0]
        for level in range(1, hard + 1):
            if (self.level_samples(level) >= min_samples
                    and self.level_upper_bound(level) < floor):
                if level not in self._trim_logged:
                    self._trim_logged.add(level)
                    logger.warning(
                        '主动搭话 L%d 的接受率上界 %.3f（%d 次样本）仍低于 %.2f，'
                        '判定这一档白花钱，不再升级到它',
                        level, self.level_upper_bound(level), self.level_samples(level), floor)
                break
            levels.append(level)
        return tuple(levels)

    def cycle_plan(self, session_key: str, now: float = 0) -> CyclePlan:
        """决定这个周期从哪一级起手、最多走到哪一级。"""
        levels = self.effective_levels()
        drought = int(self._get_float('active_drought', session_key))
        crit = prd.hit(drought, self.prd_constant())
        start = 0
        if crit and len(levels) > 1:
            start = levels[1]
            strong_at = int(self.config.get('active_prd_strong_at', 4) or 0)
            if drought >= strong_at and len(levels) > 2:
                start = levels[2]
        return CyclePlan(
            start_level=start,
            levels=tuple(l for l in levels if l >= start),
            drought=drought,
            crit=bool(crit),
            deficit=round(self.deficit(session_key), 3),
        )

    def note_attempt(self, session_key: str, level: int, spoke: bool,
                     model: str = '', now: float = 0) -> None:
        """记一次真实模型调用的结果（周期里每一次都记；10 分钟成本护栏也数在这里）。"""
        now = now or time.time()
        level = max(0, min(2, int(level)))
        self._attempts.setdefault(session_key, []).append(now)
        n_key = 'active_level_n%d' % level
        self.store.set_setting(n_key, str(self._level_counter(n_key) + 1))
        if spoke:
            s_key = 'active_level_s%d' % level
            self.store.set_setting(s_key, str(self._level_counter(s_key) + 1))
        alpha = float(self.config.get('active_level_alpha', 0.25) or 0.25)
        old = self.level_accept(level)
        new = old + alpha * ((1.0 if spoke else 0.0) - old)
        self.store.set_setting('active_level_p%d' % level, '%.4f' % new)

    def end_cycle(self, session_key: str, spoke: bool) -> None:
        """一个周期结束：更新兑现率，以及「连着几个周期没兑现」（干旱计数）。"""
        self._bump_conversion(session_key, spoke)
        cycles = int(self._get_float('active_cycles', session_key)) + 1
        self._set('active_cycles', session_key, cycles)
        if spoke:
            self._set('active_drought', session_key, 0)
            # 外环：记一次真实产出（消息数在 observe_message 里已经记过）
            self._roll_rate_window(session_key, speaks=1)
        else:
            self._set('active_drought', session_key,
                      int(self._get_float('active_drought', session_key)) + 1)

    def rate_info(self, session_key: str) -> dict:
        """给命令/日志用的一行调度状态。"""
        return {
            'target_per_100': self._target_per_100(),
            'conv': round(self.conversion(session_key), 3),
            'gain': round(self.rate_gain(session_key), 2),
            'required': round(self.cycle_messages(session_key), 1),
            'counter': int(self._get_float('active_msg_counter', session_key)),
            'deficit': round(self.deficit(session_key), 2),
            'drought': int(self._get_float('active_drought', session_key)),
            'levels': list(self.effective_levels()),
        }

    def _gate_open(self, session_key: str, now: float) -> tuple[bool, str]:
        """欠账够不够 + 最小间隔 + 10 分钟尝试上限。"""
        cfg = self.config
        if self._target_per_100() > 0 and self.deficit(session_key) < 1.0:
            return False, 'rate'
        gap = max(float(cfg.get('active_rate_min_gap', 15) or 0),
                  float(self.cooldown_seconds(session_key, now)))
        last_attempt = self._get_float('last_active_eval', session_key)
        if gap > 0 and last_attempt and now - last_attempt < gap:
            return False, 'gap'
        cap = int(cfg.get('active_rate_max_attempts_10min', 40) or 0)
        if cap > 0:
            recent = [t for t in self._attempts.get(session_key, []) if now - t < 600]
            self._attempts[session_key] = recent
            if len(recent) >= cap:
                return False, 'attempt_cap'
        return True, ''

    def _consume_attempt(self, session_key: str, now: float) -> None:
        """放行一个周期：清零消息计数、扣掉一次欠账。

        欠账在**周期开始时**就扣，失败也不退——退了会在模型连续拒绝时立刻再发一轮，
        变成「同一分钟连试 3 轮」。少掉的产量由兑现率 conv 反向补偿。
        """
        self._set('active_msg_counter', session_key, 0)
        self._spend_deficit(session_key)

    def _rate_allows(self, session_key: str, now: float) -> tuple[bool, str]:
        """兼容旧名（v2.8.7 的调用点/测试）。"""
        return self._gate_open(session_key, now)

    def _score_allows(self, session_key: str, features: Features,
                      probability_factor: float) -> tuple[bool, float]:
        """打分只当「噪声地板」用，不再当配额用。

        为什么：闭环控制的是**产出**，如果打分再按分位数砍一刀，闭环会为了够产量
        而把尝试次数翻倍（钱花在被打分挡掉的调用上）。所以默认 `gate_quantile_keep=1.0`
        （不砍），只留一个绝对地板 `gate_min_score`，把「纯图 / @别人 / 窗口很热闹」
        这种明显不该接的挡在本地。
        """
        cfg = self.config
        weights = weights_from_config(cfg)
        probability = score(features, weights) * max(0.0, min(1.0, probability_factor))
        size = int(cfg.get('gate_score_window', 80) or 80)
        window = self._recent_scores.setdefault(
            session_key, collections.deque(maxlen=max(8, size)))
        window.append(probability)
        floor = float(cfg.get('gate_min_score', 0.05) or 0.0)
        keep = float(cfg.get('gate_quantile_keep', 1.0) or 0.0)
        threshold = floor
        if 0.0 < keep < 1.0:
            threshold = max(floor, resolve_threshold(list(window), keep))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('打分 %.3f 阈值 %.3f（keep=%.2f）%s',
                         probability, threshold, keep, explain(features, weights))
        return probability >= threshold, probability

    # ==================== 主流程 ====================

    def evaluate(self, session_key: str, now: float = 0,
                 probability_factor: float = 1.0,
                 features: Features | None = None) -> GateResult:
        """走一遍四道闸门。

        `probability_factor` 给调用方按消息类型调权：纯表情包不该和「有人找你说话」
        同权——群里刷图是常态，等权就会变成见图就评（线上实测）。

        `features` 是打分闸门要的现场特征：传了它就**不掷骰子**，改用 8 维逻辑回归
        算一个概率，和预算反解出来的阈值比大小（纯算术，不发模型请求）。
        """
        cfg = self.config
        now = now or time.time()

        if not cfg.get('active_enabled', True):
            return GateResult(False, 'disabled')
        if self.store.get_setting(f'group_active:{session_key}') == '0':
            return GateResult(False, 'group_off')
        if self.in_quiet_hours(now):
            return self._log_denied(session_key, GateResult(False, 'quiet_hours'), now)
        # 全局日预算：主动搭话不该把当天的调用额度吃光（被动回复优先）
        try:
            today = self.store.today_stats()
            if today['calls'] >= int(cfg.get('daily_call_budget', 999999)):
                return self._log_denied(session_key, GateResult(False, 'budget'), now)
        except Exception:  # noqa: BLE001 - 统计读不到不该阻断主动搭话
            pass
        # 本群额度（按群人数算）：小群被刷爆时先在这里挡住，别等全局预算被吃光
        profile = getattr(self, 'group_profile', None)
        if profile is not None:
            try:
                ok, _why = profile.allow(session_key)
                if not ok:
                    return self._log_denied(session_key, GateResult(False, 'quota'), now)
            except Exception:  # noqa: BLE001
                pass
        # 先结算上一次主动到底有没有人接话，退避判定要用最新结果
        self.settle_pending(session_key, now)
        if self._get_float('active_stopped', session_key):
            return self._log_denied(session_key, GateResult(False, 'stopped'), now)
        until = self._get_float('active_backoff_until', session_key)
        if until:
            if now < until:
                return self._log_denied(session_key, GateResult(False, 'backoff'), now)
            else:
                # 退避期已顺利度过：自动清空退避与冷场惩罚
                self._set('active_backoff_until', session_key, 0)
                self._set('active_nr', session_key, 0)
        # 超过 8 小时无主动搭话，自动清空历史连击计数，保持每天/每时段全新开始
        last_eval = self._get_float('last_active_eval', session_key)
        if last_eval and (now - last_eval > 28800):
            self._set('active_nr', session_key, 0)
            self._set('active_backoff_until', session_key, 0)
        spoken = self.store.count_active_spoken(session_key, _midnight(now))
        cap = int(cfg.get('active_daily_cap', 320))
        if spoken >= cap:
            return self._log_denied(session_key, GateResult(False, 'daily_cap'), now)
        # 欠账够不够（按消息条数攒，取代固定冷却）
        allowed, why = self._gate_open(session_key, now)
        if not allowed:
            result = GateResult(False, why, cooldown=self.cooldown_seconds(session_key, now))
            return self._log_denied(session_key, result, now)
        last_self = self._get_float('last_self_talk', session_key)
        gap = float(cfg.get('active_min_self_gap', 45))
        if last_self and now - last_self < gap:
            return GateResult(False, 'self_gap')
        tier = self.tier_of(session_key, now)
        if features is not None and cfg.get('gate_scorer_enabled', True):
            passed, probability = self._score_allows(session_key, features, probability_factor)
            if not passed:
                self._score_deny += 1
                self._set('active_miss', session_key, self._get_float('active_miss', session_key) + 1)
                # 注意：这里**不**动兑现率 conv。v2.8.7 把「本地挡掉」也记成「模型没开口」，
                # 于是越挡越像模型挑食、间隔越算越短，正反馈把尝试率推到 13.7 次/百条。
                result = GateResult(False, 'probability', tier=tier, probability=probability)
                return self._log_denied(session_key, result, now)
            self._score_pass += 1
        else:
            probability = self.probability_of(tier, session_key) * max(0.0, min(1.0, probability_factor))
            if random.random() >= probability:
                self._set('active_miss', session_key, self._get_float('active_miss', session_key) + 1)
                result = GateResult(False, 'probability', tier=tier, probability=probability)
                return self._log_denied(session_key, result, now)
        self._consume_attempt(session_key, now)
        self._last_tier[session_key] = tier
        plan = self.cycle_plan(session_key, now)
        # 计划只在这里生成一次：router 放行 → scheduler 稍后执行，中间不允许重新掷骰，
        # 否则「暴击起手」会被掷两次，实际强度就不受控了。
        self._plans[session_key] = plan
        return GateResult(True, 'pass', tier=tier, probability=probability,
                          cooldown=self.cooldown_seconds(session_key, now),
                          strikes=int(self._get_float('active_nr', session_key)),
                          start_level=plan.start_level, levels=plan.levels)

    def gate_summary(self, session_key: str = '') -> dict:
        """给「歌伊决策 / 歌伊状态」看：样本数、最近分数、闭环状态、今日过/拦。"""
        cfg = self.config
        if session_key:
            scores = list(self._recent_scores.get(session_key, ()))
        else:
            scores = [s for window in self._recent_scores.values() for s in window]
        keep = float(cfg.get('gate_quantile_keep', 1.0) or 0.0)
        floor = float(cfg.get('gate_min_score', 0.05) or 0.0)
        threshold = floor
        if 0.0 < keep < 1.0:
            threshold = max(floor, resolve_threshold(scores, keep))
        info = {
            'samples': len(scores),
            'latest': scores[-1] if scores else None,
            'threshold': threshold,
            'floor': floor,
            'keep': keep,
            # 兼容老字段：老口径的「每批放行比例」，现在是参考值不是开关
            'ratio': target_ratio(cfg.get('gate_replies_per_hour', 6),
                                  cfg.get('gate_batches_per_hour', 60)),
            'passed': self._score_pass,
            'denied': self._score_deny,
        }
        info.update(self.rate_info(session_key or next(iter(self._recent_scores), '')))
        return info

    def last_tier(self, session_key: str) -> int:
        return int(self._last_tier.get(session_key, 3))

    def record_model(self, session_key: str, action: str, detail: str = '',
                     latency_ms: int = 0, level: int = -1) -> int:
        """记一条模型决策（speak / observe）。返回行号，真发出去后再升级成 sent。

        `level` 是这次用的升级档位（-1 表示不适用）。它写进 detail 前缀，
        因为 active_decisions 的 tier 列有别的含义（2=接自己的话 / 3=群里自说），
        共用一个列会让「哪一档更管用」再也算不出来。
        """
        reason = 'model_declined' if action == 'observe' else ''
        text = str(detail or '')
        if level >= 0:
            text = 'L%d %s' % (int(level), text)
        # 注意：观察/发话都**不**在这里动兑现率——一个周期可能有 3 次调用，
        # 兑现率按「周期」结算（end_cycle），按调用结算会把它算成 3 倍分母。
        return self.store.add_active_decision(
            session_key, stage='model', action=action, reason=reason,
            tier=self.last_tier(session_key), detail=text[:480], latency_ms=latency_ms,
        )

    def pending_plan(self, session_key: str) -> CyclePlan:
        """取走 router 放行时定下的升级计划。

        进程刚好在这中间重载时内存里没有计划，就按当前干旱状态重新定一次
        （`cycle_plan` 只看已有状态，不会重复扣欠账）。
        """
        return self._plans.pop(session_key, None) or self.cycle_plan(session_key)

    # ==================== 事件回调 ====================
    def note_evaluated(self, session_key: str, now: float = 0) -> None:
        """该群通过了闸门、真的要去调模型了——冷却从这一刻开始算。"""
        self._set('last_active_eval', session_key, now or time.time())

    def note_spoken(self, session_key: str, now: float = 0) -> None:
        """她说了话（主动被动都算）。自适应冷却的依据，同时清掉升级计数。"""
        now = now or time.time()
        self._set('last_self_talk', session_key, now)
        self._set('active_miss', session_key, 0)

    def note_active_sent(self, session_key: str, now: float = 0) -> None:
        """主动消息发出去了，挂一条待结算：接下来这段时间有没有人接话。

        兑现率/干旱已经由 app 在「模型点头」那一刻结过账（end_cycle），这里不重复结算。
        """
        now = now or time.time()
        self._set('last_active_sent', session_key, now)
        self.store.set_setting(self._key('active_pending', session_key),
                               json.dumps({'ts': now, 'responded': False, 'msg_count': 0}))

    def observe_message(self, session_key: str, responded: bool, now: float = 0) -> str:
        """每收到一条群消息都调一次：标记「有人接话了」，或到期结算退避。

        返回结算结果（'responded' / 'strike_N' / 'backoff_Nm' / ''）。
        """
        now = now or time.time()

        # 欠账的计费点：**每条群消息 += λ**（私聊不会进来，调用点在 router 里判过 is_group）
        self._set('active_msg_counter', session_key,
                  self._get_float('active_msg_counter', session_key) + 1)
        self._add_deficit(session_key, self.deficit_lambda(session_key))
        self._roll_rate_window(session_key, msgs=1)

        # 任何时候只要群里有针对歌伊的发言（被@ 或 触发词），立即解除退避与冷场惩罚！
        if responded:
            self._set('active_nr', session_key, 0)
            self._set('active_backoff_until', session_key, 0)
            self._set('active_stopped', session_key, 0)

        raw = self.store.get_setting(self._key('active_pending', session_key))
        if raw:
            try:
                data = json.loads(str(raw))
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                # 记录歌伊主动发言后的群友后续消息条数
                data['msg_count'] = int(data.get('msg_count', 0)) + 1
                # 只要有人接话，或者群里后续有至少 2 条消息（证明群活跃，没有冷场冷掉），即算成功响应
                if responded or data['msg_count'] >= 2:
                    data['responded'] = True
                self.store.set_setting(self._key('active_pending', session_key),
                                       json.dumps(data))
        return self.settle_pending(session_key, now)

    def settle_pending(self, session_key: str, now: float = 0) -> str:
        """观察窗口到期后结算：有人接话就清零，没人接就记一次并可能退避。"""
        now = now or time.time()
        key = self._key('active_pending', session_key)
        raw = self.store.get_setting(key)
        if not raw:
            return ''
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        ts = float(data.get('ts') or 0)
        # 观察窗口：默认 5 分钟 (300s)
        window = float(self.config.get('active_no_response_window', 600))
        if now - ts < window:
            return ''                                # 还在观察窗口里，等有人接话
        self.store.set_setting(key, '')
        if data.get('responded'):
            self._set('active_nr', session_key, 0)
            self._set('active_backoff_until', session_key, 0)
            return 'responded'
        strikes = int(self._get_float('active_nr', session_key)) + 1
        self._set('active_nr', session_key, strikes)
        return self._apply_backoff(session_key, strikes, now)

    def _apply_backoff(self, session_key: str, strikes: int, now: float) -> str:
        # 人性化梯级退避（以分钟/小时为单位，不再动辄拉黑 3 天 / 14 天）：
        # 连冷 1~2 次：无需惩罚，走正常基础冷却
        # 连冷 3 次：休眠 30 分钟 (1800s)
        # 连冷 4 次：休眠 1 小时 (3600s)
        # 连冷 5 次：休眠 2 小时 (7200s)
        # 连冷 >= 6 次：休眠 4 小时 (14400s)
        # 绝不永久停用（stopped），避免大群因偶发冷场被彻底封印
        seconds = 0
        if strikes >= 6:
            seconds = 14400
        elif strikes == 5:
            seconds = 7200
        elif strikes == 4:
            seconds = 3600
        elif strikes == 3:
            seconds = 1800
        if seconds:
            self._set('active_backoff_until', session_key, now + seconds)
            return f'backoff_{seconds // 60}m'
        return f'strike_{strikes}'

    def reset_group(self, session_key: str) -> None:
        """主人手工重置：清退避、清连击、解停止，把欠账与干旱放回初始状态。"""
        for name in ('active_nr', 'active_backoff_until', 'active_stopped', 'active_miss',
                     'active_msg_counter', 'active_deficit', 'active_drought',
                     'active_win_msgs', 'active_win_speaks', 'active_rate_gain',
                     'active_cyc_n', 'active_cyc_s'):
            self._set(name, session_key, 0)
        self.store.set_setting(self._key('active_accept', session_key), '')
        self.store.set_setting(self._key('active_conv', session_key), '')
        self._attempts.pop(session_key, None)
        self._plans.pop(session_key, None)
        self.store.set_setting(self._key('active_pending', session_key), '')

    # ==================== 日志与统计 ====================
    def _log_denied(self, session_key: str, result: GateResult, now: float) -> GateResult:
        """被闸门挡掉也记一条——但按「原因变化或超过间隔」节流，否则日志会被刷屏。

        日志是拿来回答「她被哪道闸门挡了多少次」的，不是拿来记录每条群消息的。
        """
        interval = float(self.config.get('active_log_interval', 300))
        last_ts = self._get_float('active_log_ts', session_key)
        last_reason = str(self.store.get_setting(self._key('active_log_reason', session_key)) or '')
        if result.reason == last_reason and now - last_ts < interval:
            return result
        self._set('active_log_ts', session_key, now)
        self.store.set_setting(self._key('active_log_reason', session_key), result.reason)
        self.store.add_active_decision(
            session_key, stage='gate', action='blocked', reason=result.reason,
            tier=result.tier,
        )
        return result

    def stats_text(self, session_key: str, hours: float = 24) -> str:
        now = time.time()
        since = int(now - hours * 3600)
        stats = self.store.active_decision_stats(session_key, since)
        by_action = stats['by_action']
        spoken = self.store.count_active_spoken(session_key, _midnight(now))
        factor, recent = self.self_factor(session_key, now)
        cooldown = self.cooldown_seconds(session_key, now)
        strikes = int(self._get_float('active_nr', session_key))
        until = self._get_float('active_backoff_until', session_key)
        rate = self.rate_info(session_key)
        lines = [
            '--- 主动搭话观察（近 %g 小时）---' % hours,
            '模式: %s' % ('影子（只记录不真发）' if self.config.get('active_shadow', True) else '真发'),
            '今天已主动 %d/%s 条 | 连续无回应 %d 次'
            % (spoken, self.config.get('active_daily_cap', 320), strikes),
            '欠账调度: 目标 %g 句/百条 | 周期兑现率 %.2f × 外环修正 %.2f → 每 %.1f 条消息一个周期'
            '（当前欠账 %.2f、干旱 %d 个周期）'
            % (rate['target_per_100'], rate['conv'], rate['gain'], rate['required'],
               rate['deficit'], rate['drought']),
            '升级档位: %s | L0 %.2f(%d) / L1 %.2f(%d) / L2 %.2f(%d)'
            % ('→'.join('L%d' % l for l in rate['levels']),
               self.level_accept(0), self.level_samples(0),
               self.level_accept(1), self.level_samples(1),
               self.level_accept(2), self.level_samples(2)),
            '最小间隔 %d 秒（基础 %s 秒 × 她发言系数 %g；最近 %g 分钟她说了 %d 条）'
            % (cooldown, int(float(self.config.get('active_base_cooldown', 15))),
               factor, float(self.config.get('active_self_window', 1800)) / 60, recent),
        ]
        if until and until > now:
            lines.append('退避中，还需 %.1f 小时' % ((until - now) / 3600))
        if self._get_float('active_stopped', session_key):
            lines.append('已停止主动（发「本群主动开」可重置）')
        lines.append('')
        lines.append('近 %g 小时共记录 %d 条决策：' % (hours, stats['total']))
        action_label = {'blocked': '被闸门挡下', 'speak': '决定说话', 'observe': '看过没说'}
        if not by_action:
            lines.append('  （还没攒到记录）')
        for action, count in sorted(by_action.items(), key=lambda kv: -kv[1]):
            lines.append('  %s: %d 次' % (action_label.get(action, action), count))
        if stats['by_reason']:
            lines.append('')
            lines.append('被挡原因分布：')
            for reason, count in sorted(stats['by_reason'].items(), key=lambda kv: -kv[1])[:8]:
                lines.append('  %s: %d 次' % (REASONS.get(reason, reason), count))
        return '\n'.join(lines)
