# -*- encoding: utf-8 -*-
"""静态配置、路径和默认值。"""

from __future__ import annotations

import copy
import datetime
import json
import os
import shutil
from pathlib import Path

PLUGIN_NAME = 'newGeYi'
NAMESPACE = 'newGeYi'
MENU_EVENT = 'newGeYi_Menu_001'
DATA_DIR_NAME = 'newGeYi'
# 插件版本：只在这一处维护，日志 / 菜单 / 状态都引用它
# （app.json 的 version 要跟它一致，有测试锁着——两个版本号打架时最难排查）
PLUGIN_VERSION = '2.5.0'

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

DEFAULT_CONFIG = {
    'api_key': '',
    'api_endpoint': 'https://api.openai.com/v1/chat/completions',
    'api_protocol': 'openai',
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
    #     {"name": "openai-chat", "kinds": ["chat"], "priority": 1,
    #      "endpoint": "https://api.openai.com/v1/chat/completions",
    #      "model": "gpt-4o-mini", "api_key": "sk-..."},
    #     {"name": "openai-curate", "kinds": ["memory_curate"], "priority": 1,
    #      "endpoint": "https://api.openai.com/v1/chat/completions",
    #      "model": "gpt-4o", "api_key": "sk-..."}
    #   ]
    # 注意：中转站**关不掉思考**（12 种写法实测全无效），官方可以。
    #      官方的 temperature 在思考模式下无效，关掉思考才生效。
    # providers 留空则退回上面的 api_endpoint / api_key / fast_model 单渠道模式。
    'providers': [],
    'failover': {
        'fail_threshold': 2,             # 连续失败几次判定该渠道不可用（防抖动）
        'probe_interval_seconds': 900,   # 后台探测间隔（秒）：15 分钟
        'probe_success_needed': 1,       # 探测成功几次算恢复并切回
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
    'owner_ids': [],
    'ignore_sender_ids': [],
    'private_enabled': False,
    'passive_enabled': True,
    'active_enabled': True,
    'route_debug': False,
    'worldbook_enabled': False,
    'memory_enabled': True,
    'auto_memory_enabled': False,
    # ===== 自动记忆：批量提炼 =====
    'memory_curator_enabled': True,
    'memory_curate_messages': 40,
    'memory_curate_idle_seconds': 900,
    'memory_curate_max_messages': 40,
    'memory_curate_min_interval': 120,
    'memory_curate_max_tokens': 3200,
    'memory_curate_tick_seconds': 60,
    'time_awareness_enabled': True,
    # ===== 图片理解（多模态大幅放宽）=====
    'image_enabled': True,
    'image_max_per_turn': 5,           # 每次最多附几张
    'image_max_bytes': 10485760,       # 单张上限 10MB
    'image_total_max_bytes': 20971520, # 总体积上限 20MB
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
    'active_stop_strikes': 10,
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
    """
    candidate = get_data_dir() / name
    if candidate.exists():
        return candidate
    return BASE_DIR / 'data' / name


def get_owner_ids() -> set[str]:
    raw = os.environ.get('GEYI_OWNER_IDS', '')
    return {item.strip() for item in raw.replace('，', ',').split(',') if item.strip().isdigit()}


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
                    value.update(loaded)
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        save_config(value)
        return value
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            value.update(loaded)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return value
    return value


def save_config(value: dict) -> None:
    path = get_config_path()
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)
