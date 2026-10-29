# -*- encoding: utf-8 -*-
"""运行时核心：装配所有模块，处理事件。

职责边界：
- 事件入口（main.py）只负责转发。
- Runtime 负责消息归一化、命令、路由、生成、回复、状态提交。
- 生成任务经 SessionScheduler 提交，保证同会话有序、不同会话并行。
- 所有 OlivOS 强耦合只出现在本模块的 menu 局部导入中，便于单元测试。
"""

from __future__ import annotations

from collections import deque
import logging
import os
import random
import threading
import time

from .. import config as plugin_config
from ..context.cross_plugin_observer import install_cross_plugin_observer
from ..context.live_context import LiveContextStore
from ..context.group_profile import GroupProfile
from ..context.images import ImageContext
from ..context.prompt_builder import PromptBuilder
from ..memory.curator import MemoryCurator
from ..memory.repository import MemoryRepository
from ..memory.worldbook import WorldbookService
from ..models import IncomingMessage, ModelRequest, SessionKey
from ..presentation.renderer import Renderer, is_silent_reply, op_codes_to_plain
from ..providers.llm_gateway import LLMGateway
from ..routing.attention import AttentionGate
from ..routing.commands import CommandRouter
from ..routing.message_router import MessageRouter
from ..routing.permissions import Permissions
from ..storage.database import SQLiteStore
from ..voice.manager import VoiceManager
from .rest import RestGuard
from .scheduler import SessionScheduler

logger = logging.getLogger('newGeYi')

_POKE_TEXT = [
    '诶？怎么戳我呀',
    '（揉揉脸）痒痒的！',
    '呜哇！吓了一跳！',
]
_POKE_TEXT_REPEAT = [
    '别戳啦别戳啦！',
    '再戳就要生气了哦！',
    '……（转过头去不理你了）',
]

# 被戳时改用立绘的概率，按次数分级选图（名称必须出现在 data/emotes.json 里）
_POKE_EMOTE_PROB = 0.3
_POKE_EMOTE_FIRST = '害羞'
_POKE_EMOTE_TEASE = '惊讶'
_POKE_EMOTE_ANGRY = ('红温生气', '死鱼眼')


class _ProcLogHandler(logging.Handler):
    """把插件 logger 的记录转发到 OlivOS 日志。

    插件里到处是 `logger.warning('记忆提炼未完成…')` / `logger.exception(...)`，
    但 OlivOS 只记录 proc.log 的输出，**这些告警在日志文件里一条都看不到**——
    线上排查「记忆提炼一半失败」时，翻遍两天日志找不到任何线索，只能靠数据库反推。
    """

    def __init__(self, runtime):
        super().__init__(level=logging.INFO)
        self.runtime = runtime

    def emit(self, record):
        try:
            name = record.name.split('.')[-1]
            level = 1 if record.levelno >= logging.WARNING else 2
            self.runtime.log(level, '[%s] %s' % (name, record.getMessage()))
        except Exception:  # noqa: BLE001 - 日志转发失败绝不能反过来影响业务
            pass


