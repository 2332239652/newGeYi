# -*- encoding: utf-8 -*-
"""私聊关系服务：把状态机、评审、持久化、prompt 注入粘在一起。

对外只有这些入口（都不会抛异常，出错一律退化为"系统不存在"）：

- `stage_for(msg)`        世界书/记忆用：当前该按哪个阶段放行内容（None = 不设限）
- `block_for(msg)`        PromptBuilder 用：【此刻你与对方的关系】块
- `memory_policy(msg)`    记忆召回用：互通记忆/事实条数的阶段限制
- `after_reply(msg, text)` 回复发出后异步评审并应用（不增加回复延迟）
- `curator_hint / mark_impression_done`  记忆提炼用：印象带阶段视角
- 指令用：`describe / feeling / force_stage / reset / forget / recent_log`

主人（owner_ids ∪ galgame_exempt_ids）豁免：不评分、不注入关系块，世界书按最高阶段放行。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..memory.worldbook import WorldbookService
from ..models import IncomingMessage, ModelRequest
from ..runtime import sponsors
from ..storage.database import SQLiteStore, text_similarity
from .judge import build_judge_messages, parse_verdict
from .stages import (FACT_LIMIT_BY_STAGE, FEELINGS, MAX_STAGE, MILESTONES, STAGE_NAMES,
                     build_relation_block)
from .state import (Outcome, Relation, allowed_advance, apply_delta, apply_presence,
                    complete_event, day_key, is_narration_hijack, is_repeat, is_trivial,
                    params_from_config, set_stage)

logger = logging.getLogger('newGeYi.relationship')

MOOD_HOURS = 6
TOPIC_TURNS_PER_DAY = 2
TOPIC_TOLD_KEEP = 40

# ===== 轻微越界（2026-11-07 新增）=====
# 数据背景：41 条扣分**全部**发生在 stage 0/1（允许 L1），其中 30 条是 L2——
# 也就是「朋友间正常语气」（"你真可爱""给你比心""摸摸歌伊"）。全按越界扣分，
# 等于每个新用户第一句话就踩雷，而且他完全不知道自己踩了（阿P 连踩 17 次）。
# 现在：只超前一点的（gap ≤ galgame_mild_gap）不扣分，改成「轻微反应」——
# 让她下一轮有点被打扰到，但对话不断。
_MILD_PREFIX = 'rel_mild:'
# 反应的有效期：超过这个时间就不"记仇"了（免得隔几小时突然情绪不对）
MILD_WINDOW_SECONDS = 900
MILD_REACTION = (
    '【刚才那句有点太亲近了】\n'
    '对方上一句说得比你们现在的关系更近一点。这一轮你有点被打扰到：'
    '可以脸红、往后退半步、把话岔开，或者装作没听懂带过去。'
    '别生气、别说教、也别把「我们现在只是……」挂在嘴边——轻轻挡一下就好，不记账。'
)


class RelationshipService:
    def __init__(self, config: dict, store: SQLiteStore, gateway=None,
                 worldbook: WorldbookService | None = None, permissions=None):
        self.config = config
        self.store = store
        self.gateway = gateway
        self.worldbook = worldbook
        self.permissions = permissions
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='geyi-affection')
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ==================== 开关 / 身份 ====================
    def enabled(self) -> bool:
        return bool(self.config.get('galgame_enabled', False))

    def shadow(self) -> bool:
        return bool(self.config.get('galgame_shadow', True))

    def params(self):
        return params_from_config(self.config)

    def is_exempt(self, user_id: str) -> bool:
        if not self.config.get('galgame_owner_exempt', True):
            return False
        uid = str(user_id or '')
        if uid in {str(x) for x in (self.config.get('galgame_exempt_ids') or [])}:
            return True
        try:
            if self.permissions is not None and uid in self.permissions._owner_set():
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def active_for(self, msg: IncomingMessage) -> bool:
        """这条消息要不要走关系系统：开着、是私聊、不是机器人、不是主人。"""
        return (self.enabled() and not msg.is_group and not getattr(msg, 'sender_is_bot', False)
                and bool(msg.sender_id) and not self.is_exempt(msg.sender_id))

    def stage_for(self, msg: IncomingMessage) -> int | None:
        """世界书门槛用的阶段。群聊 / 功能关闭 → None（按阶段 0 过滤）；主人 → 最高阶段。"""
        try:
            if msg.is_group:
                return None
            if self.is_exempt(msg.sender_id):
                return MAX_STAGE
            if not self.enabled():
                return None
            return self.load(msg.sender_id).stage
        except Exception:  # noqa: BLE001
            logger.exception('读取关系阶段失败')
            return None

    # ==================== 持久化 ====================
    def _lock_for(self, uid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(str(uid), threading.Lock())

    def load(self, uid: str) -> Relation:
        row = self.store._conn().execute(
            'SELECT affection, stage, stage_since, days_active, last_day, gain_today, tag_counts, '
            'bad_streak, locked_until, last_advance_day, mood, mood_until, flags, updated_at '
            'FROM user_relationship WHERE user_id=?', (str(uid),)).fetchone()
        if not row:
            return Relation(user_id=str(uid))
        keys = ('affection', 'stage', 'stage_since', 'days_active', 'last_day', 'gain_today',
                'tag_counts', 'bad_streak', 'locked_until', 'last_advance_day', 'mood',
                'mood_until', 'flags', 'updated_at')
        return Relation.from_row(uid, dict(zip(keys, row)))

    def save(self, rel: Relation) -> None:
        data = rel.to_row()
        cols = ['user_id'] + list(data.keys())
        marks = ','.join('?' for _ in cols)
        updates = ','.join(f'{k}=excluded.{k}' for k in data)
        self.store._conn().execute(
            f'INSERT INTO user_relationship({",".join(cols)}) VALUES({marks}) '
            f'ON CONFLICT(user_id) DO UPDATE SET {updates}',
            [rel.user_id] + list(data.values()))

    def _log(self, uid: str, *, delta_raw: int, delta_applied: float, tag: str, level: int,
             aff_before: float, aff_after: float, stage_before: int, stage_after: int,
             shadow: bool, note: str, user_text: str, reply_text: str) -> None:
        try:
            self.store._conn().execute(
                'INSERT INTO relationship_log(user_id, ts, delta_raw, delta_applied, tag, '
                'advance_level, aff_before, aff_after, stage_before, stage_after, shadow, note, '
                'user_text, reply_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(uid), time.time(), int(delta_raw), round(float(delta_applied), 3), tag,
                 int(level), round(aff_before, 3), round(aff_after, 3), int(stage_before),
                 int(stage_after), 1 if shadow else 0, str(note)[:120],
                 str(user_text)[:200], str(reply_text)[:200]))
        except Exception:  # noqa: BLE001 - 流水写失败不能影响主流程
            logger.exception('写关系流水失败')

    def recent_log(self, uid: str, limit: int = 8) -> list[dict]:
        rows = self.store._conn().execute(
            'SELECT ts, delta_raw, delta_applied, tag, advance_level, aff_after, stage_after, '
            'shadow, note, id, user_text, reply_text FROM relationship_log '
            'WHERE user_id=? ORDER BY id DESC LIMIT ?',
            (str(uid), int(limit))).fetchall()
        keys = ('ts', 'delta_raw', 'delta_applied', 'tag', 'level', 'aff_after', 'stage_after',
                'shadow', 'note', 'id', 'user_text', 'reply_text')
        return [dict(zip(keys, r)) for r in rows]

    # ==================== prompt 注入 ====================
    def block_for(self, msg: IncomingMessage) -> str:
        if not self.active_for(msg):
            return ''
        try:
            now = time.time()
            with self._lock_for(msg.sender_id):
                rel = self.load(msg.sender_id)
                mood = rel.mood if rel.mood_until > now else ''
                event = str(rel.flags.get('pending_event') or '')
                dirty = False
                if event:
                    rel.flags['event_turns'] = int(rel.flags.get('event_turns') or 0) + 1
                    dirty = True
                topic, changed = self._topic_for(rel, now)
                if dirty or changed:
                    self.save(rel)
            block = build_relation_block(rel.stage, mood, event, topic)
            # 上一轮对方有点太亲近（轻微越界）：这一轮让她有点小反应，读一次就消掉
            note = self._take_mild_note(msg, now)
            return (block + '\n' + note).strip() if note else block
        except Exception:  # noqa: BLE001 - 关系块拼不出来就当没有，不能挡住回复
            logger.exception('生成关系块失败')
            return ''

    def _mark_mild_overstep(self, session_key: str, now: float) -> None:
        """记一笔「对方刚才那句比现在的关系更近一点」，下一轮让她有点小反应。

        为什么要记而不是当场反应：评审是**回复发出之后**才跑的（不增加延迟），
        所以对越界的反应只能体现在下一轮。这也符合真人：被人突然说"喜欢你"，
        你愣一下，下一句才调整过来。
        """
        try:
            self.store.set_setting(_MILD_PREFIX + str(session_key), '%.0f' % now)
        except Exception:  # noqa: BLE001 - 记账失败不能影响评审结果
            logger.debug('记录轻微越界失败', exc_info=True)

    def _take_mild_note(self, msg: IncomingMessage, now: float) -> str:
        """取出并清除「轻微越界」标记；过期的直接丢掉（一次性）。"""
        session_key = msg.session.as_text()
        key = _MILD_PREFIX + session_key
        try:
            raw = self.store.get_setting(key)
        except Exception:  # noqa: BLE001
            return ''
        if not raw:
            return ''
        try:
            self.store.set_setting(key, '')
        except Exception:  # noqa: BLE001
            pass
        try:
            stamp = float(raw)
        except (TypeError, ValueError):
            return ''
        if now - stamp > MILD_WINDOW_SECONDS:
            return ''
        return MILD_REACTION

    def _topic_for(self, rel: Relation, now: float) -> tuple[str, bool]:
        """今天的主动谈资：每天抽一条，最多放进前几轮 prompt。返回 (文本, 是否改了 rel)。"""
        if self.worldbook is None:
            return '', False
        day = day_key(now)
        changed = False
        if rel.flags.get('topic_day') != day:
            pool = self.worldbook.topics(stage=rel.stage, scope='private')
            told = list(rel.flags.get('told_topics') or [])
            fresh = [t for t in pool if t['id'] not in told] or pool
            if fresh and not [t for t in pool if t['id'] not in told]:
                told = []                                   # 全讲过了：重新开始轮转
            pick = None
            if fresh:
                pick = min(fresh, key=lambda t: hashlib.md5(
                    f'{rel.user_id}{day}{t["id"]}'.encode('utf-8')).hexdigest())
            rel.flags['topic_day'] = day
            rel.flags['topic_n'] = 0
            rel.flags['topic_text'] = pick['content'][:240] if pick else ''
            if pick:
                told.append(pick['id'])
                rel.flags['told_topics'] = told[-TOPIC_TOLD_KEEP:]
            changed = True
        text = str(rel.flags.get('topic_text') or '')
        if text and int(rel.flags.get('topic_n') or 0) < TOPIC_TURNS_PER_DAY:
            rel.flags['topic_n'] = int(rel.flags.get('topic_n') or 0) + 1
            return text, True
        return '', changed

    def memory_policy(self, msg: IncomingMessage) -> dict | None:
        """记忆召回的阶段限制：陌生人面前她记不住那么多，也不会冒出群里听来的事。"""
        if not self.active_for(msg):
            return None
        try:
            stage = self.load(msg.sender_id).stage
        except Exception:  # noqa: BLE001
            return None
        min_cross = int(self.config.get('galgame_cross_memory_min_stage', 2))
        # 二档赞助者有一个**下限**（默认 8 条）：陌生阶段只给 2 条时她几乎等于
        # 没有记忆，而这个下限只是「记得住的条目多一点」，不改关系阶段本身。
        limit = FACT_LIMIT_BY_STAGE[stage]
        floor = sponsors.fact_floor(self.config, msg.sender_id)
        if floor > limit:
            limit = floor
        return {
            'stage': stage,
            'cross': stage >= min_cross,
            'max_facts': limit,
            'hedge': stage <= 2,
        }

    # ==================== 回复后评审 ====================
    def after_reply(self, msg: IncomingMessage, reply_text: str) -> None:
        if not self.active_for(msg):
            return
        job = {
            'uid': str(msg.sender_id), 'name': str(msg.sender_name or '对方'),
            'session_key': msg.session.as_text(), 'user_text': str(msg.text or ''),
            'reply_text': str(reply_text or ''),
        }
        try:
            self._pool.submit(self._run_job, job)
        except RuntimeError:                       # 线程池已关闭（退出中）
            pass

    def _run_job(self, job: dict) -> None:
        try:
            self.process_interaction(**job)
        except Exception:  # noqa: BLE001 - 后台线程绝不能带着异常死掉
            logger.exception('关系评审失败 | %s', job.get('uid'))

    def process_interaction(self, uid: str, name: str, session_key: str, user_text: str,
                            reply_text: str, now: float | None = None, judge=None) -> dict:
        """处理一轮互动。`judge` 可注入（测试用），默认调模型。返回摘要 dict。"""
        now = now or time.time()
        p = self.params()
        shadow = self.shadow()
        turns = self.store.recent_turns(session_key, limit=10)
        history = turns[:-2] if len(turns) >= 2 else []
        recent_users = [t['content'] for t in history if t.get('role') == 'user']
        recent_lines = [('对方：' if t.get('role') == 'user' else '歌伊：') + str(t.get('content'))[:120]
                        for t in history[-6:]]

        with self._lock_for(uid):
            rel = self.load(uid)
            stage_before, aff_before = rel.stage, rel.affection
            trivial = is_trivial(user_text, int(self.config.get('galgame_judge_min_chars', 4)))
            spam = is_repeat(user_text, recent_users, text_similarity)
            hijack = is_narration_hijack(user_text)

            pending = str(rel.flags.get('pending_event') or '')
            verdict = None
            raw, tag, level, note = 0, 'neutral', 0, ''
            if spam:
                raw, tag, note = -1, 'spam', 'repeat'
            elif hijack:
                raw, tag, note = 0, 'pushy', 'narration'
            elif not trivial:
                verdict = (judge or self._judge)(rel, recent_lines, user_text, reply_text, pending)
                if verdict is None:
                    tag, note = 'judge_fail', 'judge_fail'
                else:
                    raw, tag, level, note = (verdict['delta'], verdict['tag'],
                                             verdict['advance_level'], verdict['note'])
                    if level > allowed_advance(rel.stage):         # 超前：代码说了算，但分轻重
                        gap = level - allowed_advance(rel.stage)
                        mild_gap = int(self.config.get('galgame_mild_gap', 1) or 0)
                        if gap <= mild_gap:
                            # 只超前一点点（陌生阶段说句"你真可爱""给你比心"）：
                            # 不扣分、也不加分，只记一笔「轻微越界」，下一轮让她有点小反应。
                            # 为什么：2026-11-07 实测 41 条扣分全部发生在 stage 0/1，
                            # 其中 30 条是 L2——也就是"朋友间正常语气"。全按越界扣分，
                            # 等于每个新用户第一句话就踩雷，而他完全不知道自己踩了。
                            raw, tag = 0, 'overstep'
                            note = ('mild_overstep ' + note).strip()
                        else:
                            raw = min(raw, -1)
                            tag = 'flirt_early' if level >= 3 else 'pushy'
                            note = ('overstep ' + note).strip()
                        self._mark_mild_overstep(session_key, now)

            total = Outcome(stage_before=stage_before, stage_after=stage_before)
            applied = 0.0
            if shadow:
                if not (trivial or spam):
                    out = apply_presence(rel, now, p) if day_key(now) != rel.last_day else None
                    if out is not None:
                        applied += out.applied
                        total.stage_after = rel.stage
            else:
                if not (trivial or spam) and day_key(now) != rel.last_day:
                    applied += apply_presence(rel, now, p).applied
                out = apply_delta(rel, raw, tag, now, p)
                applied += out.applied
                total.event_started = out.event_started
                if verdict and verdict.get('mood'):
                    rel.mood, rel.mood_until = verdict['mood'], now + MOOD_HOURS * 3600
                # 羁绊事件：评审确认演完，或者超时自动完成（防止卡死）
                pending = str(rel.flags.get('pending_event') or '')
                if pending:
                    turns_done = int(rel.flags.get('event_turns') or 0)
                    if ((verdict and verdict.get('event_done') and turns_done >= 1)
                            or turns_done >= p.event_timeout_turns):
                        ev_out = complete_event(rel, pending, now, p)
                        applied += ev_out.applied
                        note = (note + f' event_{pending}_done').strip()
                total.stage_after = rel.stage

            self.save(rel)
            self._log(uid, delta_raw=raw, delta_applied=applied, tag=tag, level=level,
                      aff_before=aff_before, aff_after=rel.affection,
                      stage_before=stage_before, stage_after=rel.stage, shadow=shadow,
                      note=note, user_text=user_text, reply_text=reply_text)
            if rel.stage > stage_before:
                self._on_stage_up(session_key, name, rel.stage)
            if rel.stage != stage_before:
                logger.info('关系阶段变化 %s: %s → %s (%.1f)', uid, STAGE_NAMES[stage_before],
                            STAGE_NAMES[rel.stage], rel.affection)
        return {'delta_raw': raw, 'applied': applied, 'tag': tag, 'stage': rel.stage,
                'affection': rel.affection, 'shadow': shadow}

    def _judge(self, rel: Relation, recent_lines: list[str], user_text: str,
               reply_text: str, pending: str):
        if self.gateway is None:
            return None
        messages = build_judge_messages(rel.stage, allowed_advance(rel.stage), recent_lines,
                                        user_text, reply_text, pending)
        req = ModelRequest(
            messages=messages,
            model=str(self.config.get('galgame_judge_model') or self.config.get('fast_model') or ''),
            temperature=0.2,
            max_tokens=int(self.config.get('galgame_judge_max_tokens', 1024)),
        )
        try:
            result = self.gateway.chat(req, kind='affection', session_key='')
        except Exception:  # noqa: BLE001
            logger.exception('评审调用抛异常')
            return None
        if not getattr(result, 'ok', False):
            return None
        return parse_verdict(result.text)

    def _on_stage_up(self, session_key: str, name: str, stage: int) -> None:
        text = MILESTONES[stage].format(name=name) if MILESTONES[stage] else ''
        if not text:
            return
        try:
            self.store.add_self_memory(session_key, text, kind='milestone', importance=0.9)
        except Exception:  # noqa: BLE001
            logger.exception('写里程碑记忆失败')

    # ==================== 记忆提炼联动 ====================
    @staticmethod
    def _uid_of_session(session_key: str) -> str:
        return str(session_key or '').rsplit(':', 1)[-1]

    def curator_hint(self, session_key: str) -> str:
        if not self.enabled():
            return ''
        uid = self._uid_of_session(session_key)
        if not uid or self.is_exempt(uid):
            return ''
        try:
            rel = self.load(uid)
        except Exception:  # noqa: BLE001
            return ''
        text = (f'【关系阶段】你和这位对方（私聊）目前处于「{STAGE_NAMES[rel.stage]}」阶段。'
                '写他的印象时，用歌伊的主观视角，亲疏程度不得超过这个阶段'
                '（陌生/熟人阶段只能写成"还不太了解的人"，不要写出亲近、依赖、喜欢）。\n')
        if rel.flags.get('impression_dirty'):
            text += ('你们的关系刚刚进入新的阶段：即使这批聊天没有新内容，也请为他输出一条'
                     '符合该阶段的完整新印象。\n')
        return text

    def mark_impression_done(self, session_key: str) -> None:
        uid = self._uid_of_session(session_key)
        if not uid:
            return
        try:
            with self._lock_for(uid):
                rel = self.load(uid)
                if rel.flags.get('impression_dirty'):
                    rel.flags['impression_dirty'] = False
                    self.save(rel)
        except Exception:  # noqa: BLE001
            logger.exception('清除印象待重写标记失败')

    # ==================== 指令 / 运维 ====================
    def feeling(self, uid: str) -> str:
        rel = self.load(uid)
        return f'「{STAGE_NAMES[rel.stage]}」——{FEELINGS[rel.stage]}'

    def describe(self, uid: str) -> str:
        rel = self.load(uid)
        p = self.params()
        nxt = rel.stage + 1
        lines = [
            f'{uid}：{STAGE_NAMES[rel.stage]}（{rel.stage}）| 好感 {rel.affection:.1f}'
            f'{"/" + str(int(p.thresholds[nxt])) if nxt <= MAX_STAGE else ""} | 有效日 {rel.days_active}'
            f'{"/" + str(p.min_days[nxt]) if nxt <= MAX_STAGE else ""} | 今日 +{rel.gain_today:.1f}',
        ]
        if rel.locked_until > time.time():
            lines.append('冷却中至 ' + time.strftime('%m-%d %H:%M', time.localtime(rel.locked_until)))
        if rel.flags.get('pending_event'):
            lines.append(f'待演出事件：{rel.flags["pending_event"]}（已注入 {rel.flags.get("event_turns", 0)} 轮）')
        for row in self.recent_log(uid, 5):
            lines.append('  %s %+d→%+.1f %s L%d%s %s' % (
                time.strftime('%m-%d %H:%M', time.localtime(row['ts'])), row['delta_raw'],
                row['delta_applied'], row['tag'], row['level'],
                ' [影子]' if row['shadow'] else '', row['note'] or ''))
        return '\n'.join(lines)

    # ---------- 面板用（只读快照，不带任何会改状态的副作用）----------
    def snapshot(self, uid: str, log_limit: int = 0) -> dict:
        rel = self.load(uid)
        p = self.params()
        nxt = rel.stage + 1
        data = {
            'user_id': str(uid), 'stage': rel.stage, 'stage_name': STAGE_NAMES[rel.stage],
            'affection': round(rel.affection, 1),
            'next_threshold': float(p.thresholds[nxt]) if nxt <= MAX_STAGE else None,
            'days_active': rel.days_active,
            'next_min_days': int(p.min_days[nxt]) if nxt <= MAX_STAGE else None,
            'gain_today': round(rel.gain_today, 1), 'daily_cap': float(p.daily_cap[rel.stage]),
            'bad_streak': rel.bad_streak,
            'locked_until': rel.locked_until if rel.locked_until > time.time() else 0,
            'pending_event': str(rel.flags.get('pending_event') or ''),
            'events_done': sorted(k for k, v in rel.flags.items()
                                  if k.startswith('event_') and k.endswith('_done') and v),
            'mood': rel.mood if rel.mood_until > time.time() else '',
            'impression_dirty': bool(rel.flags.get('impression_dirty')),
            'exempt': self.is_exempt(uid), 'updated_at': rel.updated_at,
        }
        if log_limit:
            data['log'] = self.recent_log(uid, log_limit)
        return data

    def list_relations(self, limit: int = 50, offset: int = 0) -> dict:
        conn = self.store._conn()
        total = conn.execute('SELECT COUNT(*) FROM user_relationship').fetchone()[0]
        rows = conn.execute(
            'SELECT user_id FROM user_relationship ORDER BY updated_at DESC, user_id LIMIT ? OFFSET ?',
            (int(limit), int(offset))).fetchall()
        counts = {r[0]: r[1] for r in conn.execute(
            'SELECT stage, COUNT(*) FROM user_relationship GROUP BY stage').fetchall()}
        return {'items': [self.snapshot(r[0]) for r in rows], 'total': int(total),
                'stage_counts': {STAGE_NAMES[s]: counts.get(s, 0) for s in range(MAX_STAGE + 1)}}

    def log_stats(self, since: float) -> dict:
        """影子模式观察用：某时间点以来评审的整体分布（条数、各标签、平均 delta、被代码改判的比例）。"""
        conn = self.store._conn()
        rows = conn.execute(
            'SELECT tag, delta_raw, shadow, note FROM relationship_log WHERE ts>=?', (float(since),)).fetchall()
        tags: dict[str, int] = {}
        for tag, _delta, _shadow, _note in rows:
            tags[tag] = tags.get(tag, 0) + 1
        n = len(rows)
        return {
            'count': n, 'tags': tags,
            'avg_delta': round(sum(r[1] for r in rows) / n, 2) if n else 0.0,
            'shadow_count': sum(1 for r in rows if r[2]),
            'overruled': sum(1 for r in rows if 'overstep' in (r[3] or '')),
            'judge_fail': tags.get('judge_fail', 0),
        }

    def force_stage(self, uid: str, stage: int) -> str:
        with self._lock_for(uid):
            rel = self.load(uid)
            set_stage(rel, stage, time.time(), self.params())
            self.save(rel)
        return f'已把 {uid} 设为「{STAGE_NAMES[rel.stage]}」'

    def reset(self, uid: str) -> None:
        with self._lock_for(uid):
            self.store._conn().execute('DELETE FROM user_relationship WHERE user_id=?', (str(uid),))

    def forget(self, uid: str) -> None:
        """"忘了我"：关系数据和流水一起清（和印象历史一致的隐私承诺）。"""
        with self._lock_for(uid):
            conn = self.store._conn()
            conn.execute('DELETE FROM user_relationship WHERE user_id=?', (str(uid),))
            conn.execute('DELETE FROM relationship_log WHERE user_id=?', (str(uid),))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
