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
import math
import re
import threading
import time

from ..models import ModelRequest
from ..runtime import sponsors
from ..storage.database import SQLiteStore

logger = logging.getLogger('newGeYi.curator')

# ==================== 视频/链接降噪 ====================
# 为什么单独做：QQ 的 B 站分享卡片进到消息文本里只剩一串 BV 号（"歌伊看看BV1cPuAzDEZP"）。
# 提炼模型看到编号往往会老实记一条「对方分享了B站视频（BV1xx）」——零信息量，
# 还因为 bili 插件写的那条是 explicit（免淘汰）而越积越多。
# 处理原则：编号换成「（分享了视频）」这种可读占位；整条只有链接的更直接不记。
_BV_RE = re.compile(r'(?<![0-9A-Za-z])BV[0-9A-Za-z]{8,12}')
_VIDEO_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:bilibili\.com|b23\.tv|bili2233\.cn)/[^\s，,。）)]*', re.I)
_OP_CODE_RE = re.compile(r'\[(?:OP|CQ):[^\]]*\]')

# 只剩"看看/听听/这个"这类指令词时，说明这条消息除了分享没别的内容
_STUB_WORDS = ('看看这个', '看一下', '看看嘛', '看看', '看下', '康康', '听听',
               '这条', '这个', '视频', '分享', '快看', '歌伊', '你')


