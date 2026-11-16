# -*- encoding: utf-8 -*-
"""完整会话级剧情空间。

current_space 只读；ensure_space 仅首次迁移旧剧情开关，已有 chat 须显式续接。
切换在同一短事务中保存空间、scene 代际和历史边界，不删除任何原始消息。
历史记录应优先按 metadata.space_id 过滤；boundary 是旧记录的兼容水位，
source_seq/buffer_id 均为切换前已存在记录的上界，同秒记录不要只凭 ts 判断。
"""

from __future__ import annotations

import json
import time
import uuid

SPACE_PREFIX = 'memory.space:'
STORIES_PREFIX = 'memory.stories:'
BOUNDARY_PREFIX = 'memory.space_boundary:'


def current_space(store, session_key: str) -> str:
    """返回 chat 或 story:uuid；损坏/缺失设置按 chat 读取。"""
    value = str(store.get_setting(SPACE_PREFIX + session_key) or '')
    if value == 'chat':
        return value
    if value.startswith('story:'):
        try:
            return 'story:' + str(uuid.UUID(value[6:]))
        except ValueError:
            pass
    return 'chat'


def snapshot_space(store, session_key: str) -> dict:
    """原子读取空间与 scene 代际，供在途任务提交时比较。"""
    with store.transaction():
        return {
            'space_id': current_space(store, session_key),
            'generation': int(store.get_setting('scene.generation:' + session_key, '0') or 0),
        }


def list_stories(store, session_key: str) -> list[dict]:
    """按创建顺序返回篇章；编号稳定，只在当前完整会话内选择。"""
    try:
        rows = json.loads(store.get_setting(STORIES_PREFIX + session_key) or '[]')
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)
            and str(row.get('space_id', '')).startswith('story:')]


def space_boundary(store, session_key: str, space_id: str | None = None) -> dict:
    """读取最近一次进入该空间的边界（恢复篇章时，旧篇章metadata仍可读取）。"""
    key = BOUNDARY_PREFIX + session_key + ':' + (space_id or current_space(store, session_key))
    try:
        value = json.loads(store.get_setting(key) or '{}')
        return {name: int(value.get(name, 0)) for name in ('source_seq', 'ts', 'buffer_id')}
    except (ValueError, TypeError, AttributeError):
        return {'source_seq': 0, 'ts': 0, 'buffer_id': 0}


def _switch(store, conn, session_key: str, space_id: str) -> str:
    previous = store.get_setting(SPACE_PREFIX + session_key)
    if previous == space_id:
        return space_id
    boundary = {
        'source_seq': int(conn.execute(
            'SELECT COALESCE(MAX(seq),0) FROM turns WHERE session_key=?', (session_key,),
        ).fetchone()[0]),
        'buffer_id': int(conn.execute(
            'SELECT COALESCE(MAX(id),0) FROM memory_buffer WHERE session_key=?', (session_key,),
        ).fetchone()[0]),
        'ts': int(time.time()),
    }
    store.set_setting(SPACE_PREFIX + session_key, space_id)
    if space_id.startswith('story:'):
        store.set_setting('memory.last_story:' + session_key, space_id)
    store.set_setting(BOUNDARY_PREFIX + session_key + ':' + space_id, json.dumps(boundary))
    generation_key = 'scene.generation:' + session_key
    generation = int(store.get_setting(generation_key, '0') or 0)
    store.set_setting(generation_key, generation + 1)
    conn.execute('DELETE FROM settings WHERE key IN (?,?)',
                 ('scene:' + session_key, 'scene.fail_until:' + session_key))
    return space_id


def _new_story(store, conn, session_key: str, title: str) -> str:
    rows = list_stories(store, session_key)
    space_id = 'story:' + str(uuid.uuid4())
    rows.append({'space_id': space_id, 'title': str(title or '').strip()[:80] or f'篇章 {len(rows) + 1}',
                 'created_at': int(time.time())})
    store.set_setting(STORIES_PREFIX + session_key, json.dumps(rows, ensure_ascii=False))
    return _switch(store, conn, session_key, space_id)


def ensure_space(store, session_key: str, story_on: bool) -> str:
    """首次有旧剧情开关时创建篇章；重复调用不改变 scene 代际。"""
    with store.transaction() as conn:
        if store.get_setting(SPACE_PREFIX + session_key) is not None:
            return current_space(store, session_key)
        if story_on:
            return _new_story(store, conn, session_key, '')
        return _switch(store, conn, session_key, 'chat')


def new_story(store, session_key: str, title: str = '') -> str:
    """明确新开/重开始终分配新的 UUID，即便标题相同。"""
    with store.transaction() as conn:
        return _new_story(store, conn, session_key, title)


def pause_story(store, session_key: str) -> str:
    """回日常，仅暂停篇章，保留所有经历/消息及续接选择。"""
    with store.transaction() as conn:
        return _switch(store, conn, session_key, 'chat')


def resume_story(store, session_key: str, selector: str = '') -> str:
    """空选择续接最近篇章；支持列表编号、精确标题、完整空间 ID。

    没有篇章或选择歧义时抛出 ValueError，由命令给出统一选择提示。
    """
    with store.transaction() as conn:
        rows = list_stories(store, session_key)
        value = str(selector or '').strip()
        if not rows:
            raise ValueError('暂无可续接的篇章')
        if not value:
            last = store.get_setting('memory.last_story:' + session_key)
            row = next((item for item in rows if item['space_id'] == last), rows[-1])
        elif value.isdecimal():
            number = int(value)
            if number < 1 or number > len(rows):
                raise ValueError('篇章编号不存在')
            row = rows[number - 1]
        else:
            matches = [item for item in rows if value in (item['space_id'], item.get('title'))]
            if len(matches) != 1:
                raise ValueError('请选择唯一的篇章编号')
            row = matches[0]
        return _switch(store, conn, session_key, row['space_id'])
