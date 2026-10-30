# -*- encoding: utf-8 -*-
"""静态配置、路径和默认值。"""

from __future__ import annotations

import copy
import datetime
import json
import os
import shutil
import time
from pathlib import Path

PLUGIN_NAME = 'newGeYi'
NAMESPACE = 'newGeYi'
MENU_EVENT = 'newGeYi_Menu_001'
DATA_DIR_NAME = 'newGeYi'
# 插件版本：只在这一处维护，日志 / 菜单 / 状态都引用它
# （app.json 的 version 必须跟它一致 —— 有 `tests/test_config.py` 锁着：
#   `python -m unittest discover -s newGeYi/tests -t .`，两个版本号打架时最难排查）
# 2026-10-30：上一轮发了 v2.6.0 但只改了 app.json，这里还留在 2.5.0，
# 于是日志/「歌伊状态」全都是 v2.5.0，和 GitHub 上的 tag 对不上。
# 现在启动时会主动比对两个版本号，不一致就打 warning（见 version_mismatch）。
PLUGIN_VERSION = '2.6.5'

# 主动搭话的“无事可说不回复”标记（模型只需输出这一行即表示忽略）
SKIP_ACTIVE = '【歌伊忽略】'


def code_stamp() -> str:
    """插件包里最新的 .py 修改时间，只用于启动日志。

    为什么要这个：OlivOS 的「重载插件」是**重启插件进程**（pluginAPI.shallow 跑在
    独立的 multiprocessing.Process 里，enable_auto_restart 默认 True），所以理论上
    会重新 import 模块。但万一没生效，从行为上完全看不出来——旧代码照样跑得欢。
    启动日志带上这个时间戳，重载后对一眼就知道是不是真的换了代码。
    """
    newest = 0.0
    skip = {'__pycache__', 'data', 'tests', 'tools', '.git'}
    for dirpath, dirnames, filenames in os.walk(BASE_DIR):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if not name.endswith('.py'):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                pass
    if newest <= 0:
        return '未知'
    return datetime.datetime.fromtimestamp(newest).strftime('%m-%d %H:%M:%S')

# 注意：早期那套「智慧歌伊模式」（快速模型 / 推理模型 双模型 + 模型池 + 切换模型指令）
# 已经删掉了。现在模型由**渠道**决定，掉线时在渠道之间按稳定性切换（见 providers）。
# 残留的 fast_model 只是「单渠道兼容模式」下的模型名（providers 为空时才会用到）。

BASE_DIR = Path(__file__).resolve().parent
ROLE_PROMPT_PATH = BASE_DIR / 'data' / 'role_prompt.txt'


