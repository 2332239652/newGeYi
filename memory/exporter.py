# -*- encoding: utf-8 -*-
"""记忆导出：一个自洽的数据库快照 + 一份人能读的文本。

为什么要单独有这个东西：
- 直接复制 geyi.sqlite3 会**漏掉 WAL 里的最新写入**（见 SQLiteStore.snapshot_to），
  排查记忆问题时最气人的就是「拷出来的库缺最近两天」；
- 二进制库不方便人眼扫，先给一份纯文本，四层记忆各存了什么一眼能看完。

导出物写进数据目录的「导出」子文件夹，不动运行中的数据库。
"""

from __future__ import annotations

import os
import time

from ..config import PLUGIN_VERSION, get_data_dir, get_state_dir

CLIP = 200  # 单条内容在文本里的显示上限（完整内容在快照里）


def _fmt_time(value) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return '—'
    if seconds <= 0:
        return '—'
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(seconds))


def _short_time(value) -> str:
    text = _fmt_time(value)
    return text[5:] if len(text) == 16 else text


def _clip(text, limit: int = CLIP) -> str:
    single = str(text or '').replace('\r', ' ').replace('\n', ' / ').strip()
    return single if len(single) <= limit else single[:limit] + '…'


def _num(value) -> str:
    try:
        return '%.2f' % float(value)
    except (TypeError, ValueError):
        return '?'


def _unique_path(folder: str, stem: str, suffix: str) -> str:
    path = os.path.join(folder, stem + suffix)
    index = 1
    while os.path.exists(path):
        path = os.path.join(folder, '%s-%d%s' % (stem, index, suffix))
        index += 1
    return path


def _group(rows: list, key: str = 'session_key') -> list:
    """按会话分组，保持原顺序（数据本身已按时间倒序）。"""
    buckets: dict = {}
    for row in rows:
        buckets.setdefault(str(row.get(key) or '-'), []).append(row)
    return list(buckets.items())


def _render(data: dict, counts: dict, meta: dict, clip: int = CLIP) -> str:
    lines: list[str] = []
    add = lines.append

    add('=' * 60)
    add('歌伊记忆导出')
    add('=' * 60)
    add('导出时间: %s' % meta.get('exported_at', '—'))
    add('插件版本: %s' % meta.get('version', '?'))
    add('数据目录: %s' % meta.get('data_dir', '?'))
    add('数据库:   %s' % meta.get('db_path', '?'))
    add('快照文件: %s' % meta.get('snapshot', '?'))
    add('')
    add('--- 导出时的开关 ---')
    add(meta.get('switches', ''))
    add('')

    l2_live = counts.get('user_facts_live', 0)
    l2_dead = max(0, counts.get('user_facts', 0) - l2_live)
    add('--- 各层计数 ---')
    add('L0 原始缓冲   %5d 条 · %d 个会话（只用来攒批提炼，不直接进 prompt）'
        % (counts.get('buffer', 0), counts.get('buffer_sessions', 0)))
    add('L1 群摘要     %5d 条 · %d 个会话（只放公共事件）'
        % (counts.get('group_summaries', 0), len(_group(data.get('group_summaries') or []))))
    add('L2 用户事实   %5d 条（在效 %d / 已失效 %d）'
        % (counts.get('user_facts', 0), l2_live, l2_dead))
    add('L3 自身记忆   %5d 条（在效 %d）'
        % (counts.get('self_memories', 0), counts.get('self_memories_live', 0)))
    add('对话历史      %5d 条' % counts.get('turns', 0))
    add('')

    names = {str(row.get('sender_id')): str(row.get('sender_name') or '')
             for row in (data.get('names') or [])}

    # ---- L1 ----
    add('=' * 60)
    add('L1 群摘要（只放公共事件，任何人名都不该出现在这里）')
    add('=' * 60)
    groups = _group(data.get('group_summaries') or [])
    if not groups:
        add('（空）')
    for session, rows in groups:
        add('')
        add('[会话 %s]  %d 条' % (session, len(rows)))
        for row in rows:
            add('  %s  %s' % (_fmt_time(row.get('created_at')), _clip(row.get('content'), clip)))
    add('')

    # ---- L2 ----
    add('=' * 60)
    add('L2 用户事实（按「会话 + QQ 号」隔离；同一人的事实按时间倒序）')
    add('=' * 60)
    add('标注: [重要度 命中次数 最后命中] 已失效的会标出来')
    for session, rows in _group(data.get('user_facts') or []):
        add('')
        add('[会话 %s]  %d 条' % (session, len(rows)))
        by_user: dict = {}
        for row in rows:
            by_user.setdefault(str(row.get('user_id') or '-'), []).append(row)
        for user_id, facts in by_user.items():
            nickname = names.get(user_id, '')
            add('  ● %s%s  %d 条' % (user_id, '（%s）' % nickname if nickname else '', len(facts)))
            for row in facts:
                flags = '%s 命中%d %s' % (
                    _num(row.get('importance')),
                    int(row.get('hit_count') or 0),
                    _short_time(row.get('last_hit_at')),
                )
                dead = ' ［已失效 %s］' % _fmt_time(row.get('valid_to')) if row.get('valid_to') else ''
                origin = str(row.get('source') or '')
                add('      · [%s%s] %s' % (flags, dead, _clip(row.get('fact'), clip)))
                if origin and origin != 'inferred':
                    add('        来源: %s' % origin)
    if not data.get('user_facts'):
        add('')
        add('（空）')
    add('')

    # ---- L3 ----
    add('=' * 60)
    add('L3 歌伊自己的记忆（她自己的经历/承诺/情绪）')
    add('=' * 60)
    groups = _group(data.get('self_memories') or [])
    if not groups:
        add('（空）')
    for session, rows in groups:
        add('')
        add('[会话 %s]  %d 条' % (session, len(rows)))
        for row in rows:
            dead = ' ［已失效］' if row.get('valid_to') else ''
            add('  · [%s 命中%d %s] %s｜%s%s' % (
                _num(row.get('importance')),
                int(row.get('hit_count') or 0),
                _short_time(row.get('last_hit_at')),
                str(row.get('kind') or 'event'),
                _clip(row.get('content'), clip),
                dead,
            ))
    add('')

    # ---- L0 ----
    add('=' * 60)
    add('L0 原始缓冲（还没被提炼进 L1/L2/L3 的消息）')
    add('=' * 60)
    groups = _group(data.get('buffer') or [])
    if not groups:
        add('（空）')
    for session, rows in groups:
        add('')
        add('[会话 %s]  %d 条' % (session, len(rows)))
        # 缓冲是消息流水，按时间正序读才像一段对话（提炼器也是这个顺序消费的）
        for row in reversed(rows):
            who = str(row.get('sender_name') or '') or str(row.get('sender_id') or '')
            role = str(row.get('role') or '')
            tag = '歌伊' if role == 'assistant' else (who or '?')
            add('  [%s] %s: %s' % (_fmt_time(row.get('created_at')), tag, _clip(row.get('content'), clip)))
    add('')

    add('=' * 60)
    add('主动搭话决策（影子模式调阈值就看这一段）')
    add('=' * 60)
    add('动作: blocked=被闸门挡下 / speak=决定说话 / observe=看过没说')
    decisions = data.get('active_decisions') or []
    if not decisions:
        add('（空）')
    else:
        reason_label = {'model_declined': '模型觉得没什么可说'}
        for session, rows in _group(decisions):
            add('')
            add('[会话 %s]  最近 %d 条' % (session, len(rows)))
            # 决策流水按时间正序读，才看得出「被挡了什么、后来为什么说了」
            for row in reversed(rows):
                reason = str(row.get('reason') or '')
                reason = reason_label.get(reason, reason)
                tail = ('｜' + _clip(row.get('detail'), 60)) if row.get('detail') else ''
                add('  [%s] %s%s%s%s' % (
                    _fmt_time(row.get('created_at')),
                    str(row.get('action') or '?'),
                    ('（tier%s）' % row.get('tier')) if row.get('tier') else '',
                    ('｜' + reason) if reason else '',
                    tail,
                ))
    add('')

    add('=' * 60)
    add('说明')
    add('=' * 60)
    add('- 单条内容超过 %d 字的会截断，完整内容见同目录的 .sqlite3 快照。' % clip)
    add('- 快照用 SQLite 在线备份导出，是自洽的单文件，不依赖 -wal/-shm。')
    add('- 库里有群聊原文和真实 QQ 号，外发前请自行脱敏。')
    return '\n'.join(lines) + '\n'


