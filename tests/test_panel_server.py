# -*- encoding: utf-8 -*-
"""本地面板宿主（panel_server）契约测试。

为什么要这个模块：OlivOS 0.11.x 的核心没有 WebUI（`webui_config` 是 1.0.x 的能力），
侧栏永远不会有「插件页面」。这里起 127.0.0.1 小服务，把同一个 `webui/index.html`
桥到进程内同一个 `Panel`。测试重点：只有带令牌的路径可用、写操作不能被 CSRF、
静态资源不许越界、回包就是 Panel 的回包。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import os

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_panel_server_tests_'))

from newGeYi import config as plugin_config  # noqa: E402
from newGeYi.panel import EVENT_NAME, Panel  # noqa: E402
from newGeYi.panel_server import LocalPanelServer  # noqa: E402
from newGeYi.runtime.app import Runtime  # noqa: E402


def _request(url, data=None, headers=None, method=None):
    body = None if data is None else json.dumps(data).encode('utf-8')
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers.get('Content-Type', '')
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get('Content-Type', '')


class PanelServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = Runtime()
        cls.runtime.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')
        cls.panel = Panel(cls.runtime)
        cls.server = LocalPanelServer(cls.panel, {'panel_local_enabled': True,
                                                 'panel_local_port': 0})
        cls.url = cls.server.start()
        cls.base = cls.url.rstrip('/')

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.panel.close()
        try:
            cls.runtime.store.close()
        except Exception:  # noqa: BLE001 - 关不掉不算失败
            pass

    # ---------- 静态页面 ----------
    def test_wrapper_and_page_are_served(self):
        self.assertTrue(self.url.startswith('http://127.0.0.1:'), self.url)
        status, body, kind = _request(self.url)
        self.assertEqual(status, 200)
        self.assertIn('iframe', body.decode('utf-8'))
        self.assertIn('text/html', kind)

        status, body, _kind = _request(self.base + '/page')
        self.assertEqual(status, 200)
        page = body.decode('utf-8')
        self.assertIn(EVENT_NAME, page)
        self.assertIn('歌伊', page)

        status, body, kind = _request(self.base + '/assets/geyi-avatar.webp')
        self.assertEqual(status, 200)
        self.assertEqual(kind, 'image/webp')
        self.assertEqual(body[:4], b'RIFF')

    def test_token_is_required(self):
        root = self.url.split('/p/')[0]
        status, _body, _kind = _request(root + '/p/wrong-token/')
        self.assertEqual(status, 404)
        status, _body, _kind = _request(root + '/p/wrong-token/page')
        self.assertEqual(status, 404)

    def test_traversal_is_blocked(self):
        status, _body, _kind = _request(self.base + '/assets/../../app.json')
        self.assertIn(status, (403, 404))

    # ---------- 桥接 ----------
    def _envelope(self, payload, request_id='t-1'):
        return {'type': 'olivos:plugin_event', 'event': EVENT_NAME,
                'request_id': request_id, 'payload': payload}

    def test_overview_roundtrip(self):
        status, body, kind = _request(
            self.base + '/api', self._envelope({'action': 'overview'}),
            headers={'Content-Type': 'application/json', 'X-Geyi-Panel': '1'})
        self.assertEqual(status, 200)
        self.assertIn('application/json', kind)
        reply = json.loads(body.decode('utf-8'))
        self.assertEqual(reply['type'], 'olivos:plugin_reply')
        self.assertEqual(reply['request_id'], 't-1')
        self.assertTrue(reply['payload']['ok'])
        self.assertEqual(reply['payload']['data']['version'], plugin_config.PLUGIN_VERSION)

    def test_config_get_hides_secrets_and_exposes_new_keys(self):
        status, body, _kind = _request(
            self.base + '/api', self._envelope({'action': 'config_get'}, 't-2'),
            headers={'Content-Type': 'application/json', 'X-Geyi-Panel': '1'})
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))['payload']['data']
        self.assertIn('panel_local_enabled', data['editable_keys'])
        self.assertIn('panel_local_port', data['editable_keys'])
        self.assertNotIn('"api_key": "', json.dumps(data['config']))

    def test_post_without_bridge_header_is_rejected(self):
        status, _body, _kind = _request(self.base + '/api', self._envelope({'action': 'overview'}))
        self.assertEqual(status, 403, '没有自定义头的请求必须被挡（跨站写操作防线）')

    def test_foreign_origin_is_rejected(self):
        status, _body, _kind = _request(
            self.base + '/api', self._envelope({'action': 'overview'}),
            headers={'Content-Type': 'application/json', 'X-Geyi-Panel': '1',
                     'Origin': 'http://evil.example'})
        self.assertEqual(status, 403)

    def test_unknown_action_reports_panel_error(self):
        status, body, _kind = _request(
            self.base + '/api', self._envelope({'action': 'nope'}, 't-3'),
            headers={'Content-Type': 'application/json', 'X-Geyi-Panel': '1'})
        self.assertEqual(status, 200)
        payload = json.loads(body.decode('utf-8'))['payload']
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['code'], 'unknown_action')

    def test_dispatch_rejects_bad_envelope(self):
        reply = self.server.dispatch({'type': 'nope'})
        self.assertFalse(reply['ok'])
        reply = self.server.dispatch({'type': 'olivos:plugin_event', 'event': EVENT_NAME,
                                      'request_id': 'x', 'payload': []})
        self.assertFalse(reply['ok'])

    def test_oversized_body_is_refused(self):
        status, _body, _kind = _request(
            self.base + '/api', {'type': 'olivos:plugin_event', 'blob': 'x' * (1 << 21)},
            headers={'Content-Type': 'application/json', 'X-Geyi-Panel': '1'})
        self.assertEqual(status, 413)


class PanelServerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.runtime.prompt_builder.set_role_prompt('你是歌伊，一个测试用人设。')
        self.panel = Panel(self.runtime)

    def tearDown(self):
        self.panel.close()
        try:
            self.runtime.store.close()
        except Exception:  # noqa: BLE001
            pass

    def test_disabled_start_is_noop(self):
        server = LocalPanelServer(self.panel, {'panel_local_enabled': False,
                                              'panel_local_port': 0})
        self.assertEqual(server.start(), '')
        self.assertFalse(server.running)

    def test_stop_releases_port(self):
        server = LocalPanelServer(self.panel, {'panel_local_enabled': True,
                                              'panel_local_port': 0})
        url = server.start()
        self.assertTrue(url)
        port = int(url.split(':')[2].split('/')[0])
        server.stop()
        with self.assertRaises(OSError):
            urllib.request.urlopen('http://127.0.0.1:%d/' % port, timeout=3).read()

    def test_port_busy_is_tolerated(self):
        first = LocalPanelServer(self.panel, {'panel_local_enabled': True, 'panel_local_port': 0})
        url = first.start()
        port = int(url.split(':')[2].split('/')[0])
        second = LocalPanelServer(self.panel, {'panel_local_enabled': True,
                                              'panel_local_port': port})
        try:
            self.assertEqual(second.start(), '', '端口占用时应该只打 warning，不让插件崩')
            self.assertFalse(second.running)
        finally:
            second.stop()
            first.stop()

    def test_token_is_not_reused(self):
        a = LocalPanelServer(self.panel, {'panel_local_enabled': True, 'panel_local_port': 0})
        b = LocalPanelServer(self.panel, {'panel_local_enabled': True, 'panel_local_port': 0})
        self.assertNotEqual(a.token, b.token)


if __name__ == '__main__':
    unittest.main(verbosity=2)