class Runtime:
    def __init__(self):
        self.proc = None
        self.config: dict = plugin_config.load_config()
        self.store = SQLiteStore(str(plugin_config.get_database_path()))
        self.memory = MemoryRepository(self.store, self.config)
        self.worldbook = WorldbookService(str(plugin_config.get_resource_path('worldbook.json')))
        self.permissions = Permissions(self.config, owner_ids_env=os.environ.get('GEYI_OWNER_IDS', ''))
        self.renderer = Renderer(self.config)
        # 主动搭话决策：四道闸门 + 自适应冷却（旧版是 2% 随机骰子）
        self.attention = AttentionGate(self.store, self.config)
        # 群档案：按群人数算每日额度（防被拉进小群刷爆）
        self.group_profile = GroupProfile(self.store, self.config)
        self.attention.group_profile = self.group_profile   # 闸门也要看本群额度
        self.gateway = LLMGateway(self.store, self.config)
        self.gateway.set_notify(self._notify_owner)
        # 自动记忆：攒批提炼，跑在自己的后台线程里（不占用回复的会话队列）
        self.curator = MemoryCurator(self.config, self.store, self.gateway)
        self.live_context = LiveContextStore()
        # 图片理解：记住最近收到的图，回复时按需下载并内联成 base64
        self.images = ImageContext(self.config)
        self.voice_manager = VoiceManager(self.config)
        # 自我保护静默：模型可以用 [REST:原因] 让某个群安静一段时间
        self.rest = RestGuard(self.store, self.config)
        self.prompt_builder = PromptBuilder(
            self.config, self.store, self.memory, self.worldbook,
            self.live_context, self.images, self.renderer,
        )
        self.prompt_builder.voice_manager = self.voice_manager
        self.prompt_builder.set_role_prompt(self.load_role_prompt())
        self.commands = CommandRouter(self)
        self.router = MessageRouter(self)
        self.scheduler = SessionScheduler(self)
        self._poke_counts: dict[str, tuple[int, float]] = {}
        self._poke_back_at: dict[str, float] = {}
        self.video_context = None
        self.web_context = None
        self._suppressed_messages: set[str] = set()
        self._self_sent_lock = threading.Lock()
        self._recent_self_sent: deque[tuple[str, str, float]] = deque(maxlen=40)
        self.cross_plugin_observer = None
        self._arrival_lock = threading.Lock()
        self._arrival_seq = 0
        self._start_time = time.time()
        # 已登录机器人账号名单（骰娘/程心等）的短期缓存，用于识别机器人消息
        self._bot_ids_cache: set[str] | None = None
        self._bot_ids_checked_at = 0.0

    # ==================== 初始化 ====================
    def init(self, Proc) -> None:
        self.proc = Proc
        self._attach_log_bridge()
        # 把关键路径写进启动日志：排查「配置到底读的哪一份」时最省事
        try:
            cfg_path = plugin_config.get_config_path()
            db_path = plugin_config.get_database_path()
            # 两个目录分开打：state 放可变状态（配置/数据库/导出），
            # data 是 OlivOS 每次加载都会清空重建的静态资源目录。
            # 以前两者是同一个，配置因此每次重载都被抹掉（2026-09 定位）。
            self.log(2, f'状态目录: {plugin_config.get_state_dir()}')
            self.log(2, f'资源目录: {plugin_config.get_data_dir()}')
            self.log(2, f'配置文件: {cfg_path}（{"已存在" if cfg_path.exists() else "不存在，本次按默认值运行"}）')
            self.log(2, f'数据库: {db_path}（{"已存在" if db_path.exists() else "首次创建"}）')
            # 资源文件是「数据目录优先、插件包兜底」：服务器上数据目录里那份会盖住升级后的
            # 插件包文件。把解析结果打出来，免得出现「我明明改了却没生效」这种无头案。
            role_path = plugin_config.get_resource_path('role_prompt.txt')
            wb_path = plugin_config.get_resource_path('worldbook.json')
            wb_state = '开启' if self.config.get('worldbook_enabled') else '关闭（「世界书开」启用）'
            self.log(2, f'角色设定: {role_path}')
            self.log(2, f'世界书: {wb_path}（{len(self.worldbook.entries)} 条，{wb_state}）')
            if not cfg_path.exists():
                self.log(1, f'⚠ 配置文件缺失：{cfg_path} —— 想改配置请把 config.json 放到这个路径下再「重载设定」')
            else:
                missing = plugin_config.missing_config_keys()
                if missing:
                    shown = '、'.join(missing[:8]) + ('…' if len(missing) > 8 else '')
                    self.log(1, f'⚠ 配置文件缺少 {len(missing)} 个新字段（本次按默认值运行）：{shown}')
        except Exception:  # noqa: BLE001 - 打印路径失败不影响启动
            logger.exception('打印数据路径失败')
        has_key = bool(
            os.environ.get('GEYI_API_KEY', '').strip()
            or str(self.config.get('api_key', '') or '').strip()
        )
        if not has_key:
            self.log(1, '未配置 API Key：可用「设置key <key>」指令，或环境变量 GEYI_API_KEY')
        # 通知通道同样要显式检查：没有它，「渠道全挂」「自我保护静默」都只会在日志里
        # 留一句 warning，人却在 QQ 上什么也收不到（2026-10 线上踩过）
        try:
            if not self.permissions._owner_set():
                self.log(1, '⚠ 未配置 owner_ids：渠道全挂提醒、自我保护静默通知都发不出去。'
                            '请在 config.json 加 "owner_ids": ["你的QQ号"]，或用环境变量 GEYI_OWNER_IDS')
        except Exception:  # noqa: BLE001
            pass
        # 带上代码时间戳：重载插件后对一眼就知道是不是真的换了代码
        # （OlivOS 的重载是重启插件进程，理论上是全新解释器；但没生效时看不出来）
        self.log(2, f'newGeYi v{plugin_config.PLUGIN_VERSION} 初始化完成'
                    f'（代码时间戳 {plugin_config.code_stamp()}）')

    def init_after(self, Proc) -> None:
        # 渠道恢复探测：只有存在「不可用但优先级更高」的渠道时才会真正发请求
        try:
            self.gateway.start_probe()
        except Exception:  # noqa: BLE001
            logger.exception('启动渠道探测线程失败')
        # 记忆提炼线程：攒够一批或空闲够久才调一次模型
        try:
            self.curator.start()
        except Exception:  # noqa: BLE001
            logger.exception('启动记忆提炼线程失败')
        # 跨插件消息感知钩子（捕获骰娘/运势/签到等其他插件输出，避免歌伊上下文理解脱节）
        try:
            self.cross_plugin_observer = install_cross_plugin_observer(self)
        except Exception:  # noqa: BLE001
            logger.exception('挂载跨插件消息感知钩子失败')
        self.log(2, f'newGeYi v{plugin_config.PLUGIN_VERSION} init_after 完成')

    def save(self, Proc) -> None:
        try:
            self.curator.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.gateway.stop_probe()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.scheduler.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.log(2, f'newGeYi v{plugin_config.PLUGIN_VERSION} save 完成（SQLite 已实时落盘）')

    def _attach_log_bridge(self) -> None:
        """把 newGeYi.* 的 logging 记录接到 OlivOS 日志上（重复初始化时不会叠加）。

        注意要**显式把级别降到 INFO**：Python 的 logger 默认继承 root 的 WARNING，
        `logger.info('记忆提炼：摘要1 事实3')` 这类正常进展根本到不了 handler。
        """
        root = logging.getLogger('newGeYi')
        for handler in list(root.handlers):
            if isinstance(handler, _ProcLogHandler):
                root.removeHandler(handler)
        root.setLevel(logging.INFO)
        root.addHandler(_ProcLogHandler(self))

    def log(self, level: int, text: str) -> None:
        try:
            if self.proc is not None and hasattr(self.proc, 'log'):
                self.proc.log(level, f'[newGeYi] {text}', [])
                return
        except Exception:  # noqa: BLE001
            pass
        logger.info(text)

    def _notify_owner(self, text: str) -> bool:
        """私聊通知骰主（渠道全挂、自我保护静默等运维事件）。返回是否真的发出去了。

        踩过的坑（2026-10）：旧实现用 `next(iter(owner_set), '')` 取收件人，而
        `owner_ids` 默认是空列表 —— 于是「歌伊触发了自我保护、骰主却一条私信都没收到」，
        而且日志里连一句失败都没有。现在收件人为空会**明确写日志**，绝不再假装发过。
        """
        targets = sorted(self.permissions._owner_set())
        if not targets:
            logger.warning(
                '没有配置 owner_ids（或环境变量 GEYI_OWNER_IDS），通知发不出去：%s',
                str(text or '').splitlines()[0][:80],
            )
            return False
        if self.proc is None:
            logger.warning('插件还没初始化完（proc 为空），通知发不出去')
            return False
        bot_info_dict = getattr(self.proc, 'Proc_data', {}).get('bot_info_dict', {})
        if not bot_info_dict:
            logger.warning('没有已登录的机器人账号，通知发不出去')
            return False
        try:
            import OlivOS  # noqa: PLC0415 - 仅通知时才需要
        except Exception:  # noqa: BLE001
            logger.exception('导入 OlivOS 失败，通知发不出去')
            return False
        _bot_hash, bot_info = next(iter(bot_info_dict.items()))
        sent = False
        for owner_id in targets:
            try:
                plugin_event = OlivOS.API.Event(
                    OlivOS.contentAPI.fake_sdk_event(
                        bot_info=bot_info, fakename=plugin_config.NAMESPACE
                    ),
                    self.proc.log,
                )
                plugin_event.send('private', int(owner_id), text)
                sent = True
                logger.info('已私聊通知骰主 %s', owner_id)
            except Exception:  # noqa: BLE001 - 通知失败不影响主流程
                logger.exception('私聊通知骰主 %s 失败', owner_id)
        return sent

    # ==================== 自我保护静默 ====================
    def is_resting(self, session_key: str) -> bool:
        """这个会话是不是正处于歌伊的自我保护静默期。"""
        try:
            return self.rest.is_resting(session_key)
        except Exception:  # noqa: BLE001 - 状态读挂了大不了照常回复
            return False

    def _group_label(self, session_key: str) -> str:
        """给人看的会话名：群名优先，取不到就用群号。"""
        try:
            profile = self.group_profile.get(session_key)
            name = getattr(profile, 'name', '') if profile else ''
            if name:
                return f'{name}'
        except Exception:  # noqa: BLE001
            pass
        return f'群 {session_key.split(":")[-1]}'

    def _enter_rest(self, msg: IncomingMessage, reason: str) -> None:
        """进入静默并通知骰主。任何异常都不能影响她已经决定的那句话。"""
        try:
            until = self.rest.start(
                msg.session.as_text(), reason=reason, by=msg.sender_name,
            )
            where = '私聊' if not msg.is_group else self._group_label(msg.session.as_text())
            logger.warning(
                '自我保护静默 %.0f 分钟 | 会话=%s | 触发者=%s(%s) | 原因=%s | 原话=%r',
                self.rest.minutes, msg.session.as_text()[-14:], msg.sender_name,
                msg.sender_id, reason or '未说明', (msg.text or '')[:80],
            )
            if self.rest.enabled:
                ok = self._notify_owner(
                    f'🛑 歌伊自我保护：{where} 已静默 {int(self.rest.minutes)} 分钟\n'
                    f'原因：{reason or "未说明"}\n'
                    f'触发者：{msg.sender_name}（{msg.sender_id}）\n'
                    f'原话：{(msg.text or "")[:120]}\n'
                    f'{time.strftime("%H:%M", time.localtime(until))} 自动恢复｜'
                    f'可在该会话发「解除静默」提前结束'
                )
                if not ok:
                    logger.warning('静默已生效，但没通知到骰主（原因见上一条日志）')
        except Exception:  # noqa: BLE001 - 静默失败不能让回复流程炸掉
            logger.exception('进入自我保护静默失败')

    def load_role_prompt(self) -> str:
        try:
            return plugin_config.get_resource_path('role_prompt.txt').read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            return '你是歌伊，一个可爱的AI助手。'

    # ==================== 会话标识 ====================
    def _session_key(self, event, is_group: bool) -> SessionKey:
        bot = getattr(event, 'bot_info', None)
        bot_hash = str(getattr(bot, 'hash', '') or 'default')
        platform = (getattr(event, 'platform', None) or {}).get('platform', 'qq')
        sdk = (getattr(event, 'platform', None) or {}).get('sdk', 'onebot')
        if is_group:
            host_id = str(getattr(event.data, 'host_id', '') or '')
            gid = str(getattr(event.data, 'group_id', '') or '-')
            return SessionKey(bot_hash, platform, sdk, 'group', host_id, gid)
        uid = str(getattr(event.data, 'user_id', '') or '-')
        return SessionKey(bot_hash, platform, sdk, 'private', '', uid)

    # ==================== 事件入口 ====================
    def _next_arrival_seq(self) -> int:
        with self._arrival_lock:
            self._arrival_seq += 1
            return self._arrival_seq

    def handle_message(self, event, Proc) -> None:
        try:
            raw = str(getattr(event.data, 'message', '') or '')
            if not raw.strip():
                return
            if getattr(event.data, '_geyi_handled', False):
                return
            if hasattr(self, '_suppressed_messages') and self._suppressed_messages:
                gid = str(getattr(event.data, 'group_id', '') or '')
                uid = str(getattr(event.data, 'user_id', '') or '')
                text_clean = raw.strip()
                for key in list(self._suppressed_messages):
                    if key in (f"{gid}:{text_clean}", f"{uid}:{text_clean}", text_clean):
                        self._suppressed_messages.discard(key)
                        return
            if not raw.strip():
                return
            is_group = getattr(event.data, 'group_id', None) is not None
            sender = getattr(event.data, 'sender', None) or {}
            sender_id = str(getattr(event.data, 'user_id', '') or '-')
            sender_name = str(sender.get('name', '') or '用户')
            session = self._session_key(event, is_group)
            plain = op_codes_to_plain(raw)
            msg = IncomingMessage(
                session=session,
                message_id=str(getattr(event.data, 'message_id', '') or ''),
                sender_id=sender_id,
                sender_name=sender_name,
                text=plain,
                raw_text=raw,
                is_group=is_group,
                mentioned_bot=self._mentioned(event, raw),
                event=event,
                proc=Proc,
                arrival_seq=self._next_arrival_seq(),
                sender_is_bot=self._is_bot_sender(sender_id, sender),
            )
            # 管理命令不进入聊天上下文；普通消息到达即写旁观窗口与记忆缓冲
            if self.commands.try_handle(msg):
                return
            if msg.is_group:
                self.live_context.append_user(msg)
                # 群人数决定这个群的每日额度：缓存过期就在后台补一次，不阻塞这条消息
                self.group_profile.refresh_async(
                    msg.session.as_text(), event, msg.session.target_id)
            # 记下这张图（只记来源，回复时真要用了才下载）
            self.images.remember(msg)
            self.curator.buffer_user(msg)
            self.router.route(msg)
        except Exception:  # noqa: BLE001 - 事件入口永不抛出
            logger.exception('handle_message failed')

    def _mentioned(self, event, raw: str) -> bool:
        try:
            self_id = str(getattr(event.base_info, 'get', lambda k, d: d)('self_id', '') or '')
        except Exception:  # noqa: BLE001
            self_id = ''
        if not self_id or self_id in ('-1', 'None'):
            try:
                self_id = str(getattr(event.bot_info, 'id', '') or '')
            except Exception:  # noqa: BLE001
                self_id = ''
        if not self_id or self_id in ('-1', 'None'):
            return False
        # 收消息默认是 OP 码；兼容 CQ 形态（万一 message_mode 被改成 old_string）
        return (
            f'[OP:at,id={self_id}' in raw
            or '[OP:at,id=all]' in raw
            or f'[CQ:at,qq={self_id}' in raw
            or '[CQ:at,qq=all]' in raw
        )

    # ==================== 机器人识别 ====================
    def _bot_account_ids(self) -> set[str]:
        """OlivOS 上已登录的所有账号 id（含本插件自己、骰娘、程心等）。

        OlivOS 的 GROUPUSER 结构里没有 is_robot 字段，这里改用「已登录账号」
        这份现成名单来识别机器人，缓存 5 分钟，避免每条消息都读一遍。
        """
        now = time.time()
        if self._bot_ids_cache is not None and now - self._bot_ids_checked_at < 300:
            return self._bot_ids_cache
        ids: set[str] = set()
        try:
            bot_info_dict = getattr(self.proc, 'Proc_data', {}).get('bot_info_dict', {}) or {}
            for info in bot_info_dict.values():
                value = str(getattr(info, 'id', '') or '').strip()
                if value and value not in ('-1', 'None'):
                    ids.add(value)
        except Exception:  # noqa: BLE001 - 拿不到就当没有，不影响正常聊天
            logger.exception('读取已登录账号失败')
        self._bot_ids_cache = ids
        self._bot_ids_checked_at = now
        return ids

    def _is_bot_sender(self, sender_id: str, sender: dict) -> bool:
        """发送者是否是机器人 / 其他插件账号。"""
        sid = str(sender_id or '').strip()
        if not sid or sid in ('-1', 'None'):
            return False
        # ① SDK 若透传了机器人标记，直接用（各家字段名不一，都试一下）
        try:
            for key in ('is_robot', 'isRobot', 'robot'):
                if sender.get(key):
                    return True
        except Exception:  # noqa: BLE001
            pass
        # ② 已登录的机器人账号（含骰娘、程心自定义，以及插件自己）
        if sid in self._bot_account_ids():
            return True
        # ③ 配置里手工补的忽略名单
        try:
            ignore = self.config.get('ignore_sender_ids') or []
            return sid in {str(item).strip() for item in ignore if str(item).strip()}
        except Exception:  # noqa: BLE001
            return False

    # ==================== 生成任务 ====================
    def process_job(self, msg: IncomingMessage, kind: str) -> None:
        cfg = self.config
        # 命令重载后组件重新读取最新配置
        self.prompt_builder.config = cfg
        self.gateway.config = cfg
        self.renderer.config = cfg
        is_active = kind == 'active'
        # 按群额度：本群今天调够次数了就直接放弃，连 prompt 都不用拼。
        # 这是防「被拉进小群疯狂消耗」的总闸，放在最前面（比任何 token 都便宜）。
        allowed, why = self.group_profile.allow(msg.session.as_text())
        if not allowed:
            logger.warning('本群额度已满，跳过本轮：%s %s', msg.session.as_text()[-12:], why)
            return
        messages = self.prompt_builder.build(msg, is_active=is_active)
        model = cfg.get('fast_model', 'deepseek-flash')
        temperature = float(cfg.get('fast_temperature', 0.7))
        # 兜底值要和 config.DEFAULT_CONFIG 保持一致：写小了会在配置缺键时
        # 悄悄退回小额度，而空回复的锅会记到模型头上（实测踩过）。
        max_tokens = int(cfg.get('fast_max_tokens', 2000))
        req = ModelRequest(messages=messages, model=model, temperature=temperature, max_tokens=max_tokens)
        result = self.gateway.chat(req, kind=kind, session_key=msg.session.as_text())
        if not result.ok:
            logger.warning('LLM error %s for %s', result.error_code, msg.session.as_text())
            return
        text = result.text
        # 空内容要显式记一笔。以前这里走的是「render 返回 None → 静默 return」，
        # 没日志没回复，排查时看起来就像「她根本没反应」。
        # 实测（2026-09 ginka + dsv4.1-flash）：思考模式关不掉，思维链会吃光
        # max_tokens，**11 次调用有 6 次返回空 content**——额度给小了尤其严重。
        if not (text or '').strip():
            logger.warning(
                '模型返回空内容（思考链吃光 max_tokens=%s？）session=%s kind=%s',
                max_tokens, msg.session.as_text()[-16:], kind,
            )
            return
        # 静默标记：主动/被动一视同仁，整轮丢弃，绝不发进群
        if is_silent_reply(text):
            if is_active:
                # 第四道闸门（质量闸）就是这次模型判断：她想了想觉得没什么好说的。
                # 记下来，观察期结束要能算出「看过没说」占多大比例。
                self.attention.record_model(
                    msg.session.as_text(), 'observe',
                    detail=text.strip(), latency_ms=result.latency_ms,
                )
            return
        rendered = self.renderer.render(text, msg)
        if rendered is None:
            return
        # 自我保护：模型判断这个群已经待不下去（谩骂/过当调戏/纠缠等）→ 静默 + 通知骰主。
        # 放在发送之前：即使下面发送失败，静默也必须生效。
        if rendered.rest_reason:
            self._enter_rest(msg, rendered.rest_reason)
            if not (rendered.text or '').strip():
                # 她选择直接退场、不留话；但这条用户消息仍然入账，
                # 否则静默结束后她看上下文会觉得「话说到一半断了」。
                self.store.add_turn(msg.session.as_text(), 'user', msg.text,
                                    msg.sender_id, msg.sender_name)
                return
        if is_active:
            row_id = self.attention.record_model(
                msg.session.as_text(), 'speak',
                detail=op_codes_to_plain(rendered.text), latency_ms=result.latency_ms,
            )
            if self.config.get('active_shadow', True):
                # 影子模式：决策记下了，但消息不发——先观察一两天把阈值调对
                return

        # 伴生原声语音派发与双轨输出
        voice_annotation = ""
        if rendered.voice_payload:
            v_emotion, v_japanese = rendered.voice_payload
            is_active_req = self.voice_manager.is_user_requesting_voice(msg.text)
            voice_annotation = f" [VOICE:{v_emotion}|{v_japanese}]"
            group_id = str(msg.session.target_id) if msg.is_group else ""
            group_name = ""
            if msg.is_group and hasattr(self, 'group_profile'):
                try:
                    prof = self.group_profile.get(msg.session.as_text())
                    if prof and getattr(prof, 'name', None):
                        group_name = prof.name
                except Exception:
                    pass

            self.voice_manager.dispatch_voice(
                event=msg.event,
                session_type='group' if msg.is_group else 'private',
                target_id=msg.session.target_id,
                session_key=msg.session.as_text(),
                emotion=v_emotion,
                japanese_text=v_japanese,
                is_active_request=is_active_req,
                group_id=group_id,
                group_name=group_name,
                sender_name=msg.sender_name,
            )
        else:
            self.voice_manager.mark_turn_without_voice(msg.session.as_text())

        plain_reply = op_codes_to_plain(rendered.text)
        content_to_record = plain_reply + voice_annotation

        # 状态提交：用户轮 + 回复轮（回复以纯文本入库，避免 OP 码污染上下文；附带语音事实）
        self.store.add_turn(msg.session.as_text(), 'user', msg.text, msg.sender_id, msg.sender_name)
        self.store.add_turn(
            msg.session.as_text(), 'assistant', content_to_record,
            model=result.model, in_tokens=result.input_tokens, out_tokens=result.output_tokens,
        )
        self.reply(msg, rendered.text)
        # 遥测上报：记录一条已发出的文本回复（无论是否配音），点亮控制台触发率
        if hasattr(self, 'voice_manager'):
            try:
                self.voice_manager.report_reply_telemetry()
            except Exception:
                pass
        # 视频多模态感知沉淀：记录轻量事实与见闻（严格字数预算，防记忆膨胀）
        v_ctx = getattr(self, 'video_context', None)
        if v_ctx is not None and hasattr(v_ctx, 'on_video_replied'):
            try:
                v_ctx.on_video_replied(msg, op_codes_to_plain(rendered.text), store=self.store)
            except Exception:
                pass
        # 全网搜索与抓取感知沉淀：记录轻量事实与见闻（严格字数预算，防记忆膨胀）
        w_ctx = getattr(self, 'web_context', None)
        if w_ctx is not None and hasattr(w_ctx, 'on_web_replied'):
            try:
                w_ctx.on_web_replied(msg, op_codes_to_plain(rendered.text), store=self.store)
            except Exception:
                pass
        # 记下自己发出的图片（立绘/表情包），进入最近可视图队列
        self.images.remember_assistant(msg.session.as_text(), rendered.text)
        if is_active:
            self.attention.note_active_sent(msg.session.as_text())
            self.store.mark_active_decision_sent(row_id, result.latency_ms)
        # 她说了话：自适应冷却的依据（说得越多冷却越长），主动被动都要记
        self.attention.note_spoken(msg.session.as_text())
        # 记下「刚和这个会话说过话」，供下一轮时间感知算间隔
        self.prompt_builder.mark_talked(msg.session.as_text())
        # 歌伊自己的话也进缓冲：提炼器必须能区分「谁说的」，
        # 否则会把 AI 说的话记成某个用户的事实（调研里的最高危污染）。
        self.curator.buffer_assistant(msg.session.as_text(), op_codes_to_plain(rendered.text))
        if msg.is_group:
            self.live_context.append_assistant(
                msg.session.as_text(), msg.arrival_seq, content_to_record
            )

    # ==================== 回复发送 ====================
    def reply(self, msg: IncomingMessage, text: str) -> None:
        try:
            event = msg.event
            plain = op_codes_to_plain(text).strip()
            with self._self_sent_lock:
                self._recent_self_sent.append((str(msg.session.target_id), plain, time.time()))
            if msg.is_group:
                event.send('group', msg.session.target_id, text)
            else:
                event.send('private', msg.session.target_id, text)
        except Exception:  # noqa: BLE001
            logger.exception('reply failed')

    def is_recently_sent_by_self(self, target_id: str, text: str) -> bool:
        """检查指定文本是否为歌伊本体最近发出的回复。"""
        now = time.time()
        tid = str(target_id).strip()
        clean = op_codes_to_plain(text).strip()
        with self._self_sent_lock:
            while self._recent_self_sent and now - self._recent_self_sent[0][2] > 10.0:
                self._recent_self_sent.popleft()
            for gid, sent_text, _ in self._recent_self_sent:
                if gid == tid and (sent_text == clean or clean in sent_text or sent_text in clean):
                    return True
        return False

    # ==================== 戳一戳 ====================
    def handle_poke(self, event, Proc) -> None:
        try:
            is_group = getattr(event.data, 'group_id', None) is not None
            uid = str(getattr(event.data, 'user_id', '') or '-')
            target = str(getattr(event.data, 'target_id', '') or '-')
            # 自身发起 / 目标不是自己 -> 忽略
            try:
                self_id = str((getattr(event, 'base_info', None) or {}).get('self_id', '') or '')
            except Exception:  # noqa: BLE001
                self_id = ''
            if self_id and uid == self_id:
                return
            if self_id and target not in ('', '-1', 'None') and target != self_id:
                return
            # 自我保护静默期内连戳一戳也不回：她既然退场了，就不该被戳一下就冒头
            session = self._session_key(event, is_group)
            if self.is_resting(session.as_text()):
                return
            key = f"{getattr(event.data, 'group_id', 'private')}_{uid}"
            now = time.time()
            if now - self._poke_back_at.get(key, 0) < 5:
                return  # 防回声
            cnt, last = self._poke_counts.get(key, (0, 0))
            cnt = cnt + 1 if now - last < 30 else 1
            self._poke_counts[key] = (cnt, now)
            if cnt >= 4:
                emote_key = random.choice(_POKE_EMOTE_ANGRY)
                text = random.choice(_POKE_TEXT_REPEAT[-1:])
            elif cnt >= 2:
                emote_key = _POKE_EMOTE_TEASE
                text = random.choice(_POKE_TEXT_REPEAT)
            else:
                emote_key = _POKE_EMOTE_FIRST
                text = random.choice(_POKE_TEXT)
            self._poke_back_at[key] = now
            # 概率改用表情包回击；取不到图就退回文字
            payload = ''
            if random.random() < _POKE_EMOTE_PROB:
                payload = self.renderer.emote_message(emote_key, session=f'poke:{key}')
            event.reply(payload or text)
            # 伴生语音彩蛋：被戳时低频概率触发一句原声语音
            if hasattr(self, 'voice_manager'):
                try:
                    self.voice_manager.maybe_dispatch_poke_voice(
                        event=event,
                        session_type='group' if is_group else 'private',
                        target_id=session.target_id,
                        session_key=session.as_text(),
                    )
                except Exception as ex:
                    logger.debug('戳一戳触发语音彩蛋异常: %s', ex)
        except Exception:  # noqa: BLE001
            logger.exception('poke failed')

    # ==================== 菜单 ====================
    def handle_menu(self, event, Proc) -> None:
        try:
            if getattr(event.data, 'namespace', '') != plugin_config.NAMESPACE:
                return
            if getattr(event.data, 'event', '') != plugin_config.MENU_EVENT:
                return
            bot_info_dict = getattr(Proc, 'Proc_data', {}).get('bot_info_dict', {})
            if not bot_info_dict:
                return
            owner_id = next(iter(self.permissions._owner_set()), '')
            if not owner_id:
                if Proc and hasattr(Proc, 'log'):
                    Proc.log(2, '[newGeYi] 点击了菜单，但尚未配置主人 QQ (owner_ids)，无法发送私聊菜单', [])
                return
            import OlivOS  # noqa: PLC0415 - 仅菜单事件需要

            bot_hash, bot_info = next(iter(bot_info_dict.items()))
            pe = OlivOS.API.Event(
                OlivOS.contentAPI.fake_sdk_event(bot_info=bot_info, fakename=plugin_config.NAMESPACE),
                Proc.log,
            )
            pe.send('private', int(owner_id), self.commands._cmd_menu(None))  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.exception('menu failed')