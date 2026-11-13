# -*- encoding: utf-8 -*-
"""原生 WebUI 后端离线契约测试。

运行前先在外层 shell 设置 GEYI_DATA_DIR 为临时目录：
Python 的 -m unittest 导入 newGeYi 包早于本模块，第16行附近的隔离不保护包的首次导入。
推荐从 plugin/app 运行 python newGeYi/tools/run_webui_tests.py，由入口先隔离再加载测试。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['GEYI_DATA_DIR'] = tempfile.mkdtemp(prefix='geyi_panel_import_')

from newGeYi import config as C  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402
from newGeYi.panel import EVENT_NAME, Panel  # noqa: E402
from newGeYi.runtime.app import Runtime  # noqa: E402


class Event:
    def __init__(self, payload, request_id='req-1', session='browser-one', namespace='newGeYi', webui=True):
        self.data = types.SimpleNamespace(namespace=namespace, event=EVENT_NAME, payload=payload)
        if webui:
            self.data.webui = {'request_id': request_id, 'session': session, 'payload': payload}
        self.sent = []
        self.bot_info = None

    def send(self, *args):
        self.sent.append(args)
        return True


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get('GEYI_DATA_DIR')
        self.temp = Path(tempfile.mkdtemp(prefix='geyi_panel_case_'))
        os.environ['GEYI_DATA_DIR'] = str(self.temp)
        C._CONFIG_STATE['error'] = ''
        self.runtime = Runtime()
        self.runtime.config.update({'api_key': 'secret-top-test', 'voice_auth_token': 'secret-voice-test',
                                    'extra_headers': {'Authorization': 'Bearer secret-header-test'},
                                    'api_endpoint': 'https://user:secret-url-test@example.com/v1?access_token=secret-query-test'})
        self.runtime.gateway.reload(self.runtime.config)
        C.save_config(self.runtime.config)
        self.runtime.prompt_builder.set_role_prompt('离线测试角色设定')
        self.panel = Panel(self.runtime)
        self.session = SessionKey('bot', 'qq', 'onebot', 'group', '', '123')
        self.key = self.session.as_text()

    def tearDown(self):
        self.panel.close()
        self.runtime.scheduler.shutdown()
        self.runtime.voice_manager._executor.shutdown(wait=False)
        self.runtime.store.close()
        C._CONFIG_STATE['error'] = ''
        if self.previous is None:
            os.environ.pop('GEYI_DATA_DIR', None)
        else:
            os.environ['GEYI_DATA_DIR'] = self.previous

    def call(self, action, request_id='req-1', **params):
        event = Event(dict(params, action=action), request_id=request_id)
        self.panel.handle_event(event, None)
        self.assertEqual(event.sent[0][:2], ('webui', request_id))
        return event.sent[0][2]

    def test_event_without_bot_context(self):
        result = self.call('overview')
        self.assertTrue(result['ok'])
        self.assertIn('providers', result['data'])
        self.assertIn('role_prompt', result['data']['paths'])

    def test_ordinary_menu_and_other_namespace_are_ignored(self):
        for event in (Event({'action': 'overview'}, webui=False),
                      Event({'action': 'overview'}, namespace='other')):
            self.panel.handle_event(event, None)
            self.assertEqual(event.sent, [])

    def test_invalid_request_id_not_replied(self):
        for request_id in ('', None, 'x' * 129):
            event = Event({'action': 'overview'}, request_id=request_id)
            self.panel.handle_event(event, None)
            self.assertFalse(event.sent)

    def test_malformed_and_unknown(self):
        for payload in (None, [], {'action': []}):
            event = Event(payload)
            self.panel.handle_event(event, None)
            self.assertFalse(event.sent[0][2]['ok'])
        self.assertEqual(self.call('not_an_action')['code'], 'unknown_action')
        event = Event({'action': 'overview'})
        event.data.event = 'wrong'
        self.panel.handle_event(event, None)
        self.assertEqual(event.sent[0][2]['code'], 'unknown_event')

    def test_config_view_has_no_keys_or_url_credentials(self):
        result = self.call('config_get')
        self.assertTrue(result['ok'])
        data = result['data']
        self.assertIn('secret_fields', data)
        self.assertNotIn('api_key', data['config'])
        self.assertNotIn('extra_headers', data['config'])
        dumped = json.dumps(data)
        for secret in ('secret-top-test', 'secret-voice-test', 'secret-header-test', 'secret-url-test', 'secret-query-test'):
            self.assertNotIn(secret, dumped)
        self.assertNotIn('user:', dumped)
        self.assertNotIn('providers', data['editable_keys'])

    def test_environment_key_is_redacted_from_logs(self):
        with patch.dict(os.environ, {'GEYI_API_KEY': 'secret-env-test'}):
            self.runtime.gateway.reload(self.runtime.config)
            panel = Panel(self.runtime)
            try:
                panel._append_log('INFO', 'remote failed secret-env-test')
                dumped = json.dumps(panel._get_logs({}))
                self.assertNotIn('secret-env-test', dumped)
            finally:
                panel.close()

    def test_save_preserves_secrets_and_config_identity(self):
        identity = id(self.runtime.config)
        before = copy.deepcopy(self.runtime.config)
        revision = self.panel.config_get()['revision']
        result = self.call('config_save', patch={'passive_enabled': False}, revision=revision, confirm=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(id(self.runtime.config), identity)
        self.assertFalse(self.runtime.config['passive_enabled'])
        for key in ('api_key', 'providers', 'extra_headers', 'voice_auth_token', 'api_endpoint'):
            self.assertEqual(self.runtime.config[key], before[key])
        saved = json.loads((self.temp / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['api_key'], before['api_key'])
        self.assertTrue((self.temp / 'config.json.bak-auto').is_file())

    def test_confirmation_and_revision_required(self):
        old = (self.temp / 'config.json').read_bytes()
        revision = self.panel.config_get()['revision']
        self.assertEqual(self.call('config_save', patch={}, revision=revision)['code'], 'confirmation_required')
        self.assertEqual(self.call('config_save', patch={}, revision='stale', confirm=True,
                                   request_id='new')['code'], 'revision_conflict')
        self.assertEqual((self.temp / 'config.json').read_bytes(), old)

    def test_validation_is_strict_and_atomic(self):
        invalid = [{'providers': []}, {'api_key': 'replace'}, {'private_enabled': 'false'},
                   {'fast_temperature': float('nan')}, {'fast_max_tokens': True}, {'fast_max_tokens': 0},
                   {'owner_ids': [123]}, {'triggers': ['x' * 81]}, {'gate_weights': {'unknown': 1}},
                   {'gate_weights': {'intercept': '1'}}, {'active_tier_probability': {'1': 0.5}},
                   {'active_quiet_hours': '99:99-12:00'}]
        for index, values in enumerate(invalid):
            before = copy.deepcopy(self.runtime.config)
            disk = (self.temp / 'config.json').read_bytes()
            result = self.call('config_save', request_id='invalid-%d' % index, patch=values,
                               revision=self.panel.config_get()['revision'], confirm=True)
            self.assertFalse(result['ok'], values)
            self.assertEqual(before, self.runtime.config)
            self.assertEqual(disk, (self.temp / 'config.json').read_bytes())

    def test_redacted_editable_text_is_not_saved_back(self):
        original = 'fallback https://example.com/v1?access_token=private-fallback-token'
        self.runtime.config['reply_audit_fallback'] = original
        view = self.panel.config_get()
        self.assertIn('reply_audit_fallback', view['redacted_editable_keys'])
        self.assertNotIn('reply_audit_fallback', view['editable_keys'])
        result = self.call('config_save', patch={'reply_audit_fallback': view['config']['reply_audit_fallback']},
                           revision=view['revision'], confirm=True)
        self.assertEqual(result['code'], 'sensitive_config_field')
        self.assertEqual(self.runtime.config['reply_audit_fallback'], original)

    def test_preview_features_use_five_minutes_not_context_window(self):
        from newGeYi.context.live_context import LiveTurn
        import time
        with self.runtime.live_context._lock:
            self.runtime.live_context._states[self.key].append(
                LiveTurn(1, 1, 'user', 'older text', 'tester', '42', time.time() - 600))
        result = self.call('debug_preview', session_key=self.key, text='现在好吗？', sender_id='42')['data']
        self.assertEqual(result['summary']['history_count'], 1)
        self.assertEqual(result['route']['contributions']['recent_count'], 0)

    def test_save_failure_preserves_memory(self):
        old = copy.deepcopy(self.runtime.config)
        with patch.object(C, 'save_config', side_effect=C.ConfigWriteBlocked('blocked')):
            result = self.call('config_save', patch={'active_enabled': False},
                               revision=self.panel.config_get()['revision'], confirm=True)
        self.assertFalse(result['ok'])
        self.assertEqual(self.runtime.config, old)

    def test_request_id_idempotent_and_fingerprint_conflict(self):
        revision = self.panel.config_get()['revision']
        params = dict(patch={'passive_enabled': False}, revision=revision, confirm=True)
        first = self.call('config_save', **params)
        with patch.object(C, 'save_config', side_effect=AssertionError('duplicate write')):
            second = self.call('config_save', **params)
        self.assertEqual(first, second)
        result = self.call('config_save', patch={'passive_enabled': True}, revision=revision, confirm=True)
        self.assertEqual(result['code'], 'request_conflict')

    def test_write_id_isolated_by_browser_session(self):
        revision = self.panel.config_get()['revision']
        self.call('config_save', patch={}, revision=revision, confirm=True)
        payload = {'action': 'config_save', 'patch': {'passive_enabled': False},
                   'revision': self.panel.config_get()['revision'], 'confirm': True}
        event = Event(payload, session='browser-two')
        self.panel.handle_event(event, None)
        self.assertTrue(event.sent[0][2]['ok'])

    def test_disk_change_invalidates_revision(self):
        revision = self.panel.config_get()['revision']
        (self.temp / 'config.json').write_text('{"triggers":["changed"]}', encoding='utf-8')
        result = self.call('config_save', patch={}, revision=revision, confirm=True)
        self.assertEqual(result['code'], 'revision_conflict')

    def test_reload_error_rolls_back_config(self):
        before = copy.deepcopy(self.runtime.config)
        (self.temp / 'config.json').write_text('{broken', encoding='utf-8')
        result = self.call('config_reload', revision=self.panel.config_get()['revision'], confirm=True)
        self.assertEqual(result['code'], 'config_load_error')
        self.assertEqual(before, self.runtime.config)
        self.assertTrue(C.config_load_error())
        self.assertEqual((self.temp / 'config.json').read_text(encoding='utf-8'), '{broken')

    def test_prompt_save_uses_persistent_override_and_revision(self):
        before = self.panel.prompt_get()
        result = self.call('prompt_save', text='新的离线角色设定', revision=before['revision'], confirm=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['path'], str(self.temp / 'role_prompt.webui.txt'))
        self.assertEqual(self.runtime.prompt_builder.role_prompt, '新的离线角色设定')
        second = self.call('prompt_save', request_id='prompt-next', text='第三版',
                           revision=result['data']['revision'], confirm=True)
        self.assertTrue(second['ok'])
        self.assertEqual((self.temp / 'role_prompt.webui.txt.bak-auto').read_text(encoding='utf-8'), '新的离线角色设定')
        conflict = self.call('prompt_save', request_id='prompt-stale', text='不覆盖',
                             revision=before['revision'], confirm=True)
        self.assertEqual(conflict['code'], 'revision_conflict')

    def test_multiline_prompt_roundtrip_keeps_lf(self):
        info = self.panel.prompt_get()
        text = '第一行\n第二行\n第三行'
        result = self.call('prompt_save', text=text, revision=info['revision'], confirm=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['text'], text)
        self.assertEqual((self.temp / 'role_prompt.webui.txt').read_bytes(), text.encode('utf-8'))

    def test_sensitive_prompt_cannot_be_saved_from_redacted_view(self):
        (self.temp / 'role_prompt.webui.txt').write_text('role secret-top-test', encoding='utf-8')
        info = self.panel.prompt_get()
        self.assertNotIn('secret-top-test', info['text'])
        result = self.call('prompt_save', text=info['text'], revision=info['revision'], confirm=True)
        self.assertEqual(result['code'], 'sensitive_prompt')

    def test_read_actions_do_not_resolve_or_mirror_resources(self):
        before = sorted(str(item.relative_to(self.temp)) for item in self.temp.rglob('*'))
        with patch.object(C, 'get_resource_path', side_effect=AssertionError('resource mirror')):
            for action in ('overview', 'config_get', 'prompt_get', 'sessions', 'logs'):
                self.assertTrue(self.call(action)['ok'], action)
        self.assertEqual(before, sorted(str(item.relative_to(self.temp)) for item in self.temp.rglob('*')))

    def test_sessions_pagination_and_session_get(self):
        self.runtime.store.add_turn(self.key, 'user', 'offline-message')
        self.runtime.store.add_user_fact(self.key, '42', 'offline-fact')
        private = SessionKey('bot', 'qq', 'onebot', 'private', '', '456').as_text()
        self.runtime.store.add_turn(private, 'user', 'private-offline')
        first = self.call('sessions', limit=1, offset=0)['data']
        second = self.call('sessions', limit=1, offset=1)['data']
        self.assertEqual(first['total'], 2)
        self.assertEqual(len(first['items']), 1)
        self.assertNotEqual(first['items'][0]['session_key'], second['items'][0]['session_key'])
        result = self.call('session_get', session_key=self.key)['data']
        self.assertEqual(result['turns'][0]['content'], 'offline-message')
        self.assertEqual(result['memories'][0]['layer'], 'L2')
        self.assertIn('usage', result)
        self.assertNotIn('offline-message', json.dumps(first))

    def test_preview_no_network_send_or_db_mutation(self):
        self.runtime.store.add_user_fact(self.key, '42', '喜欢离线测试')
        msg = IncomingMessage(self.session, '1', '42', '测试者', '前一条', '前一条', True, False,
                              None, None, arrival_seq=1)
        self.runtime.live_context.append_user(msg)
        before_db = '\n'.join(self.runtime.store._conn().iterdump())
        before_live = copy.deepcopy(self.runtime.live_context._states)
        before_gate = copy.deepcopy(self.runtime.attention._recent_scores)
        with (
            patch('requests.sessions.Session.request', side_effect=AssertionError('network')),
            patch.object(self.runtime.scheduler, 'submit', side_effect=AssertionError('queue')),
            patch.object(self.runtime, 'reply', side_effect=AssertionError('send')),
            patch.object(self.runtime.router, 'route', side_effect=AssertionError('route')),
            patch.object(self.runtime.attention, 'evaluate', side_effect=AssertionError('gate')),
        ):
            result = self.call('debug_preview', session_key=self.key, text='歌伊你好？', sender_id='42',
                               sender_name='测试者', is_group=True, mentioned_bot=False, is_active=False)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['effects'], {'network': False, 'send': False, 'persist': False})
        self.assertEqual(result['data']['route']['branch'], 'passive')
        self.assertFalse(result['data']['route']['model_evaluated'])
        self.assertIn('喜欢离线测试', json.dumps(result['data']['messages'], ensure_ascii=False))
        self.assertEqual(before_db, '\n'.join(self.runtime.store._conn().iterdump()))
        self.assertEqual(before_live, self.runtime.live_context._states)
        self.assertEqual(before_gate, self.runtime.attention._recent_scores)

    def test_preview_pointed_but_passive_off_is_ignored(self):
        self.runtime.store.set_setting('group_passive:' + self.key, '0')
        result = self.call('debug_preview', session_key=self.key, text='歌伊', sender_id='42')
        self.assertEqual(result['data']['route']['branch'], 'ignored')
        self.assertEqual(result['data']['route']['reason'], 'passive_disabled')

    def test_preview_private_includes_relationship_and_worldbook_stage_without_mutation(self):
        self.runtime.config.update({'private_enabled': True, 'galgame_enabled': True, 'worldbook_enabled': True})
        self.runtime.worldbook.load()
        uid = '888888'
        pkey = SessionKey('bot', 'qq', 'onebot', 'private', '', uid).as_text()
        self.runtime.relationship.force_stage(uid, 3)
        before_db = '\n'.join(self.runtime.store._conn().iterdump())
        result = self.call('debug_preview', session_key=pkey, text='你有哥哥吗？', sender_id=uid,
                           sender_name='测试私聊', is_group=False)
        self.assertTrue(result['ok'], result)
        sys_prompt = result['data']['messages'][0]['content']
        self.assertIn('【此刻你与对方的关系', sys_prompt)
        self.assertIn('阶段：亲近', sys_prompt)
        self.assertIn('废墟', sys_prompt)
        self.assertEqual(before_db, '\n'.join(self.runtime.store._conn().iterdump()))

    def test_isolated_authorization_token_and_url_password_are_redacted(self):
        self.panel._append_log('WARN', 'failed secret-header-test and secret-url-test')
        dumped = json.dumps(self.panel._get_logs({}))
        self.assertNotIn('secret-header-test', dumped)
        self.assertNotIn('secret-url-test', dumped)

    def test_business_log_keeps_constant_template_not_arguments(self):
        record = logging.LogRecord('newGeYi.curator', logging.WARNING, '', 1,
                                   '记忆提炼失败：%s', ('private chat sample',), None)
        self.panel._handler.emit(record)
        dumped = json.dumps(self.panel._get_logs({}), ensure_ascii=False)
        self.assertIn('记忆提炼失败', dumped)
        self.assertNotIn('private chat sample', dumped)

    def test_business_log_bodies_are_not_exposed(self):
        for source in ('newGeYi.renderer', 'newGeYi.curator', 'newGeYi.gateway', 'newGeYi.router'):
            self.panel._handler.emit(logging.LogRecord(source, logging.WARNING, '', 1,
                                                     'raw private chat example', (), None))
        self.assertNotIn('raw private chat example', json.dumps(self.panel._get_logs({})))

    def test_new_environment_key_after_panel_creation_is_redacted(self):
        with patch.dict(os.environ, {'GEYI_KEY_FRESH': 'fresh-env-secret'}):
            self.panel._append_log('INFO', 'test fresh-env-secret')
            self.assertNotIn('fresh-env-secret', json.dumps(self.panel._get_logs({})))

    def test_logs_are_bounded_redacted_cursor_and_closed(self):
        self.panel._handler.emit(logging.LogRecord('newGeYi.tests', logging.WARNING, '', 1,
                                                 'failure secret-top-test', (), None))
        result = self.call('logs', since=0, limit=2)['data']
        self.assertNotIn('secret-top-test', json.dumps(result))
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['next_since'], result['items'][-1]['id'])
        for index in range(900):
            self.panel._append_log('INFO', 'item %d' % index)
        self.assertEqual(len(self.panel._logs), 800)
        self.assertTrue(self.call('logs', since=1)['data']['dropped'])
        self.panel.close()
        self.assertNotIn(self.panel._handler, logging.getLogger('newGeYi').handlers)
        self.assertEqual(self.call('logs')['code'], 'closed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
