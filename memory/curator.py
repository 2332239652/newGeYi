# -*- encoding: utf-8 -*-
"""记忆提炼器：把 L0 原始缓冲成批提炼成 L1 群摘要 / L2 用户事实 / L3 角色自身记忆。

设计要点（均取自调研结论）：
- **一次调用产三层**：抽取不额外增加请求，成本与「批数」成正比而非「消息数」，
  比「每条消息都跑一次抽取」便宜一个数量级。
- **攒批触发**：攒够 N 条，或空闲 T 秒才值得花一次调用。
- **speaker 硬校验**：模型给的 user_id 必须在本批里作为「用户消息的发送者」真实出现过，
  否则直接丢弃。这是防「记忆污染 / 张冠李戴」的最后一道闸——不靠模型自觉。
- **判重不花 LLM**：归一化文本比对，命中已有事实只刷新命中时间（越用越牢）。
- **失败不推进光标**：这一批没提炼成功就留着下轮重试，绝不丢消息。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from ..models import ModelRequest
from ..storage.database import SQLiteStore

logger = logging.getLogger('newGeYi.curator')

CURATOR_SYSTEM = '你是记忆整理助手，只输出 JSON，不要任何解释、不要 Markdown 代码块。'

# ⚠️ 静态规则和动态数据**必须分开**（2026-09-19 改）。
#
# 缓存只命中「从头开始连续相同」的 token 序列。以前整个模板（含这几百 token
# 的规则）都在 user 消息里、后面紧跟着每轮都变的 {known}/{records}，
# 于是 system 那 30 token 之后立刻失配 —— 可缓存前缀只有 30 token，等于没有。
#
# 拆开之后：system = CURATOR_SYSTEM + CURATOR_RULES（约 330 token，恒定），
# user = CURATOR_TAIL（{known} + 聊天记录，每轮都变）。
# 官方实测缓存命中 100%（20/20），中转站 55%（多后端轮询）。
CURATOR_RULES = """下面是一个 QQ 群最近的一段聊天记录（每行开头是「QQ号 昵称」，AI 说的标为 AI）。
请提炼值得长期记住的信息，只输出 JSON：

{{
  "group_summary": "这段群聊发生了什么（1-2 句，只写公共事件）",
  "user_facts": [
    {{"user_id": "QQ号", "fact": "关于这个人的稳定事实", "category": "身份/偏好/习惯/经历", "importance": 0.6}}
  ],
  "self_memories": [
    {{"content": "歌伊自己的经历/承诺/情绪", "kind": "event/promise/feeling", "importance": 0.6}}
  ]
}}

规则（务必遵守）：
1. user_id 必须严格照抄聊天记录里出现过的 QQ 号，绝对不许编造、推测或改写。
2. user_facts 只记「这个人本人的稳定事实」：身份、职业、偏好、习惯、长期经历。
   - 不记：一次性的互动、玩笑、角色扮演内容、情绪化的气话
   - 不记：{assistant}（AI）说的话——那是 AI 自己的事，不属于任何用户
   - 没把握的宁可不记
3. self_memories 只记「{assistant}自己」的经历、答应过的承诺、明显的情绪波动。
4. group_summary 只写公共事件，不要写任何人的私人信息。
5. 没有可记的内容就留空数组，不要硬凑。
6. **已经在「已知事实」里的信息不要再记一遍**——哪怕换了说法。只有出现**新信息**
   （新的身份、新的偏好、情况发生变化）才记；同一类信息只保留最新的一条。"""

# 动态部分：这两块每轮都变，必须排在静态规则之后
CURATOR_TAIL = """{known}
聊天记录：
{records}
"""



def clamp01(value, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象（容忍代码围栏与前后废话）。"""
    raw = str(text or '').strip()
    if not raw:
        return {}
    if '```' in raw:
        for part in raw.split('```'):
            part = part.strip()
            if part.lower().startswith('json'):
                part = part[4:].strip()
            if part.startswith('{'):
                raw = part
                break
    start = raw.find('{')
    end = raw.rfind('}')
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


