# -*- encoding: utf-8 -*-
"""WebUI 入口回归：普通菜单保留旧行为。

先从 tools/run_webui_tests.py 启动，或在 shell 设置临时 GEYI_DATA_DIR。
-m unittest 包路径会先执行 newGeYi.__init__，早于本文件的环境设置。
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

if not os.environ.get('GEYI_DATA_DIR'):
    os.environ['GEYI_DATA_DIR'] = tempfile.mkdtemp(prefix='geyi_panel_entry_')

from newGeYi import main  # noqa: E402


def web_event(namespace='newGeYi', request_id='entry-1'):
    return SimpleNamespace(
        data=SimpleNamespace(namespace=namespace, event='newGeYi_WebUI', webui={'request_id': request_id}),
        bot_info=None,
        send=Mock(),
    )


class PanelEntryTest(unittest.TestCase):
    def test_webui_uses_panel_not_qq_menu(self):
        event, runtime, panel = web_event(), Mock(), Mock()
        with patch.object(main, '_runtime', runtime), patch.object(main, '_load_error', ''), \
                patch.object(main, '_get_panel', return_value=panel):
            main.Event.menu(event, None)
        panel.handle_event.assert_called_once_with(event, None)
        runtime.handle_menu.assert_not_called()
        event.send.assert_not_called()

    def test_ordinary_menu_preserves_runtime_handler(self):
        event, runtime = SimpleNamespace(data=SimpleNamespace(namespace='newGeYi', event='newGeYi_Menu_001')), Mock()
        with patch.object(main, '_runtime', runtime), patch.object(main, '_get_panel') as getter:
            main.Event.menu(event, None)
        runtime.handle_menu.assert_called_once_with(event, None)
        getter.assert_not_called()

    def test_other_namespace_never_uses_panel(self):
        event, runtime = web_event(namespace='other'), Mock()
        with patch.object(main, '_runtime', runtime), patch.object(main, '_get_panel') as getter:
            main.Event.menu(event, None)
        getter.assert_not_called()
        event.send.assert_not_called()

    def test_broken_runtime_replies_without_leaking_load_error(self):
        event = web_event()
        with patch.object(main, '_load_error', 'secret-config-value'), patch.object(main, '_get_panel') as getter:
            main.Event.menu(event, None)
        getter.assert_not_called()
        event.send.assert_called_once()
        args = event.send.call_args.args
        self.assertEqual(args[:2], ('webui', 'entry-1'))
        self.assertFalse(args[2]['ok'])
        self.assertNotIn('secret-config-value', str(args[2]))

    def test_panel_failure_returns_generic_error(self):
        event = web_event()
        with patch.object(main, '_load_error', ''), \
                patch.object(main, '_get_panel', side_effect=ValueError('secret-credentials')):
            main.Event.menu(event, None)
        self.assertNotIn('secret-credentials', str(event.send.call_args))
        self.assertEqual(event.send.call_args.args[2]['code'], 'panel_unavailable')

    def test_save_closes_panel_even_when_runtime_save_fails(self):
        runtime, panel = Mock(), Mock()
        runtime.save.side_effect = RuntimeError('shutdown failed')
        with patch.object(main, '_runtime', runtime), patch.object(main, '_panel', panel):
            with self.assertRaises(RuntimeError):
                main.Event.save(None, None)
            panel.close.assert_called_once()
            self.assertIsNone(main._panel)

    def test_init_builds_panel_after_runtime(self):
        runtime, panel = Mock(), Mock()
        with patch.object(main, '_runtime', runtime), patch.object(main, '_load_error', ''), \
                patch.object(main, '_get_panel', return_value=panel) as getter:
            main.Event.init(None, None)
        runtime.init.assert_called_once_with(None)
        getter.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
