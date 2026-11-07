# -*- encoding: utf-8 -*-
"""WebUI 的输入校验与脱敏；不执行网络或状态写入。"""

from __future__ import annotations

import copy
import math
import re

from . import config as plugin_config
from .routing.likelihood import DEFAULT_WEIGHTS


class PanelError(ValueError):
    def __init__(self, message: str, code: str = 'invalid_request'):
        super().__init__(message)
        self.code = code


BOOL_KEYS = {
    'private_enabled', 'private_trigger_only', 'passive_enabled', 'active_enabled', 'active_shadow', 'mention_on_at_all',
    'route_debug', 'worldbook_enabled', 'memory_enabled', 'memory_curator_enabled',
    'memory_private_enabled', 'memory_private_to_group',
    'image_enabled', 'emote_enabled', 'voice_enabled', 'rest_enabled', 'rest_private_forever',
    'panel_local_enabled',
    'galgame_enabled', 'galgame_shadow', 'galgame_owner_exempt',
    'reply_audit_enabled', 'religion_guard_enabled',
    'sleep_enabled', 'sleep_notice_once', 'sleep_owner_exempt',
    'sponsor_memory_keep',
    'memory_relevance_enabled', 'memory_temporal_anchor', 'memory_cross_group_enabled',
    'memory_impression_enabled', 'memory_diversity_enabled', 'time_awareness_enabled',
    'active_escalate_enabled', 'gate_scorer_enabled', 'group_quota_enabled',
    'collapse_short_multiline', 'strip_repeated_ellipsis', 'strip_trailing_period',
}
# 数字键 = (最小值, 最大值, 必须整数)。只开放确有热更新语义的字段。
NUMBER_KEYS = {
    'fast_temperature': (0, 2, False), 'fast_max_tokens': (1, 32768, True),
    'max_recent_turns': (1, 200, True), 'max_context_chars': (100, 200000, True),
    'max_reply_chars': (1, 10000, True), 'group_live_window_seconds': (1, 7200, True),
    'group_live_max_messages': (1, 200, True), 'group_live_max_chars': (100, 100000, True),
    'max_pending_per_session': (1, 100, True), 'request_timeout_seconds': (1, 120, False),
    'max_retries': (0, 3, True), 'daily_call_budget': (0, 1000000, True),
    'daily_output_token_budget': (0, 100000000, True),
    'memory_inject_facts': (1, 100, True), 'memory_inject_self': (1, 100, True),
    'memory_inject_summaries': (0, 30, True), 'memory_max_chars': (1, 100000, True),
    'memory_relevance_weight': (0, 1, False), 'memory_diversity_threshold': (0, 1, False),
    'memory_fuzzy_dedup_threshold': (0, 1, False), 'memory_max_facts_per_user': (1, 1000, True),
    'memory_max_facts_per_group': (1, 100000, True), 'memory_curate_messages': (1, 500, True),
    'memory_fact_candidates': (50, 5000, True), 'memory_video_share_keep': (0, 50, True),
    'private_min_interval': (0, 3600, False), 'private_burst_window': (0, 3600, True),
    'private_burst_max': (0, 1000, True), 'private_daily_quota': (0, 100000, True),
    'private_daily_quota_global': (0, 1000000, True),
    'private_quota_squeeze_at': (0, 1, False), 'private_quota_squeeze': (0, 100000, True),
    'memory_curate_idle_seconds': (0, 86400, True), 'memory_curate_max_messages': (1, 500, True),
    'memory_curate_min_interval': (1, 86400, True), 'memory_curate_max_tokens': (1, 8000, True),
    'memory_curate_max_tokens_cap': (1, 8000, True), 'memory_curate_tick_seconds': (1, 3600, True),
    'active_rate_target_per_100': (0, 100, False), 'active_daily_cap': (0, 10000, True),
    'active_min_self_gap': (0, 86400, False), 'active_rate_min_gap': (0, 86400, False),
    'active_rate_max_attempts_10min': (0, 1000, True), 'active_burst_max_attempts': (1, 3, True),
    'active_burst_max_seconds': (1, 120, False), 'active_base_cooldown': (0, 86400, True),
    'gate_min_score': (0, 1, False), 'gate_quantile_keep': (0, 1, False),
    'gate_score_window': (8, 1000, True), 'active_bare_image_factor': (0, 1, False),
    'rest_minutes': (0.1, 1440, False), 'voice_group_cooldown': (0, 86400, False),
    'panel_local_port': (0, 65535, True),
    'galgame_judge_min_chars': (1, 50, True), 'galgame_cross_memory_min_stage': (0, 5, True),
    'galgame_tag_daily_limit': (1, 5, True),
    'galgame_mild_gap': (0, 3, True),
    'voice_poke_rate': (0, 1, False), 'voice_speed': (0.5, 2, False), 'voice_timeout': (1, 120, False),
    'image_max_per_turn': (1, 10, True), 'image_max_bytes': (1024, 12582912, True),
    'image_total_max_bytes': (1024, 33554432, True), 'image_download_timeout': (1, 60, False),
    'image_download_max_bytes': (1024, 33554432, True), 'image_download_max_redirects': (0, 5, True),
    'group_quota_base': (0, 10000, False), 'group_quota_per_member': (0, 100, False),
    'group_quota_min': (0, 100000, True), 'group_quota_max': (0, 100000, True),
    'group_quota_default': (0, 100000, True),
    'sleep_pre_minutes': (0, 180, True),
    'private_daily_quota_sponsor': (0, 100000, True),
    'private_daily_quota_sponsor_tier1': (0, 100000, True),
    'private_daily_quota_sponsor_tier2': (0, 100000, True),
    'sponsor_fact_floor': (0, 100, True),
}
STRING_KEYS = {'assistant_name': 80, 'active_quiet_hours': 11, 'reply_audit_fallback': 2000,
               'galgame_judge_model': 120, 'affection_provider': 80,
               'sponsor_thanks_text': 200,
               'sleep_window': 11, 'sleep_notice_text': 120}
