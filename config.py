# -*- encoding: utf-8 -*-
"""静态配置、路径和默认值。"""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger('newGeYi.config')

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
PLUGIN_VERSION = '2.9.9'

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
    #      "endpoint": "https://api.ginka.cloud/v1/chat/completions",
    #      "model": "deepseek-v4.1-flash", "api_key": "sk-...",
    #      "extra_body": {"reasoning_effort": "low"}}
    #   ]
    # 注意：中转站**关不掉思考**（12 种写法实测全无效），官方可以。
    #      官方的 temperature 在思考模式下无效，关掉思考才生效。
    # providers 留空则退回上面的 api_endpoint / api_key / fast_model 单渠道模式。
    'providers': [],
    'failover': {
        # 失败几次判定该渠道不可用（防抖动）。
        # 2026-11-07 从 1 改成 2：原来"一次超时就判死"，而上游偶发 60s 超时很常见
        # （实测 star 站在晚上会出现 40~70s 的正常响应），一次抖动就把整条线判死 5 分钟，
        # 多条线同时抖一下 = 「显示全部掉线」。两次才判死，抗抖动能力翻倍。
        'fail_threshold': 2,             # 连续失败几次判定该渠道不可用（防抖动）
        'probe_interval_seconds': 300,   # 后台探测间隔（秒）：5 分钟爬一级
        'probe_success_needed': 1,       # 探测成功几次算恢复并切回
        'probe_repair_per_round': 2,     # 每轮顺手体检几个「挂着不可用标签」的低优先渠道
        'probe_timeout_seconds': 10,     # 单次探测超时
        'notify_owner': True,            # 所有渠道都挂时私聊通知骰主
        'notify_cooldown_seconds': 600,  # 同类通知节流间隔，防刷屏
        'switch_history_limit': 50,      # 保留多少条切换历史
        # 「轮次循环」：一轮走到底（最后一个渠道也挂了）就回到第一个再来一轮。
        # 骰主 2026-11-01 的口径：「第五个挂了就重回第一个开始连接」。
        # 2 轮 = 最坏情况每个渠道试两次；只有一个渠道时自动降为 1 轮（内部已重试过）。
        'full_cycles': 2,
        # 一次 chat() 的总时长预算（秒）：超过就不再换渠道兜圈。
        # 以前没有这条：5 候选 × 2 轮 × 单渠道超时，最坏能到 8 分钟，
        # 而调度器的看门狗是 300 秒——任务会先被当成「卡死」踢掉。
        # 2026-11-07 从 60 提到 150：60 秒比**单条渠道自己的超时**还小
        # （provider.timeout 是 45~60s），第一条超时就把预算吃光，
        # 后面几条根本没机会试——“故障转移”形同虚设，直接判成全挂。
        # 150 秒 ≈ 两条线各超一次 + 第三条有机会回；仍在 300 秒看门狗之内。
        'total_timeout_seconds': 150,
        # 轮满了还全挂 → 切到这个渠道并**锁定**（不再尝试其它渠道），
        # 直到骰主「切换渠道 <名称>」手动切回。空字符串 = 不降级，只发通知。
        'last_resort_provider': 'official-chat',
    },
    # 单渠道兼容模式用的模型名：只在 providers 为空时才会被用到。
    'fast_model': 'gemini-3.8-flash-medium',
    # 记忆提炼专用的模型名。留空则沿用 fast_model。
    'curate_model': '',
    # 记忆提炼固定走哪个渠道（providers[].name）。留空 = 跟聊天用同一条。
    # 提炼是后台机械活（抽事实、写摘要），不需要聊天主力那套理解力，却占了近一成
    # 输入 token；指到最便宜的渠道能直接省下来，聊天本身不受影响。
    # 那条渠道挂了照常按优先级往下兜底，不会因为省钱把提炼饿死。
    'curate_provider': '',
    'fast_temperature': 0.7,
    # 输出上限。大幅放开 token 上限，给长文本和细腻 RP 留足空间。
    'fast_max_tokens': 8192,
    # 触发词：子串匹配，命中任一即视为「在叫她」。默认给全名和两个常用简称。
    'triggers': ['歌伊'],
    # 骰主 QQ 号：**通知类功能的唯一收件人**（「所有渠道都挂了」「自我保护静默」）。
    # 留空 = 这些通知全部发不出去，只在日志里留一条 warning（2026-10 踩过：
    # 歌伊触发退场、骰主却一条私信都没收到）。也可以改用环境变量 GEYI_OWNER_IDS。
    'owner_ids': [],
    # @全体成员 算不算「在叫她」：默认**不算**（那是在叫所有人，不是跟她说话）。
    # 2026-11-03 之前它被当成 @她，于是群里发个公告她就抢着接话。
    'mention_on_at_all': False,
    'ignore_sender_ids': [],
    'private_enabled': False,
    # ===== 私聊闸门（并发保护）=====
    # 私聊以前是「每句都回」，调度器只有 6 个 worker，人一多就排队、表现为"她不理人"。
    # 下面这几个开关是同一个目的：把"每句都回"降级成可控的频次。
    # 只在命中触发词时回应（triggers + private_trigger_extra + assistant_name）。
    # 关掉时保持旧行为（私聊每句都回）。没命中的消息仍进记忆缓冲，她知道你在说什么。
    'private_trigger_only': False,
    # 私聊额外的触发词（不想动 triggers 时用这个，例如 ['在吗', '歌伊歌伊']）
    'private_trigger_extra': [],
    # 同一个人两条回复之间的最小间隔（秒）；期间到达的消息直接丢，不排队
    'private_min_interval': 4,
    # 连发达标：window 秒内超过 max 条就静默（防"连发十条追着问"）
    'private_burst_window': 15,
    'private_burst_max': 4,
    # 每日限额：单人每天最多回几条 / 全体私聊每天最多回几条（0=不限）。
    # 后者才是真正管并发的那个闸——前两个只压单个人的频率。
    #
    # 2026-11-07 重新分配（骰主反馈「有人太热情，一天就打满 300」）：
    # 拉出 11-06 的真实分布后看得很清楚——12 个人 317 条，**前两名占了 50.5%**
    # （87 + 73），前六名占 80%，中位数只有 16 条。所以瓶颈不是总量不够，
    # 是分配极不均：两个人能把公共额度吃光，后面来的人一条都排不上。
    #   · 单人 100 → 60：第 3 名才 34 条，60 已经覆盖 99% 的人；
    #   · 全局 300 → 400：给更多人留位置（14 人 × 28 条 ≈ 392）；
    #   · 再加一段「拥挤收紧」：全局用到 70% 之后，单人上限临时压到 35，
    #     保证晚来的人还有得聊。平时完全不生效。
    'private_daily_quota': 60,
    'private_daily_quota_global': 400,
    # 拥挤阈值：全局用量超过 总量×该比例 就开始收紧（0=不收紧）
    'private_quota_squeeze_at': 0.7,
    # 收紧后的单人上限（0=不收紧）
    'private_quota_squeeze': 35,
    'passive_enabled': True,
    'active_enabled': True,
    'route_debug': False,
    'worldbook_enabled': False,
    # ===== 私聊 galgame：好感度 / 关系阶段（设计见 docs/GALGAME_PLAN.md）=====
    # 总开关。关着时私聊和以前完全一样；世界书里 min_stage>0 的深层设定也不会放行（主人除外）。
    'galgame_enabled': False,
    # 影子模式：评审照常运行并写入 relationship_log，但**不生效**（只有"每天来"的小奖励会计分）。
    # 先开着观察一两周、确认评审口径没问题，再关掉让分数真正生效。
    'galgame_shadow': True,
    # 主人豁免：不评分、不注入关系块，世界书按最高阶段放行。
    'galgame_owner_exempt': True,
    # 额外豁免名单（role_prompt 里写死的主人 QQ；owner_ids 里的人也自动豁免）
    'galgame_exempt_ids': ['2332239652'],
    # 评审用的模型（留空沿用 fast_model）；它走 kind='affection'，可用 affection_provider 指到便宜渠道
    'galgame_judge_model': '',
    'galgame_judge_max_tokens': 1024,
    'affection_provider': '',
    # 轻微越界的宽容档（2026-11-07）：评审判「亲密程度超前」时，如果只超了这么多档以内，
    # **不扣分**，只记一笔"轻微越界"，让她下一轮有点被打扰到的小反应。
    # 为什么：实测 41 条扣分全部发生在 stage 0/1，其中 30 条是 L2 = 朋友间正常语气
    # （"你真可爱""给你比心"）。全按越界扣，等于每个新用户第一句话就踩雷。
    # 设为 0 = 恢复旧行为（超前一点也扣分）。
    'galgame_mild_gap': 1,
    # 短于这么多字（去标点后）的消息不送评审（"嗯""哦"）    'galgame_judge_min_chars': 4,
    # 低于该阶段时，群聊印象/事实不会带进私聊（互通记忆开着也一样）
    'galgame_cross_memory_min_stage': 2,
    # 难度旋钮（长度必须是 6，对应阶段 0~5；写错会自动回退默认值）：
    'galgame_thresholds': [0, 15, 50, 110, 190, 280],   # 进入各阶段所需分数
    'galgame_min_days': [0, 1, 3, 7, 14, 21],           # 进入各阶段所需的"有效日"
    'galgame_daily_cap': [12, 12, 12, 10, 9, 8],        # 各阶段每日最多加几分
    # 同一个标签（care/share/humor/…）每天最多记几次分。
    # 2026-11-06 从 1 改成 2：以前同一标签当天第二次起要减掉「已记过几次」，
    # 结果是「第一次 +1、之后全 0」，实测大多数人一天只能加 3~5 点，升一档要一周。
    # 现在改成每天两次、各自独立计分（第二次不折损），第三次起归零。
    'galgame_tag_daily_limit': 2,
    'memory_enabled': True,
    # 注意：`auto_memory_enabled` 已经废除了——它从 v2.5 起就没有任何代码读，
    # 用户设 true 以为开了自动记忆，实际生效的一直是 memory_curator_enabled。
    # load_config 会把只写了旧键的老配置迁移成 memory_curator_enabled（见 _migrate_legacy_keys）。
    # ===== 自动记忆：批量提炼 =====
    'memory_curator_enabled': True,
    # 私聊与群聊记忆库互通开关：默认关闭（私聊与群聊记忆独立隔离）。
    # 开启后，私聊里能读到群聊的记忆；是否**反向**也开放，由下面那个键决定。
    # 关闭时，私聊和群聊各自单独提取记忆和印象，互不影响。
    'memory_private_enabled': False,
    # 反向共享：**群聊里能不能读到私聊的记忆**（默认关）。
    # 关着 = 单向共享（私聊知道群里聊了什么，群里不知道私聊里的事）；
    # 这是 2026-11-05 骰主要的口径：私聊能接住群聊的话题，
    # 又不会出现「群里好几个恋爱对象」的尴尬。
    'memory_private_to_group': False,
    'memory_curate_messages': 40,
    'memory_curate_idle_seconds': 900,
    'memory_curate_max_messages': 40,
    'memory_curate_min_interval': 120,
    'memory_curate_max_tokens': 3200,
    'memory_curate_tick_seconds': 60,
    # 提炼单次输出上限（硬顶，防止一次吐 3 万字把预算烧光）
    'memory_curate_max_tokens_cap': 8000,
    # ===== 记忆注入条数与容量扩展（v2.8 新渠道升级）=====
    'memory_inject_facts': 12,      # 每轮注入几条群友事实（L2，由 6 扩至 12）
    'memory_inject_self': 6,        # 每轮注入几条她的自我记忆（L3，由 3 扩至 6）
    'memory_inject_summaries': 3,   # 每轮注入几条群摘要（L1，由 2 扩至 3）
    'memory_max_chars': 3200,       # 记忆注入总字符上限（由硬编码 900 放宽至 3200）
    'memory_relevance_enabled': True,   # 是否启用话题关键词重叠相关度召回
    'memory_relevance_weight': 0.25,    # 话题相关度在打分中的权重（0~1）
    'memory_temporal_anchor': True,     # 是否在记忆前标注相对时间（如 [3天前]、[昨天]）
    'memory_cross_group_enabled': True, # 是否允许跨会话检索同一用户的全局偏好事实
    'memory_impression_enabled': True,  # 是否启用群友印象总结卡片（高好感/常聊群友高维总结）
    'memory_diversity_enabled': True,   # 是否启用检索召回 MMR 语义多样性去重过滤（防同类记忆霸屏）
    'memory_diversity_threshold': 0.30, # 检索去重相似度阈值（超过此值视为同类话题，只保留最高分那条）
    'memory_fuzzy_dedup_threshold': 0.60, # 写入端模糊判重阈值（超过此值视为重复事实，转为刷新已有条目）
    'time_awareness_enabled': True,
    # 记忆提炼里代表「她自己」的署名（也是「AI 说了什么」的行首标签）
    'assistant_name': '歌伊',
    # 全局附加请求头，会并进每个渠道（个别中转站要求自定义 header 时用）
    'extra_headers': {},
    # ===== 图片理解（多模态大幅放宽）=====
    'image_enabled': True,
    'image_max_per_turn': 5,           # 每次最多附几张
    # 单张上限 4 MB。**超过会先降采样**（Pillow 压到 1568px 的 JPEG），压不下来才丢弃；
    # 注意：首张即使超过总量预算也会保留（见 images.plan_for）——
    # 宁可多花点 token，也别让用户发一张大图就彻底看不到。
    'image_max_bytes': 4 * 1024 * 1024,
    # 单轮总量 7 MB：3 张 2 MB 的截图勉强收得下，再多就只留前几张。
    'image_total_max_bytes': 7 * 1024 * 1024,
    'image_download_timeout': 15,
    # 下载硬上限与重定向上限：以前没有体积上限、也不拦内网地址，
    # 一个异常/恶意链接就能吃满内存，或者借她的进程去访问本机服务（2026-11-03 审查发现）。
    'image_download_max_bytes': 12 * 1024 * 1024,
    'image_download_max_redirects': 3,
    'image_window_seconds': 0,
    'image_fresh_seconds': 120,
    'active_bare_image_factor': 0.3,
    # 缓存 TTL 1800s：**必须 ≥ 消息窗口（group_live_window_seconds）**，
    # 否则"看看刚才那张图"回溯到 10 分钟前时缓存已经过期，而 QQ 图链早死了。
    'image_cache_seconds': 1800,
    # 内存里 data URL 的总字节上限（一张 ≤4MB，不设顶会把内存吃掉）
    'image_cache_max_bytes': 64 * 1024 * 1024,
    # 「取不到」的负缓存：QQ 图链过期是常态，10 分钟内同一个 ref 不再重试
    'image_fail_cache_seconds': 600,
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
    # 本机 TTS 网关的共享口令：**默认留空**，别在源码里写死。
    # 2026-11-03：以前这里写死了一个共享口令，而且 code 里还硬编码了
    # 本机 OneBot HTTP 的 access_token —— 打包成 OPK 就等于把凭据一起发出去。
    'voice_auth_token': '',
    'voice_timeout': 10.0,
    'voice_group_cooldown': 45,       # 单群语音常规冷却秒数（防刷屏）
    'voice_poke_rate': 0.25,          # 戳一戳触发语音的概率（与表情包联动）
    'voice_speed': 1.0,
    # 语音气泡直发用的本地 OneBot HTTP 地址（含 access_token 就写在这里，别写进代码）
    'voice_onebot_http_url': '',
    'voice_onebot_access_token': '',
    # ===== 自我保护静默 =====
    # 模型遇到谩骂/过当调戏/针对角色人格的侮辱/被连续纠缠时，可以输出 [REST:原因]
    # 让**当前会话**安静一段时间，并私聊通知骰主。到期自动恢复。
    'rest_enabled': True,
    'rest_minutes': 10,
    # 私聊里的退场是否无限期（默认开）：一对一骚扰是「同一个人一直追着说」，
    # 等她自动回来=同一套话术再来一遍；只认骰主发「解除静默」。
    'rest_private_forever': True,
    # ===== 作息（休眠）=====
    # 她 24 小时在线，凌晨三点还在秒回——太假了。开起来之后按这段作息走：
    #   ① 睡前 sleep_pre_minutes 分钟内，提示词里加一段"快要去睡了"，让她自己收尾；
    #   ② 休眠时段内第一次被叫（@ / 私聊）：回一句"睡着呢"，之后完全缄默；
    #   ③ 休眠期间不主动搭话、不调模型（省 token，也避免半夜说话）。
    # 时间一律本地时间；跨午夜（23:30-07:00）按区间处理。
    # ⚠️ 骰主指令永远不受休眠影响（否则没法远程关掉它）。
    'sleep_enabled': False,
    'sleep_window': '23:30-07:00',
    'sleep_pre_minutes': 30,
    'sleep_notice_once': True,
    'sleep_notice_text': '（迷迷糊糊翻了个身）唔……歌伊已经睡下了……有事明天说嘛……',
    'sleep_owner_exempt': False,
    # ===== 本地面板 =====
    # OlivOS 的「插件页面」是 1.0.x 才有的能力（webui_config 消息桥）。核心没有它时，
    # 插件自己起一个只绑 127.0.0.1 的小服务把同一套页面挂起来；换端口改 port，
    # 或者干脆 false 关掉（例如升级到 1.0.x 之后走原生入口）。
    'panel_local_enabled': True,
    'panel_local_port': 18765,
    'reply_audit_enabled': True,
    'reply_audit_fallback': '',
    # 神职头衔削弱：她跟人玩「封官过家家」时会自称「本教皇」「本大主教」，
    # 甚至给别人封大主教——这是 QQ 内容风控最敏感的一类（2026-11 实测：715 条
    # 回复里 6.2% 命中，且成串连发）。开着只削头衔，不删任何设定：
    # 修女／教会／祈祷／礼拜堂照旧说，见 presentation/renderer.sanitize_religion。
    'religion_guard_enabled': True,
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
    'active_base_cooldown': 15,                # 基础最小间隔（闭环前是 180s，那是"绝对节奏"的病根）
    'active_daily_cap': 320,                   # 每群每天上限（3/百条 × 9100 条/天 ≈ 273，150 会顶住）
    'active_quiet_hours': '',                  # 全天候陪伴，取消安静时段
    'active_min_self_gap': 45,
    'active_self_window': 1800,
    'active_self_factor_cap': 2.0,             # 她发言频率系数上限（以前最高 4 倍 = 12 分钟不能说）
    'active_tier_probability': {'2': 0.60, '3': 0.30},
    'active_tier2_seconds': 180,
    'active_escalation_max': 8,
    'active_escalation_cap': 0.98,
    'active_no_response_window': 600,
    # ── 主动搭话欠账调度（v2.8.9）：目标句/百条，靠「欠账 + 逐级升级」落地 ──
    # 机制：每条群消息欠账 += λ，开口一次 -= 1；λ = 目标/100 ÷ 兑现率 conv。
    #      一个周期最多试 3 档提示词（L0 严格 / L1 中性 / L2 软化），被拒就升一级。
    # 为什么不再用 v2.8.7 的 `required = (100/target) × accept`：accept 是**模型服从度**，
    # 同一段代码实测 9%~79%，于是「3 句/百条」变成「13.7 次调用/百条、0.75 句/百条」。
    # 现在的 conv 是「一个周期兑现率」（0.6~0.95），补偿量温和、收敛快。
    # 详见 routing/attention.py「欠账调度 + 暴击升级」与交接文档 §27。
    'active_rate_target_per_100': 3.0,         # 目标落地句数（0 = 不限，只走间隔护栏）
    'active_rate_min_gap': 15,                 # 同一群两个周期的最小间隔（秒）
    'active_rate_max_attempts_10min': 40,      # 成本护栏（防跑飞用，**不负责节奏**）：正常群碰不到它
    'active_deficit_cap': 1.0,                 # 欠账不排队：攒够就花，攒不出去的直接作废
    'active_burst_max_attempts': 3,            # 一个周期最多调几次模型（1~3 = 只到 L0/L1/L2）
    'active_escalate_enabled': True,           # 关掉就退回「一次调用定生死」（L0 only）
    'active_prd_crit_rate': 0.25,              # 暴击名义概率：平均每 4 个失败周期被强推一次
    'active_prd_strong_at': 4,                 # 干旱到这个周期数，暴击直接起手 L2
    'active_burst_max_seconds': 12,            # 一轮升级的时长预算（超时收手：别让 @她 的人干等）
    'active_level_min_samples': 60,            # 某档攒够多少次才敢判它没用（样本少了必误杀）
    'active_level_min_accept': 0.05,           # 连「上界」都低于它 → 砍掉该档（约 20 次调用换 1 句）
    'active_level_init_accept': 0.50,          # 档位接受率先验（没样本时）
    'active_level_alpha': 0.25,                # 档位接受率 EWMA 系数（仅展示，判定用上界）
    'active_conv_init': 0.15,                  # 周期兑现率初值：**宁可低估**（低估=多试几次，收敛快）
    'active_conv_min': 0.08,                   # 下限：兑现率再差也要能补偿回来，成本靠 10 分钟上限挡
    'active_conv_max': 1.0,
    'active_conv_prior': 1.0,                  # 先验权重（个「假周期」）：只要兜住冷启动，给大了会一直把兑现率拖低
    'active_conv_window': 40,                  # 计数窗口：满 40 个周期就减半遗忘
    # 外环：每 N 条消息结算一次「实际产出/目标」，修正所有与模型无关的损耗
    # （最小间隔、10 分钟上限、打分地板、日上限）。没有它，回放里 9% 的模型只能落地
    # 1.38 句/百条而不是 3.0——差额全被护栏吃掉了。
    'active_update_messages': 100,             # 结算窗口（条消息）
    'active_rate_gain_min': 0.25,              # 修正系数下限
    'active_rate_gain_max': 4.0,               # 上限
    'active_gain_expo': 0.6,                   # 修正强度（1.0 = 一步到位，容易震荡）
    'active_gain_step': 1.6,                   # 窗口内一句都没说成时的加速倍率
    'active_gain_deadband': 0.08,               # 死区：偏 8% 以内不修（太宽会停在区间边缘）
    # 注意：曾经有个 `active_stop_strikes`（=10），但没有任何代码读它——
    # 真正的退避档位在 attention._apply_backoff 里（连冷 3/4/5/6 次 → 30min/1h/2h/4h）。
    # 键已删除：一个"看起来能调、实际没接上"的开关比没有更糟。
    'active_log_interval': 300,
    # ── 主动搭话打分闸门（v2.8）：8 维逻辑回归，纯算术，不发模型请求 ──
    # v2.8.7 起打分只当「噪声地板」：不再是配额（配额交给闭环），
    # `gate_quantile_keep=1.0` 表示不按分位砍（砍了闭环会为凑产量把尝试数翻倍，钱白花）。
    'gate_scorer_enabled': True,
    'gate_min_score': 0.05,                    # 绝对地板：低于它就当噪声，零成本挡掉
    'gate_quantile_keep': 1.0,                 # 0~1 保留比例；<1 时按每群滑窗分位再砍一刀
    'gate_score_window': 80,                   # 每群分数滑窗长度
    'gate_replies_per_hour': 6,                # 老口径，仅用于 gate_summary 参考显示
    'gate_batches_per_hour': 60,
    'gate_weights': {},
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
    # 2026-11-05：从 12/600 放宽到 50/3000。为什么敢放宽——
    # ① 每条事实写入时带 3-6 个检索关键词，换说法也能命中，不再靠"少存"来保精度；
    # ② 注入端仍是每轮 12 条（私聊还要过阶段门 2~12），放宽的只是"记得住多久"；
    # ③ 视频分享单独按条数封顶（memory_video_share_keep），不会再把人的记忆占满。
    'memory_max_facts_per_user': 50,
    'memory_max_facts_per_group': 3000,
    # 召回候选池：只按重要度取，量大了会把"低重要度但正好切题"的挡在门外，
    # 所以池子放大 + 关键词预取一次（见 MemoryRepository.recall）。
    'memory_fact_candidates': 400,
    # 视频分享类事实每人保留几条（bili 插件按事件写，每条一条，不封顶会占满）
    'memory_video_share_keep': 3,
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


