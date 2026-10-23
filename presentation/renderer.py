# -*- encoding: utf-8 -*-
"""渲染器：把模型输出变成安全、合规的可发送消息。

原则：模型只负责语言，动作/格式由程序决定。
- 只允许 [EMOTE:名称]（映射到已安装表情）与单个 @ 当前发言人
- 其他 OP / CQ 码一律剥离，禁止模型输出任意路径/参数
- 空回复、超长回复被拦截
- 表情包：emote_enabled 总开关；一条消息最多一张；同一会话不连用同一张；
  未知名称或图片缺失时静默移除标签，绝不把 [EMOTE:...] 原样发出去
- 回复审计：命中本机路径/凭据特征时整条不发（见 AUDIT_RULES 说明）

发送为什么用 CQ 码而不是 OP 码（2026-09 对照 OlivOS 源码实测）：
onebotSDK.formatMessage() 固定按 old_string 解析待发消息，而 API.__message_router
会先把插件给的串按 app.json 的 message_mode(tx) 解析、再按 SDK 侧格式(rx) 取出。
两种 rx 下实测结果：
    rx=old_string   : 发 OP → 转成 CQ ✓   | 发 CQ → 原样 CQ ✓
    rx=olivos_string: 发 OP → 原样 OP ✗   | 发 CQ → 原样 CQ ✓
只有 CQ 码在两种情况下都能被 formatMessage 解析成图片；发 OP 码在
rx=olivos_string 时会被当纯文本发出去（[OP:image,...] 直接显示在群里）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time

from ..config import BASE_DIR, SKIP_ACTIVE, get_data_dir, get_state_dir
from ..models import IncomingMessage, RenderedReply

logger = logging.getLogger('newGeYi.renderer')

# 发送统一用 CQ 码（原因见模块开头）
SEND_AT = '[CQ:at,qq=%s]'
SEND_IMAGE = '[CQ:image,file=%s]'

# 立绘表：名称 -> {'file': 文件名, 'desc': 什么时候用}
#
# 为什么放在数据文件里而不是写死在代码里（2026-09 改）：
# 立绘属于**素材**，会随整合包替换、增删。写死在代码里意味着「换一张图要改代码 + 跑测试」，
# 而且模型根本不知道每张图长什么样、该什么时候发。
# 现在换成：emotes.json 描述「有哪些立绘、各自什么时机」，
# 提示词里的【可用立绘】由它自动生成 —— 加一张图只需要改数据。
EMOTE_TABLE_FILE = 'emotes.json'
# file 留空时按「文件名主干 == 名称」找，这些扩展名都认
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.gif')

# ===== 立绘节流：硬闸门 =====
# 提示词里写了「每 5 条最多 1 张」这类纪律，但**模型不一定听**——线上实测过。
# 只靠提示词的后果就是群里说一句甩一张图，看起来像刷屏机器人。
# 所以这里再兜一层，而且这一层是**确定性**的：不满足就静默把标记摘掉，
# 文字照常发出去（少一张图无所谓，整条不发才是事故）。
EMOTE_MIN_GAP_SECONDS = 180      # 同一会话两次发图至少隔这么久
EMOTE_WINDOW_REPLIES = 5         # 滑动窗口：最近这几条回复里最多出现 1 张
EMOTE_HOURLY_CAP = 6             # 同一会话每小时上限

# 内置兜底表：插件包里的 emotes.json 读不到时才用。
# 本插件故意留空——歌伊的素材全部由 data/emotes.json 描述，代码里不写死任何角色素材。
EMOTE_MAP: dict[str, str] = {}


def load_emote_table(paths) -> dict:
    """从若干候选路径里读第一份能用的 emotes.json，返回 {名称: {'file','desc'}}。

    读不到就返回空字典（调用方自己决定兜底），不抛异常——
    素材文件缺失不该让插件起不来。
    """
    for path in paths:
        try:
            if not path or not os.path.exists(path):
                continue
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        items = raw.get('emotes') if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            continue
        table: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name', '')).strip()
            if not name:
                continue
            table[name] = {
                'file': str(item.get('file', '') or '').strip(),
                'desc': str(item.get('desc', '') or '').strip(),
            }
        if table:
            return table
    return {}

OP_TAG_RE = re.compile(r'\[OP:[^\]]*\]')
CQ_TAG_RE = re.compile(r'\[CQ:[^\]]*\]')
EMOTE_RE = re.compile(r'\[EMOTE:([^\]]+)\]')
# 模型可能写 OP 或 CQ 两种形态的 @，统一归一化
AT_TAG_RE = re.compile(r'\[(?:OP:at,id|CQ:at,qq)=([^,\]]+)[^\]]*\]')
FENCE_RE = re.compile(r'```[\s\S]*?```|`[^`\n]+`')

# 模型表示「这次不回复」的标记（主动搭话用 SKIP_ACTIVE，另兼容常见写法）。
# 只要标记出现在回复里，就整轮丢弃不下发：漏发一条只是少说一句，
# 而标记本身漏进群里会让角色瞬间出戏（调研里反复出现的翻车点）。
SILENT_MARKS = (SKIP_ACTIVE, '[NO_REPLY]', '【不回复】')


def is_silent_reply(text: str) -> bool:
    """模型输出是否表示「这次不回复」。"""
    cleaned = str(text or '').strip()
    if not cleaned:
        return False
    return any(mark and mark in cleaned for mark in SILENT_MARKS)


# ===== 回复审计：防泄露的最后一道闸门 =====
# 为什么要有这一层（2026-09 新增）：
# 提示词里写了「不泄露本机路径/凭据」，但模型不一定听；而这一条一旦漏了就是**不可逆**的
# ——消息已经发进群里，撤回也来不及。所以凡是「绝对不能发生」的事，都不能只写在提示词里，
# 代码里得再拦一道。（这个思路来自开源项目 qq-bridge 的回复审计设计。）
#
# 做法：命中就**整条不发**，而不是把敏感片段抠掉。抠掉之后剩下的半句往往残缺、更奇怪，
# 而且会把问题掩盖掉——日志里也就看不到模型真的犯过。
#
# 误伤是有代价的（用户那边表现为「她突然不说话」），所以每条规则都加了边界约束，
# 避免把网址、正常数字误判成路径/密钥；命中时写一条 warning，带上规则名和片段，方便定位。
AUDIT_RULES = (
    # Windows 盘符路径。前面不能紧跟字母数字，否则 http:// 里的 "p:/" 会被误判成本机路径。
    (re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]'), '本机路径'),
    # Unix 家目录/系统目录。前面不能紧跟字母数字或点，否则网址里的 x.com/home/ 会被误判。
    (re.compile(r'(?<![A-Za-z0-9.\-])/(?:home|Users|root|etc|var|opt|srv)/'), '本机路径'),
    # 凭据字样后面跟着赋值号，例如 token= / api_key:
    (re.compile(r'(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret'
                r'|password|passwd|credential)\b\s*[:=]'), '凭据'),
    # 典型密钥字面量
    (re.compile(r'\bsk-[A-Za-z0-9_\-]{16,}'), '密钥'),
    # 32 位以上的十六进制串（要求至少含一个字母，免得把一长串纯数字误判成密钥）
    (re.compile(r'\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{32,}\b'), '疑似密钥'),
    # 上游服务商安全拦截/策略拒绝报错泄露
    (re.compile(r'(?i)(?:The prompt could not be submitted|Generative AI Prohibited Use policy|'
                r'violate[s]? Google\'s|ai\.google\.dev/gemini-api|'
                r'content management policy|content filter|'
                r'HARM_CATEGORY_|SAFETY_RATING|'
                r'Request was rejected due to safety policies)'), '上游服务商安全策略拦截报错'),
)


def audit_reply(text: str) -> str:
    """回复审计：命中返回「规则名（片段）」，没命中返回空串。

    只判能不能发，不做任何改写——改写会让人以为模型没犯过错。
    """
    if not text:
        return ''
    for pattern, label in AUDIT_RULES:
        found = pattern.search(text)
        if found:
            return f'{label}｜命中片段 {found.group(0)[:40]!r}'
    return ''


# 模型有强烈的「用括号写舞台提示」倾向。歌伊的（）动作描写是有意设计、要发出去的，
# 但同一个通道也会漏出「（内心：好烦）」这种不该出现在群里的东西——线上同类翻车点
# 在 qq-bridge 里被单独写了一条规则（它那边是括号内心戏被当成正式消息发出去）。
# 只拦「括号一开头就是自白词」的情况，正常的（歪头）（眨眼）（笑）一律不动。
INNER_MONOLOGUE_RE = re.compile(
    r'[（(]\s*(?:内心|心里|心想|暗想|心理活动|心声|脑内|os|OS|旁白)\s*[:：]?[^）)]*[）)]'
)


def op_codes_to_plain(text: str) -> str:
    """把收到的码转成模型能看懂的标记（入参前清洗，OP 为主、兼容 CQ）。"""
    if not text:
        return ''
    text = re.sub(r'\[(?:OP:at,id|CQ:at,qq)=([^,\]]+)[^\]]*\]', r'【@\1】', text)
    text = re.sub(r'\[(?:OP|CQ):image[^\]]*\]', '【图片】', text)
    text = re.sub(r'\[(?:OP|CQ):face[^\]]*\]', '【表情】', text)
    text = re.sub(r'\[(?:OP|CQ):record[^\]]*\]', '【语音】', text)
    text = re.sub(r'\[(?:OP|CQ):video[^\]]*\]', '【视频】', text)
    text = re.sub(r'\[(?:OP|CQ):file[^\]]*\]', '【文件】', text)
    text = re.sub(r'\[(?:OP|CQ):reply[^\]]*\]', '【引用消息】', text)
    text = re.sub(r'\[OP:[^\]]*\]', '', text)
    text = re.sub(r'\[CQ:[^\]]*\]', '', text)
    return text.strip()


class Renderer:
    def __init__(self, config: dict):
        self.config = config
        # 用户通过「加载立绘」加进来的图放 state 目录 —— 放 data/ 里的话，
        # OlivOS 每次重载都会把 data/ 整个删掉重建（见 config.get_state_dir 说明）
        self.emote_dir = get_state_dir() / 'emote'
        self.emote_dir.mkdir(parents=True, exist_ok=True)
        # 兜底查找路径：OlivOS 释放出来的副本、插件包里的原件、以及历史遗留的 emote/
        self.released_emote_dir = get_data_dir() / 'emote'
        self.bundled_emote_dir = BASE_DIR / 'data' / 'emote'
        self._baked_emote_dir = BASE_DIR / 'emote'
        # 立绘表：用户改的优先（且活得过重载），其次 OlivOS 释放的副本，最后插件包原件。
        # 三份都没有才退回代码里的内置兜底表。
        self.emote_table: dict[str, dict] = load_emote_table([
            get_state_dir() / EMOTE_TABLE_FILE,
            get_data_dir() / EMOTE_TABLE_FILE,
            BASE_DIR / 'data' / EMOTE_TABLE_FILE,
        ])
        if not self.emote_table:
            self.emote_table = {
                name: {'file': fname, 'desc': ''} for name, fname in EMOTE_MAP.items()
            }
        self._last_emote: dict[str, str] = {}
        # 上一条回复是不是以省略号开头（防止连着两条都「……」开头）
        self._last_head: dict[str, bool] = {}
        # 节流状态：每个会话一份
        self._emote_at: dict[str, list[float]] = {}      # 发图时间戳
        self._recent_replies: dict[str, list[bool]] = {}  # 最近几条回复带没带图
        self._lock = threading.Lock()

    # ---------------- 立绘 ----------------
    def _candidates(self, key: str) -> list[str]:
        """某个名称可能对应的文件名（按优先级）。

        emotes.json 里显式写了 file 就用它；留空则按「主干 == 名称」找，扩展名不限。
        """
        entry = self.emote_table.get(key) or {}
        fname = str(entry.get('file') or '').strip()
        if fname:
            return [fname]
        return [key + ext for ext in IMAGE_SUFFIXES]

    def _find_emote(self, key: str):
        """按优先级找立绘文件：用户目录 > OlivOS 释放目录 > 插件包 > 历史遗留。"""
        for fname in self._candidates(key):
            for folder in (self.emote_dir, self.released_emote_dir,
                           self.bundled_emote_dir, self._baked_emote_dir):
                path = folder / fname
                if path.exists():
                    return path
        return None

    def available_emotes(self) -> list[tuple[str, str]]:
        """已经装好文件的立绘：[(名称, 使用时机说明)]，保持 emotes.json 里的顺序。

        给 prompt_builder 用：只有图真的存在才写进提示词，缺图的条目等于不存在，
        不会白白占 token，也不会让模型发一个发不出来的名字。
        """
        out: list[tuple[str, str]] = []
        for name, entry in self.emote_table.items():
            if self._find_emote(name) is None:
                continue
            out.append((name, str(entry.get('desc') or '')))
        return out

    # ---------------- 立绘节流 ----------------
    def _cfg_num(self, key: str, default):
        try:
            return type(default)(self.config.get(key))
        except (TypeError, ValueError):
            return default

    def _emote_allowed(self, session: str, now: float) -> bool:
        """硬闸门：不满足就把标记静默摘掉（文字照常发出去）。"""
        if not session:
            return True
        gap = self._cfg_num('emote_min_gap_seconds', EMOTE_MIN_GAP_SECONDS)
        window = self._cfg_num('emote_window_replies', EMOTE_WINDOW_REPLIES)
        cap = self._cfg_num('emote_hourly_cap', EMOTE_HOURLY_CAP)
        with self._lock:
            stamps = self._emote_at.get(session, [])
            if stamps and now - stamps[-1] < gap:
                return False
            if sum(1 for stamp in stamps if now - stamp < 3600) >= cap:
                return False
            if any(self._recent_replies.get(session, [])[-window:]):
                return False
        return True

    def _note_emote(self, session: str, now: float) -> None:
        with self._lock:
            stamps = [t for t in self._emote_at.get(session, []) if now - t < 3600]
            stamps.append(now)
            self._emote_at[session] = stamps

    def _note_reply(self, session: str, had_emote: bool) -> None:
        """每条回复都记一笔（带没带图），给滑动窗口用。"""
        if not session:
            return
        window = max(1, self._cfg_num('emote_window_replies', EMOTE_WINDOW_REPLIES))
        with self._lock:
            recent = self._recent_replies.setdefault(session, [])
            recent.append(bool(had_emote))
            del recent[:max(0, len(recent) - window)]

    def emote_budget(self, session: str, now: float | None = None) -> str:
        """当前会话的立绘额度，给「立绘状态」用。"""
        now = time.time() if now is None else now
        gap = self._cfg_num('emote_min_gap_seconds', EMOTE_MIN_GAP_SECONDS)
        cap = self._cfg_num('emote_hourly_cap', EMOTE_HOURLY_CAP)
        window = self._cfg_num('emote_window_replies', EMOTE_WINDOW_REPLIES)
        with self._lock:
            stamps = [t for t in self._emote_at.get(session, []) if now - t < 3600]
            recent = list(self._recent_replies.get(session, []))
        wait = 0.0
        if stamps:
            wait = max(0.0, gap - (now - stamps[-1]))
        return (f'本群：最近 1 小时 {len(stamps)}/{cap} 张 | '
                f'最近 {len(recent)} 条回复里 {sum(recent)} 张 | '
                + (f'还需等 {wait:.0f} 秒' if wait > 0 else '现在可以发'))

    def emote_message(self, key: str, session: str = '') -> str:
        """立绘名称 -> 可直接发送的图片码；不可用时返回空串。

        session 非空时启用两道会话级限制：
        - 同一张不连续用（防复读）
        - 节流闸门：最短间隔 / 滑动窗口比例 / 每小时上限
        """
        if not self.config.get('emote_enabled', True):
            return ''
        key = str(key).strip()
        if key not in self.emote_table:
            return ''
        path = self._find_emote(key)
        if path is None:
            return ''
        if session:
            now = time.time()
            if not self._emote_allowed(session, now):
                return ''
            with self._lock:
                if self._last_emote.get(session) == key:
                    return ''
                self._last_emote[session] = key
            self._note_emote(session, now)
        return SEND_IMAGE % f'file:///{path.resolve()}'

    def setup_emotes(self, source_dir: str) -> int:
        """从指定目录（含子目录）复制立绘进来，返回成功张数。

        认两种文件：① emotes.json 里显式写了 file 的；② 文件名主干正好是某个立绘名称的。
        递归是为了兼容「解压后多包了一层」的目录，例如 立绘包/立绘包/*.png。

        如果整合包的文件名跟立绘名称对不上（很常见，比如 歌伊_01.png），
        就在 emotes.json 里给对应条目写上 file，再跑一次本命令。
        """
        copied = 0
        if not source_dir or not os.path.isdir(source_dir):
            return 0
        # 目标文件名（小写）-> 实际要写进去的文件名
        wanted: dict[str, str] = {}
        for key in self.emote_table:
            for fname in self._candidates(key):
                wanted[fname.lower()] = fname
        for root, _dirs, files in os.walk(source_dir):
            for name in files:
                target = wanted.get(name.lower())
                if not target:
                    continue
                try:
                    shutil.copy2(os.path.join(root, name), self.emote_dir / target)
                    copied += 1
                except OSError:
                    pass
        return copied

    def emote_locations(self) -> list[str]:
        """立绘的查找目录，用于状态显示。"""
        return [str(self.emote_dir), str(self.released_emote_dir),
                str(self.bundled_emote_dir), str(self._baked_emote_dir)]

    def reload_emotes(self) -> int:
        """重新读 emotes.json，返回新的条目数。

        立绘表是 __init__ 时读进来的，所以改完 data/emotes.json 得让插件重载才生效。
        给个「立绘重载」指令，省得为了加一张图去重启整个插件。
        """
        table = load_emote_table([
            get_state_dir() / EMOTE_TABLE_FILE,
            get_data_dir() / EMOTE_TABLE_FILE,
            BASE_DIR / 'data' / EMOTE_TABLE_FILE,
        ])
        if not table:
            table = {name: {'file': fname, 'desc': ''} for name, fname in EMOTE_MAP.items()}
        with self._lock:
            self.emote_table = table
            # 防复读记的是名称，表换了就可能指到别的图，清掉更安全
            self._last_emote.clear()
        return len(table)

    def emote_status(self) -> tuple[int, list[str]]:
        loaded = 0
        lines = []
        for key in self.emote_table:
            path = self._find_emote(key)
            if path is not None:
                loaded += 1
            shown = path.name if path is not None else self._candidates(key)[0]
            lines.append(f"  {'[OK]' if path is not None else '[--]'} {key}: {shown}")
        return loaded, lines

    # ---------------- 渲染 ----------------
    def render(self, text: str, msg: IncomingMessage) -> RenderedReply | None:
        """清洗模型输出为可发送的回复。"""
        if not text:
            return None
        # 静默标记：主动/被动一视同仁，绝不让它漏进群里
        if is_silent_reply(text):
            return None
        # 去掉代码围栏
        text = FENCE_RE.sub('', text).strip()
        # 思考过程/思维链/草稿：剥除混入的思考过程，防止模型思维链漏出到群里
        text = self._strip_thinking_process(text).strip()
        # 括号内心戏：先摘掉，免得它既被当成正式消息发出去、又去触发下面的审计
        text = INNER_MONOLOGUE_RE.sub('', text).strip()
        if not text:
            return None
        # 回复审计：必须放在表情码替换**之前**——替换之后正文里会带上我们自己生成的
        # file:/// 本机路径，那时候再审计就是自己拦自己。
        if self.config.get('reply_audit_enabled', True):
            hit = audit_reply(text)
            if hit:
                logger.warning('回复被审计拦截（%s）：%r', hit, text[:120])
                fallback = str(self.config.get('reply_audit_fallback', '') or '').strip()
                return RenderedReply(text=fallback) if fallback else None
        # 先归一化 @：仅允许 @ 当前发送者，且最多一次
        allowed_at = SEND_AT % msg.sender_id
        text = AT_TAG_RE.sub(
            lambda m: allowed_at if m.group(1) == msg.sender_id else '【@某用户】',
            text,
        )
        at_ops = re.findall(r'\[CQ:at,qq=[^,\]]+\]', text)
        if len(at_ops) > 1:
            first = at_ops[0]
            idx = text.index(first) + len(first)
            after = re.sub(r'\[CQ:at,qq=[^,\]]+\]', '', text[idx:])
            text = text[:idx] + after
        # 再剥离模型自带的其余所有码（OP 与 CQ 都剥，防止模型注入图片/路径）
        text = OP_TAG_RE.sub(lambda m: m.group(0) if m.group(0) == allowed_at else '', text)
        text = CQ_TAG_RE.sub(lambda m: m.group(0) if m.group(0) == allowed_at else '', text)
        # 剥除称呼时误带的纯数字 QQ 账号（例如 "张三(123456789)" -> "张三"）
        text = re.sub(r'\s*[（(]\s*\d{5,12}\s*[）)]', '', text)
        # 表情：一条消息最多一张，同一会话不连用同一张
        session = msg.session.as_text()
        state = {'used': False}

        # 兼容处理：若模型误写成了【图片:名称】或[图片:名称]，自动纠正为 [EMOTE:名称]
        def _alias_emote(m: re.Match) -> str:
            name = m.group(1).strip()
            if name in self.emote_table:
                return f'[EMOTE:{name}]'
            return ''

        text = re.sub(r'[【\[［(（]\s*(?:图片|表情|立绘)\s*[:：]\s*([^】\]］)）]+)[】\]］)）]', _alias_emote, text)

        def _emote(match: re.Match) -> str:
            if state['used']:
                return ''
            op = self.emote_message(match.group(1), session=session)
            if op:
                state['used'] = True
            return op

        text = EMOTE_RE.sub(_emote, text)
        # 彻底清除模型输出的任何裸【图片】/【表情】/（图片）占位文字，杜绝将占位符发进群聊
        text = re.sub(r'[【\[［(（]\s*(?:图片|表情|立绘|image|pic)\s*[】\]］)）]', '', text)
        # 记一笔「这条回复带没带图」——节流的滑动窗口要用
        self._note_reply(session, state['used'])
        # 形态整备：折叠空行、去掉重复的开头省略号（见方法上的说明）
        text = self._tidy_reply(text, session)
        # 长度上限
        max_len = int(self.config.get('max_reply_chars', 500))
        if len(text) > max_len:
            text = text[:max_len].rstrip() + '…'
        text = text.strip()
        if not text:
            return None
        return RenderedReply(text=text)

    # ---------------- 输出形态整备 ----------------
    # 为什么要用代码兜：这两件事提示词里都写了，但**模型不一定听**——
    # 尤其「省略号别每句都用」这种风格类要求，实测压不住（同类教训见
    # 「没什么想说的就忽略」拦不住模型）。能用代码保证的形态就别指望提示词。
    _ELLIPSIS_HEAD_RE = re.compile(r'^\s*(?:…{1,}|\.{3,}|。{2,})+\s*')
    _BLANK_LINE_RE = re.compile(r'\n\s*\n+')

    def _tidy_reply(self, text: str, session: str) -> str:
        """把模型输出整成「一条聊天消息」的样子。

        ① **折叠空行**：模型爱写成好几个段落，中间还空行，在 QQ 里看着像刷屏。
           连续空行压成一个换行，行首行尾的空白也去掉。
        ② **剥掉重复的开头省略号**：她连着两条都以「……」开头就太假了。
           上一条已经这么开头、这条又是，就把这条开头的省略号去掉
           （首部省略号只是语气，剥掉不损失信息：「……嗯。」→「嗯。」）。
        ③ **剥掉结尾句号**：中文聊天里句号看着像生气或者冷淡，真人几乎不打。
           这是中文 IM 的硬约定，模型十有八九会带，所以用代码剥。
        """
        if not text:
            return text
        # ① 空行折叠与两段式短句合并（防两截式回复刷屏）
        text = self._BLANK_LINE_RE.sub('\n', text)
        if self.config.get('collapse_short_multiline', False):
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            if len(lines) == 2 and len(text) < 70 and not any(lines[0].startswith(c) for c in ('-', '*', '1.', '2.')):
                punc = ('，', '。', '！', '？', '…', '~', '、', ',', '!', '?')
                first, second = lines[0], lines[1]
                if first.endswith(punc):
                    text = f'{first} {second}'
                else:
                    text = f'{first}……{second}'
            else:
                text = '\n'.join(lines)
        else:
            text = '\n'.join(line.strip() for line in text.split('\n'))
        text = text.strip('\n')
        if not text:
            return text
        # ② 重复的开头省略号
        if self.config.get('strip_repeated_ellipsis', True):
            starts_with_ellipsis = bool(self._ELLIPSIS_HEAD_RE.match(text))
            with self._lock:
                previous = self._last_head.get(session, False)
                self._last_head[session] = starts_with_ellipsis
            if starts_with_ellipsis and previous:
                stripped = self._ELLIPSIS_HEAD_RE.sub('', text).strip()
                if stripped:          # 别把整条只剩省略号的回复清空
                    text = stripped
        # ③ 结尾句号
        if self.config.get('strip_trailing_period', True):
            text = self._drop_trailing_period(text)
        return text

    @staticmethod
    def _drop_trailing_period(text: str) -> str:
        """去掉结尾那**一个**句号。

        - 「好的。。。」不动 —— 那是表示无语的聊天写法，是有意打的
        - 剥完变成空串就保留原样 —— 别把一条回复整没了
        """
        stripped = text.rstrip()
        if not stripped.endswith('。') or stripped.endswith('。。'):
            return text
        result = stripped[:-1].rstrip()
        return result if result else text

    @staticmethod
    def _strip_thinking_process(text: str) -> str:
        """剥除模型输出中混入的思考过程、分析草稿或标签。

        包含：
        ① XML 思考标签：<think>...</think> 或未闭合的 <think>
        ② 结构化思考分析块：如【当前用户】、【用户特点】、【思考过程】、【回复思路】等
        ③ 视觉解析与多模态构思草稿：如「xxx发了截图... 人设：... 可以吐槽/安慰：...」
        """
        if not text:
            return text
        # ① 去除 XML 思考标签
        text = re.sub(r'<(?:think|thought)>[\s\S]*?</(?:think|thought)>', '', text, flags=re.IGNORECASE)
        if re.match(r'^\s*<(?:think|thought)>', text, re.IGNORECASE):
            parts = re.split(r'</(?:think|thought)>', text, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) > 1:
                text = parts[1]

        # ② 检查是否包含元分析 / 思考规划特征（文本草稿模式）
        meta_patterns = [
            r'【(?:提炼|核心|人设|约束|要求|检查|表情包|拟定|草稿|思考|思路|步骤|分析|用户|特点|画像|问题|态度|任务|目标|背景|审核|复核|评价)[^】]*】',
            r'(?:思考过程|回复思路|思考步骤|思维链|分析过程|构思草稿|拟定回复|思考|思路|分析|字数)\s*[:：]',
            r'(?:歌伊|角色|人物)?人设\s*[:：]',
            r'(?:可以|建议)?(?:吐槽|回复|安慰|开玩笑|发个?)\s*[:：]',
            r'(?:或者|也可)?(?:发个?|说|回复|吐槽|安慰)\s*[:：]',
            r'对方正在.*?(?:边缘|抽|说|问|聊)',
            r'发了.*?的截图',
            r'约\d+字[。！？\s]*符合',
        ]
        has_meta = any(re.search(p, text) for p in meta_patterns)

        if has_meta:
            candidate = ''
            # A. 优先寻找显式的最终输出标识（包括【拟定回复】、【最终回复】等）
            anchor = re.search(r'(?:最终回复|实际回复|正式回复|输出|回复|拟定回复|拟定)\s*[:：\]】]\s*([^\n]+(?:\n[^\n]+)*)$', text)
            if anchor:
                candidate = anchor.group(1).strip()
            else:
                # B. 逐行排查，过滤掉所有分析规划行
                lines = text.split('\n')
                kept = []
                for line in lines:
                    sline = line.strip()
                    if not sline:
                        continue
                    if any(re.search(p, sline) for p in meta_patterns):
                        continue
                    # 过滤掉单行纯引号草稿行
                    if re.match(r'^[“"『「].*[”"』」]$', sline):
                        continue
                    kept.append(sline)
                if kept:
                    candidate = '\n'.join(kept).strip()

            if not candidate:
                candidate = text.strip()

            # C. 清洗字数自审与字数自评草稿（如 "约40字。符合。"、"字数：38字"）
            candidate = re.sub(r'约\d+字[。！？\s]*(?:符合|达标|合适)?[。！？\s]*', '', candidate)
            candidate = re.sub(r'字数\s*[:：][^。！？\n]*[。！？\n]?', '', candidate)

            # D. 处理修改重构导致的双重动作拼接（例如 "(草稿)...(定稿)"，提取最后一段真正的最终回复）
            # 仅在文本包含草稿/修改等提示词时才裁剪，避免误伤正常的连续动作 RP 描写
            if re.search(r'(?:草稿|定稿|拟定|修改|重构|思路|思考)', candidate):
                actions = list(re.finditer(r'([（(][^）)]+[）)][^（(]+)', candidate))
                if len(actions) >= 2:
                    last_action = actions[-1].group(0).strip()
                    if len(last_action) >= 10:
                        candidate = last_action

            # E. 若结尾仍有残余引号草稿粘连，摘掉引号草稿部分
            candidate = re.sub(r'^[“"『「][^”"』」]+[”"』」]\s*', '', candidate)
            return candidate.strip()
        return text.strip()
