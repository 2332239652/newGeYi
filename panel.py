# -*- encoding: utf-8 -*-
"""OlivOS 原生页面后端：显式白名单消息桥，无额外服务或依赖。"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from . import config as plugin_config
from .context import aliases
from .context import scene as scene_brief
from .context.prompt_builder import PromptBuilder
from .memory.repository import MemoryRepository
from .models import IncomingMessage, SessionKey
from .panel_helpers import (
    EDITABLE_KEYS, SECRET_FIELDS, PanelError, Redactor, integer, text_value, validate_patch,
)
from .routing.likelihood import Features, contributions, resolve_threshold, score, weights_from_config
from .runtime import sponsors

EVENT_NAME = 'newGeYi_WebUI'
MAX_REQUEST_BYTES = 262144
MAX_RESPONSE_BYTES = 900000
WRITE_ACTIONS = {'config_save', 'config_reload', 'prompt_save',
                 'relationship_set_stage', 'relationship_reset'}


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


class _RingHandler(logging.Handler):
    def __init__(self, panel):
        super().__init__(logging.INFO)
        self.panel = panel

    def emit(self, record):
        try:
            # 原始对话片段、提示词与模型正文不进入面板日志。
            source = record.name
            message = record.getMessage()
            if source not in ('newGeYi.panel', 'newGeYi.tests'):
                if record.args and isinstance(record.msg, str):
                    # 只保留格式模板，参数一律丢弃，避免群聊/模型正文进入 ring。
                    message = re.sub(r'%(?:\([^)]+\))?[#0 +\-]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]',
                                     '[参数已省略]', record.msg).replace('%%', '%')
                else:
                    message = '[业务正文已省略] ' + source
            elif ' | ' in message:
                message = message.split(' | ', 1)[0] + ' [业务明细已省略]'
            if record.exc_info:
                message += ' [%s]' % getattr(record.exc_info[0], '__name__', 'Exception')
            self.panel._append_log(record.levelname, '[%s] %s' % (source, message), record.created)
        except Exception:  # 日志记录不影响业务，也不再调用 logger 形成递归。
            pass


class _MemoryPreview(MemoryRepository):
    def _touch(self, data: dict) -> None:
        # 只读预览保留原召回/格式化，但不刷新命中次数与时间。
        pass


class _RelationshipPreview:
    """只读关系服务代理：供调试台预览私聊阶段与世界书解锁，不触发任何数据库写入。"""

    def __init__(self, svc):
        self.svc = svc

    def stage_for(self, msg):
        return self.svc.stage_for(msg) if self.svc is not None else None

    def memory_policy(self, msg):
        return self.svc.memory_policy(msg) if self.svc is not None else None

    def block_for(self, msg):
        if self.svc is None or not hasattr(self.svc, 'preview_block_for'):
            return ''
        return self.svc.preview_block_for(msg)


class _HistoryPreview:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def snapshot(self, session_key, before_source_seq, window_seconds=1800, max_messages=30, max_chars=6000):
        rows = self.rows[-max_messages:]
        used = 0
        kept = []
        for row in reversed(rows):
            size = len(str(row.get('content') or ''))
            if kept and used + size > max_chars:
                break
            kept.append(copy.deepcopy(row))
            used += size
        return list(reversed(kept))


class _ReadStore:
    ALLOWED = {
        'get_setting', 'get_background', 'recent_turns', 'list_user_facts',
        'list_self_memories', 'recent_group_summaries', 'get_user_impression',
    }

    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        if name not in self.ALLOWED:
            raise PanelError('只读预览禁止状态写入', 'read_only')
        return getattr(self.store, name)


class Panel:
    def __init__(self, runtime):
        self.runtime = runtime
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._logs = deque(maxlen=800)
        self._log_seq = 0
        self._writes = OrderedDict()
        self._closed = False
        self._redactor = Redactor()
        self._redactor.update(runtime.config)
        for name, value in os.environ.items():
            if name == 'GEYI_API_KEY' or name.startswith('GEYI_KEY_'):
                if value:
                    self._redactor.values.add(value.strip())
        with runtime.gateway._lock:
            for provider, _ in runtime.gateway._pairs:
                if provider.api_key:
                    self._redactor.values.add(provider.api_key)
        # 不调用会 mkdir/迁移/资源镜像的 get_*_path。运行时已经持有真实数据库路径。
        self._database = Path(runtime.store.path)
        self._state = self._database.parent
        self._config_path = self._state / 'config.json'
        self._prompt_override = self._state / 'role_prompt.webui.txt'
        root = logging.getLogger('newGeYi')
        self._handler = _RingHandler(self)
        root.addHandler(self._handler)
        self._append_log('INFO', 'WebUI 面板已就绪；预览不联网、不发送、不落库')

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            logging.getLogger('newGeYi').removeHandler(self._handler)
            self._handler.close()
            self._writes.clear()
            with self._log_lock:
                self._logs.clear()

    def _refresh_secrets(self):
        self._redactor.update(self.runtime.config)
        for name, value in os.environ.items():
            if (name == 'GEYI_API_KEY' or name.startswith('GEYI_KEY_')) and value.strip():
                self._redactor.values.add(value.strip())
        with self.runtime.gateway._lock:
            for provider, _ in self.runtime.gateway._pairs:
                if provider.api_key:
                    self._redactor.values.add(provider.api_key)

    def _append_log(self, level, message, ts=None):
        # 仅读明确属于插件的环境 key；不在日志 emit 时获取 gateway 锁。
        for name, value in os.environ.items():
            if (name == 'GEYI_API_KEY' or name.startswith('GEYI_KEY_')) and value.strip():
                self._redactor.values.add(value.strip())
        with self._log_lock:
            if self._closed:
                return
            self._log_seq += 1
            self._logs.append({'id': self._log_seq, 'ts': ts or time.time(), 'level': str(level),
                               'message': self._redactor.text(message)[:2000]})

    def _prompt_path(self) -> Path:
        if self._prompt_override.is_file():
            return self._prompt_override
        resource = self._state / 'role_prompt.txt' if os.environ.get('GEYI_DATA_DIR') else self._state / 'data' / 'role_prompt.txt'
        return resource if resource.is_file() else plugin_config.BASE_DIR / 'data' / 'role_prompt.txt'

    def _disk(self, path: Path) -> bytes:
        return path.read_bytes() if path.is_file() else b''

    @plugin_config.config_locked
    def _config_revision(self) -> str:
        return _hash(self._disk(self._config_path) + b'\x00' + _json(self.runtime.config))

    @plugin_config.config_locked
    def config_get(self) -> dict:
        self._refresh_secrets()
        sensitive = [key for key in EDITABLE_KEYS if key in self.runtime.config
                     and self._redactor.scrub(self.runtime.config[key]) != self.runtime.config[key]]
        return {'config': self._redactor.clean(copy.deepcopy(self.runtime.config)),
                'revision': self._config_revision(),
                'editable_keys': [key for key in EDITABLE_KEYS if key not in sensitive],
                'redacted_editable_keys': sensitive, 'secret_fields': SECRET_FIELDS}

    def prompt_get(self) -> dict:
        path = self._prompt_path()
        raw = self._disk(path)
        current = str(self.runtime.prompt_builder.role_prompt)
        text = raw.decode('utf-8') if raw else current
        revision = _hash(str(path).encode('utf-8') + b'\x00' + raw + b'\x00' + current.encode('utf-8'))
        return {'text': self._redactor.text(text), 'revision': revision, 'path': str(path)}

    @staticmethod
    def _confirmed(params):
        if params.get('confirm') is not True:
            raise PanelError('写操作必须显式 confirm=true', 'confirmation_required')

    @plugin_config.config_locked
    def _config_save(self, params):
        self._confirmed(params)
        if params.get('revision') != self._config_revision():
            raise PanelError('配置已改变，请重新读取后保存', 'revision_conflict')
        current = self.runtime.config
        snapshot = copy.deepcopy(current)
        candidate = validate_patch(params.get('patch'), snapshot)
        if any(self._redactor.scrub(snapshot.get(key)) != snapshot.get(key) for key in params['patch']):
            raise PanelError('该配置字段含脱敏内容，先在本机移除凭据后再编辑', 'sensitive_config_field')
        plugin_config.save_config(candidate)
        try:
            self.runtime.commit_config(candidate, persist=False)
        except Exception:
            # Persistence completed; report component application separately.
            result = self.config_get()
            result['warnings'] = ['配置已保存；部分组件应用失败，请检查日志并重新加载']
            return result
        return self.config_get()

    @plugin_config.config_locked
    def _config_reload(self, params):
        self._confirmed(params)
        if params.get('revision') != self._config_revision():
            raise PanelError('配置已改变，请重新读取后重载', 'revision_conflict')
        error = self.runtime.reload_config()
        if error:
            raise PanelError(str(error), 'config_load_error')
        return self.config_get()

    def _prompt_save(self, params):
        self._confirmed(params)
        info = self.prompt_get()
        if params.get('revision') != info['revision']:
            raise PanelError('角色设定已改变，请重新读取后保存', 'revision_conflict')
        text = text_value(params.get('text'), 'text', 100000, empty=False)
        # 从脱敏视图保存带凭据的底稿会丢失原文；要求先人工移除底稿凭据。
        effective = self._prompt_path()
        original = self._disk(effective).decode('utf-8') if effective.is_file() else self.runtime.prompt_builder.role_prompt
        if self._redactor.text(original) != original:
            raise PanelError('生效设定含凭据，先在本机移除后再编辑', 'sensitive_prompt')
        target = self._prompt_override
        if target.is_file():
            (self._state / 'role_prompt.webui.txt.bak-auto').write_bytes(target.read_bytes())
        temp = self._state / 'role_prompt.webui.txt.tmp'
        try:
            temp.write_text(text, encoding='utf-8', newline='\n')
            os.replace(temp, target)
        finally:
            if temp.is_file():
                temp.unlink()
        self.runtime.prompt_builder.set_role_prompt(text)
        return self.prompt_get()

    def _provider_status(self):
        gateway = self.runtime.gateway
        with gateway._lock:
            providers = []
            for provider, state in gateway._pairs:
                providers.append({
                    'name': provider.name, 'model': provider.model, 'priority': provider.priority,
                    'protocol': provider.protocol, 'kinds': sorted(provider.kinds or []),
                    'manual_only': provider.manual_only, 'configured': bool(provider.api_key),
                    'healthy': bool(state.healthy), 'fail_count': state.fail_count,
                    'last_error': state.last_error, 'last_ok_at': state.last_ok_at,
                    'last_fail_at': state.last_fail_at, 'last_probe_at': state.last_probe_at,
                    'current': provider.name == gateway._current_name,
                    'anchor': provider.name == gateway._anchor_name, 'hold': provider.name == gateway._hold_name,
                })
            status = {'current': gateway._current_name, 'anchor': gateway._anchor_name,
                      'hold': gateway._hold_name, 'history': copy.deepcopy(gateway._history[-20:])}
        return self._redactor.clean(providers), self._redactor.clean(status)

    def _overview(self):
        providers, status = self._provider_status()
        bots = []
        proc = getattr(self.runtime, 'proc', None)
        for bot_hash, bot in (getattr(proc, 'Proc_data', {}) or {}).get('bot_info_dict', {}).items():
            platform = getattr(bot, 'platform', {}) or {}
            bots.append({'hash': str(bot_hash), 'id': str(getattr(bot, 'id', '')),
                         'platform': str(platform.get('platform') or ''), 'sdk': str(platform.get('sdk') or '')})
        status['queue'] = self.runtime.scheduler.pending_snapshot()
        status['usage'] = self.runtime.store.today_stats()
        voice = getattr(self.runtime, 'voice_manager', None)
        if voice is not None:
            healthy, reason, latency = voice.client.health_cached()
            status['voice'] = {'enabled': bool(voice.enabled), 'healthy': bool(healthy),
                               'reason': str(reason), 'latency_ms': latency, 'cached': True}
        return {'version': plugin_config.PLUGIN_VERSION,
                'uptime_seconds': max(0, int(time.time() - self.runtime._start_time)), 'bots': bots,
                'paths': {'config': str(self._config_path), 'state': str(self._state),
                          'database': str(self._database), 'role_prompt': str(self._prompt_path())},
                'flags': {key: value for key, value in self.runtime.config.items() if type(value) is bool},
                'providers': providers, 'status': status,
                'galgame': {'enabled': bool(self.runtime.config.get('galgame_enabled')),
                            'shadow': bool(self.runtime.config.get('galgame_shadow', True)),
                            'worldbook': bool(self.runtime.config.get('worldbook_enabled'))}}

    def _conn(self):
        # 复用已初始化的存储；不重新实例化/迁移 SQLiteStore。
        return self.runtime.store._conn()

    def _rows(self, sql, args=()):
        cursor = self._conn().execute(sql, args)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _session(key):
        text_value(key, 'session_key', 512, empty=False)
        parts = key.split(':')
        if len(parts) != 6 or parts[3] not in ('group', 'private'):
            raise PanelError('session_key 格式无效')
        return SessionKey(*(part if part != '-' else '' for part in parts))

    def _sessions(self, params):
        limit = integer(params.get('limit', 50), 'limit', 1, 100)
        offset = integer(params.get('offset', 0), 'offset', 0, 100000)
        # 完整会话目录不以 buffer_sessions 冒充全量，且列表从不取正文。
        union = ' UNION '.join('SELECT session_key FROM ' + table for table in (
            'turns', 'memory_buffer', 'backgrounds', 'group_summaries', 'user_facts',
            'self_memories', 'active_decisions'))
        count = self._conn().execute('SELECT COUNT(*) FROM (' + union + ')').fetchone()[0]
        rows = self._rows(
            'SELECT s.session_key, (SELECT COUNT(*) FROM turns t WHERE t.session_key=s.session_key) AS turn_count, '
            'MAX(COALESCE((SELECT MAX(created_at) FROM turns t WHERE t.session_key=s.session_key),0), '
            'COALESCE((SELECT MAX(created_at) FROM memory_buffer b WHERE b.session_key=s.session_key),0)) AS last_ts '
            'FROM (' + union + ') s ORDER BY last_ts DESC, s.session_key LIMIT ? OFFSET ?', (limit, offset))
        items = []
        for row in rows:
            try:
                session = self._session(row['session_key'])
            except PanelError:
                continue
            items.append(dict(row, target_id=session.target_id, session_type=session.session_type))
        return {'items': items, 'total': int(count), 'limit': limit, 'offset': offset,
                'scope': 'persisted_sessions'}

    def _usage(self, key):
        rows = self._rows(
            'SELECT COUNT(*) AS calls, COALESCE(SUM(in_tokens),0) AS input_tokens, '
            'COALESCE(SUM(out_tokens),0) AS output_tokens, COALESCE(SUM(cached_tokens),0) AS cached_tokens, '
            'COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failures FROM api_calls '
            'WHERE session_key=? AND day=?', (key, time.strftime('%Y-%m-%d')))
        return rows[0]

    def _turns(self, key, limit):
        rows = self._rows('SELECT role,content,sender_id,sender_name,created_at FROM turns '
                          'WHERE session_key=? ORDER BY seq DESC LIMIT ?', (key, limit))
        return list(reversed(rows))

    def _state_info(self, key):
        store = self.runtime.store
        info = {'passive_enabled': store.get_setting('group_passive:' + key) != '0',
                'active_enabled': store.get_setting('group_active:' + key) != '0',
                'rest_remaining_seconds': self.runtime.rest.remaining(key),
                'memory_pending': store.buffer_pending(key, self.runtime.curator.cursor(key)),
                'quota': self.runtime.group_profile.quota_for(key)}
        # 工作记忆纸条（会话级，只读）：排查「她怎么知道几十分钟前的事」时看这个
        brief = scene_brief.get_brief(store, key)
        if brief:
            updated = int(brief.get('updated_at') or 0)
            info['scene'] = {
                'text': str(brief.get('text') or ''),
                'covered_to': int(brief.get('covered_to') or 0),
                'updated_at': updated,
                'age_seconds': max(0, int(time.time() - updated)) if updated else None,
                'fresh': scene_brief.is_fresh(
                    brief, float(self.runtime.config.get('scene_ttl_seconds', 7200) or 0)),
            }
        # 私聊闸门的用量：排查「她为什么不回私聊」时，这几个数比翻日志直观
        try:
            session = self._session(key)
            if getattr(session, 'session_type', '') == 'private':
                gate = getattr(self.runtime, 'private_gate', None)
                if gate is not None:
                    info['private_used_today'] = gate.used_today(key)
                    # 拥挤期单人上限会被收紧、赞助者上限另算，所以显示**当前生效的**那个
                    uid = str(key).rsplit(':', 1)[-1]
                    info['private_quota'] = int(gate._effective_user_quota(uid) or 0)
                    info['private_squeezed'] = gate.squeezed()
                    info['private_sponsor'] = gate.is_sponsor(uid)
                    info['private_sponsor_tier'] = gate.sponsor_tier(uid)
                    info['private_sponsor_beta'] = sponsors.is_beta(self.runtime.config, uid)
                    info['private_used_global'] = gate.used_today_global()
                    info['private_quota_global'] = int(
                        self.runtime.config.get('private_daily_quota_global', 400) or 0)
                    info['private_trigger_only'] = gate.trigger_only()
                # 群聊记忆共享是**会话级**选择（None = 跟随默认）。排查「她怎么知道群里的事」时，
                # 这个字段比翻 settings 表直观；写入仍只走 QQ 指令，面板不给改。
                info['group_memory_share'] = self.runtime.memory.share_override(key)
                info['group_memory_default'] = bool(
                    self.runtime.config.get('memory_private_enabled', False))
                # 自定义称呼（按 QQ 号，全局一个）：空串 = 还在用 QQ 昵称
                info['alias'] = aliases.get(self.runtime.store, str(key).rsplit(':', 1)[-1])
        except Exception:  # noqa: BLE001 - 面板少一个字段不算错
            pass
        return info

    def _session_get(self, params):
        key = params.get('session_key')
        session = self._session(key)
        limit = integer(params.get('limit', 50), 'limit', 1, 100)
        store = self.runtime.store
        memories = []
        for row in store.recent_group_summaries(key, limit=limit):
            memories.append(dict(row, layer='L1'))
        for row in store.list_user_facts(key, limit=limit):
            item = dict(row, layer='L2', content=row['fact'])
            item.pop('fact', None)
            memories.append(item)
        for row in store.list_self_memories(key, limit=limit):
            memories.append(dict(row, layer='L3'))
        # L4：这个会话对方的印象卡（跨群共享）+ 它的历史版本（只追加，可回溯）
        # 私聊的 target_id 就是对方；群聊没有"对方"，取最近一条用户消息的发言人。
        try:
            peer_id = session.target_id if session.session_type == 'private' else ''
            if not peer_id:
                recent_users = [t.get('sender_id') for t in self._turns(key, 30)
                                if t.get('role') == 'user' and t.get('sender_id')]
                peer_id = str(recent_users[-1]) if recent_users else ''
            scope = 'private' if session.session_type == 'private' else 'group'
            if peer_id:
                impression = store.get_user_impression(peer_id, scope=scope)
                if not impression and session.session_type == 'private':
                    impression = store.get_user_impression(peer_id, scope='group')
                if impression and str(impression.get('summary') or '').strip():
                    memories.append({'id': 0, 'layer': 'L4', 'content': impression['summary'],
                                     'created_at': impression.get('updated_at') or 0,
                                     'user_id': peer_id, 'current': True, 'scope': impression.get('scope', scope)})
                hist_rows = store.impression_history(peer_id, scope=scope, limit=min(limit, 20))
                if not hist_rows and session.session_type == 'private':
                    hist_rows = store.impression_history(peer_id, scope='group', limit=min(limit, 20))
                for row in hist_rows:
                    memories.append({'id': row['id'], 'layer': 'L4-history',
                                     'content': row['summary'], 'created_at': row['created_at'],
                                     'user_id': peer_id, 'reason': row.get('reason') or '',
                                     'scope': row.get('scope') or scope})
            # L2 回收站：被容量淘汰的事实（先留档再删），留个可回溯的尾巴
            for row in store.archived_facts(key, limit=min(limit, 20)):
                memories.append({'id': row['id'], 'layer': 'L2-archive',
                                 'content': row['fact'], 'created_at': row['archived_at'],
                                 'user_id': row['user_id'], 'reason': row['reason']})
        except Exception:  # noqa: BLE001 - 印象读坏了不能让整个会话详情失败
            pass
        relation = None
        try:
            svc = getattr(self.runtime, 'relationship', None)
            if svc is not None and session.session_type == 'private' and session.target_id.isdigit():
                relation = svc.snapshot(session.target_id)
        except Exception:  # noqa: BLE001 - 关系读不出来不影响会话详情
            relation = None
        return {'session_key': key, 'turns': self._turns(key, limit), 'memories': memories,
                'state': self._state_info(key), 'usage': self._usage(key), 'relationship': relation}

    # ---------- 私聊关系（galgame）----------
    def _relationship(self):
        svc = getattr(self.runtime, 'relationship', None)
        if svc is None:
            raise PanelError('关系系统不可用', 'unavailable')
        return svc

    @staticmethod
    def _user_id(params):
        uid = params.get('user_id')
        if not isinstance(uid, str) or not uid.isdigit() or len(uid) > 40:
            raise PanelError('user_id 必须是数字字符串')
        return uid

    def _relationship_list(self, params):
        svc = self._relationship()
        limit = integer(params.get('limit', 50), 'limit', 1, 100)
        offset = integer(params.get('offset', 0), 'offset', 0, 100000)
        data = svc.list_relations(limit, offset)
        day = svc.log_stats(time.time() - 86400)
        week = svc.log_stats(time.time() - 7 * 86400)
        data.update({'limit': limit, 'offset': offset, 'enabled': svc.enabled(), 'shadow': svc.shadow(),
                     'stats_24h': day, 'stats_7d': week})
        return data

    def _relationship_get(self, params):
        svc = self._relationship()
        uid = self._user_id(params)
        limit = integer(params.get('limit', 30), 'limit', 1, 100)
        return svc.snapshot(uid, log_limit=limit)

    def _relationship_set_stage(self, params):
        self._confirmed(params)
        svc = self._relationship()
        uid = self._user_id(params)
        stage = integer(params.get('stage'), 'stage', 0, 5)
        svc.force_stage(uid, stage)
        return svc.snapshot(uid, log_limit=10)

    def _relationship_reset(self, params):
        self._confirmed(params)
        svc = self._relationship()
        uid = self._user_id(params)
        svc.reset(uid)
        return svc.snapshot(uid, log_limit=10)

    def _history(self, key, is_group, limit=60, window_seconds=None):
        live = getattr(self.runtime, 'live_context', None)
        if not is_group or live is None:
            return self._turns(key, limit)
        with live._lock:
            # 只复制，不调用 snapshot 的共享缓存 prune。
            turns = list(live._states.get(key, ()))
        now = time.time()
        window = (int(self.runtime.config['group_live_window_seconds'])
                  if window_seconds is None else window_seconds)
        turns = [turn for turn in turns if now - turn.created_at <= window]
        turns.sort(key=lambda turn: (turn.source_seq, 0 if turn.role == 'user' else 1, turn.order))
        return [{'role': turn.role, 'content': turn.content, 'sender_id': turn.sender_id,
                 'sender_name': turn.sender_name} for turn in turns[-limit:]]

    def _debug_preview(self, params):
        key = params.get('session_key')
        session = self._session(key)
        text = text_value(params.get('text'), 'text', 10000, empty=False)
        sender_id = text_value(params.get('sender_id', 'preview'), 'sender_id', 80, empty=False)
        name = text_value(params.get('sender_name', '预览用户'), 'sender_name', 80)
        for flag in ('is_group', 'mentioned_bot', 'is_active'):
            if flag in params and type(params[flag]) is not bool:
                raise PanelError('%s 必须是布尔值' % flag)
        is_group = params.get('is_group', session.session_type == 'group')
        if is_group != (session.session_type == 'group'):
            raise PanelError('is_group 与会话类型不一致')
        mentioned = params.get('mentioned_bot', False)
        active = params.get('is_active', False)
        cfg = copy.deepcopy(self.runtime.config)
        msg = IncomingMessage(session, 'preview', sender_id, name, text, text, is_group, mentioned,
                              None, None, arrival_seq=1)
        history = self._history(key, is_group)
        facade = _ReadStore(self.runtime.store)
        memory = _MemoryPreview(facade, cfg)
        builder = PromptBuilder(cfg, facade, memory, self.runtime.worldbook,
                                _HistoryPreview(history), images=None, renderer=self.runtime.renderer)
        builder.relationship = _RelationshipPreview(getattr(self.runtime, 'relationship', None))
        builder.set_role_prompt(self.runtime.prompt_builder.role_prompt)
        messages = builder.build(msg, is_active=active)
        directed = mentioned or any(word in text for word in (cfg['triggers'] or ['歌伊'])) or not is_group
        passive_allowed = (cfg['passive_enabled'] and self.runtime.store.get_setting('group_passive:' + key) != '0'
                           if is_group else cfg['private_enabled'])
        ignored = sender_id in [str(item) for item in cfg['ignore_sender_ids']]
        resting = self.runtime.rest.is_resting(key)
        if ignored or resting:
            branch, reason = 'ignored', 'ignore_sender' if ignored else 'rest'
        elif directed:
            branch, reason = ('passive', '') if passive_allowed else ('ignored', 'passive_disabled')
        elif is_group and cfg['active_enabled'] and self.runtime.store.get_setting('group_active:' + key) != '0':
            branch, reason = 'active_candidate', '完整主动闸门与模型决策未执行'
        else:
            branch, reason = 'ignored', 'active_disabled'
        recent = self._history(key, is_group, limit=200, window_seconds=300) if is_group else []
        own = sum(1 for row in recent if row['role'] == 'assistant')
        raw_last = self.runtime.store.get_setting('last_self_talk:' + key)
        try:
            elapsed = max(0.0, time.time() - float(raw_last)) if raw_last else 3600.0
        except (TypeError, ValueError):
            elapsed = 3600.0
        features = Features(mention_bot=mentioned, at_other=('@' in text or '[CQ:at' in text) and not mentioned,
                            question='?' in text or '？' in text, media_only=not text.strip(),
                            self_ratio_5m=own / len(recent) if recent else 0.0,
                            recent_count_5m=len(recent), seconds_since_self=elapsed)
        weights = weights_from_config(cfg)
        probability = score(features, weights)
        attention = self.runtime.attention
        scores = list(attention._recent_scores.get(key, ()))
        floor, keep = cfg['gate_min_score'], cfg['gate_quantile_keep']
        threshold = max(floor, resolve_threshold(scores + [probability], keep)) if 0 < keep < 1 else floor
        return {'route': {'branch': branch, 'reason': reason, 'directed': bool(directed),
                          'passive_allowed': bool(passive_allowed), 'score': probability, 'threshold': threshold,
                          'contributions': contributions(features, weights), 'model_evaluated': False,
                          'preview_mode': 'simplified_read_only'},
                'messages': messages,
                'summary': {'message_count': len(messages), 'history_count': len(history),
                            'chars': sum(len(str(row['content'])) for row in messages),
                            'omitted': ['image_download', 'video', 'web', 'voice', 'full_active_gate'],
                            'note': '仅预览文本上下文与本地分数；不代表完整闸门放行或实际模型回复'},
                'effects': {'network': False, 'send': False, 'persist': False}}

    def _get_logs(self, params):
        since = integer(params.get('since', 0), 'since', 0, 2**63 - 1)
        limit = integer(params.get('limit', 100), 'limit', 1, 200)
        with self._log_lock:
            items = [dict(row) for row in self._logs if row['id'] > since][:limit]
            oldest = self._logs[0]['id'] if self._logs else 0
        return {'items': items, 'next_since': items[-1]['id'] if items else since,
                'dropped': bool(since and oldest > since + 1)}

    def _dispatch(self, params):
        action = params.get('action')
        actions = {'overview': lambda: self._overview(), 'config_get': self.config_get,
                   'config_save': lambda: self._config_save(params),
                   'config_reload': lambda: self._config_reload(params), 'prompt_get': self.prompt_get,
                   'prompt_save': lambda: self._prompt_save(params), 'sessions': lambda: self._sessions(params),
                   'session_get': lambda: self._session_get(params),
                   'debug_preview': lambda: self._debug_preview(params), 'logs': lambda: self._get_logs(params),
                   'relationship_list': lambda: self._relationship_list(params),
                   'relationship_get': lambda: self._relationship_get(params),
                   'relationship_set_stage': lambda: self._relationship_set_stage(params),
                   'relationship_reset': lambda: self._relationship_reset(params)}
        if not isinstance(action, str) or action not in actions:
            raise PanelError('未知 WebUI action', 'unknown_action')
        return actions[action]()

    def handle_event(self, event, Proc) -> None:
        data = getattr(event, 'data', None)
        if getattr(data, 'namespace', None) != plugin_config.NAMESPACE:
            return
        context = getattr(data, 'webui', None)
        if not isinstance(context, dict):
            return
        request_id = context.get('request_id')
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return
        with self._lock:
            response = None
            cache_key = None
            fingerprint = None
            try:
                if self._closed:
                    raise PanelError('面板已关闭', 'closed')
                self._refresh_secrets()
                if getattr(data, 'event', None) != EVENT_NAME:
                    raise PanelError('未知 WebUI 事件', 'unknown_event')
                params = getattr(data, 'payload', context.get('payload'))
                if not isinstance(params, dict) or len(_json(params)) > MAX_REQUEST_BYTES:
                    raise PanelError('payload 格式或大小无效')
                if not isinstance(params.get('action'), str):
                    raise PanelError('action 必须是文本')
                if params['action'] in WRITE_ACTIONS:
                    session_hash = _hash(_json(context.get('session')))
                    cache_key = (session_hash, request_id)
                    fingerprint = _hash(_json(params))
                    if cache_key in self._writes:
                        old_fingerprint, response = self._writes[cache_key]
                        if old_fingerprint != fingerprint:
                            raise PanelError('同一写请求 ID 的内容冲突', 'request_conflict')
                if response is None:
                    result = self._dispatch(params)
                    response = {'ok': True, 'data': self._redactor.scrub(result)}
                if len(_json(response)) > MAX_RESPONSE_BYTES:
                    raise PanelError('结果过大，请减小查询条数', 'response_too_large')
            except PanelError as exc:
                response = {'ok': False, 'error': self._redactor.text(exc), 'code': exc.code}
            except Exception as exc:
                response = {'ok': False, 'error': '处理失败：' + type(exc).__name__, 'code': 'internal_error'}
            if cache_key is not None and fingerprint is not None and response.get('code') != 'request_conflict':
                self._writes[cache_key] = (fingerprint, copy.deepcopy(response))
                while len(self._writes) > 256:
                    self._writes.popitem(last=False)
            try:
                event.send('webui', request_id, response)
            except Exception:
                # 回包失败不重试写操作，原请求结果已记录可幂等读取。
                pass
