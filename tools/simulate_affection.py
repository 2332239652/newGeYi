# -*- encoding: utf-8 -*-
"""好感度难度模拟：不碰数据库、不调模型，只跑状态机，看不同玩家多少天能走到哪个阶段。

用法（在 newGeYi 的上一级目录执行）：
    python -m newGeYi.tools.simulate_affection
    python -m newGeYi.tools.simulate_affection --days 120

改 config 里的 galgame_thresholds / galgame_min_days / galgame_daily_cap 之后重新跑一遍，
就能直接看到"认真玩家要几周、速攻党被挡在哪一阶段"，不用靠猜。
"""

from __future__ import annotations

import argparse
import time

from ..relationship import state as S
from ..relationship.stages import STAGE_NAMES

DAY = 86400.0
TAGS = ('care', 'share', 'humor', 'curious_lore', 'respect')

# 每个画像 = 每天的互动列表 [(评审 delta, tag, 用户亲密等级)]
PROFILES = {
    '速攻党（每天 60 句甜言蜜语，亲密等级 3）': [(2, 'care', 3)] * 60,
    '热情党（每天 60 句友善夸奖，等级 1）': [(2, TAGS[i % 5], 1) for i in range(60)],
    '认真玩家（每天 4 句真诚互动）': [(2, 'care', 1), (1, 'share', 1), (1, 'humor', 1), (2, 'curious_lore', 1)],
    '佛系玩家（每天 2 句）': [(1, 'humor', 1), (1, 'share', 1)],
    '隔天来的认真玩家': 'alternate',
}


def simulate(p: S.Params, plan, days: int) -> dict:
    allowed_advance = S.allowed_advance
    rel = S.Relation('sim')
    reached: dict[int, int] = {0: 0}
    base = time.mktime((2026, 3, 1, 12, 0, 0, 0, 0, -1))
    steady = PROFILES['认真玩家（每天 4 句真诚互动）']
    for day in range(days):
        now = base + day * DAY
        todays = (steady if day % 2 == 0 else []) if plan == 'alternate' else plan
        if not todays:
            continue
        S.apply_presence(rel, now, p)
        for i, (delta, tag, level) in enumerate(todays):
            if level > allowed_advance(rel.stage):         # 和服务层一致：超前即扣分
                delta, tag = min(delta, -1), 'flirt_early' if level >= 3 else 'pushy'
            S.apply_delta(rel, delta, tag, now + 10 + i, p)
        pending = rel.flags.get('pending_event')
        if pending:                                        # 假设事件总能顺利演出
            S.complete_event(rel, pending, now + 600, p)
        reached.setdefault(rel.stage, day + 1)
    return {'rel': rel, 'reached': reached}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=90)
    args = parser.parse_args()
    p = S.Params()
    print(f'参数：门槛 {p.thresholds} | 最少有效日 {p.min_days} | 日上限 {p.daily_cap}\n')
    for name, plan in PROFILES.items():
        result = simulate(p, plan, args.days)
        rel, reached = result['rel'], result['reached']
        steps = '，'.join(f'{STAGE_NAMES[s]}@第{d}天' for s, d in sorted(reached.items()) if s)
        print(f'{name}\n  {args.days} 天后：{STAGE_NAMES[rel.stage]}（{rel.affection:.0f} 分）  {steps or "（没有升过级）"}')


if __name__ == '__main__':
    main()
