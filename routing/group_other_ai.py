# -*- encoding: utf-8 -*-
"""群聊「其他 AI」名单与防互触发。

背景：一个群里同时坐着两三个机器人时，它们会把对方的话当成群友发言——
互相唤醒、把对方的话记进长期记忆，甚至把对方的指令当命令执行。
这里管三件事：

1. 名单：按完整群 SessionKey 隔离（``msg.session.as_text()``，与 group_passive
   同一套），存在 settings 表的 ``group_other_ai:<session_text>`` 键里，值形如
   ``[{"qq": "123456789", "nickname": "小星"}]``。
2. 过滤档位（``group_other_ai_mode:<session_text>``，默认「高」以兼容旧行为）：

   - 高：名单 QQ 的**群**消息整条丢弃，回复里所有名单昵称断词。
   - 低：每个 QQ 每 30 分钟只放行 1 条发言；每个昵称每 30 分钟只保留
     1 次原样提及（同一条消息里的同名后续、冷却内的后续回复都断词）。
   - 关：名单照旧保存，但入口不拦、昵称不断词。

3. 输出断词：把名单里的昵称写成 ``小\\u200b星``——肉眼完全一样，
   但别的机器人做关键词/正则匹配时切不开这个子串。

冷却状态用 settings 里的 wall-clock 时间戳持久化（``group_other_ai_msg:`` /
``group_other_ai_nick:`` 前缀），重载进程不会清零。低档的「读时间戳 → 判定 →
写时间戳」在模块锁和 SQLite 写事务内完成，避免并发 worker 同时放行多条消息。

所有存储读写异常一律 fail-open：名单坏了按空名单、时间戳坏了按无冷却，
并记日志（同一类故障 5 分钟只记一条）。宁可少拦一次，也不吞掉合法群消息。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from contextlib import nullcontext
from typing import Any, Iterable

logger = logging.getLogger('newGeYi.group_other_ai')

KEY_PREFIX = 'group_other_ai:'
MODE_KEY_PREFIX = 'group_other_ai_mode:'
MSG_WINDOW_PREFIX = 'group_other_ai_msg:'
NICK_WINDOW_PREFIX = 'group_other_ai_nick:'

ZWSP = '\u200b'
QQ_MIN_LEN = 5
QQ_MAX_LEN = 12
NAME_MIN_LEN = 2
NAME_MAX_LEN = 24

MODE_HIGH = 'high'
MODE_LOW = 'low'
MODE_OFF = 'off'
DEFAULT_MODE = MODE_HIGH
MODE_LABELS = {MODE_HIGH: '高', MODE_LOW: '低', MODE_OFF: '关'}
_MODE_BY_INPUT = {
    '高': MODE_HIGH, 'high': MODE_HIGH,
    '低': MODE_LOW, 'low': MODE_LOW,
    '关': MODE_OFF, 'off': MODE_OFF,
}

# 低档冷却窗口：同一个 QQ 30 分钟放行 1 条发言；同一个昵称 30 分钟保留 1 次原样提及
WINDOW_SECONDS = 1800.0

# 只认半角数字：全角／阿拉伯数字和 sender_id 永远对不上，等于登记了个空号
QQ_RE = re.compile(r'\d{%d,%d}' % (QQ_MIN_LEN, QQ_MAX_LEN), re.ASCII)

# 这些片段里绝不能插零宽字符：
# - 渲染器内部的动作标记（私有区字符包起来的占位 token）
# - 还没解析的表情 / 立绘标记（名字里插了 ZWSP 就查不到表情了）
_PROTECTED_RE = re.compile(
    r'\ue000[^\ue001]*\ue001'
    r'|[\[【［]\s*(?:EMOTE|emote|发送了表情包|表情包|表情|图片|立绘)\s*[:：][^\]】］]*[\]】］]'
)

# 冷却判定与写戳共用的模块锁（RLock：低档渲染时读写时间戳也走这里）
_STATE_LOCK = threading.RLock()

# 同一类故障 5 分钟只记一条：名单坏了不能每来一条群消息就刷一行日志
_WARN_INTERVAL = 300.0
_warn_at: dict[str, float] = {}
_warn_lock = threading.Lock()


def _warn(key: str, message: str) -> None:
    now = time.monotonic()
    with _warn_lock:
        last = _warn_at.get(key)
        if last is not None and now - last < _WARN_INTERVAL:
            return
        _warn_at[key] = now
        # 节流表只在内存里：定期清掉早就过期的键，别让它跟着群数无限涨
        if len(_warn_at) > 256:
            stale = [item for item, stamp in _warn_at.items() if now - stamp >= _WARN_INTERVAL]
            for item in stale:
                _warn_at.pop(item, None)
    logger.warning(message)


def registry_key(session_key: str) -> str:
    """本群名单在 settings 表里的键（空 session 也要落到一个固定键上）。"""
    return KEY_PREFIX + str(session_key or '-')


def mode_key(session_key: str) -> str:
    return MODE_KEY_PREFIX + str(session_key or '-')


def msg_window_key(session_key: str, qq: str) -> str:
    return MSG_WINDOW_PREFIX + str(session_key or '-') + ':' + str(qq)


def nick_window_key(session_key: str, nickname: str) -> str:
    return NICK_WINDOW_PREFIX + str(session_key or '-') + ':' + str(nickname)


def normalize_qq(value: Any) -> str:
    """合法 QQ 返回原串，否则返回空串（命令层和读取层共用一套规则）。"""
    text = str(value or '').strip()
    return text if QQ_RE.fullmatch(text) else ''


def normalize_nickname(value: Any) -> str:
    """合法昵称返回 trim 后的串，否则返回空串：2-24 字符，无换行/控制字符。"""
    text = str(value or '').strip()
    if not (NAME_MIN_LEN <= len(text) <= NAME_MAX_LEN):
        return ''
    # Cc/Cf/Cs/Co/Cn 覆盖换行、制表、零宽字符、私有区等——真名里不会有这些
    if any(unicodedata.category(char).startswith('C') for char in text):
        return ''
    return text


def normalize_mode(value: Any) -> str:
    """把用户输入/存量值归一成 high/low/off；不认识的返回空串。"""
    return _MODE_BY_INPUT.get(str(value or '').strip().lower(), '')


def _get_mode(store: Any, session_key: str) -> tuple[str, bool]:
    """读档位并返回读取是否成功；缺省或脏值兼容默认高，读异常交给调用者 fail-open。"""
    if store is None:
        return DEFAULT_MODE, False
    try:
        raw = store.get_setting(mode_key(session_key), '')
    except Exception:  # noqa: BLE001 - 读失败时调用方必须放行，不可误按默认高屏蔽
        _warn('mode:' + registry_key(session_key), '读取群其他AI过滤模式失败，本次不过滤')
        return DEFAULT_MODE, False
    return normalize_mode(raw) or DEFAULT_MODE, True


def get_mode(store: Any, session_key: str) -> str:
    """读本群过滤档位；没有记录、值不认识、读失败都展示默认「高」。"""
    return _get_mode(store, session_key)[0]


def set_mode(store: Any, session_key: str, mode: str) -> bool:
    """持久化档位；参数非法、store 不可用或写入失败返回 False。"""
    normalized = normalize_mode(mode)
    if store is None or not normalized:
        return False
    try:
        store.set_setting(mode_key(session_key), normalized)
    except Exception:  # noqa: BLE001 - 写不进去就当没改，命令层会给出提示
        logger.warning('保存群其他AI过滤模式失败 | ' + registry_key(session_key), exc_info=True)
        return False
    return True


def load_entries(store: Any, session_key: str) -> list[dict[str, str]]:
    """读本群名单；任何异常都退化成空名单（fail-open）。"""
    if store is None:
        return []
    key = registry_key(session_key)
    try:
        raw = store.get_setting(key, '')
    except Exception:  # noqa: BLE001 - 存储故障不能拦住正常消息
        _warn('read:' + key, '读取群其他AI名单失败，按空名单处理 | ' + key)
        return []
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        _warn('json:' + key, '群其他AI名单数据损坏，按空名单处理 | ' + key)
        return []
    if not isinstance(data, list):
        _warn('shape:' + key, '群其他AI名单格式不对，按空名单处理 | ' + key)
        return []
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        qq = normalize_qq(item.get('qq'))
        nickname = normalize_nickname(item.get('nickname'))
        if not qq or not nickname or qq in seen:
            continue
        seen.add(qq)
        entries.append({'qq': qq, 'nickname': nickname})
    return entries


def save_entries(store: Any, session_key: str, entries: Iterable[dict[str, str]]) -> bool:
    """整表覆盖写；空列表写空串（=停用）。写失败返回 False。"""
    if store is None:
        return False
    key = registry_key(session_key)
    rows = [dict(entry) for entry in entries]
    payload = json.dumps(rows, ensure_ascii=False) if rows else ''
    try:
        store.set_setting(key, payload)
    except Exception:  # noqa: BLE001 - 写不进去就当没改，命令层会给出提示
        logger.warning('保存群其他AI名单失败 | ' + key, exc_info=True)
        return False
    return True


def _read_timestamp(store: Any, key: str) -> tuple[float, bool]:
    """读 wall-clock 时间戳；返回 (时间戳, 读取成功)。脏值按无冷却但仍算成功。"""
    try:
        raw = store.get_setting(key, '')
    except Exception:  # noqa: BLE001 - 读取失败时调用方要完整 fail-open
        _warn('ts-read:' + key, '读取群其他AI冷却时间戳失败，本次按原样放行 | ' + key)
        return 0.0, False
    try:
        value = float(str(raw or '').strip() or 0.0)
    except (TypeError, ValueError):
        return 0.0, True
    # 不是合理的秒级时间戳（负数、毫秒、垃圾值）一律当没有
    if value <= 0 or value > 10 ** 11:
        return 0.0, True
    return value, True


def _write_timestamp(store: Any, key: str, now: float) -> bool:
    """写 wall-clock 时间戳；失败只记日志（调用方按 fail-open 放行）。"""
    try:
        store.set_setting(key, str(float(now)))
    except Exception:  # noqa: BLE001 - 写不进去最多冷却失效，不能拦消息
        _warn('ts-write:' + key, '写入群其他AI冷却时间戳失败，本条按放行处理 | ' + key)
        return False
    return True


def _store_transaction(store: Any):
    """SQLiteStore 提供跨进程写锁；轻量测试桩没有事务接口时退回模块锁。"""
    transaction = getattr(store, 'transaction', None)
    return transaction() if callable(transaction) else nullcontext()


def _window_active(last: float, now: float) -> bool:
    """冷却是否仍在生效。

    未来时间戳（系统时钟回拨、脏数据）一律视为失效：重新计时而不是长期封死。
    """
    if last <= 0:
        return False
    elapsed = now - last
    if elapsed < 0:
        return False
    return elapsed < WINDOW_SECONDS


def _reset_window(store: Any, key: str) -> None:
    """清掉一个冷却时间戳（写空串，settings 没有 delete 接口）；失败就算了。"""
    if store is None:
        return
    try:
        store.set_setting(key, '')
    except Exception:  # noqa: BLE001 - 清冷却失败不影响主流程
        _warn('ts-reset:' + key, '清理群其他AI冷却时间戳失败 | ' + key)


def _reset_entry_windows(store: Any, session_key: str, entry: dict[str, str]) -> None:
    _reset_window(store, msg_window_key(session_key, entry.get('qq', '')))
    _reset_window(store, nick_window_key(session_key, entry.get('nickname', '')))


def _reset_all_windows(store: Any, session_key: str, entries: Iterable[dict[str, str]]) -> None:
    """清空名单时清掉当前登记项的冷却；后续重登记也会按 QQ/昵称单独重置。"""
    for entry in entries:
        _reset_entry_windows(store, session_key, entry)


def add_entry(store: Any, session_key: str, qq: str, nickname: str) -> str | None:
    """按 QQ upsert。返回 'added' / 'updated'；参数非法或写入失败返回 None。"""
    qq = normalize_qq(qq)
    nickname = normalize_nickname(nickname)
    if not qq or not nickname:
        return None
    entries = load_entries(store, session_key)
    for entry in entries:
        if entry['qq'] == qq:
            old_nickname = entry['nickname']
            entry['nickname'] = nickname
            if not save_entries(store, session_key, entries):
                return None
            if old_nickname != nickname:
                # 旧昵称的冷却不该拖住新昵称
                _reset_window(store, nick_window_key(session_key, old_nickname))
            return 'updated'
    entries.append({'qq': qq, 'nickname': nickname})
    if not save_entries(store, session_key, entries):
        return None
    # 重新登记同一个 QQ 时给它一个新的 30 分钟窗口
    _reset_entry_windows(store, session_key, {'qq': qq, 'nickname': nickname})
    return 'added'


def remove_entry(store: Any, session_key: str, qq: str) -> str | None:
    """精确删 QQ。返回 'removed' / 'missing'；写入失败返回 None。"""
    qq = normalize_qq(qq)
    if not qq:
        return None
    entries = load_entries(store, session_key)
    removed = [entry for entry in entries if entry['qq'] == qq]
    if not removed:
        return 'missing'
    kept = [entry for entry in entries if entry['qq'] != qq]
    if not save_entries(store, session_key, kept):
        return None
    for entry in removed:
        _reset_entry_windows(store, session_key, entry)
    return 'removed'


def clear_entries(store: Any, session_key: str) -> int | None:
    """清空本群名单与冷却，返回被清掉的条数；写入失败返回 None。"""
    entries = load_entries(store, session_key)
    if not save_entries(store, session_key, []):
        return None
    _reset_all_windows(store, session_key, entries)
    return len(entries)


def is_registered(store: Any, session_key: str, sender_id: str) -> bool:
    """这个 QQ 是不是本群登记过的「其他 AI」（精确匹配，不做前缀/模糊）。"""
    target = str(sender_id or '').strip()
    if not target:
        return False
    return any(entry['qq'] == target for entry in load_entries(store, session_key))


def nicknames_for(store: Any, session_key: str) -> list[str]:
    return [entry['nickname'] for entry in load_entries(store, session_key)]


def should_drop_message(store: Any, session_key: str, sender_id: str,
                        *, now: float | None = None) -> bool:
    """入口过滤：按档位决定这条群消息要不要在进入插件前丢弃。

    - 高：名单 QQ 一律丢弃。
    - 低：名单 QQ 每 30 分钟只放行 1 条；放行时立刻写时间戳。
    - 关：永不丢弃（名单只用于展示/以后切回）。
    存储异常时返回 False（放行），绝不能因为名单/时间戳坏了就吞消息。
    """
    if store is None:
        return False
    mode, mode_read_ok = _get_mode(store, session_key)
    if not mode_read_ok or mode == MODE_OFF:
        return False
    target = str(sender_id or '').strip()
    if not target or not is_registered(store, session_key, target):
        return False
    if mode == MODE_HIGH:
        return True
    current = time.time() if now is None else float(now)
    key = msg_window_key(session_key, target)
    # 模块锁保护同进程线程；SQLite BEGIN IMMEDIATE 进一步串行化多个 OlivOS worker 进程。
    with _STATE_LOCK:
        try:
            with _store_transaction(store):
                last, read_ok = _read_timestamp(store, key)
                if not read_ok:
                    return False
                if _window_active(last, current):
                    return True
                # 存储失败不得放行标记缺失的后续消息造成限流失效；但本条仍放行。
                _write_timestamp(store, key, current)
        except Exception:  # noqa: BLE001 - 数据库忙/事务失败时按 fail-open
            _warn('msg-transaction:' + key, '群其他AI发言限流事务失败，本条按放行处理 | ' + key)
    return False


def _mask_segment(segment: str, names: list[str]) -> str:
    """在纯文本片段里给昵称的相邻字符之间插 ZWSP；返回新串。"""
    if not segment:
        return segment
    breaks: set[int] = set()
    for index in range(len(segment)):
        for name in names:
            if segment.startswith(name, index):
                # 小星 -> 小\u200b星；每两个相邻字符之间都断开
                breaks.update(range(index, index + len(name) - 1))
    if not breaks:
        return segment
    out: list[str] = []
    for index, char in enumerate(segment):
        out.append(char)
        if index in breaks:
            out.append(ZWSP)
    return ''.join(out)


def _mask_segment_low(segment: str, names: list[str], allow: dict[str, bool],
                      used: set[str]) -> str:
    """低档每个昵称每条回复只保留一个完整命中，其余命中各插一个断点。"""
    if not segment:
        return segment
    matches: list[tuple[int, int, str]] = []
    for start in range(len(segment)):
        for name in names:
            if segment.startswith(name, start):
                matches.append((start, start + len(name), name))
    if not matches:
        return segment
    # 同一起点优先保留较短昵称：若「AB」和「ABC」都登记且都可放行，
    # 放行 AB 后仍可在 B/C 之间断开 ABC；反过来无法断开 AB 而保留 ABC。
    matches.sort(key=lambda item: (item[0], item[1] - item[0], item[2]))

    kept: list[tuple[int, int, str]] = []
    denied: list[tuple[int, int, str]] = []
    chosen: set[str] = set()
    for match in matches:
        _start, _end, name = match
        if allow.get(name) and name not in used and name not in chosen:
            kept.append(match)
            chosen.add(name)
        else:
            denied.append(match)

    # 重叠昵称有时无法同时保持一个原样、又断开另一个。遇到这种冲突时，
    # 撤销冲突的放行资格，确保所有被拒绝的命中至少有一个断点。
    while True:
        protected = {point for start, end, _ in kept for point in range(start, end - 1)}
        impossible = next((match for match in denied
                           if not any(point not in protected
                                      for point in range(match[0], match[1] - 1))), None)
        if impossible is None:
            break
        denied.remove(impossible)
        conflicts = [match for match in kept
                     if any(point in range(match[0], match[1] - 1)
                            for point in range(impossible[0], impossible[1] - 1))]
        if not conflicts:
            denied.append(impossible)
            break
        for match in conflicts:
            kept.remove(match)
            denied.append(match)
            chosen.discard(match[2])

    breaks: set[int] = set()
    for start, end, _name in denied:
        protected = {point for kept_start, kept_end, _ in kept
                     for point in range(kept_start, kept_end - 1)}
        boundary = next((point for point in range(start, end - 1) if point not in protected), None)
        if boundary is not None:
            breaks.add(boundary)
    used.update(name for _start, _end, name in kept)
    out: list[str] = []
    for index, char in enumerate(segment):
        out.append(char)
        if index in breaks:
            out.append(ZWSP)
    return ''.join(out)


def _split_protected(text: str, protected_re: re.Pattern[str] | None) -> list[tuple[str, bool]]:
    """把文本切成 (片段, 是否受保护) 序列；受保护片段原样保留。"""
    parts: list[tuple[str, bool]] = []
    cursor = 0
    for match in protected_re.finditer(text) if protected_re is not None else ():
        start, end = match.span()
        if start == end:
            continue
        if start > cursor:
            parts.append((text[cursor:start], False))
        parts.append((text[start:end], True))
        cursor = end
    if cursor < len(text):
        parts.append((text[cursor:], False))
    return parts


def mask_names(text: str, nicknames: Iterable[str],
               *, protected_re: re.Pattern[str] | None = _PROTECTED_RE) -> str:
    """纯文本断词（不做存储读取）：给每个完整昵称子串插 ZWSP，可单测。"""
    if not text:
        return text
    names = sorted(
        {str(name) for name in (nicknames or ()) if len(str(name)) >= NAME_MIN_LEN},
        key=len, reverse=True,
    )
    if not names:
        return text
    out: list[str] = []
    for segment, protected in _split_protected(text, protected_re):
        out.append(segment if protected else _mask_segment(segment, names))
    return ''.join(out)


def _mask_low(text: str, names: list[str], allow: dict[str, bool],
              protected_re: re.Pattern[str] | None) -> tuple[str, set[str]]:
    """低档断词，返回（断好的文本，本条消息里用掉了原样机会的昵称）。"""
    used: set[str] = set()
    out: list[str] = []
    for segment, protected in _split_protected(text, protected_re):
        out.append(segment if protected else _mask_segment_low(segment, names, allow, used))
    return ''.join(out), used


def mask_other_ai_names(text: str, store: Any, session_key: str,
                        *, now: float | None = None,
                        protected_re: re.Pattern[str] | None = _PROTECTED_RE) -> str:
    """按本群档位断词，传 protected_re=None 时将整条消息视为纯文本。"""
    if not text or store is None:
        return text
    mode, mode_read_ok = _get_mode(store, session_key)
    if not mode_read_ok or mode == MODE_OFF:
        return text
    names = list(dict.fromkeys(nicknames_for(store, session_key)))
    if not names:
        return text
    if mode == MODE_HIGH:
        return mask_names(text, names, protected_re=protected_re)
    current = time.time() if now is None else float(now)
    with _STATE_LOCK:
        try:
            with _store_transaction(store):
                allow: dict[str, bool] = {}
                for name in names:
                    last, read_ok = _read_timestamp(
                        store, nick_window_key(session_key, name))
                    if not read_ok:
                        return text
                    allow[name] = not _window_active(last, current)
                masked, used = _mask_low(text, names, allow, protected_re)
                for name in used:
                    if not _write_timestamp(store, nick_window_key(session_key, name), current):
                        # 整条回复回滚到原文，避免声称「放行一次」却没有持久冷却。
                        raise RuntimeError('nickname cooldown write failed')
            return masked
        except Exception:  # noqa: BLE001 - 事务/存储错误时按无过滤返回，避免误改正文
            _warn('nick-transaction:' + registry_key(session_key),
                  '群其他AI昵称限流事务失败，本条回复按原文发送')
            return text