LIST_KEYS = {'triggers': (50, 80), 'owner_ids': (50, 40), 'ignore_sender_ids': (200, 40),
             'incomplete_tails': (100, 80), 'galgame_exempt_ids': (50, 40),
             'sponsor_ids': (500, 40), 'sponsor_tier1_ids': (500, 40),
             'sponsor_tier2_ids': (500, 40), 'beta_ids': (200, 40),
             'private_trigger_extra': (50, 40)}
# 难度数组：长度必须是 6（对应阶段 0~5），每项 (最小值, 最大值)；阈值还要求单调不减
GALGAME_ARRAYS = {'galgame_thresholds': (0, 300), 'galgame_min_days': (0, 365),
                  'galgame_daily_cap': (0, 100)}
EDITABLE_KEYS = sorted((BOOL_KEYS | set(NUMBER_KEYS) | set(STRING_KEYS) | set(LIST_KEYS)
                        | set(GALGAME_ARRAYS) | {'gate_weights', 'active_tier_probability'}) & set(plugin_config.DEFAULT_CONFIG))
SECRET_FIELDS = ['api_key', 'voice_auth_token', 'voice_onebot_access_token', 'extra_headers',
                 'providers[].api_key', 'providers[].extra_headers']
_SECRET_NAME = re.compile(r'(?:api[_-]?key|access[_-]?token|auth|password|secret|credential|authorization)', re.I)
_URL_AUTH = re.compile(r'(https?://)([^\s/?#]*@)', re.I)
_URL_QUERY = re.compile(r'(https?://[^\s?#]+)\?[^\s#]*', re.I)
_TEXT_SECRET = re.compile(
    r'((?:api[_-]?key|access[_-]?token|authorization|password|secret)\s*[=:]\s*)[^\s,;\]}]+', re.I)


