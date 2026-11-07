#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立开发验收工具；生产插件没有对此模块的 import。

运行：python tools/preview_webui.py [--port 0] [--keep-data]
短测：python tools/preview_webui.py --self-test
只监听 127.0.0.1；默认 Ctrl+C 后清理本次 scratch。--keep-data 保留并打印目录。
不需要 OlivOS 服务、机器人或第三方 HTTP 库。仅工具使用 http.server。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import importlib
import importlib.machinery
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_geyi_webui_preview'
EVENT_NAME = 'newGeYi_WebUI'
BODY_LIMIT = 1024 * 1024
SANDBOX = 'allow-scripts allow-forms allow-modals allow-downloads'
PANEL_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox " + SANDBOX
)
HOST_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
ROLE = '你是虚构的验收角色「星灯」，在纸上城堡与虚构群友讨论读书、花园和钟楼。所有资料仅用于开发验收。'


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def require(condition, message):
    # -O 运行也保留验收断言。
    if not condition:
        raise AssertionError(message)


class IsolationViolation(RuntimeError):
    """显式暴露意外联网、机器人接口或线程启动。"""


class Guards:
    def __init__(self):
        self.attempts = []
        self.patches = []
        self.old_requests = sys.modules.get('requests')

    def blocked(self, kind):
        def deny(*args, **kwargs):
            self.attempts.append(kind)
            print('[ISOLATION BLOCKED] ' + kind, file=sys.stderr, flush=True)
            raise IsolationViolation('隔离验收阻断：' + kind)
        return deny

    def install(self):
        # 强制替换 requests，不依赖本机是否安装；连 Session.send 也不会碰网络。
        stub = ModuleType('requests')
        exceptions = SimpleNamespace(
            RequestException=IsolationViolation, Timeout=IsolationViolation,
            ConnectionError=IsolationViolation, HTTPError=IsolationViolation,
            TooManyRedirects=IsolationViolation,
        )
        stub.exceptions = exceptions
        for name, value in vars(exceptions).items():
            setattr(stub, name, value)
        deny = self.blocked('requests network')

        class Session:
            def __init__(self):
                self.headers = {}
                self.proxies = {}
                self.trust_env = False

            request = get = post = put = patch = delete = head = options = send = deny

            def mount(self, *args, **kwargs):
                pass

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        stub.Session = Session
        stub.sessions = SimpleNamespace(Session=Session)
        for name in ('request', 'get', 'post', 'put', 'patch', 'delete', 'head', 'options'):
            setattr(stub, name, deny)
        sys.modules['requests'] = stub
        # http.server 只需 bind/listen/accept；其余出站入口全部显式阻断。
        for target, name, kind in (
            (socket.socket, 'connect', 'socket.connect'),
            (socket.socket, 'connect_ex', 'socket.connect_ex'),
            (socket, 'create_connection', 'socket.create_connection'),
            (socket, 'getaddrinfo', 'DNS resolution'),
            (threading.Thread, 'start', 'thread.start'),
        ):
            item = patch.object(target, name, self.blocked(kind))
            item.start()
            self.patches.append(item)

    def close(self):
        for item in reversed(self.patches):
            item.stop()
        self.patches.clear()
        if self.old_requests is None:
            sys.modules.pop('requests', None)
        else:
            sys.modules['requests'] = self.old_requests


