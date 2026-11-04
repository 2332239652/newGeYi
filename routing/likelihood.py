# -*- encoding: utf-8 -*-
"""接话打分器：把「这条值不值得我开口」做成一道算术题。

思路来自麦麦（MaiBot）：他们用线上日志离线拟合了一个 8 维逻辑回归，权重写死，
运行时只做几次乘加就得到一个概率，**不需要发模型请求**。我们照搬这个形状，
特征换成歌伊这边拿得到的量。

为什么要这么干：主动搭话评估一天近千次，每次都要一次模型调用（哪怕只有 200 token）。
打分类把「明显不该接」的绝大多数消息拦在本地，模型只在真的要开口时才被叫醒。

两个设计要点：
1. **权重是常数，不是让模型现场判断**——想调就拟合完改配置，不改代码路径；
2. **阈值不是常数**——由「每小时想让她主动说几句」这个预算反解出来，
   旋钮从「玄学概率 0.3」变成人话「一小时三句」（见 `resolve_threshold`）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 默认权重（先照麦麦拟合出来的形状，等我们攒够自己的日志再拟合替换）。
# 名字对得上 `Features` 的字段；`intercept` 是截距。
DEFAULT_WEIGHTS = {
    'intercept': -1.20,
    'mention_bot': 1.59,        # 有人 @ 她 / 提到名字
    'at_other': -1.12,          # @ 的是别人（说明这不是对她说的）
    'question': 0.48,           # 带问号：可能在问人
    'media_only': -1.03,        # 纯图片/表情，没文字
    'self_ratio': 2.65,         # 最近窗口里她自己发言的占比（刚说过就别急着再说）
    'recent_count': -0.037,     # 最近窗口消息条数（越热闹越不该插）
    'log_seconds_since_self': 0.057,   # 距她上次发言的秒数取对数（越久越想开口）
    'batch_size': 0.10,         # 这一批攒了几条
}

# 样本不足时用这个阈值（麦麦离线表里「保留 50%」对应的值，属于保守档）
FALLBACK_THRESHOLD = 0.336
MIN_SAMPLES = 20


@dataclass(frozen=True)
class Features:
    """打分需要的现场特征。全部是便宜就能算出来的量，不查库、不调模型。"""

    mention_bot: bool = False
    at_other: bool = False
    question: bool = False
    media_only: bool = False
    self_ratio_5m: float = 0.0        # 0~1
    recent_count_5m: int = 0
    seconds_since_self: float = 3600.0
    batch_size: int = 1


def weights_from_config(config: dict) -> dict:
    """允许在 config.json 里覆盖任意一项权重（`gate_weights`）。"""
    merged = dict(DEFAULT_WEIGHTS)
    raw = config.get('gate_weights') if isinstance(config, dict) else None
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in merged:
                try:
                    merged[key] = float(value)
                except (TypeError, ValueError):
                    continue
    return merged


def contributions(features: Features, weights: dict | None = None) -> dict:
    """每项特征的「得分贡献」，日志里用来解释「为什么给了这个分」。"""
    w = weights or DEFAULT_WEIGHTS
    return {
        'intercept': w['intercept'],
        'mention_bot': w['mention_bot'] * float(features.mention_bot),
        'at_other': w['at_other'] * float(features.at_other),
        'question': w['question'] * float(features.question),
        'media_only': w['media_only'] * float(features.media_only),
        'self_ratio': w['self_ratio'] * min(1.0, max(0.0, features.self_ratio_5m)),
        'recent_count': w['recent_count'] * max(0, features.recent_count_5m),
        'log_seconds_since_self': (
            w['log_seconds_since_self'] * math.log1p(max(0.0, features.seconds_since_self))
        ),
        'batch_size': w['batch_size'] * max(1, features.batch_size),
    }


def score(features: Features, weights: dict | None = None) -> float:
    """返回 0~1 的开口气概率。纯算术，几十微秒。"""
    logit = sum(contributions(features, weights).values())
    # 防溢出：logit 极大/极小时直接给 0/1
    if logit >= 30:
        return 1.0
    if logit <= -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-logit))


def target_ratio(replies_per_hour: float, batches_per_hour: float) -> float:
    """把「每小时想说几句」换算成「这些批次里我该接多大比例」。

    举例：一小时群里冒出来 60 批消息，我只想让她主动说 3 句，
    那就是 5% 的批次放行——比「概率 0.05」直观，而且会随群活跃度自动缩放。
    """
    batches = max(1.0, float(batches_per_hour))
    want = max(0.0, float(replies_per_hour))
    return max(0.0, min(1.0, want / batches))


def resolve_threshold(scores: list[float], ratio: float,
                      fallback: float = FALLBACK_THRESHOLD,
                      min_samples: int = MIN_SAMPLES) -> float:
    """在最近的分数分布里按分位数反解阈值。

    保留比例 20% → 取 80% 分位当门槛，只有最想开口的那 20% 批次能过去。
    样本太少（< min_samples）时退回 fallback，避免开局乱放行。
    """
    if ratio <= 0:
        return 1.01              # 谁都不许过
    if ratio >= 1:
        return 0.0
    clean = [float(s) for s in scores if s is not None]
    if len(clean) < max(1, min_samples):
        return float(fallback)
    ordered = sorted(clean)
    index = int(round((1.0 - ratio) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


def passes(probability: float, threshold: float) -> bool:
    return float(probability) >= float(threshold)


def explain(features: Features, weights: dict | None = None) -> str:
    """一行人类可读的解释，给 `detail()` 日志用。"""
    parts = contributions(features, weights)
    hot = sorted(parts.items(), key=lambda kv: -abs(kv[1]))[:3]
    return ' '.join('%s%+.2f' % (name, value) for name, value in hot)