class ConfigWriteBlocked(RuntimeError):
    """配置文件不可写（磁盘上那份解析不了 / 之前解析失败过）。

    为什么要有这个：`load_config` 以前遇到 JSON 语法错误会**静默返回默认值**，
    用户发「重载设定」还会看到「✅ 已重载」，接着任何开关命令都把默认配置写回磁盘——
    渠道和 API Key 一次全丢。现在宁可报错，也绝不覆盖一份看不懂的文件。
    """


# {error: 最近一次 load_config 的解析错误（空串 = 正常）, quarantined: 已存过副本的文件路径}
_CONFIG_STATE: dict = {'error': '', 'quarantined': set()}


def config_load_error() -> str:
    """上一次 load_config 是否解析失败（空串表示正常）。命令层据此不要说「✅」。"""
    return str(_CONFIG_STATE.get('error') or '')


def _quarantine_config(path: Path, reason: str) -> None:
    """把解析失败的文件复制成 `.broken-<时间戳>`，原文件一个字节都不动。

    同一个文件每个进程只存一份：配置坏着的时候每次「重载设定」都会重新解析一遍，
    不然一天下来能攒出几十份同样内容的副本。
    """
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    done = _CONFIG_STATE.setdefault('quarantined', set())
    if key in done:
        logger.error('配置文件解析失败（%s），本次按默认值运行，**不会覆盖磁盘上的配置**', reason)
        return
    try:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup = path.with_name('%s.broken-%s' % (path.name, stamp))
        shutil.copy2(str(path), str(backup))
        done.add(key)
        logger.error('配置文件解析失败（%s），原文件已复制到 %s，本次按默认值运行，'
                     '**不会覆盖磁盘上的配置**。改好 JSON 再「重载设定」。',
                     reason, backup.name)
    except OSError:
        logger.error('配置文件解析失败（%s），且复制备份也失败了；本次按默认值运行，'
                     '不会覆盖磁盘上的配置', reason)


