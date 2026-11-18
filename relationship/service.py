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

from ..memory.worldbook import WorldbookService, _STOP_BIGRAMS, tokenize
from ..models import IncomingMessage, ModelRequest
from ..runtime import sponsors
from ..storage.database import SQLiteStore, text_similarity
from .judge import build_judge_messages, parse_verdict
from .stages import (EVENTS, FACT_LIMIT_BY_STAGE, FEELINGS, MAX_STAGE, MILESTONES,
                     STAGE_NAMES, STAGE_UP_CUES, STAGE_UP_HINTS, build_relation_block)
from .state import (Outcome, Relation, allowed_advance, apply_delta, apply_presence,
                    complete_event, day_key, is_narration_hijack, is_repeat, is_trivial,
                    params_from_config, set_stage)

logger = logging.getLogger('newGeYi.relationship')

MOOD_HOURS = 6
TOPIC_TURNS_PER_DAY = 2
TOPIC_TOLD_KEEP = 40
# 刚升级后，"关系"指令里的那句关系提示保留多久（之后自然不再出现，不写数据库）。
STAGE_UP_HINT_SECONDS = 24 * 3600


def _merge_notes(*parts) -> str:
    """合并评审 note 与状态机写回的限流 note（去空、去重、按顺序用空格连起来）。

    2026-11 修复：以前 `verdict['note']` 会把 apply_delta 写回的
    diminished / daily_cap / locked 覆盖掉，日志里就看不出"这一轮为什么只加 0.x"。
    """
    out = []
    for part in parts:
        text = ' '.join(str(part or '').split())
        if text and text not in out:
            out.append(text)
    return ' '.join(out)

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


