# -*- encoding: utf-8 -*-
"""SQLite 存储层：会话、记忆、背景、调用指标。"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import math
import time
from typing import Any, Optional

# 事实判重用的归一化键：只留实义字符（中文、字母、数字），标点空白一律丢掉。
# 这样「我喜欢喝牛奶。」和「我喜欢喝牛奶」会被认成同一条。
_KEEP_RE = re.compile(r'[^\w\u4e00-\u9fff]+')
_TOKEN_RE = re.compile(r'[\u4e00-\u9fa5]{2,4}|[a-zA-Z0-9_]{2,}')
_STOP_WORDS = {
    '这个', '那个', '什么', '怎么', '可以', '没有', '不是', '就是',
    '我们', '你们', '他们', '感觉', '觉得', '因为', '所以', '虽然',
    '但是', '如果', '一下', '今天', '明天', '昨天', '现在', '刚刚'
}


_CHINESE_STOP_CHARS = set('的一是不了在人有我他这个上们来到时大地为子中你说生国年着就那和要她它么得之过想买点去用会看能好把给让做多等别再现因真听太问些很')


def normalize_fact(text: str) -> str:
    return _KEEP_RE.sub('', str(text or '')).lower()


def extract_keywords(text: str) -> set[str]:
    """提取用于比对的短语与关键词。"""
    raw = str(text or '').strip()
    if not raw:
        return set()
    tokens = {t.lower() for t in _TOKEN_RE.findall(raw) if t.lower() not in _STOP_WORDS}
    chars = [c for c in raw if '\u4e00' <= c <= '\u9fa5']
    for i in range(len(chars) - 1):
        bg = chars[i] + chars[i + 1]
        if bg not in _STOP_WORDS:
            tokens.add(bg)
    return tokens


# ==================== 事实关键词（写入端摊平语义） ====================
# 为什么要在写入时就存关键词，而不是检索时现算：
# 检索时现算只能拿"事实原文"做字面匹配，换说法就丢（「跑团新人」vs「刚玩 TRPG」）。
# 让提炼模型在写入时顺手给出同义词/上位词（「跑团」「TRPG」「桌游」），
# 检索端就只需要做一次字符串包含——0 依赖、0 延迟，效果接近轻量语义召回。
KEYWORD_SEP = ','


def normalize_keywords(value) -> str:
    """把模型给的关键词/本地分词结果统一成 `a,b,c`：去重、限长 12、最多 8 个。"""
    if value is None:
        return ''
    if isinstance(value, str):
        raw = re.split(r'[,，、;；/\s]+', value)
    else:
        try:
            raw = list(value)
        except TypeError:
            return ''
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or '').strip()
        if not text or len(text) > 12:
            continue
        low = text.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(text)
        if len(out) >= 8:
            break
    return KEYWORD_SEP.join(out)


def split_keywords(value) -> list[str]:
    return [x for x in str(value or '').split(KEYWORD_SEP) if x]


def text_similarity(text1: str, text2: str) -> float:
    """计算两段记忆文本之间的语义重叠相似度 (0.0 ~ 1.0)。纯本地运算，0 延迟。
    结合核心实义汉字与词汇 Overlap/Jaccard 系数。
    """
    t1 = str(text1 or '').strip().lower()
    t2 = str(text2 or '').strip().lower()
    if not t1 or not t2:
        return 0.0
    if t1 == t2:
        return 1.0
    if (t1 in t2 or t2 in t1) and min(len(t1), len(t2)) / max(len(t1), len(t2)) > 0.6:
        return 0.9

    c1 = {c for c in t1 if '\u4e00' <= c <= '\u9fa5' and c not in _CHINESE_STOP_CHARS}
    c2 = {c for c in t2 if '\u4e00' <= c <= '\u9fa5' and c not in _CHINESE_STOP_CHARS}
    e1 = set(re.findall(r'[a-zA-Z0-9_]{2,}', t1))
    e2 = set(re.findall(r'[a-zA-Z0-9_]{2,}', t2))
    all1 = c1 | e1
    all2 = c2 | e2
    if not all1 or not all2:
        return 0.0

    inter = len(all1 & all2)
    if inter == 0:
        return 0.0
    overlap = inter / min(len(all1), len(all2))
    jaccard = inter / len(all1 | all2)
    return 0.5 * overlap + 0.5 * jaccard

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
    cached_tokens INTEGER DEFAULT 0,
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
    created_at INTEGER NOT NULL,
    keywords TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ufact ON user_facts(session_key, user_id, valid_to);
CREATE INDEX IF NOT EXISTS idx_ufact_norm ON user_facts(session_key, user_id, fact_norm);

-- 事实回收站：容量淘汰（单人 12 条 / 单群 600 条）以前是**直接 DELETE**，
-- 淘汰掉的那条就永远没了。这里先留档再删，想看「她以前记得什么」随时能翻。
CREATE TABLE IF NOT EXISTS user_facts_archive(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER,
    session_key TEXT,
    user_id TEXT,
    fact TEXT,
    category TEXT,
    source TEXT,
    importance REAL,
    valid_from INTEGER,
    hit_count INTEGER,
    archived_at INTEGER NOT NULL,
    reason TEXT DEFAULT 'capacity'
);
CREATE INDEX IF NOT EXISTS idx_ufact_arch ON user_facts_archive(session_key, user_id, id);

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

-- L2 扩展：群友/私聊印象与羁绊卡片（区分群聊与私聊独立画像）
CREATE TABLE IF NOT EXISTS user_impressions(
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'group',
    summary TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, scope)
);

-- 印象历史：只追加，永不改。主表是"当前生效的那一版"，这里留每一次改动前/后的全文，
-- 便于回溯「她对这个人的看法是怎么变的」，也避免改砸了就把旧句子永久丢掉。
CREATE TABLE IF NOT EXISTS user_impressions_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'group',
    summary TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    reason TEXT DEFAULT ''          -- 'create' / 'update' / 'backfill'
);
CREATE INDEX IF NOT EXISTS idx_imphist_uid ON user_impressions_history(user_id, id);

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

-- 私聊关系状态（好感度 / 阶段 / 心情 / 事件进度）。只记私聊，按用户一行。
-- 与记忆开关无关：关掉记忆不该让所有人重新变回陌生人。
CREATE TABLE IF NOT EXISTS user_relationship(
    user_id TEXT PRIMARY KEY,
    affection REAL NOT NULL DEFAULT 0,
    stage INTEGER NOT NULL DEFAULT 0,
    stage_since REAL NOT NULL DEFAULT 0,
    days_active INTEGER NOT NULL DEFAULT 0,
    last_day TEXT NOT NULL DEFAULT '',
    gain_today REAL NOT NULL DEFAULT 0,
    tag_counts TEXT NOT NULL DEFAULT '{}',
    bad_streak INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    last_advance_day TEXT NOT NULL DEFAULT '',
    mood TEXT NOT NULL DEFAULT '',
    mood_until REAL NOT NULL DEFAULT 0,
    flags TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT 0
);

-- 关系流水：只追加，不改不删（除非"忘了我"）。调参、申诉、复盘"她为什么突然变冷"都靠它。
-- shadow=1 表示影子模式：评审给了分，但没有生效。
CREATE TABLE IF NOT EXISTS relationship_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ts REAL NOT NULL,
    delta_raw INTEGER NOT NULL DEFAULT 0,
    delta_applied REAL NOT NULL DEFAULT 0,
    tag TEXT NOT NULL DEFAULT '',
    advance_level INTEGER NOT NULL DEFAULT 0,
    aff_before REAL NOT NULL DEFAULT 0,
    aff_after REAL NOT NULL DEFAULT 0,
    stage_before INTEGER NOT NULL DEFAULT 0,
    stage_after INTEGER NOT NULL DEFAULT 0,
    shadow INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    user_text TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rellog_uid ON relationship_log(user_id, id);
"""