def app_json_version() -> str:
    """app.json 里声明的版本（OlivOS 插件列表里显示的就是它）。"""
    try:
        data = json.loads((BASE_DIR / 'app.json').read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ''
    if isinstance(data, dict):
        return str(data.get('version') or '').strip()
    return ''


def version_mismatch() -> str:
    """两个版本号打架时返回一句人话，一致（或读不到 app.json）时返回空串。"""
    declared = app_json_version()
    if declared and declared != PLUGIN_VERSION:
        return f'app.json 写的是 v{declared}，config.PLUGIN_VERSION 是 v{PLUGIN_VERSION}'
    return ''

DEFAULT_CONFIG = {
    'api_key': '',
    'api_endpoint': 'http://localhost:8787/v1',
    'api_protocol': 'anthropic',
    # 厂商专有请求参数，会原样合并进请求体；默认留空只发标准字段。
    # 例：用 DeepSeek 官方时关闭思考模式 {'thinking': {'type': 'disabled'}}
    # ——思考模式先输出思维链、挤占 max_tokens，且此时 temperature 不生效。
    'extra_body': {},
    # ===== 多渠道分层调用（故障转移 + 按类型分流）=====
    # providers 非空时启用多渠道路由：按 priority 从小到大尝试，便宜的排前面。
    # 每个渠道 = 一套 (endpoint, api_key, model)；api_key 也可用环境变量
    # GEYI_KEY_<渠道名大写> 提供，避免明文写在配置里。
    #
    # ★ 渠道可选 `kinds` 字段，决定它服务哪些调用类型（2026-09-19 新增）：
    #     不填        → 服务全部（老配置不用改）
    #     ["chat"]    → 只服务聊天（被动回复、主动搭话、图片理解…）
    #     ["memory_curate"] → 只服务记忆提炼
    #
    # 为什么要分流：聊天和提炼的成本结构完全相反。
    #   聊天：输入大、输出小 → 缓存能省（实测官方缓存 100% vs 中转站 55%）
    #   提炼：输入≈输出，67% 成本在输出 → 缓存打不到痛点
    # 实测（歌伊 582 次提炼 + 904 次聊天）分流后比纯中转站便宜 4~10%，
    # 且聊天快 5 倍（1.7s vs 8.6s）。见 _research/final_cost_v2.py。
    #
    # 分流配置示例（聊天走官方、提炼走中转站）：
    #   [
    #     {"name": "official", "kinds": ["chat"], "priority": 1,
    #      "endpoint": "https://api.deepseek.com/v1/chat/completions",
    #      "model": "deepseek-flash", "api_key": "sk-...",
    #      "extra_body": {"thinking": {"type": "disabled"}}},
    #     {"name": "relay", "kinds": ["memory_curate"], "priority": 1,
    #      "endpoint": "https://api.example.com/v1/chat/completions",
    #      "model": "deepseek-v4.1-flash", "api_key": "sk-...",
    #      "extra_body": {"reasoning_effort": "low"}}
    #   ]
    # 注意：中转站**关不掉思考**（12 种写法实测全无效），官方可以。
    #      官方的 temperature 在思考模式下无效，关掉思考才生效。
    # providers 留空则退回上面的 api_endpoint / api_key / fast_model 单渠道模式。
    'providers': [],
    'failover': {
        # 失败 1 次就把当前渠道判死、下一条消息直接走备用渠道。
        # 之所以敢用 1：/chat 内部本身还对 429/5xx/超时重试过一次（max_retries），
        # 到这里还失败基本就是渠道真的挂了，让下一条消息再陪它浪费时间没意义。
        'fail_threshold': 1,             # 连续失败几次判定该渠道不可用（防抖动）
        'probe_interval_seconds': 300,   # 后台探测间隔（秒）：5 分钟爬一级
        'probe_success_needed': 1,       # 探测成功几次算恢复并切回
        'probe_repair_per_round': 2,     # 每轮顺手体检几个「挂着不可用标签」的低优先渠道
        'probe_timeout_seconds': 10,     # 单次探测超时
        'notify_owner': True,            # 所有渠道都挂时私聊通知骰主
        'notify_cooldown_seconds': 600,  # 同类通知节流间隔，防刷屏
        'switch_history_limit': 50,      # 保留多少条切换历史
    },
    # 单渠道兼容模式用的模型名：只在 providers 为空时才会被用到。
    'fast_model': 'gemini-3.8-flash-medium',
    # 记忆提炼专用的模型名。留空则沿用 fast_model。
    'curate_model': '',
    'fast_temperature': 0.7,
    # 输出上限。大幅放开 token 上限，给长文本和细腻 RP 留足空间。
    'fast_max_tokens': 8192,
    # 触发词：子串匹配，命中任一即视为「在叫她」。默认给全名和两个常用简称。
    'triggers': ['歌伊'],
    # 骰主 QQ 号：**通知类功能的唯一收件人**（「所有渠道都挂了」「自我保护静默」）。
    # 留空 = 这些通知全部发不出去，只在日志里留一条 warning（2026-10 踩过：
    # 歌伊触发退场、骰主却一条私信都没收到）。也可以改用环境变量 GEYI_OWNER_IDS。
    'owner_ids': [],
    'ignore_sender_ids': [],
    'private_enabled': False,
    'passive_enabled': True,
    'active_enabled': True,
    'route_debug': False,
    'worldbook_enabled': False,
    'memory_enabled': True,
    # 注意：`auto_memory_enabled` 已经废除了——它从 v2.5 起就没有任何代码读，
    # 用户设 true 以为开了自动记忆，实际生效的一直是 memory_curator_enabled。
    # load_config 会把只写了旧键的老配置迁移成 memory_curator_enabled（见 _migrate_legacy_keys）。
    # ===== 自动记忆：批量提炼 =====
    'memory_curator_enabled': True,
    'memory_curate_messages': 40,
    'memory_curate_idle_seconds': 900,
    'memory_curate_max_messages': 40,
    'memory_curate_min_interval': 120,
    'memory_curate_max_tokens': 3200,
    'memory_curate_tick_seconds': 60,
    # 提炼单次输出上限（硬顶，防止一次吐 3 万字把预算烧光）
    'memory_curate_max_tokens_cap': 8000,
    # ===== 记忆注入条数（这些键以前只在代码里 .get() 兜底，没进默认值，
    # 导致 missing_config_keys() 永远发现不了、用户也不知道能配）=====
    'memory_inject_facts': 6,       # 每轮注入几条群友事实（L2）
    'memory_inject_self': 3,        # 每轮注入几条她的自我记忆（L3）
    'memory_inject_summaries': 2,   # 每轮注入几条群摘要（L1）
    'time_awareness_enabled': True,
    # 记忆提炼里代表「她自己」的署名（也是「AI 说了什么」的行首标签）
    'assistant_name': '歌伊',
    # 全局附加请求头，会并进每个渠道（个别中转站要求自定义 header 时用）
    'extra_headers': {},
    # ===== 图片理解（多模态大幅放宽）=====
    'image_enabled': True,
    'image_max_per_turn': 5,           # 每次最多附几张
    # 单张上限 4 MB。注意：**首张即使超过总量预算也会保留**（见 images.plan_for）——
    # 宁可多花点 token，也别让用户发一张大图就彻底看不到。
    'image_max_bytes': 4 * 1024 * 1024,
    # 单轮总量 7 MB：3 张 2 MB 的截图勉强收得下，再多就只留前几张。
    'image_total_max_bytes': 7 * 1024 * 1024,
    'image_download_timeout': 15,
    'image_window_seconds': 0,
    'image_fresh_seconds': 120,
    'active_bare_image_factor': 0.3,
    'image_cache_seconds': 300,
    'emote_enabled': True,
    'collapse_short_multiline': False,  # 默认关闭（由 config.json 开起）；单元测试依赖默认换行行为
    'strip_repeated_ellipsis': True,
    'strip_trailing_period': True,
    'emote_min_gap_seconds': 180,
    'emote_window_replies': 5,
    'emote_hourly_cap': 6,
    # ===== 伴生语音（日语原声 · 本地极速TTS对接）=====
    'voice_enabled': True,
    'voice_gateway_url': 'http://127.0.0.1:9885/v1/voice/synthesize',
    'voice_health_url': 'http://127.0.0.1:9885/health',
    'voice_auth_token': '',
    'voice_timeout': 10.0,
    'voice_group_cooldown': 45,       # 单群语音常规冷却秒数（防刷屏）
    'voice_poke_rate': 0.25,          # 戳一戳触发语音的概率（与表情包联动）
    'voice_speed': 1.0,
    # ===== 自我保护静默 =====
    # 模型遇到谩骂/过当调戏/针对角色人格的侮辱/被连续纠缠时，可以输出 [REST:原因]
    # 让**当前会话**安静一段时间，并私聊通知骰主。到期自动恢复。
    'rest_enabled': True,
    'rest_minutes': 10,
    'reply_audit_enabled': True,
    'reply_audit_fallback': '',
    'incomplete_hold_seconds': 8,
    'incomplete_tails': [],
    # 私聊使用 SQLite 连续历史；群聊使用下面的实时旁观窗口
    'max_recent_turns': 20,
    'max_context_chars': 32000,
    'group_live_window_seconds': 1800,
    'group_live_max_messages': 60,
    'group_live_max_chars': 16000,
    'max_reply_chars': 1200,
    'max_pending_per_session': 6,
    'request_timeout_seconds': 45,
    'max_retries': 1,
    'global_concurrency': 4,
    # ── 主动搭话（大幅放宽频率与取消安静时段）────────────────────────────
    'active_shadow': True,                     # 影子模式默认开（防止新安装未调优时乱说话；实机由 config.json 覆盖为 False）
    'active_base_cooldown': 180,               # 基础冷却 3 分钟
    'active_daily_cap': 60,                    # 每群每天主动发言上限大幅放宽
    'active_quiet_hours': '',                  # 全天候陪伴，取消安静时段
    'active_min_self_gap': 45,
    'active_self_window': 1800,
    'active_tier_probability': {'2': 0.60, '3': 0.30},
    'active_tier2_seconds': 180,
    'active_escalation_max': 8,
    'active_escalation_cap': 0.98,
    'active_no_response_window': 600,
    # 注意：曾经有个 `active_stop_strikes`（=10），但没有任何代码读它——
    # 真正的退避档位在 attention._apply_backoff 里（连冷 3/4/5/6 次 → 30min/1h/2h/4h）。
    # 键已删除：一个"看起来能调、实际没接上"的开关比没有更糟。
    'active_log_interval': 300,
    'daily_call_budget': 999999,               # 日调用预算完全放开
    'daily_output_token_budget': 99999999,     # 日输出 token 完全放开
    # ===== 按群人数算额度 =====
    'group_quota_enabled': False,              # 解除每群额度硬限制
    'group_profile_ttl_seconds': 43200,
    'group_quota_base': 30,
    'group_quota_per_member': 0.4,
    'group_quota_min': 40,
    'group_quota_max': 250,
    'group_quota_default': 60,
    # ===== 记忆容量 =====
    'memory_max_facts_per_user': 12,
    'memory_max_facts_per_group': 600,
    'memory_curate_known_facts': 5,    # 提炼时给每个说话人带几条现有事实去判重
}


def _find_plugin_root() -> Path:
    """找到包含 plugin/app 的 OlivOS 根目录；开发目录则退回插件父目录。"""
    current = BASE_DIR
    for _ in range(8):
        if (current / 'app').is_dir():
            return current
        if current.parent == current:
            break
        current = current.parent
    return BASE_DIR.parent


def get_state_dir() -> Path:
    """**可变状态**放这里：配置、数据库、导出的记忆、用户自己加的立绘。

    ⚠️ 为什么不能放在 data/ 里（2026-09 源码级确认，歌伊和歌伊都踩过）：
    OlivOS 每次加载插件都会执行 pluginAPI.run_plugin_data_release_by_name：

        dataPath = './plugin/data/<ns>/data'
        removeDir(dataPath)                            # shutil.rmtree，整个删掉
        shutil.copytree('./plugin/app/<ns>/data', dataPath)

    也就是**把 data/ 整个删掉重建**。放在里面的可变数据每次重载都会丢。

    实测症状：config.json 稳定消失（用户改的配置全没）；
    数据库侥幸活着，只因为 SQLite 占着文件句柄，rmtree 删到它就抛异常中断了——
    而那个异常被 `except Exception: pass` 吞掉，同时让后面的 copytree
    撞上残留目录，在日志里留下一条 FileExistsError。

    放在 data/ 的**上一层**就安全：release 只删 data 子目录。
    """
    override = os.environ.get('GEYI_DATA_DIR', '').strip()
    if override:
        # 测试 / 自定义模式：状态和资源合并到同一个目录，保持隔离
        path = Path(override)
    else:
        path = _find_plugin_root() / 'data' / DATA_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    if not override:
        _migrate_state_out_of_data(path)
    return path


# 老版本把这些放在 data/ 里，被 OlivOS 的 release_data 抹过。
# 首次运行搬到 state 目录；WAL 和 SHM 必须跟着主库一起搬，否则会丢最新写入。
_LEGACY_STATE_FILES = (
    'config.json',
    'geyi.sqlite3',
    'geyi.sqlite3-wal',
    'geyi.sqlite3-shm',
)


def _migrate_state_out_of_data(state: Path) -> None:
    """把还留在 data/ 里的旧配置/数据库搬到 state 目录（只搬，不删）。"""
    data = state / 'data'
    if not data.is_dir():
        return
    for name in _LEGACY_STATE_FILES:
        src = data / name
        dst = state / name
        if not src.exists() or dst.exists():
            continue
        try:
            # 用 os.replace（原子重命名）而不是 shutil.move：
            # Windows 上 shutil.move 遇到被占用的文件会退化成「先复制再删除」，
            # 复制成功、删除失败——留下一个**半吊子副本**，而下一次迁移又会
            # 因为它已存在而跳过真正的那份（实测踩过：数据库被劈成两半）。
            # os.replace 要么整体成功，要么干净地失败，不会留垃圾。
            os.replace(str(src), str(dst))
        except OSError:
            pass


def get_data_dir() -> Path:
    """OlivOS 释放**静态资源**的地方（role_prompt / worldbook / emotes）。

    这个目录**每次加载插件都会被清空重建**，所以只放「插件包里带、运行时只读」的东西。
    任何要长期保留的一律走 get_state_dir()。
    """
    override = os.environ.get('GEYI_DATA_DIR', '').strip()
    if override:
        path = Path(override)
    else:
        path = get_state_dir() / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path


# ===== 资源目录自愈 =====
# OlivOS 0.11.81 在这台机器上的 pluginAPI.run_plugin_data_release_by_name 每次加载都会失败：
# 它先把 ./plugin/data/<ns>/data 建好，再用 shutil.copytree 往这个**已经存在**的目录上复制，
# 于是抛 FileExistsError(WinError 183) 被上层 except 吞掉——
# 结果是 data/ 被 rmtree 清空之后**再也没被填回来**（2026-10-30 日志实测：目录里 0 个文件）。
#
# 插件一直是「资源目录优先、插件包兜底」，所以功能没哑；但带来了两个坑：
#   1) 启动日志打印的「角色设定: …/data/role_prompt.txt」指向一个不存在的文件，
#      照着它去改会白改一场；
#   2) 想在服务器上热改 role_prompt / 世界书的人，改了没反应也没提示。
# 这里补上自愈：把插件包 data/ 里缺的（或比目标更新的）文件补进资源目录。
_MIRROR_SKIP_SUFFIXES = ('.bak', '.pyc', '.tmp', '.log', '.swp')
_MIRROR_SKIP_DIRS = {'__pycache__', '.git'}
_MIRROR_MIN_INTERVAL = 60.0   # 懒同步节流：同一进程内 60 秒最多扫一次
_last_mirror_at = 0.0


def _copy_if_stale(src: Path, dst: Path) -> bool:
    """目标缺失、或插件包那份更新时复制过去。返回是否真的复制了。"""
    try:
        if dst.exists():
            # 1 秒容差：Windows 上 copy2 的 mtime 精度容易让人误判「包里的更新」
            if src.stat().st_mtime <= dst.stat().st_mtime + 1.0:
                return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        return True
    except OSError:
        return False


def sync_released_data(force: bool = False) -> list[str]:
    """把插件包 data/ 补齐到资源目录，返回本次补上的相对路径（空列表 = 无需补）。

    **只补不删**：目标里用户自己加的立绘、改过的设定一律不动；同名文件只有当
    插件包里那份更新（升级插件）时才会覆盖——正好和 OlivOS 本来「每次加载用插件包
    重建 data/」的语义一致，所以不会出现「改了包里却不生效」。
    """
    global _last_mirror_at
    now = time.time()
    if not force and (now - _last_mirror_at) < _MIRROR_MIN_INTERVAL:
        return []
    _last_mirror_at = now
    source_root = BASE_DIR / 'data'
    if not source_root.is_dir():
        return []
    try:
        target_root = get_data_dir()
    except OSError:
        return []
    if target_root.resolve() == source_root.resolve():
        return []
    copied: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames if d not in _MIRROR_SKIP_DIRS]
        for name in filenames:
            if name.endswith(_MIRROR_SKIP_SUFFIXES):
                continue
            src = Path(dirpath) / name
            rel = src.relative_to(source_root)
            if _copy_if_stale(src, target_root / rel):
                copied.append(str(rel).replace('\\', '/'))
    if copied:
        _last_mirror_at = time.time()
    return copied


