# -*- encoding: utf-8 -*-
"""配置层的回归测试（离线、无网络、不碰线上数据）。

跑法（在 plugin/app 目录下）：

    set GEYI_DATA_DIR=%TEMP%\\geyi_test_scratch   &&  python -m unittest discover -s newGeYi/tests -t .

为什么会自动隔离：导入 `newGeYi` 会执行 main.py 里的 `Runtime()`，它立刻打开数据库和
配置文件。所以**必须先设 `GEYI_DATA_DIR`**（本文件在 import 之前就设好了），
否则测试会往线上状态目录里写东西——这一点踩过，别再改回去。

这四个断言正好锁住 2026-10-30 那批隐患：
  · 版本号两处一致（app.json vs PLUGIN_VERSION）
  · 深合并：写半个 failover 不会抹掉其余键
  · 被代码读取的配置键，默认值里必须有
  · 任何 `.get('键', 字面量)` 的兜底值不许和 DEFAULT_CONFIG 打架
"""

from __future__ import annotations

import ast
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

# ⚠️ 必须在 import newGeYi 之前：把状态目录指到临时目录
_SCRATCH = tempfile.mkdtemp(prefix='geyi_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi import config as C  # noqa: E402 - 故意放在环境变量之后

ROOT = Path(C.__file__).resolve().parent
# 代码里被读取、但历史上没进默认值的键（2026-10-30 补齐）
CONSUMED_KEYS = (
    'assistant_name',
    'extra_headers',
    'memory_inject_facts',
    'memory_inject_self',
    'memory_inject_summaries',
    'memory_curate_max_tokens_cap',
)
# 历史兜底值与默认值打架、已统一的那批（抽查，关键是别反弹）
SPOT_CHECK_FALLBACKS = {
    'active_base_cooldown': 180,
    'active_daily_cap': 60,
    'active_no_response_window': 600,
    'active_quiet_hours': '',
    'group_quota_enabled': False,
    'max_reply_chars': 1200,
}


def _write_config(data: dict) -> None:
    path = C.get_config_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


class VersionTest(unittest.TestCase):
    def test_app_json_matches_plugin_version(self):
        """菜单/状态读 PLUGIN_VERSION，OlivOS 插件列表读 app.json —— 必须一致。"""
        self.assertEqual(C.app_json_version(), C.PLUGIN_VERSION)
        self.assertEqual(C.version_mismatch(), '')


class DeepMergeTest(unittest.TestCase):
    def test_partial_failover_keeps_other_keys(self):
        """只写一个子项时，其余默认值必须留着（这是判死阈值曾经从 1 变 2 的原因）。"""
        _write_config({'failover': {'probe_interval_seconds': 600}})
        cfg = C.load_config()
        self.assertEqual(cfg['failover']['probe_interval_seconds'], 600)
        self.assertEqual(cfg['failover']['fail_threshold'], C.DEFAULT_CONFIG['failover']['fail_threshold'])
        for key in C.DEFAULT_CONFIG['failover']:
            self.assertIn(key, cfg['failover'], f'failover.{key} 被浅合并吃掉了')

    def test_nested_dict_and_list_semantics(self):
        """字典深合并；列表整体替换（providers/triggers 必须能被改成空）。"""
        _write_config({'active_tier_probability': {'2': 0.9}, 'triggers': []})
        cfg = C.load_config()
        self.assertEqual(cfg['active_tier_probability']['2'], 0.9)
        # 列表不做合并：清空就是清空
        self.assertEqual(cfg['triggers'], [])


class DefaultsTest(unittest.TestCase):
    def test_consumed_keys_have_defaults(self):
        for key in CONSUMED_KEYS:
            self.assertIn(key, C.DEFAULT_CONFIG, f'{key} 被代码读取，却没进 DEFAULT_CONFIG')

    def test_spot_checked_defaults(self):
        for key, expected in SPOT_CHECK_FALLBACKS.items():
            self.assertEqual(C.DEFAULT_CONFIG[key], expected, key)

    def test_image_budget_sane(self):
        """单张上限不应大于总量上限（示例配置曾经反过来，照抄只收得到一张图）。"""
        self.assertLessEqual(C.DEFAULT_CONFIG['image_max_bytes'],
                             C.DEFAULT_CONFIG['image_total_max_bytes'])

    def test_full_default_config_has_no_missing_keys(self):
        _write_config(dict(C.DEFAULT_CONFIG))
        self.assertEqual(C.missing_config_keys(), [])


class LegacyKeyTest(unittest.TestCase):
    def test_auto_memory_enabled_migrates(self):
        """老配置只写了废弃的 auto_memory_enabled → 迁移成 memory_curator_enabled。"""
        _write_config({'auto_memory_enabled': True})
        self.assertTrue(C.load_config()['memory_curator_enabled'])
        _write_config({'auto_memory_enabled': False})
        self.assertFalse(C.load_config()['memory_curator_enabled'])

    def test_new_key_wins_over_legacy(self):
        _write_config({'auto_memory_enabled': True, 'memory_curator_enabled': False})
        self.assertFalse(C.load_config()['memory_curator_enabled'])


class FallbackLiteralTest(unittest.TestCase):
    """静态扫描：`.get('键', 字面量)` 的兜底值必须等于 DEFAULT_CONFIG 的同名默认值。

    为什么值得一条测试：这些兜底值平时是死代码（load_config 总会补齐默认值），
    只在"配置缺键/传了半个 dict"时才生效——那时行为会莫名其妙跳一个数量级，
    而且没人会想到去查它。2026-10-30 一次扫描就翻出 27 处不一致。
    """

    PATTERN = re.compile(r"\.get\(\s*'([a-z_0-9]+)'\s*,\s*([^),]+)\)")

    def test_no_fallback_contradicts_default(self):
        problems: list[str] = []
        for path in sorted(ROOT.rglob('*.py')):
            if 'tests' in path.parts or '__pycache__' in path.parts:
                continue
            for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                for key, literal in self.PATTERN.findall(line):
                    if key not in C.DEFAULT_CONFIG:
                        continue
                    try:
                        value = ast.literal_eval(literal.strip())
                    except (ValueError, SyntaxError):
                        continue                      # 兜底是个常量名，跳过
                    default = C.DEFAULT_CONFIG[key]
                    if isinstance(default, (dict, list)):
                        continue
                    if value != default:
                        problems.append(
                            f'{path.relative_to(ROOT)}:{lineno} '
                            f'.get({key!r}, {literal.strip()}) != 默认值 {default!r}'
                        )
        self.assertEqual(problems, [], '\n兜底值与默认值不一致：\n' + '\n'.join(problems))


if __name__ == '__main__':
    unittest.main(verbosity=2)
