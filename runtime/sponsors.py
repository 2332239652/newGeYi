# -*- encoding: utf-8 -*-
"""赞助档位：把「谁赞助了、赞助到哪一档」收在一处。

为什么单独一个模块（2026-11-08）
--------------------------------
以前只有一份 `sponsor_ids` + 一个额度，表达不了两档赞助。更要紧的是：**不能把
同一套判断复制到四个地方**。现在需要读档位的至少有四处——

  1. `runtime/private_gate.py`：按档给私聊额度、按档免掉拥挤期收紧；
  2. `memory/curator.py`：二档的记忆不参与容量裁剪；
  3. `relationship/service.py`：二档的记忆召回条数有下限；
  4. `routing/commands.py`：状态/菜单里显示档位。

四处各写一遍 `if uid in cfg.get('sponsor_tier2_ids') or ...`，迟早会不一致，
而且改一档的规则要改四个文件。所以这里只放**纯函数**：只读 config，不碰数据库、
不碰运行时对象，谁都能直接 import 用，不需要接线。

档位约定
--------
**累计关系：2 档包含 1 档的全部权益。**

    0 = 普通用户
    1 = 「请歌伊吃一天饭」  6 元/月
    2 = 「帮歌伊租半个月房子」20 元/月

老配置里的 `sponsor_ids` 没有档位概念，统一按 **1 档** 处理——行为与 2.9.10 完全一致，
不需要迁移。默认三份名单都是空、两档额度也是 0，所以这个功能**默认全关**，不影响任何人。
"""

from __future__ import annotations

# 两档的对外名字（状态、菜单、面板共用，别在别处再写一遍）
TIER_NAMES = {
    1: '一档·请歌伊吃一天饭',
    2: '二档·帮歌伊租半个月房子',
}

# 额度键：档位 → 配置键。加档位只改这张表。
_QUOTA_KEYS = {
    1: 'private_daily_quota_sponsor_tier1',
    2: 'private_daily_quota_sponsor_tier2',
}

# 名单键：档位 → 配置键
_ID_KEYS = {
    1: 'sponsor_tier1_ids',
    2: 'sponsor_tier2_ids',
}

# 没有档位概念的旧名单：一律当 1 档
_LEGACY_KEY = 'sponsor_ids'


def _num(config: dict, key: str, default: float = 0) -> float:
    try:
        return float(config.get(key, default) or 0)
    except (TypeError, ValueError):
        return float(default)


def id_set(config: dict, key: str) -> set:
    """读一份 QQ 号名单。只认列表/元组/集合，元素允许脏值（数字/None 混合），坏的忽略。

    为什么**不接受**裸字符串/裸数字：配置被手改坏时最常见的就是这个（`"sponsor_ids": "123"`），
    当成单元素列表处理会让"配置坏了"看起来像"配好了"，而这类静默误解最难排查。
    宁可返回空集，让人一眼看出没生效。
    """
    raw = config.get(key)
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def tier_of(config: dict, sender_id) -> int:
    """这个 QQ 在第几档。0 = 普通用户。

    `sender_id` 允许是任何东西（消息对象里偶尔是 int / None），一律转字符串再比。
    """
    uid = str(sender_id or '').strip()
    if not uid:
        return 0
    if uid in id_set(config, _ID_KEYS[2]):
        return 2
    if uid in id_set(config, _ID_KEYS[1]) or uid in id_set(config, _LEGACY_KEY):
        return 1
    return 0


def is_sponsor(config: dict, sender_id) -> bool:
    return tier_of(config, sender_id) > 0


def all_sponsors(config: dict) -> set:
    """三份名单的并集（面板统计「一共几个赞助者」用）。"""
    out: set = set()
    for key in (_LEGACY_KEY, _ID_KEYS[1], _ID_KEYS[2]):
        out |= id_set(config, key)
    return out


def quota_for(config: dict, sender_id) -> float:
    """这个人的赞助额度（0 = 没设 / 不限）。

    读不到档位专属额度时回落旧键 `private_daily_quota_sponsor`——这样 2.9.10 的
    老配置（只写了旧键）在升级后仍然生效，不会突然把赞助者的额度打回 60。
    """
    tier = tier_of(config, sender_id)
    if not tier:
        return 0.0
    value = _num(config, _QUOTA_KEYS[tier], 0)
    if value <= 0:
        value = _num(config, 'private_daily_quota_sponsor', 0)
    return value


def memory_keep(config: dict, sender_id) -> bool:
    """记忆是否免裁剪 —— **只有二档**有。

    意义是「半年后回来，她手里关于你的东西还在」。不改她怎么说话，只保证
    容量兜底不会把赞助者的事实当垃圾挤掉。
    """
    if not config.get('sponsor_memory_keep', True):
        return False
    return tier_of(config, sender_id) >= 2


def protected_ids(config: dict) -> set:
    """记忆免裁剪的 QQ 名单，直接喂给 `SQLiteStore.trim_user_facts(protect_ids=...)`。

    开关关掉时返回空集，裁剪行为与从前完全一致。
    """
    if not config.get('sponsor_memory_keep', True):
        return set()
    return id_set(config, _ID_KEYS[2])


def fact_floor(config: dict, sender_id) -> int:
    """记忆召回条数的下限 —— 只有二档有，低于这个数就抬到下限。

    为什么只是「下限」而不是「加成」：`FACT_LIMIT_BY_STAGE` 是随关系阶段长上去的
    （陌生 2 条 → 特别的人 12 条），擅自乘系数会让陌生阶段显得很熟，破坏人设。
    抬一个下限只是「她记得住的条目多一点」，不改变关系阶段本身。
    """
    if tier_of(config, sender_id) < 2:
        return 0
    try:
        return max(0, int(config.get('sponsor_fact_floor', 8) or 0))
    except (TypeError, ValueError):
        return 0


def is_beta(config: dict, sender_id) -> bool:
    """内测优先：二档自动在内，另外还能手加 `beta_ids`（给帮忙测试的朋友）。"""
    uid = str(sender_id or '').strip()
    if uid and uid in id_set(config, 'beta_ids'):
        return True
    return tier_of(config, sender_id) >= 2


def tier_name(config: dict, sender_id) -> str:
    tier = tier_of(config, sender_id)
    return TIER_NAMES.get(tier, '')
