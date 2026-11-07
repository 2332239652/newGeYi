# -*- encoding: utf-8 -*-
"""本地面板宿主：给"核心没有 OlivOS WebUI"的机器补一个 127.0.0.1 页面入口。

为什么需要它（2026-11-04 实测）：OlivOS 的「插件页面」是 **1.0.x** 才有的能力
（`app.json` 的 `webui_config` + `olivos:plugin_event` 消息桥 + `plugin/cache/webui/`
资源释放）。0.11.x 的核心完全不认识这个字段，插件写得再对，侧栏也不会出现入口：

- 日志里从来没有 `WebUI 已启动`（0 次），也没有任何 `webui` 关键字；
- 没有 `conf/webui_auth.json`、没有 `data/webui/static`；
- 没有 `plugin/cache/webui/`（宿主从未释放过页面资源）；
- `OlivOS.exe` 只有一个 `0.0.0.0:55001`，对 `/`、`/webui`、`/console` 全 404。

所以这里起一个**只绑 127.0.0.1** 的极小 HTTP 服务，把同一个 `webui/index.html`
包在宿主壳里，桥接到**进程内同一个 `Panel`**（真配置、真数据库、真写入）。

刻意不复制 Panel 的任何校验逻辑：请求一律折成 `Event.menu` 的形状交给
`Panel.handle_event` —— 白名单、revision 乐观锁、写幂等、凭据脱敏全都照旧生效。
宿主支持 `webui_config` 之后可以 `panel_local_enabled=false` 把它关掉。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger('newGeYi.panel_server')

HOST = '127.0.0.1'                  # 只监听回环，别改
EVENT_NAME = 'newGeYi_WebUI'
NAMESPACE = 'newGeYi'
MAX_BODY = 1 << 20                  # 与 OlivOS 的 /api/plugin_event 单请求上限一致
_BRIDGE_HEADER = 'X-Geyi-Panel'     # 自定义头 → 跨域 POST 必须先过预检（我们不给 CORS）

_HOST_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>歌伊调试台</title>
<style>
html,body{height:100%;margin:0;background:#0f1115;color:#e7e9ee;font:14px/1.5 system-ui,sans-serif}
header{position:fixed;inset:0 0 auto 0;height:34px;display:flex;align-items:center;gap:10px;
padding:0 14px;background:#171a21;border-bottom:1px solid #262b36;font-size:12px;z-index:2}
#dot{width:8px;height:8px;border-radius:50%;background:#8a8f9c}
#dot.ok{background:#3ecf8e}#dot.bad{background:#e5534b}
#where{margin-left:auto;opacity:.6}
iframe{position:fixed;inset:34px 0 0 0;width:100%;height:calc(100% - 34px);border:0;background:#fff}
a{color:#7cc4ff}
</style></head>
<body>
<header><span id="dot"></span><span id="state">正在连接本机 Python 面板…</span>
<span id="where">127.0.0.1 本地面板 · 非 OlivOS WebUI</span></header>
<iframe id="panel" title="歌伊调试台" src="./page"></iframe>
<script>
'use strict';
const frame = document.getElementById('panel');
const dot = document.getElementById('dot');
const state = document.getElementById('state');
const BASE = './';
let serial = 0;
const inflight = new Set();

function requestID() { return 'local-' + Date.now() + '-' + (++serial); }

function mark(ok, text) {
  dot.className = ok ? 'ok' : 'bad';
  state.textContent = text;
}

async function submit(envelope) {
  if (inflight.size >= 128) throw new Error('待处理请求达到 128 上限');
  const body = JSON.stringify({type: envelope.type, event: envelope.event,
                               request_id: envelope.request_id, payload: envelope.payload});
  if (new TextEncoder().encode(body).byteLength > 1048576) throw new Error('请求体超过 1 MiB');
  inflight.add(envelope.request_id);
  try {
    const response = await fetch(BASE + 'api', {
      method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-Geyi-Panel': '1'}, body});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || ('本机 HTTP ' + response.status));
    if (data.type !== 'olivos:plugin_reply' || data.request_id !== envelope.request_id) {
      throw new Error('Python 回包协议不匹配');
    }
    mark(true, '已连接 · 本机进程内 Panel');
    return data;
  } finally { inflight.delete(envelope.request_id); }
}

window.addEventListener('message', async function (event) {
  if (event.source !== frame.contentWindow) return;
  const data = event.data;
  if (!data || data.type !== 'olivos:plugin_event') return;
  if (typeof data.request_id !== 'string' || !data.request_id || data.request_id.length > 128) return;
  let reply;
  try {
    reply = await submit(data);
  } catch (error) {
    reply = {type: 'olivos:plugin_reply', request_id: data.request_id,
             error: String(error && error.message || error)};
    mark(false, '桥接失败：' + (error && error.message || error));
  }
  frame.contentWindow.postMessage(reply, '*');
});

frame.addEventListener('load', function () {
  frame.contentWindow.postMessage({type: 'olivos:plugin_visibility', visible: true}, '*');
  mark(true, '页面已加载；在页面里点任意操作即可验证桥接');
});
</script>
</body></html>
'''


class _BridgeEvent:
    """把一次 HTTP 请求折成 OlivOS 的 `Event.menu` 形状，回包走 `send`。"""

    def __init__(self, data):
        self.data = data
        self.reply = None

    def send(self, channel, request_id, payload, *args, **kwargs):
        if channel == 'webui':
            self.reply = payload
        return True


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def _make_handler(server: 'LocalPanelServer'):
    root = server.web_root
    token_path = '/p/%s/' % server.token
    allowed_hosts = {HOST, 'localhost'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'GeyiPanel'
        sys_version = ''

        # 不写访问日志（OlivOS 的日志桥只关心插件业务）
        def log_message(self, fmt, *args):  # noqa: A003 - 覆盖标准库签名
            logger.debug('本地面板 %s', fmt % args)

        # ---------- 工具 ----------
        def _send(self, status, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            # 同源使用，不需要任何跨域许可；明确拒绝被别的站点内嵌
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def _json(self, status, value) -> None:
            self._send(status, _json_bytes(value), 'application/json; charset=utf-8')

        def _host_ok(self) -> bool:
            """挡 DNS rebinding：Host 头必须就是本机。"""
            raw = (self.headers.get('Host') or '').strip().lower()
            name = raw.rsplit(':', 1)[0] if raw else ''
            return name in allowed_hosts

        def _origin_ok(self) -> bool:
            origin = (self.headers.get('Origin') or '').strip().lower()
            if not origin:
                return True                     # 同源 fetch 不一定带 Origin
            return origin in {'http://%s:%d' % (HOST, self.server.server_address[1]),
                              'http://localhost:%d' % self.server.server_address[1]}

        def _route(self):
            """返回令牌之后的相对路径；令牌不对返回 None（不能当成宿主页兜底）。"""
            path = (self.path or '').split('?', 1)[0]
            if not path.startswith(token_path):
                return None
            return path[len(token_path):]

        def _file(self, relative: str):
            try:
                candidate = (root / relative).resolve(strict=True)
            except (OSError, RuntimeError):
                return None
            if candidate != root and root not in candidate.parents:
                return None                     # 越界（.. 或符号链接）
            return candidate if candidate.is_file() else None

        # ---------- 路由 ----------
        def do_GET(self):  # noqa: N802 - 标准库要求这个名字
            if not self._host_ok():
                self._json(403, {'error': '仅允许本机 Host 访问'})
                return
            route = self._route()
            if route is None:
                self._json(404, {'error': '路径无效'})
                return
            if route in ('', 'index.html'):
                self._send(200, _HOST_HTML.encode('utf-8'), 'text/html; charset=utf-8')
                return
            if route in ('page', 'page/'):
                target = self._file('index.html')
                if target is None:
                    self._json(404, {'error': 'webui/index.html 缺失'})
                    return
                self._send(200, target.read_bytes(), 'text/html; charset=utf-8')
                return
            if route.startswith('assets/'):
                target = self._file(route)
                if target is None:
                    self._json(404, {'error': '资源不存在'})
                    return
                suffix = target.suffix.lower()
                kind = {'.webp': 'image/webp', '.png': 'image/png', '.jpg': 'image/jpeg',
                        '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.css': 'text/css; charset=utf-8',
                        '.js': 'application/javascript; charset=utf-8'}.get(suffix, 'application/octet-stream')
                self._send(200, target.read_bytes(), kind)
                return
            self._json(404, {'error': '未知路径'})

        def _drain(self, length: int) -> None:
            """把请求体读掉再回包。

            不读就关连接时，Windows 会因为 socket 里还有未读数据直接发 RST，
            浏览器/客户端只能看到"连接被重置"，看不到我们那个 413。
            """
            try:
                remaining = min(max(0, int(length)), 4 << 20)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except Exception:  # noqa: BLE001 - 读不掉就算了，回包优先
                pass

        def do_POST(self):  # noqa: N802
            if not self._host_ok() or not self._origin_ok():
                self._json(403, {'error': '来源不被允许'})
                return
            if self.headers.get(_BRIDGE_HEADER) != '1':
                # 没有自定义头 = 跨站表单/简单请求，直接拒绝（写操作不能被 CSRF）
                self._json(403, {'error': '缺少本机桥接头'})
                return
            if self._route() != 'api':
                self._json(404, {'error': '未知路径'})
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                length = -1
            if length <= 0 or length > MAX_BODY:
                self._drain(length)
                self._json(413, {'error': '请求体为空或过大'})
                return
            try:
                envelope = json.loads(self.rfile.read(length).decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                self._json(400, {'error': '不是合法 JSON'})
                return
            self._json(200, server.dispatch(envelope))

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

    return Handler


class _PanelHTTPServer(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 允许「抢绑」别人已经监听的端口，端口冲突会静默变成
    # 两个服务抢同一个口。宁可启动失败（只打 warning）也不要这种假成功。
    allow_reuse_address = False
    daemon_threads = True


class LocalPanelServer:
    """进程内本地面板宿主；`start()` 之后用 `url` 打开浏览器。"""

    def __init__(self, panel, config: dict | None = None):
        self.panel = panel
        self.config = panel.runtime.config if config is None else config
        self.token = secrets.token_urlsafe(24)
        self.web_root = (Path(__file__).resolve().parent / 'webui').resolve()
        self._http = None
        self._thread = None
        self._url = ''

    # ---------- 配置 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get('panel_local_enabled', True))

    @property
    def port(self) -> int:
        try:
            value = int(self.config.get('panel_local_port', 18765))
        except (TypeError, ValueError):
            return 18765
        return value if 0 <= value <= 65535 else 18765

    @property
    def url(self) -> str:
        return self._url

    @property
    def running(self) -> bool:
        return self._http is not None

    # ---------- 生命周期 ----------
    def start(self) -> str:
        if not self.enabled:
            logger.info('本地面板已在配置里关闭（panel_local_enabled=false）')
            return ''
        if self._http is not None:
            return self._url
        try:
            httpd = _PanelHTTPServer((HOST, self.port), _make_handler(self))
        except OSError as exc:
            logger.warning('本地面板启动失败（端口 %s）：%s；改 config.json 的 panel_local_port 或设 0 自动选端口',
                           self.port, exc)
            return ''
        self._http = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name='geyi-panel', daemon=True)
        self._thread.start()
        self._url = 'http://%s:%d/p/%s/' % (HOST, httpd.server_address[1], self.token)
        logger.info('本地面板已启动：%s （只监听 127.0.0.1；QQ 里发「面板地址」可再取一次）', self._url)
        return self._url

    def stop(self) -> None:
        httpd, self._http = self._http, None
        self._url = ''
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001 - 关闭失败不影响插件卸载
            logger.debug('本地面板 shutdown 异常', exc_info=True)
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            logger.debug('本地面板 close 异常', exc_info=True)

    # ---------- 桥接 ----------
    def dispatch(self, envelope) -> dict:
        """校验信封 → 交给 Panel.handle_event → 取回包。"""
        if not isinstance(envelope, dict) or envelope.get('type') != 'olivos:plugin_event':
            return {'ok': False, 'error': '请求 type 应为 olivos:plugin_event'}
        request_id = envelope.get('request_id')
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return {'ok': False, 'error': 'request_id 无效'}
        if envelope.get('event') != EVENT_NAME:
            return {'ok': False, 'error': '未知 WebUI 事件'}
        payload = envelope.get('payload')
        if not isinstance(payload, dict):
            return {'ok': False, 'error': 'payload 必须是对象'}
        data = SimpleNamespace(namespace=NAMESPACE, event=EVENT_NAME, payload=payload,
                               webui={'request_id': request_id, 'payload': payload,
                                      'session': 'local-panel'})
        event = _BridgeEvent(data)
        self.panel.handle_event(event, None)
        if event.reply is None:
            return {'ok': False, 'error': '面板没有回包（可能已关闭）', 'code': 'no_reply'}
        return {'type': 'olivos:plugin_reply', 'request_id': request_id, 'payload': event.reply}