class MemoryCurator:
    """后台把缓冲消息提炼进四层记忆。"""

    def __init__(self, config: dict, store: SQLiteStore, gateway):
        self.config = config
        self.store = store
        self.gateway = gateway
        self.assistant_name = str(config.get('assistant_name') or '歌伊')
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_stats: dict = {}

    # ==================== 写入缓冲（L0）====================
    def buffer_user(self, msg) -> int:
        """把一条用户消息写进缓冲。

        机器人的话不进记忆：骰子结果、插件播报不是群友的真实表达，
        提炼进库只会污染人格（而且它们本来就不该被「记住」）。
        """
        if getattr(msg, 'sender_is_bot', False):
            return 0
        return self.store.buffer_append(
            msg.session.as_text(), 'user', msg.text,
            sender_id=msg.sender_id, sender_name=msg.sender_name,
        )

    def buffer_assistant(self, session_key: str, content: str) -> int:
        """把歌伊自己刚发的回复写进缓冲——提炼器据此区分「谁说的」。"""
        return self.store.buffer_append(
            session_key, 'assistant', content, sender_name=self.assistant_name,
        )

    # ==================== 光标（提炼进度）====================
    def _cursor_key(self, session_key: str) -> str:
        return f'mem.cursor:{session_key}'

    def cursor(self, session_key: str) -> int:
        try:
            return int(self.store.get_setting(self._cursor_key(session_key)) or 0)
        except (TypeError, ValueError):
            return 0

    def set_cursor(self, session_key: str, value: int) -> None:
        self.store.set_setting(self._cursor_key(session_key), str(int(value)))

    # ---- 连续失败的退避 ----
    def _note_failure(self, session_key: str) -> float:
        """记一次失败并算退避时长。

        失败不推进光标（消息不能丢），所以**必须**退避：线上实测提炼有一半批次失败，
        而每 2 分钟重试一次意味着同一批能被重试上百遍，一天白烧 190 次调用、
        51 分钟 API 时间。退避 1→2→4→…→30 分钟封顶。
        """
        count = int(self.store.get_setting(f'mem.fail:{session_key}') or 0) + 1
        self.store.set_setting(f'mem.fail:{session_key}', str(count))
        wait = min(60.0 * (2 ** (count - 1)), 1800.0)
        self.store.set_setting(f'mem.fail_until:{session_key}', str(time.time() + wait))
        return wait

    def _note_success(self, session_key: str) -> None:
        self.store.set_setting(f'mem.fail:{session_key}', '0')
        self.store.set_setting(f'mem.fail_until:{session_key}', '0')

    def _last_run_at(self, session_key: str) -> float:
        try:
            return float(self.store.get_setting(f'mem.last_run:{session_key}') or 0)
        except (TypeError, ValueError):
            return 0.0

    def _mark_run(self, session_key: str) -> None:
        self.store.set_setting(f'mem.last_run:{session_key}', str(time.time()))

    # ==================== 触发条件 ====================
    def should_run(self, session_key: str) -> bool:
        cfg = self.config
        if not cfg.get('memory_curator_enabled', True):
            return False
        cursor = self.cursor(session_key)
        pending = self.store.buffer_pending(session_key, cursor)
        if pending <= 0:
            return False
        # 同一会话两次提炼之间留最小间隔，避免抖动时反复烧调用
        interval = float(cfg.get('memory_curate_min_interval', 120) or 120)
        if time.time() - self._last_run_at(session_key) < interval:
            return False
        # 上一批失败过就退避，别每 2 分钟拿同一批去撞墙
        until = float(self.store.get_setting(f'mem.fail_until:{session_key}') or 0)
        if until and time.time() < until:
            return False
        # 尊重全局每日调用预算（提炼也算调用）
        try:
            today = self.store.today_stats()
            if today['calls'] >= int(cfg.get('daily_call_budget', 999999) or 999999):
                return False
        except Exception:  # noqa: BLE001 - 读不到统计就不拦
            pass
        if pending >= int(cfg.get('memory_curate_messages', 40) or 40):
            return True
        idle = float(cfg.get('memory_curate_idle_seconds', 900) or 900)
        last_at = self.store.buffer_last_at(session_key, cursor)
        return bool(last_at) and (time.time() - last_at) >= idle

    # ==================== 提炼 ====================
    def _build_records(self, rows: list[dict]) -> tuple[str, set[str]]:
        """拼成给模型看的记录；同时收集「本批真实出现过的用户 QQ 号」用于硬校验。"""
        lines: list[str] = []
        valid_ids: set[str] = set()
        for row in rows:
            content = str(row.get('content') or '').strip()
            if not content:
                continue
            if row.get('role') == 'assistant':
                lines.append(f'[AI {self.assistant_name}] {content}')
                continue
            uid = str(row.get('sender_id') or '').strip()
            name = str(row.get('sender_name') or '用户').strip()
            if uid and uid not in ('-1', 'None'):
                valid_ids.add(uid)
                lines.append(f'[{uid} {name}] {content}')
            else:
                lines.append(f'[未知用户 {name}] {content}')
        return '\n'.join(lines), valid_ids

    def run_once(self, session_key: str) -> dict:
        """提炼一批。返回统计；失败时不推进光标，下轮重试。"""
        stats = {'summary': 0, 'facts': 0, 'self': 0, 'skipped': 0, 'error': ''}
        if not self._lock.acquire(blocking=False):
            stats['error'] = 'busy'
            return stats
        try:
            cursor = self.cursor(session_key)
            limit = int(self.config.get('memory_curate_max_messages', 40) or 40)
            rows = self.store.buffer_fetch(session_key, cursor, limit=limit)
            if not rows:
                return stats
            records, valid_ids = self._build_records(rows)
            if not records.strip():
                self.set_cursor(session_key, rows[-1]['id'])
                return stats

            tail = CURATOR_TAIL.format(
                records=records[:8000],
                known=self._known_facts_block(session_key, valid_ids),
            )
            data = self._request_and_parse(tail, session_key)
            if isinstance(data, str):
                stats['error'] = data
                wait = self._note_failure(session_key)
                logger.warning('记忆提炼 %s 未完成（%s），%.0f 分钟后再试',
                               session_key, data, wait / 60.0)
                return stats
            stats.update(self._store_results(session_key, data, valid_ids, rows[-1]['id']))
            self._note_success(session_key)
            self._last_stats = dict(stats)
            return stats
        except Exception as exc:  # noqa: BLE001 - 提炼永不抛出
            logger.exception('记忆提炼失败')
            stats['error'] = type(exc).__name__
            return stats
        finally:
            self._lock.release()

    def _request_and_parse(self, prompt: str, session_key: str):
        """调模型并要求 JSON。失败时最多再试两次，返回 dict 或错误码。

        两条重试理由（都实测踩过）：
        ① **思考模式把 token 预算吃光** → content 是 null（empty_content）。
           线上实测：提炼输出 token 的第 90 百分位正好顶在 1600 上限上，
           于是**一半的提炼批次失败**，而失败不推进光标 → 同一批每 2 分钟重试一次，
           一天白烧 190 次调用。这里的对策是**同样的提示词、加倍的预算**再来一次
           ——比「改配置」可靠：服务器上已存在的配置值会盖住新的默认值。
        ② 模型偶尔夹带解释或直接不吐 JSON → 把原输出回灌，纠正一次。
        """
        budget = int(self.config.get('memory_curate_max_tokens', 3200) or 3200)
        cap = int(self.config.get('memory_curate_max_tokens_cap', 8000) or 8000)
        model = str(self.config.get('curate_model')
                    or self.config.get('fast_model') or '')
        last_text = ''
        # 静态前缀（system）每轮都一样 → 进缓存；动态数据（tail）放 user。
        # 顺序不能反：缓存只认「从头连续相同」的序列。
        system_prefix = CURATOR_SYSTEM + '\n\n' + CURATOR_RULES.format(
            assistant=self.assistant_name)
        for attempt in (1, 2, 3):
            messages = [
                {'role': 'system', 'content': system_prefix},
                {'role': 'user', 'content': prompt},
            ]
            if last_text:
                messages.append({'role': 'assistant', 'content': last_text[:600]})
                messages.append({
                    'role': 'user',
                    'content': '上面这段不是合法 JSON。请只输出一个 JSON 对象，不要任何解释、不要代码块。',
                })
            req = ModelRequest(
                messages=messages,
                model=model,
                temperature=0.2 if not last_text else 0.0,
                max_tokens=budget,
            )
            result = self.gateway.chat(req, kind='memory_curate', session_key=session_key)
            self._mark_run(session_key)
            if not result.ok:
                code = result.error_code or 'failed'
                if code == 'empty_content' and budget < cap:
                    budget = min(budget * 2, cap)
                    last_text = ''
                    logger.warning('提炼返回空内容（预算不够），提到 %d token 再试', budget)
                    continue
                return code
            last_text = str(result.text or '')
            data = extract_json(last_text)
            if data:
                return data
            logger.warning('第 %d 次提炼未返回合法 JSON：%s',
                           attempt, last_text[:200].replace('\n', ' '))
        return 'bad_json'

    def _known_facts_block(self, session_key: str, valid_ids) -> str:
        """把本批次说话人的现有事实列给模型，让它自己判重。

        为什么不在本地做：线上实测的重复是**换说法**的——「跑团新人」「自称TRPG萌新，
        刚加入群」「跑团纯萌新，想通过跑一局来了解流程」字符重合度极低，字符串相似度
        根本抓不住；而模型一眼就知道这是同一件事。
        代价可控：只带本批出现过的说话人，每人最多几条。
        """
        per_user = int(self.config.get('memory_curate_known_facts', 5) or 0)
        if per_user <= 0:
            return ''
        try:
            known = self.store.list_facts_of_users(session_key, valid_ids, per_user)
        except Exception:  # noqa: BLE001 - 拿不到就不带去重上下文，不影响提炼
            return ''
        lines: list[str] = []
        for user_id, facts in known.items():
            for fact in facts:
                lines.append('  %s：%s' % (user_id, fact))
        if not lines:
            return ''
        # 上限兜一下，别让去重上下文把提示词撑大
        body = '\n'.join(lines[:60])
        return ('\n已知事实（这些**已经记过了**，不要重复记）：\n' + body + '\n')

    def _store_results(self, session_key: str, data: dict, valid_ids: set[str],
                       last_id: int) -> dict:
        stats = {'summary': 0, 'facts': 0, 'self': 0, 'skipped': 0}

        # ---- L1 群级摘要（只有公共事件）----
        summary = str(data.get('group_summary') or '').strip()
        if 0 < len(summary) <= 500:
            self.store.add_group_summary(session_key, summary, covers_to=last_id)
            stats['summary'] = 1
        elif summary:
            logger.warning('群摘要过长已丢弃（%d 字）', len(summary))

        # ---- L2 用户事实（speaker 硬校验）----
        for item in (data.get('user_facts') or [])[:20]:
            if not isinstance(item, dict):
                continue
            uid = str(item.get('user_id') or '').strip()
            fact = str(item.get('fact') or '').strip()
            if not uid or not fact:
                continue
            if uid not in valid_ids:
                # 模型编造的 QQ 号，或不是本批用户 —— 丢弃，绝不入库
                stats['skipped'] += 1
                logger.debug('丢弃来源不明的用户事实：%s / %s', uid, fact[:40])
                continue
            fact = fact[:200]
            existing = self.store.find_user_fact(session_key, uid, fact)
            if existing:
                self.store.touch_user_fact(existing['id'])
                stats['skipped'] += 1
                continue
            self.store.add_user_fact(
                session_key, uid, fact,
                category=str(item.get('category') or '').strip()[:12],
                source='inferred',
                importance=clamp01(item.get('importance'), 0.5),
            )
            stats['facts'] += 1
            # 容量兜底：线上实测一个人能攒到 17 条、5 条在说同一件事。
            # 字面归一化挡不住换说法的重复，所以超量就淘汰分数最低的。
            stats['trimmed'] = stats.get('trimmed', 0) + self.store.trim_user_facts(
                session_key, uid,
                max_per_user=int(self.config.get('memory_max_facts_per_user', 12) or 0),
                max_per_group=0,
            )

        # ---- L3 角色自身记忆 ----
        for item in (data.get('self_memories') or [])[:10]:
            if not isinstance(item, dict):
                continue
            content = str(item.get('content') or '').strip()[:200]
            if not content:
                continue
            if self.store.find_self_memory(session_key, content):
                stats['skipped'] += 1
                continue
            self.store.add_self_memory(
                session_key, content,
                kind=str(item.get('kind') or 'event').strip()[:12],
                importance=clamp01(item.get('importance'), 0.5),
            )
            stats['self'] += 1

        # 这一批成功消费掉了
        self.set_cursor(session_key, last_id)
        self.store.buffer_trim(session_key)
        # 每群总容量兜底（每人的上限在写每条时已经查过）
        stats['trimmed'] = stats.get('trimmed', 0) + self.store.trim_user_facts(
            session_key, '',
            max_per_user=0,
            max_per_group=int(self.config.get('memory_max_facts_per_group', 600) or 0),
        )
        return stats

    # ==================== 后台线程 ====================
    def start(self) -> None:
        if not self.config.get('memory_curator_enabled', True):
            logger.info('自动记忆已关闭，提炼线程不启动')
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='geyi-curator', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _loop(self) -> None:
        tick = max(15, int(self.config.get('memory_curate_tick_seconds', 60) or 60))
        while not self._stop.wait(tick):
            try:
                for session_key in self.store.buffer_sessions():
                    if self._stop.is_set():
                        return
                    if not self.should_run(session_key):
                        continue
                    stats = self.run_once(session_key)
                    if stats.get('summary') or stats.get('facts') or stats.get('self'):
                        logger.info(
                            '记忆提炼 %s：摘要%d 事实%d 自身%d 跳过%d',
                            session_key, stats['summary'], stats['facts'],
                            stats['self'], stats['skipped'],
                        )
                    elif stats.get('error'):
                        logger.warning('记忆提炼 %s 未完成：%s', session_key, stats['error'])
            except Exception:  # noqa: BLE001 - 后台线程永不退出
                logger.exception('记忆提炼循环异常')

    def status_text(self, session_key: str) -> str:
        """给命令用的状态摘要（骰主可查：她到底记住了什么）。"""
        cursor = self.cursor(session_key)
        pending = self.store.buffer_pending(session_key, cursor)
        last = self._last_run_at(session_key)
        when = time.strftime('%m-%d %H:%M', time.localtime(last)) if last else '还没跑过'
        lines = [
            f'自动记忆: {"开" if self.config.get("memory_curator_enabled", True) else "关"}',
            f'待提炼缓冲: {pending} 条 | 上次提炼: {when}',
            f'群摘要 {len(self.store.recent_group_summaries(session_key, limit=50))} 条 | '
            f'用户事实 {self.store.count_user_facts(session_key)} 条 | '
            f'角色记忆 {len(self.store.list_self_memories(session_key, limit=100))} 条',
        ]
        facts = self.store.list_user_facts(session_key, limit=8)
        if facts:
            lines.append('')
            lines.append('最近记住的群友事实:')
            for row in facts:
                lines.append(f'  · [{row["user_id"]}] {row["fact"]}')
        memories = self.store.list_self_memories(session_key, limit=6)
        if memories:
            lines.append('')
            lines.append('歌伊自己的记忆:')
            for row in memories:
                lines.append(f'  · ({row["kind"]}) {row["content"]}')
        summaries = self.store.recent_group_summaries(session_key, limit=2)
        if summaries:
            lines.append('')
            lines.append('最近的群事件:')
            for row in summaries:
                lines.append(f'  · {row["content"]}')
        return '\n'.join(lines)