def load_config() -> dict:
    path = get_config_path()
    value = copy.deepcopy(DEFAULT_CONFIG)
    _CONFIG_STATE['error'] = ''
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
        save_config(value, force=True)
        return value
    try:
        raw = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        _CONFIG_STATE['error'] = '%s: %s' % (path.name, exc)
        logger.error('配置文件读不出来（%s），本次按默认值运行', _CONFIG_STATE['error'])
        return value
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        reason = '%s 第 %d 行第 %d 列：%s' % (path.name, exc.lineno, exc.colno, exc.msg)
        _CONFIG_STATE['error'] = reason
        _quarantine_config(path, reason)
        return value
    if not isinstance(loaded, dict):
        _CONFIG_STATE['error'] = '%s: 顶层不是 JSON 对象' % path.name
        logger.error('%s，本次按默认值运行', _CONFIG_STATE['error'])
        return value
    value = _migrate_legacy_keys(loaded, _deep_merge(value, loaded))
    return value


def save_config(value: dict, force: bool = False) -> None:
    """原子写入配置。

    - 磁盘上那份**解析不了**时拒绝覆盖（抛 `ConfigWriteBlocked`），除非 `force=True`；
    - 写入前留一份滚动备份 `config.json.bak-auto`（只留最近一份，避免越堆越多带走密钥）。
    """
    path = get_config_path()
    if _CONFIG_STATE.get('error') and not force:
        raise ConfigWriteBlocked(
            '配置文件上次解析失败（%s），为避免覆盖你的手改内容，这次没有写盘。'
            '改好 JSON 再发「重载设定」。' % _CONFIG_STATE['error'])
    if path.exists():
        try:
            json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            if not force:
                raise ConfigWriteBlocked(
                    '磁盘上的 config.json 现在解析不了（%s），拒绝覆盖。'
                    '改好 JSON 再试。' % exc.__class__.__name__)
        try:
            shutil.copy2(str(path), str(path.with_name(path.name + '.bak-auto')))
        except OSError:
            logger.warning('配置滚动备份失败（不影响本次写入）')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)
