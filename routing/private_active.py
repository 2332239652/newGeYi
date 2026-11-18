# -*- encoding: utf-8 -*-
"""私聊主动搭话：判定逻辑（不发送、不起线程，纯函数式，方便单测）。

只在近期有相关的共同经历或未完成约定时考虑主动开口。连续两次无人回复、
安静时段、额度用尽或有在途事件时暂停主动搭话。
所有状态都写 `settings` 表，键名统一以 `privactive.` 开头，见 `KEY_*` 常量。
"""

import logging
import threading
import time

logger = logging.getLogger('newGeYi.private_active')

# settings 表键名（按会话维度）
KEY_SEEN = 'privactive.seen:'      # 最后一次对方说话的本地秒时间戳
KEY_SEND = 'privactive.send:'      # 最后一次主动搭话的本地秒时间戳
KEY_COLD = 'privactive.cold:'      # 连续未回复次数
KEY_DAY = 'privactive.day:'        # 'YYYYMMDD:n' 当日已发条数
KEY_HOOK = 'privactive.hook:'      # 'hook_id|YYYYMMDD' 最近一次被主动搭话引用的记忆
# 跨会话（每个私聊用户一条）
KEY_BUSY = 'privactive.busy:'      # 对方说「去忙了/先睡了/晚安」的秒时间戳

# 开场模板中的槽位来自当前会话可见的经历、约定或话题。
TEMPLATES = {
    1: '忽然想起「{commitment}」这件事。你现在有空聊聊吗？',
    2: '刚才想起了「{episode}」，想和你再聊聊。',
    3: '说到{topic}，我又想起「{episode}」了。',
    4: '我们约好的「{commitment}」，我还记得。',
    5: '刚才想起「{episode}」，也想和你聊聊{topic}。',
}

# 对方明确表示「去忙了 / 先睡了 / 晚安」时，24 小时内不主动开口
_BUSY_WORDS = ('去忙了', '先睡了', '晚安', '我先走', '下了', '拜拜', '睡了')
# 上一轮收尾偏负向时不开口
_COLD_WORDS = ('别烦', '闭嘴', '滚', '烦死', '不想说', '没空理', '讨厌你')
# 巡检线程最多记多少个私聊会话（避免长期存活把 adapter 事件全钉在内存里）
MAX_SESSIONS = 64


def _today(now: float) -> str:
    return time.strftime('%Y%m%d', time.localtime(now))


