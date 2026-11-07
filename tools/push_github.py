# -*- coding: utf-8 -*-
"""把本地 newGeYi 的更新推到 GitHub（走 Git Data API，一次一个 commit）。

为什么不用 git：这台机器上没有 git，也没有 gh；MCP 的 GitHub 工具要求
**把文件内容当参数传进去**，73 个文件 / 1.3 MB（其中 index.html 180 KB）搬不动。
所以改成在这里直接调 API：文件从磁盘读、base64 编码，全程不进模型上下文。

用法：
    python tools/push_github.py --dry-run     # 只打印将要推送的清单
    python tools/push_github.py

凭据：从 ~/.dsh/.env 的 GITHUB_PERSONAL_ACCESS_TOKEN 读（不打印、不落盘）。

安全：
  - 只推显式白名单范围内的文件；`.gitignore` 里那些（config.json / *.sqlite3 /
    *.bak / *.log / __pycache__）一律跳过，二次校验文件名黑名单；
  - 推之前对每个文本文件跑一遍去敏检查（真实 QQ / sk- key / 本机路径），命中就中止；
  - 表情包图片默认不推（体积大、不是代码更新），需要时加 `--with-emote`。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER, REPO, BRANCH = '2332239652', 'newGeYi', 'main'

# 绝不推送
SKIP_NAMES = {'config.json', 'config.example.local.json'}
SKIP_SUFFIX = ('.pyc', '.pyo', '.bak', '.log', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm',
               '.db', '.tmp', '.swp')
SKIP_DIRS = {'__pycache__', '.git', '.idea', '.vscode', 'emote-cache', '导出'}

# 只推这些顶层目录 + 根目录文件
ALLOW_TOP_DIRS = {'context', 'memory', 'presentation', 'providers', 'relationship', 'routing',
                  'runtime', 'storage', 'tests', 'tools', 'voice', 'webui', 'docs', 'data'}
ALLOW_ROOT_FILES = {'__init__.py', 'app.json', 'config.py', 'main.py', 'models.py',
                    'panel.py', 'panel_helpers.py', 'panel_server.py',
                    'README.md', 'CHANGELOG.md', 'LICENSE', '.gitignore', 'config.example.json'}

# 去敏底线：命中就中止整个推送。
# 注意：**这里不写任何真实值**——真实 QQ / token 列表放在仓库外的本地文件
# `~/.dsh/.geyi-push-deny`（每行一条，字面匹配）。写在这里等于把要防的东西
# 一起提交上去（第一版就踩了这个坑：检查器自己申报了自己）。
DENY_FILE = os.path.expanduser(r'~\.dsh\.geyi-push-deny')

# 明显是测试用的假凭据，放行（避免每次都要手工加白名单）
FAKE_HINTS = ('test', 'fake', 'dummy', 'example', 'placeholder', 'abcdefghijklmnop',
              'sk-xxx', 'sk-000', 'local-proxy', 'fallback')

SENSITIVE = [
    (re.compile(r'sk-[A-Za-z0-9_\-]{16,}'), '疑似 API key'),
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9_\-\.]{12,}'), '疑似 Bearer token'),
    (re.compile(r'[A-Za-z]:\\+Users\\+[^\\\s]+'), '本机绝对路径'),
    (re.compile(r'(?i)(access_token|api_key|password|secret)\s*=\s*[A-Za-z0-9_\-]{16,}'),
     '疑似凭据赋值'),
]


def _deny_words() -> list[str]:
    try:
        return [line.strip() for line in open(DENY_FILE, encoding='utf-8')
                if line.strip() and not line.startswith('#')]
    except OSError:
        return []


def _is_fake(value: str) -> bool:
    low = value.lower()
    return any(hint in low for hint in FAKE_HINTS)


def load_token() -> str:
    path = os.path.expanduser(r'~\.dsh\.env')
    for line in open(path, encoding='utf-8', errors='ignore'):
        line = line.strip()
        for key in ('GITHUB_PERSONAL_ACCESS_TOKEN=', 'GITHUB_TOKEN='):
            if line.startswith(key):
                value = line[len(key):].strip().strip('"').strip("'")
                if value and not value.startswith('#'):
                    return value
    raise SystemExit('没有在 ~/.dsh/.env 里找到 GitHub token')


def api(token: str, path: str, method: str = 'GET', payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        'https://api.github.com' + path, data=data, method=method,
        headers={'Authorization': 'Bearer ' + token,
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json',
                 'User-Agent': 'geyi-push',
                 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode() or '{}'
    return json.loads(body)


def collect(with_emote: bool) -> list[tuple[str, str]]:
    """返回 [(仓库内相对路径, 本地绝对路径)]，已按白名单与黑名单过滤。"""
    picked: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, ROOT).replace('\\', '/')
        if rel_dir == '.':
            rel_dir = ''
        top = rel_dir.split('/')[0] if rel_dir else ''
        if rel_dir and top not in ALLOW_TOP_DIRS:
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if name in SKIP_NAMES or name.lower().endswith(SKIP_SUFFIX):
                continue
            rel = f'{rel_dir}/{name}' if rel_dir else name
            if not rel_dir and name not in ALLOW_ROOT_FILES:
                continue
            if rel.startswith('data/emote/') and not with_emote:
                continue
            picked.append((rel, os.path.join(dirpath, name)))
    return picked


def check_sensitive(items: list[tuple[str, str]]) -> list[str]:
    deny = _deny_words()
    problems = []
    for rel, path in items:
        if path.lower().endswith(('.webp', '.png', '.jpg', '.jpeg', '.gif')):
            continue
        try:
            text = open(path, 'rb').read().decode('utf-8', 'ignore')
        except OSError:
            continue
        for word in deny:
            if word in text:
                problems.append(f'{rel}: 命中本地 deny 清单（{word[:4]}…）')
        for pattern, label in SENSITIVE:
            for hit in pattern.finditer(text):
                if _is_fake(hit.group(0)):
                    continue
                problems.append(f'{rel}: {label} → {hit.group(0)[:50]}')
                break
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--with-emote', action='store_true', help='连表情包图片一起推（约 2.2MB）')
    parser.add_argument('--message', default='')
    args = parser.parse_args(argv)

    items = collect(args.with_emote)
    total = sum(os.path.getsize(p) for _, p in items)
    print(f'待推 {len(items)} 个文件，合计 {total / 1024:.1f} KB')

    problems = check_sensitive(items)
    if problems:
        print('\n✗ 去敏检查未通过，已中止：')
        for line in problems[:20]:
            print('   ' + line)
        return 2
    print('✓ 去敏检查通过')

    if args.dry_run:
        for rel, _ in items:
            print('   ' + rel)
        return 0

    token = load_token()
    user = api(token, '/user')
    print(f'认证：{user.get("login")}')

    ref = api(token, f'/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}')
    head = ref['object']['sha']
    base_tree = api(token, f'/repos/{OWNER}/{REPO}/git/commits/{head}')['tree']['sha']
    print(f'基线 commit：{head[:8]}')

    entries, skipped = [], 0
    for index, (rel, path) in enumerate(items, 1):
        raw = open(path, 'rb').read()
        blob = api(token, f'/repos/{OWNER}/{REPO}/git/blobs', 'POST',
                   {'content': base64.b64encode(raw).decode(), 'encoding': 'base64'})
        existing = None
        try:
            existing = api(token, f'/repos/{OWNER}/{REPO}/contents/'
                                  + urllib.parse.quote(rel) + f'?ref={BRANCH}')
        except urllib.error.HTTPError:
            existing = None
        if isinstance(existing, dict) and existing.get('sha') == blob['sha']:
            skipped += 1
            continue
        entries.append({'path': rel, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        if index % 10 == 0 or index == len(items):
            print(f'  已编码 {index}/{len(items)}（无变化 {skipped}）')

    if not entries:
        print('没有实际变化，无需提交。')
        return 0

    tree = api(token, f'/repos/{OWNER}/{REPO}/git/trees', 'POST',
               {'base_tree': base_tree, 'tree': entries})
    message = args.message or f'更新到 v2.9.9：作息/神职守卫/好感提速/额度重分配/全挂降噪（{len(entries)} 个文件）'
    commit = api(token, f'/repos/{OWNER}/{REPO}/git/commits', 'POST',
                 {'message': message, 'tree': tree['sha'], 'parents': [head]})
    api(token, f'/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}', 'PATCH',
        {'sha': commit['sha'], 'force': False})
    print(f'\n✓ 已推送 {len(entries)} 个文件（无变化 {skipped} 个）')
    print(f'  commit: {commit["sha"]}')
    print(f'  https://github.com/{OWNER}/{REPO}/commit/{commit["sha"]}')
    return 0


if __name__ == '__main__':
    import urllib.parse  # noqa: PLC0415
    raise SystemExit(main())
