"""Independent F23 review regressions; local fixtures only, no network calls.

The desired assertions intentionally expose unfixed paths on the review baseline.
"""
import copy
import json
import os
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from newGeYi import config as C
from newGeYi.panel import Panel
from newGeYi.runtime.app import Runtime


class ConfigReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='config_review_')
        self.env = patch.dict(os.environ, {'GEYI_DATA_DIR': self.temp.name})
        self.env.start()
        C._CONFIG_STATE['error'] = ''
        self.runtime = Runtime()
        C.save_config(self.runtime.config)
        self.panel = Panel(self.runtime)

    def tearDown(self):
        self.panel.close()
        self.runtime.scheduler.shutdown()
        self.runtime.voice_manager._executor.shutdown(wait=False)
        self.runtime.store.close()
        self.env.stop()
        self.temp.cleanup()
        C._CONFIG_STATE['error'] = ''

    def test_config_identity_reaches_every_constructor_holder(self):
        original = self.runtime.config
        self.runtime.commit_config(dict(original, private_enabled=True, scene_enabled=False))
        for name in (*self.runtime._CFG_HOLDERS, 'scene', 'private_gate', 'sleep'):
            if name != 'gateway':
                self.assertTrue(getattr(self.runtime, name).config is original, name)
        self.assertEqual(self.runtime.gateway.config, original)
        self.assertIsNot(self.runtime.gateway.config, original)
        self.assertEqual(json.loads(C.get_config_path().read_text('utf-8')), original)

    def test_process_job_preserves_gateway_snapshot(self):
        self.runtime.is_resting = lambda session: True
        message = types.SimpleNamespace(is_group=True, session=types.SimpleNamespace(as_text=lambda: 'fixture'))
        self.runtime.process_job(message, 'passive')
        self.assertIsNot(self.runtime.gateway.config, self.runtime.config)

    def test_missing_reload_preserves_runtime_without_creating_defaults(self):
        before = copy.deepcopy(self.runtime.config)
        path = C.get_config_path()
        self.assertEqual(path.name, 'config.json')
        self.assertTrue(str(path.resolve()).startswith(str(self.temp.name)))
        path.unlink()
        self.assertTrue(self.runtime.reload_config())
        self.assertEqual(self.runtime.config, before)
        self.assertFalse(path.exists())

    def test_setting_key_preserves_concurrent_provider_edit(self):
        self.runtime.commit_config(dict(self.runtime.config, providers=[{
            'name': 'fixture', 'endpoint': 'https://fixture.invalid/v1/chat/completions',
            'model': 'before', 'api_key': 'old-fixture-key', 'priority': 1,
        }]))
        self.runtime.permissions = types.SimpleNamespace(is_master=lambda msg: True)
        self.runtime.reply = lambda *args: None
        router = self.runtime.commands
        entered, release = threading.Event(), threading.Event()
        original_save = router._save_and_reload
        errors = []

        def delayed_save(changes=None):
            entered.set()
            if not release.wait(3):
                raise AssertionError('review barrier timeout')
            return original_save(changes)

        def command():
            try:
                router._dispatch(types.SimpleNamespace(text='设置key new-fixture-key', is_group=False))
            except Exception as exc:
                errors.append(exc)

        edit_started = threading.Event()

        def concurrent_edit():
            edit_started.set()
            try:
                with C.CONFIG_LOCK:
                    candidate = copy.deepcopy(self.runtime.config)
                    candidate['providers'][0]['model'] = 'concurrent-new-model'
                    self.runtime.commit_config(candidate)
            except Exception as exc:
                errors.append(exc)

        with patch.object(router, '_save_and_reload', side_effect=delayed_save):
            worker = threading.Thread(target=command)
            editor = threading.Thread(target=concurrent_edit)
            worker.start()
            self.assertTrue(entered.wait(2))
            editor.start()
            self.assertTrue(edit_started.wait(2))
            # On fixed code the editor blocks on the complete key RMW lock.
            # On the old code it commits before release and gets overwritten.
            editor.join(.1)
            release.set()
            worker.join(4)
            editor.join(4)
        self.assertFalse(worker.is_alive() or editor.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.runtime.config['providers'][0]['api_key'], 'new-fixture-key')
        self.assertEqual(self.runtime.config['providers'][0]['model'], 'concurrent-new-model')

    def test_panel_surfaces_voice_application_failure(self):
        revision = self.panel.config_get()['revision']
        old = self.runtime.voice_manager.enabled
        with patch.object(self.runtime.voice_manager, 'reload', side_effect=ValueError('voice fixture')):
            result = self.panel._config_save({'revision': revision, 'patch': {'voice_enabled': not old}, 'confirm': True})
        self.assertEqual(self.runtime.config['voice_enabled'], not old)
        self.assertEqual(self.runtime.voice_manager.enabled, old)
        self.assertTrue(result.get('warnings'), 'voice apply failed but panel returned unqualified success')

    def test_voice_reload_reports_bad_disk_configuration(self):
        C.get_config_path().write_text('{bad', encoding='utf-8')
        result = self.runtime.commands._cmd_voice_reload(None)
        self.assertNotIn('✅', result, 'reload_config error was discarded by voice reload command')

    def test_runtime_epoch_invalidates_same_dict_aba(self):
        gateway = self.runtime.gateway
        identity = id(self.runtime.config)
        with gateway._lock:
            token = gateway._operation_token_locked()
        old = copy.deepcopy(self.runtime.config)
        self.runtime.commit_config(dict(old, passive_enabled=not old['passive_enabled']))
        self.runtime.commit_config(old)
        self.assertEqual(id(self.runtime.config), identity)
        self.assertEqual(self.runtime.config, old)
        with gateway._lock:
            self.assertFalse(gateway._operation_valid_locked(token))


if __name__ == '__main__':
    unittest.main(verbosity=2)
