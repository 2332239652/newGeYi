"""Configuration writer ordering across commands, panel CAS and runtime publication."""
import copy
import json
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from newGeYi import config as C
from newGeYi.runtime.app import Runtime
from newGeYi.panel import Panel, PanelError
from newGeYi.routing.commands import CommandRouter


class ConfigurationConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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

    def test_panel_cas_competes_with_command_without_torn_revision(self):
        revision = self.panel.config_get()['revision']
        entered, release, command_done = threading.Event(), threading.Event(), threading.Event()
        saved = C.save_config
        failures = []
        def blocked_save(value, *args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('test release missing')
            return saved(value, *args, **kwargs)
        def panel_write():
            try:
                self.panel._config_save({'revision': revision, 'patch': {'passive_enabled': False}, 'confirm': True})
            except Exception as exc:
                failures.append(exc)
        def command_write():
            try:
                self.runtime.commands._save_and_reload({'active_enabled': False})
            except Exception as exc:
                failures.append(exc)
            finally:
                command_done.set()
        with patch.object(C, 'save_config', side_effect=blocked_save):
            first = threading.Thread(target=panel_write)
            first.start()
            self.assertTrue(entered.wait(1))
            second = threading.Thread(target=command_write)
            second.start()
            self.assertFalse(command_done.wait(.05))
            release.set()
            first.join(3); second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(failures, [])
        disk = json.loads(C.get_config_path().read_text('utf-8'))
        self.assertFalse(disk['passive_enabled'])
        self.assertFalse(disk['active_enabled'])
        self.assertEqual(disk, self.runtime.config)
        with self.assertRaises(PanelError) as caught:
            self.panel._config_save({'revision': revision, 'patch': {'active_enabled': True}, 'confirm': True})
        self.assertEqual(caught.exception.code, 'revision_conflict')

    def test_failed_write_never_publishes_candidate(self):
        before = copy.deepcopy(self.runtime.config)
        identity = id(self.runtime.config)
        with patch.object(C, 'save_config', side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):
                self.runtime.commands._save_and_reload({'passive_enabled': not before['passive_enabled']})
        self.assertEqual(self.runtime.config, before)
        self.assertEqual(id(self.runtime.config), identity)

    def test_bad_reload_preserves_last_valid_runtime(self):
        before = copy.deepcopy(self.runtime.config)
        C.get_config_path().write_text('{bad', 'utf-8')
        self.assertTrue(self.runtime.reload_config())
        self.assertEqual(self.runtime.config, before)

    def test_unique_temp_cleaned_when_replace_fails(self):
        original = C.get_config_path().read_bytes()
        with patch.object(C.os, 'replace', side_effect=OSError('replace fixture')):
            with self.assertRaises(OSError):
                C.save_config(dict(self.runtime.config, passive_enabled=False))
        self.assertEqual(C.get_config_path().read_bytes(), original)
        self.assertEqual(list(C.get_config_path().parent.glob('*.tmp')), [])

    def test_two_trigger_adds_keep_both_values(self):
        router = CommandRouter(self.runtime)
        barrier = threading.Barrier(3)
        failures = []
        def add(value):
            barrier.wait()
            try:
                router._cmd_trigger_add(None, value)
            except Exception as exc:
                failures.append(exc)
        workers = [threading.Thread(target=add, args=(value,)) for value in ('trigger-A', 'trigger-B')]
        for worker in workers: worker.start()
        barrier.wait()
        for worker in workers: worker.join(3)
        self.assertEqual(failures, [])
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertTrue({'trigger-A', 'trigger-B'}.issubset(set(self.runtime.config['triggers'])))
        self.assertEqual(json.loads(C.get_config_path().read_text('utf-8'))['triggers'], self.runtime.config['triggers'])