KEEP_TURNS_PER_SESSION = 200


def is_private_session_key(session_key: str) -> bool:
    """会话键是否代表私聊会话（冒号或竖线格式）。"""
    key = str(session_key or '')
    return ':private:' in key or '|private|' in key or key.endswith('|private')


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

    def close(self) -> None:
        """关掉本线程的连接（测试/退出用）。

        连接是 thread-local 的，所以只能关当前线程这一条；没关掉的连接在解释器退出时
        会刷一串 ResourceWarning（2026-11-03 审查提到）。运行时不需要主动调用
        ——进程重启时由操作系统回收，SQLite 是实时落盘的。
        """
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def _init(self) -> None:
        conn = self._conn()
        self._migrate(conn)
        conn.executescript(_SCHEMA)
        if self.get_setting('schema_version') is None:
            self.set_setting('schema_version', '1')
        # 一次性清掉第一版用本地分词硬灌的关键词（噪音，见 backfill_fact_keywords 的注释）。
        # 之后 keywords 只由提炼模型在写入时给出。
        if self.get_setting('facts.keywords_cleanup') is None:
            try:
                cleared = self.backfill_fact_keywords()
                if cleared:
                    logger.info('清理本地分词关键词：%d 条（关键词改为只收模型给的同义词）', cleared)
            except sqlite3.Error:
                logger.exception('清理关键词失败（不影响启动）')

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """老库补列：只做加法，绝不删列——升级不能把数据搞丢。"""
        cols = {row[1] for row in conn.execute('PRAGMA table_info(api_calls)')}
        if cols and 'cached_tokens' not in cols:
            conn.execute('ALTER TABLE api_calls ADD COLUMN cached_tokens INTEGER DEFAULT 0')
        # 印象表增加 scope 支持（区分 group 与 private，独立提炼互不覆盖）
        try:
            imp_cols = {row[1] for row in conn.execute('PRAGMA table_info(user_impressions)')}
            if imp_cols and 'scope' not in imp_cols:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS user_impressions_v2(
                        user_id TEXT NOT NULL,
                        scope TEXT NOT NULL DEFAULT 'group',
                        summary TEXT NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(user_id, scope)
                    )
                ''')
                conn.execute('''
                    INSERT OR IGNORE INTO user_impressions_v2(user_id, scope, summary, updated_at)
                    SELECT user_id, 'group', summary, updated_at FROM user_impressions
                ''')
                conn.execute('DROP TABLE user_impressions')
                conn.execute('ALTER TABLE user_impressions_v2 RENAME TO user_impressions')
        except sqlite3.Error:
            pass

        try:
            hcols = {row[1] for row in conn.execute('PRAGMA table_info(user_impressions_history)')}
            if hcols and 'scope' not in hcols:
                conn.execute("ALTER TABLE user_impressions_history ADD COLUMN scope TEXT DEFAULT 'group'")
                conn.execute("UPDATE user_impressions_history SET scope='group' WHERE scope IS NULL OR scope=''")
        except sqlite3.Error:
            pass

        # 事实关键词（写入端摊平语义，检索端做字面匹配用）：老库补列
        try:
            fcols = {row[1] for row in conn.execute('PRAGMA table_info(user_facts)')}
            if fcols and 'keywords' not in fcols:
                conn.execute("ALTER TABLE user_facts ADD COLUMN keywords TEXT DEFAULT ''")
        except sqlite3.Error:
            pass

        # 印象历史表是后加的：第一次建好后，把**当前**这批印象作为第一版补进去，
        # 否则老库的历史是空的，回溯时看不到"最初她是怎么看这个人的"。
        try:
            hist = conn.execute('SELECT COUNT(*) FROM user_impressions_history').fetchone()[0]
            if not hist:
                conn.execute(
                    'INSERT INTO user_impressions_history(user_id, scope, summary, created_at, reason) '
                    'SELECT user_id, scope, summary, updated_at, ? FROM user_impressions', ('backfill',))
        except sqlite3.Error:
            pass

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

    def settings_with_prefix(self, prefix: str) -> list:
        """按前缀列出 (key, value)——静默状态是 `rest:<session_key>` 这样的键，
        骰主想知道「现在都有谁被静默着」时必须能枚举出来。"""
        rows = self._conn().execute(
            'SELECT key, value FROM settings WHERE key LIKE ? ORDER BY key',
            (str(prefix) + '%',),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

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
        cached_tokens: int = 0,
    ) -> None:
        self._conn().execute(
            'INSERT INTO api_calls(day, ts, session_key, kind, model, in_tokens, out_tokens, cached_tokens, latency_ms, ok, error_code) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (time.strftime('%Y-%m-%d'), int(time.time()), session_key, kind, model,
             in_tokens, out_tokens, int(cached_tokens or 0), latency_ms, 1 if ok else 0, error_code),
        )

    def today_stats(self) -> dict:
        """今日统计：总调用数、输入/输出 token、其中命中缓存的输入 token。"""
        day = time.strftime('%Y-%m-%d')
        row = self._conn().execute(
            'SELECT COUNT(*), COALESCE(SUM(out_tokens), 0), '
            "COALESCE(SUM(CASE WHEN kind = 'active' THEN 1 ELSE 0 END), 0), "
            'COALESCE(SUM(in_tokens), 0), COALESCE(SUM(cached_tokens), 0) '
            'FROM api_calls WHERE day=?',
            (day,),
        ).fetchone()
        return {
            'calls': int(row[0]),
            'output_tokens': int(row[1]),
            'active_calls': int(row[2]),
            'input_tokens': int(row[3]),
            'cached_tokens': int(row[4]),
        }

    def today_stats_by_kind(self) -> list[dict]:
        """今天按用途拆账：kind / 次数 / 输入 token（大的排前面）。

        拆账的意义在于回答「钱到底花在哪」——搭话判断、主动回复、被动回复、
        记忆提炼的单价和频次差着一个量级，混成一个总数什么也看不出来。
        """
        day = time.strftime('%Y-%m-%d')
        rows = self._conn().execute(
            'SELECT kind, COUNT(*), COALESCE(SUM(in_tokens), 0) '
            'FROM api_calls WHERE day=? GROUP BY kind ORDER BY 3 DESC',
            (day,),
        ).fetchall()
        return [
            {'kind': str(r[0] or ''), 'calls': int(r[1]), 'input_tokens': int(r[2])}
            for r in rows
        ]

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
                      source: str = 'inferred', importance: float = 0.5,
                      keywords=None) -> int:
        """写一条用户事实。

        `keywords` **只收模型给的**同义词/上位词，不做本地分词兜底：
        本地 `extract_keywords` 吐的是"喜欢/来自/和牛"这种二元组，它们本来就在
        fact 原文里（子串匹配已经能中），再拿去当关键词只会让"喜欢"命中几十条事实、
        把真正相关的那条挤出候选池（实测过，噪音比收益大）。没有就留空。
        """
        now = int(time.time())
        kw = normalize_keywords(keywords)
        cur = self._conn().execute(
            'INSERT INTO user_facts(session_key, user_id, fact, fact_norm, category, source, '
            'importance, valid_from, valid_to, last_hit_at, hit_count, created_at, keywords) '
            'VALUES(?,?,?,?,?,?,?,?,0,0,0,?,?)',
            (session_key, str(user_id), str(fact), normalize_fact(fact), category or '',
             source or 'inferred', float(importance), now, now, kw),
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

    def find_similar_user_fact(self, session_key: str, user_id: str, fact: str,
                               threshold: float = 0.60) -> Optional[dict]:
        """模糊判重：若该用户已有相似度 >= threshold 的事实，返回已有条目，避免存入同义废话。"""
        exact = self.find_user_fact(session_key, user_id, fact)
        if exact:
            return exact
        rows = self.list_user_facts(session_key, user_id, limit=30, include_expired=False)
        for r in rows:
            sim = text_similarity(fact, r.get('fact') or '')
            if sim >= threshold:
                return r
        return None

    def list_user_facts(self, session_key: str, user_id: Optional[str] = None,
                        limit: int = 20, include_expired: bool = False,
                        include_global: bool = False,
                        cross_scope: bool = False) -> list[dict]:
        is_priv = is_private_session_key(session_key)
        args: list = []
        if is_priv:
            if cross_scope and user_id is not None:
                # 私聊互通模式：查私聊事实 + 全局事实 + 该用户的群聊事实
                sql = ('SELECT id, user_id, fact, category, source, importance, valid_from, valid_to, '
                       'keywords, last_hit_at, hit_count, session_key FROM user_facts '
                       'WHERE user_id=? AND (session_key=? OR session_key=\'global\' OR '
                       '(session_key NOT LIKE \'%:private:%\' AND session_key NOT LIKE \'%|private|%\'))')
                args = [str(user_id), session_key]
            else:
                # 私聊隔离模式：仅查该私聊会话的事实（不混入群聊与全局事实）
                sql = ('SELECT id, user_id, fact, category, source, importance, valid_from, valid_to, '
                       'keywords, last_hit_at, hit_count, session_key FROM user_facts WHERE session_key=?')
                args = [session_key]
                if user_id is not None:
                    sql += ' AND user_id=?'
                    args.append(str(user_id))
        else:
            # 群聊会话
            if cross_scope and user_id is not None:
                # 群聊互通模式：查本群事实 + 全局事实 + 该用户的私聊事实
                sql = ('SELECT id, user_id, fact, category, source, importance, valid_from, valid_to, '
                       'keywords, last_hit_at, hit_count, session_key FROM user_facts '
                       'WHERE user_id=? AND (session_key=? OR session_key=\'global\' OR '
                       'session_key LIKE \'%:private:%\' OR session_key LIKE \'%|private|%\')')
                args = [str(user_id), session_key]
            elif include_global and user_id is not None and session_key != 'global':
                # 群聊隔离模式：仅查本群 + global（不混入私聊事实）
                sql = ('SELECT id, user_id, fact, category, source, importance, valid_from, valid_to, '
                       'keywords, last_hit_at, hit_count, session_key FROM user_facts '
                       'WHERE (session_key=? OR session_key=\'global\') AND user_id=?')
                args = [session_key, str(user_id)]
            else:
                sql = ('SELECT id, user_id, fact, category, source, importance, valid_from, valid_to, '
                       'keywords, last_hit_at, hit_count, session_key FROM user_facts WHERE session_key=?')
                args = [session_key]
                if user_id is not None:
                    sql += ' AND user_id=?'
                    args.append(str(user_id))

        if not include_expired:
            sql += ' AND valid_to=0'
        sql += ' ORDER BY importance DESC, id DESC LIMIT ?'
        args.append(int(limit))
        rows = self._conn().execute(sql, tuple(args)).fetchall()
        return [{'id': r[0], 'user_id': r[1], 'fact': r[2], 'category': r[3], 'source': r[4],
                 'importance': r[5], 'valid_from': r[6], 'valid_to': r[7], 'keywords': r[8],
                 'last_hit_at': r[9], 'hit_count': r[10], 'session_key': r[11]} for r in rows]

    def facts_matching(self, session_key: str, user_id: str, words, limit: int = 40,
                       include_global: bool = True) -> list[dict]:
        """按关键词预取候选事实（关键词命中优先，别让高重要度的废话挤掉真正相关的）。

        为什么单独查一次：容量放大到几千条之后，`ORDER BY importance LIMIT N` 很容易
        把"低重要度但正好切题"的那条挡在候选池外——检索层再聪明也看不到它。
        这里用 `keywords` + `fact` 的 LIKE 做一次粗筛，一次几十毫秒，换回召回率。
        """
        keys = [str(w).strip() for w in (words or []) if len(str(w).strip()) >= 2][:6]
        uid = str(user_id or '').strip()
        if not keys or not uid:
            return []
        sessions = [str(session_key)]
        if include_global and session_key != 'global':
            sessions.append('global')
        like = ' OR '.join(['keywords LIKE ?'] * len(keys) + ['fact LIKE ?'] * len(keys))
        marks = ','.join('?' for _ in sessions)
        sql = ('SELECT id, user_id, fact, category, source, importance, valid_from, valid_to, '
               'keywords, last_hit_at, hit_count, session_key FROM user_facts '
               'WHERE session_key IN (%s) AND user_id=? AND valid_to=0 AND (%s) '
               'ORDER BY importance DESC, id DESC LIMIT ?' % (marks, like))
        args = list(sessions) + [uid]
        args += ['%%%s%%' % k for k in keys] * 2
        args.append(int(limit))
        rows = self._conn().execute(sql, tuple(args)).fetchall()
        return [{'id': r[0], 'user_id': r[1], 'fact': r[2], 'category': r[3], 'source': r[4],
                 'importance': r[5], 'valid_from': r[6], 'valid_to': r[7], 'keywords': r[8],
                 'last_hit_at': r[9], 'hit_count': r[10], 'session_key': r[11]} for r in rows]

    def backfill_fact_keywords(self) -> int:
        """**清掉**早期用本地分词硬填的关键词，返回清理条数。

        第一版为了"老事实也能按关键词搜到"，用 `extract_keywords` 给历史事实灌了
        二元组（"喜欢/来自/和牛/的小"）。实测发现它们全是噪音：
        任何一句带"喜欢"的话都会同时命中几十条事实的相关度 0.95，
        把真正切题的那条挤出候选池。关键词只该来自模型给的同义词——
        老数据留空即可，它们靠 `fact` 原文匹配（`facts_matching` 里同时 LIKE fact）。
        """
        conn = self._conn()
        cur = conn.execute("UPDATE user_facts SET keywords='' WHERE COALESCE(keywords,'')<>''")
        # 两个标记都置上：`keywords_cleanup` 是 _init 判重用的，旧名字留着免得老库回头又"回填"一次
        self.set_setting('facts.keywords_cleanup', '1')
        self.set_setting('facts.keywords_backfilled', '1')
        return int(cur.rowcount or 0)

    def get_user_impression(self, user_id: str, scope: str = 'group') -> Optional[dict]:
        """获取用户高维印象卡片（支持 scope='group' 与 'private'，隔离提炼）。"""
        sc = str(scope or 'group').strip().lower()
        row = self._conn().execute(
            'SELECT user_id, summary, updated_at, scope FROM user_impressions WHERE user_id=? AND scope=?',
            (str(user_id), sc),
        ).fetchone()
        if not row:
            return None
        return {'user_id': row[0], 'summary': row[1], 'updated_at': row[2], 'scope': row[3]}

    def set_user_impression(self, user_id: str, summary: str, scope: str = 'group') -> bool:
        """更新用户高维印象卡片（按 scope 隔离群聊与私聊）；**每次真实改动都追加一条历史**。返回是否真的改了。"""
        text = str(summary or '').strip()
        if not text:
            return False
        uid = str(user_id)
        sc = str(scope or 'group').strip().lower()
        conn = self._conn()
        now = int(time.time())
        row = conn.execute(
            'SELECT summary FROM user_impressions WHERE user_id=? AND scope=?',
            (uid, sc),
        ).fetchone()
        if row and str(row[0] or '').strip() == text:
            return False                      # 一字未改：不动主表，也不写历史
        conn.execute(
            'INSERT INTO user_impressions(user_id, scope, summary, updated_at) VALUES(?,?,?,?) '
            'ON CONFLICT(user_id, scope) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at',
            (uid, sc, text, now),
        )
        try:
            conn.execute(
                'INSERT INTO user_impressions_history(user_id, scope, summary, created_at, reason) '
                'VALUES(?,?,?,?,?)', (uid, sc, text, now, 'create' if not row else 'update'),
            )
        except sqlite3.Error:                 # 历史写失败不能拖累主表
            pass
        return True

    def impression_history(self, user_id: str, scope: Optional[str] = 'group', limit: int = 20) -> list[dict]:
        """这个人的印象所有历史版本，新的在前。"""
        if scope is not None:
            rows = self._conn().execute(
                'SELECT id, summary, created_at, reason, scope FROM user_impressions_history '
                'WHERE user_id=? AND scope=? ORDER BY id DESC LIMIT ?',
                (str(user_id), str(scope), int(limit)),
            ).fetchall()
        else:
            rows = self._conn().execute(
                'SELECT id, summary, created_at, reason, scope FROM user_impressions_history '
                'WHERE user_id=? ORDER BY id DESC LIMIT ?',
                (str(user_id), int(limit)),
            ).fetchall()
        return [{'id': r[0], 'summary': r[1], 'created_at': r[2], 'reason': r[3], 'scope': r[4]} for r in rows]

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

    def purge_user_facts(self, session_key: str, user_id: str,
                         include_global: bool = True) -> int:
        """清空某个用户的事实（返回清掉的有效条数）。

        注意方法名：旧的 memories 表已经有 clear_user_facts(user_key)，
        这里不能再叫同名，否则会把旧方法覆盖掉（实测踩过）。

        `include_global=True` 会连**全局范围**（session_key='global'）的事实一起清掉：
        「忘了我」以前只删当前群的，而跨群偏好存在 global 里，于是她换个群还记得你
        （2026-11-03 审查发现）。
        """
        conn = self._conn()
        sessions = [session_key]
        if include_global and session_key != 'global':
            sessions.append('global')
        placeholders = ','.join('?' for _ in sessions)
        row = conn.execute(
            'SELECT COUNT(*) FROM user_facts WHERE session_key IN (%s) AND user_id=? '
            'AND valid_to=0' % placeholders,
            tuple(sessions) + (str(user_id),),
        ).fetchone()
        conn.execute(
            'DELETE FROM user_facts WHERE session_key IN (%s) AND user_id=?' % placeholders,
            tuple(sessions) + (str(user_id),),
        )
        conn.execute('DELETE FROM user_impressions WHERE user_id=?', (str(user_id),))
        # 「忘了我」要连历史一起清：历史里存的是同一批私人信息，留着等于没忘
        conn.execute('DELETE FROM user_impressions_history WHERE user_id=?', (str(user_id),))
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

    def find_similar_self_memory(self, session_key: str, content: str,
                                 threshold: float = 0.60) -> Optional[dict]:
        """模糊判重：若当前会话已有相似度 >= threshold 的歌伊自身记忆，返回已有条目。"""
        exact = self.find_self_memory(session_key, content)
        if exact:
            return exact
        rows = self.list_self_memories(session_key, limit=50, include_expired=False)
        for r in rows:
            sim = text_similarity(content, r.get('content') or '')
            if sim >= threshold:
                return r
        return None

    def expire_self_memory(self, memory_id: int, when: Optional[int] = None) -> None:
        """失效但不删除自身记忆。"""
        self._conn().execute(
            'UPDATE self_memories SET valid_to=? WHERE id=? AND valid_to=0',
            (int(when or time.time()), int(memory_id)),
        )

    def deduplicate_active_memories(self, threshold: float = 0.55) -> dict:
        """存量去重：扫描库中全部有效事实与自身记忆，将语义相似度 >= threshold 的冗余旧条目软失效。"""
        now = int(time.time())
        stats = {'user_facts_expired': 0, 'self_memories_expired': 0}

        # 1. 自身记忆去重：按 session_key 分组
        cur = self._conn().execute(
            'SELECT id, session_key, content, importance, hit_count, created_at '
            'FROM self_memories WHERE valid_to=0 ORDER BY id DESC'
        )
        self_rows = cur.fetchall()
        groups: dict[str, list] = {}
        for r in self_rows:
            groups.setdefault(r[1], []).append({
                'id': r[0], 'session_key': r[1], 'content': r[2],
                'importance': r[3], 'hit_count': r[4], 'created_at': r[5]
            })
        for sk, items in groups.items():
            kept: list = []
            for item in items:
                duplicate_of = None
                for k in kept:
                    if text_similarity(item['content'], k['content']) >= threshold:
                        duplicate_of = k
                        break
                if duplicate_of:
                    self.expire_self_memory(item['id'], when=now)
                    stats['self_memories_expired'] += 1
                else:
                    kept.append(item)

        # 2. 用户事实去重：按 (session_key, user_id) 分组
        cur_u = self._conn().execute(
            'SELECT id, session_key, user_id, fact, importance, hit_count, created_at '
            'FROM user_facts WHERE valid_to=0 ORDER BY id DESC'
        )
        u_rows = cur_u.fetchall()
        u_groups: dict[tuple[str, str], list] = {}
        for r in u_rows:
            u_groups.setdefault((r[1], r[2]), []).append({
                'id': r[0], 'session_key': r[1], 'user_id': r[2], 'fact': r[3],
                'importance': r[4], 'hit_count': r[5], 'created_at': r[6]
            })
        for (sk, uid), items in u_groups.items():
            kept_u: list = []
            for item in items:
                dup = None
                for k in kept_u:
                    if text_similarity(item['fact'], k['fact']) >= threshold:
                        dup = k
                        break
                if dup:
                    self.expire_user_fact(item['id'], when=now)
                    stats['user_facts_expired'] += 1
                else:
                    kept_u.append(item)

        return stats

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

        线上实测（群 957309761，19 人，配额 40）：

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
                        max_per_user: int = 0, max_per_group: int = 0,
                        video_share_keep: int = 0) -> int:
        """事实超容量就淘汰「最不值钱」的那几条。

        线上实测：一个大群一天塞进来 94 条，其中一个人占了 17 条、5 条在说同一件事
        （「跑团新人」「TRPG萌新」「刚加入群」…）。字面归一化挡不住这种换说法的重复，
        所以除了让模型自己判重（见 curator 的「已知事实」），还得有**容量硬上限**兜底：
        分数 = 重要度 + 命中次数×0.1 + 是否手工记过，低的先走。

        `video_share_keep`：视频分享类单独封顶（默认 3 条）。这类是**别的插件按事件写的**，
        每条一条、永不重复、还标了 explicit，不单独限流就会把一个人的记忆全占满
        （线上：一个用户 12 条上限里 6 条是"分享了视频《xxx》"）。
        """
        removed = 0
        if max_per_user > 0 and user_id:
            removed += self._trim_facts(
                'session_key=? AND user_id=? AND valid_to=0', (session_key, str(user_id)),
                int(max_per_user), int(video_share_keep))
        if max_per_group > 0:
            removed += self._trim_facts('session_key=? AND valid_to=0', (session_key,),
                                        int(max_per_group), int(video_share_keep))
        return removed

    def _trim_facts(self, where: str, args: tuple, limit: int, video_keep: int = 0) -> int:
        conn = self._conn()
        rows = conn.execute(
            'SELECT id, importance, hit_count, source, category, created_at FROM user_facts'
            ' WHERE %s' % where, args,
        ).fetchall()
        if not rows:
            return 0

        killed: set = set()

        # ① 视频分享单独封顶：只留最近 N 条，先于总容量判断（否则小体量时永远触发不到）
        #    排序用 (created_at, id)：同一秒写入多条时 created_at 全一样，
        #    只按时间排会退化成"保留最旧的"，反而把刚分享的删了。
        if video_keep > 0:
            videos = [row for row in rows if str(row[4] or '') == 'bili_share']
            if len(videos) > video_keep:
                keep = {row[0] for row in sorted(
                    videos, key=lambda r: (int(r[5] or 0), int(r[0])), reverse=True)[:video_keep]}
                killed |= {row[0] for row in videos if row[0] not in keep}

        alive = [row for row in rows if row[0] not in killed]
        if len(alive) > limit:
            # 手工记的（「记住 xxx」）**不参与淘汰**：那是人明确要求留下的。
            # 但机器按事件写的（bili_share 等）不算"手工"，它们该和推断事实一起排队。
            def is_manual(row):
                return str(row[3]) == 'explicit' and str(row[4] or '') != 'bili_share'

            manual = [row for row in alive if is_manual(row)]
            others = [row for row in alive if not is_manual(row)]

            def weight(row):
                return float(row[1] or 0) + int(row[2] or 0) * 0.1

            room = max(0, limit - len(manual))
            if room == 0:                  # 手工事实本身就超了，只能按分数留
                kept = {row[0] for row in sorted(manual, key=weight, reverse=True)[:limit]}
            else:
                kept = {row[0] for row in manual}
                kept |= {row[0] for row in sorted(others, key=weight, reverse=True)[:room]}
            killed |= {row[0] for row in alive if row[0] not in kept}

        if not killed:
            return 0
        doomed = sorted(killed)
        if doomed:
            # 先留档再删：淘汰是「从记忆里拿掉」，不是「历史里抹掉」
            try:
                now = int(time.time())
                conn.executemany(
                    'INSERT INTO user_facts_archive(fact_id, session_key, user_id, fact, category, '
                    'source, importance, valid_from, hit_count, archived_at, reason) '
                    'SELECT id, session_key, user_id, fact, category, source, importance, '
                    'valid_from, hit_count, ?, ? FROM user_facts WHERE id=?',
                    [(now, 'capacity', i) for i in doomed],
                )
            except sqlite3.Error:
                pass                        # 留档失败不能挡住淘汰（否则容量会一直涨）
            conn.executemany('DELETE FROM user_facts WHERE id=?', [(i,) for i in doomed])
        return len(doomed)

    def archived_facts(self, session_key: str, user_id: str = '', limit: int = 50) -> list[dict]:
        """回收站里的事实（被容量淘汰的那些），新的在前。"""
        sql = ('SELECT id, session_key, user_id, fact, importance, archived_at, reason '
               'FROM user_facts_archive WHERE session_key=?')
        args: list = [str(session_key)]
        if user_id:
            sql += ' AND user_id=?'
            args.append(str(user_id))
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(int(limit))
        rows = self._conn().execute(sql, tuple(args)).fetchall()
        return [{'id': r[0], 'session_key': r[1], 'user_id': r[2], 'fact': r[3],
                 'importance': r[4], 'archived_at': r[5], 'reason': r[6]} for r in rows]

    def list_facts_of_users(self, session_key: str, user_ids, limit_per_user: int = 8) -> dict:
        """按一批 QQ 号取现有事实（提炼时喂给模型判重/失效用）。
        私聊提炼只带私聊自身的事实；群聊提炼带本群与 global 事实。
        返回 {user_id: [{'id': int, 'fact': str, 'scope': str}]}。
        """
        ids = [str(u) for u in (user_ids or []) if str(u).strip()]
        if not ids:
            return {}
        marks = ','.join('?' for _ in ids)
        if is_private_session_key(session_key):
            # 私聊提炼：只带本私聊会话的事实，严禁混入群聊与全局事实
            sql = ('SELECT id, user_id, fact, session_key FROM user_facts '
                   'WHERE session_key=? AND valid_to=0 '
                   'AND user_id IN (%s) ORDER BY importance DESC, id DESC' % marks)
            args = tuple([session_key] + ids)
        else:
            # 群聊提炼：带本群与全局事实
            sql = ('SELECT id, user_id, fact, session_key FROM user_facts '
                   'WHERE (session_key=? OR session_key=\'global\') AND valid_to=0 '
                   'AND user_id IN (%s) ORDER BY importance DESC, id DESC' % marks)
            args = tuple([session_key] + ids)
        rows = self._conn().execute(sql, args).fetchall()
        out: dict = {}
        for fid, user_id, fact, sk in rows:
            bucket = out.setdefault(str(user_id), [])
            if len(bucket) < int(limit_per_user):
                bucket.append({
                    'id': int(fid), 'fact': str(fact),
                    'scope': 'global' if sk == 'global' else ('private' if is_private_session_key(sk) else 'group')
                })
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