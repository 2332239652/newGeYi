# -*- encoding: utf-8 -*-
"""SQLite 存储层：会话、记忆、背景、调用指标。"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Any, Optional

# 事实判重用的归一化键：只留实义字符（中文、字母、数字），标点空白一律丢掉。
# 这样「我喜欢喝牛奶。」和「我喜欢喝牛奶」会被认成同一条。
_KEEP_RE = re.compile(r'[^\w\u4e00-\u9fff]+')


def normalize_fact(text: str) -> str:
    return _KEEP_RE.sub('', str(text or '')).lower()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    sender_id TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    content TEXT NOT NULL,
    model TEXT DEFAULT '',
    in_tokens INTEGER DEFAULT 0,
    out_tokens INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_sk ON turns(session_key, seq);

CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS memories(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    fact TEXT NOT NULL,
    source TEXT DEFAULT '',
    confirmed INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_uk ON memories(user_key);

CREATE TABLE IF NOT EXISTS backgrounds(
    session_key TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_calls(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ts INTEGER NOT NULL,
    session_key TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    model TEXT DEFAULT '',
    in_tokens INTEGER DEFAULT 0,
    out_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1,
    error_code TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON api_calls(day);

-- ==================== 四层记忆系统 ====================
-- 设计要点：四层物理分表，不同代码路径写入。
-- 靠一个 speaker/scope 字段区分迟早出事（已有项目踩过「把用户事实记成角色人设」的坑）。

-- L0：原始消息缓冲。只用于攒批触发提炼与短期兜底，不直接进 prompt。
CREATE TABLE IF NOT EXISTS memory_buffer(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL,
    sender_id TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mbuf_sk ON memory_buffer(session_key, id);

-- L1：群级滚动摘要。只放公共事件，严禁放任何人的个人事实。
CREATE TABLE IF NOT EXISTS group_summaries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    content TEXT NOT NULL,
    covers_to INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gsum_sk ON group_summaries(session_key, id);

-- L2：用户级结构化事实。按「会话 + QQ 号」隔离；失效不删除，只填 valid_to。
CREATE TABLE IF NOT EXISTS user_facts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    fact TEXT NOT NULL,
    fact_norm TEXT DEFAULT '',
    category TEXT DEFAULT '',
    source TEXT DEFAULT 'inferred',
    importance REAL DEFAULT 0.5,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER DEFAULT 0,
    last_hit_at INTEGER DEFAULT 0,
    hit_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ufact ON user_facts(session_key, user_id, valid_to);
CREATE INDEX IF NOT EXISTS idx_ufact_norm ON user_facts(session_key, user_id, fact_norm);

-- L3：角色自身记忆。歌伊自己的经历/承诺/情绪——通用框架都没有这层，
-- 但角色扮演缺了它就不成立（否则她只记得你，不记得自己）。
CREATE TABLE IF NOT EXISTS self_memories(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    content TEXT NOT NULL,
    content_norm TEXT DEFAULT '',
    kind TEXT DEFAULT 'event',
    importance REAL DEFAULT 0.5,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER DEFAULT 0,
    last_hit_at INTEGER DEFAULT 0,
    hit_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_sk ON self_memories(session_key, valid_to);

-- 主动搭话决策日志。三道本地闸门 + 模型决策都记一条，用来观察「她多久想说一次、
-- 被哪道闸门挡住、影子模式下本来会说什么」。这是调阈值的唯一依据。
CREATE TABLE IF NOT EXISTS active_decisions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    stage TEXT DEFAULT '',        -- gate 被闸门挡 / model 模型已决策 / sent 真发出去了
    tier INTEGER DEFAULT 0,       -- 2=接自己的话 3=群闲聊
    action TEXT DEFAULT '',       -- blocked / speak / observe
    reason TEXT DEFAULT '',       -- 闸门代号，见 attention.REASONS
    detail TEXT DEFAULT '',       -- 影子模式下「本来会说什么」
    latency_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_adec_sk ON active_decisions(session_key, id);
"""

KEEP_TURNS_PER_SESSION = 200