class Scratch:
    def __init__(self, keep):
        self.keep = keep
        self.temp_root = Path(tempfile.gettempdir()).resolve()
        self.path = Path(tempfile.mkdtemp(prefix='geyi_webui_preview_', dir=str(self.temp_root))).resolve()
        require(self.path.parent == self.temp_root and self.path != ROOT, 'scratch 创建路径异常')
        self.identity = uuid.uuid4().hex
        (self.path / '.preview-owned').write_text(self.identity, encoding='utf-8')
        print('SCRATCH=' + str(self.path), flush=True)
        print('DATA_POLICY=' + ('保留本次临时资料' if keep else '正常关闭清理本次临时资料'), flush=True)

    def close(self):
        if self.keep:
            print('SCRATCH_RETAINED=' + str(self.path), flush=True)
            return
        # 删除前重新验证同一绝对目录、随机所有权标记；跳过任何链接/联接。
        try:
            path = self.path
            marker = path / '.preview-owned'
            valid = (
                path.resolve() == path and path.parent == self.temp_root
                and path.name.startswith('geyi_webui_preview_') and not path.is_symlink()
                and marker.is_file() and not marker.is_symlink()
                and marker.read_text(encoding='utf-8') == self.identity
            )
            require(valid, 'scratch 所有权检查失败，目录已保留')
            for entry in path.rglob('*'):
                require(not entry.is_symlink(), 'scratch 含链接，目录已保留')
                require(entry.resolve().is_relative_to(path), 'scratch 含外部联接，目录已保留')
            print('SCRATCH_VERIFIED_FOR_CLEANUP=' + str(path), flush=True)
            shutil.rmtree(path)
            print('SCRATCH_CLEANED=' + str(path), flush=True)
        except (OSError, AssertionError) as exc:
            print('SCRATCH_RETAINED=' + str(self.path) + ' (' + str(exc) + ')', file=sys.stderr, flush=True)


class FakeEvent:
    def __init__(self, request, session, guards):
        self.data = SimpleNamespace(
            namespace='newGeYi', event=EVENT_NAME,
            webui={'request_id': request['request_id'], 'session': session, 'payload': request['payload']},
            payload=request['payload'],
        )
        self.bot_info = None
        self.reply_payload = None
        self.guards = guards

    def send(self, channel, request_id, payload, *args, **kwargs):
        if channel != 'webui' or request_id != self.data.webui['request_id'] or args or kwargs:
            return self.guards.blocked('fakeevent non-webui send')()
        require(self.reply_payload is None, 'Panel 重复回包')
        encoded(payload)
        self.reply_payload = copy.deepcopy(payload)
        return True

    def __getattr__(self, name):
        # 不提供群成员、账号查询、QQ reply 等接口；意外调用被记录。
        return self.guards.blocked('fakeevent.' + name)


