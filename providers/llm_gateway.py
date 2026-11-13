# -*- encoding: utf-8 -*-
"""模型网关：统一 OpenAI 兼容 chat/completions 调用，支持多渠道故障转移。

职责：
- 多渠道分层调用：按 priority 依次尝试，便宜/稳定的渠道排前面
- 故障转移：连续失败达阈值才判定渠道不可用（单次抖动不切换）
- 自动恢复：后台定期探测「优先级更高但不可用」的渠道，恢复即切回
- 连接复用（thread-local requests.Session）
- 超时、有限重试、指数退避
- 全局并发信号量
- 调用指标记录（api_calls 表）+ 切换历史（settings 表）

设计要点：
- 认证类错误（401/403/key 缺失）不会自愈，立即判定不可用，不等阈值
- empty_content 属于模型输出问题、不是渠道故障，不计入失败计数
- 状态（当前渠道、不可用渠道、切换历史）落 SQLite，重启后仍记得
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid

import requests

from ..models import ModelRequest, ModelResult
from ..storage.database import SQLiteStore

logger = logging.getLogger('newGeYi.gateway')

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 认证类错误：key 无效/欠费不会自己好，重试与等待都没意义
AUTH_STATUS = {401, 403}
# 请求本身有问题的状态码（请求体坏了 / 图太大 / 参数不合法）。
# 这类错误**换渠道也没用**，更不能记成「渠道不健康」——否则一个坏请求就能把
# 所有渠道判死、切到兜底渠道并锁定，还要给骰主发一条「全挂」（2026-11-03 审查发现）。
CLIENT_ERROR_STATUS = {400, 404, 405, 409, 413, 414, 415, 422, 431, 501}
# 后台任务：它们的超时/连接抖动**不计入渠道健康**（提炼是"尽力而为"，不该拖累聊天）。
# 见 _mark_failure 里的说明与 2026-11-07 的失败率数据（提炼 28% vs 聊天 1~3%）。
BACKGROUND_KINDS = frozenset({'memory_curate'})
PROBE_PROMPT = 'hi'


def is_provider_fault(error_code: str) -> bool:
    """这个错误码算不算「渠道自己有问题」。

    - 空回复（思考链吃光 max_tokens）：是**请求/模型输出**问题，不是渠道故障；
    - 4xx 请求错误：这一次的请求有问题，换个渠道问同一件事还是同样的错；
    - 超时 / 连接 / 429 / 5xx / 401 / 403：渠道问题。
    `chat()` 只有在最后一轮失败**是渠道问题**时才升级到兜底渠道并锁定。
    """
    code = str(error_code or '')
    if not code:
        return True
    if code == 'empty_content':
        return False
    if code in ('no_api_key', 'no_provider'):
        return True
    if code.startswith('http_'):
        try:
            status = int(code[5:])
        except ValueError:
            return True
        return status not in CLIENT_ERROR_STATUS
    return True


# 各家 usage 里「命中前缀缓存的输入 token」可能出现的位置，按优先级排列。
# 为什么列这么长：缓存命中率是「省钱改造」唯一的尺子，而厂商连同中转站字段名完全不统一，
# 漏一个就永远是 0% —— 而 0% 既可能是"没命中"，也可能只是"没读到"，两者该做的是相反的事。
_CACHE_TOKEN_PATHS = (
    ('prompt_tokens_details', 'cached_tokens'),      # OpenAI / 智谱 / 多数中转
    ('input_tokens_details', 'cached_tokens'),       # 新版 OpenAI Responses
    ('cached_tokens',),                              # 部分中转拍平
    ('cached_input_tokens',),                        # 另一批中转的写法
    ('prompt_cache_hit_tokens',),                    # DeepSeek 官方
    ('cache_read_input_tokens',),                    # Anthropic（读缓存）
    ('cachedContentTokenCount',),                    # Gemini 原生（驼峰）
    ('cached_content_token_count',),                 # Gemini 原生（下划线）
    ('usageMetadata', 'cachedContentTokenCount'),    # Gemini 原生整块透传
    ('usageMetadata', 'cached_content_token_count'),
)


def _int_at(usage: dict, path: tuple) -> int:
    """按路径取一个整数；缺失或类型不对都当 0（usage 是厂商自由发挥的重灾区）。"""
    node = usage
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
    try:
        return int(node) if node is not None else 0
    except (TypeError, ValueError):
        return 0


def _has_at(usage: dict, path: tuple) -> bool:
    """这条路径是否**存在字段**（值是 0 也算存在，和"没这个字段"是两件事）。"""
    node = usage
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node.get(part)
    return node is not None


def cached_tokens_from_usage(usage: dict) -> int:
    """从各家 response.usage 里挖出「命中前缀缓存的输入 token 数」。

    支持 OpenAI/智谱的 details、DeepSeek 的 prompt_cache_hit_tokens、Anthropic 的
    cache_read_input_tokens，以及 Gemini 原生的 cachedContentTokenCount（见路径表）。
    拿不到就当 0。这个数字**只用来算缓存命中率与观测**，不参与任何业务判断。
    """
    if not isinstance(usage, dict):
        return 0
    for path in _CACHE_TOKEN_PATHS:
        value = _int_at(usage, path)
        if value > 0:
            return value
    return 0


def usage_reports_cache(usage: dict) -> bool:
    """这份 usage 里是否**出现**了缓存字段（哪怕是显式的 0）。

    用来把「真的没命中」和「渠道压根没透传」分开：界面上两者都是 0，
    但前者该去调提示词/前缀，后者该去换渠道——不做这个区分，
    「缓存命中 0%」就永远是一句没有信息量的话。
    """
    if not isinstance(usage, dict):
        return False
    return any(_has_at(usage, path) for path in _CACHE_TOKEN_PATHS)


def response_usage(data: dict) -> dict:
    """从响应体取 usage：优先 `usage`，退回 Gemini 原生的 `usageMetadata`。"""
    if not isinstance(data, dict):
        return {}
    usage = data.get('usage')
    if isinstance(usage, dict) and usage:
        return usage
    meta = data.get('usageMetadata')
    if isinstance(meta, dict):
        return meta
    return usage if isinstance(usage, dict) else {}


def usage_tokens(usage: dict, protocol: str = 'openai') -> tuple[int, int, int]:
    """把各家 usage 归一成（**总输入** token, 输出 token, 命中缓存 token）。

    归一的关键是「总输入」：命中率的分子是 cached，分母必须是**含缓存的全部输入**，
    否则这个百分比会撒谎。三套口径线上都真的出现过：

    - Anthropic：`input_tokens` **不含** `cache_read_input_tokens` /
      `cache_creation_input_tokens`。命中时会出现「输入 43、缓存 5376」这种
      12500% 的荒谬比例（历史库里有 2 行）。
    - OpenAI 兼容：`prompt_tokens` 通常**已含**缓存（子集），直接当总量。
    - 个别中转（实测某个 gemini 反代）：把两者报成**互斥**，`prompt_tokens` 只给未命中的
      部分，于是 cached > prompt（历史库里有 4 行），既少算 token 又会算出 >100%。
      判据就是 `cached > prompt`：只有互斥口径才可能这样。
    """
    usage = usage if isinstance(usage, dict) else {}
    cached = cached_tokens_from_usage(usage)
    if str(protocol or '').strip().lower() == 'anthropic':
        prompt = _int_at(usage, ('input_tokens',))
        cache_write = _int_at(usage, ('cache_creation_input_tokens',))
        output = _int_at(usage, ('output_tokens',))
        total = prompt + cached + cache_write
    else:
        prompt = (_int_at(usage, ('prompt_tokens',))
                  or _int_at(usage, ('input_tokens',))
                  or _int_at(usage, ('promptTokenCount',)))
        output = (_int_at(usage, ('completion_tokens',))
                  or _int_at(usage, ('output_tokens',))
                  or _int_at(usage, ('candidatesTokenCount',)))
        total = prompt + cached if cached > prompt else prompt
    if total < cached:      # 兜底：命中数不可能超过总输入
        total = cached
    return total, output, cached


# 仅单渠道兼容模式使用：opencode 网关要求 x-opencode-session 路由头
_OPCODE_SESSION_ID = uuid.uuid4().hex

# 上游服务商直接作为正文返回的安全策略/拦截错误特征
_UPSTREAM_SAFETY_RE = re.compile(
    r'(?i)(?:The prompt could not be submitted|Generative AI Prohibited Use policy|'
    r'violate[s]? Google\'s|ai\.google\.dev/gemini-api|'
    r'content management policy|content filter|'
    r'HARM_CATEGORY_|SAFETY_RATING|'
    r'Request was rejected due to safety policies)'
)

# settings 表中的持久化键
_KEY_CURRENT = 'gw.current'
_KEY_UNHEALTHY = 'gw.unhealthy'
_KEY_HISTORY = 'gw.history'
_KEY_ANCHOR = 'gw.anchor'
# 「锁定到兜底渠道」：五个候选全挂之后按骰主的口径切官方 DeepSeek 并锁死，
# 直到有人手动「切换渠道 X」。和 anchor 分开存，因为 anchor 的语义是
# 「不许越过它往上爬」，而这里是「**只准用它**」，用它表达不了。
_KEY_HOLD = 'gw.hold'


def _env_key_for(name: str) -> str:
    """渠道 key 也可从环境变量读：GEYI_KEY_<渠道名大写，非字母数字转下划线>。"""
    safe = ''.join(ch if ch.isalnum() else '_' for ch in str(name)).upper()
    return os.environ.get(f'GEYI_KEY_{safe}', '').strip()


class Provider:
    """一个渠道的静态配置。

    `kinds` 决定这个渠道服务哪些调用类型（2026-09-19 新增）：

        不填 / None      → 服务全部（向后兼容，老配置不用改）
        ['memory_curate'] → 只服务记忆提炼
        ['chat']          → 服务除提炼外的全部（聊天、主动搭话等）

    为什么需要它：聊天和提炼的成本结构完全相反——
      聊天：输入大、输出小，缓存能省（实测官方缓存 100% vs 中转站 55%）
      提炼：输入≈输出，成本 67% 在输出，缓存打不到痛点
    实测（歌伊 582 次提炼 + 904 次聊天）混合路由比纯中转站便宜 4~10%，
    且聊天快 5 倍（1.7s vs 8.6s）。详见 _research/final_cost_v2.py。
    """

    __slots__ = ('name', 'endpoint', 'api_key', 'model', 'priority', 'timeout',
                 'extra_headers', 'extra_body', 'legacy', 'kinds', 'protocol', 'manual_only')

    def __init__(self, name, endpoint, api_key, model, priority=1, timeout=25.0,
                 extra_headers=None, extra_body=None, legacy=False, kinds=None,
                 protocol='openai', manual_only=False):
        self.name = name
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.priority = int(priority)
        self.timeout = float(timeout)
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})
        self.legacy = bool(legacy)
        raw_kinds = kinds if isinstance(kinds, (list, tuple, set)) else None
        self.kinds = {str(k).strip() for k in raw_kinds if str(k).strip()} if raw_kinds else None
        self.protocol = str(protocol).strip().lower() or 'openai'
        # manual_only：可选渠道。会加载、可「切换渠道」选中，但**永不参与自动故障转移**，
        # 也不会被恢复探测自动切回去——只有骰主手动切换才会启用。
        self.manual_only = bool(manual_only)

    def serves(self, kind: str) -> bool:
        """这个渠道接不接某类调用。kinds 为空 = 全都接。"""
        if self.kinds is None:
            return True
        if kind in self.kinds:
            return True
        # 'chat' 是通配：除提炼之外的都算聊天类
        if 'chat' in self.kinds and kind != 'memory_curate':
            return True
        return False

    def __repr__(self) -> str:
        tag = '' if self.kinds is None else f' kinds={sorted(self.kinds)}'
        manual = ' manual_only' if self.manual_only else ''
        return f'<Provider {self.name} {self.model} p{self.priority}{tag}{manual}>'


class ProviderState:
    """渠道运行时状态（随调用变化，部分会持久化）。"""

    __slots__ = ('name', 'fail_count', 'healthy', 'last_error', 'last_fail_at',
                 'last_ok_at', 'last_probe_at', 'probe_ok_streak', 'cooldown_until')

    def __init__(self, name):
        self.name = name
        self.fail_count = 0
        self.healthy = True
        self.last_error = ''
        self.last_fail_at = 0.0
        self.last_ok_at = 0.0
        self.last_probe_at = 0.0
        self.probe_ok_streak = 0
        # 熔断冷却：判死之后到这一刻之前**不再进入候选队列**（2026-11-12，骰主需求
        # 「挂了就先移出队列，通了再回来」）。以前只是把不可用渠道排到队尾，
        # 于是每条消息都要在某几条死渠道上各等 60 秒超时，实测一条回复卡 150 秒。
        self.cooldown_until = 0.0


def build_providers(config: dict) -> list[Provider]:
    """从配置构造渠道列表。

    - config['providers'] 非空：多渠道模式，每个渠道自己的 endpoint/key/model
    - 否则：退回 api_endpoint / api_key / fast_model 单渠道模式（兼容老配置）
    """
    default_timeout = float(config.get('request_timeout_seconds', 45))
    blocking = config.get('extra_body') if isinstance(config.get('extra_body'), dict) else {}
    providers: list[Provider] = []

    raw = config.get('providers')
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or not item.get('enabled', True):
                continue
            name = str(item.get('name') or f'provider{index + 1}').strip()
            endpoint = str(item.get('endpoint') or '').strip()
            model = str(item.get('model') or '').strip()
            if not endpoint or not model:
                logger.warning('渠道 %s 缺少 endpoint 或 model，已跳过', name)
                continue
            api_key = str(item.get('api_key') or '').strip() or _env_key_for(name)
            extra_body = dict(blocking)
            extra_body.update(item.get('extra_body') if isinstance(item.get('extra_body'), dict) else {})
            protocol = str(item.get('protocol') or '').strip().lower()
            if not protocol:
                protocol = 'anthropic' if ('/messages' in endpoint or 'anthropic' in endpoint) else 'openai'
            providers.append(Provider(
                name=name,
                endpoint=endpoint,
                api_key=api_key,
                model=model,
                priority=item.get('priority', index + 1),
                timeout=item.get('timeout', default_timeout),
                extra_headers=item.get('extra_headers') if isinstance(item.get('extra_headers'), dict) else {},
                extra_body=extra_body,
                kinds=item.get('kinds'),
                protocol=protocol,
                manual_only=bool(item.get('manual_only', False)),
            ))

    if not providers:
        endpoint = str(config.get('api_endpoint') or '').strip()
        model = str(config.get('fast_model') or '').strip()
        if endpoint and model:
            protocol = str(config.get('api_protocol') or '').strip().lower()
            if not protocol:
                protocol = 'anthropic' if ('/messages' in endpoint or 'anthropic' in endpoint) else 'openai'
            providers.append(Provider(
                name='default',
                endpoint=endpoint,
                api_key=LLMGateway.resolve_api_key(config),
                model=model,
                priority=1,
                timeout=default_timeout,
                extra_headers=config.get('extra_headers') if isinstance(config.get('extra_headers'), dict) else {},
                extra_body=dict(blocking),
                legacy=True,
                protocol=protocol,
            ))

    providers.sort(key=lambda p: (p.priority, p.name))
    return providers


class LLMGateway:
    def __init__(self, store: SQLiteStore, config: dict):
        self.store = store
        self.config = config
        self._semaphore = threading.Semaphore(int(config.get('global_concurrency', 4)))
        self._local = threading.local()
        self._lock = threading.RLock()
        self._pairs: list[tuple[Provider, ProviderState]] = []
        self._current_name = ''
        self._anchor_name = ''
        self._hold_name = ''
        self._history: list[dict] = []
        self._last_down_notify = 0.0
        # 「软降级」（2026-11-12）：全挂后不立刻锁定，先等 retry_before_hold_seconds
        # 再从锚点重试，仍失败才通知 + 锁定。0 = 没有待重试。
        self._pending_hold_at = 0.0
        self._pending_error = ''
        self._pending_from = ''
        # 手动切换的代数：后台重试跑到一半时骰主手动切了渠道，就靠它作废这次重试，
        # 否则「我刚切到官方，13 秒后它又把我锁回中转站」（2026-11-12 现场）
        self._manual_epoch = 0

        # 上一轮被冷却跳过的渠道名单：只在名单发生变化时打一行日志，免得每条消息都刷
        self._cooling_note = ''
        self._notify_cb = None
        self._probe_stop = threading.Event()
        self._probe_thread: threading.Thread | None = None
        self.reload(config)
        with self._lock:
            # 启动时恢复上次的当前渠道与不可用标记
            self._apply_persisted_locked()

    # ==================== 配置与状态 ====================
    def reload(self, config: dict) -> None:
        """按新配置重建渠道列表，保留同名渠道的运行时状态。

        注意：运行中重载**不**回读持久化的不可用标记 —— 内存里的健康状态更新，
        否则「刚恢复但尚未写回」的渠道会被旧数据再次标成不可用。
        """
        self.config = config
        with self._lock:
            old_states = {p.name: st for p, st in self._pairs}
            providers = build_providers(config)
            self._pairs = [(p, old_states.get(p.name) or ProviderState(p.name)) for p in providers]
            names = [p.name for p, _ in self._pairs]
            if self._current_name not in names:
                self._current_name = names[0] if names else ''
            # 注意：这里刻意不写盘 —— __init__ 时写盘会覆盖上次保存的状态，
            # 而运行中的状态变化都会由 _mark_* / _switch_locked / probe 负责持久化。
        logger.info('网关渠道已加载：%s', ', '.join(names) or '（无）')

    @property
    def _failover_cfg(self) -> dict:
        value = self.config.get('failover')
        return value if isinstance(value, dict) else {}

    def _cooldown_cfg(self) -> tuple[float, float]:
        """熔断冷却的 (首次秒数, 上限秒数)。`cooldown_base_seconds = 0` 表示关闭熔断
        （退回「不可用渠道排到队尾」的老行为）。配置写错一律回默认，绝不让机器人哑掉。"""
        cfg = self._failover_cfg
        raw = cfg.get('cooldown_base_seconds', 120)
        try:
            base = float(120 if raw is None else raw)
        except (TypeError, ValueError):
            base = 120.0
        if base <= 0:
            return 0.0, 0.0
        raw_cap = cfg.get('cooldown_max_seconds', 1800)
        try:
            cap = float(1800 if raw_cap is None else raw_cap)
        except (TypeError, ValueError):
            cap = 1800.0
        if cap < base:
            cap = base
        return base, cap

    def _last_resort_name(self) -> str:
        """兜底渠道名（`failover.last_resort_provider`），没配就是空串。"""
        return str(self._failover_cfg.get('last_resort_provider', 'official-chat') or '').strip()

    def set_notify(self, callback) -> None:
        """注入「通知骰主」的回调：callback(text) -> None。"""
        self._notify_cb = callback

    # ==================== 手动锚点 ====================
    def _anchor_priority_locked(self) -> int | None:
        """手动锚点渠道的优先级；None 表示没有锚点（纯按优先级顺序）。

        语义（2026-10 起，需求来自「中转站靠不住、反代额度会用完」）：

        - 骰主用「切换渠道 X」手动指定的那个渠道是**顶**；
        - 自动故障转移只在锚点**及其以下**的渠道里挑备份，绝不自作主张往上跳；
        - 后台探测也只爬回到锚点为止，不会去碰比它更高优先级的渠道
          （典型：免费反代额度用完了，探测偶尔还能通，但一上去就又 429）。

        没有锚点时行为和以前完全一致（按 priority 从高到低自动恢复）。
        """
        with self._lock:
            name = self._anchor_name
            if not name:
                return None
            for provider, _ in self._pairs:
                if provider.name == name:
                    return provider.priority
            return None

    def anchor_name(self) -> str:
        with self._lock:
            return self._anchor_name

    def _ordered_pairs(self, kind: str = '') -> list[tuple[Provider, ProviderState]]:
        """候选顺序：当前渠道 → 其他健康渠道 → 不健康渠道兜底（同级按 priority）。

        `kind` 非空时先按渠道的 `kinds` 过滤（提炼可以走另一家）。
        过滤后为空则**退回全部渠道**——配置写错不该让整个机器人哑掉，
        宁可走错渠道也不要一条都发不出去。

        `manual_only` 的可选渠道不进入自动候选：除非它正是被骰主手动选中的当前渠道，
        否则永远不参与故障转移。

        有手动锚点时，排在锚点**之上**的渠道全部不参与——这就是「反代额度用完了
        别再去碰它」的实现方式。
        """
        with self._lock:
            pairs = list(self._pairs)
            current = self._current_name
            hold = self._hold_name

        # 锁定兜底渠道：只准用它，一个别的都不试。
        # 骰主的口径是「五个全挂就切官方 DeepSeek 不再尝试，直到我手动切回其中一个」——
        # 这个语义 anchor 表达不了（anchor 只是「不许越过它往上」，
        # 而官方 DeepSeek 排在最后，锚点逻辑会判定"之下没有渠道"从而忽略限制）。
        if hold:
            held = [p for p in pairs if p[0].name == hold]
            if held:
                return held
            logger.warning('锁定的兜底渠道 %s 已不在配置里，自动解除锁定', hold)
            with self._lock:
                if self._hold_name == hold:
                    self._hold_name = ''
                    self._persist_locked()

        if kind:
            scoped = [p for p in pairs if p[0].serves(kind)]
            if scoped:
                pairs = scoped
            else:
                logger.warning('没有渠道声明服务 %s，退回全部渠道', kind)

        # 可选渠道只在「它就是当前渠道」时保留，其余一律排除出自动顺序
        auto_pairs = [p for p in pairs if (not p[0].manual_only) or p[0].name == current]
        if auto_pairs:
            pairs = auto_pairs
        elif pairs:
            # 全是 manual_only 且当前不在其中：宁可退回全部，也别让机器人哑掉
            logger.warning('仅剩 manual_only 渠道可用，临时退回全部渠道以免无法回复')

        # 手动锚点：备份只在锚点及其以下挑。锚点下面一个都没有时（例如锚点选的是
        # 最后一个渠道），宁可越过锚点兜底——回复优先级永远高于「别乱花钱」。
        ceiling = self._anchor_priority_locked()
        if ceiling is not None:
            scoped = [p for p in pairs if p[0].priority >= ceiling or p[0].name == current]
            if any(p[0].name != current for p in scoped):
                pairs = scoped
            else:
                logger.warning('手动锚点 %s 之下没有其他渠道，本轮忽略锚点限制',
                               self._anchor_name)

        def rank(pair):
            provider, state = pair
            return (
                0 if provider.name == current else 1,
                1 if not state.healthy else 0,
                provider.priority,
                provider.name,
            )

        # ===== 熔断冷却（2026-11-12）=====
        # 判死的渠道**整条移出候选**，不是排到队尾：排到队尾等于每条消息都还要在它身上
        # 等一次 60 秒超时（实测四个死渠道能把一条回复拖到 150 秒上限）。
        # 全部都在冷却时挑最早到期的一条硬试，免得「一条都发不出去」。
        now = time.time()
        alive = [(p, st) for p, st in pairs if st.healthy or st.cooldown_until <= now]
        if alive:
            skipped = [p.name for p, st in pairs if not (st.healthy or st.cooldown_until <= now)]
            if skipped and self._cooling_note != ','.join(skipped):
                self._cooling_note = ','.join(skipped)
                logger.info('本轮跳过冷却中的渠道：%s', '、'.join(skipped))
            pairs = alive
        elif pairs:
            pairs = sorted(pairs, key=lambda x: (x[1].cooldown_until, x[0].priority))[:1]
            if self._cooling_note != 'all':
                self._cooling_note = 'all'
                logger.warning('所有渠道都在熔断冷却中，本轮只硬试最早到期的一条：%s',
                               pairs[0][0].name)

        ordered = sorted(pairs, key=rank)
        preferred = self._preferred_for(kind)
        if preferred:
            for index, pair in enumerate(ordered):
                if pair[0].name == preferred:
                    if not pair[1].healthy:
                        break       # 它也挂了就别硬塞到第一个去撞墙
                    if index:
                        ordered.insert(0, ordered.pop(index))
                    break
        return ordered

    # 哪些 kind 允许在配置里指定专用渠道（<kind> → 配置键名）
    _KIND_PROVIDER_KEYS = {
        'memory_curate': 'curate_provider',
        'gate': 'gate_provider',
        'affection': 'affection_provider',
        # 工作记忆的状态纸条（2026-11-10）：机械压缩活，指到便宜渠道
        'scene': 'scene_provider',
    }

    def _preferred_for(self, kind: str) -> str:
        """某些 kind 可以固定走专用渠道，聊天本身完全不受影响。

        - `memory_curate`（记忆提炼）：后台机械活，不需要聊天主力那套理解力；
        - `gate`（搭话判断）：只要回答 SPEAK/SKIP，专用小模型 + 独立计费。

        指定的渠道挂了照常按优先级往下兜底，不会因为省钱把任务饿死。
        """
        key = self._KIND_PROVIDER_KEYS.get(kind)
        if not key:
            return ''
        return str(self.config.get(key, '') or '').strip()

    def has_multiple_providers(self) -> bool:
        with self._lock:
            return len(self._pairs) > 1

    def provider_names(self) -> list[str]:
        with self._lock:
            return [p.name for p, _ in self._pairs]

    def missing_keys(self) -> list[str]:
        """哪些渠道没配 Key。

        「设置key」写的是顶层 api_key，多渠道路由下每个渠道有自己的 Key
        （providers[].api_key 或环境变量 GEYI_KEY_<渠道名>），
        所以状态里不能只看顶层那一个，否则会显示「未配置」而其实跑得好好的。
        """
        with self._lock:
            return [p.name for p, _ in self._pairs if not p.api_key]

    def current_summary(self) -> str:
        """当前渠道摘要，用于状态显示（不展开全部渠道）。"""
        with self._lock:
            pairs = list(self._pairs)
            current = self._current_name
            anchor = self._anchor_name
        if not pairs:
            return '未配置'
        provider = next((p for p, _ in pairs if p.name == current), pairs[0][0])
        unhealthy = [p.name for p, st in pairs if not st.healthy]
        text = f'{provider.name}（{provider.model}）'
        if anchor and anchor != provider.name:
            text += f' | 锚点 {anchor}'
        if unhealthy:
            text += f' | ⚠ 不可用: {"、".join(unhealthy)}'
        return text

    def status_text(self) -> str:
        """给命令用的渠道状态文本。"""
        with self._lock:
            pairs = list(self._pairs)
            current = self._current_name
            anchor = self._anchor_name
            hold = self._hold_name
            history = list(self._history)
        if not pairs:
            return '尚未配置任何模型渠道（config.json 的 providers 为空且缺少 api_endpoint/api_key）'
        lines = ['--- 模型渠道 ---']
        if hold:
            lines.append(f'⛔ 已锁定兜底渠道: {hold}（五个候选全挂后自动切入）'
                         f' → 不再尝试其它渠道；「切换渠道 <名称|自动>」可解除')
        elif anchor:
            anchor_provider = next((p for p, _ in pairs if p.name == anchor), None)
            model = anchor_provider.model if anchor_provider is not None else '?'
            lines.append(f'手动锚点: {anchor}（{model}）'
                         f' → 故障只在它以下找备份，恢复只回到这里；「切换渠道 自动」取消')
        else:
            lines.append('手动锚点: 无（按优先级自动恢复，会一路爬回最上面）')
        pending = self._pending_hold_at
        if pending:
            left = max(0, int(pending - time.time()))
            lines.append(f'⏳ 全挂冷却中：{left} 秒后从锚点重试（重试成功不通知也不锁定；'
                         f'仍失败才切兜底并通知）')
        for index, (provider, state) in enumerate(pairs, 1):
            marks = ''
            if provider.name == current:
                marks += ' ← 当前'
            if provider.name == anchor:
                marks += ' ⇤ 锚点'
            if provider.name == hold:
                marks += ' ⛔ 锁定'
            health = '正常' if state.healthy else f'不可用({state.last_error or "未知"})'
            if not state.healthy and state.cooldown_until > time.time():
                health += f'·冷却{max(0, int(state.cooldown_until - time.time()))}s'
            tag = '可选·手动' if provider.manual_only else f'优先级{provider.priority}'
            lines.append(
                f'{index}. {provider.name} | {provider.model} | {tag} | '
                f'{health} | 连败{state.fail_count}{marks}'
            )
        if history:
            lines.append('')
            lines.append('--- 最近切换 ---')
            for item in reversed(history[-8:]):
                stamp = time.strftime('%m-%d %H:%M', time.localtime(float(item.get('ts') or 0)))
                detail = f" ({item['detail']})" if item.get('detail') else ''
                lines.append(f'{stamp}  {item.get("from") or "-"} → {item.get("to") or "-"}  [{item.get("reason") or "-"}]{detail}')
        else:
            lines.append('')
            lines.append('（暂无切换记录）')
        return '\n'.join(lines)

    def switch_to(self, name_or_index: str) -> str:
        """手工切换渠道，返回结果文本。

        手动切换会同时把目标设为**手动锚点**：之后自动故障转移只在它及其以下找备份，
        后台探测也只爬回到它为止。传「自动」则取消锚点，恢复成按优先级自动恢复。
        """
        with self._lock:
            pairs = list(self._pairs)
            key = str(name_or_index or '').strip()
            self._manual_epoch += 1      # 任何手动切换都让后台重试作废

            if key in ('自动', 'auto', 'AUTO', 'Auto', '自动选择', '取消锚点'):
                self._anchor_name = ''
                # 手动操作一律解除「锁定兜底渠道」：骰主回来了就听骰主的
                was_held = self._hold_name
                self._hold_name = ''
                self._pending_hold_at = 0.0      # 手动操作也撤掉「待重试」
                self._pending_error = ''
                best = None
                for provider, state in sorted(pairs, key=lambda x: (x[0].priority, x[0].name)):
                    if provider.manual_only or not state.healthy:
                        continue
                    best = provider
                    break
                if best is None:
                    self._persist_locked()
                    return '已取消手动锚点，但当前没有健康渠道可选，保持原渠道'
                old = self._current_name
                self._current_name = best.name
                self._append_history_locked(old, best.name, 'manual', '取消锚点')
                self._persist_locked()
                note = '（已解除兜底锁定）' if was_held else ''
                return (f'✅ 已取消手动锚点{note}，当前渠道 {best.name}（{best.model}），'
                        f'恢复成按优先级自动恢复')

            target = None
            if key.isdigit() and 1 <= int(key) <= len(pairs):
                target = pairs[int(key) - 1][0]
            else:
                for provider, _ in pairs:
                    if provider.name == key:
                        target = provider
                        break
            if target is None:
                return f'找不到渠道：{key}（用「渠道状态」看可用列表）'
            old = self._current_name
            same = (target.name == old)
            was_held = bool(self._hold_name)
            self._current_name = target.name
            self._anchor_name = target.name
            self._hold_name = ''        # 手动指定锚点 = 解除兜底锁定
            self._pending_hold_at = 0.0  # 也撤掉全挂后的待重试
            self._pending_error = ''
            state = next(st for p, st in pairs if p.name == target.name)
            state.healthy = True
            state.fail_count = 0
            state.last_error = ''
            state.cooldown_until = 0.0   # 骰主亲手点的渠道，冷却清零：必须真的去试
            self._append_history_locked(old, target.name, 'manual', '锚点')
            self._persist_locked()
            unlock = '\n（已解除「五连挂后锁定兜底渠道」的状态）' if was_held else ''
            if same:
                return (f'✅ {target.name}（{target.model}）本来就是当前渠道，'
                        f'已设为手动锚点，自动恢复只会回到这里{unlock}')
            return (f'✅ 已切换到渠道 {target.name}（{target.model}）\n'
                    f'已设为手动锚点：故障时只在它以下的渠道里找备份，'
                    f'恢复也只回到这里（「切换渠道 自动」可取消）{unlock}')

    # ==================== 调用 ====================
    def chat(self, req: ModelRequest, kind: str = 'chat', session_key: str = '') -> ModelResult:
        # 按 kind 选渠道：提炼可以走和聊天不同的供应商（成本结构相反，见 Provider.kinds）
        candidates = self._ordered_pairs(kind)
        if not candidates:
            return ModelResult(ok=False, error_code='no_provider',
                               error_message='未配置任何模型渠道')
        start = time.time()
        last_error = 'unknown'
        last_message = ''
        # 总时长预算：5 个候选 × 2 轮 × (重试) × 单渠道超时，最坏能到 8 分钟，
        # 而调度器的看门狗是 300 秒 —— 会先把这次任务当成"卡死"再踢掉。
        budget = float(self._failover_cfg.get('total_timeout_seconds', 60) or 0)
        deadline = (start + budget) if budget > 0 else 0.0
        # 骰主口径（2026-11-01）：「第 5 个挂了就重回第一个开始连接」——
        # 一轮走到底不算完，要整轮重来一次（failover_full_cycles，默认 2 轮）。
        # 只有一个候选时不做第二轮：_call_provider 内部已经重试过了，纯浪费延迟。
        cycles = max(1, int(self._failover_cfg.get('full_cycles', 2) or 1))
        if len(candidates) < 2:
            cycles = 1
        empty_retry_used = False
        for cycle in range(cycles):
            for provider, state in candidates:
                if deadline and time.time() > deadline:
                    last_error = last_error if last_error != 'unknown' else 'deadline'
                    logger.warning('模型调用总时长超过 %.0fs（%s），停止本次故障转移',
                                   budget, last_error)
                    break
                # 第二轮跳过「key 无效」这类不会自己好的渠道：重试一次只是白等。
                # 超时 / 5xx / 空回复仍然照骰主的意思再撞一次（可能是瞬时抖动）。
                if cycle and state.last_error in ('http_401', 'http_403', 'no_api_key'):
                    continue
                result = self._call_provider(provider, state, req, kind, session_key)
                if result.ok:
                    if cycle:
                        logger.info('第 %d 轮才连通：%s（前一轮全军覆没后才接上）',
                                    cycle + 1, provider.name)
                    return result
                last_error = result.error_code or last_error
                last_message = result.error_message or last_message
                if not is_provider_fault(last_error):
                    # 空回复（思考链吃光预算）**不是渠道故障**，换一个模型再试一次还有救；
                    # 4xx 是这一次请求本身有问题，换渠道问同一件事还是同样的错，立刻收手。
                    # 两种情况都**不判渠道死、不升级兜底、不给骰主发「全挂」**。
                    if last_error == 'empty_content' and not empty_retry_used:
                        empty_retry_used = True
                        logger.warning('空回复（非渠道故障），换下一个渠道再试一次：%s',
                                       provider.name)
                        continue
                    logger.warning('请求级错误 %s，停止故障转移（不判渠道失败）：%s',
                                   last_error, provider.name)
                    return ModelResult(
                        ok=False, model=req.model,
                        latency_ms=int((time.time() - start) * 1000),
                        error_code=last_error, error_message=last_message,
                    )
            if cycle + 1 < cycles:
                logger.warning('第 %d 轮 %d 个渠道全失败（%s），回到第一个渠道再来一轮',
                               cycle + 1, len(candidates), last_error)
            if deadline and time.time() > deadline:
                break
        if not is_provider_fault(last_error):
            # 兜底：到这一步说明是请求/内容问题，不该触发「全挂」流程
            return ModelResult(
                ok=False, model=req.model,
                latency_ms=int((time.time() - start) * 1000),
                error_code=last_error, error_message=last_message,
            )
        # 两轮都没活口
        if self._hold_name:
            # 已经锁在兜底渠道上还失败：按冷却通知，不做二次降级
            self._notify_all_down(last_error)
        else:
            self._handle_all_down(last_error)
        return ModelResult(
            ok=False,
            model=req.model,
            latency_ms=int((time.time() - start) * 1000),
            error_code=last_error,
            error_message=last_message,
        )

    # ==================== 全挂：先软降级，再决定要不要锁 ====================
    def _handle_all_down(self, error_code: str) -> None:
        """候选全挂后的第一反应：**先不锁定**。

        2026-11-12 骰主口径：中转/反代渠道本来就爱抖，一次「两轮全挂」常常是十几秒的
        链路波动——以前这时立刻通知 + 锁定，等于每次抖动都把机器人钉在贵的官方渠道上，
        得等人手动切回。现在：

        1. 记下 `pending_hold_at = now + retry_before_hold_seconds`（默认 300 秒），
           期间**不通知、不锁定**；当前渠道先切到兜底那份，保证这几分钟还有话可回；
        2. 到点由探测线程从锚点开始重试（`retry_pending_once`）；
        3. 重试成功 → 直接切回去，当成没发生过；
        4. 重试仍失败 → 才走 `_escalate_to_last_resort`（通知 + 锁定），也就是老行为。

        `retry_before_hold_seconds = 0` 时退回 2026-11-01 的「立刻锁定」。
        """
        cfg = self._failover_cfg
        try:
            wait = float(cfg.get('retry_before_hold_seconds', 300) or 0)
        except (TypeError, ValueError):
            wait = 300.0
        if wait <= 0:
            self._escalate_to_last_resort(error_code)
            return
        now = time.time()
        with self._lock:
            first = not self._pending_hold_at
            if first:
                self._pending_hold_at = now + wait
                self._pending_error = error_code
                self._pending_from = self._current_name
                logger.warning('候选渠道全挂（%s）：先不锁定，%d 秒后从锚点重试；'
                               '期间继续用兜底渠道回话', error_code, int(wait))
            else:
                # 冷却期内的再次全挂**不重置**倒计时：否则晚上一直有人聊，
                # 这个定时器永远到不了点，等于永不重试。
                logger.info('候选渠道仍然全挂（%s），%d 秒后按原计划重试',
                            error_code, max(0, int(self._pending_hold_at - now)))
            self._soft_switch_to_last_resort_locked(error_code)
            self._persist_locked()

    def _soft_switch_to_last_resort_locked(self, error_code: str) -> None:
        """切到兜底渠道但**不锁定**（调用方持锁）。

        与 `_escalate_to_last_resort` 的唯一区别就是 `_hold_name` 保持空：
        下一次调用仍会从兜底渠道开始，但别的渠道也还在候选里，
        真恢复了自然就切回去了。
        """
        name = self._last_resort_name()
        if not name:
            return
        target = next((p for p, _ in self._pairs if p.name == name), None)
        if target is None or self._current_name == name:
            return
        old = self._current_name
        self._current_name = name
        state = next(st for p, st in self._pairs if p.name == name)
        state.healthy = True
        state.fail_count = 0
        state.last_error = ''
        self._append_history_locked(old, name, 'all_down_soft', error_code)
        logger.warning('已临时切到兜底渠道 %s（未锁定，%s）', name, error_code)

    def pending_retry_at(self) -> float:
        """全挂后计划重试的时间戳；0 = 没有待重试。"""
        with self._lock:
            return self._pending_hold_at

    def _anchor_retry_targets(self) -> list[tuple[Provider, ProviderState]]:
        """待重试时的候选顺序：锚点排第一，其余按优先级（绝不越过锚点往上）。

        **锚点即使标了 `manual_only` 也要试**：骰主手动切过去的那条本来就可能带这个标记
        （线上 official-chat 就是 manual_only），把它过滤掉等于「从锚点重试」却一次都不碰锚点。
        """
        with self._lock:
            pairs = list(self._pairs)
            anchor = self._anchor_name
            ceiling = self._anchor_priority_locked()
        auto = [(p, st) for p, st in pairs if (not p.manual_only) or p.name == anchor]
        if anchor:
            scoped = [(p, st) for p, st in auto if ceiling is None or p.priority >= ceiling]
            if scoped:
                auto = scoped
        ordered = sorted(auto, key=lambda x: (0 if x[0].name == anchor else 1,
                                              x[0].priority, x[0].name))
        if not ordered:
            ordered = sorted(pairs, key=lambda x: (x[0].priority, x[0].name))
        return ordered

    def retry_pending_once(self) -> str:
        """执行一次「全挂后的重试」。返回 'recovered' / 'held' / 'cancelled' / ''。"""
        with self._lock:
            if not self._pending_hold_at:
                return ''
            epoch = self._manual_epoch
            self._pending_hold_at = 0.0
            error = self._pending_error or 'retry'
            self._pending_error = ''
        targets = self._anchor_retry_targets()
        logger.warning('全挂重试：从锚点开始试 %d 个渠道（%s）',
                       len(targets), '、'.join(p.name for p, _ in targets) or '（无）')
        for provider, state in targets:
            with self._lock:
                if self._manual_epoch != epoch:
                    # 骰主在这轮探测期间手动切了渠道：他的话比后台判断优先，
                    # 这次重试作废——**不锁定、不通知**，按新渠道继续跑
                    logger.info('全挂重试期间骰主手动切换了渠道，放弃本次重试（不锁定、不通知）')
                    return 'cancelled'
            if not self._probe_provider(provider):
                continue
            with self._lock:
                if self._manual_epoch != epoch:
                    logger.info('探测刚通过时骰主手动切换了渠道，按手动选择为准')
                    return 'cancelled'
                state.healthy = True
                state.fail_count = 0
                state.last_error = ''
                old = self._current_name
                self._current_name = provider.name
                self._pending_from = ''
                self._append_history_locked(old, provider.name, 'retry_recover',
                                            '全挂重试成功')
                self._persist_locked()
            logger.info('全挂重试成功：已切回 %s（未锁定、未通知骰主）', provider.name)
            return 'recovered'
        with self._lock:
            if self._manual_epoch != epoch:
                logger.info('全挂重试期间骰主手动切换了渠道，放弃本次重试（不锁定、不通知）')
                return 'cancelled'
        logger.warning('全挂重试仍然失败（%s），开始通知骰主并锁定兜底渠道', error)
        self._escalate_to_last_resort(error)
        return 'held'

    def _escalate_to_last_resort(self, error_code: str) -> None:
        """候选渠道全挂 → 通知骰主 + 切到兜底渠道并锁定（等人手动切回）。

        全挂重试失败后通知并切到兜底渠道，等待操作者手动选择恢复目标。

        实现要点：
        - **锁定**（`gw.hold`）而不是普通锚点：官方 DeepSeek 排在最后，普通锚点的
          「不许越过它往上」语义在这里会被 "之下没有渠道" 的兜底逻辑绕过；
        - 锁定期间 `probe_once` 不再自动爬回，`_switch_locked` 也不再把渠道切走；
        - 解除只有一条路：骰主「切换渠道 X」或「切换渠道 自动」。
        """
        name = self._last_resort_name()
        if not name:
            self._notify_all_down(error_code)
            return
        with self._lock:
            target = next((p for p, _ in self._pairs if p.name == name), None)
            if target is None:
                # 没配兜底渠道：退回老行为（只发通知）
                self._notify_all_down(error_code)
                return
            if self._hold_name == name and self._current_name == name:
                self._notify_all_down(error_code)
                return
            old = self._current_name
            self._current_name = name
            self._hold_name = name
            # 2026-11-12：**不再清掉手动锚点**。锁定期间 `_ordered_pairs` 只看 hold，
            # 锚点本来就无效；清掉只会让骰主解锁后发现「我设的锚点没了」。
            self._pending_hold_at = 0.0      # 已经真锁了，待重试状态收掉
            self._pending_error = ''
            state = next(st for p, st in self._pairs if p.name == name)
            state.healthy = True
            state.fail_count = 0
            state.last_error = ''
            self._append_history_locked(old, name, 'all_down', error_code)
            self._persist_locked()
        logger.warning('候选渠道两轮全挂（%s），已切到兜底渠道 %s 并锁定；'
                       '「切换渠道 X」可解除', error_code, name)
        cfg = self._failover_cfg
        if not cfg.get('notify_owner', True) or self._notify_cb is None:
            return
        tried = '、'.join(dict.fromkeys(p.name for p, _ in self._pairs)) or '（无）'
        # 走统一入口：以前这里直接 `_notify_cb(...)`，**绕过 600 秒冷却**——
        # 结果是每挂一次发一条（19:37、19:38、19:39 连着三条），看起来像"老是掉线"。
        # 手动切回会让 _hold_name 清空，下一次全挂又走这条路径，冷却形同虚设。
        self._notify_all_down(
            error_code,
            detail=(f'已自动切到兜底渠道「{name}」并锁定，不再尝试其它渠道。\n'
                    f'想切回：在群里发「切换渠道 <名称>」（先「渠道状态」看列表）。'),
            tried=tried,
        )

    def _call_provider(self, provider: Provider, state: ProviderState,
                       req: ModelRequest, kind: str, session_key: str) -> ModelResult:
        cfg = self.config
        start = time.time()
        if not provider.api_key:
            code = 'no_api_key'
            result = ModelResult(ok=False, model=provider.model, error_code=code,
                                 error_message=f'渠道 {provider.name} 未配置 API Key')
            self._record(session_key, kind, result)
            self._mark_failure(provider, state, code, kind)
            return result

        url, body = self._build_request_body(provider, req)

        retries = int(cfg.get('max_retries', 1))
        last_error = ''
        with self._semaphore:
            session = self._get_session()
            for attempt in range(retries + 1):
                try:
                    resp = session.post(
                        url,
                        headers=self._headers(provider),
                        json=body,
                        timeout=provider.timeout,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        ok, content, input_tokens, output_tokens, cached_tokens, err_msg = self._parse_response(provider, data)
                        # 「有没有缓存字段」和「缓存命中多少」是两件事：前者为零时说明渠道没透传，
                        # 界面要能说清，不然优化提示词前缀会变成对着空气使劲。
                        cache_reported = usage_reports_cache(response_usage(data))
                        if not ok:
                            last_error = 'empty_content'
                            result = ModelResult(
                                ok=False, model=provider.model,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                cache_reported=cache_reported,
                                latency_ms=int((time.time() - start) * 1000),
                                error_code=last_error, error_message=err_msg or '模型没有输出内容',
                            )
                            self._record(session_key, kind, result)
                            self._mark_failure(provider, state, last_error, kind)
                            return result
                        result = ModelResult(
                            ok=True,
                            text=content,
                            model=provider.model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_tokens=cached_tokens,
                            cache_reported=cache_reported,
                            latency_ms=int((time.time() - start) * 1000),
                        )
                        self._record(session_key, kind, result)
                        self._mark_success(provider, state)
                        return result
                    if resp.status_code in AUTH_STATUS:
                        last_error = f'http_{resp.status_code}'
                        break  # key 无效，重试无意义
                    if resp.status_code in RETRYABLE_STATUS and attempt < retries:
                        last_error = f'http_{resp.status_code}'
                        time.sleep(min(2 ** attempt, 4) + 0.1 * attempt)
                        continue
                    last_error = f'http_{resp.status_code}'
                    break
                except requests.Timeout:
                    last_error = 'timeout'
                    if attempt < retries:
                        time.sleep(0.5 + 0.5 * attempt)
                        continue
                    break
                except requests.ConnectionError:
                    last_error = 'connection_error'
                    if attempt < retries:
                        time.sleep(0.5 + 0.5 * attempt)
                        continue
                    break
                except Exception as exc:  # noqa: BLE001 - 网关层吞掉所有网络异常
                    last_error = type(exc).__name__
                    break

        result = ModelResult(
            ok=False,
            model=provider.model,
            latency_ms=int((time.time() - start) * 1000),
            error_code=last_error or 'unknown',
            error_message=last_error,
        )
        self._record(session_key, kind, result)
        self._mark_failure(provider, state, result.error_code, kind)
        return result

    def _resolve_url(self, provider: Provider) -> str:
        url = provider.endpoint.strip()
        if provider.protocol == 'anthropic':
            if url.endswith('/messages'):
                return url
            elif url.endswith('/v1'):
                return f'{url}/messages'
            else:
                return f"{url.rstrip('/')}/v1/messages"
        return url

    def _headers(self, provider: Provider) -> dict:
        if provider.protocol == 'anthropic':
            headers = {
                'x-api-key': provider.api_key or 'local-proxy',
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json',
            }
            if provider.extra_headers:
                headers.update({str(k): str(v) for k, v in provider.extra_headers.items()})
            return headers

        headers = {'Authorization': f'Bearer {provider.api_key}'}
        if provider.extra_headers:
            headers.update({str(k): str(v) for k, v in provider.extra_headers.items()})
        elif provider.legacy:
            # 兼容模式：老配置走 opencode 时需要这个路由头
            headers['x-opencode-session'] = _OPCODE_SESSION_ID
        return headers

    def _build_request_body(self, provider: Provider, req: ModelRequest) -> tuple[str, dict]:
        url = self._resolve_url(provider)
        if provider.protocol == 'anthropic':
            system_parts = []
            anthropic_messages = []
            for m in req.messages:
                role = m.get('role')
                content = m.get('content')
                if role == 'system':
                    if isinstance(content, str) and content.strip():
                        system_parts.append(content.strip())
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get('text'):
                                system_parts.append(part['text'].strip())
                    continue

                if isinstance(content, list):
                    converted_blocks = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get('type')
                        if btype == 'text':
                            converted_blocks.append({'type': 'text', 'text': block.get('text', '')})
                        elif btype == 'image_url':
                            img_url = block.get('image_url', {}).get('url', '')
                            if img_url.startswith('data:'):
                                try:
                                    header, b64_data = img_url.split('base64,', 1)
                                    media_type = header.split(';')[0].replace('data:', '').strip()
                                    converted_blocks.append({
                                        'type': 'image',
                                        'source': {
                                            'type': 'base64',
                                            'media_type': media_type or 'image/jpeg',
                                            'data': b64_data.strip(),
                                        }
                                    })
                                except Exception:
                                    converted_blocks.append({'type': 'text', 'text': '[图片]'})
                            else:
                                converted_blocks.append({'type': 'text', 'text': f'[图片: {img_url}]'})
                    anthropic_messages.append({'role': role, 'content': converted_blocks})
                else:
                    anthropic_messages.append({'role': role, 'content': content or ''})

            # Anthropic 协议要求角色严格交替：合并连续同角色消息，防止 400 roles must alternate 错误
            merged_messages = []
            for m in anthropic_messages:
                if merged_messages and merged_messages[-1]['role'] == m['role']:
                    prev = merged_messages[-1]
                    prev_c = prev['content']
                    curr_c = m['content']
                    if isinstance(prev_c, str) and isinstance(curr_c, str):
                        prev['content'] = (prev_c + '\n\n' + curr_c).strip()
                    elif isinstance(prev_c, list) and isinstance(curr_c, list):
                        prev['content'].extend(curr_c)
                    elif isinstance(prev_c, list) and isinstance(curr_c, str):
                        prev['content'].append({'type': 'text', 'text': curr_c})
                    elif isinstance(prev_c, str) and isinstance(curr_c, list):
                        prev['content'] = [{'type': 'text', 'text': prev_c}] + curr_c
                else:
                    merged_messages.append(m)
            anthropic_messages = merged_messages

            if anthropic_messages and anthropic_messages[0]['role'] == 'assistant':
                anthropic_messages.insert(0, {'role': 'user', 'content': '...'})

            body = {
                'model': provider.model,
                'messages': anthropic_messages,
                'max_tokens': req.max_tokens or 4096,
            }
            if system_parts:
                body['system'] = '\n\n'.join(system_parts)
            if req.temperature is not None:
                body['temperature'] = req.temperature
            body.update(provider.extra_body)
            return url, body

        body = {
            'model': provider.model,
            'messages': req.messages,
            'temperature': req.temperature,
            'max_tokens': req.max_tokens,
        }
        body.update(provider.extra_body)
        return url, body

    def _parse_response(self, provider: Provider, data: dict) -> tuple[bool, str, int, int, int, str]:
        # usage 统一走 usage_tokens：Token 口径（总输入是否含缓存）只在这一处定义，
        # 三个协议分支不再各算一套，否则命中率的分母迟早又对不上。
        input_tokens, output_tokens, cached_tokens = usage_tokens(
            response_usage(data), provider.protocol)
        if provider.protocol == 'anthropic':
            raw_content = data.get('content', [])
            text_parts = []
            thinking_parts = []
            if isinstance(raw_content, list):
                for block in raw_content:
                    if isinstance(block, dict):
                        b_type = block.get('type')
                        if b_type == 'text':
                            text_parts.append(block.get('text', ''))
                        elif b_type == 'thinking':
                            thinking_parts.append(block.get('thinking', ''))
            elif isinstance(raw_content, str):
                text_parts.append(raw_content)

            text = ''.join(text_parts).strip()
            thinking = ''.join(thinking_parts).strip()
            if thinking:
                logger.debug('渠道 %s [%s] 思考链输出 (len=%d): %s',
                             provider.name, provider.model, len(thinking), thinking[:300])

            if not text:
                msg = '思考链耗尽 max_tokens' if (thinking and data.get('stop_reason') == 'max_tokens') else '模型没有输出内容'
                return False, '', input_tokens, output_tokens, cached_tokens, msg
            if _UPSTREAM_SAFETY_RE.search(text):
                logger.warning('渠道 %s [%s] 触发上游安全策略拦截：%s', provider.name, provider.model, text[:120])
                return False, '', input_tokens, output_tokens, cached_tokens, 'upstream_safety_block'
            return True, text, input_tokens, output_tokens, cached_tokens, ''

        choices = data.get('choices') or []
        message = (choices[0].get('message') if choices else None) or {}
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            return False, '', input_tokens, output_tokens, cached_tokens, '模型没有输出内容'
        if _UPSTREAM_SAFETY_RE.search(content):
            logger.warning('渠道 %s [%s] 触发上游安全策略拦截：%s', provider.name, provider.model, content[:120])
            return False, '', input_tokens, output_tokens, cached_tokens, 'upstream_safety_block'
        return True, content.strip(), input_tokens, output_tokens, cached_tokens, ''

    def _get_session(self) -> requests.Session:
        s = getattr(self._local, 'session', None)
        if s is None:
            s = requests.Session()
            # 宿主系统代理可能指向失效的本地代理；模型 API 需走已验证的直连链路。
            s.trust_env = False
            s.headers.update({
                'Content-Type': 'application/json',
                'User-Agent': 'newGeYi/2.0',
            })
            self._local.session = s
        return s

    @staticmethod
    def resolve_api_key(config: dict) -> str:
        """单渠道模式的 Key 解析：环境变量 GEYI_API_KEY > config.json 的 api_key。

        多渠道模式下每个渠道用自己的 api_key（或 GEYI_KEY_<渠道名> 环境变量）。
        """
        env_key = os.environ.get('GEYI_API_KEY', '').strip()
        if env_key:
            return env_key
        return str(config.get('api_key', '') or '').strip()

    # ==================== 健康状态 ====================
    def _mark_success(self, provider: Provider, state: ProviderState) -> None:
        with self._lock:
            changed = state.fail_count or not state.healthy or state.cooldown_until
            state.fail_count = 0
            state.healthy = True
            state.probe_ok_streak = 0
            state.cooldown_until = 0.0
            state.last_ok_at = time.time()
            if self._pending_hold_at and provider.name != self._last_resort_name():
                # 冷却期内真的有调用通了：链路自己恢复了，撤掉「待锁定」
                self._pending_hold_at = 0.0
                self._pending_error = ''
                self._pending_from = ''
                logger.info('渠道 %s 调用成功，取消全挂待锁定状态', provider.name)
                changed = True
            # 兜底渠道自己通了**不算**链路恢复（2026-11-12）：待重试期间我们被软切到兜底
            # 渠道上，它当然会成功；一旦撤掉定时器，`probe_once` 又因为「当前是 manual_only
            # 且非锚点」完全不动手，机器人就会安静地一直挂在官方 DeepSeek 上花钱。留着定时
            # 器，到点照常探测一轮：真恢复就切回自动链路，没恢复才通知骰主并锁定兜底。
            if changed:
                self._persist_locked()

    def _mark_failure(self, provider: Provider, state: ProviderState, error_code: str,
                      kind: str = '') -> None:
        # 只有**渠道自己的问题**才算失败：空回复是模型输出问题（思考模式耗尽 max_tokens），
        # 4xx 是这次请求本身有问题。以前这两类都被记成渠道故障 → 两轮之后切兜底 + 锁定 +
        # 给骰主发「渠道全挂」，一个坏请求就能造成这个后果（2026-11-03 审查发现）。
        if not is_provider_fault(error_code):
            logger.warning('请求级错误（%s）不计入渠道健康：%s', error_code, provider.name)
            return
        # 后台提炼任务（memory_curate）的**超时/连接抖动不算渠道故障**（2026-11-07）。
        # 为什么：提炼是全量数据里失败率最高的一类（17409 次调用失败 28%，其中 502/504/
        # 连接/超时占了大头），而它跟"用户这条消息能不能回"没有必然关系。
        # 以前它一次超时（60s×2 重试=120s）就把整条渠道判死 5 分钟，
        # 聊天明明好的，却因为提炼慢被一起判了——这正是「显示全部掉线」的主要来源。
        # 认证类（401/403）和 5xx 仍然计入：那些是渠道德问题，得让人知道。
        if kind in BACKGROUND_KINDS and error_code in ('timeout', 'connection_error'):
            logger.info('后台任务 %s 的 %s 不计入渠道健康：%s', kind, error_code, provider.name)
            with self._lock:
                state.last_error = error_code
                state.last_fail_at = time.time()
                self._persist_locked()
            return
        # 链路抖动容忍（2026-11-12）：刚成功过的渠道隔几十秒又来一次超时/连不上，
        # 大概率是这条路抖了一下，不是渠道坏了。中转站和反代的实测就是这个样子
        # （晚上 40~70 秒的正常响应、偶发 connection reset）。宽限期内先不计数，
        # 只留一行 INFO；连续抖到宽限期外才照常累计。
        try:
            grace = float(self._failover_cfg.get('failure_grace_seconds', 90) or 0)
        except (TypeError, ValueError):
            grace = 90.0
        if (grace > 0 and error_code in ('timeout', 'connection_error')
                and state.last_ok_at and (time.time() - state.last_ok_at) < grace):
            logger.info('渠道 %s %.0f 秒前刚成功过，这次 %s 先不计故障',
                        provider.name, time.time() - state.last_ok_at, error_code)
            with self._lock:
                state.last_error = error_code
                state.last_fail_at = time.time()
                self._persist_locked()
            return
        threshold = int(self._failover_cfg.get('fail_threshold', 1) or 1)
        immediate = error_code in ('http_401', 'http_403', 'no_api_key')
        with self._lock:
            state.fail_count += 1
            state.last_error = error_code
            state.last_fail_at = time.time()
            became_unhealthy = False
            if state.healthy and (immediate or state.fail_count >= threshold):
                state.healthy = False
                became_unhealthy = True
            # 冷却到期后被重新召回、结果又失败：重新关起来并翻倍。
            # （以前只在「首次判死」那一瞬间设冷却，召回后一失败就再也不关，
            # 死渠道又会跟着每条消息各等一次 60 秒超时。）
            if not state.healthy and state.cooldown_until <= time.time():
                base, cap = self._cooldown_cfg()
                if immediate:
                    delay = cap          # 401/403/key 缺失不会自己好，直接关满
                else:
                    over = max(0, state.fail_count - threshold)
                    delay = min(cap, base * (2 ** min(over, 6)))
                if delay > 0:            # 0 = 熔断关闭，保持老行为
                    state.cooldown_until = time.time() + delay
                    logger.warning('渠道 %s 判为不可用（%s x%d），移出候选队列 %.0f 秒',
                                   provider.name, error_code, state.fail_count, delay)
            if became_unhealthy and provider.name == self._current_name:
                self._switch_locked('auto', f'{error_code} x{state.fail_count}')
            self._persist_locked()

    def _switch_locked(self, reason: str, detail: str = '') -> bool:
        """切到下一个可用渠道（调用方需持锁）。

        顺序：健康、不是当前渠道、**不越过手动锚点**，按 priority 从小到大。
        `manual_only` 的可选渠道不是自动切换的目标；锚点下方全挂时才越过锚点兜底。
        """
        with self._lock:
            # 锁定兜底渠道期间不做任何自动切换：骰主要的就是「别再尝试」，等人手动切回
            if self._hold_name:
                return False
            pairs = list(self._pairs)
            ceiling = self._anchor_priority_locked()

        def pick(seq):
            for provider, state in sorted(seq, key=lambda x: (x[0].priority, x[0].name)):
                if provider.manual_only or not state.healthy:
                    continue
                if provider.name == self._current_name:
                    continue
                return provider
            return None

        target = None
        if ceiling is not None:
            target = pick([p for p in pairs if p[0].priority >= ceiling])
            if target is None:
                logger.warning('手动锚点 %s 以下没有健康渠道，越过锚点兜底',
                               self._anchor_name)
        if target is None:
            target = pick(pairs)
        if target is None:
            return False
        old = self._current_name
        self._current_name = target.name
        self._append_history_locked(old, target.name, reason, detail)
        self._persist_locked()
        logger.warning('渠道切换：%s → %s（%s %s）', old, target.name, reason, detail)
        return True

    def _append_history_locked(self, old: str, new: str, reason: str, detail: str = '') -> None:
        self._history.append({
            'ts': time.time(),
            'from': old or '-',
            'to': new or '-',
            'reason': reason,
            'detail': detail,
        })
        limit = int(self._failover_cfg.get('switch_history_limit', 50) or 50)
        if len(self._history) > limit:
            self._history = self._history[-limit:]

    def _notify_all_down(self, error_code: str, detail: str = '', tried: str = '') -> None:
        cfg = self._failover_cfg
        if not cfg.get('notify_owner', True) or self._notify_cb is None:
            return
        cooldown = int(cfg.get('notify_cooldown_seconds', 600) or 0)
        now = time.time()
        with self._lock:
            if now - self._last_down_notify < cooldown:
                logger.info('全挂通知在冷却期内（%ds），跳过', int(cooldown - (now - self._last_down_notify)))
                return
            self._last_down_notify = now
        names = tried or ('、'.join(p.name for p, _ in self._pairs) or '（无）')
        try:
            self._notify_cb(
                f'🚨 所有模型渠道都连不上了（最后一次错误：{error_code}）\n'
                f'渠道：{names}\n'
                + (detail or '歌伊暂时无法回复，恢复后会自动继续。可用「渠道状态」查看详情。')
            )
        except Exception:  # noqa: BLE001 - 通知失败不影响主流程
            logger.exception('通知骰主失败')

    # ==================== 恢复探测 ====================
    def start_probe(self) -> None:
        """启动后台探测线程（幂等）。"""
        if self._probe_thread is not None and self._probe_thread.is_alive():
            return
        self._probe_stop.clear()
        self._probe_thread = threading.Thread(
            target=self._probe_loop, name='geyi-probe', daemon=True
        )
        self._probe_thread.start()
        # 2026-11-12：以前探测线程起来后**一行日志都没有**，出问题时无法判断
        # 「它到底在跑吗」。这行在 init_after 里打，日志桥已经装好了。
        cfg = self._failover_cfg
        logger.info('渠道探测已启动：每 %s 秒一轮，单次超时 %s 秒，失败重试 %s 次，'
                    '刚成功过 %s 秒内的抖动不计故障',
                    int(cfg.get('probe_interval_seconds', 900) or 900),
                    cfg.get('probe_timeout_seconds', 10),
                    int(cfg.get('probe_retry', 1) or 0),
                    int(cfg.get('failure_grace_seconds', 90) or 0))

    def stop_probe(self) -> None:
        self._probe_stop.set()
        thread = self._probe_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _probe_loop(self) -> None:
        interval = max(30, int(self._failover_cfg.get('probe_interval_seconds', 900) or 900))
        # 启动后先短等一次，好让「重启前就挂着」的渠道尽快被验证
        wait_for = min(interval, 60)
        while not self._probe_stop.wait(wait_for):
            wait_for = interval
            try:
                # 全挂待重试优先：到点就重试，没到点就睡到点（不要等满一个探测周期）
                pending = self.pending_retry_at()
                if pending:
                    remaining = pending - time.time()
                    if remaining > 0:
                        wait_for = min(interval, max(5.0, remaining))
                        continue
                    self.retry_pending_once()
                    continue
                recovered = self.probe_once()
                if recovered:
                    logger.info('渠道已恢复并切回：%s', ', '.join(recovered))
            except Exception:  # noqa: BLE001 - 探测线程永不退出
                logger.exception('渠道探测异常')

    def probe_once(self) -> list[str]:
        """探测「比当前渠道优先级更高」的渠道；恢复则切回去。

        只探测优先级更高的渠道：备用渠道坏掉不影响当前使用，没必要为它花钱。

        三种情况下完全不介入：
        - 当前渠道是 `manual_only` 手动渠道、且它不是锚点（老语义：不抢回自动链路）；
        - 候选是 `manual_only` 渠道、且它不是锚点——可选渠道永不自动启用；
        - 候选站在**手动锚点之上**——一律不碰（例如额度已用完的反代）。

        有锚点时目标就是锚点：按 priority 从高到低逐个试，**第一个通的立刻切过去
        并停下**，剩下的留到下一轮。所以「隔一阵子自动爬回手动设置的渠道」是逐级
        发生的，不会一次跳到底，也不会越过锚点。

        往上爬完之后，还会顺带体检几个「比当前低、但还挂着不可用标签」的渠道，
        只复位健康状态、不切换（见 probe_repair_per_round）。
        """
        cfg = self._failover_cfg
        need = max(1, int(cfg.get('probe_success_needed', 1) or 1))
        with self._lock:
            if self._hold_name:
                # 锁定兜底渠道期间不探测、不爬回：这是骰主明说的「不再尝试」
                return []
            pairs = list(self._pairs)
            current = self._current_name
            anchor = self._anchor_name
            current_p = next((p for p, _ in pairs if p.name == current), None)
            current_rank = current_p.priority if current_p is not None else 10 ** 6
            ceiling = self._anchor_priority_locked()

        if current_p is not None and current_p.manual_only and current_p.name != anchor:
            return []  # 手动选中的可选渠道：不被自动链路抢回

        targets = []
        for provider, state in pairs:
            if provider.name == current or provider.priority >= current_rank:
                continue
            if provider.manual_only and provider.name != anchor:
                continue  # 可选渠道不参与自动恢复
            if ceiling is not None and provider.priority < ceiling:
                continue  # 不越过手动锚点
            if state.cooldown_until > time.time():
                continue  # 熔断冷却还没到期：到点再召回，不白花探测时间
            targets.append((provider, state))
        targets.sort(key=lambda x: (x[0].priority, x[0].name))

        recovered: list[str] = []
        failed_probes: list[str] = []
        for provider, state in targets:
            # 已经健康、只是当前站在它下面的渠道：直接切回，不用再花钱探测
            if not provider.manual_only and state.healthy:
                with self._lock:
                    old = self._current_name
                    self._current_name = provider.name
                    self._append_history_locked(old, provider.name, 'priority_recover')
                    self._persist_locked()
                    recovered.append(provider.name)
                break

            ok = self._probe_provider(provider)
            switched = False
            with self._lock:
                state.last_probe_at = time.time()
                if ok:
                    state.probe_ok_streak += 1
                else:
                    state.probe_ok_streak = 0
                    state.last_error = state.last_error or 'probe_failed'
                if ok and state.probe_ok_streak >= need:
                    state.healthy = True
                    state.fail_count = 0
                    state.last_error = ''
                    old = self._current_name
                    self._current_name = provider.name
                    self._append_history_locked(old, provider.name, 'probe_recover')
                    self._persist_locked()
                    recovered.append(provider.name)
                    switched = True
                elif not ok:
                    failed_probes.append(provider.name)
            if switched:
                break
        if recovered:
            self._persist_locked()
        if failed_probes:
            # 以前探测失败**一条日志都没有**：看不出「它在跑」还是「它早死了」。
            # 只在这一轮真的发过请求且没通过时打一行，频率很低（每轮最多一次）。
            logger.info('探测未通过：%s（下一轮再试；429 视为活着）',
                        '、'.join(dict.fromkeys(failed_probes)))

        # 体检：顺手复位「比当前低、却还挂着不可用标签」的渠道。
        # 不复位的话标志会永远留着——真需要它兜底时会被排到更差的渠道后面，
        # 「按顺序往下开」就名不副实了。每轮最多查 probe_repair_per_round 个，
        # 按上次探测时间从旧到新，**不切换渠道**。
        limit = max(0, int(cfg.get('probe_repair_per_round', 2) or 0))
        if limit:
            with self._lock:
                cur_name = self._current_name
                cur_p2 = next((p for p, _ in pairs if p.name == cur_name), None)
                cur_rank2 = cur_p2.priority if cur_p2 is not None else 10 ** 6
                stale = [
                    (p, st) for p, st in pairs
                    if not p.manual_only and not st.healthy
                    and p.name != cur_name and p.priority > cur_rank2
                    and st.cooldown_until <= time.time()   # 冷却中的先不动，到点再查
                ]
            stale.sort(key=lambda x: ((x[1].last_probe_at or 0), x[0].priority))
            for provider, state in stale[:limit]:
                ok = self._probe_provider(provider)
                with self._lock:
                    state.last_probe_at = time.time()
                    if ok:
                        state.healthy = True
                        state.fail_count = 0
                        state.last_error = ''
                        state.cooldown_until = 0.0
                        self._persist_locked()
                        logger.info('渠道 %s 体检通过，已复位为可用', provider.name)
                    else:
                        # 查了还是不通：再关一段时间，别每轮都去撞同一堵墙
                        base, _cap = self._cooldown_cfg()
                        state.cooldown_until = time.time() + base
                        self._persist_locked()
        return recovered

    def _probe_provider(self, provider: Provider) -> bool:
        """一次极轻量请求：只为验证渠道是否可达（成本可忽略）。

        2026-11-12 增加两条容忍（中转/反代夜里抖动很常见）：
        - **429 算活着**：被限流说明渠道是通的，只是忙；真要判死交给真实调用去累计；
        - **连接类抖动重试一次**：单次 reset / 超时不再直接判「未恢复」。
        """
        url, body = self._build_request_body(provider, ModelRequest(
            model=provider.model,
            messages=[{'role': 'user', 'content': PROBE_PROMPT}],
            max_tokens=1,
        ))
        cfg = self._failover_cfg
        timeout = float(cfg.get('probe_timeout_seconds', 10) or 10)
        try:
            ok_statuses = {int(x) for x in (cfg.get('probe_ok_statuses') or (200, 429))}
        except (TypeError, ValueError):
            ok_statuses = {200, 429}
        try:
            retries = max(0, int(cfg.get('probe_retry', 1) or 0))
        except (TypeError, ValueError):
            retries = 1
        for attempt in range(retries + 1):
            try:
                resp = self._get_session().post(
                    url,
                    headers=self._headers(provider),
                    json=body,
                    timeout=timeout,
                )
            except Exception:  # noqa: BLE001 - 连接类异常：先按抖动重试一次
                if attempt < retries:
                    time.sleep(min(2.0, 0.5 * (attempt + 1)))
                    continue
                return False
            if resp.status_code in ok_statuses:
                return True
            # 4xx（key 错、模型名错）重试没用；5xx 留给下一轮探测
            return False
        return False

    # ==================== 持久化 ====================
    def _persist_locked(self) -> None:
        try:
            self.store.set_setting(_KEY_CURRENT, self._current_name)
            self.store.set_setting(_KEY_ANCHOR, self._anchor_name)
            self.store.set_setting(_KEY_HOLD, self._hold_name)
            unhealthy = [p.name for p, st in self._pairs if not st.healthy]
            self.store.set_setting(_KEY_UNHEALTHY, json.dumps(unhealthy, ensure_ascii=False))
            self.store.set_setting(_KEY_HISTORY, json.dumps(self._history, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 持久化失败不影响主流程
            logger.exception('渠道状态持久化失败')

    def _apply_persisted_locked(self) -> None:
        """启动时恢复上次的当前渠道、手动锚点与不可用标记（失败不影响启动）。"""
        try:
            names = [p.name for p, _ in self._pairs]
            current = str(self.store.get_setting(_KEY_CURRENT) or '').strip()
            anchor = str(self.store.get_setting(_KEY_ANCHOR) or '').strip()
            hold = str(self.store.get_setting(_KEY_HOLD) or '').strip()
            unhealthy = json.loads(self.store.get_setting(_KEY_UNHEALTHY) or '[]')
            history = json.loads(self.store.get_setting(_KEY_HISTORY) or '[]')
        except Exception:  # noqa: BLE001
            return
        if isinstance(history, list):
            self._history = [item for item in history if isinstance(item, dict)]
        if hold and hold in names:
            # 锁定状态优先于其它一切：只认它一个
            self._hold_name = hold
            self._current_name = hold
            state = next(st for p, st in self._pairs if p.name == hold)
            state.healthy = True
            state.fail_count = 0
            logger.warning('已恢复「锁定兜底渠道」状态：%s（「切换渠道 X」可解除）', hold)
            return
        if anchor and anchor in names:
            self._anchor_name = anchor
        if current and current in names:
            self._current_name = current
        if isinstance(unhealthy, list):
            for provider, state in self._pairs:
                if provider.name in unhealthy:
                    state.healthy = False
                    state.last_error = 'restored_unhealthy'
        # 若当前渠道恰好是被标记不可用的，自动挪到第一个健康的
        cur = next((st for p, st in self._pairs if p.name == self._current_name), None)
        if cur is not None and not cur.healthy:
            self._switch_locked('restore', '上次的当前渠道不可用')

        # 当前渠道站在手动锚点之上（锚点改过、或老数据）：先退回锚点，否则锚点等于没设
        ceiling = self._anchor_priority_locked()
        cur_p = next((p for p, _ in self._pairs if p.name == self._current_name), None)
        if ceiling is not None and cur_p is not None and cur_p.priority < ceiling:
            anchor_p = next((p for p, st in self._pairs
                             if p.name == self._anchor_name and st.healthy), None)
            if anchor_p is not None:
                old = self._current_name
                self._current_name = anchor_p.name
                self._append_history_locked(old, anchor_p.name, 'anchor',
                                            '当前渠道高于手动锚点，退回锚点')
                self._persist_locked()
                cur_p = anchor_p

        # 启动/重载时：恢复更高的健康渠道，但**不越过手动锚点**
        # （若当前渠道是骰主手动选中、又不是锚点的 manual_only 渠道，就不抢回去）
        if cur_p is not None and cur_p.manual_only and cur_p.name != self._anchor_name:
            return
        cur_rank = cur_p.priority if cur_p else 10**6
        best_healthy = None
        for p, st in sorted(self._pairs, key=lambda x: (x[0].priority, x[0].name)):
            if p.manual_only and p.name != self._anchor_name:
                continue
            if ceiling is not None and p.priority < ceiling:
                continue
            if st.healthy and p.priority < cur_rank:
                best_healthy = p
                break
        if best_healthy is not None:
            old = self._current_name
            self._current_name = best_healthy.name
            self._append_history_locked(old, best_healthy.name, 'priority_restore',
                                        '启动/重载切回更高优先级渠道')
            self._persist_locked()

    # ==================== 指标 ====================
    def _record(self, session_key: str, kind: str, result: ModelResult) -> None:
        try:
            self.store.add_api_call(
                session_key=session_key[:120],
                kind=kind,
                model=result.model,
                in_tokens=result.input_tokens,
                out_tokens=result.output_tokens,
                cached_tokens=result.cached_tokens,
                cache_reported=result.cache_reported,
                latency_ms=result.latency_ms,
                ok=result.ok,
                error_code=result.error_code,
            )
        except Exception:  # noqa: BLE001 - 指标失败不影响主流程
            pass


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
