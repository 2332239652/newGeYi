# -*- encoding: utf-8 -*-
"""配置读写的安全边界（离线、临时目录）。

2026-11-03 外部审查发现的三个高危点，全部钉在这里：

1. **配置文件写坏不能静默重置**：JSON 语法错误时 `load_config` 以前直接返回默认值、
   连一行日志都没有；用户发「重载设定」还看到「✅ 已重载」，之后任何开关命令都会
   拿默认配置覆盖磁盘 —— 渠道和 API Key 一次全丢。
2. **看不懂的文件绝不覆盖**：`save_config` 现在遇到解析失败会抛 `ConfigWriteBlocked`。
3. **主人名单的脏配置不能炸**：`owner_ids: null` / 直接写个数字时，
   以前 `for x in None` 每条消息都 TypeError（而这个函数每条消息都会调用）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_cfg_tests_'))

from newGeYi import config as C  # noqa: E402
from newGeYi.routing.permissions import Permissions  # noqa: E402

BROKEN = '{\n  "passive_enabled": true,\n  "providers": [ {"name": "x", "api_key": "<REDACTED>"} ,]\n}\n'


class _TempState(unittest.TestCase):
    """每个用例一个独立 state 目录（GEYI_DATA_DIR 是运行时读的，所以用环境变量切换）。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='geyi_cfg_state_'))
        self._prev = os.environ.get('GEYI_DATA_DIR')
        os.environ['GEYI_DATA_DIR'] = str(self.dir)
        self.path = C.get_config_path()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('GEYI_DATA_DIR', None)
        else:
            os.environ['GEYI_DATA_DIR'] = self._prev

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding='utf-8')


class BrokenConfigTest(_TempState):
    def test_broken_json_keeps_defaults_and_reports_error(self):
        self.write(BROKEN)
        cfg = C.load_config()
        self.assertIn('triggers', cfg, '解析失败时应退回默认配置保证能跑')
        self.assertEqual(cfg['triggers'], C.DEFAULT_CONFIG['triggers'])
        self.assertTrue(C.config_load_error(), '解析失败必须能被命令层看到')

    def test_broken_file_is_not_touched(self):
        self.write(BROKEN)
        C.load_config()
        self.assertEqual(self.path.read_text(encoding='utf-8'), BROKEN,
                         '磁盘上的配置文件被改写了（密钥就丢在这一步）')
        backups = list(self.dir.glob('config.json.broken-*'))
        self.assertEqual(len(backups), 1, '应该另存一份 .broken 备份方便人工修')

    def test_save_refuses_to_overwrite_broken_file(self):
        self.write(BROKEN)
        cfg = C.load_config()
        with self.assertRaises(C.ConfigWriteBlocked):
            C.save_config(cfg)
        self.assertEqual(self.path.read_text(encoding='utf-8'), BROKEN)

    def test_error_clears_after_file_is_fixed(self):
        self.write(BROKEN)
        C.load_config()
        self.assertTrue(C.config_load_error())
        self.write(json.dumps({'triggers': ['歌伊'], 'passive_enabled': True}))
        cfg = C.load_config()
        self.assertEqual(C.config_load_error(), '')
        C.save_config(dict(cfg, passive_enabled=False))
        self.assertFalse(json.loads(self.path.read_text(encoding='utf-8'))['passive_enabled'])

    def test_non_object_toplevel_is_rejected(self):
        self.write('[1, 2, 3]')
        cfg = C.load_config()
        self.assertIn('triggers', cfg)
        self.assertIn('顶层', C.config_load_error())


class NormalSaveTest(_TempState):
    def test_save_writes_atomically_and_keeps_rolling_backup(self):
        self.write(json.dumps({'triggers': ['歌伊']}))
        cfg = C.load_config()
        C.save_config(dict(cfg, passive_enabled=False))
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertFalse(saved['passive_enabled'])
        self.assertTrue((self.dir / 'config.json.bak-auto').exists(), '没有滚动备份')
        self.assertFalse(list(self.dir.glob('*.tmp')), '留下了临时文件')

    def test_first_run_creates_defaults(self):
        cfg = C.load_config()                    # 文件不存在 → 写一份默认
        self.assertTrue(self.path.exists())
        self.assertEqual(C.config_load_error(), '')
        self.assertIn('active_rate_target_per_100', cfg)


class OwnerIdsTest(unittest.TestCase):
    def test_dirty_owner_ids_do_not_raise(self):
        cases = [None, 2332239652, '2332239652', [], [None, 2332239652, 'abc'], {'a': 1}]
        for raw in cases:
            perms = Permissions({'owner_ids': raw}, owner_ids_env='')
            ids = perms._owner_set()             # 以前这里会 TypeError
            self.assertTrue(all(str(x).isdigit() for x in ids))
        self.assertEqual(Permissions({'owner_ids': 2332239652}, '')._owner_set(), {'2332239652'})
        self.assertEqual(Permissions({'owner_ids': ['2332239652', 'abc']}, '')._owner_set(),
                         {'2332239652'})

    def test_env_owners_are_merged(self):
        perms = Permissions({'owner_ids': None}, owner_ids_env='111，222')
        self.assertEqual(perms._owner_set(), {'111', '222'})


class ExampleConfigTest(unittest.TestCase):
    """示例配置是给人抄的，抄错一次就是「改了不生效」。"""

    def setUp(self):
        self.example = json.loads(
            (Path(__file__).resolve().parent.parent / 'config.example.json')
            .read_text(encoding='utf-8'))

    def test_triggers_are_readable_chinese(self):
        triggers = self.example.get('triggers') or []
        self.assertEqual(triggers, ['歌伊'], '触发词是 GBK 乱码（姝屼紛），照抄会叫不醒她')

    def test_no_dead_keys(self):
        for dead in ('auto_memory_enabled', 'active_probability', 'active_cooldown_seconds',
                     'reasoner_model'):
            self.assertNotIn(dead, self.example, f'{dead} 已经没有任何代码读了')

    def test_secrets_are_blank(self):
        self.assertEqual(self.example.get('api_key', ''), '')
        self.assertEqual(self.example.get('voice_auth_token', ''), '')
        self.assertEqual(self.example.get('voice_onebot_access_token', ''), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