class SQLiteStore:
    """线程安全（thread-local 连接）的 SQLite 存储。"""

    def __init__(self, path: str):
        self.path = str(path)
        self._local = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=20, isolation_level=None, check_same_thread=False)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=20000')
            conn.execute('PRAGMA synchronous=NORMAL')
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)
        if self.get_setting('schema_version') is None:
            self.set_setting('schema_version', '1')

    # ---------------- settings ----------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._conn().execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self._conn().execute(
            'INSERT INTO settings(key, value) VALUES(?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)),
        )

    # ---------------- turns ----------------
    def add_turn(
        self,
        session_key: str,
        role: str,
        content: str,
        sender_id: str = '',
        sender_name: str = '',
        model: str = '',
        in_tokens: int = 0,
        out_tokens: int = 0,
    ) -> int:
        conn = self._conn()
        row = conn.execute(
            'SELECT COALESCE(MAX(seq), 0) FROM turns WHERE session_key=?', (session_key,)
        ).fetchone()
        seq = int(row[0]) + 1
        conn.execute(
            'INSERT INTO turns(session_key, seq, role, sender_id, sender_name, content, model, in_tokens, out_tokens, created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (session_key, seq, role, sender_id, sender_name, content, model, in_tokens, out_tokens, int(time.time())),
        )
        # 控制单会话总行数，防止无限增长
        conn.execute(
            'DELETE FROM turns WHERE session_key=? AND seq <= ?',
            (session_key, seq - KEEP_TURNS_PER_SESSION),
        )
        return seq

    def recent_turns(self, session_key: str, limit: int = 12) -> list[dict]:
        rows = self._conn().execute(
            'SELECT role, sender_id, sender_name, content FROM turns '
            'WHERE session_key=? ORDER BY seq DESC LIMIT ?',
            (session_key, limit),
        ).fetchall()
        return [
            {'role': r[0], 'sender_id': r[1], 'sender_name': r[2], 'content': r[3]}
            for r in reversed(rows)
        ]

    def clear_turns(self, session_key: str) -> None:
        self._conn().execute('DELETE FROM turns WHERE session_key=?', (session_key,))

    # ---------------- memories ----------------
    def add_fact(self, user_key: str, fact: str, source: str = '') -> None:
        now = int(time.time())
        self._conn().execute(
            'INSERT INTO memories(user_key, fact, source, confirmed, created_at, last_used_at) '
            'VALUES(?,?,?,1,?,?)',
            (user_key, fact, source, now, now),
        )

    def list_facts(self, user_key: str, limit: int = 10) -> list[str]:
        rows = self._conn().execute(
            'SELECT fact FROM memories WHERE user_key=? ORDER BY last_used_at DESC, id DESC LIMIT ?',
            (user_key, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def count_facts(self, user_key: str) -> int:
        row = self._conn().execute(
            'SELECT COUNT(*) FROM memories WHERE user_key=?', (user_key,)
        ).fetchone()
        return int(row[0])

    def remove_fact(self, user_key: str, fact: str) -> None:
        self._conn().execute('DELETE FROM memories WHERE user_key=? AND fact=?', (user_key, fact))

    def clear_user_facts(self, user_key: str) -> None:
        self._conn().execute('DELETE FROM memories WHERE user_key=?', (user_key,))

    def touch_fact(self, user_key: str, fact: str) -> None:
        self._conn().execute(
            'UPDATE memories SET last_used_at=? WHERE user_key=? AND fact=?',
            (int(time.time()), user_key, fact),
        )

    # ---------------- group backgrounds ----------------
    def set_background(self, session_key: str, content: str) -> None:
        self._conn().execute(
            'INSERT INTO backgrounds(session_key, content, updated_at) VALUES(?,?,?) '
            'ON CONFLICT(session_key) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at',
            (session_key, content, int(time.time())),
        )

    def get_background(self, session_key: str) -> str:
        row = self._conn().execute(
            'SELECT content FROM backgrounds WHERE session_key=?', (session_key,)
        ).fetchone()
        return row[0] if row else ''

    # ---------------- api metrics ----------------
    def add_api_call(
        self,
        session_key: str,
        kind: str,
        model: str,
        in_tokens: int,
        out_tokens: int,
        latency_ms: int,
        ok: bool,
        error_code: str = '',
    ) -> None:
        self._conn().execute(
            'INSERT INTO api_calls(day, ts, session_key, kind, model, in_tokens, out_tokens, latency_ms, ok, error_code) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (time.strftime('%Y-%m-%d'), int(time.time()), session_key, kind, model,
             in_tokens, out_tokens, latency_ms, 1 if ok else 0, error_code),
        )

    def today_stats(self) -> dict:
        """今日统计：总调用数、输出 token、主动搭话触发次数。"""
        day = time.strftime('%Y-%m-%d')
        row = self._conn().execute(
            'SELECT COUNT(*), COALESCE(SUM(out_tokens), 0), '
            "COALESCE(SUM(CASE WHEN kind = 'active' THEN 1 ELSE 0 END), 0) "
            'FROM api_calls WHERE day=?',
            (day,),
        ).fetchone()
        return {
            'calls': int(row[0]),
            'output_tokens': int(row[1]),
            'active_calls': int(row[2]),
        }

    # ==================== 记忆 L0：原始缓冲 ====================
    def buffer_append(self, session_key: str, role: str, content: str,
                      sender_id: str = '', sender_name: str = '') -> int:
        """把一条消息追加进缓冲，等提炼器成批消费（不直接进 prompt）。"""
        cur = self._conn().execute(
            'INSERT INTO memory_buffer(session_key, role, sender_id, sender_name, content, created_at) '
            'VALUES(?,?,?,?,?,?)',
            (session_key, role, sender_id, sender_name, str(content or ''), int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def buffer_fetch(self, session_key: str, after_id: int = 0, limit: int = 200) -> list[dict]:
        rows = self._conn().execute(
            'SELECT id, role, sender_id, sender_name, content, created_at FROM memory_buffer '
            'WHERE session_key=? AND id>? ORDER BY id LIMIT ?',
            (session_key, int(after_id), int(limit)),
        ).fetchall()
        return [{'id': r[0], 'role': r[1], 'sender_id': r[2], 'sender_name': r[3],
                 'content': r[4], 'created_at': r[5]} for r in rows]

    def buffer_pending(self, session_key: str, after_id: int = 0) -> int:
        row = self._conn().execute(
            'SELECT COUNT(*) FROM memory_buffer WHERE session_key=? AND id>?',
            (session_key, int(after_id)),
        ).fetchone()
        return int(row[0])

    def buffer_max_id(self, session_key: str) -> int:
        row = self._conn().execute(
            'SELECT COALESCE(MAX(id), 0) FROM memory_buffer WHERE session_key=?', (session_key,)
        ).fetchone()
        return int(row[0])

    def buffer_trim(self, session_key: str, keep: int = 300) -> None:
        """清掉已消费的旧缓冲，并给未消费的部分留一个上限。"""
        conn = self._conn()
        try:
            cursor = int(self.get_setting(f'mem.cursor:{session_key}') or 0)
        except (TypeError, ValueError):
            cursor = 0
        if cursor > 0:
            conn.execute('DELETE FROM memory_buffer WHERE session_key=? AND id<=?', (session_key, cursor))
        row = conn.execute(
            'SELECT COALESCE(MAX(id), 0) FROM memory_buffer WHERE session_key=?', (session_key,)
        ).fetchone()
        cutoff = int(row[0]) - int(keep)
        if cutoff > 0:
            conn.execute('DELETE FROM memory_buffer WHERE session_key=? AND id<=?', (session_key, cutoff))

    def buffer_last_at(self, session_key: str, after_id: int = 0) -> int:
        """未消费缓冲里最后一条的写入时间（判断「空闲多久了」用）。"""
        row = self._conn().execute(
            'SELECT COALESCE(MAX(created_at), 0) FROM memory_buffer WHERE session_key=? AND id>?',
            (session_key, int(after_id)),
        ).fetchone()
        return int(row[0])

    def buffer_sessions(self) -> list[str]:
        """还有残留缓冲的会话列表（含已消费未清理的，由提炼器自行判断）。"""
        rows = self._conn().execute(
            'SELECT DISTINCT session_key FROM memory_buffer ORDER BY session_key'
        ).fetchall()
        return [str(r[0]) for r in rows]

    # ==================== 记忆 L1：群级摘要 ====================
    def add_group_summary(self, session_key: str, content: str, covers_to: int = 0) -> int:
        cur = self._conn().execute(
            'INSERT INTO group_summaries(session_key, content, covers_to, created_at) VALUES(?,?,?,?)',
            (session_key, str(content or ''), int(covers_to), int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def recent_group_summaries(self, session_key: str, limit: int = 3) -> list[dict]:
        rows = self._conn().execute(
            'SELECT id, content, covers_to, created_at FROM group_summaries '
            'WHERE session_key=? ORDER BY id DESC LIMIT ?',
            (session_key, int(limit)),
        ).fetchall()
        return [{'id': r[0], 'content': r[1], 'covers_to': r[2], 'created_at': r[3]}
                for r in reversed(rows)]

    def trim_group_summaries(self, session_key: str, keep: int = 30) -> None:
        self._conn().execute(
            'DELETE FROM group_summaries WHERE session_key=? AND id NOT IN '
            '(SELECT id FROM group_summaries WHERE session_key=? ORDER BY id DESC LIMIT ?)',
            (session_key, session_key, int(keep)),
        )

    # ==================== 记忆 L2：用户事实 ====================
    def add_user_fact(self, session_key: str, user_id: str, fact: str, category: str = '',
                      source: str = 'inferred', importance: float = 0.5) -> int:
        now = int(time.time())
        cur = self._conn().execute(
            'INSERT INTO user_facts(session_key, user_id, fact, fact_norm, category, source, '
            'importance, valid_from, valid_to, last_hit_at, hit_count, created_at) '
            'VALUES(?,?,?,?,?,?,?,?,0,0,0,?)',
            (session_key, str(user_id), str(fact), normalize_fact(fact), category or '',
             source or 'inferred', float(importance), now, now),
        )
        return int(cur.lastrowid or 0)

    def find_user_fact(self, session_key: str, user_id: str, fact: str) -> Optional[dict]:
        """按归一化文本找仍有效的事实——判重不花任何 LLM 调用。"""
        row = self._conn().execute(
            'SELECT id, fact, importance, source FROM user_facts '
            'WHERE session_key=? AND user_id=? AND fact_norm=? AND valid_to=0 LIMIT 1',
            (session_key, str(user_id), normalize_fact(fact)),
        ).fetchone()
        if not row:
            return None
        return {'id': row[0], 'fact': row[1], 'importance': row[2], 'source': row[3]}

    def list_user_facts(self, session_key: str, user_id: Optional[str] = None,
                        limit: int = 20, include_expired: bool = False) -> list[dict]:
        sql = ('SELECT id, user_id, fact, category, source, importance, valid_from, valid_to, '
               'last_hit_at, hit_count FROM user_facts WHERE session_key=?')
        args: list = [session_key]
        if user_id is not None:
            sql += ' AND user_id=?'
            args.append(str(user_id))
        if not include_expired:
            sql += ' AND valid_to=0'
        sql += ' ORDER BY importance DESC, id DESC LIMIT ?'
        args.append(int(limit))
        rows = self._conn().execute(sql, tuple(args)).fetchall()
        return [{'id': r[0], 'user_id': r[1], 'fact': r[2], 'category': r[3], 'source': r[4],
                 'importance': r[5], 'valid_from': r[6], 'valid_to': r[7],
                 'last_hit_at': r[8], 'hit_count': r[9]} for r in rows]

    def touch_user_fact(self, fact_id: int) -> None:
        """命中一次：刷新时间戳与次数。衰减用 last_hit_at 而非 created_at——越用越牢。"""
        self._conn().execute(
            'UPDATE user_facts SET last_hit_at=?, hit_count=hit_count+1 WHERE id=?',
            (int(time.time()), int(fact_id)),
        )

    def expire_user_fact(self, fact_id: int, when: Optional[int] = None) -> None:
        """失效但不删除：保留时间轴，可追溯、可回滚。"""
        self._conn().execute(
            'UPDATE user_facts SET valid_to=? WHERE id=? AND valid_to=0',
            (int(when or time.time()), int(fact_id)),
        )

    def count_user_facts(self, session_key: str, user_id: Optional[str] = None) -> int:
        sql = 'SELECT COUNT(*) FROM user_facts WHERE session_key=? AND valid_to=0'
        args: list = [session_key]
        if user_id is not None:
            sql += ' AND user_id=?'
            args.append(str(user_id))
        row = self._conn().execute(sql, tuple(args)).fetchone()
        return int(row[0])

    def purge_user_facts(self, session_key: str, user_id: str) -> int:
        """清空某个用户在新记忆表里的事实（返回清掉的有效条数）。

        注意方法名：旧的 memories 表已经有 clear_user_facts(user_key)，
        这里不能再叫同名，否则会把旧方法覆盖掉（实测踩过）。
        """
        conn = self._conn()
        row = conn.execute(
            'SELECT COUNT(*) FROM user_facts WHERE session_key=? AND user_id=? AND valid_to=0',
            (session_key, str(user_id)),
        ).fetchone()
        conn.execute('DELETE FROM user_facts WHERE session_key=? AND user_id=?',
                     (session_key, str(user_id)))
        return int(row[0])

    def migrate_legacy_memories(self) -> int:
        """把旧 memories 表（user_key = session|u|uid）迁进 user_facts，只做一次。"""
        if self.get_setting('schema.memory_migrated'):
            return 0
        moved = 0
        try:
            rows = self._conn().execute('SELECT user_key, fact FROM memories').fetchall()
        except sqlite3.Error:
            rows = []
        for user_key, fact in rows:
            parts = str(user_key).split('|u|')
            if len(parts) != 2 or not str(fact).strip():
                continue
            session_key, user_id = parts[0], parts[1]
            if self.find_user_fact(session_key, user_id, fact):
                continue
            self.add_user_fact(session_key, user_id, fact, source='explicit', importance=0.9)
            moved += 1
        self.set_setting('schema.memory_migrated', '1')
        return moved

    # ==================== 记忆 L3：角色自身 ====================
    def add_self_memory(self, session_key: str, content: str, kind: str = 'event',
                        importance: float = 0.5) -> int:
        now = int(time.time())
        cur = self._conn().execute(
            'INSERT INTO self_memories(session_key, content, content_norm, kind, importance, '
            'valid_from, valid_to, last_hit_at, hit_count, created_at) VALUES(?,?,?,?,?,?,0,0,0,?)',
            (session_key, str(content), normalize_fact(content), kind or 'event',
             float(importance), now, now),
        )
        return int(cur.lastrowid or 0)

    def find_self_memory(self, session_key: str, content: str) -> Optional[dict]:
        row = self._conn().execute(
            'SELECT id, content FROM self_memories WHERE session_key=? AND content_norm=? '
            'AND valid_to=0 LIMIT 1',
            (session_key, normalize_fact(content)),
        ).fetchone()
        return {'id': row[0], 'content': row[1]} if row else None

    def list_self_memories(self, session_key: str, limit: int = 10,
                           include_expired: bool = False) -> list[dict]:
        sql = ('SELECT id, content, kind, importance, valid_from, valid_to, last_hit_at, hit_count '
               'FROM self_memories WHERE session_key=?')
        if not include_expired:
            sql += ' AND valid_to=0'
        sql += ' ORDER BY importance DESC, id DESC LIMIT ?'
        rows = self._conn().execute(sql, (session_key, int(limit))).fetchall()
        return [{'id': r[0], 'content': r[1], 'kind': r[2], 'importance': r[3],
                 'valid_from': r[4], 'valid_to': r[5], 'last_hit_at': r[6], 'hit_count': r[7]}
                for r in rows]

    def touch_self_memory(self, memory_id: int) -> None:
        self._conn().execute(
            'UPDATE self_memories SET last_hit_at=?, hit_count=hit_count+1 WHERE id=?',
            (int(time.time()), int(memory_id)),
        )

    # ==================== 主动搭话决策日志 ====================
    ACTIVE_LOG_KEEP = 5000

    def add_active_decision(self, session_key: str, stage: str, action: str,
                            reason: str = '', tier: int = 0, detail: str = '',
                            latency_ms: int = 0) -> int:
        conn = self._conn()
        cur = conn.execute(
            'INSERT INTO active_decisions(session_key, created_at, stage, tier, action, reason, '
            'detail, latency_ms) VALUES(?,?,?,?,?,?,?,?)',
            (session_key, int(time.time()), str(stage), int(tier), str(action),
             str(reason), str(detail or '')[:500], int(latency_ms)),
        )
        # 日志只是观察用，别让它无限长：只保留最近 N 条
        conn.execute('DELETE FROM active_decisions WHERE id <= '
                     '(SELECT MAX(id) FROM active_decisions) - ?', (self.ACTIVE_LOG_KEEP,))
        return int(cur.lastrowid or 0)

    def mark_active_decision_sent(self, decision_id: int, latency_ms: int = 0) -> None:
        """影子模式关闭、消息真发出去之后把那条 model 记录升级成 sent。"""
        if not decision_id:
            return
        self._conn().execute(
            'UPDATE active_decisions SET stage=?, latency_ms=? WHERE id=?',
            ('sent', int(latency_ms), int(decision_id)),
        )

    def recent_active_decisions(self, session_key: str, limit: int = 20) -> list[dict]:
        rows = self._conn().execute(
            'SELECT id, created_at, stage, tier, action, reason, detail FROM active_decisions '
            'WHERE session_key=? ORDER BY id DESC LIMIT ?', (session_key, int(limit)),
        ).fetchall()
        return [{'id': r[0], 'created_at': r[1], 'stage': r[2], 'tier': r[3],
                 'action': r[4], 'reason': r[5], 'detail': r[6]} for r in rows]

    def active_decision_stats(self, session_key: str, since: int = 0) -> dict:
        """近一段时间的决策分布——调阈值就看这个。"""
        conn = self._conn()
        rows = conn.execute(
            'SELECT action, reason, COUNT(*) FROM active_decisions '
            'WHERE session_key=? AND created_at>=? GROUP BY action, reason '
            'ORDER BY COUNT(*) DESC', (session_key, int(since)),
        ).fetchall()
        total = sum(int(r[2]) for r in rows)
        by_action: dict = {}
        by_reason: dict = {}
        for action, reason, count in rows:
            by_action[str(action)] = by_action.get(str(action), 0) + int(count)
            if reason:
                by_reason[str(reason)] = by_reason.get(str(reason), 0) + int(count)
        return {'total': total, 'by_action': by_action, 'by_reason': by_reason}

    def count_active_spoken(self, session_key: str, since: int) -> int:
        """（模拟的）主动发言次数：影子模式下也算，否则日上限在观察期永远不生效。"""
        row = self._conn().execute(
            "SELECT COUNT(*) FROM active_decisions WHERE session_key=? AND created_at>=? "
            "AND action='speak' AND stage IN ('model','sent')", (session_key, int(since)),
        ).fetchone()
        return int(row[0])

    def count_recent_assistant_msgs(self, session_key: str, since: int) -> int:
        """歌伊自己在这个会话最近说了多少条（含被动回复）——自适应冷却的依据。"""
        row = self._conn().execute(
            "SELECT COUNT(*) FROM turns WHERE session_key=? AND role='assistant' AND created_at>=?",
            (session_key, int(since)),
        ).fetchone()
        return int(row[0])

    def count_calls_today(self, session_key: str) -> int:
        """这个会话今天调了几次模型 —— **只算对话类**（被动 + 主动）。

        ⚠️ 2026-09-19 修：以前这里数的是**全部**调用（含 memory_curate），
        结果和群配额撞出一个致命 bug——

        配额按**人数**算（`30 + 人数×0.4`，小群夹在 40/天），
        而提炼按**消息量**算（每 `memory_curate_messages` 条一次）。
        小群一天几十次提炼，配额瞬间见底。

        线上实测（群 123456789，19 人，配额 40）：

            09-17   提炼 39 + 被动 6 + 主动 21 = 66   ★ 超配额
            09-18   提炼 33 + 被动 0 + 主动 10 = 43   ★ 超配额
                          ↑ 被动一次都没有 —— 群里怎么叫都不回话

        用户看到的症状是「其他群都好好在说话，就这一个群怎么叫都不回复」，
        而日志里只有一条 `本群额度已满，跳过本轮` 的 warning，很容易漏掉。

        配额的本意是「防被拉进小群疯狂消耗」，那指的是**对话**消耗；
        提炼是后台维护，跟群活跃度无关，不该占这个额度
        （它自己有 `memory_curate_min_interval` / `messages` / `idle_seconds` 三重节流）。
        """
        row = self._conn().execute(
            "SELECT COUNT(*) FROM api_calls WHERE session_key=? AND day=? "
            "AND kind IN ('passive', 'active')",
            (session_key, time.strftime('%Y-%m-%d')),
        ).fetchone()
        return int(row[0])

    # ==================== 事实容量控制 ====================
    def trim_user_facts(self, session_key: str, user_id: str = '',
                        max_per_user: int = 0, max_per_group: int = 0) -> int:
        """事实超容量就淘汰「最不值钱」的那几条。

        线上实测：一个大群一天塞进来 94 条，其中一个人占了 17 条、5 条在说同一件事
        （「跑团新人」「TRPG萌新」「刚加入群」…）。字面归一化挡不住这种换说法的重复，
        所以除了让模型自己判重（见 curator 的「已知事实」），还得有**容量硬上限**兜底：
        分数 = 重要度 + 命中次数×0.1 + 是否手工记过，低的先走。
        """
        removed = 0
        if max_per_user > 0 and user_id:
            removed += self._trim_facts(
                'session_key=? AND user_id=? AND valid_to=0', (session_key, str(user_id)),
                int(max_per_user))
        if max_per_group > 0:
            removed += self._trim_facts('session_key=? AND valid_to=0', (session_key,),
                                        int(max_per_group))
        return removed

    def _trim_facts(self, where: str, args: tuple, limit: int) -> int:
        conn = self._conn()
        rows = conn.execute(
            'SELECT id, importance, hit_count, source FROM user_facts WHERE %s' % where,
            args,
        ).fetchall()
        if len(rows) <= limit:
            return 0

        # 手工记的（「记住 xxx」）**不参与淘汰**：那是人明确要求留下的，
        # 不能因为一条重要度 0.9 的推断事实就把它挤掉（实测踩过：加个 0.3 的
        # 权重根本护不住它）。容量不够时先牺牲推断出来的。
        explicit = [row for row in rows if str(row[3]) == 'explicit']
        inferred = [row for row in rows if str(row[3]) != 'explicit']

        def weight(row):
            return float(row[1] or 0) + int(row[2] or 0) * 0.1

        room = max(0, limit - len(explicit))
        if room == 0:                      # 手工事实本身就超了，只能按分数留
            kept = {row[0] for row in sorted(explicit, key=weight, reverse=True)[:limit]}
        else:
            kept = {row[0] for row in explicit}
            kept |= {row[0] for row in sorted(inferred, key=weight, reverse=True)[:room]}
        doomed = [row[0] for row in rows if row[0] not in kept]
        if doomed:
            conn.executemany('DELETE FROM user_facts WHERE id=?', [(i,) for i in doomed])
        return len(doomed)

    def list_facts_of_users(self, session_key: str, user_ids, limit_per_user: int = 8) -> dict:
        """按一批 QQ 号取现有事实（提炼时喂给模型判重用）。返回 {user_id: [fact, ...]}。"""
        ids = [str(u) for u in (user_ids or []) if str(u).strip()]
        if not ids:
            return {}
        marks = ','.join('?' for _ in ids)
        rows = self._conn().execute(
            'SELECT user_id, fact FROM user_facts WHERE session_key=? AND valid_to=0 '
            'AND user_id IN (%s) ORDER BY importance DESC, id DESC' % marks,
            tuple([session_key] + ids),
        ).fetchall()
        out: dict = {}
        for user_id, fact in rows:
            bucket = out.setdefault(str(user_id), [])
            if len(bucket) < int(limit_per_user):
                bucket.append(str(fact))
        return out

    # ==================== 导出（排查/备份用） ====================
    def snapshot_to(self, target_path: str) -> str:
        """导出一个**自洽**的数据库快照。

        为什么不能直接复制 geyi.sqlite3：库开了 WAL，最近的写入还在 -wal 文件里，
        只拿主库会缺最新数据（实测踩过：拷出来一看最近两天的记忆全没有）。
        这里走 SQLite 的在线备份 API，它会把 WAL 里的内容一起并进目标文件，
        得到的单文件不依赖 -wal/-shm，别人拿去直接能打开。
        """
        target = str(target_path)
        folder = os.path.dirname(target)
        if folder:
            os.makedirs(folder, exist_ok=True)
        dest = sqlite3.connect(target)
        try:
            self._conn().backup(dest)
        finally:
            dest.close()
        return target

    def dump_memory(self, limit: int = 500) -> dict:
        """把四层记忆整表读出来（导出用，按时间倒序截断）。"""
        conn = self._conn()

        def rows(sql: str, args: tuple = ()) -> list[dict]:
            cur = conn.execute(sql, args)
            cols = [item[0] for item in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

        cap = int(limit)
        data = {
            'buffer': rows(
                'SELECT session_key, role, sender_id, sender_name, content, created_at '
                'FROM memory_buffer ORDER BY id DESC LIMIT ?', (cap,)),
            'group_summaries': rows(
                'SELECT session_key, content, covers_to, created_at '
                'FROM group_summaries ORDER BY id DESC LIMIT ?', (cap,)),
            'user_facts': rows(
                'SELECT session_key, user_id, fact, category, source, importance, '
                'valid_from, valid_to, last_hit_at, hit_count FROM user_facts '
                'ORDER BY id DESC LIMIT ?', (cap,)),
            'self_memories': rows(
                'SELECT session_key, content, kind, importance, valid_from, valid_to, '
                'last_hit_at, hit_count FROM self_memories ORDER BY id DESC LIMIT ?', (cap,)),
            # 说话人昵称：给导出的 QQ 号配上可读的名字
            'names': rows(
                'SELECT sender_id, sender_name FROM turns WHERE id IN '
                '(SELECT MAX(id) FROM turns WHERE sender_id<>\'\' GROUP BY sender_id)', ()),
            # 主动搭话的决策流水：影子模式调阈值就看它
            'active_decisions': rows(
                'SELECT session_key, created_at, stage, tier, action, reason, detail '
                'FROM active_decisions ORDER BY id DESC LIMIT ?', (min(cap, 200),)),
        }
        return data

    def count_all_memory(self) -> dict:
        """各层计数（导出表头用；不受 dump 的条数上限影响）。"""
        conn = self._conn()
        out = {}
        for label, sql in (
            ('buffer', 'SELECT COUNT(*) FROM memory_buffer'),
            ('buffer_sessions', 'SELECT COUNT(DISTINCT session_key) FROM memory_buffer'),
            ('group_summaries', 'SELECT COUNT(*) FROM group_summaries'),
            ('user_facts', 'SELECT COUNT(*) FROM user_facts'),
            ('user_facts_live', 'SELECT COUNT(*) FROM user_facts WHERE valid_to=0'),
            ('self_memories', 'SELECT COUNT(*) FROM self_memories'),
            ('self_memories_live', 'SELECT COUNT(*) FROM self_memories WHERE valid_to=0'),
            ('turns', 'SELECT COUNT(*) FROM turns'),
        ):
            try:
                out[label] = int(conn.execute(sql).fetchone()[0])
            except sqlite3.Error:
                out[label] = 0
        return out