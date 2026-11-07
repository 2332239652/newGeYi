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
import re
import threading
import time
import traceback

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
from ..relationship.service import RelationshipService
from ..relationship.stages import audit_overstep
from ..routing.attention import AttentionGate
from ..routing.commands import CommandRouter
from ..routing.message_router import MessageRouter
from ..routing.permissions import Permissions
from ..storage.database import SQLiteStore
from ..voice.manager import VoiceManager
from .rest import RestGuard
from .private_gate import PrivateGate
from .scheduler import SessionScheduler
from .sleep import SleepWindow

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

    ⚠️ 2026-11-01 修正：`Proc.log` 的档位在本机 OlivOS（0.11.81）上**只有 2 会落到文件**
    （2 → `[INFO]`）；0/1/3 这些档位实测一条都不会写进日志。原来这里把 WARNING 映射成 1，
    于是「额度用完 / 模型报错 / 会话任务失败 / 配置缺键」这些**最该看见的告警全被静默吞掉**——
    翻遍 2026-09 至今的全部日志，newGeYi 的 WARN/ERROR 行数为 0，就是这么来的。
    现在一律用档位 2，把级别和异常栈写进正文：宁可多一行前缀，也不能再让报错无声消失。
    """

    LEVEL_TAGS = {
        logging.DEBUG: '[DEBUG] ',
        logging.INFO: '',
        logging.WARNING: '[WARN] ',
        logging.ERROR: '[ERROR] ',
        logging.CRITICAL: '[CRIT] ',
    }

    def __init__(self, runtime):
        super().__init__(level=logging.INFO)
        self.runtime = runtime

    def emit(self, record):
        try:
            name = record.name.split('.')[-1]
            # INFO 保持原格式（`[newGeYi] [router] …`，老日志里的 grep 还认它）；
            # WARNING 及以上加标记，一眼能看出「这行是告警」。
            tag = self.LEVEL_TAGS.get(record.levelno, '[WARN] ')
            text = '[%s] %s%s' % (name, tag, record.getMessage())
            if record.exc_info:
                # logger.exception 的价值全在栈里；以前只转发 getMessage()，栈直接丢了
                text += '\n' + ''.join(traceback.format_exception(*record.exc_info)).rstrip()
            self.runtime.log(2, text)
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
        # 私聊闸门：触发词 / 防连发 / 每日限额（并发保护，见 runtime/private_gate.py）
        self.private_gate = PrivateGate(self.store, self.config)
        # 作息：睡前提醒 + 睡着时缄默（见 runtime/sleep.py）
        self.sleep = SleepWindow(self.store, self.config)
        self.prompt_builder = PromptBuilder(
            self.config, self.store, self.memory, self.worldbook,
            self.live_context, self.images, self.renderer,
        )
        self.prompt_builder.voice_manager = self.voice_manager
        self.prompt_builder.sleep_window = self.sleep
        # 私聊 galgame：好感度 / 关系阶段（总开关 galgame_enabled，默认关）
        self.relationship = RelationshipService(
            self.config, self.store, self.gateway, self.worldbook, self.permissions)
        self.prompt_builder.relationship = self.relationship
        self.curator.relationship = self.relationship
        self.prompt_builder.set_role_prompt(self.load_role_prompt())
        self.commands = CommandRouter(self)
        self.router = MessageRouter(self)
        self.scheduler = SessionScheduler(self)
        self._poke_counts: dict[str, tuple[int, float]] = {}
        self._poke_back_at: dict[str, float] = {}
        self.video_context = None
        self.web_context = None
        self._self_sent_lock = threading.Lock()
        self._recent_self_sent: deque[tuple[str, str, float]] = deque(maxlen=40)
        self.cross_plugin_observer = None
        self._arrival_lock = threading.Lock()
        self._arrival_seq = 0
        self._start_time = time.time()
        # 已登录机器人账号名单（骰娘/程心等）的短期缓存，用于识别机器人消息
        self._bot_ids_cache: set[str] | None = None
        self._bot_ids_checked_at = 0.0

    # ==================== 配置热更新 ====================
    # 约定：**self.config 这个字典对象的 identity 永不改变**。
    # 以前每个开关命令都 `self.r.config = load_config()`，换的只是这一个属性，
    # 而 attention / permissions / curator 等组件怀里抱的还是启动时那份字典
    # —— 于是命令回「✅」、行为却不变（第二次开关尤其明显）。见交接文档 §28。
    _CFG_HOLDERS = ('prompt_builder', 'gateway', 'renderer', 'curator', 'images',
                    'attention', 'permissions', 'group_profile', 'rest', 'memory', 'relationship')

    def apply_config(self) -> dict:
        """把当前 self.config 推给所有缓存了配置的组件。"""
        for name in self._CFG_HOLDERS:
            comp = getattr(self, name, None)
            if comp is not None and hasattr(comp, 'config'):
                comp.config = self.config
        voice = getattr(self, 'voice_manager', None)
        if voice is not None:
            try:
                voice.reload(self.config)
            except Exception:  # noqa: BLE001 - 语音配置坏了不该挡住其它热更新
                logger.exception('重载语音配置失败')
        return self.config

    def reload_config(self) -> str:
        """重新读盘并**原地**更新配置，返回人话错误说明（空串 = 成功）。"""
        fresh = plugin_config.load_config()
        error = plugin_config.config_load_error()
        self.config.clear()
        self.config.update(fresh)
        self.apply_config()
        if error:
            return ('⚠ 配置文件解析失败：%s\n'
                    '本次按默认值运行，磁盘上的文件**没有被覆盖**（已另存 .broken 备份）。'
                    '改好 JSON 再发「重载设定」。' % error)
        return ''

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
            # 两个版本号打架时最难排查（2026-10-30 就发生过：app.json 是 2.6.0、
            # config.PLUGIN_VERSION 还停在 2.5.0，日志和状态里全是旧版本号）
            mismatch = plugin_config.version_mismatch()
            if mismatch:
                self.log(1, f'⚠ 版本号不一致：{mismatch}（发布、排查时容易对不上）')
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
        # 资源目录自愈：本机 OlivOS 的 release_data 每次加载都失败（WinError 183），
        # data/ 会被清空却不再回填（见 config.sync_released_data 的注释）。
        # 必须放在 init_after：release 恰好发生在 init 之后、init_after 之前。
        try:
            copied = plugin_config.sync_released_data(force=True)
            if copied:
                shown = '、'.join(copied[:5]) + ('…' if len(copied) > 5 else '')
                self.log(2, f'资源目录自愈：从插件包补齐 {len(copied)} 个文件（{shown}）')
            else:
                self.log(2, '资源目录自愈：资源目录完整，无需补齐')
        except Exception:  # noqa: BLE001 - 补齐失败不能让插件起不来
            logger.exception('补齐资源目录失败')
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
            self.relationship.shutdown()
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
        # 还原猴补丁：跨插件钩子是直接替换别人模块属性，不还原就会随重载层层嵌套
        try:
            if self.cross_plugin_observer is not None:
                self.cross_plugin_observer.detach()
        except Exception:  # noqa: BLE001
            logger.exception('卸载跨插件感知钩子失败')
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

    # OlivOS 的 Proc.log 档位 → 正文前缀。档位本身不再往上传（只有 2 会落盘）。
    _LOG_PREFIX = {0: '[DEBUG] ', 1: '[WARN] ', 2: '', 3: '[ERROR] '}

    def log(self, level: int, text: str) -> None:
        """写一行 OlivOS 日志。档位一律用 2——本机 OlivOS 只把 2 落进日志文件。"""
        try:
            prefix = self._LOG_PREFIX.get(int(level), '')
        except (TypeError, ValueError):
            prefix = ''
        try:
            if self.proc is not None and hasattr(self.proc, 'log'):
                self.proc.log(2, f'[newGeYi] {prefix}{text}', [])
                return
        except Exception:  # noqa: BLE001
            pass
        logger.info('%s%s', prefix, text)

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

    # ==================== 作息（休眠）====================
    def send_sleep_notice(self, msg: IncomingMessage) -> None:
        """睡着时被叫醒的那一句。

        **不调模型**：一句话的成本没必要花，而且半夜的模型输出不可控。
        走 `_track_self_sent` 补上账本（她说过什么 / turns / 旁观窗口），
        否则醒来会一脸茫然地"不记得自己回过"。
        """
        try:
            text = self.sleep.notice_text()
        except Exception:  # noqa: BLE001 - 配置脏了也要能回一句
            logger.exception('读取休眠提示文案失败')
            text = '（迷迷糊糊翻了个身）唔……已经睡下了……明天再说嘛……'
        # 这句不走 process_job，所以私聊的用户轮要在这里补记：
        # 否则历史里会出现一条「她回了句睡下了」但前面没有对应提问的孤儿助手轮。
        self._remember_user_turn(msg)
        self.reply(msg, text)
        try:
            self._track_self_sent(msg.session, text, msg.is_group)
        except Exception:  # noqa: BLE001 - 记账失败不影响已经发出去的那句话
            logger.debug('休眠提示写入账本失败', exc_info=True)

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
        """进入静默并通知骰主。任何异常都不能影响她已经决定的那句话。

        私聊 → **无限期**（2026-11-04 起，`rest_private_forever`）：一对一的
        骚扰是"同一个人一直追着说"，等她 10 分钟后自动回来，同一套话术会立刻
        再来一遍；所以私聊的退场只能由骰主发「解除静默」放出来。群里是公共
        场合，照旧 N 分钟后自动恢复。
        """
        try:
            session_key = msg.session.as_text()
            forever = bool(self.rest.private_forever) and not msg.is_group
            until = self.rest.start(
                session_key, reason=reason, by=msg.sender_name, forever=forever,
            )
            where = '私聊' if not msg.is_group else self._group_label(session_key)
            logger.warning(
                '自我保护静默 %s | 会话=%s | 触发者=%s(%s) | 原因=%s | 原话=%r',
                '无限期（私聊）' if forever else '%.0f 分钟' % self.rest.minutes,
                session_key[-14:], msg.sender_name,
                msg.sender_id, reason or '未说明', (msg.text or '')[:80],
            )
            if self.rest.enabled:
                if forever:
                    tail = '无限期静默（私聊），需在该会话发「解除静默」才会恢复'
                else:
                    tail = (f'{time.strftime("%H:%M", time.localtime(until))} 自动恢复｜'
                            f'可在该会话发「解除静默」提前结束')
                ok = self._notify_owner(
                    f'🛑 歌伊自我保护：{where} 已静默'
                    f'{"（无限期）" if forever else " %d 分钟" % int(self.rest.minutes)}\n'
                    f'原因：{reason or "未说明"}\n'
                    f'触发者：{msg.sender_name}（{msg.sender_id}）\n'
                    f'原话：{(msg.text or "")[:120]}\n'
                    f'{tail}'
                )
                if not ok:
                    logger.warning('静默已生效，但没通知到骰主（原因见上一条日志）')
        except Exception:  # noqa: BLE001 - 静默失败不能让回复流程炸掉
            logger.exception('进入自我保护静默失败')

    def load_role_prompt(self) -> str:
        """角色设定：**先读状态目录里的网页覆盖**，没有再读包内出厂文件。

        为什么覆盖放 state 而不是写回 `data/role_prompt.txt`：插件包在升级/重载时会被
        整体替换，包内文件也可能只读；而 state 目录是线上数据的家，重载后应该继续生效。
        （网页编辑器写的就是 `role_prompt.webui.txt`，见 panel.py。）
        """
        try:
            override = plugin_config.get_state_dir() / 'role_prompt.webui.txt'
            path = override if override.is_file() else plugin_config.get_resource_path('role_prompt.txt')
            return path.read_text(encoding='utf-8').strip()
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
            if not raw.strip():
                return
            is_group = getattr(event.data, 'group_id', None) is not None
            sender = getattr(event.data, 'sender', None) or {}
            sender_id = str(getattr(event.data, 'user_id', '') or '-')
            sender_name = str(sender.get('name', '') or '用户')
            session = self._session_key(event, is_group)
            # @码 → @名字：少数协议端不带 name=，这时至少把「@她自己」说成 @你，
            # 别让模型看到一串数字（2026-11-04：她学会了写【@2309394954】）。
            try:
                self_id = str((getattr(event, 'base_info', None) or {}).get('self_id', '') or '')
            except Exception:  # noqa: BLE001 - base_info 结构变了就当没有
                self_id = ''
            at_names = {sender_id: sender_name}
            if self_id:
                at_names.setdefault(self_id, '你')
            plain = op_codes_to_plain(raw, names=at_names)
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
            # 只有「会进记忆」的会话才写缓冲：私聊默认不进（隐私），
            # 记忆/提炼关掉时也不写（否则缓冲只增不减，而且会被提炼成记忆）。
            if self._memory_tracking_on(msg):
                self.curator.buffer_user(msg)
            self.router.route(msg)
        except Exception:  # noqa: BLE001 - 事件入口永不抛出
            logger.exception('handle_message failed')

    def _memory_tracking_on(self, msg: IncomingMessage) -> bool:
        """这条消息该不该进记忆缓冲。私聊与群聊均独立缓冲和提炼。"""
        cfg = self.config
        if not cfg.get('memory_enabled', True):
            return False
        if not cfg.get('memory_curator_enabled', True):
            return False
        return True

    # 收消息里的 @：OP 与 CQ 两种形态，取方括号里的 id
    AT_ID_RE = re.compile(r'\[(?:OP:at,id|CQ:at,qq)=([^,\]]+)')

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
        # 用**精确 id 比较**：以前是 `f'[OP:at,id={self_id}' in raw` 的前缀匹配，
        # 于是机器人 123456789 会被 `[OP:at,id=1234567890]`（@别人）命中。
        ids = {match.group(1).strip() for match in self.AT_ID_RE.finditer(raw or '')}
        if self_id in ids:
            return True
        # @全体成员 默认**不算**点名她（那是在叫所有人，不是跟她说话）；
        # 想让她被 @全体 也叫醒就打开 mention_on_at_all。
        return 'all' in ids and bool(self.config.get('mention_on_at_all', False))

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
    def _ask(self, msg: IncomingMessage, kind: str, level: int, cfg: dict):
        """拼上下文 + 调一次模型。返回 (result, text)；失败返回 (None, '')。

        `level` 只对主动搭话有意义（0 严格 / 1 中性 / 2 软化），被动回复恒为 0。
        """
        try:
            messages = self.prompt_builder.build(
                msg, is_active=(kind == 'active'), active_level=level)
        except Exception:  # noqa: BLE001 - 拼上下文失败只该丢这一条，不该让会话队列陪葬
            logger.exception('拼 prompt 失败（kind=%s, level=%s）| %s',
                             kind, level, msg.session.as_text()[-16:])
            return None, ''
        # 兜底值要和 config.DEFAULT_CONFIG 保持一致：写小了会在配置缺键时
        # 悄悄退回小额度，而空回复的锅会记到模型头上（实测踩过）。
        req = ModelRequest(
            messages=messages,
            model=cfg.get('fast_model', 'gemini-3.8-flash-medium'),
            temperature=float(cfg.get('fast_temperature', 0.7)),
            max_tokens=int(cfg.get('fast_max_tokens', 8192)),
        )
        # 网关本身吞掉了网络异常，但「拼请求体」等步骤在 try 外面，以前一抛就直接
        # 冒到 _drain 的 except 里，整条栈又进不了日志 —— 线上就是「没有 api_calls、
        # 没有回复、也没有任何报错」。这里兜住并留痕。
        try:
            result = self.gateway.chat(req, kind=kind, session_key=msg.session.as_text())
        except Exception:  # noqa: BLE001
            logger.exception('网关调用抛异常（kind=%s, level=%s）| %s',
                             kind, level, msg.session.as_text()[-16:])
            return None, ''
        return result, (result.text or '')

    def _active_cycle(self, msg: IncomingMessage, kind: str, cfg: dict):
        """主动搭话的一个周期：逐级加强，最多试 `active_burst_max_attempts` 次。

        为什么要在**同一个周期里**连试几档，而不是被拒了等下一轮：
        模型拒的是「这条消息」，等五分钟换条新消息再问，等于重新掷骰——钱花了，
        但拿不到「换一种说法她肯不肯」这个信息。同一段上下文里升级，才有信息量。
        这也是省钱的关键：v2.8.7 是 11 次调用换 1 句，这里是 2~3 次。

        返回 (result, text, level)；整轮被拒返回 (None, '', -1)。
        """
        session = msg.session.as_text()
        plan = self.attention.pending_plan(session)
        levels = list(plan.levels) or [0]
        logger.info('主动周期 %s | %s | 欠账 %.2f 干旱 %d 兑现率 %.2f',
                    plan.text(), session[-16:], plan.deficit, plan.drought,
                    self.attention.conversion(session))
        started = time.time()
        budget = float(cfg.get('active_burst_max_seconds', 12) or 0)
        for index, level in enumerate(levels):
            # 时间预算：这个群是**每会话串行**的，一条被动回复会排在整轮升级后面。
            # 试到一半超时就直接收手，宁可这一轮不说，也不能让 @她 的人等十几秒。
            if budget > 0 and index > 0 and time.time() - started > budget:
                logger.info('主动周期超时（%.1fs > %.0fs），放弃剩余档位 | %s',
                            time.time() - started, budget, session[-16:])
                break
            # 每次调用前都过一遍本群额度：连试三次等于三次真实调用，不能只查一次
            allowed, why = self.group_profile.allow(session)
            if not allowed:
                logger.warning('本群额度已满，本轮升级中断：%s %s', session[-12:], why)
                break
            result, text = self._ask(msg, kind, level, cfg)
            if result is None or not result.ok:
                if result is not None:
                    logger.warning('LLM error %s for %s（kind=%s, L%s）',
                                   result.error_code, session[-16:], kind, level)
                # 网络/额度错误不是「她不想说」，不记档位、也不再往上加码
                self.attention.end_cycle(session, False)
                return None, '', -1
            if not text.strip():
                logger.warning(
                    '模型返回空内容（思考链吃光 max_tokens=%s？）session=%s kind=%s L%s',
                    cfg.get('fast_max_tokens', 8192), session[-16:], kind, level)
                self.attention.note_attempt(session, level, False, result.model)
                continue
            if is_silent_reply(text):
                # 这个档位被拒：记档位（算它的真实接受率），再往上加一档
                self.attention.note_attempt(session, level, False, result.model)
                self.attention.record_model(
                    session, 'observe', detail=text.strip(),
                    latency_ms=result.latency_ms, level=level)
                logger.info('主动搭话 L%s 被拒 | %s', level, session[-16:])
                continue
            self.attention.note_attempt(session, level, True, result.model)
            # 周期兑现了就在这里结账：即使后面被渲染层拦下（计划泄露/审计命中），
            # 「模型这次肯开口」这个事实也该算进兑现率，否则会被误判成挑食。
            self.attention.end_cycle(session, True)
            return result, text, level
        self.attention.end_cycle(session, False)
        logger.info('主动周期整轮被拒，本轮不开口 | %s', session[-16:])
        return None, '', -1

    def _remember_user_turn(self, msg: IncomingMessage) -> None:
        """把用户这句话先记进 turns，**只对私聊**。

        为什么只对私聊：群聊上下文来自 live_context（消息一到就写），这里再写会重复；
        而私聊的上下文**只**来自 turns。以前所有早退分支（模型报错 / 空内容 /
        静默 / 渲染被拦 / 本轮在静默期）一句都不写，于是她「完全不记得对方说过什么」，
        下一句还会答非所问。先记账再生成，任何分支都不会丢。
        """
        if msg.is_group:
            return
        try:
            self.store.add_turn(msg.session.as_text(), 'user', msg.text,
                                msg.sender_id, msg.sender_name)
        except Exception:  # noqa: BLE001 - 记账失败不该挡住回复
            logger.exception('记录私聊用户轮失败')

    def _retry_if_overstep(self, msg: IncomingMessage, kind: str, cfg: dict, rendered, result):
        """回复比当前关系阶段亲密得多 → 带"重写要求"再问一次。

        返回 (result, text, rendered) 表示换用重写后的版本；None 表示保持原回复
        （没越界 / 重写也失败 / 重写后仍越界——这种情况只记日志，不拦截，
        拦了就是"她不回话"，比多说一句亲昵的话更糟）。
        """
        try:
            if not self.relationship.active_for(msg):
                return None
            stage = self.relationship.stage_for(msg)
            if stage is None:
                return None
            hit = audit_overstep(op_codes_to_plain(rendered.text), stage)
            if not hit:
                return None
            session = msg.session.as_text()
            logger.warning('私聊回复越界（阶段 %s，命中「%s」），要求重写 | %s', stage, hit, session[-16:])
            self.prompt_builder.overstep_hint[session] = hit
            try:
                result2, text2 = self._ask(msg, kind, 0, cfg)
            finally:
                self.prompt_builder.overstep_hint.pop(session, None)
            if result2 is None or not result2.ok or not (text2 or '').strip() or is_silent_reply(text2):
                return None
            rendered2 = self.renderer.render(text2, msg)
            if rendered2 is None or not rendered2.text:
                return None
            if audit_overstep(op_codes_to_plain(rendered2.text), stage):
                logger.warning('重写后仍然越界，保留原回复 | %s', session[-16:])
                return None
            return result2, text2, rendered2
        except Exception:  # noqa: BLE001
            logger.exception('越界重写失败，保留原回复')
            return None

    def process_job(self, msg: IncomingMessage, kind: str) -> None:
        cfg = self.config
        # 命令重载后组件重新读取最新配置
        self.prompt_builder.config = cfg
        self.gateway.config = cfg
        self.renderer.config = cfg
        is_active = kind == 'active'
        session_key = msg.session.as_text()
        # 私聊先记账：后面有六条早退分支，以前每一条都会把她这句话弄丢
        remember_user = not msg.is_group
        if remember_user:
            self._remember_user_turn(msg)
        # 静默期内不回复：以前只有 router 拦住了**新到**的消息，
        # 队列里已经排着的任务照样落地（静默刚生效时最明显）。
        is_resting = getattr(self, 'is_resting', None)
        if callable(is_resting) and is_resting(session_key):
            logger.info('本会话处于自我保护静默，丢弃排队任务（kind=%s）| %s',
                        kind, session_key[-16:])
            return
        # 按群额度：本群今天调够次数了就直接放弃，连 prompt 都不用拼。
        # 这是防「被拉进小群疯狂消耗」的总闸，放在最前面（比任何 token 都便宜）。
        allowed, why = self.group_profile.allow(session_key)
        if not allowed:
            logger.warning('本群额度已满，跳过本轮：%s %s', session_key[-12:], why)
            return
        # 主动搭话的去留已经在路由层由打分闸门决定了（见 routing/attention.py），
        # 这里只按升级计划逐级尝试；被动回复照旧一次。
        spoken_level = 0
        if is_active:
            result, text, spoken_level = self._active_cycle(msg, kind, cfg)
            if result is None:
                return
        else:
            result, text = self._ask(msg, kind, 0, cfg)
            if result is None:
                return
            if not result.ok:
                logger.warning('LLM error %s for %s（kind=%s）',
                               result.error_code, msg.session.as_text()[-16:], kind)
                return
            if not (text or '').strip():
                # 空内容要显式记一笔。以前这里走的是「render 返回 None → 静默 return」，
                # 没日志没回复，排查时看起来就像「她根本没反应」。
                # 实测（2026-09 ginka + dsv4.1-flash）：思考模式关不掉，思维链会吃光
                # max_tokens，**11 次调用有 6 次返回空 content**——额度给小了尤其严重。
                logger.warning(
                    '模型返回空内容（思考链吃光 max_tokens=%s？）session=%s kind=%s',
                    cfg.get('fast_max_tokens', 8192), msg.session.as_text()[-16:], kind,
                )
                return
        # 静默标记：主动/被动一视同仁，整轮丢弃，绝不发进群
        if is_silent_reply(text):
            if not is_active:
                # 被动还选择不说话是反常的：有人叫她，她却回了静默标记。
                logger.warning('被动回复被模型判定为静默（有人叫她却不答）| %s | %r',
                               msg.session.as_text()[-16:], text.strip()[:60])
            return
        rendered = self.renderer.render(text, msg)
        if rendered is None:
            logger.warning('渲染层判定整条不发（审计命中或正文被清空）| %s | %r',
                           msg.session.as_text()[-16:], (text or '')[:80])
            return
        # 私聊关系越界审计：阶段不够却说了过分亲密的话 → 让她按阶段重写一次（最多一次）
        if not msg.is_group and not is_active and rendered.text:
            retried = self._retry_if_overstep(msg, kind, cfg, rendered, result)
            if retried is not None:
                result, text, rendered = retried
        # 自我保护：模型判断这个群已经待不下去（谩骂/过当调戏/纠缠等）→ 静默 + 通知骰主。
        # 放在发送之前：即使下面发送失败，静默也必须生效。
        if rendered.rest_reason:
            self._enter_rest(msg, rendered.rest_reason)
            if not (rendered.text or '').strip():
                # 她选择直接退场、不留话；但这条用户消息仍然入账，
                # 否则静默结束后她看上下文会觉得「话说到一半断了」。
                if not remember_user:
                    self.store.add_turn(msg.session.as_text(), 'user', msg.text,
                                        msg.sender_id, msg.sender_name)
                return
        if is_active:
            row_id = self.attention.record_model(
                msg.session.as_text(), 'speak',
                detail=op_codes_to_plain(rendered.text,
                                         names={msg.sender_id: msg.sender_name}),
                latency_ms=result.latency_ms,
                level=spoken_level,
            )
            if self.config.get('active_shadow', True):
                # 影子模式：决策记下了，但消息不发——先观察一两天把阈值调对
                logger.info('影子模式：本次主动搭话只记账不发（L%s）| %s',
                            spoken_level, msg.session.as_text()[-16:])
                return

        # 伴生原声语音派发与双轨输出
        voice_annotation = ""
        if rendered.voice_payload:
            v_emotion, v_japanese = rendered.voice_payload
            is_active_req = self.voice_manager.is_user_requesting_voice(msg.text)
            group_id = str(msg.session.target_id) if msg.is_group else ""
            group_name = ""
            if msg.is_group and hasattr(self, 'group_profile'):
                try:
                    prof = self.group_profile.get(msg.session.as_text())
                    if prof and getattr(prof, 'name', None):
                        group_name = prof.name
                except Exception:
                    pass

            queued = self.voice_manager.dispatch_voice(
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
            # 只有**真的排上了**才写进历史。以前不管发没发出去都写 [VOICE:…]，
            # 于是网关离线或冷却拦下时，她之后会「记得」自己发过一条根本不存在的语音。
            if queued:
                voice_annotation = f" [VOICE:{v_emotion}|{v_japanese}]"
            else:
                logger.info('语音未发出（冷却/熔断/关闭），本次不写进历史：%s',
                            msg.session.as_text()[-16:])
        else:
            self.voice_manager.mark_turn_without_voice(msg.session.as_text())

        plain_reply = op_codes_to_plain(rendered.text, names={msg.sender_id: msg.sender_name})
        content_to_record = plain_reply + voice_annotation

        # 状态提交：用户轮 + 回复轮（回复以纯文本入库，避免 OP 码污染上下文；附带语音事实）
        # 私聊的用户轮已经在 process_job 开头记过了（保证早退分支也不丢）。
        if not remember_user:
            self.store.add_turn(msg.session.as_text(), 'user', msg.text,
                                msg.sender_id, msg.sender_name)
        self.store.add_turn(
            msg.session.as_text(), 'assistant', content_to_record,
            model=result.model, in_tokens=result.input_tokens, out_tokens=result.output_tokens,
        )
        self.reply(msg, rendered.text)
        # 私聊 galgame：回复发出后异步评审（不增加回复延迟，结果影响下一轮）
        if not msg.is_group:
            try:
                self.relationship.after_reply(msg, plain_reply)
            except Exception:  # noqa: BLE001 - 评审再出事也不能影响聊天
                logger.exception('触发关系评审失败')
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
        # ⚠️ 必须和用户消息用**同一个门**：私聊开关关掉 / 记忆关掉时，这里要是照写，
        # 那个会话就只剩「她自己的话」的缓冲，而 should_run 又永远不会去提炼它
        # → 越攒越多且没人清（2026-11-04 复查发现）。
        if self._memory_tracking_on(msg):
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

    def _track_self_sent(self, session, text: str, is_group: bool) -> None:
        """把她自己发出的一句话记进「她说过什么」的账本。

        正常回复走 reply() + process_job 那套（去重缓存 / turns / 旁观窗口）；
        戳一戳回复、戳一戳语音彩蛋这类**不经过消息管道**的输出以前漏记，后果是：
        ① `_recent_self_sent` 里没有它 → 出站钩子会把自己说的话当成别人说的记下来；
        ② 她自己不记得刚回过这句，群友接着追问会一脸茫然。

        这里只补**去重缓存 + turns + 旁观窗口**，**不写** curator 缓冲——
        戳一戳回的是模板句，进了提炼器只会污染长期记忆。
        """
        plain = op_codes_to_plain(text).strip()
        if not plain:
            return
        try:
            with self._self_sent_lock:
                self._recent_self_sent.append((str(session.target_id), plain, time.time()))
        except Exception:  # noqa: BLE001 - 记账失败不能影响已经发出去的消息
            pass
        try:
            self.store.add_turn(session.as_text(), 'assistant', plain)
        except Exception:  # noqa: BLE001
            logger.debug('戳一戳回复写入 turns 失败', exc_info=True)
        if is_group:
            try:
                self.live_context.append_assistant(
                    session.as_text(), self._next_arrival_seq(), plain)
            except Exception:  # noqa: BLE001
                logger.debug('戳一戳回复写入旁观窗口失败', exc_info=True)

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
            out = payload or text
            event.reply(out)
            # 记账：她要记得自己刚戳回去说过什么（详见 _track_self_sent）
            self._track_self_sent(session, out, is_group)
            # 万一回的是表情图，也进「她刚发过的图」，群友追问时能认出来
            try:
                self.images.remember_assistant(session.as_text(), out)
            except Exception:  # noqa: BLE001
                logger.debug('戳一戳表情图入库失败', exc_info=True)
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