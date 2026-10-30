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
影子模式（默认开）下模型照调、决策照记，但消息**不真发**——先观察一两天再开真发。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field

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

    @property
    def reason_text(self) -> str:
        return REASONS.get(self.reason, self.reason)


class AttentionGate:
    def __init__(self, store, config: dict):
        self.store = store
        self.config = config
        self._last_tier: dict = {}

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
        """她的发言频率系数：最近说得越多，冷却越长。返回 (系数, 条数)。"""
        now = now or time.time()
        window = float(self.config.get('active_self_window', 1800))
        count = self.store.count_recent_assistant_msgs(session_key, int(now - window))
        for threshold, factor in ((10, 4.0), (6, 2.5), (3, 1.5)):
            if count >= threshold:
                return factor, count
        return 1.0, count

    def cooldown_seconds(self, session_key: str, now: float = 0) -> int:
        base = float(self.config.get('active_base_cooldown', 180))
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
        """分层概率 + 升级公式：一直没轮上说话，想插话的意愿会累积。"""
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

    # ==================== 主流程 ====================
    def evaluate(self, session_key: str, now: float = 0,
                 probability_factor: float = 1.0) -> GateResult:
        """走一遍四道闸门。allow=True 才轮到调模型（第四道闸门）。

        `probability_factor` 给调用方按消息类型调权：纯表情包不该和「有人找你说话」
        同权——群里刷图是常态，等权就会变成见图就评（线上实测）。
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
        cap = int(cfg.get('active_daily_cap', 60))
        if spoken >= cap:
            return self._log_denied(session_key, GateResult(False, 'daily_cap'), now)
        cooldown = self.cooldown_seconds(session_key, now)
        last_eval = self._get_float('last_active_eval', session_key)
        if last_eval and now - last_eval < cooldown:
            result = GateResult(False, 'cooldown', cooldown=cooldown)
            return self._log_denied(session_key, result, now)
        last_self = self._get_float('last_self_talk', session_key)
        gap = float(cfg.get('active_min_self_gap', 45))
        if last_self and now - last_self < gap:
            return GateResult(False, 'self_gap')
        tier = self.tier_of(session_key, now)
        probability = self.probability_of(tier, session_key) * max(0.0, min(1.0, probability_factor))
        if random.random() >= probability:
            self._set('active_miss', session_key, self._get_float('active_miss', session_key) + 1)
            result = GateResult(False, 'probability', tier=tier, probability=probability)
            return self._log_denied(session_key, result, now)
        self._last_tier[session_key] = tier
        return GateResult(True, 'pass', tier=tier, probability=probability, cooldown=cooldown,
                          strikes=int(self._get_float('active_nr', session_key)))

    def last_tier(self, session_key: str) -> int:
        return int(self._last_tier.get(session_key, 3))

    def record_model(self, session_key: str, action: str, detail: str = '',
                     latency_ms: int = 0) -> int:
        """记一条模型决策（speak / observe）。返回行号，真发出去后再升级成 sent。"""
        reason = 'model_declined' if action == 'observe' else ''
        return self.store.add_active_decision(
            session_key, stage='model', action=action, reason=reason,
            tier=self.last_tier(session_key), detail=detail, latency_ms=latency_ms,
        )

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
        """主动消息发出去了，挂一条待结算：接下来这段时间有没有人接话。"""
        now = now or time.time()
        self._set('last_active_sent', session_key, now)
        self.store.set_setting(self._key('active_pending', session_key),
                               json.dumps({'ts': now, 'responded': False, 'msg_count': 0}))

    def observe_message(self, session_key: str, responded: bool, now: float = 0) -> str:
        """每收到一条群消息都调一次：标记「有人接话了」，或到期结算。

        返回结算结果（'responded' / 'strike_N' / 'backoff_Nm' / ''）。
        """
        now = now or time.time()

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
        """主人手工重置：清退避、清连击、解停止。"""
        for name in ('active_nr', 'active_backoff_until', 'active_stopped', 'active_miss'):
            self._set(name, session_key, 0)
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
        lines = [
            '--- 主动搭话观察（近 %g 小时）---' % hours,
            '模式: %s' % ('影子（只记录不真发）' if self.config.get('active_shadow', True) else '真发'),
            '今天已主动 %d/%s 条 | 连续无回应 %d 次'
            % (spoken, self.config.get('active_daily_cap', 60), strikes),
            '当前冷却 %d 分钟（基础 %s 分钟 × 她发言系数 %g；最近 %g 分钟她说了 %d 条）'
            % (cooldown // 60, int(float(self.config.get('active_base_cooldown', 180)) // 60),
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