def export_memory(store, config: dict, worldbook=None, channel: str = '',
                  out_dir: str = '', limit: int = 500) -> dict:
    """导出四层记忆，返回各产物的路径与一句话摘要。"""
    cfg = config or {}
    # 导出到 state 目录，不是 data —— data/ 每次重载都会被 OlivOS 清空重建
    folder = out_dir or os.path.join(str(get_state_dir()), '导出')
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')

    snapshot = _unique_path(folder, '记忆快照-%s' % stamp, '.sqlite3')
    store.snapshot_to(snapshot)

    counts = store.count_all_memory()
    data = store.dump_memory(limit=limit)

    entries = len(getattr(worldbook, 'entries', []) or []) if worldbook is not None else 0
    switches = '记忆: %s | 自动提炼: %s | 世界书: %s | 表情包: %s | 图片理解: %s' % (
        '开' if cfg.get('memory_enabled') else '关',
        '开' if cfg.get('memory_curator_enabled', True) else '关',
        '开' if cfg.get('worldbook_enabled') else '关',
        '开' if cfg.get('emote_enabled', True) else '关',
        '开' if cfg.get('image_enabled', True) else '关',
    )
    if channel:
        switches += '\n渠道: %s' % channel
    if worldbook is not None:
        switches += '\n世界书条目: %d' % entries

    text_path = _unique_path(folder, '记忆导出-%s' % stamp, '.txt')
    body = _render(data, counts, {
        'exported_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'version': PLUGIN_VERSION,
        'state_dir': str(get_state_dir()),
        'released_dir': str(get_data_dir()),
        'db_path': str(getattr(store, 'path', '?')),
        'snapshot': snapshot,
        'switches': switches,
    })
    with open(text_path, 'w', encoding='utf-8') as handle:
        handle.write(body)

    summary = ('已导出记忆：\n'
               'L2 用户事实 %d 条 | L3 自身记忆 %d 条 | L1 群摘要 %d 条 | L0 缓冲 %d 条\n'
               '文本: %s\n快照: %s' % (
                   counts.get('user_facts', 0), counts.get('self_memories', 0),
                   counts.get('group_summaries', 0), counts.get('buffer', 0),
                   text_path, snapshot))
    return {'text': text_path, 'snapshot': snapshot, 'summary': summary,
            'counts': counts, 'folder': folder}
