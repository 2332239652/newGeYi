# -*- encoding: utf-8 -*-
"""批量加/删赞助名单（运维工具，直接改生产 config.json）。

为什么要有：爱发电过审后一次会来一批人，用 QQ 发几十条「赞助加」既慢又容易漏；
而手工改 config.json 很容易只改一半（一档名单加进去了、二档名单没摘掉）。

安全约定：
- **默认 dry-run**，只打印将要发生的变化；必须显式 `--apply` 才写盘；
- 写盘前留 `config.json.bak-sponsor-<时间戳>`，并用临时文件 + 原子替换；
- 写的是「默认配置 ⊕ 现有配置」的深合并结果，顺手补齐生产缺的 8 个赞助键；
- 写盘不会热更新：**必须重载插件**（`tools/../reload_plugin.ps1` 或宿主 WebUI）才生效；
  这期间不要让插件自己写配置（任何开关命令），否则会用它的内存副本覆盖掉这次改动。

用法（在插件根目录，Python 3.11+）：

    python tools/add_sponsors.py --tier 1 --ids 123456789,987654321
    python tools/add_sponsors.py --tier 2 --file ids.txt --apply
    python tools/add_sponsors.py --remove 123456789 --apply
    python tools/add_sponsors.py --list
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
TIER_KEYS = {1: 'sponsor_tier1_ids', 2: 'sponsor_tier2_ids'}


def state_dir() -> Path:
    override = os.environ.get('GEYI_DATA_DIR', '').strip()
    if override:
        return Path(override)
    return BASE_DIR.parents[1] / 'data' / 'newGeYi'


def default_config() -> dict:
    """从 config.py 里抠出 DEFAULT_CONFIG 字面量：**不 import 插件**（import 会建 Runtime）。"""
    tree = ast.parse((BASE_DIR / 'config.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'DEFAULT_CONFIG' for t in node.targets)):
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict = {}
            exec(compile(module, str(BASE_DIR / 'config.py'), 'exec'), namespace)  # noqa: S102
            return namespace['DEFAULT_CONFIG']
    raise SystemExit('config.py 里没找到 DEFAULT_CONFIG')


def merge(defaults: dict, current: dict) -> dict:
    """深合并：字典递归、其它整体替换（与 config.load_config 同口径）。"""
    out = copy.deepcopy(defaults)
    for key, value in (current or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def id_list(config: dict, key: str) -> list:
    raw = config.get(key)
    if not isinstance(raw, (list, tuple, set)):
        return []
    return sorted({str(x).strip() for x in raw if str(x).strip()})


def parse_ids(args) -> list:
    ids: list = []
    for chunk in (args.ids or '').split(','):
        chunk = chunk.strip()
        if chunk:
            ids.append(chunk)
    if args.file:
        text = Path(args.file).read_text(encoding='utf-8')
        for line in text.replace(',', '\n').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                ids.append(line.split()[0])
    bad = [uid for uid in ids if not (uid.isdigit() and 5 <= len(uid) <= 12)]
    if bad:
        raise SystemExit('这些不是像样的 QQ 号，先改掉再跑：%s' % ', '.join(bad))
    return sorted(set(ids))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='批量维护歌伊的赞助名单')
    parser.add_argument('--tier', type=int, choices=(1, 2), default=1, help='加到哪一档（默认一档）')
    parser.add_argument('--ids', help='逗号分隔的 QQ 号')
    parser.add_argument('--file', help='每行一个 QQ 号的文件')
    parser.add_argument('--remove', help='从所有名单里删掉这个 QQ 号')
    parser.add_argument('--list', action='store_true', help='只看当前名单')
    parser.add_argument('--apply', action='store_true', help='真的写盘（不加就只预览）')
    args = parser.parse_args(argv)

    path = state_dir() / 'config.json'
    if not path.exists():
        raise SystemExit('找不到生产配置：%s' % path)
    current = json.loads(path.read_text(encoding='utf-8'))
    config = merge(default_config(), current)

    if args.list or not (args.ids or args.file or args.remove):
        for tier, key in TIER_KEYS.items():
            members = id_list(config, key)
            print('%d 档（%s）：%d 人 %s' % (tier, key, len(members), '、'.join(members) or '—'))
        print('旧名单 sponsor_ids：%s' % ('、'.join(id_list(config, 'sponsor_ids')) or '—'))
        print('内测 beta_ids：%s' % ('、'.join(id_list(config, 'beta_ids')) or '—'))
        return 0

    before = {key: set(id_list(config, key)) for key in TIER_KEYS.values()}
    before['sponsor_ids'] = set(id_list(config, 'sponsor_ids'))

    if args.remove:
        uid = str(args.remove).strip()
        if not (uid.isdigit() and 5 <= len(uid) <= 12):
            raise SystemExit('不像样的 QQ 号：%s' % uid)
        for key in list(TIER_KEYS.values()) + ['sponsor_ids']:
            config[key] = sorted(before[key] - {uid})
        touched = ['删 %s' % uid]
    else:
        ids = parse_ids(args)
        if not ids:
            raise SystemExit('没有给出任何 QQ 号')
        target, other = TIER_KEYS[args.tier], TIER_KEYS[3 - args.tier]
        config[target] = sorted(set(config[target]) | set(ids))
        config[other] = sorted(set(config[other]) - set(ids))
        config['sponsor_ids'] = sorted(set(config['sponsor_ids']) - set(ids))
        touched = ['%d 档 +%d 人' % (args.tier, len(ids))]

    after = {key: set(id_list(config, key)) for key in TIER_KEYS.values()}
    after['sponsor_ids'] = set(id_list(config, 'sponsor_ids'))
    print('改动：%s' % '，'.join(touched))
    for key in list(TIER_KEYS.values()) + ['sponsor_ids']:
        added, removed = after[key] - before[key], before[key] - after[key]
        print('  %-24s %d 人%s%s' % (
            key, len(after[key]),
            ('  +' + '、'.join(sorted(added))) if added else '',
            ('  -' + '、'.join(sorted(removed))) if removed else ''))
    print('键数：写盘前 %d，写盘后 %d（补齐缺省键）' % (len(current), len(config)))

    if not args.apply:
        print('\n这是预览。确认无误后加 --apply 真正写盘，然后**重载插件**才生效。')
        return 0

    stamp = time.strftime('%Y%m%d-%H%M%S') + '-%03d' % (int(time.time() * 1000) % 1000)
    backup = path.with_name('config.json.bak-sponsor-%s' % stamp)
    shutil.copy2(path, backup)
    tmp = path.with_name('config.json.tmp-%s' % stamp)
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=4) + '\n', encoding='utf-8')
    os.replace(tmp, path)
    print('已写盘：%s' % path)
    print('已备份：%s' % backup.name)
    print('下一步：重载插件（reload_plugin.ps1），并在日志里确认新版本与 0 Traceback。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
