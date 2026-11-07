# -*- encoding: utf-8 -*-
"""好感度状态机：**纯代码，不碰数据库、不碰模型**。

所有"难度"都在这里，评审（LLM）只能给建议，最终加多少、能不能升级由这里说了算：

- 单条加减分夹紧；每日加分上限；同类行为当日递减；高阶段边际收益更低；
- 升级需要同时满足：分数达标 + 有效天数达标 + （4/5 阶段）羁绊事件完成；每天最多升一级；
  天数或事件没到时，分数最多停在下一档门槛，不能"存分"；
- 降级很难：分数有下限，且只有持续的恶劣行为（bad_streak）才会让阶段掉一级；
- 连续 pushy / 超前 / 刷屏会进入冷却（locked_until），冷却期内不加分。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .stages import ALLOWED_ADVANCE, MAX_STAGE

# 视为"恶劣"的标签：连续出现会触发冷却/降级
BAD_TAGS = frozenset({'rude', 'pushy', 'flirt_early', 'spam', 'lie'})
ALL_TAGS = frozenset({'care', 'share', 'humor', 'curious_lore', 'respect', 'neutral',
                      'presence', 'event', 'judge_fail', 'overstep'} | BAD_TAGS)


@dataclass
class Params:
    thresholds: tuple = (0, 15, 50, 110, 190, 280)
    min_days: tuple = (0, 1, 3, 7, 14, 21)
    daily_cap: tuple = (12, 12, 12, 10, 9, 8)
    stage_factor: tuple = (1.0, 1.0, 0.9, 0.8, 0.7, 0.6)
    delta_min: int = -3
    # 2026-11-06：单次上限从 2 提到 3。评审给 1~3 分（1=日常有来有往，2=明显用心，
    # 3=真的走进心里、一天最多一两次），这里只做保险夹紧。
    delta_max: int = 3
    # 同一个标签每天最多记几次分。改成 2 之后「第二次不计分」的递减整段删掉：
    # 两次各自按自己的 delta 算（分开计算），第三次起才归零。
    tag_daily_limit: int = 2
    max_affection: float = 300.0
    presence_bonus: int = 1
    lock_hours: float = 4.0
    bad_streak_lock: int = 3
    demote_margin: float = 25.0
    event_stages: dict = field(default_factory=lambda: {4: 'A', 5: 'B'})
    event_timeout_turns: int = 6
    event_bonus: float = 3.0


def params_from_config(cfg: dict | None) -> Params:
    """从配置读取可调项；缺省/非法一律回退默认值（改坏配置不能让系统失控）。"""
    p = Params()
    cfg = cfg or {}

    def seq(key, current, cast):
        raw = cfg.get(key)
        if isinstance(raw, (list, tuple)) and len(raw) == len(current):
            try:
                return tuple(cast(x) for x in raw)
            except (TypeError, ValueError):
                return current
        return current

    p.thresholds = seq('galgame_thresholds', p.thresholds, float)
    p.min_days = seq('galgame_min_days', p.min_days, int)
    p.daily_cap = seq('galgame_daily_cap', p.daily_cap, float)
    # 同一个标签每天记几次分：只接受 1~5 的正常整数，脏值一律回默认
    try:
        limit = int(cfg.get('galgame_tag_daily_limit', p.tag_daily_limit))
    except (TypeError, ValueError):
        limit = p.tag_daily_limit
    p.tag_daily_limit = limit if 1 <= limit <= 5 else Params().tag_daily_limit
    # 阈值必须单调不减，否则升级逻辑会乱
    if any(b < a for a, b in zip(p.thresholds, p.thresholds[1:])):
        p.thresholds = Params().thresholds
    return p


@dataclass
class Relation:
    user_id: str
    affection: float = 0.0
    stage: int = 0
    stage_since: float = 0.0
    days_active: int = 0
    last_day: str = ''
    gain_today: float = 0.0
    tag_counts: dict = field(default_factory=dict)
    bad_streak: int = 0
    locked_until: float = 0.0
    last_advance_day: str = ''
    mood: str = ''
    mood_until: float = 0.0
    flags: dict = field(default_factory=dict)
    updated_at: float = 0.0

    def to_row(self) -> dict:
        data = {
            'affection': round(float(self.affection), 3), 'stage': int(self.stage),
            'stage_since': float(self.stage_since), 'days_active': int(self.days_active),
            'last_day': self.last_day, 'gain_today': round(float(self.gain_today), 3),
            'bad_streak': int(self.bad_streak), 'locked_until': float(self.locked_until),
            'last_advance_day': self.last_advance_day, 'mood': self.mood,
            'mood_until': float(self.mood_until), 'updated_at': float(self.updated_at),
            'tag_counts': json.dumps(self.tag_counts, ensure_ascii=False),
            'flags': json.dumps(self.flags, ensure_ascii=False),
        }
        return data

    @classmethod
    def from_row(cls, user_id: str, row: dict) -> 'Relation':
        def load(raw):
            try:
                value = json.loads(raw or '{}')
                return value if isinstance(value, dict) else {}
            except (TypeError, ValueError):
                return {}
        return cls(
            user_id=str(user_id),
            affection=float(row.get('affection') or 0.0),
            stage=max(0, min(MAX_STAGE, int(row.get('stage') or 0))),
            stage_since=float(row.get('stage_since') or 0.0),
            days_active=int(row.get('days_active') or 0),
            last_day=str(row.get('last_day') or ''),
            gain_today=float(row.get('gain_today') or 0.0),
            tag_counts=load(row.get('tag_counts')),
            bad_streak=int(row.get('bad_streak') or 0),
            locked_until=float(row.get('locked_until') or 0.0),
            last_advance_day=str(row.get('last_advance_day') or ''),
            mood=str(row.get('mood') or ''),
            mood_until=float(row.get('mood_until') or 0.0),
            flags=load(row.get('flags')),
            updated_at=float(row.get('updated_at') or 0.0),
        )


@dataclass
class Outcome:
    applied: float = 0.0
    note: str = ''
    stage_before: int = 0
    stage_after: int = 0
    event_started: str = ''
    new_day: bool = False

    @property
    def stage_changed(self) -> bool:
        return self.stage_after != self.stage_before


def day_key(now: float) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(now))


def touch_day(rel: Relation, now: float) -> bool:
    """进入新的一天：有效日 +1，当日计数清零。返回是否是新的一天。"""
    key = day_key(now)
    if rel.last_day == key:
        return False
    rel.last_day = key
    rel.days_active += 1
    rel.gain_today = 0.0
    rel.tag_counts = {}
    return True


def _gate_cap(rel: Relation, p: Params) -> float:
    """分数封顶：天数或事件没到时，最多停在下一档门槛（不能存分）。"""
    if rel.stage >= MAX_STAGE:
        return p.max_affection
    nxt = rel.stage + 1
    blocked = rel.days_active < p.min_days[nxt]
    event = p.event_stages.get(nxt)
    if event and not rel.flags.get(f'event_{event}_done'):
        blocked = True
    return p.thresholds[nxt] if blocked else p.max_affection


def _try_advance(rel: Relation, now: float, p: Params, out: Outcome) -> None:
    if rel.stage >= MAX_STAGE:
        return
    today = day_key(now)
    if rel.last_advance_day == today:
        return                                    # 每天最多升一级
    nxt = rel.stage + 1
    if rel.affection < p.thresholds[nxt] or rel.days_active < p.min_days[nxt]:
        return
    event = p.event_stages.get(nxt)
    if event and not rel.flags.get(f'event_{event}_done'):
        if not rel.flags.get('pending_event'):
            rel.flags['pending_event'] = event
            rel.flags['event_turns'] = 0
            out.event_started = event
        return
    rel.stage = nxt
    rel.stage_since = now
    rel.last_advance_day = today
    rel.flags['impression_dirty'] = True
    out.stage_after = nxt


def _try_demote(rel: Relation, now: float, p: Params, out: Outcome) -> None:
    if rel.stage <= 0:
        return
    floor = max(0.0, p.thresholds[rel.stage] - p.demote_margin)
    if rel.affection <= floor and rel.bad_streak >= p.bad_streak_lock:
        rel.stage -= 1
        rel.stage_since = now
        rel.affection = p.thresholds[rel.stage] + 5.0
        rel.bad_streak = 0
        rel.flags['impression_dirty'] = True
        out.stage_after = rel.stage
        out.note = (out.note + ' demoted').strip()


def apply_delta(rel: Relation, delta: int, tag: str, now: float, p: Params) -> Outcome:
    """应用一次加减分。所有夹紧、递减、门槛都在这里。"""
    out = Outcome(stage_before=rel.stage, stage_after=rel.stage)
    out.new_day = touch_day(rel, now)
    tag = tag if tag in ALL_TAGS else 'neutral'
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        delta = 0
    delta = max(p.delta_min, min(p.delta_max, delta))

    if delta > 0:
        if rel.locked_until > now:
            out.note = 'locked'
        else:
            seen = int(rel.tag_counts.get(tag, 0))
            limit = max(1, int(p.tag_daily_limit))
            if seen >= limit:
                # 这个标签今天已经记满两次：不再计分（但新一轮对话可以喂别的标签）
                out.note = 'diminished'
            else:
                # 两次独立计分：第二次照样按它自己的 delta 算，不再做「delta - 已记次数」的递减
                eff = delta * p.stage_factor[rel.stage]
                eff = max(0.0, min(eff, p.daily_cap[rel.stage] - rel.gain_today))
                rel.tag_counts[tag] = seen + 1
                rel.gain_today += eff
                out.applied = eff
                if eff > 0:
                    rel.bad_streak = 0
                elif rel.gain_today >= p.daily_cap[rel.stage]:
                    out.note = 'daily_cap'
    elif delta < 0:
        out.applied = float(delta)
        if tag in BAD_TAGS:
            rel.bad_streak += 1
            if rel.bad_streak >= p.bad_streak_lock:
                rel.locked_until = max(rel.locked_until, now + p.lock_hours * 3600)
                out.note = 'lock_started'

    floor = max(0.0, p.thresholds[rel.stage] - p.demote_margin) if rel.stage > 0 else 0.0
    new_value = rel.affection + out.applied
    if out.applied < 0:
        new_value = max(floor, new_value)
    new_value = min(new_value, max(rel.affection, _gate_cap(rel, p)))
    new_value = max(0.0, min(new_value, p.max_affection))
    out.applied = new_value - rel.affection
    rel.affection = new_value
    rel.updated_at = now

    _try_advance(rel, now, p, out)
    _try_demote(rel, now, p, out)
    return out


def apply_presence(rel: Relation, now: float, p: Params) -> Outcome:
    """每天第一次有效互动的小奖励：养成"每天来"的习惯，同时推进有效日。"""
    return apply_delta(rel, p.presence_bonus, 'presence', now, p)


def complete_event(rel: Relation, event: str, now: float, p: Params) -> Outcome:
    """羁绊事件演完：置位 + 小额加分 + 尝试升级。"""
    out = Outcome(stage_before=rel.stage, stage_after=rel.stage)
    out.new_day = touch_day(rel, now)
    rel.flags[f'event_{event}_done'] = True
    rel.flags['pending_event'] = ''
    rel.flags['event_turns'] = 0
    rel.flags.setdefault('milestones', []).append({'event': event, 'at': now})
    rel.affection = min(p.max_affection, rel.affection + p.event_bonus)
    out.applied = p.event_bonus
    out.note = f'event_{event}_done'
    rel.updated_at = now
    _try_advance(rel, now, p, out)
    return out


def set_stage(rel: Relation, stage: int, now: float, p: Params) -> None:
    """骰主手动设阶段（调试/补偿用）：同时把分数和天数对齐到该阶段，事件视为已完成。"""
    stage = max(0, min(MAX_STAGE, int(stage)))
    rel.stage = stage
    rel.stage_since = now
    rel.affection = float(p.thresholds[stage])
    rel.days_active = max(rel.days_active, p.min_days[stage])
    rel.bad_streak = 0
    rel.locked_until = 0.0
    for nxt, event in p.event_stages.items():
        if nxt <= stage:
            rel.flags[f'event_{event}_done'] = True
    rel.flags['pending_event'] = ''
    rel.flags['impression_dirty'] = True
    rel.updated_at = now


def allowed_advance(stage: int) -> int:
    return ALLOWED_ADVANCE[max(0, min(MAX_STAGE, int(stage)))]


# ---------------- 规则层（无需 LLM） ----------------
_PUNCT = re.compile(r'[\s\W_]+', re.UNICODE)
_NARRATION = re.compile(r'[（(][^）)]{2,}[）)]|\*[^*]{2,}\*')


def is_trivial(text: str, min_chars: int = 4) -> bool:
    """"嗯""哦""?""哈哈"这种没有信息量的话，不值得评审。"""
    return len(_PUNCT.sub('', str(text or ''))) < max(1, int(min_chars))


def is_narration_hijack(text: str) -> bool:
    """用户用（动作/旁白）替她做决定：括号内容占大头且在写"你"。"""
    raw = str(text or '')
    spans = _NARRATION.findall(raw)
    if not spans:
        return False
    inside = sum(len(s) for s in spans)
    wrote_you = any('你' in s for s in spans)
    return wrote_you and inside >= 12 and inside * 2 >= len(raw)


def is_repeat(text: str, recent: list[str], similarity) -> bool:
    """和最近几条自己说过的话高度相似（复读/刷屏）。similarity 由调用方传入，避免循环依赖。"""
    cur = _PUNCT.sub('', str(text or ''))
    if len(cur) < 4:
        return False
    for old in recent[-4:]:
        if similarity(cur, _PUNCT.sub('', str(old or ''))) >= 0.85:
            return True
    return False