def get_config_path() -> Path:
    # 注意是 state 目录，不是 data —— 放 data 里会被 release_data 抹掉
    return get_state_dir() / 'config.json'


def missing_config_keys() -> list[str]:
    """磁盘上的 config.json 缺了哪些新字段。

    缺失的字段在内存里会用默认值（不影响运行），但值得在启动日志里提一句——
    否则会出现「我明明升级了，配置里却没有新选项」这种困惑（实测踩过）。
    """
    path = get_config_path()
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(loaded, dict):
        return []
    return [key for key in DEFAULT_CONFIG if key not in loaded]


def get_database_path() -> Path:
    # 同上：数据库也不能放 data/ 里，否则哪天 SQLite 松了文件句柄，
    # release_data 的 rmtree 就会把它连同全部记忆一起删掉。
    return get_state_dir() / 'geyi.sqlite3'


def get_resource_path(name: str) -> Path:
    """资源文件路径：优先运行时数据目录（服务器上可热改），缺失时回退插件包内 data/。

    OlivOS 首次部署会把插件包 data/ 整体复制到 plugin/data/newGeYi/data，
    因此“数据目录优先”能保证：改了数据目录里的角色/世界书文件后，
    「重载设定」「世界书重载」命令实际生效。

    2026-10-30 补两件事：
    1. 本机 OlivOS 的 release 每次都会失败（见 sync_released_data 注释），data/ 一直是空的，
       所以这里要先补一次，否则日志里打印的路径跟实际读到的文件永远对不上；
    2. **包内那份是源头**：它更新时要同步过去再读。少了这一步，改完包里 role_prompt.txt
       再「重载设定」，读到的还是 data 目录里的旧副本——等于改了不生效。
       （反方向仍然成立：谁改了 data 目录里那份、且比包里新，就按 data 那份来。）
    """
    candidate = get_data_dir() / name
    source = BASE_DIR / 'data' / name
    if source.exists():
        _copy_if_stale(source, candidate)
    if candidate.exists():
        return candidate
    return source


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并配置：字典逐层合并，其他类型（含列表）整体替换。

    为什么需要（2026-10-30 复盘）：以前是 `base.update(override)` 的**浅合并**。
    用户只要在 config.json 里写半个字典，默认值就被整块抹掉：

        "failover": {"probe_interval_seconds": 600}

    DEFAULT_CONFIG 里 failover 的另外 7 个键会消失，`fail_threshold` 退回
    `.get(..., 2)` —— 判死阈值从**刻意的 1** 静默变成 2（渠道挂了还要陪它多耗一轮）。
    列表仍然整体替换（`providers`、`triggers` 这类必须能被清空，不能与默认合并）。
    """
    merged = copy.deepcopy(base if isinstance(base, dict) else {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _migrate_legacy_keys(loaded: dict, merged: dict) -> dict:
    """把已废弃的旧键迁移成新键（只在磁盘上写了旧键、没写新键时生效）。"""
    # auto_memory_enabled 从 v2.5 起就没人读了；老配置里只写它的人，
    # 本意是「开启自动记忆」，所以按它的值补 memory_curator_enabled。
    if 'auto_memory_enabled' in loaded and 'memory_curator_enabled' not in loaded:
        merged['memory_curator_enabled'] = bool(loaded['auto_memory_enabled'])
    return merged


def load_config() -> dict:
    path = get_config_path()
    value = copy.deepcopy(DEFAULT_CONFIG)
    if not path.exists():
        # 兼容旧版：配置曾经写在插件目录里（BASE_DIR/config.json）。
        # 数据目录里找不到配置时把它迁移过来——否则会出现
        # 「改了 config.json 却不生效」这种极难排查的情况
        # （实测踩过：数据目录里只有 geyi.sqlite3，配置却躺在插件目录）。
        legacy = BASE_DIR / 'config.json'
        if legacy.exists():
            try:
                loaded = json.loads(legacy.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    value = _migrate_legacy_keys(loaded, _deep_merge(value, loaded))
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        save_config(value)
        return value
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            value = _migrate_legacy_keys(loaded, _deep_merge(value, loaded))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return value
    return value


def save_config(value: dict) -> None:
    path = get_config_path()
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)