def _topic_mentioned(topic_text: str, reply_text: str) -> bool:
    """判断歌伊本轮的回复是否实际上提起了今日谈资（至少重合 2 个非虚词二元组）。"""
    if not topic_text or not reply_text:
        return False
    ignore = _STOP_BIGRAMS | {'歌伊', '今天', '刚才', '现在', '有点', '一点', '觉得', '怀疑', '正在'}
    t_tokens = {tok for tok in tokenize(topic_text) if len(tok) >= 2 and tok not in ignore}
    if not t_tokens:
        return False
    r_tokens = set(tokenize(reply_text))
    return len(t_tokens & r_tokens) >= 2


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
    def block_for(self, msg: IncomingMessage, consume: bool = True) -> str:
        """生成【此刻你与对方的关系】块。

        `consume=False` 时完全只读：不推进 event_turns、不消费 stage_up_cue、不写谈资。
        主回复链路一律走只读版，等回复真的发出去以后再调 `commit_prompt_effects()` ——
        否则一次重试/失败就会把「刚升级」的演出和事件轮次各吃掉一次（双倍消耗）。
        """
        if not self.active_for(msg):
            return ''
        if not consume:
            return self.preview_block_for(msg)
        try:
            now = time.time()
            with self._lock_for(msg.sender_id), self.store.transaction():
                rel = self.load(msg.sender_id)
                mood = rel.mood if rel.mood_until > now else ''
                event = str(rel.flags.get('pending_event') or '')
                dirty = False
                if event:
                    rel.flags['event_turns'] = int(rel.flags.get('event_turns') or 0) + 1
                    dirty = True
                # 刚升级那一轮只演"关系近了一步"：一次性取出，并把每日谈资顺延到下一轮
                # （两段戏一起灌进去会把人设演碎，见设计文档备注）。
                stage_up = ''
                cue_stage = int(rel.flags.get('stage_up_cue') or 0)
                if 0 < cue_stage <= MAX_STAGE:
                    stage_up = STAGE_UP_CUES[cue_stage]
                    rel.flags.pop('stage_up_cue', None)
                    dirty = True
                if stage_up:
                    topic, changed = '', False
                elif rel.flags.get('topic_spoken'):
                    # 今天这条已经讲过了：即使 date 变了也不再抽（同一天内讲过就是讲过）
                    topic, changed = '', False
                else:
                    topic, changed = self._topic_for(rel, now)
                if dirty or changed:
                    self.save(rel)
            block = build_relation_block(rel.stage, mood, event, topic, stage_up)
            # 上一轮对方有点太亲近（轻微越界）：这一轮让她有点小反应，读一次就消掉
            note = self._take_mild_note(msg, now)
            return (block + '\n' + note).strip() if note else block
        except Exception:  # noqa: BLE001 - 关系块拼不出来就当没有，不能挡住回复
            logger.exception('生成关系块失败')
            return ''

    def preview_block_for(self, msg: IncomingMessage) -> str:
        """只读预览版 block_for：不产生任何数据库写入或状态变更。

        预览也不**抽**当天的谈资：抽签本身会写一条「今日谈资」记录，
        拼一次 prompt 就顺手把当天的谈资定下来，等于用读操作改了状态。
        当天还没抽时这里按同样的规则算出一条候选，但不落库。
        """
        if not self.active_for(msg):
            return ''
        try:
            now = time.time()
            with self._lock_for(msg.sender_id), self.store.transaction():
                rel = self.load(msg.sender_id)
                mood = rel.mood if rel.mood_until > now else ''
                event = str(rel.flags.get('pending_event') or '')
                # 预览不消耗一次性状态：刚升级的 cue 只读展示，等真正说话时才清掉
                stage_up = ''
                cue_stage = int(rel.flags.get('stage_up_cue') or 0)
                if 0 < cue_stage <= MAX_STAGE:
                    stage_up = STAGE_UP_CUES[cue_stage]
                if stage_up:
                    topic = ''
                elif rel.flags.get('topic_day') == day_key(now):
                    # 当天已抽过：走同一套判定（讲过 / 轮次用尽都返回空）
                    topic, _ = self._topic_for(rel, now, readonly=True)
                elif rel.flags.get('topic_spoken'):
                    topic = ''
                else:
                    # 今天还没抽过：只算候选，不写库（真抽签留给 commit_prompt_effects）
                    topic = self._peek_topic(rel, now)
            block = build_relation_block(rel.stage, mood, event, topic, stage_up)
            note = self._peek_mild_note(msg, now)
            return (block + '\n' + note).strip() if note else block
        except Exception:  # noqa: BLE001
            return ''

    def _peek_topic(self, rel: Relation, now: float) -> str:
        """按今天的抽签规则算出谈资，但不落任何状态。"""
        if self.worldbook is None:
            return ''
        day = day_key(now)
        pool = self.worldbook.topics(stage=rel.stage, scope='private')
        told = list(rel.flags.get('told_topics') or [])
        fresh = [t for t in pool if t['id'] not in told]
        if not fresh and pool:
            fresh = list(pool)
        last_unspoken = str(rel.flags.get('last_unspoken_topic') or '')
        if last_unspoken and len(fresh) > 1:
            alt = [t for t in fresh if t['id'] != last_unspoken]
            if alt:
                fresh = alt
        if not fresh:
            return ''
        pick = min(fresh, key=lambda t: hashlib.md5(
            f'{rel.user_id}{day}{t["id"]}'.encode('utf-8')).hexdigest())
        return (pick['content'] or '')[:240]

    def commit_prompt_effects(self, msg: IncomingMessage, now: float | None = None) -> None:
        """回复成功发出后，才真正消费这一轮 prompt 里一次性用掉的关系状态。

        与 `block_for(consume=True)` 等价的那部分写入，但没有"每轮必加"的副作用：
          * 有在途事件 → event_turns += 1（超时兜底与裁判判据都靠它计数）；
          * stage_up_cue 正好是当前阶段 → 消费掉（"刚升级"的演出只演一轮）；
          * 当天还没抽谈资时抽一次（抽签要落库，读操作里不能抽）；
          * 消费一次性的轻度越界提示。
        这样 LLM 报错、重试、并发攒批都只是多算了一次 prompt，不会白吃状态。
        """
        if msg.is_group or not self.enabled() or self.is_exempt(msg.sender_id):
            return
        if not msg.sender_id:
            return
        now = time.time() if now is None else float(now)
        try:
            with self._lock_for(msg.sender_id), self.store.transaction():
                rel = self.load(msg.sender_id)
                changed = False
                cue = int(rel.flags.get('stage_up_cue') or 0)
                # 谈资放进 prompt 算一轮：读操作不计数，这里补记（否则「今天最多提三轮」永不生效）
                topic_shown = bool(
                    not cue and rel.flags.get('topic_text')
                    and not rel.flags.get('topic_spoken')
                    and int(rel.flags.get('topic_n') or 0) < TOPIC_TURNS_PER_DAY)
                # 今天还没抽谈资、而这一轮 prompt 里又没有升级演出 → 现在真的抽一条
                # （抽签要落库，只读的 preview 里只能算候选）
                if not cue and rel.flags.get('topic_day') != day_key(now):
                    self._topic_for(rel, now)
                    changed = True
                    topic_shown = True
                if rel.flags.get('pending_event'):
                    rel.flags['event_turns'] = int(rel.flags.get('event_turns') or 0) + 1
                    changed = True
                if cue == rel.stage:
                    rel.flags.pop('stage_up_cue', None)
                    changed = True
                if topic_shown and not rel.flags.get('topic_spoken'):
                    rel.flags['topic_n'] = int(rel.flags.get('topic_n') or 0) + 1
                    changed = True
                if changed:
                    self.save(rel)
            self._take_mild_note(msg, now)
        except Exception:  # noqa: BLE001 - 记账失败不能挡住已经发出去的回复
            logger.exception('提交关系状态失败')

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

    def _peek_mild_note(self, msg: IncomingMessage, now: float) -> str:
        """只读查看「轻微越界」标记，不清除。"""
        key = _MILD_PREFIX + msg.session.as_text()
        try:
            raw = self.store.get_setting(key)
            stamp = float(raw) if raw else 0.0
        except Exception:  # noqa: BLE001
            return ''
        if stamp <= 0 or now - stamp > MILD_WINDOW_SECONDS:
            return ''
        return MILD_REACTION

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

    def _topic_for(self, rel: Relation, now: float, readonly: bool = False) -> tuple[str, bool]:
        """今天的主动谈资：每天抽一条，最多放进前几轮 prompt。返回 (文本, 是否改了 rel)。

        优化：抽中时只登记当日候选，等歌伊在回复里实际提起了该谈资，才计入 told_topics 并停止重复注入；
        若当天前几轮都在聊别的正事没来得及提，不会白白把这条谈资永久消耗掉。
        """
        if self.worldbook is None:
            return '', False
        day = day_key(now)
        changed = False
        if rel.flags.get('topic_day') != day:
            pool = self.worldbook.topics(stage=rel.stage, scope='private')
            told = list(rel.flags.get('told_topics') or [])
            fresh = [t for t in pool if t['id'] not in told]
            if not fresh and pool:
                told = []                                   # 全讲过了：重新开始轮转
                fresh = list(pool)
            last_unspoken = str(rel.flags.get('last_unspoken_topic') or '')
            if last_unspoken and len(fresh) > 1:
                alt = [t for t in fresh if t['id'] != last_unspoken]
                if alt:
                    fresh = alt
            pick = None
            if fresh:
                pick = min(fresh, key=lambda t: hashlib.md5(
                    f'{rel.user_id}{day}{t["id"]}'.encode('utf-8')).hexdigest())
            text_picked = pick['content'][:240] if pick else ''
            if readonly:
                return text_picked, False
            rel.flags['topic_day'] = day
            rel.flags['topic_n'] = 0
            rel.flags['topic_id'] = pick['id'] if pick else ''
            rel.flags['topic_text'] = text_picked
            rel.flags['topic_spoken'] = False
            rel.flags['told_topics'] = told[-TOPIC_TOLD_KEEP:]
            changed = True
        if rel.flags.get('topic_spoken'):
            return '', changed
        text = str(rel.flags.get('topic_text') or '')
        # 计数只在 commit_prompt_effects 里加：读操作（preview_block_for）不能推进轮次，
        # 否则调试台预览几次就把「今天最多提三轮」用光了。
        if text and int(rel.flags.get('topic_n') or 0) < TOPIC_TURNS_PER_DAY:
            return text, changed
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
    def after_reply(self, msg: IncomingMessage, reply_text: str, *, history=None,
                    now: float | None = None) -> None:
        if not self.active_for(msg):
            return
        if history is None:
            # 兼容旧调用：仅在入队时识别并移除已提交的当前轮，worker 永不读取最新历史。
            history = self.store.recent_turns(msg.session.as_text(), limit=10)
            if (len(history) >= 2 and history[-2].get('role') == 'user'
                    and history[-2].get('content') == msg.text
                    and history[-1].get('role') == 'assistant'
                    and history[-1].get('content') == reply_text):
                history = history[:-2]
        job = {
            'uid': str(msg.sender_id), 'name': str(msg.sender_name or '对方'),
            'session_key': msg.session.as_text(), 'user_text': str(msg.text or ''),
            'reply_text': str(reply_text or ''),
            'history': tuple(dict(turn) for turn in history),
            'now': time.time() if now is None else now,
            'generation': self.store.forget_generation('relationship', str(msg.sender_id)),
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
                            reply_text: str, now: float | None = None, judge=None,
                            generation: int | None = None, history=None) -> dict:
        """处理一轮互动；history 为本轮之前的冻结历史，judge 可注入测试。"""
        now = time.time() if now is None else now
        p = self.params()
        shadow = self.shadow()
        with self._lock_for(uid), self.store.transaction():
            current = self.store.forget_generation('relationship', uid)
            if generation is None:
                generation = current
            if generation != current:
                return {'discarded': True, 'reason': 'forgotten'}
            rel = self.load(uid)
        # 直接调用也只采一次快照；显式 history 的空元组表示确实没有前序消息。
        if history is None:
            history = self.store.recent_turns(session_key, limit=10)
            if (len(history) >= 2 and history[-2].get('role') == 'user'
                    and history[-2].get('content') == user_text
                    and history[-1].get('role') == 'assistant'
                    and history[-1].get('content') == reply_text):
                history = history[:-2]
        history = tuple(dict(turn) for turn in history)
        # 带上时间戳：刷屏判定要有时间窗，否则低频私聊里"三天前说过的话"也会被判复读
        # （2026-11-10 抽查：7 天 30 条 spam 里约 1/4 是跨小时的误判）。
        recent_users = [(t['content'], t.get('created_at') or 0)
                        for t in history if t.get('role') == 'user']
        recent_lines = [('对方：' if t.get('role') == 'user' else '歌伊：') + str(t.get('content'))[:120]
                        for t in history[-6:]]

        # 入队时的代际由 after_reply 传入；直接调用在上面的快照事务中捕获。
        trivial = is_trivial(user_text, int(self.config.get('galgame_judge_min_chars', 4)))
        spam = is_repeat(user_text, recent_users, text_similarity,
                         window_seconds=float(self.config.get('galgame_repeat_window_seconds', 1800) or 0),
                         now=now)
        hijack = is_narration_hijack(user_text)
        verdict = None
        if not spam and not hijack and not trivial:
            verdict = (judge or self._judge)(rel, recent_lines, user_text, reply_text,
                                            str(rel.flags.get('pending_event') or ''))

        with self._lock_for(uid), self.store.transaction():
            if generation != self.store.forget_generation('relationship', uid):
                return {'discarded': True, 'reason': 'forgotten'}
            # 同代际下其他本地写入也可能已发生，应用评审时重载最新状态。
            rel = self.load(uid)
            stage_before, aff_before = rel.stage, rel.affection
            trivial = is_trivial(user_text, int(self.config.get('galgame_judge_min_chars', 4)))
            spam = is_repeat(user_text, recent_users, text_similarity,
                             window_seconds=float(self.config.get('galgame_repeat_window_seconds', 1800) or 0),
                             now=now)
            hijack = is_narration_hijack(user_text)

            pending = str(rel.flags.get('pending_event') or '')
            raw, tag, level, note = 0, 'neutral', 0, ''
            if spam:
                raw, tag, note = -1, 'spam', 'repeat'
            elif hijack:
                raw, tag, note = 0, 'pushy', 'narration'
            elif not trivial:
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
                            # 一次越界最多扣 1 分：以前评审给 -2 就照扣 -2，
                            # 一个玩笑抵掉日上限的 1/6（2026-11-10 抽查）。
                            # lie/rude 这类真恶劣不设上限，该罚就罚。
                            harsh = str(tag) in ('lie', 'rude')
                            raw = min(raw, -1) if harsh else -1
                            tag = 'flirt_early' if level >= 3 else 'pushy'
                            note = ('overstep ' + note).strip()
                        self._mark_mild_overstep(session_key, now)

            total = Outcome(stage_before=stage_before, stage_after=stage_before)
            applied = 0.0
            if shadow:
                if not trivial:
                    out = apply_presence(rel, now, p) if day_key(now) != rel.last_day else None
                    if out is not None:
                        applied += out.applied
                        total.stage_after = rel.stage
            else:
                # 刷屏不再吞掉「今日首次互动」的加成：以前 spam 会连 +1 一起跳过，
                # 等于同一件事罚两次（2026-11-10 抽查：748048217 只说了 1 句就从 2.0 掉到 1.0）。
                if not trivial and day_key(now) != rel.last_day:
                    applied += apply_presence(rel, now, p).applied
                out = apply_delta(rel, raw, tag, now, p)
                applied += out.applied
                # apply_delta 会写回 diminished/daily_cap/locked 等限流 note，不能丢：
                # 和评审 note 合并成一条（评审 note 在前），日志和 relationship_log 都看得到。
                note = _merge_notes(note, out.note)
                total.event_started = out.event_started
                if verdict and verdict.get('mood'):
                    rel.mood, rel.mood_until = verdict['mood'], now + MOOD_HOURS * 3600
                # 羁绊事件：评审确认演完，或者超时自动完成（防止卡死）
                pending = str(rel.flags.get('pending_event') or '')
                if pending:
                    turns_done = int(rel.flags.get('event_turns') or 0)
                    # 超时兜底只在"这一轮是正常互动"时才放行：
                    # 否则刷屏、挑衅、旁白劫持、评审宕机拖到第 6 轮，都会把事件白白送过去。
                    bad_tag = (str(tag) in ('rude', 'lie', 'spam', 'flirt_early', 'pushy',
                                            'overstep', 'judge_fail'))
                    timeout_ok = (turns_done >= p.event_timeout_turns
                                  and not spam and not hijack and not bad_tag)
                    if ((verdict and verdict.get('event_done') and turns_done >= 1)
                            or timeout_ok):
                        ev_out = complete_event(rel, pending, now, p)
                        applied += ev_out.applied
                        note = _merge_notes(note, ev_out.note or f'event_{pending}_done')
                    elif turns_done >= p.event_timeout_turns:
                        note = _merge_notes(note, 'event_timeout_blocked')
                total.stage_after = rel.stage

            # 主动谈资消耗判定：若本轮回复里已经实际提起了今日谈资，标记为已讲并停止后续轮次重复注入；
            # 若当日注入次数已用尽仍未提起，记为 last_unspoken_topic，不污染 told_topics。
            if rel.flags.get('topic_text') and not rel.flags.get('topic_spoken'):
                t_text = str(rel.flags.get('topic_text') or '')
                t_id = str(rel.flags.get('topic_id') or '')
                if _topic_mentioned(t_text, reply_text):
                    rel.flags['topic_spoken'] = True
                    rel.flags['topic_n'] = TOPIC_TURNS_PER_DAY
                    if t_id:
                        told = list(rel.flags.get('told_topics') or [])
                        if t_id not in told:
                            told.append(t_id)
                        rel.flags['told_topics'] = told[-TOPIC_TOLD_KEEP:]
                elif int(rel.flags.get('topic_n') or 0) >= TOPIC_TURNS_PER_DAY and t_id:
                    rel.flags['last_unspoken_topic'] = t_id

            if rel.stage > stage_before:
                # 下一轮注入一次"关系近了一步"的演出提示（block_for 读完即清），
                # 同时让「关系」指令在 24 小时内带上对应的一句关系提示。
                rel.flags['stage_up_cue'] = int(rel.stage)
                rel.flags['stage_up_hint'] = int(rel.stage)
                rel.flags['stage_up_hint_at'] = float(now)
            self.save(rel)
            self._log(uid, delta_raw=raw, delta_applied=applied, tag=tag, level=level,
                      aff_before=aff_before, aff_after=rel.affection,
                      stage_before=stage_before, stage_after=rel.stage, shadow=shadow,
                      note=note, user_text=user_text, reply_text=reply_text)
            logger.debug('关系结算 %s applied=%+.1f tag=%s note=%s', uid, applied, tag, note)
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
        # 待演出的小事件：别让记忆提炼把"还没发生的事"当成已经发生（演完了下一轮才清）。
        pending = str(rel.flags.get('pending_event') or '')
        if pending in EVENTS:
            text += (f'你们之间还有一件还没演完的小事「{EVENTS[pending]["title"]}」：'
                     '写他的印象时，不要把这件还没发生的事写成已经发生。\n')
        return text

    def mark_impression_done(self, session_key: str) -> None:
        uid = self._uid_of_session(session_key)
        if not uid:
            return
        try:
            with self._lock_for(uid), self.store.transaction():
                rel = self.load(uid)
                if rel.flags.get('impression_dirty'):
                    rel.flags['impression_dirty'] = False
                    self.save(rel)
        except Exception:  # noqa: BLE001
            logger.exception('清除印象待重写标记失败')

    # ==================== 指令 / 运维 ====================
    def feeling(self, uid: str) -> str:
        rel = self.load(uid)
        p = self.params()
        cap = float(p.daily_cap[rel.stage])
        # gain_today 是"最后一次互动那一天"的累计，只有 touch_day 才会清零。
        # 隔天但还没说话时它仍是昨天的数字 —— 直接显示会变成「今天 +12/12 已经满啦」，
        # 用户会以为"没说话也涨好感 / 一早就满了"（2026-11-11 群友反馈）。
        fresh = rel.last_day == time.strftime('%Y-%m-%d')
        gain = float(rel.gain_today) if fresh else 0.0
        if not fresh:
            tail = '\n（今天还没聊呢，来说句话吧～）'
        else:
            tail = '\n（今天的好感度 +%d/%d' % (gain, cap)
            if gain >= cap:
                tail += '，已经满啦，明天再来吧'
            tail += '）'
        hint = self._stage_up_hint(rel)
        text = f'「{STAGE_NAMES[rel.stage]}」——{FEELINGS[rel.stage]}'
        if hint:
            text += '\n' + hint
        return text + tail

    @staticmethod
    def _stage_up_hint(rel: Relation) -> str:
        """刚升级的 24 小时内，"关系"指令里多一句关系提示（不改状态，过期自然消失）。"""
        stage = int(rel.flags.get('stage_up_hint') or 0)
        if not 0 < stage <= MAX_STAGE:
            return ''
        stamp = float(rel.flags.get('stage_up_hint_at') or 0)
        if stamp and time.time() - stamp > STAGE_UP_HINT_SECONDS:
            return ''
        return STAGE_UP_HINTS[stage]

    def describe(self, uid: str) -> str:
        rel = self.load(uid)
        p = self.params()
        nxt = rel.stage + 1
        # 同理：别把昨天的 gain_today 当成今天的（隔天还没说话时显示 +0）
        gain = rel.gain_today if rel.last_day == time.strftime('%Y-%m-%d') else 0.0
        lines = [
            f'{uid}：{STAGE_NAMES[rel.stage]}（{rel.stage}）| 好感 {rel.affection:.1f}'
            f'{"/" + str(int(p.thresholds[nxt])) if nxt <= MAX_STAGE else ""} | 有效日 {rel.days_active}'
            f'{"/" + str(p.min_days[nxt]) if nxt <= MAX_STAGE else ""} | 今日 +{gain:.1f}',
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
            'event_turns': int(rel.flags.get('event_turns') or 0),
            # 当日谈资（与 prompt 里注入的那条一致）：私聊主动搭话要挂得上同一件事
            'topic': (str(rel.flags.get('topic_text') or '')
                      if str(rel.flags.get('topic_day') or '') == day_key(time.time()) else ''),
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
        with self._lock_for(uid), self.store.transaction():
            rel = self.load(uid)
            set_stage(rel, stage, time.time(), self.params())
            self.save(rel)
        return f'已把 {uid} 设为「{STAGE_NAMES[rel.stage]}」'

    def reset(self, uid: str) -> None:
        with self._lock_for(uid), self.store.transaction():
            self.store._conn().execute('DELETE FROM user_relationship WHERE user_id=?', (str(uid),))

    def forget(self, uid: str) -> None:
        """"忘了我"：关系数据和流水一起清（和印象历史一致的隐私承诺）。"""
        with self._lock_for(uid), self.store.transaction() as conn:
            self.store._advance_forget('relationship', uid, self.store._buffer_high_watermark())
            conn.execute('DELETE FROM user_relationship WHERE user_id=?', (str(uid),))
            conn.execute('DELETE FROM relationship_log WHERE user_id=?', (str(uid),))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