def _ts(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _num(config, key, default):
    """读数值配置；缺省、空串、非法值都退回 default（None 不能变成 0）。"""
    raw = config.get(key, default)
    if raw is None or raw == '':
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _clip(text: str, limit: int = 24) -> str:
    """槽位内容截短：模板是一句话，塞不进整段记忆原文。"""
    clean = ' '.join(str(text or '').split())
    return clean[:max(1, limit)].rstrip('，。、；：,. ')


def _as_hook(value):
    """记忆 id 在库里是整数，落到 settings 里是字符串；比较前统一成能比的形态。"""
    text = str(value or '').strip()
    return int(text) if text.isdigit() else text


class PrivateActivePlanner:
    """读出 settings/关系/记忆，回答「现在能不能发、发哪一条」。"""

    def __init__(self, store, config, episodes=None):
        self.store = store
        self.config = config or {}
        self._episodes = episodes

    # ---------------- 配置 ----------------
    def enabled(self) -> bool:
        return bool(self.config.get('private_active_enabled', False))

    def min_silence_hours(self) -> float:
        return _num(self.config, 'private_active_min_silence_hours', 6.0)

    def daily_limit(self) -> int:
        try:
            return max(0, int(_num(self.config, 'private_active_daily_limit', 1)))
        except (TypeError, ValueError):
            return 1

    def quiet_start(self) -> int:
        try:
            return int(_num(self.config, 'private_active_quiet_start', 23))
        except (TypeError, ValueError):
            return 23

    def quiet_end(self) -> int:
        try:
            return int(_num(self.config, 'private_active_quiet_end', 8))
        except (TypeError, ValueError):
            return 8

    # ---------------- settings 读写 ----------------
    def _get(self, key):
        try:
            return self.store.get_setting(key)
        except TypeError:  # 老 fake 只接受一个参数
            try:
                return self.store.get_setting(key, None)
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    def _set(self, key, value) -> None:
        try:
            self.store.set_setting(key, value)
        except Exception:  # noqa: BLE001 - 状态写失败不该打断路由
            logger.exception('私聊主动状态写入失败 %s', key)

    def last_seen(self, session_key) -> float:
        return _ts(self._get(KEY_SEEN + session_key))

    def last_send(self, session_key) -> float:
        return _ts(self._get(KEY_SEND + session_key))

    def cold_count(self, session_key) -> int:
        try:
            return int(_ts(self._get(KEY_COLD + session_key)))
        except (TypeError, ValueError):
            return 0

    def used_today(self, session_key, now: float) -> int:
        raw = str(self._get(KEY_DAY + session_key) or '')
        day, _, count = raw.partition(':')
        if day != _today(now):
            return 0
        try:
            return int(count)
        except (TypeError, ValueError):
            return 0

    def busy_until(self, session_key) -> float:
        return _ts(self._get(KEY_BUSY + session_key))

    def hook_reference(self, session_key, now: float):
        """返回 (hook_id, 是否近 3 天内被主动搭话引用过)。"""
        raw = str(self._get(KEY_HOOK + session_key) or '')
        hook_id, _, day = raw.partition('|')
        if not hook_id or not day:
            return '', False
        try:
            stamp = time.mktime(time.strptime(day, '%Y%m%d'))
        except (TypeError, ValueError, OverflowError, OSError):
            stamp = 0.0
        return _as_hook(hook_id), (now - stamp) < 3 * 86400

    # ---------------- 事件记录 ----------------
    def note_user_message(self, session_key, text, now=None) -> None:
        """对方说话了：刷新活跃时间，并解除连续冷场熔断。"""
        now = time.time() if now is None else now
        self._set(KEY_SEEN + session_key, int(now))
        if self.cold_count(session_key):
            self._set(KEY_COLD + session_key, 0)
        lowered = str(text or '')
        if any(word in lowered for word in _BUSY_WORDS):
            self._set(KEY_BUSY + session_key, int(now))

    def note_send(self, session_key, hook_id, now=None) -> None:
        """真的发出去了：记时间、累计当日条数、累加未回复次数、记被引用的记忆。"""
        now = time.time() if now is None else now
        self._set(KEY_SEND + session_key, int(now))
        self._set(KEY_DAY + session_key, '%s:%d' % (_today(now), self.used_today(session_key, now) + 1))
        self._set(KEY_COLD + session_key, self.cold_count(session_key) + 1)
        if hook_id:
            self._set(KEY_HOOK + session_key, '%s|%s' % (hook_id, _today(now)))

    # ---------------- 判定 ----------------
    def _in_quiet(self, now: float) -> bool:
        hour = int(time.localtime(now).tm_hour)
        start, end = self.quiet_start(), self.quiet_end()
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # 跨零点，例如 23 → 8

    def _memory_hooks(self, session_key, uid, space_id):
        if self._episodes is None:
            return [], None
        try:
            known = self._episodes.list_for_user(session_key, uid, space_id, limit=60)
        except Exception:  # noqa: BLE001
            logger.exception('私聊主动：读取记忆失败')
            return [], None
        commitments = [r for r in known.get('commitments') or [] if r.get('status') in ('open', 'proposed')]
        events = [r for r in known.get('events') or [] if r.get('evidence_type') == 'co_enacted']
        commitments.sort(key=lambda r: (r.get('updated_at') or 0, r['id']), reverse=True)
        events.sort(key=lambda r: (r.get('updated_at') or 0, r['id']), reverse=True)
        return commitments, events

    def decide(self, session_key, uid, space_id, stage, now=None, silence=None, last_user_text='',
               topic='', pending_event='', event_turns=0):
        """返回 `(模板序号, 文案, 记忆 id)` 或 `(None, 否决原因, '')`。原因一律写日志。

        `silence` 是「距上一次对话」的秒数，由调用方在记录本条消息之前算好传进来；
        不传则退回 `_watch_point()` 自己算。
        """
        now = time.time() if now is None else now
        if not self.enabled():
            return None, 'disabled', ''
        if stage is None or int(stage) <= 0:
            return None, 'stage0', ''
        if self.cold_count(session_key) >= 2:
            return None, 'circuit', ''
        if str(pending_event or '') and int(event_turns or 0) > 0:
            return None, 'pending_event', ''
        if self._in_quiet(now):
            return None, 'quiet', ''
        if self.used_today(session_key, now) >= self.daily_limit():
            return None, 'daily_limit', ''
        if silence is None:
            silence = now - self._watch_point(session_key)
        silence = max(0.0, float(silence))
        if silence < self.min_silence_hours() * 3600:
            return None, 'silence', ''
        if self.busy_until(session_key) and now - self.busy_until(session_key) < 86400:
            return None, 'busy', ''
        closing = str(last_user_text or '')
        if any(word in closing for word in _COLD_WORDS):
            return None, 'negative', ''
        commitments, events = self._memory_hooks(session_key, uid, space_id)
        if not commitments and not events:
            return None, 'no_memory', ''
        plan = self._pick_template(now, silence, int(stage), commitments, events, topic)
        if plan is None:
            return None, 'no_memory', ''
        number, text, hook_id = plan
        last_hook, reused = self.hook_reference(session_key, now)
        if reused and last_hook != '' and last_hook == _as_hook(hook_id):
            return None, 'reused', ''
        return number, text, hook_id

    def _watch_point(self, session_key) -> float:
        """静默起点取「对方最后说话」与「最后一次主动搭话」里更晚的那个。"""
        return max(self.last_seen(session_key), self.last_send(session_key))

    def _pick_template(self, now, silence, stage, commitments, events, topic):
        top_commit = commitments[0] if commitments else None
        top_event = events[0] if events else None
        hours = silence / 3600.0
        # 模板 4：高好感阶段 + 挂着重承诺，用留白表达惦记
        if stage >= 4 and top_commit is not None:
            return 4, TEMPLATES[4].format(commitment=_clip(top_commit['content'])), top_commit['id']
        # 模板 5：久未联系（>48h）+ 旧经历 + 当日谈资
        if stage >= 3 and hours > 48 and top_event is not None and topic:
            return 5, TEMPLATES[5].format(episode=_clip(top_event['summary']), topic=_clip(topic, 12)), top_event['id']
        # 模板 3：2~4 阶段，有谈资且能串起一条经历
        if 2 <= stage <= 4 and topic and top_event is not None:
            return 3, TEMPLATES[3].format(topic=_clip(topic, 12), episode=_clip(top_event['summary'])), top_event['id']
        # 模板 1：1~3 阶段、6~24 小时、有未完成约定
        if 1 <= stage <= 3 and 6 <= hours <= 24 and top_commit is not None:
            return 1, TEMPLATES[1].format(commitment=_clip(top_commit['content'])), top_commit['id']
        # 模板 2：其余情况退回「共同经历唤起」
        if top_event is not None:
            return 2, TEMPLATES[2].format(episode=_clip(top_event['summary'])), top_event['id']
        # 只剩约定时用模板 1 兜底，仍要挂得上记忆
        if top_commit is not None:
            return 1, TEMPLATES[1].format(commitment=_clip(top_commit['content'])), top_commit['id']
        return None


def log_veto(session_key: str, reason: str) -> None:
    """每个否决原因一条日志，方便线上复盘「她今天为什么没说话」。

    `silence` 是每轮巡检都会撞上的正常状态，压到 debug，否则一分钟一条刷屏。
    """
    if reason == 'silence':
        logger.debug('私聊主动不发送 | 原因=%s | %s', reason, str(session_key)[-16:])
        return
    logger.info('私聊主动不发送 | 原因=%s | %s', reason, str(session_key)[-16:])


class PrivateActiveService:
    """把 `PrivateActivePlanner` 接到运行时上：记住会话、定时巡检、真的发出去。

    为什么需要定时器：主动搭话的定义就是「对方安静了 6 小时以上」，
    而 `route()` 只会在收到消息时被调用 —— 消息来的时候静默已经结束了。
    所以这里起一个守护线程（与 `geyi-curator` / `geyi-scene` 同类），
    `private_active_enabled` 为假时线程根本不会创建，行为与改动前完全一致。
    """

    def __init__(self, runtime, config=None, episodes=None):
        self.r = runtime
        self.store = getattr(runtime, 'store', None)
        self.config = config if config is not None else getattr(runtime, 'config', {})
        if episodes is None:
            try:
                from ..memory.episodes import EpisodeMemory  # noqa: PLC0415 - 避免循环导入
                episodes = EpisodeMemory(self.store, self.config)
            except Exception:  # noqa: BLE001 - 记忆读不出来时退化为「没有可挂钩记忆」
                episodes = None
        self.planner = PrivateActivePlanner(self.store, self.config, episodes)
        self._messages = {}
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    # ---------------- 生命周期 ----------------
    def enabled(self) -> bool:
        return self.planner.enabled()

    def tick_seconds(self) -> int:
        return max(30, int(_num(self.config, 'private_active_tick_seconds', 60)))

    def start(self) -> None:
        """幂等启动巡检线程；开关关着时什么都不做。"""
        if not self.enabled() or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='geyi-private-active', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 巡检线程不许把插件带崩
                logger.exception('私聊主动巡检异常')
            self._stop.wait(self.tick_seconds())

    # ---------------- 会话记忆 ----------------
    def remember(self, msg) -> None:
        """对方发来私聊时登记：刷新静默计时、解除熔断、留下可复用的 event。

        只留最近 `MAX_SESSIONS` 个私聊会话的 event 引用：这是一个长期存活的守护线程，
        不设上限的话私聊用户一多就把 `IncomingMessage`（连带 adapter 事件）全钉在内存里。
        """
        session_key = msg.session.as_text()
        with self._lock:
            if session_key not in self._messages and len(self._messages) >= MAX_SESSIONS:
                self._messages.pop(next(iter(self._messages)), None)
            self._messages[session_key] = msg
        try:
            self.planner.note_user_message(session_key, msg.text)
        except Exception:  # noqa: BLE001
            logger.exception('私聊主动：记录对方消息失败')

    def _sessions(self):
        with self._lock:
            return list(self._messages.items())

    # ---------------- 巡检 ----------------
    def _stage_of(self, msg):
        relationship = getattr(self.r, 'relationship', None)
        if relationship is None:
            return None
        try:
            return int(relationship.snapshot(str(msg.sender_id)).get('stage'))
        except Exception:  # noqa: BLE001 - 关系表读不出来就当没到门槛
            logger.exception('私聊主动：读取关系阶段失败')
            return None

    def _space_of(self, session_key) -> str:
        try:
            from ..memory.spaces import current_space  # noqa: PLC0415
            return current_space(self.store, session_key)
        except Exception:  # noqa: BLE001
            return 'chat'

    def _pending_event(self, msg):
        relationship = getattr(self.r, 'relationship', None)
        if relationship is None:
            return '', 0
        try:
            snap = relationship.snapshot(str(msg.sender_id))
            return str(snap.get('pending_event') or ''), int(snap.get('event_turns') or 0)
        except Exception:  # noqa: BLE001
            return '', 0

    def _topic_of(self, session_key) -> str:
        """当日谈资：优先关系表里今天已记下的话题，退回 settings 里的旧键。"""
        uid = str(session_key or '').rsplit(':', 1)[-1]
        relationship = getattr(self.r, 'relationship', None)
        if relationship is not None and uid and uid != '-':
            try:
                topic = str(relationship.snapshot(uid).get('topic') or '')
            except Exception:  # noqa: BLE001 - 关系快照读不出来就走旧键
                topic = ''
            if topic:
                return topic
        try:
            return str(self.store.get_setting('topic:' + session_key) or '')
        except Exception:  # noqa: BLE001
            return ''

    def tick(self, now=None):
        """巡检一圈，返回真的发出去的 `[(session_key, 文案)]`。"""
        if not self.enabled():
            return []
        now = time.time() if now is None else now
        sent = []
        for session_key, msg in self._sessions():
            try:
                result = self._tick_one(session_key, msg, now)
            except Exception:  # noqa: BLE001 - 单个会话异常不影响其他人
                logger.exception('私聊主动：会话巡检失败')
                continue
            if result:
                sent.append((session_key, result))
        return sent

    def _tick_one(self, session_key, msg, now):
        stage = self._stage_of(msg)
        pending, turns = self._pending_event(msg)
        number, text, hook_id = self.planner.decide(
            session_key,
            str(msg.sender_id),
            self._space_of(session_key),
            stage,
            now=now,
            last_user_text='',
            topic=self._topic_of(session_key),
            pending_event=pending,
            event_turns=turns,
        )
        if number is None:
            log_veto(session_key, text)
            return ''
        if not self._deliver(msg, text):
            return ''
        self.planner.note_send(session_key, hook_id, now=now)
        logger.info('私聊主动搭话 | 模板=%s | %s | %r', number, session_key[-16:], text[:40])
        return text

    def _deliver(self, msg, text) -> bool:
        # 静默/睡着的时候不该有任何外发——主动搭话是唯一「不请自来」的消息，
        # 它是作息和休息开关唯一漏掉的出口（2026-11-17 补齐）。
        session_key = msg.session.as_text()
        gate = getattr(self.r, 'is_resting', None)
        if callable(gate):
            try:
                if gate(session_key):
                    log_veto(session_key, 'resting')
                    return False
            except Exception:  # noqa: BLE001 - 判不出来就按可以发处理，别把功能卡死
                logger.exception('私聊主动：休息状态检查失败')
        sleep = getattr(self.r, 'sleep', None)
        phase = getattr(sleep, 'phase', None)
        if callable(phase):
            try:
                if str(phase() or '') == 'asleep':
                    log_veto(session_key, 'asleep')
                    return False
            except Exception:  # noqa: BLE001
                logger.exception('私聊主动：作息状态检查失败')
        reply = getattr(self.r, 'reply', None)
        if not callable(reply):
            return False
        try:
            result = reply(msg, text)
        except Exception:  # noqa: BLE001
            logger.exception('私聊主动：发送失败')
            return False
        if result is None:
            return False
        accepted = bool(getattr(result, 'accepted', result))
        if not accepted:
            logger.info('私聊主动发送被拒 | %s', str(getattr(result, 'error', ''))[:40])
            return False
        track = getattr(self.r, '_track_self_sent', None)
        if callable(track):
            try:
                track(msg.session, text, False, space_id=self._space_of(msg.session.as_text()))
            except Exception:  # noqa: BLE001
                logger.exception('私聊主动：记录自发消息失败')
        return True
