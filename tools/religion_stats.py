# -*- encoding: utf-8 -*-
"""神职词统计：看看她最近到底还在不在自称教皇/大主教。

为什么要单独一个脚本：风控这件事只有**趋势**有意义——单条消息看着没问题，
一周几十条「本大主教」叠起来才叫问题。所以看的是比例和形态，不是某一条。

刻意不 import newGeYi：包 __init__ 会拉 main.py（OlivOS 运行时才有的东西），
所以这里只做 sqlite 读取，跑法随便，不依赖插件进程。

用法（任意位置）：

    python newGeYi/tools/religion_stats.py            # 默认最近 7 天
    python newGeYi/tools/religion_stats.py --days 30
    python newGeYi/tools/religion_stats.py --db <路径>
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from collections import Counter

# 自称神职高位：这一类的风险最高（QQ 内容风控对「自称宗教领袖」最敏感）
SELF_TITLE = re.compile(r'本(?:大主教|教皇|主教|圣女|祭司|先知)')
# 称呼别人神职
ADDRESS_TITLE = re.compile(r'(?:大主教|教皇|主教|圣女|祭司|先知)大人')
# 神职名词（不区分自称还是话题）
TITLE_NOUN = re.compile(r'大主教|教皇|主教|圣女|祭司|先知')
# 背景词：这些是她的职业与住处，出现是**正常**的，不该被压
BACKGROUND = re.compile(r'修女|教会|祈祷|祷|礼拜|净化术|治愈粒子|圣职|女神|钟楼')
# 宗教权威叙事：成段出现才危险（封官/敕令/审判/传教）
AUTHORITY = re.compile(r'敕令|册封|封圣|异端|审判|传教|布道|皈依|入教|洗礼|忏悔')


def _default_db() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))  # ...\plugin
    candidates = [
        os.path.join(plugin_root, 'data', 'newGeYi', 'geyi.sqlite3'),
        os.path.join(os.environ.get('GEYI_DATA_DIR', ''), 'geyi.sqlite3'),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return candidates[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='神职词统计（防 QQ 风控的观察工具）')
    parser.add_argument('--days', type=float, default=7.0, help='只统计最近这么多天（默认 7）')
    parser.add_argument('--db', default='', help='数据库路径（默认自动定位 state 目录）')
    parser.add_argument('--show', type=int, default=8, help='打印几条样本（0 = 不打印）')
    args = parser.parse_args(argv)

    db = args.db or _default_db()
    if not os.path.isfile(db):
        print(f'找不到数据库：{db}', file=sys.stderr)
        return 2
    since = int(time.time() - args.days * 86400)

    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        rows = [r for r in con.execute(
            'select content from turns where created_at >= ? and role in (?, ?) '
            'and content is not null order by id desc', (since, 'assistant', 'bot'))]
    finally:
        con.close()
    texts = [r[0] for r in rows if r[0]]
    total = len(texts)
    if not total:
        print(f'最近 {args.days:g} 天没有她的回复记录。')
        return 0

    self_hits = [t for t in texts if SELF_TITLE.search(t)]
    addr_hits = [t for t in texts if ADDRESS_TITLE.search(t)]
    noun_hits = [t for t in texts if TITLE_NOUN.search(t)]
    bg_hits = [t for t in texts if BACKGROUND.search(t)]
    auth_hits = [t for t in texts if AUTHORITY.search(t)]

    def pct(n):
        return f'{n}/{total} = {n / total * 100:.1f}%'

    print(f'最近 {args.days:g} 天她的回复：{total} 条')
    print()
    print(f'自称神职高位   {pct(len(self_hits))}   ← 关键指标，应接近 0')
    print(f'称呼对方神职   {pct(len(addr_hits))}')
    print(f'神职名词出现   {pct(len(noun_hits))}   （含话题式提及，不必压到 0）')
    print(f'宗教权威叙事   {pct(len(auth_hits))}   （敕令/册封/异端/传教…）')
    print(f'教会背景词     {pct(len(bg_hits))}   ← 人设底色，正常')
    print()

    for label, items in (('自称神职高位', self_hits), ('宗教权威叙事', auth_hits)):
        if not items:
            continue
        counter = Counter()
        for text in items:
            for match in (SELF_TITLE if label.startswith('自称') else AUTHORITY).findall(text):
                counter[match] += 1
        print(f'{label} 词频：{counter.most_common(10)}')

    if args.show and self_hits:
        print()
        print(f'—— 自称样本（前 {args.show} 条）——')
        for text in self_hits[:args.show]:
            print('  ' + text.replace('\n', ' ')[:150])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