class Redactor:
    """只持有用于过滤的字符串，不把配置/记录对象挂进日志缓存。"""

    def __init__(self):
        self.values: set[str] = set()

    def update(self, config: dict) -> None:
        def walk(value, sensitive=False):
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(child, sensitive or bool(_SECRET_NAME.search(str(key))) or str(key) == 'extra_headers')
            elif isinstance(value, (list, tuple)):
                for child in value:
                    walk(child, sensitive)
            elif isinstance(value, str):
                if sensitive and value:
                    self.values.add(value)
                    auth_parts = value.strip().split(None, 1)
                    if len(auth_parts) == 2 and auth_parts[0].lower() in ('bearer', 'basic'):
                        self.values.add(auth_parts[1])
                for auth in re.finditer(r'https?://([^\s/?#]+)@', value):
                    credentials = auth.group(1)
                    self.values.add(credentials)
                    if ':' in credentials:
                        self.values.add(credentials.split(':', 1)[1])
                for match in re.finditer(r'https?://([^\s/?#]*@)?[^\s?#]+\?([^\s#]*)', value):
                    if match.group(1):
                        self.values.add(match.group(1).rstrip('@'))
                    for pair in match.group(2).split('&'):
                        name, _, item = pair.partition('=')
                        if _SECRET_NAME.search(name) and item:
                            self.values.add(item)
        walk(config)

    def text(self, text) -> str:
        text = str(text)
        for secret in sorted(self.values, key=len, reverse=True):
            text = text.replace(secret, '[REDACTED]')
        text = _URL_AUTH.sub(r'\1', text)
        text = _URL_QUERY.sub(r'\1?[REDACTED]', text)
        return _TEXT_SECRET.sub(r'\1[REDACTED]', text)

    def scrub(self, value):
        if isinstance(value, dict):
            return {str(key): self.scrub(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(child) for child in value]
        return self.text(value) if isinstance(value, str) else value

    def clean(self, value):
        if isinstance(value, dict):
            return {str(key): self.clean(child) for key, child in value.items()
                    if not _SECRET_NAME.search(str(key)) and str(key) != 'extra_headers'}
        if isinstance(value, (list, tuple)):
            return [self.clean(child) for child in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def integer(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise PanelError('%s 必须是 %d 至 %d 的整数' % (name, low, high))
    return value


def text_value(value, name: str, cap: int, empty=True) -> str:
    if not isinstance(value, str) or len(value) > cap or '\x00' in value or (not empty and not value.strip()):
        raise PanelError('%s 文本格式或长度无效' % name)
    return value


def number(value, name: str, low, high, integral=False):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise PanelError('%s 数值应在 %s 至 %s 之间' % (name, low, high))
    if integral and type(value) is not int:
        raise PanelError('%s 必须是整数' % name)
    return value


def validate_patch(patch: dict, current: dict) -> dict:
    if not isinstance(patch, dict) or len(patch) > len(EDITABLE_KEYS):
        raise PanelError('patch 必须是配置对象')
    unknown = set(patch) - set(EDITABLE_KEYS)
    if unknown:
        raise PanelError('包含未开放编辑的配置字段', 'unknown_key')
    candidate = copy.deepcopy(current)
    for key, value in patch.items():
        if key in BOOL_KEYS:
            if type(value) is not bool:
                raise PanelError('%s 必须是布尔值' % key)
        elif key in NUMBER_KEYS:
            number(value, key, *NUMBER_KEYS[key])
        elif key in STRING_KEYS:
            text_value(value, key, STRING_KEYS[key])
            if key in ('active_quiet_hours', 'sleep_window') and value and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d', value):
                raise PanelError('时段格式为 HH:MM-HH:MM（例如 23:30-07:00）')
        elif key in LIST_KEYS:
            count, cap = LIST_KEYS[key]
            if not isinstance(value, list) or len(value) > count:
                raise PanelError('%s 列表格式或长度无效' % key)
            for item in value:
                text_value(item, key, cap, empty=False)
                if key in ('owner_ids', 'ignore_sender_ids', 'galgame_exempt_ids') and not item.isdigit():
                    raise PanelError('%s 仅接受数字字符串' % key)
        elif key in GALGAME_ARRAYS:
            low, high = GALGAME_ARRAYS[key]
            if not isinstance(value, list) or len(value) != 6:
                raise PanelError('%s 必须是长度为 6 的数组（对应阶段 0~5）' % key)
            for item in value:
                number(item, key, low, high)
            if key == 'galgame_thresholds' and any(b < a for a, b in zip(value, value[1:])):
                raise PanelError('galgame_thresholds 必须单调不减')
        elif key == 'gate_weights':
            if not isinstance(value, dict) or set(value) - set(DEFAULT_WEIGHTS):
                raise PanelError('gate_weights 字段无效')
            for name, weight in value.items():
                number(weight, name, -100, 100)
        elif key == 'active_tier_probability':
            if not isinstance(value, dict) or set(value) - {'2', '3'}:
                raise PanelError('active_tier_probability 字段无效')
            for name, probability in value.items():
                number(probability, name, 0, 1)
        if isinstance(value, dict) and isinstance(candidate.get(key), dict):
            candidate[key].update(copy.deepcopy(value))
        else:
            candidate[key] = copy.deepcopy(value)
    if candidate['image_max_bytes'] > candidate['image_total_max_bytes']:
        raise PanelError('单张图片上限应小于或等于单轮总量上限')
    if candidate['group_quota_min'] > candidate['group_quota_max']:
        raise PanelError('群额度下限应小于或等于上限')
    if candidate['memory_curate_max_tokens'] > candidate['memory_curate_max_tokens_cap']:
        raise PanelError('记忆提炼 token 上限超出硬顶')
    return candidate