def clean_video_noise(text) -> tuple[str, bool]:
    """去视频编号/链接。返回 (清理后的文本, 是否整条只是分享)。

    第二个返回值为 True 时调用方应当**整条丢弃**：除了一个链接它什么信息都没有。
    """
    raw = str(text or '')
    if not raw:
        return '', False
    stripped = _OP_CODE_RE.sub(' ', raw)
    stripped = _VIDEO_URL_RE.sub(' ', stripped)
    stripped = _BV_RE.sub(' ', stripped)
    if stripped == raw:                     # 没有视频/链接，原样返回
        return raw, False

    stub = re.sub(r'\s+', '', stripped).strip('，,。.!！?？~～:：、')
    for word in _STUB_WORDS:
        stub = stub.replace(word, '')
    if len(stub) <= 1:                      # 去掉编号后只剩指令词
        return '', True
    cleaned = re.sub(r'[ \t]{2,}', ' ', stripped).strip()
    return (cleaned + '（分享了视频）'), False

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
    {{"user_id": "QQ号", "fact": "关于这个人的稳定事实", "category": "身份/偏好/习惯/经历", "importance": 0.6, "scope": "global/group", "keywords": "检索关键词，逗号分隔，3-6个"}}
  ],
  "expired_fact_ids": [被新事实替代或已失效的已知事实ID数字列表],
  "user_impressions": [
    {{"user_id": "QQ号", "summary": "该群友的个性特质与互动印象（1句话，无明显特质则留空）"}}
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
   - scope：通用个人特质/偏好填 "global"，本群特有梗/职务填 "group"
   - keywords：给 3-6 个**将来能用来搜到这条事实**的词，逗号分隔：
     · 同义词和上位词都要给（事实写「跑团」，补「TRPG,桌游」；写「原神」，补「手游,米哈游」）；
     · 带上具体名词（游戏名、作品名、地点、宠物名、职业）；
     · 不要放"喜欢/觉得/今天"这种虚词，也不要抄整句原文。
   - 没把握的宁可不记
3. expired_fact_ids：如果已知事实里某条信息已被本批次的新事实推翻或替代（例如「准备考研」变成「考研已上岸」），请在 user_facts 写入新事实，并在 expired_fact_ids 列出被替代的已知事实 ID。没有失效的不要填。
4. user_impressions：只有在**现有印象**的基础上确实有新的认识时才更新，写 1 句高维印象。
   - 会整句覆盖旧印象，所以要写**完整的说法**，不要把原来写对的内容丢掉；
   - 没有值得改的就**不要输出这个人**（不输出 = 保留原样），不要为了凑数重写一遍；
   - 第一次见到这个人时，才凭这一批聊天新建。
5. self_memories 只记「{assistant}自己」的经历、答应过的承诺、明显的情绪波动。不记：关于年龄、称呼的口舌争辩或群友的调侃打趣。
   也不要用「软件/程序/数据」的视角写她：不要出现「接入模型」「部署在服务器」「功能上线」「测试数据」「代码 / bug / 接口」这类措辞，
   更不要写成「我是数据」「我是 AI」「靠电活着」。主人给她改东西，就用她的说法记成「主人在她的圣堂里添了东西、给她调了法器」。
6. group_summary 只写公共事件，不要写任何人的私人信息。
7. 没有可记的内容就留空数组，不要硬凑。
8. **已经在「已知事实」里的有效信息不要再重复记一遍**。
9. 视频/链接/图片这类分享：**不要记录编号、BV 号、URL、也不要记「分享了视频」这个动作本身**。
   要记的是「他喜欢看什么」——比如「偏好《怪物猎人》开荒实况」「关注 AI 视频生成」。
   一条消息除了分享什么都没说（例如只发了个链接），就当它没发生，不要产出任何事实。
   同理，「对方发了张图」「对方拍了张照」也不记。"""

# 动态部分：这两块每轮都变，必须排在静态规则之后
CURATOR_TAIL = """{known}{impressions}
聊天记录：
{records}
"""



def _is_private_session(session_key: str) -> bool:
    """会话键里这一段是不是 private。

    ⚠️ 真实格式是**冒号**分隔：`bot_hash:platform:sdk:private:host:target`
    （`models.SessionKey.as_text()`）。以前这里写的是 `'|private|'`，于是判断**永远为假**
    —— `memory_private_enabled=false` 时本该「丢弃私聊缓冲、不提炼」，实际上既没丢、
    也照炼（2026-11-04 复查发现，测试用的是竖线格式才没暴露）。这里两种都认，兼容老数据。
    """
    key = str(session_key or '')
    return ':private:' in key or '|private|' in key or key.endswith('|private')


def clamp01(value, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象（容忍代码围栏与前后废话）。

    **解析失败返回 None，合法空对象返回 `{}`**——两者必须能分开：
    以前统一返回 `{}`，于是「模型老老实实答了个空 JSON（这批没什么可记的）」
    和「模型压根没吐 JSON」走同一条失败路径，光标不推进 → 同一批无限重试
    （2026-11-03 审查发现）。
    """
    raw = str(text or '').strip()
    if not raw:
        return None
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
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def validate_curator_data(data: dict) -> None:
    """完整校验已知 schema 后才允许写库；缺省字段兼容合法空对象。"""
    if not isinstance(data, dict):
        raise ValueError('curator schema: expected object')
    if 'group_summary' in data and not isinstance(data['group_summary'], str):
        raise ValueError('curator schema: group_summary must be string')
    fields = {
        'user_facts': ('user_id', 'fact', 'category', 'scope'),
        'user_impressions': ('user_id', 'summary'),
        'self_memories': ('content', 'kind'),
    }
    required = {'user_facts': ('user_id', 'fact'),
                'user_impressions': ('user_id', 'summary'), 'self_memories': ('content',)}
    for field, strings in fields.items():
        items = data.get(field, [])
        if not isinstance(items, list):
            raise ValueError(f'curator schema: {field} must be array')
        # 连同超过写入条数上限的尾项一起验证，避免半批合法掩盖坏输出。
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f'curator schema: {field} item must be object')
            for key in strings:
                if (key in item or key in required[field]) and not isinstance(item.get(key), str):
                    raise ValueError(f'curator schema: {field}.{key} must be string')
            if 'importance' in item:
                value = item['importance']
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not 0 <= value <= 1 or not math.isfinite(value)):
                    raise ValueError(f'curator schema: {field}.importance must be finite number in [0,1]')
            if 'keywords' in item:
                value = item['keywords']
                if not (isinstance(value, str) or
                        isinstance(value, list) and all(isinstance(word, str) for word in value)):
                    raise ValueError(f'curator schema: {field}.keywords must be string or string array')
    expired = data.get('expired_fact_ids', [])
    if not isinstance(expired, list) or any(type(fid) is not int or fid <= 0 for fid in expired):
        raise ValueError('curator schema: expired_fact_ids must be positive integer array')


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
        # 私聊关系系统（galgame）：由 Runtime 挂上；私聊印象据此带"关系阶段"视角
        self.relationship = None

    # ==================== 写入缓冲（L0）====================
    def buffer_user(self, msg) -> int:
        """把一条用户消息写进缓冲。

        机器人的话不进记忆：骰子结果、插件播报不是群友的真实表达，
        提炼进库只会污染人格（而且它们本来就不该被「记住」）。

        视频/链接先降噪：QQ 分享卡片到这里只剩一串 BV 号，提炼出来就是
        「对方分享了视频 BV1xx」这种废物事实（还因为 explicit 免淘汰越积越多）。
        """
        if getattr(msg, 'sender_is_bot', False):
            return 0
        text, pure_share = clean_video_noise(msg.text)
        if pure_share:
            logger.debug('整条都是视频/链接分享，不进记忆缓冲 | %s', msg.sender_id)
            return 0
        return self.store.buffer_append(
            msg.session.as_text(), 'user', text,
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

    def _drop_backlog(self, session_key: str) -> None:
        """把不该进记忆的会话缓冲整段丢掉（游标推到末尾即可，行留给自己的过期清理）。"""
        try:
            rows = self.store.buffer_fetch(session_key, self.cursor(session_key), limit=500)
            if rows:
                self.set_cursor(session_key, rows[-1]['id'])
                logger.info('私聊内容不进记忆，已丢弃 %s 的 %d 条缓冲',
                            session_key[-14:], len(rows))
        except Exception:  # noqa: BLE001 - 清理失败不该影响后台线程
            logger.exception('丢弃私聊缓冲失败')

    # ==================== 触发条件 ====================
    def should_run(self, session_key: str) -> bool:
        cfg = self.config
        if not cfg.get('memory_curator_enabled', True):
            return False
        if not cfg.get('memory_enabled', True):
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
            # 历史缓冲里可能还有没降噪过的视频分享（带 BV 号），这里再兜一层
            content, pure_share = clean_video_noise(content)
            if pure_share or not content:
                continue
            uid = str(row.get('sender_id') or '').strip()
            name = str(row.get('sender_name') or '用户').strip()
            if uid and uid not in ('-1', 'None'):
                valid_ids.add(uid)
                lines.append(f'[{uid} {name}] {content}')
            else:
                lines.append(f'[未知用户 {name}] {content}')
        return '\n'.join(lines), valid_ids

    def _fragment_state(self, session_key: str) -> tuple[int, int]:
        raw = self.store.get_setting(f'mem.fragment:{session_key}', '[0,0]')
        try:
            row_id, offset = json.loads(raw)
            if type(row_id) is int and type(offset) is int and row_id >= 0 and offset >= 0:
                return row_id, offset
        except (ValueError, TypeError):
            pass
        return 0, 0

    def _select_records(self, fetched: list[dict], rows: list[dict], cursor: int,
                        fragment: tuple[int, int]) -> tuple[str, list[dict], int, tuple[int, int]]:
        """普通记录完整装入8000字符预算；超长首条逐片提交，原文留在L0。"""
        budget = 8000
        allowed = {row['id'] for row in rows}
        lines, selected = [], []
        last_id, used = cursor, 0
        for row in fetched:
            if row['id'] not in allowed:
                last_id = row['id']       # 遗忘水位明确排除的原文
                continue
            # 分片偏移基于稳定渲染；assistant_name可在重启时改变，别让可变前缀
            # 挪动已持久化的字符位置。用户行使用L0里保存的昵称，天然稳定。
            if row.get('role') == 'assistant':
                content = str(row.get('content') or '').strip()
                line = f'[AI] {content}' if content else ''
            else:
                line, _ = self._build_records([row])
            if not line.strip():
                last_id = row['id']       # 空白/纯分享由既有降噪策略明确排除
                continue
            offset = fragment[1] if fragment[0] == row['id'] else 0
            if offset or len(line) > budget:
                if lines:
                    break
                # 分片只切渲染后的记录；每片注明来源/范围，续片不被当成完整句子。
                label = (f'[记录ID={row["id"]} 发送者={str(row.get("sender_id") or "AI")[:80]} '
                         f'续片起点={offset} 总字符={len(line)}；仅按本片明确内容提炼]\n')
                end = min(len(line), offset + budget - len(label))
                if offset >= len(line):
                    raise ValueError('curator fragment offset exceeds record')
                selected.append(row)
                text = label + line[offset:end]
                if end == len(line):
                    return text, selected, row['id'], (0, 0)
                return text, selected, last_id, (row['id'], end)
            cost = len(line) + (1 if lines else 0)
            if used + cost > budget:
                break
            lines.append(line)
            selected.append(row)
            used += cost
            last_id = row['id']
        return '\n'.join(lines), selected, last_id, (0, 0)

    def run_once(self, session_key: str) -> dict:
        """提炼一批。返回统计；失败时不推进光标，下轮重试。"""
        stats = {'summary': 0, 'facts': 0, 'self': 0, 'skipped': 0, 'error': ''}
        if not self._lock.acquire(blocking=False):
            stats['error'] = 'busy'
            return stats
        try:
            # 水位、版本、原文在同一短事务中快照；网络调用期间不占数据库锁。
            with self.store.transaction() as conn:
                epoch = self.store.forget_epoch()
                cursor = self.cursor(session_key)
                limit = int(self.config.get('memory_curate_max_messages', 40) or 40)
                fetched = self.store.buffer_fetch(session_key, cursor, limit=limit)
                local_high = conn.execute(
                    'SELECT COALESCE(MAX(buffer_id),0) FROM forget_state WHERE scope=?',
                    (session_key,)).fetchone()[0]
                rows = [row for row in fetched if (
                    row['id'] > (local_high if row['role'] == 'assistant' else
                                 self.store.forget_watermark(session_key, row['sender_id'])))]
                fragment = self._fragment_state(session_key)
                records, selected, last_id, next_fragment = self._select_records(
                    fetched, rows, cursor, fragment)
                source_ids = {}
                for row in selected:
                    if row['role'] == 'user' and row['sender_id']:
                        uid = str(row['sender_id'])
                        source_ids[uid] = min(source_ids.get(uid, row['id']), row['id'])
                _, valid_ids = self._build_records(selected)
            if not fetched:
                return stats
            if not records.strip():
                with self.store.transaction():
                    if (self.store.forget_epoch() == epoch and self.cursor(session_key) == cursor
                            and self._fragment_state(session_key) == fragment):
                        self.set_cursor(session_key, last_id)
                        self.store.set_setting(f'mem.fragment:{session_key}', '[0,0]')
                return stats

            is_priv = _is_private_session(session_key)
            cur_scope = 'private' if is_priv else 'group'
            known_text, allowed_fact_ids = self._known_facts_block(session_key, valid_ids)
            impression_text = self._existing_impressions_block(valid_ids, scope=cur_scope)
            if impression_text:
                # 留一行可核对的现场证据：提炼时到底带了几张旧卡片进去
                logger.info('提炼带上 %d 张现有印象 | %s',
                            impression_text.count('\n  - '), session_key[-16:])
            if is_priv and self.relationship is not None:
                try:
                    impression_text = self.relationship.curator_hint(session_key) + impression_text
                except Exception:  # noqa: BLE001 - 取不到阶段就按老样子提炼
                    logger.exception('取关系阶段失败')
            tail = CURATOR_TAIL.format(
                records=records,
                known=known_text,
                impressions=impression_text,
            )
            data = self._request_and_parse(tail, session_key)
            if isinstance(data, str):
                stats['error'] = data
                wait = self._note_failure(session_key)
                logger.warning('记忆提炼 %s 未完成（%s），%.0f 分钟后再试',
                               session_key, data, wait / 60.0)
                return stats
            try:
                validate_curator_data(data)
            except ValueError:
                stats['error'] = 'bad_schema'
                self._note_failure(session_key)
                logger.warning('记忆提炼输出schema无效 | %s', session_key)
                return stats
            # BEGIN IMMEDIATE 将版本/进度校验与整批写回串行化。
            with self.store.transaction():
                if self.store.forget_epoch() != epoch:
                    stats['error'] = 'forgotten'
                    return stats
                if (self.cursor(session_key) != cursor or self._fragment_state(session_key) != fragment):
                    stats['error'] = 'stale_batch'
                    return stats
                stored = self._store_results(session_key, data, valid_ids, last_id, allowed_fact_ids,
                                             source_ids=source_ids)
                self.store.set_setting(f'mem.fragment:{session_key}', json.dumps(next_fragment))
                self._note_success(session_key)
            stats.update(stored)
            if is_priv and self.relationship is not None:
                self.relationship.mark_impression_done(session_key)
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
            if data is not None:
                return data
            logger.warning('第 %d 次提炼未返回合法 JSON：%s',
                           attempt, last_text[:200].replace('\n', ' '))
        return 'bad_json'

    def _existing_impressions_block(self, valid_ids, scope: str = 'group') -> str:
        """把本批说话人**现有的印象**带给模型，让它「保留 / 补充 / 改写」而不是每次盲写。
        按 scope 区分群聊印象与私聊印象，独立提炼互不覆盖。
        """
        if not self.config.get('memory_impression_enabled', True) or not valid_ids:
            return ''
        lines: list[str] = []
        for uid in list(valid_ids)[:30]:
            try:
                row = self.store.get_user_impression(str(uid), scope=scope)
            except Exception:  # noqa: BLE001 - 读不到就当这个人没有旧印象
                continue
            summary = str((row or {}).get('summary') or '').strip()
            if summary:
                lines.append(f'  - [{uid}] {summary[:200]}')
        if not lines:
            return ''
        scope_name = '私聊' if scope == 'private' else '群聊'
        return (f'这些人现有的{scope_name}印象（要更新就在 user_impressions 里写**完整的新句子**，'
                '会整句覆盖；没有新认识就不要输出他）：\n' + '\n'.join(lines) + '\n\n')

    def _known_facts_block(self, session_key: str, valid_ids) -> tuple[str, set]:
        """带上去重用的已知事实；返回 (文本, 本批**允许失效**的事实 ID 集合)。

        为什么要把允许失效的 ID 带回来：`expired_fact_ids` 是模型给的裸数字，
        以前直接 `expire_user_fact(id)` —— 模型幻觉出一个 ID 就能把**别人**的事实
        标成失效（2026-11-03 审查发现）。现在只认「刚才真的给它看过的那几条」。
        """
        """把本批次说话人的现有事实列给模型，让它自己判重。

        为什么不在本地做：线上实测的重复是**换说法**的——「跑团新人」「自称TRPG萌新，
        刚加入群」「跑团纯萌新，想通过跑一局来了解流程」字符重合度极低，字符串相似度
        根本抓不住；而模型一眼就知道这是同一件事。
        代价可控：只带本批出现过的说话人，每人最多几条。
        """
        per_user = int(self.config.get('memory_curate_known_facts', 5) or 0)
        if per_user <= 0:
            return '', set()
        try:
            known = self.store.list_facts_of_users(session_key, valid_ids, per_user)
        except Exception:  # noqa: BLE001 - 拿不到就不带去重上下文，不影响提炼
            return '', set()
        lines: list[str] = []
        allowed: set = set()
        for user_id, facts in known.items():
            for item in facts:
                fid = item.get('id') if isinstance(item, dict) else 0
                fact = item.get('fact') if isinstance(item, dict) else str(item)
                scope = item.get('scope', 'group') if isinstance(item, dict) else 'group'
                if fid:
                    lines.append('  [ID=%d] %s（%s）：%s' % (fid, user_id, scope, fact))
                    allowed.add(int(fid))
                else:
                    lines.append('  %s：%s' % (user_id, fact))
        if not lines:
            return '', set()
        # 上限兜一下，别让去重上下文把提示词撑大
        body = '\n'.join(lines[:60])
        return ('\n已知事实（这些**已经记过了**，若未失效不要重复记；若已失效请在 expired_fact_ids 列出其 ID）：\n'
                + body + '\n'), allowed

    def _store_results(self, session_key: str, data: dict, valid_ids: set[str],
                       last_id: int, allowed_fact_ids: set | None = None,
                       source_ids: dict | None = None) -> dict:
        validate_curator_data(data)
        with self.store.transaction():
            return self._apply_results(session_key, data, valid_ids, last_id, allowed_fact_ids, source_ids)

    def _apply_results(self, session_key: str, data: dict, valid_ids: set[str], last_id: int,
                       allowed_fact_ids: set | None, source_ids: dict | None) -> dict:
        stats = {'summary': 0, 'facts': 0, 'self': 0, 'skipped': 0, 'expired': 0, 'impressions': 0}

        # ---- L1 群级摘要（只有公共事件）----
        summary = str(data.get('group_summary') or '').strip()
        if 0 < len(summary) <= 500:
            self.store.add_group_summary(session_key, summary, covers_to=last_id)
            stats['summary'] = 1
            # 裁剪也属于本批事务，失败时连同新摘要回滚。
            self.store.trim_group_summaries(
                session_key, keep=int(self.config.get('memory_summary_keep', 200) or 200))
        elif summary:
            logger.warning('群摘要过长已丢弃（%d 字）', len(summary))

        # ---- 过期/失效旧事实 ----
        # 只认「本批真的给它看过」的 ID：模型幻觉一个数字，绝不能把别人的事实标成失效。
        for fid in data.get('expired_fact_ids') or []:
            try:
                fid_int = int(fid)
            except (ValueError, TypeError):
                continue
            if allowed_fact_ids is not None and fid_int not in allowed_fact_ids:
                logger.info('忽略模型给的过期事实 ID=%s（不在本批已知事实里）', fid_int)
                continue
            self.store.expire_user_fact(fid_int)
            stats['expired'] += 1

        is_priv = _is_private_session(session_key)
        cur_scope = 'private' if is_priv else 'group'

        # ---- 群友/私聊高维印象更新（按 scope 隔离）----
        for imp in data.get('user_impressions') or []:
            if not isinstance(imp, dict):
                continue
            uid = str(imp.get('user_id') or '').strip()
            imp_summary = str(imp.get('summary') or '').strip()
            # 太短的（"无明显特质""是个群友"）不覆盖已有卡片：宁可留旧的，别把写好的写没了
            if len(imp_summary) < 6:
                continue
            if (source_ids is not None and source_ids.get(uid, 0) <=
                    self.store.forget_watermark('impression', uid)):
                continue
            if uid in valid_ids and imp_summary and len(imp_summary) <= 200:
                # set_user_impression 自己会挡住"一字未改"的写入，并按需追加历史
                if self.store.set_user_impression(uid, imp_summary, scope=cur_scope):
                    stats['impressions'] += 1

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
            if is_priv:
                # 私聊提炼：事实严格绑定当前私聊会话，绝不设为 global 跨群扩散
                target_session = session_key
            else:
                scope = str(item.get('scope') or 'group').strip().lower()
                target_session = 'global' if (scope == 'global' and self.config.get('memory_cross_group_enabled', True)) else session_key
            if (source_ids is not None and source_ids.get(uid, 0) <=
                    self.store.forget_watermark(target_session, uid)):
                stats['skipped'] += 1
                continue
            fuzzy_thresh = float(self.config.get('memory_fuzzy_dedup_threshold', 0.60))

            existing = self.store.find_similar_user_fact(target_session, uid, fact, threshold=fuzzy_thresh)
            if existing:
                self.store.touch_user_fact(existing['id'])
                stats['skipped'] += 1
                continue
            self.store.add_user_fact(
                target_session, uid, fact,
                category=str(item.get('category') or '').strip()[:12],
                source='inferred',
                importance=clamp01(item.get('importance'), 0.5),
                # 模型给的同义/上位词：检索端靠它实现「换说法也能搜到」
                keywords=item.get('keywords'),
            )
            stats['facts'] += 1
            # 容量兜底：字面归一化挡不住换说法的重复，超量就淘汰分数最低的。
            # 二档赞助者的记忆不参与淘汰（protect_ids），单人裁剪和按群总量裁剪都认。
            stats['trimmed'] = stats.get('trimmed', 0) + self.store.trim_user_facts(
                target_session, uid,
                max_per_user=int(self.config.get('memory_max_facts_per_user', 50) or 0),
                max_per_group=0,
                video_share_keep=int(self.config.get('memory_video_share_keep', 3) or 0),
                protect_ids=sponsors.protected_ids(self.config),
            )

        # ---- L3 角色自身记忆 ----
        for item in (data.get('self_memories') or [])[:10]:
            if not isinstance(item, dict):
                continue
            content = str(item.get('content') or '').strip()[:200]
            if not content:
                continue
            fuzzy_thresh = float(self.config.get('memory_fuzzy_dedup_threshold', 0.60))
            existing_self = self.store.find_similar_self_memory(session_key, content, threshold=fuzzy_thresh)
            if existing_self:
                self.store.touch_self_memory(existing_self['id'])
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
        # 保留窗口：状态纸条要读「刚过去、还没进长期记忆」的那一段，所以游标以下
        # 的行不立刻删，留 memory_buffer_retention_seconds（默认 2 小时）。
        try:
            retention = int(self.config.get('memory_buffer_retention_seconds', 7200) or 0)
        except (TypeError, ValueError):
            retention = 7200
        self.store.buffer_trim(session_key, retention_seconds=retention)
        # 每群总容量兜底（每人的上限在写每条时已经查过）
        stats['trimmed'] = stats.get('trimmed', 0) + self.store.trim_user_facts(
            session_key, '',
            max_per_user=0,
            max_per_group=int(self.config.get('memory_max_facts_per_group', 3000) or 0),
            video_share_keep=int(self.config.get('memory_video_share_keep', 3) or 0),
            protect_ids=sponsors.protected_ids(self.config),
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