class Fixture:
    def __init__(self, scratch):
        self.scratch = scratch
        self.runtime = None
        self.panel = None
        self.guards = Guards()
        self.session = {'id': 'preview-' + uuid.uuid4().hex}
        self.sequence = 0
        self.config_identity = None
        self.old_environment = {name: value for name, value in os.environ.items() if name.startswith('GEYI_')}
        self.old_bytecode = sys.dont_write_bytecode
        self.config_patches = []

    def open(self):
        # 清除所有 GEYI_KEY_*、owner、API 环境；绝不沿用生产 GEYI_DATA_DIR。
        for name in tuple(os.environ):
            if name.startswith('GEYI_'):
                os.environ.pop(name)
        os.environ['GEYI_DATA_DIR'] = str(self.scratch.path)
        sys.dont_write_bytecode = True
        self.guards.install()
        # 仅加载现有模块的源码，不执行生产 __init__ → main → 全局 Runtime。
        require(PACKAGE not in sys.modules and 'newGeYi' not in sys.modules, '请用独立 Python CLI 运行预览')
        package = ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        package.__package__ = PACKAGE
        package.__spec__ = importlib.machinery.ModuleSpec(PACKAGE, loader=None, is_package=True)
        package.__spec__.submodule_search_locations = package.__path__
        sys.modules[PACKAGE] = package
        config = importlib.import_module(PACKAGE + '.config')
        # BASE_DIR 的 fixture 化也阻断 Renderer 的包内资源兜底与配置迁移来源。
        config.BASE_DIR = self.scratch.path
        config.ROLE_PROMPT_PATH = self.scratch.path / 'role_prompt.txt'

        def resource(name):
            if not isinstance(name, str) or Path(name).name != name or '\\' in name or '/' in name:
                raise IsolationViolation('resource 必须是 scratch 内单个文件名')
            return self.scratch.path / name

        for name, value in (('get_resource_path', resource), ('sync_released_data', lambda *a, **k: [])):
            item = patch.object(config, name, value)
            item.start()
            self.config_patches.append(item)
        # 在 Runtime import/构造前写全量默认配置：load_config 永远见不到缺失/生产迁移分支。
        cfg = copy.deepcopy(config.DEFAULT_CONFIG)
        cfg.update({
            'api_key': '', 'api_endpoint': 'http://fixture.invalid/v1', 'providers': [],
            'extra_headers': {'X-Preview-Private': 'fictional-header-sentinel'},
            'preview_secret': 'fictional-config-preservation-sentinel',
            'owner_ids': [], 'ignore_sender_ids': [], 'assistant_name': '星灯', 'triggers': ['星灯'],
            'voice_enabled': False, 'voice_gateway_url': 'http://fixture.invalid/voice',
            'voice_health_url': 'http://fixture.invalid/health', 'voice_auth_token': '',
            'voice_onebot_http_url': '', 'voice_onebot_access_token': '',
            'image_enabled': False, 'emote_enabled': False, 'worldbook_enabled': True,
            'active_enabled': True, 'active_shadow': True, 'memory_enabled': True,
            'memory_curator_enabled': False,
        })
        state = self.scratch.path
        (state / 'config.json').write_bytes(encoded(cfg))
        (state / 'role_prompt.webui.txt').write_text(ROLE, encoding='utf-8')
        (state / 'role_prompt.txt').write_text(ROLE, encoding='utf-8')
        (state / 'emotes.json').write_bytes(encoded({'emotes': []}))
        (state / 'worldbook.json').write_bytes(encoded([{
            'keywords': ['钟楼', '花园'], 'content': '纸上城堡的钟楼每晚响三声，花园由虚构居民共同照料。',
            'enabled': True,
        }]))
        Runtime = importlib.import_module(PACKAGE + '.runtime.app').Runtime
        before = set(threading.enumerate())
        self.runtime = Runtime()  # 唯一一次构造；不 init/init_after/save，不调发送或模型。
        require(set(threading.enumerate()) == before, 'Runtime 构造启动了线程')
        require(not self.guards.attempts, 'Runtime 构造出现隔离副作用')
        require(Path(self.runtime.store.path).resolve().parent == state, '数据库不在 scratch')
        require(config.get_state_dir().resolve() == state and config.get_data_dir().resolve() == state,
                '状态/资源目录不在 scratch')
        self.config_identity = id(self.runtime.config)
        for name in ('init', 'init_after', 'handle_message', 'handle_poke', 'handle_menu',
                     'handle_poke_back', '_notify_owner', '_send_reply'):
            if hasattr(self.runtime, name):
                setattr(self.runtime, name, self.guards.blocked('runtime.' + name))
        for component, methods in (
            (self.runtime.gateway, ('chat', 'start_probe', 'probe_once')),
            (self.runtime.curator, ('start',)),
            (self.runtime.scheduler, ('submit',)),
            (self.runtime.group_profile, ('fetch', 'refresh_async')),
        ):
            for name in methods:
                setattr(component, name, self.guards.blocked(type(component).__name__ + '.' + name))
        self._seed()
        self.panel = importlib.import_module(PACKAGE + '.panel').Panel(self.runtime)
        print('RUNTIME=constructed-once; lifecycle=not-called; workers=not-started; requests=stubbed', flush=True)

    def _seed(self):
        SessionKey = importlib.import_module(PACKAGE + '.models').SessionKey
        self.keys = []
        topics = [('garden', '花园读书会', '大家约好在纸上花园交流一本虚构小说。'),
                  ('clock', '钟楼讨论组', '大家讨论纸上钟楼的新钟声。'),
                  ('tea', '茶室写作组', '大家在纸上茶室合作写童话。')]
        store = self.runtime.store
        for index, (slug, name, topic) in enumerate(topics, 1):
            key = SessionKey('fixture-bot', 'fixture', 'fixture', 'group', '', 'fixture-' + slug).as_text()
            self.keys.append(key)
            user = 'fixture-reader-' + str(index)
            turns = [
                ('user', name + '开场啦，星灯，你记得今天的话题吗？', user, '虚构群友' + str(index)),
                ('assistant', '记得呀，' + topic, '', '星灯'),
                ('user', '我喜欢看虚构的冒险故事，也喜欢照料纸上花园。', user, '虚构群友' + str(index)),
            ]
            for seq, (role, text, sender, nickname) in enumerate(turns, 1):
                store.add_turn(key, role, text, sender_id=sender, sender_name=nickname, model='fixture-model')
                store.buffer_append(key, role, text, sender_id=sender, sender_name=nickname)
                self.runtime.live_context.append(key, seq, role, text, sender_name=nickname, sender_id=sender)
            store.set_background(key, name + '：全部为虚构开发资料。')
            store.add_group_summary(key, topic)
            store.add_user_fact(key, user, '喜欢虚构冒险故事和纸上花园。', category='偏好', source='fixture')
            store.add_self_memory(key, '星灯答应下次带来一段纸上城堡的童话。', kind='promise')
            store.set_user_impression(user, '虚构群友热爱读书，愿意帮助大家整理纸上花园。')
            store.set_setting('group_passive:' + key, '0' if index == 3 else '1')
            store.set_setting('group_active:' + key, '1')
            self.runtime.group_profile.save(key, index * 7)
            store.add_api_call(key, 'fixture', 'fixture-model', 120 * index, 36 * index, 0, True,
                               cached_tokens=40 * index)
            store.add_api_call(key, 'fixture', 'fixture-model', 0, 0, 0, False, error_code='fixture_example')
        import logging
        logging.getLogger('newGeYi.preview').warning('隔离验收已 seed 三个虚构群会话；调用统计也是虚构记录')

    @staticmethod
    def validate_request(request):
        if not isinstance(request, dict) or request.get('type') != 'olivos:plugin_event':
            raise ValueError('请求 type 应为 olivos:plugin_event')
        if request.get('event') != EVENT_NAME:
            raise ValueError('请求 event 应为 ' + EVENT_NAME)
        request_id = request.get('request_id')
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
            raise ValueError('request_id 应为 1 至 128 字符')
        if not isinstance(request.get('payload'), dict):
            raise ValueError('payload 应为 JSON 对象')
        return request

    def dispatch(self, request):
        request = self.validate_request(request)
        event = FakeEvent(request, self.session, self.guards)
        previous = len(self.guards.attempts)
        self.panel.handle_event(event, None)
        require(len(self.guards.attempts) == previous, 'Panel 触发联网、QQ 接口或线程：查看终端 ISOLATION BLOCKED')
        require(event.reply_payload is not None, 'Panel 未按原 request_id 回包')
        require(id(self.runtime.config) == self.config_identity, 'Panel 替换了共享 config 对象')
        return {'type': 'olivos:plugin_reply', 'request_id': request['request_id'], 'payload': event.reply_payload}

    def call(self, action, **params):
        self.sequence += 1
        request = {'type': 'olivos:plugin_event', 'event': EVENT_NAME,
                   'request_id': 'self-test-' + str(self.sequence), 'payload': dict(params, action=action)}
        response = self.dispatch(request)['payload']
        require(response.get('ok') is True, action + ' 失败：' + str(response))
        require(isinstance(response.get('data'), dict), action + ' data schema 错误')
        return response['data']

    def snapshot(self):
        runtime = self.runtime
        return {
            'database': hashlib.sha256('\n'.join(runtime.store._conn().iterdump()).encode('utf-8')).hexdigest(),
            'config': copy.deepcopy(runtime.config), 'role': runtime.prompt_builder.role_prompt,
            'disk_config': (self.scratch.path / 'config.json').read_bytes(),
            'disk_prompt': (self.scratch.path / 'role_prompt.webui.txt').read_bytes(),
            'live': copy.deepcopy(dict(runtime.live_context._states)), 'live_order': runtime.live_context._order,
            'scores': copy.deepcopy(dict(runtime.attention._recent_scores)),
            'queue': copy.deepcopy(runtime.scheduler.pending_snapshot()),
        }

    def self_test(self):
        config = self.call('config_get')
        require({'config', 'revision', 'editable_keys', 'secret_fields'} <= config.keys(), 'config_get schema')
        for field in ('api_key', 'extra_headers', 'preview_secret', 'voice_auth_token'):
            require(field not in config['config'], 'config_get 暴露私有配置字段：' + field)
        overview = self.call('overview')
        require({'version', 'uptime_seconds', 'bots', 'paths', 'flags', 'providers', 'status'} <= overview.keys(),
                'overview schema')
        require(overview['bots'] == [], 'preview 出现机器人账号')
        for value in overview['paths'].values():
            require(Path(value).resolve().is_relative_to(self.scratch.path), 'overview 路径未隔离')
        sessions = self.call('sessions')
        require(sessions['total'] == 3 and len(sessions['items']) == 3, '虚构会话目录 schema')
        for key in self.keys:
            data = self.call('session_get', session_key=key)
            require({'session_key', 'turns', 'memories', 'state', 'usage'} <= data.keys(), 'session_get schema')
            require(len(data['turns']) == 3 and {m['layer'] for m in data['memories']} == {'L1', 'L2', 'L3'},
                    'turns/memory seed 不完整')
            require(data['usage']['calls'] == 2, 'usage schema')
        prompt = self.call('prompt_get')
        require(prompt['text'] == ROLE, '角色未使用 scratch override')
        logs = self.call('logs')
        require({'items', 'next_since', 'dropped'} <= logs.keys(), 'logs schema')
        before = self.snapshot()
        for key in self.keys:
            for mentioned in (False, True):
                data = self.call('debug_preview', session_key=key, sender_id='fixture-reader-1',
                                 sender_name='虚构验收员', text='星灯，我们去纸上花园读书吧？',
                                 is_group=True, mentioned_bot=mentioned, is_active=False)
                require({'route', 'messages', 'summary', 'effects'} <= data.keys(), 'debug_preview schema')
                require({'branch', 'reason'} <= data['route'].keys(), 'debug route schema')
                require({'message_count', 'history_count', 'chars', 'omitted', 'note'} <= data['summary'].keys(),
                        'debug summary schema')
                require(data['effects'] == {'network': False, 'send': False, 'persist': False}, 'debug effects')
                require(data['summary']['message_count'] == len(data['messages']), 'debug message_count')
        require(self.snapshot() == before, 'debug_preview 写了数据库/配置/角色/内存状态')
        print('PASS state_unchanged_by_debug', flush=True)
        secrets = {name: copy.deepcopy(self.runtime.config[name]) for name in
                   ('api_key', 'extra_headers', 'preview_secret', 'voice_auth_token', 'voice_onebot_access_token')}
        original = self.runtime.config['fast_temperature']
        candidate = 0.42 if original != 0.42 else 0.43
        saved = self.call('config_save', confirm=True, revision=config['revision'], patch={'fast_temperature': candidate})
        require(saved['config']['fast_temperature'] == candidate, 'config_save roundtrip')
        disk = json.loads((self.scratch.path / 'config.json').read_text(encoding='utf-8'))
        for name, expected in secrets.items():
            require(self.runtime.config[name] == expected and disk[name] == expected, '配置私有字段丢失：' + name)
        require(disk['fast_temperature'] == candidate, '配置没有真正 Python 落盘')
        self.call('config_reload', confirm=True, revision=saved['revision'])
        print('PASS config_secret_preserved; Python config_save/config_reload roundtrip', flush=True)
        current = self.call('prompt_get')
        new_role = ROLE + '\n虚构验收员确认了真正 Python 写盘 roundtrip。'
        self.call('prompt_save', confirm=True, revision=current['revision'], text=new_role)
        returned_role = self.call('prompt_get')['text']
        require(returned_role.replace('\r\n', '\n') == new_role, 'prompt_save roundtrip')
        if returned_role != new_role:
            print('NOTE prompt_get exposes Windows CRLF; text comparison normalizes CRLF only', flush=True)
        require((self.scratch.path / 'role_prompt.webui.txt').read_text(encoding='utf-8') == new_role,
                'prompt_save 未落盘')
        require(not self.guards.attempts, '验收出现隔离副作用')
        print('PASS all_known_actions; prompt_save roundtrip; network=0; QQ=0; threads=0', flush=True)

    def close(self):
        try:
            if self.panel is not None:
                self.panel.close()
            if self.runtime is not None:
                # 无生命周期调用，仅关闭本工具创建的连接与惰性线程池。
                self.runtime.scheduler._executor.shutdown(wait=False, cancel_futures=True)
                self.runtime.voice_manager._executor.shutdown(wait=False, cancel_futures=True)
                self.runtime.store.close()
        finally:
            for item in reversed(self.config_patches):
                item.stop()
            for name in tuple(sys.modules):
                if name == PACKAGE or name.startswith(PACKAGE + '.'):
                    sys.modules.pop(name, None)
            self.guards.close()
            for name in tuple(os.environ):
                if name.startswith('GEYI_'):
                    os.environ.pop(name)
            os.environ.update(self.old_environment)
            sys.dont_write_bytecode = self.old_bytecode


