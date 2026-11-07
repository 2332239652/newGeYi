# -*- encoding: utf-8 -*-
"""页面角色覆盖应在插件重载后继续生效，原包资源保持独立。

从 tools/run_webui_tests.py 启动，或在 shell 先设置临时 GEYI_DATA_DIR。
仅在本测试内设置变量，晚于 -m unittest 的包导入顺序。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if not os.environ.get('GEYI_DATA_DIR'):
    os.environ['GEYI_DATA_DIR'] = tempfile.mkdtemp(prefix='geyi_prompt_override_')

from newGeYi import config  # noqa: E402
from newGeYi.runtime.app import Runtime  # noqa: E402


class PromptOverrideTest(unittest.TestCase):
    def test_persistent_override_wins_without_resource_mirror(self):
        with tempfile.TemporaryDirectory(prefix='geyi_prompt_') as folder:
            state = Path(folder)
            (state / 'role_prompt.webui.txt').write_text('  网页保存的角色  \n', encoding='utf-8')
            with patch.object(config, 'get_state_dir', return_value=state), \
                    patch.object(config, 'get_resource_path') as resource:
                result = Runtime.load_role_prompt(object())
            self.assertEqual(result, '网页保存的角色')
            resource.assert_not_called()

    def test_original_resource_when_no_override(self):
        with tempfile.TemporaryDirectory(prefix='geyi_prompt_') as folder:
            state = Path(folder)
            original = state / 'original-role.txt'
            original.write_text('原包角色', encoding='utf-8')
            with patch.object(config, 'get_state_dir', return_value=state), \
                    patch.object(config, 'get_resource_path', return_value=original):
                self.assertEqual(Runtime.load_role_prompt(object()), '原包角色')

    def test_unreadable_override_returns_existing_fallback(self):
        with tempfile.TemporaryDirectory(prefix='geyi_prompt_') as folder:
            state = Path(folder)
            (state / 'role_prompt.webui.txt').write_bytes(b'\xff\xff')
            with patch.object(config, 'get_state_dir', return_value=state):
                self.assertEqual(Runtime.load_role_prompt(object()), '你是歌伊，一个可爱的AI助手。')


if __name__ == '__main__':
    unittest.main()
