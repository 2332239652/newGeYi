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
PROBE_PROMPT = 'hi'

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
                 'extra_headers', 'extra_body', 'legacy', 'kinds', 'protocol')

    def __init__(self, name, endpoint, api_key, model, priority=1, timeout=25.0,
                 extra_headers=None, extra_body=None, legacy=False, kinds=None, protocol='openai'):
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
        return f'<Provider {self.name} {self.model} p{self.priority}{tag}>'


class ProviderState:
    """渠道运行时状态（随调用变化，部分会持久化）。"""

    __slots__ = ('name', 'fail_count', 'healthy', 'last_error', 'last_fail_at',
                 'last_ok_at', 'last_probe_at', 'probe_ok_streak')

    def __init__(self, name):
        self.name = name
        self.fail_count = 0
        self.healthy = True
        self.last_error = ''
        self.last_fail_at = 0.0
        self.last_ok_at = 0.0
        self.last_probe_at = 0.0
        self.probe_ok_streak = 0


def build_providers(config: dict) -> list[Provider]:
    """从配置构造渠道列表。

    - config['providers'] 非空：多渠道模式，每个渠道自己的 endpoint/key/model
    - 否则：退回 api_endpoint / api_key / fast_model 单渠道模式（兼容老配置）
    """
    default_timeout = float(config.get('request_timeout_seconds', 25))
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
            p_key = str(item.get('api_key') or '').strip()
            if p_key == 'sk-your-openai-api-key':
                p_key = ''
            api_key = p_key or _env_key_for(name) or LLMGateway.resolve_api_key(config)
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
        self._history: list[dict] = []
        self._last_down_notify = 0.0
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

    def set_notify(self, callback) -> None:
        """注入「通知骰主」的回调：callback(text) -> None。"""
        self._notify_cb = callback

    def _ordered_pairs(self, kind: str = '') -> list[tuple[Provider, ProviderState]]:
        """候选顺序：当前渠道 → 其他健康渠道 → 不健康渠道兜底（同级按 priority）。

        `kind` 非空时先按渠道的 `kinds` 过滤（提炼可以走另一家）。
        过滤后为空则**退回全部渠道**——配置写错不该让整个机器人哑掉，
        宁可走错渠道也不要一条都发不出去。
        """
        with self._lock:
            pairs = list(self._pairs)
            current = self._current_name

        if kind:
            scoped = [p for p in pairs if p[0].serves(kind)]
            if scoped:
                pairs = scoped
            else:
                logger.warning('没有渠道声明服务 %s，退回全部渠道', kind)

        def rank(pair):
            provider, state = pair
            return (
                0 if provider.name == current else 1,
                1 if not state.healthy else 0,
                provider.priority,
                provider.name,
            )

        return sorted(pairs, key=rank)

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
        if not pairs:
            return '未配置'
        provider = next((p for p, _ in pairs if p.name == current), pairs[0][0])
        unhealthy = [p.name for p, st in pairs if not st.healthy]
        text = f'{provider.name}（{provider.model}）'
        if unhealthy:
            text += f' | ⚠ 不可用: {"、".join(unhealthy)}'
        return text

    def status_text(self) -> str:
        """给命令用的渠道状态文本。"""
        with self._lock:
            pairs = list(self._pairs)
            current = self._current_name
            history = list(self._history)
        if not pairs:
            return '尚未配置任何模型渠道（config.json 的 providers 为空且缺少 api_endpoint/api_key）'
        lines = ['--- 模型渠道 ---']
        for index, (provider, state) in enumerate(pairs, 1):
            mark = ' ← 当前' if provider.name == current else ''
            health = '正常' if state.healthy else f'不可用({state.last_error or "未知"})'
            lines.append(
                f'{index}. {provider.name} | {provider.model} | 优先级{provider.priority} | '
                f'{health} | 连败{state.fail_count}{mark}'
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
        """手工切换渠道，返回结果文本。"""
        with self._lock:
            pairs = list(self._pairs)
            target = None
            key = str(name_or_index or '').strip()
            if key.isdigit() and 1 <= int(key) <= len(pairs):
                target = pairs[int(key) - 1][0]
            else:
                for provider, _ in pairs:
                    if provider.name == key:
                        target = provider
                        break
            if target is None:
                return f'找不到渠道：{key}（用「渠道状态」看可用列表）'
            if target.name == self._current_name:
                return f'当前已经是渠道 {target.name}'
            old = self._current_name
            self._current_name = target.name
            state = next(st for p, st in pairs if p.name == target.name)
            state.healthy = True
            state.fail_count = 0
            self._append_history_locked(old, target.name, 'manual')
            self._persist_locked()
            return f'✅ 已切换到渠道 {target.name}（{target.model}）'

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
        for provider, state in candidates:
            result = self._call_provider(provider, state, req, kind, session_key)
            if result.ok:
                return result
            last_error = result.error_code or last_error
            last_message = result.error_message or last_message
        # 所有渠道都失败
        self._notify_all_down(last_error)
        return ModelResult(
            ok=False,
            model=req.model,
            latency_ms=int((time.time() - start) * 1000),
            error_code=last_error,
            error_message=last_message,
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
            self._mark_failure(provider, state, code)
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
                        ok, content, input_tokens, output_tokens, err_msg = self._parse_response(provider, data)
                        if not ok:
                            last_error = 'empty_content'
                            result = ModelResult(
                                ok=False, model=provider.model,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                latency_ms=int((time.time() - start) * 1000),
                                error_code=last_error, error_message=err_msg or '模型没有输出内容',
                            )
                            self._record(session_key, kind, result)
                            self._mark_failure(provider, state, last_error)
                            return result
                        result = ModelResult(
                            ok=True,
                            text=content,
                            model=provider.model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
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
        self._mark_failure(provider, state, result.error_code)
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

            if anthropic_messages and anthropic_messages[0]['role'] == 'assistant':
                anthropic_messages.insert(0, {'role': 'user', 'content': '...'})

            # Anthropic 严格要求角色交替（user 与 assistant 轮流），合并连续同角色的消息
            merged_anthropic = []
            for am in anthropic_messages:
                if merged_anthropic and merged_anthropic[-1]['role'] == am['role']:
                    p_cnt = merged_anthropic[-1]['content']
                    c_cnt = am['content']
                    if isinstance(p_cnt, list) or isinstance(c_cnt, list):
                        p_list = p_cnt if isinstance(p_cnt, list) else [{'type': 'text', 'text': str(p_cnt)}]
                        c_list = c_cnt if isinstance(c_cnt, list) else [{'type': 'text', 'text': str(c_cnt)}]
                        merged_anthropic[-1]['content'] = p_list + c_list
                    else:
                        merged_anthropic[-1]['content'] = f"{p_cnt}\n{c_cnt}"
                else:
                    merged_anthropic.append(am)
            anthropic_messages = merged_anthropic

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

    def _parse_response(self, provider: Provider, data: dict) -> tuple[bool, str, int, int, str]:
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

            usage = data.get('usage') or {}
            input_tokens = int(usage.get('input_tokens') or 0)
            output_tokens = int(usage.get('output_tokens') or 0)

            if not text:
                msg = '思考链耗尽 max_tokens' if (thinking and data.get('stop_reason') == 'max_tokens') else '模型没有输出内容'
                return False, '', input_tokens, output_tokens, msg
            if _UPSTREAM_SAFETY_RE.search(text):
                logger.warning('渠道 %s [%s] 触发上游安全策略拦截：%s', provider.name, provider.model, text[:120])
                return False, '', input_tokens, output_tokens, 'upstream_safety_block'
            return True, text, input_tokens, output_tokens, ''

        choices = data.get('choices') or []
        message = (choices[0].get('message') if choices else None) or {}
        content = message.get('content')
        usage = data.get('usage') or {}
        input_tokens = int(usage.get('prompt_tokens', 0))
        output_tokens = int(usage.get('completion_tokens', 0))
        if not isinstance(content, str) or not content.strip():
            return False, '', input_tokens, output_tokens, '模型没有输出内容'
        if _UPSTREAM_SAFETY_RE.search(content):
            logger.warning('渠道 %s [%s] 触发上游安全策略拦截：%s', provider.name, provider.model, content[:120])
            return False, '', input_tokens, output_tokens, 'upstream_safety_block'
        return True, content.strip(), input_tokens, output_tokens, ''

    def _get_session(self) -> requests.Session:
        s = getattr(self._local, 'session', None)
        if s is None:
            s = requests.Session()
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
            changed = state.fail_count or not state.healthy
            state.fail_count = 0
            state.healthy = True
            state.probe_ok_streak = 0
            state.last_ok_at = time.time()
            if changed:
                self._persist_locked()

    def _mark_failure(self, provider: Provider, state: ProviderState, error_code: str) -> None:
        # empty_content 是模型输出问题（例如思考模式耗尽 max_tokens），不是渠道故障
        if error_code == 'empty_content':
            return
        threshold = int(self._failover_cfg.get('fail_threshold', 2) or 1)
        immediate = error_code in ('http_401', 'http_403', 'no_api_key')
        with self._lock:
            state.fail_count += 1
            state.last_error = error_code
            state.last_fail_at = time.time()
            became_unhealthy = False
            if state.healthy and (immediate or state.fail_count >= threshold):
                state.healthy = False
                became_unhealthy = True
            if became_unhealthy and provider.name == self._current_name:
                self._switch_locked('auto', f'{error_code} x{state.fail_count}')
            self._persist_locked()

    def _switch_locked(self, reason: str, detail: str = '') -> bool:
        """切到下一个健康渠道（调用方需持锁）。"""
        target = None
        for provider, state in sorted(self._pairs, key=lambda x: (x[0].priority, x[0].name)):
            if state.healthy and provider.name != self._current_name:
                target = provider
                break
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

    def _notify_all_down(self, error_code: str) -> None:
        cfg = self._failover_cfg
        if not cfg.get('notify_owner', True) or self._notify_cb is None:
            return
        cooldown = int(cfg.get('notify_cooldown_seconds', 600) or 0)
        now = time.time()
        with self._lock:
            if now - self._last_down_notify < cooldown:
                return
            self._last_down_notify = now
        names = '、'.join(p.name for p, _ in self._pairs) or '（无）'
        try:
            self._notify_cb(
                f'🚨 所有模型渠道都连不上了（最后一次错误：{error_code}）\n'
                f'渠道：{names}\n歌伊暂时无法回复，恢复后会自动继续。可用「渠道状态」查看详情。'
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
                recovered = self.probe_once()
                if recovered:
                    logger.info('渠道已恢复并切回：%s', ', '.join(recovered))
            except Exception:  # noqa: BLE001 - 探测线程永不退出
                logger.exception('渠道探测异常')

    def probe_once(self) -> list[str]:
        """探测「比当前渠道优先级更高、但被标记不可用」的渠道；恢复则切回。

        只探测优先级更高的渠道：备用渠道坏掉不影响当前使用，没必要为它花钱。
        """
        cfg = self._failover_cfg
        need = max(1, int(cfg.get('probe_success_needed', 1) or 1))
        with self._lock:
            pairs = list(self._pairs)
            current_rank = next(
                (p.priority for p, _ in pairs if p.name == self._current_name), 10 ** 6
            )
        recovered: list[str] = []
        for provider, state in pairs:
            if state.healthy or provider.priority >= current_rank:
                continue
            ok = self._probe_provider(provider)
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
                    old = self._current_name
                    self._current_name = provider.name
                    current_rank = provider.priority
                    self._append_history_locked(old, provider.name, 'probe_recover')
                    self._persist_locked()
                    recovered.append(provider.name)
        if recovered:
            self._persist_locked()
        return recovered

    def _probe_provider(self, provider: Provider) -> bool:
        """一次极轻量请求：只为验证渠道是否可达（成本可忽略）。"""
        url, body = self._build_request_body(provider, ModelRequest(
            model=provider.model,
            messages=[{'role': 'user', 'content': PROBE_PROMPT}],
            max_tokens=1,
        ))
        timeout = float(self._failover_cfg.get('probe_timeout_seconds', 10) or 10)
        try:
            resp = self._get_session().post(
                url,
                headers=self._headers(provider),
                json=body,
                timeout=timeout,
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - 探测失败即视为未恢复
            return False

    # ==================== 持久化 ====================
    def _persist_locked(self) -> None:
        try:
            self.store.set_setting(_KEY_CURRENT, self._current_name)
            unhealthy = [p.name for p, st in self._pairs if not st.healthy]
            self.store.set_setting(_KEY_UNHEALTHY, json.dumps(unhealthy, ensure_ascii=False))
            self.store.set_setting(_KEY_HISTORY, json.dumps(self._history, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 持久化失败不影响主流程
            logger.exception('渠道状态持久化失败')

    def _apply_persisted_locked(self) -> None:
        """启动时恢复上次的当前渠道与不可用标记（失败不影响启动）。"""
        try:
            names = [p.name for p, _ in self._pairs]
            current = str(self.store.get_setting(_KEY_CURRENT) or '').strip()
            unhealthy = json.loads(self.store.get_setting(_KEY_UNHEALTHY) or '[]')
            history = json.loads(self.store.get_setting(_KEY_HISTORY) or '[]')
        except Exception:  # noqa: BLE001
            return
        if isinstance(history, list):
            self._history = [item for item in history if isinstance(item, dict)]
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

    # ==================== 指标 ====================
    def _record(self, session_key: str, kind: str, result: ModelResult) -> None:
        try:
            self.store.add_api_call(
                session_key=session_key[:120],
                kind=kind,
                model=result.model,
                in_tokens=result.input_tokens,
                out_tokens=result.output_tokens,
                latency_ms=result.latency_ms,
                ok=result.ok,
                error_code=result.error_code,
            )
        except Exception:  # noqa: BLE001 - 指标失败不影响主流程
            pass


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