HOST_HTML = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>newGeYi 隔离验收</title>
<style>
body{margin:0;font:14px/1.5 system-ui,sans-serif;background:#f4f5f7;color:#17202a}
header{padding:12px 18px;background:#fff4cf;border-bottom:1px solid #d8c778}
strong{display:block;font-size:17px}small{display:block;overflow-wrap:anywhere}
iframe{display:block;width:100%;height:calc(100vh - 180px);min-height:480px;border:0;background:white}
details{padding:10px 18px;background:white}textarea{width:100%;height:90px;box-sizing:border-box}
button{margin:6px 8px 6px 0;padding:6px 12px}pre{max-height:260px;overflow:auto;white-space:pre-wrap}
</style>
<header><strong>隔离验收 · 临时数据 · 不连接QQ/API</strong>
<small>scratch: __SCRATCH__ · __POLICY__ · 原生 opaque iframe / requests 强制 stub / 仅开发工具</small>
<small id="bridge-status" role="status">消息桥已准备；面板只通过本机 Python Panel 收发。</small></header>
<details><summary>父统筹验收：真实 Python JSON roundtrip / scratch 配置写盘</summary>
<p>下面是工具宿主页控制台，不属于生产页面；“验收配置写盘”只修改本次 scratch 的 temperature。</p>
<textarea id="request" aria-label="JSON action">{"action":"overview"}</textarea>
<button id="raw-send" type="button">执行 JSON</button><button id="save-test" type="button">验收配置写盘</button>
<pre id="result" aria-live="polite"></pre></details>
<iframe id="panel" title="newGeYi 原生页面" src="/panel"
 sandbox="allow-scripts allow-forms allow-modals allow-downloads"></iframe>
<script>
'use strict';
const frame = document.getElementById('panel');
const status = document.getElementById('bridge-status');
const result = document.getElementById('result');
let serial = 0;
const inflight = new Set();
function requestID() {return 'preview-' + Date.now() + '-' + (++serial);}
async function submit(envelope) {
  if (inflight.size >= 128) throw new Error('待处理请求达到 128 上限');
  if (!envelope || envelope.type !== 'olivos:plugin_event' || envelope.event !== 'newGeYi_WebUI'
      || typeof envelope.request_id !== 'string' || !envelope.request_id || envelope.request_id.length > 128
      || !envelope.payload || typeof envelope.payload !== 'object' || Array.isArray(envelope.payload)) {
    throw new Error('消息桥请求格式错误');
  }
  if (inflight.has(envelope.request_id)) throw new Error('request_id 正在处理中');
  const body = JSON.stringify({type: envelope.type, event: envelope.event,
                             request_id: envelope.request_id, payload: envelope.payload});
  if (new TextEncoder().encode(body).byteLength > 1048576) throw new Error('请求体超过 1 MiB');
  inflight.add(envelope.request_id);
  try {
    const response = await fetch('/api', {method:'POST', credentials:'omit', cache:'no-store',
      headers:{'Content-Type':'application/json', 'X-Preview-Bridge':'1'}, body});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '本机 HTTP ' + response.status);
    if (data.type !== 'olivos:plugin_reply' || data.request_id !== envelope.request_id) {
      throw new Error('Python 回包协议不匹配');
    }
    return data;
  } finally {inflight.delete(envelope.request_id);}
}
window.previewRequest = async function(payload) {
  return submit({type:'olivos:plugin_event', event:'newGeYi_WebUI', request_id:requestID(), payload});
};
window.addEventListener('message', async function(event) {
  if (event.source !== frame.contentWindow || event.origin !== 'null') return;
  const data = event.data;
  if (!data || data.type !== 'olivos:plugin_event') return;
  if (typeof data.request_id !== 'string' || !data.request_id || data.request_id.length > 128) return;
  let reply;
  try {
    reply = await submit(data);
    status.textContent = 'Python roundtrip: ' + data.payload.action + ' · ' + data.request_id;
  } catch (error) {
    reply = {type:'olivos:plugin_reply', request_id:data.request_id, error:String(error.message || error)};
    status.textContent = reply.error;
  }
  frame.contentWindow.postMessage(reply, '*');
});
frame.addEventListener('load', function() {
  frame.contentWindow.postMessage({type:'olivos:plugin_visibility',visible:true}, '*');
});
document.getElementById('raw-send').addEventListener('click', async function() {
  try {result.textContent = JSON.stringify(await window.previewRequest(
    JSON.parse(document.getElementById('request').value)), null, 2);}
  catch (error) {result.textContent = String(error.message || error);}
});
document.getElementById('save-test').addEventListener('click', async function() {
  this.disabled = true;
  try {
    const before = await window.previewRequest({action:'config_get'});
    if (!before.payload.ok) throw new Error(before.payload.error);
    const old = before.payload.data.config.fast_temperature;
    const value = old === 0.42 ? 0.43 : 0.42;
    const saved = await window.previewRequest({action:'config_save', confirm:true,
      revision:before.payload.data.revision, patch:{fast_temperature:value}});
    if (!saved.payload.ok) throw new Error(saved.payload.error);
    const readback = await window.previewRequest({action:'config_get'});
    if (readback.payload.data.config.fast_temperature !== value) throw new Error('读取不一致');
    result.textContent = JSON.stringify({python_roundtrip:true, scratch_write:true,
      old_temperature:old, new_temperature:value, response:readback}, null, 2);
  } catch(error) {result.textContent = String(error.message || error);}
  finally {this.disabled = false;}
});
</script></html>'''


class PreviewServer(HTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        # 绕过 HTTPServer 对 server_name 的 getfqdn/DNS；唯一 bind 是数值 loopback。
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = '127.0.0.1'
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = 'GeYiPreview/1'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        # 不记录 body、查询串或凭据；目录完全不作静态映射。
        print('[preview HTTP] ' + str(self.command) + ' ' +
              ('whitelisted' if self.path in ('/', '/panel', '/api') else 'not-found'), flush=True)

    def response(self, status, body, content_type='application/json; charset=utf-8', csp=HOST_CSP):
        self.close_connection = True
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', csp)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def error(self, status, message):
        self.response(status, encoded({'ok': False, 'error': message}))

    def valid_host(self):
        expected = '127.0.0.1:' + str(self.server.server_port)
        if self.headers.get_all('Host') != [expected]:
            self.error(403, '请使用终端打印的 127.0.0.1 URL')
            return False
        return True

    def do_GET(self):
        if not self.valid_host():
            return
        if self.path == '/':
            scratch = self.server.fixture.scratch
            page = HOST_HTML.replace('__SCRATCH__', html.escape(str(scratch.path))).replace(
                '__POLICY__', '关闭保留 scratch' if scratch.keep else 'Ctrl+C 正常关闭清理 scratch')
            self.response(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        elif self.path == '/panel':
            # 只读这个固定文件，含编码/路径检查；不允许目录、联接、源码或生产数据映射。
            path = ROOT / 'webui' / 'index.html'
            try:
                if path.is_symlink() or path.parent.is_symlink() or path.resolve() != path:
                    self.error(403, 'panel 路径必须是插件内普通文件')
                    return
                page = path.read_text(encoding='utf-8')
            except FileNotFoundError:
                self.error(503, 'webui/index.html 尚未生成；前端落盘后刷新即可')
                return
            except (OSError, UnicodeError):
                self.error(500, 'panel HTML 读取失败，请检查文件编码')
                return
            self.response(200, page.encode('utf-8'), 'text/html; charset=utf-8', PANEL_CSP)
        else:
            self.error(404, '仅开放 /、/panel 和 POST /api')

    def do_POST(self):
        if not self.valid_host():
            return
        if self.path != '/api':
            self.error(404, '仅开放 POST /api')
            return
        origin = self.headers.get('Origin')
        expected = 'http://127.0.0.1:' + str(self.server.server_port)
        if (origin is not None and origin != expected) or self.headers.get('X-Preview-Bridge') != '1':
            self.error(403, '请经隔离宿主页消息桥提交')
            return
        if self.headers.get('Transfer-Encoding'):
            self.error(400, '请求应使用单个 Content-Length，不接受分块传输')
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.error(411, '需要有效的单个 Content-Length')
            return
        # 先限位数，避免对超长数字做 int；读体之前检查 1 MiB。
        if len(lengths[0]) > 10 or int(lengths[0]) > BODY_LIMIT:
            self.error(413, 'JSON 请求体上限为 1 MiB')
            return
        length = int(lengths[0])
        if length < 1:
            self.error(400, 'JSON 请求体为空')
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self.error(415, 'Content-Type 应为 application/json')
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                self.error(400, 'JSON 请求体未完整发送')
                return

            def bad_constant(value):
                raise ValueError('JSON 数值应为有限值')

            request = json.loads(raw.decode('utf-8'), parse_constant=bad_constant)
            self.server.fixture.validate_request(request)
        except (UnicodeError, ValueError, RecursionError):
            self.error(400, 'JSON 格式错误：需 UTF-8 对象与有效 type/event/request_id/payload')
            return
        except (TimeoutError, OSError):
            self.error(408, 'JSON 请求体读取超时或连接中断')
            return
        try:
            reply = self.server.fixture.dispatch(request)
            self.response(200, encoded(reply))
        except (AssertionError, IsolationViolation):
            self.error(500, '隔离验收捕获 contract 或副作用差异；查看终端并运行 --self-test')
        except Exception as exc:
            print('[preview backend error] ' + type(exc).__name__, file=sys.stderr, flush=True)
            self.error(500, 'Python Panel 处理失败；查看终端诊断')

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self.error(405, '使用 GET 页面或 POST /api；跨源 CORS 未开放')


def port_number(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('port 应为整数') from exc
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError('port 范围是 0 至 65535')
    return number


def main():
    parser = argparse.ArgumentParser(description='仅开发验收：临时资料 / 无 QQ / 无 API / opaque 原生消息桥')
    parser.add_argument('--port', type=port_number, default=0, help='监听端口；默认 0 由系统分配')
    parser.add_argument('--keep-data', action='store_true', help='正常关闭保留本次新建 scratch（不读取旧目录）')
    parser.add_argument('--self-test', action='store_true', help='验证已知 actions/写盘/只读不变量，然后退出；不启动 HTTP')
    args = parser.parse_args()
    scratch = Scratch(args.keep_data)
    fixture = Fixture(scratch)
    server = None
    code = 0
    try:
        fixture.open()
        if args.self_test:
            fixture.self_test()
        else:
            server = PreviewServer(('127.0.0.1', args.port), Handler)
            server.fixture = fixture
            print('URL=http://127.0.0.1:' + str(server.server_port) + '/', flush=True)
            print('PORT=' + str(server.server_port), flush=True)
            print('隔离验收 · 临时数据 · 不连接QQ/API；Ctrl+C 关闭。', flush=True)
            server.serve_forever(poll_interval=0.25)  # 单线程工具 host，不启动插件 worker。
    except KeyboardInterrupt:
        print('\nPREVIEW_STOPPED=Ctrl+C', flush=True)
    except Exception as exc:
        code = 1
        print('PREVIEW_FAILED=' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr, flush=True)
    finally:
        if server is not None:
            server.server_close()
        try:
            fixture.close()
        finally:
            scratch.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
