# 歌伊 newGeYi v2.9.11 · 维护交接文档

> **内部交接文档（运维向）**：由工作区同名文件生成，含服务器拓扑与运维路径。
> 为了能随仓库推送，**服务器公网 IP 与"用户目录下的绝对路径"已打码**；
> 工作区里的原文未打码，需要精确路径时看那份。
>
> 生成：2026-11-09 · 同步：2026-11-10 · 对应版本：v2.9.11


> 生成时间：2026-11-09（Asia/Shanghai）
> 核对对象：`C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\plugin\app\newGeYi`
> 线上状态：`C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\plugin\data\newGeYi`
> 依据：OlivOS 插件开发 skill（`olivos-plugin-developer`）+ 源码通读 + 线上日志 + 全量测试实跑

---

## 0. 一分钟速览

| 项 | 值 | 结论 |
|---|---|---|
| 插件版本 | `app.json` 2.9.11 / `config.PLUGIN_VERSION` 2.9.11 | 一致 ✅ |
| OlivOS 核心 | **1.0.0(300)**（目录名仍写 0.11.81，`OlivOS.exe` 2026-10-08 更新） | 已升级，原生 WebUI 可用 |
| 插件入口 | `webui_config` → `webui/index.html`；本地面板已关闭 | 走宿主原生「插件页面」 |
| 事件优先级 | `priority: 10000`（低于 OlivaDiceCore 的 20000） | 先于核心处理消息 |
| 消息模式 | `olivos_string`（OP 码） | 兼容 CQ/OP 双形态解析 |
| 规模 | 约 1.76 万行 Python，9 个子包 + 8 个根级模块，37 个测试模块 | 中型模块化插件 |
| 测试 | **613 / 613 通过**（26.90s，隔离临时目录） | 基线绿 ✅ |
| 今日日志 | newGeYi 记录 1372 行，WARN 76，ERROR 0，Traceback 0 | 无崩溃 ✅ |
| `app.json` 编码 | 首字节 `{`，无 BOM | 合规 ✅ |
| 数据库 | `geyi.sqlite3` 21.07 MB / 17 表 | 正常 ✅ |
| 角色设定生效源 | `plugin/data/newGeYi/role_prompt.webui.txt` | **不是包内副本**（见 P0-1） |
| 配置实况 | 186 键（默认 193，缺 8 个 v2.9.11 新键，多 1 个未知键） | 有漂移（见 P1-2） |

**一句话**：代码健康、测试全绿、线上无异常中断；真正的维护风险不在业务逻辑，而在
「双源配置/双源角色文件、告警噪音、状态目录卫生、文档漂移」这四件事上。

---

## 1. 运行环境实况

### 1.1 目录布局

```
C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\
├─ OlivOS.exe                    1.0.0(300)，2026-10-08
├─ logfile\OlivOS_logfile_2026-11-09.log
├─ plugin\app\newGeYi\           ← 代码（无 .git，靠 tools/push_github.py 推到 GitHub）
│  ├─ main.py                    事件入口（149 行）
│  ├─ config.py                  默认配置 + 路径 + 配置读写（840 行）
│  ├─ panel*.py                  WebUI 后端 / 校验脱敏 / 本地面板服务器
│  ├─ webui\index.html           单文件调试台（194 KB，零外链）
│  ├─ data\                      role_prompt.txt / worldbook.json / emotes.json / emote\*.jpg
│  ├─ docs\                      设计与交付文档（16 篇）
│  └─ tests\ tools\              613 条测试、7 个运维工具
└─ plugin\data\newGeYi\          ← 线上状态（**升级时绝不可覆盖**）
   ├─ config.json                 186 键（含 6 个渠道的明文 key）
   ├─ geyi.sqlite3(+wal/shm)      21.07 MB 记忆库
   ├─ role_prompt.webui.txt       ★ 实际生效的角色设定
   ├─ data\                       OlivOS 每次加载都会重建的资源目录（role_prompt/worldbook/emote）
   ├─ backups\ , *.bak*           历史备份（见 P2-1）
   └─ 导出\                       记忆导出文本 + SQLite 快照
```

**两处极易踩错的路径语义**（`config.py` 已用大量注释固化的结论）：

- `plugin/data/newGeYi/data/`：OlivOS **每次加载插件都会 rmtree 重建**，只放静态资源。
- `plugin/data/newGeYi/`（上一级）：可变状态的家（配置、数据库、网页角色覆盖、导出）。
  任何"要长期留着"的东西都必须放这一层。
- `config.sync_released_data()` 会在 `init_after` 里把包内 `data\` 补进资源目录（本机
  OlivOS 的 release 偶发 WinError 183 失败，这是自愈补丁）。今日日志：`资源目录自愈：资源目录完整，无需补齐`。

### 1.2 入口与生命周期

`app.json` → OlivOS 导入 `newGeYi/__init__.py` → `main.py` 在**模块导入时**构造 `Runtime()`
（配置、SQLite、世界书、网关、记忆提炼器全部实例化）→ 回调：

| 事件 | main.py | 行为 |
|---|---|---|
| `init` | `Runtime.init` | 挂日志桥、打印状态/资源/配置/数据库路径、检查版本与缺键、检查 owner_ids |
| `init_after` | `Runtime.init_after` | 资源自愈、启动渠道探测线程、启动记忆提炼线程、安装跨插件钩子 |
| `group_message` / `private_message` | `Runtime.handle_message` | 归一化 → 命令 → 旁观窗口/图片/记忆缓冲 → 路由 |
| `poke` | `Runtime.handle_poke` | 模板化戳回 + 概率表情/语音彩蛋，走 `_track_self_sent` 记账 |
| `menu` | 先判 `data.webui` → `Panel.handle_event`，否则 `Runtime.handle_menu` | WebUI 请求或私聊发菜单 |
| `save` | `Runtime.save` | 停提炼线程/关系评审/探测线程/调度器、卸载跨插件钩子、关面板 |

**导入即副作用**（重要，做实验脚本时必读）：

1. `Runtime()` 在 import 时执行；
2. `plugin_config.load_config()` 若状态目录里没有 `config.json`，会**立刻写一份默认配置**；
3. `get_resource_path()` 会从包内往资源目录做文件镜像。

→ 任何临时脚本/工具在 `import newGeYi` 之前，**必须先设 `GEYI_DATA_DIR` 指向临时目录**，
否则会写到生产状态目录。`tools/run_webui_tests.py` 就是专门为此写的（先设环境再 discover）。

### 1.3 生产开关实况（与默认值不同者）

| 键 | 线上 | 默认 | 含义 |
|---|---|---|---|
| `worldbook_enabled` | true | false | 世界书（46 条）已启用 |
| `galgame_enabled` | true | false | 私聊好感度系统已启用 |
| `galgame_shadow` | **false** | true | 评审结果**真实生效**（不再只观察） |
| `sleep_enabled` | true | false | 作息 03:00–07:00，主人豁免 |
| `private_enabled` | true | false | 私聊开启 |
| `private_trigger_only` | true | false | 私聊只在命中触发词时回 |
| `mention_on_at_all` | true | false | @全体也算叫她 |
| `active_shadow` | false | true | 主动搭话真实发送 |
| `route_debug` | true | false | 每群每条消息写一行路由日志（噪音源，见 P2-4） |
| `voice_enabled` | **true**（2026-11-09 20:48 重新开启） | true | 语音走 frps 隧道 → 家里 GPU 网关 `127.0.0.1:9885` |
| `panel_local_enabled` | **false** | true | 核心已是 1.0.x，走原生入口 |
| `fast_temperature` | 0.9 | 0.7 | — |
| `max_recent_turns` | 30 | 20 | 私聊历史 30 轮 |
| `collapse_short_multiline` | true | false | — |
| `curate_provider` / `affection_provider` | 空 | 空 | 提炼/评审与聊天共用渠道 |

渠道（`providers`，5 条，全部含 key；2026-11-12 21:32 核对过生产配置）：

| name | model | priority | protocol | kinds | 备注 |
|---|---|---|---|---|---|
| foo-lite | gemini-3.1-flash-lite | 3 | openai | chat, memory_curate | 2026-11-12 20:51 新增（foo 站点），**主力**；实测 1.9–5.3 s |
| ginka-glm | glm-5.3 | 10 | openai | chat, memory_curate | ginka 中转站；11-12 全天 227/227 = 100%，均值 7.5 s |
| foo-grok | grok-4.3 | 12 | openai | chat | 2026-11-12 21:32 新增（foo 的 xAI 分组），备用；实测 5–9 s |
| foo-sonnet | claude-sonnet-5-5 | 15 | openai | chat | 2026-11-12 21:16 新增（foo 的 Claude 分组），备用；**只能走 openai 协议**，见下 |
| star-carbon | gemini-4-carbon | 20 | anthropic | chat | star-token 的 gemini-4 分组（Claude 协议 `/antigravity/v1/messages`）；11-12 晚间上游号池空，全 503 |

**foo 站三个分组的差异**（2026-11-12 实测，三把 key 各管一组）：

| 分组 | key 前缀 | 模型 | protocol | 备注 |
|---|---|---|---|---|
| gemini | `sk-GoeS…` | gemini-3.1/3.5-flash-lite、3.8-flash、3-flash(-preview) | openai（也声明 gemini） | 主力；`usage` 把 `input_tokens`/`output_tokens` 报成 0，真实值在 `prompt_tokens`/`completion_tokens` |
| Claude | `sk-nNe3…` | claude-fable-5/-5-1、haiku-4-5、opus-4-5/4-6/4-7/4-8/5/5-5、sonnet-4-5/4-6/5/5-5 | **只能 openai** | anthropic 协议一律 400 `does not support non-default temperature`；默认带 low 思考 |
| xAI | `sk-klgA…` | grok-4.20-0309-non-reasoning/-reasoning/-multi-agent、grok-4.3/4.5/4.6/4.7/4.7-build-fast、grok-build-0.1 | **只能 openai** | anthropic 协议回 500 `not available`；同样默认带 low 思考 |

**foo 的 Claude 分组（`sk-nNe38…vWOAqB`）注意事项**（2026-11-12 实测）：
- 13 个模型：`claude-fable-5`/`-5-1`、`haiku-4-5`、`opus-4-5/4-6/4-7/4-8/5/5-5`、`sonnet-4-5/4-6/5/5-5`。
- **anthropic 协议一律 400**：`claude-sonnet-5-5 does not support non-default temperature`。插件 anthropic 分支
  必带 temperature（`providers/llm_gateway.py:1213-1214`），所以只能用 **openai 协议**（`/v1/chat/completions`，
  接受 temperature=0.9）。
- 这些模型**默认带 low 思考链**：同一句话会先吐 400–1000 字思考，计入 `completion_tokens`，耗时 8–26 秒。
  `reasoning_effort: "none"` 在长设定下不生效；要真正关掉只能走 anthropic 协议 + `thinking:{"type":"disabled"}`
  （但那样又撞上 temperature 问题，除非给 Provider 加 `no_temperature` 开关——目前没做）。
- 因此它**不适合当主力**：同样一句话比 flash-lite 贵一倍以上、慢 3–8 倍，人设也更淡（偏客服腔）。
- `timeout` 设 90 秒（默认 60 不够）；`fast_max_tokens=8192` 足够，不会被思考链吃穿。

2026-11-12 20:46 按骰主要求裁掉 7 条渠道（`local-proxy`、`star-tiered`、`star-low`、`argon-gemini4`、
`argon-opus46`、`official-chat`、`ginka-low`），理由：star-token 自 13:00 起整站 503、argon 两条整天 0 成功率、
DeepSeek 文本质量不达标。裁剪脚本 `geyi_prune_providers.py`、新增脚本 `geyi_add_foo.py`（都在工作区，
dry-run 默认、`--apply` 才写、写前备份）。配置备份：`config.json.bak-prune-20261112-204638`（8 条时的样子）、
`config.json.bak-foo-20261112-205155`（加 foo-lite 前）。

**加新渠道时必须一并看 `gw.anchor`**：`_apply_persisted_locked` 有「当前渠道优先级高于手动锚点就退回锚点」
的 ceiling 逻辑（`providers/llm_gateway.py:1716-1728`），新渠道优先级数字更小却不改锚点，重载后会被旧锚点
demote 回去。把 `settings` 里的 `gw.anchor` / `gw.current` 一起改成新渠道名（重载窗口内用
`geyi_pin_gateway_state.py` 钉住，否则会被旧进程卸载时的 save 覆盖）。

截至 v2.9.18，故障转移参数（`failover`）：`fail_threshold=2`、`full_cycles=1`、`total_timeout_seconds=60`、
`probe_interval_seconds=300`、`notify_cooldown_seconds=600`；生产 `max_retries=0`，渠道 timeout 为 25s（慢备用 35–45s）。
LLM 网关的 `requests.Session.trust_env=False`，不继承 Windows 系统代理：本机系统代理 `127.0.0.1:10808` 曾监听但请求超时；禁用环境代理后 foo/ginka 的真实模型请求 HTTP 200。

另有 `retry_before_hold_seconds=300`（全挂先软降级，v2.9.14）、`failure_grace_seconds=90`、
`cooldown_base_seconds=120` / `cooldown_max_seconds=1800`（熔断冷却，v2.9.16，**设 0 关闭**）。

兜底渠道（`failover.last_resort_provider`）= **`ginka-glm`**（2026-11-12 20:51 从 `star-carbon` 改过来）：
全挂后先软降级到它、300 秒待重试仍失败才锁定，只有骰主「切换渠道 X / 切换渠道 自动」能解除。
**兜底必须选「非主力」渠道**——v2.9.17 规定「兜底渠道自己调用成功不算链路恢复」（不撤待重试闹钟），
如果兜底就是主力，这条规则会把主力当兜底，闹钟永远撤不掉，机器人会安静地一直挂在兜底上烧钱；
同理它成功是理所当然的，撤了闹钟 `probe_once` 又因「当前是 manual_only 且非锚点」不动手。
换了任何一条渠道的优先级，都要回头检查这里指的是不是当前主力。

---

## 2. 代码结构地图

行数为实测（`get-content` 统计），**只列职责与关键入口**，细节看各模块 docstring（这个仓库的
注释密度很高，且每条都在解释"为什么"，是主要的知识资产）。

| 层 | 文件 | 行 | 职责 / 关键点 |
|---|---|---|---|
| 入口 | `main.py` | 149 | 纯转发；`_BrokenRuntime` 兜住加载期异常；面板惰性挂载 |
| 配置 | `config.py` | 905 | `DEFAULT_CONFIG`（**210 键**）、state/data 路径、深合并、坏配置隔离、原子写、资源镜像、版本比对 |
| 模型 | `models.py` | 72 | `SessionKey` / `IncomingMessage` / `ModelRequest` / `ModelResult` / `RenderedReply` |
| 运行时 | `runtime/app.py` | 1109 | 装配一切 + 消息归一化 + `process_job` 主流程 + 回复/记账 + 日志桥 + 静默 + 菜单 |
| 调度 | `runtime/scheduler.py` | 295 | 同会话 FIFO、跨会话并行（6 worker）、每会话积压 6 条、300s 看门狗、"话没说完"按住 |
| 私聊闸 | `runtime/private_gate.py` | 252 | 触发词 / 防连发 / 单人+全局日额度 / 拥挤收紧 / 赞助档位 |
| 静默 | `runtime/rest.py` | 156 | `[REST:原因]` 自我保护；群按分钟、私聊无限期（`remaining()==-1`） |
| 作息 | `runtime/sleep.py` | 228 | 睡前 30 分钟提示、睡着后只应一句、跨午夜区间 |
| 赞助 | `runtime/sponsors.py` | 162 | 两档赞助纯函数判定（`sponsor_ids` 旧键按一档） |
| 路由 | `routing/message_router.py` | 226 | 机器人过滤 → 静默 → 点名/触发 → 被动闸门 → 主动闸门 |
| 命令 | `routing/commands.py` | 1235 | 表驱动权限；公开/群管/骰主三级；`_OWNER_CMD_*` 声明 |
| 搭话 | `routing/attention.py` | 904 | 8 维打分 + 欠账调度 + PRD 暴击升级 + 自适应退避 |
| 打分 | `routing/likelihood.py` | 137 | 逻辑回归特征与阈值（纯算术） |
| PRD | `routing/prd.py` | 131 | 伪随机分布常量 |
| 权限 | `routing/permissions.py` | 86 | 主人（`owner_ids`/env）/ 群管 / 骰主 |
| 网关 | `providers/llm_gateway.py` | 1373 | OpenAI/Anthropic 双协议、多渠道路由、锚点/锁定、健康计数、探测线程、用量记账 |
| 上下文 | `context/prompt_builder.py` | 602 | 分层 system 拼装（角色→立绘→世界书→群背景→记忆→视频/网页→反幻觉→时间→睡前→关系块→称呼→档位）+ 历史裁剪 |
| 称呼 | `context/aliases.py` | 88 | 自定义称呼：校验（标签结构字符/保留词/长度）+ `settings` 存取（`alias:<QQ>`，全局一个） |
| 工作记忆 | `context/scene.py` | 364 | 状态纸条：从原文重建「场景/进行中/答应过/未解决」（`scene:<session>`，2h TTL，后台线程，`kind=scene` 可分流） |
| 图片 | `context/images.py` | 672 | 最近图片环形缓存、按需下载、降采样、内联 base64、SSRF/体积/重定向防护 |
| 旁观 | `context/live_context.py` | 140 | 群聊 30 分钟滑窗（因果序截断） |
| 群档 | `context/group_profile.py` | 152 | 群人数 → 每群日额度 |
| 跨插件 | `context/cross_plugin_observer.py` | 277 | 猴补丁捕获骰娘/运势/签到等输出，供她"看见"（`save` 时还原） |
| 记忆 | `memory/curator.py` | 692 | L0 缓冲 → 批量提炼 L1 群摘要 / L2 用户事实 / L3 自身记忆 / L4 印象（JSON 契约） |
| 记忆 | `memory/repository.py` | 422 | 召回（重要度×时近×相关度+MMR 去重）、注入块、阶段门 |
| 记忆 | `memory/worldbook.py` | 337 | BM25 + 关键词加权 + 常驻条目 + `min_stage/max_stage/scope/topic` |
| 记忆 | `memory/exporter.py` | 267 | 「导出记忆」人类可读文本 + 自洽 DB 快照 |
| 展示 | `presentation/renderer.py` | 1040 | 22 条清洗/审计规则：草稿剥离、思维链、@ 归一、码剥离、神职削弱、审计拦截、立绘节流、形态整备 |
| 关系 | `relationship/service.py` | 574 | 阶段机 + 异步评审 + 关系块注入 + 越界重写 + 面板接口 |
| 关系 | `relationship/state.py` | 342 | 纯函数状态机（夹紧、日上限、有效日、降级冷却） |
| 关系 | `relationship/stages.py` | 189 | 6 阶段合同、正反例、羁绊事件、越界词审计 |
| 关系 | `relationship/judge.py` | 98 | 评审提示词与 JSON 解析 |
| 存储 | `storage/database.py` | 1468 | 17 表 schema + 只加不删迁移 + 各类查询 |
| 语音 | `voice/client.py` / `manager.py` / `presets.py` | 490/467/252 | TTS HTTP 客户端（自带 RIFF 解析，不依赖 `wave`）、熔断退避、17 情感卡槽 |
| 面板 | `panel.py` | 651 | 14 个白名单 action、revision 乐观锁、写幂等、只读预览、800 条脱敏日志环 |
| 面板 | `panel_helpers.py` | 250 | 字段白名单校验（155 键可编辑）、递归脱敏 |
| 面板 | `panel_server.py` | 367 | 核心无 WebUI 时的 127.0.0.1 本地面板（令牌 + 自定义头 + Origin/Host 校验） |
| 工具 | `tools/*` | 7 个 | 测试运行器、WebUI 预览、素材构建、真模型验收、好感模拟、神职统计、GitHub 推送 |

---

## 3. 消息链路（被动回复的完整路径）

```
OlivOS 事件
 └─ main.Event.group_message / private_message
     └─ Runtime.handle_message
         ├─ op_codes_to_plain(raw, names)       @码→昵称，防她学说数字
         ├─ 构造 IncomingMessage（含 arrival_seq、sender_is_bot）
         ├─ commands.try_handle(msg)            命令命中即返回（不进上下文）
         ├─ live_context.append_user(msg)        群聊旁观窗口
         ├─ group_profile.refresh_async(...)     群人数（后台补缓存）
         ├─ images.remember(msg)                 只记来源，用时才下载
         ├─ curator.buffer_user(msg)             记忆 L0 缓冲（受记忆开关约束）
         └─ MessageRouter.route(msg)
             ├─ 机器人发送者 → 丢弃（防两个 bot 互相刷屏）
             ├─ 静默中 → 丢弃（仍进旁观窗口，醒来知道发生过什么）
             ├─ directed = @她 / 触发词 / 私聊每句（private_trigger_only 时看触发词）
             ├─ 休眠中 → 只回一句"睡下了"（不调模型）
             ├─ directed 且被动允许 → private_gate.allow → scheduler.submit(kind='passive')
             └─ 否则群聊 → attention.evaluate → scheduler.submit(kind='active')

SessionScheduler（同会话串行）
 └─ Runtime.process_job
     ├─ 静默/群额度/积压 早退（私聊先 _remember_user_turn 防丢话）
     ├─ 主动：_active_cycle 逐级 L0→L1→L2（PRD 暴击 + 时间预算 12s）
     ├─ PromptBuilder.build → system 分层 + 历史裁剪 → messages
     ├─ LLMGateway.chat(kind) → 渠道候选排序 → 重试/换渠道/兜底锁定 → ModelResult
     ├─ Renderer.render → 清洗/审计 → RenderedReply（text / emote / voice / rest_reason）
     ├─ 私聊越界 → 带"重写要求"重问一次（最多一次，仍越界则放行）
     ├─ [REST] → _enter_rest + 私聊通知骰主
     ├─ 语音：voice_manager.dispatch_voice（未排上则不写 [VOICE:] 进历史）
     ├─ store.add_turn(user/assistant) + reply()
     ├─ 私聊 → relationship.after_reply（异步评审，不阻塞回复）
     ├─ curator.buffer_assistant / live_context.append_assistant
     └─ attention 记账（note_spoken / note_active_sent）+ prompt_builder.mark_talked
```

关键不变量（改代码前务必守住）：

1. **`self.config` 的 dict identity 不变**：热更新一律 `clear()+update()` + `apply_config()`
   （历史上换过属性，导致"回 ✅ 但行为不变"）。
2. **私聊用户轮先落库**，再由 `PromptBuilder._drop_echoed_current()` 摘掉重复（否则一句变两遍）。
3. **回复以纯文本入库**（OP 码不进上下文），语音只在她真的发出时才记 `[VOICE:…]`。
4. **她自己说的话用与用户消息同一个记忆门**（`_memory_tracking_on`），否则会攒出无人提炼的尾巴。
5. **审计在表情码替换之前**（替换后会带上我们自己生成的 `file://` 路径，等于自己拦自己）。
6. **静默在发送之前生效**，发送失败也必须静默。

---

## 4. 数据模型（SQLite 17 表 + settings 命名空间）

| 表 | 线上行数 | 用途 |
|---|---|---|
| `turns` | 8,340 | 私聊连续历史（每会话保留 200）；群聊主上下文走内存滑窗 |
| `memory_buffer` | 333 | L0 原始缓冲（待提炼/兜底） |
| `group_summaries` | 11,981 | L1 群级滚动摘要 |
| `user_facts` | 3,588 | L2 结构化事实（含 `keywords`/`importance`/`valid_to` 软失效） |
| `user_facts_archive` | 102 | 事实回收站（容量淘汰先归档） |
| `self_memories` | 3,294 | L3 角色自身记忆 |
| `user_impressions` | 168 | L4 印象卡（`user_id + scope(group/private)`） |
| `user_impressions_history` | 245 | 印象历史（只追加） |
| `user_relationship` | 18 | 好感度/阶段/心情/事件进度（一人一行） |
| `relationship_log` | 1,064 | 关系流水（只追加，支撑"关系详情"复盘） |
| `active_decisions` | 5,000 | 主动搭话决策日志（调阈值的唯一依据） |
| `api_calls` | 53,213 | 逐次调用计量（kind/model/token/延迟/成败/缓存命中） |
| `settings` | 1,928 | KV：`kw.*` 渠道状态、`rest:*` 静默、`group_passive/active:*`、私聊闸门用量、`sleep.notice:*`、`schema_version` |
| `backgrounds` | 0 | 群背景（「设置群背景」） |
| `memories` | 0 | 历史遗留表（已迁移到四层结构） |

迁移策略：**只加不删**（`_migrate()` 补列/重建印象表 backfill 历史），`schema_version=1`。

记忆方向（当前生产）：`memory_private_enabled=true` 表示**默认**让私聊读到群聊记忆
（v2.9.11 之后它只是默认值，单个私聊可用「群聊记忆开/关」覆盖，见 §11.5）；
`memory_private_to_group=false` 表示**单向**——私聊知道群里的事，群里不知道私聊的事。

---

## 5. 三个接口面（改动最常落在这里）

### 5.1 配置面

- 默认 210 键；WebUI 可编辑 **155** 键（`panel_helpers.EDITABLE_KEYS`）；
- 不可编辑的 55 键 = 秘密（`api_key`/`voice_auth_token`/`extra_headers`）+ 结构（`providers`/`failover`/`extra_body`）+ 深度调参（`active_*` 大部分、`gate_*`、缓存毫秒级参数）；
- 写入路径只有三条：命令（`_save_and_reload`）、面板（`config_save`，需 `revision + confirm`）、手改 `state/config.json` 后发「重载设定」；
- 排序规则：深合并（字典递归、列表整体替换）；坏 JSON → 保留旧值 + 另存 `.broken-<时间戳>` + **拒绝写盘**（`ConfigWriteBlocked`）；
- 每次写盘前留 `config.json.bak-auto`（只一份）——手写备份不在此列（见 P2-1）。

添加新键的**标准动作**（缺一不可，历史上漏过 `galgame_judge_min_chars`）：
`DEFAULT_CONFIG` → 读取处兜底值用同一字面量 → 若需网页可改则加进 `panel_helpers` 对应集合
→ `tests/test_config.py` 的"兜底值一致/默认键齐全"断言会替你校验 → 文档补一行。

### 5.2 命令面（`routing/commands.py`）

| 级别 | 命令 |
|---|---|
| 公开（任何人） | `歌伊菜单`（2026-11-12 起只含用户指令） `歌伊状态`/`查看状态` `今日用量` `今日额度`/`我的额度`/`私聊额度`（私聊） `记住 <内容>` `我的记忆`/`查看记忆`/`歌伊看我` `删除记忆 <序号>` `忘了我`/`确认忘了我` `群聊记忆`/`群聊记忆开`/`群聊记忆关`/`群聊记忆默认`（仅私聊）`叫我 <名字>`/`我的称呼`/`称呼重置` `关系`/`好感度` `世界书状态` `查看触发词` |
| 群管或骰主 | `歌伊管理菜单`/`管理菜单`（2026-11-12 新增，管理指令的统一入口） `本群被动开/关` `本群主动开/关` `查看群背景` `设置群背景 <文本>` |
| 骰主 | `渠道状态` `切换渠道 <名称/序号>` `切换模型`（墓碑：只回一句"已删除"）`设置key <key>`（仅私聊）`重载设定` `重置上下文` `全局被动开/关` `主动搭话开/关` `搭话影子开/关` `搭话观察` `开启私聊/关闭私聊` `私聊用量` `私聊触发词开/关` `添加/删除触发词` `世界书开/关/重载` `好感度开/关` `好感度影子开/关` `关系详情/关系设置/关系重置 <QQ>` `立绘/表情包 开/关/状态/重载/加载` `图片理解开/关` `语音状态` `开启/关闭语音` `语音重载` `测试语音 [情感]`（`.voice status/test` 为别名）`记忆状态` `导出记忆` `群聊记忆列表` `群聊记忆重置 <QQ/全部>` `称呼列表` `称呼重置 <QQ>` `赞助名单` `赞助加/删 <QQ> [1/2]` `休眠状态/休眠开/休眠关/休眠时间 HH:MM-HH:MM/休眠重置` `静默列表` `解除静默 [<QQ/群>]` `测试通知/通知测试` `面板地址/歌伊面板/调试台` |

约定：骰主命令一律**先判权限再执行**（未授权会明确拒绝而不是落进聊天流程）；
命令处理抛异常时 `try_handle` 返回 `False` 让消息继续走聊天（不让用户消息消失）。

### 5.3 WebUI 面（`panel.py` 14 个 action）

`overview` `config_get` `config_save` `config_reload` `prompt_get` `prompt_save` `sessions`
`session_get` `debug_preview` `logs` `relationship_list` `relationship_get`
`relationship_set_stage` `relationship_reset`

- 写操作仅 5 个：`config_save` `config_reload` `prompt_save` `relationship_set_stage` `relationship_reset`，
  全部要求 `revision` + `confirm:true`，并按 `(浏览器 session, request_id)` 幂等 256 条；
- 请求 256 KiB / 回包 900 KiB 上限；响应统一 `{ok, data}` 或 `{ok:false, error, code}`；
- 密钥字段不回传；任何文本都过 `Redactor`；
- `debug_preview` 是**简化只读**：不联网、不发送、不落库（`effects` 三个 false）；
- 契约文档：`docs/WEBUI_API.md`（与代码逐条对得上，未发现漂移）。

---

## 6. 验证基线（每次改动后照跑）

### 6.1 环境

OlivOS 是 PyInstaller 打包的，没有独立 `python.exe`；插件在模块级 `import requests`
（`context/images.py`），所以用独立 Python 跑测试必须先补依赖。本次用的组合：

```powershell
$py = "<DSH 便携 Python>\python.exe"   # 3.12.14
$env:PYTHONPATH = "<工作区>\.pydeps"   # requests 2.34.2 + urllib3/certifi/idna/charset_normalizer
& $py "C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\plugin\app\newGeYi\tools\run_webui_tests.py"
```

本次实测输出：`Ran 613 tests in 26.900s` / `OK`。
（`.pydeps` 是本机临时装置，不属于插件；正式环境里 OlivOS 自带 requests。）

注意：`run_webui_tests.py` 会**无条件覆盖** `GEYI_DATA_DIR` 到 `%TEMP%\geyi_webui_tests_*`，
不继承 shell 里可能指向生产的变量；测试退出时由 `tests/__init__.py` 清理本次新增目录
（设 `GEYI_KEEP_TEST_DATA=1` 可保留现场）。

### 6.2 测试分布（613 条 / 37 模块）

| 主题 | 模块（条数） |
|---|---|
| 配置与安全 | `test_config`(10) `test_config_safety`(14) |
| 网关/故障转移 | `test_gateway_failover`(14) `test_usage`(5) `test_token_usage`(10) `test_cache_usage`(32) |
| 搭话决策 | `test_gate_scorer`(28) `test_likelihood`(13) `test_prd`(7) `test_active_burst`(5) |
| 记忆/身份 | `test_memory_system`(26) `test_memory_keywords`(17) `test_memory_direction`(9) `test_prompt_builder`(9) `test_private_memory_share`(18) `test_alias`(23) `test_scene`(28) `test_forget_and_delete_memory`(7) |
| 关系 | `test_relationship`(61) `test_relationship_flow`(28) |
| 展示/审计 | `test_renderer`(20) `test_renderer_audit`(5) `test_religion_guard`(11) |
| 运行时护栏 | `test_runtime_guards`(28) `test_scheduler`(12) `test_private_gate`(24) `test_private_chat`(8) `test_private_quota_view`(7) |
| 作息/赞助 | `test_sleep`(30) `test_sponsor_tiers`(27) |
| 面板 | `test_panel`(28) `test_panel_entry`(7) `test_panel_prompt_override`(3) `test_panel_server`(14) `test_webui_assets`(4) |
| 图片/语音 | `test_images`(14) `test_voice_wav`(7) |

### 6.3 静态检查（本次结论）

| 检查 | 结果 |
|---|---|
| 全部 `.py` 编译 | 通过（无 SyntaxError） |
| 全部 JSON 可解析 | 通过（`app.json` 987 B、`worldbook.json` 35,545 B 等） |
| `app.json` BOM | 无（首字节 `{`）✅ |
| 版本号一致性 | `version_mismatch()` 返回空 ✅ |
| 配置解析错误 | `config_load_error()` 空 ✅ |
| WebUI 外链 | 0（`webui/index.html` 零 `http(s)` 资源） |

---

## 7. 已知问题与风险清单

优先级口径：P0 = 会在下一次升级/发布时静默失效；P1 = 影响可观测性或功能可用性；
P2 = 卫生与文档。

### P0-1 角色设定是"双源"，包内更新会被静默忽略

- 证据：`Runtime.load_role_prompt()` **优先**读 `plugin/data/newGeYi/role_prompt.webui.txt`；
  线上存在该文件（10,763 B），与包内 `data/role_prompt.txt`（10,760 B）**差 1 行**
  （「你自己刚发过的**表情包**」vs「**立绘**」）。
  另有 `plugin/data/newGeYi/role_prompt.txt`（9,933 B，105 行）是 **v2.9.4 之前的陈旧副本**
  （缺【别自称高位的神职】整节、缺【性格】依恋句尾注），生产不读它，但很容易被误改。
- 后果：以后改包内 `role_prompt.txt` 并升级 → 线上完全没变，且**没有任何提示**。
- 建议：发版流程加一步"对比 `role_prompt.webui.txt` 与包内副本"，或在 `init` 里比对不一致时
  打一条 WARN；长期方案是让面板保存时同步写回包内副本（或在 `docs` 里明确"包内副本只是出厂值"）。

### P1-2 生产配置缺 8 个 v2.9.11 新键

- 证据：今日 5 次启动都报
  `⚠ 配置文件缺少 8 个新字段：sponsor_tier1_ids、sponsor_tier2_ids、
  private_daily_quota_sponsor_tier1、private_daily_quota_sponsor_tier2、sponsor_memory_keep、
  sponsor_fact_floor、beta_ids、sponsor_thanks_text`。
- 影响：赞助两档功能**跑在默认值**（两档名单空、额度 100/120、记忆不裁剪=true、召回下限 8）；
  功能"没坏"，但"配置里看不见"，且 `赞助加 <QQ> 2` 写盘后才会补齐。
- 同源线索：`config.json.bak-pre-migration-20261109-121812`（今天 12:18）说明有一次迁移尝试后回滚。
- 建议：发一次写操作（任一开关命令）或手工补键，然后确认启动日志不再告警。

### P1-3 启动误报「未配置 API Key」

- 证据：`Runtime.init` 只检查顶层 `api_key` 与 `GEYI_API_KEY`；实际 6 个渠道的 key 全在
  `providers[].api_key` 里。今天 5 次启动 = 5 条假警报。
- 影响：真警报（渠道缺 key）和假警报混在一起，告警疲劳。
- 建议：改用 `gateway.missing_keys()`（已存在）判定，只在"没有任何渠道有 key"时告警。

### P1-4 上游安全策略拦截 → 空回复（今日 18 次）

- 证据：`渠道 star-tiered [gemini-3.8-flash-tiered] 触发上游安全策略拦截：The prompt could not be
  submitted…` 9 次，`star-low` 9 次；随后 `empty_content` 换渠道一次、不判渠道健康。
- 影响：用户侧表现为"她没说话"；不污染渠道健康（v2.9.9 的设计，正确），但**内容被拦**这件事
  目前只在日志里。
- 建议：观察是否集中在特定会话/话题（世界书 + 角色设定里可能有敏感词组合）；
  可考虑在 `api_calls.error_code` 里区分 `upstream_safety` 与 `empty_content`，便于面板统计。

### P1-5 记忆提炼 `empty_content` 失败（今日 3 组）

- 证据：`提炼返回空内容（预算不够），提到 6400 token 再试`（6 次）；
  `记忆提炼 <会话> 未完成（empty_content），30 分钟后再试`（3 组）。
- 影响：这几个群的记忆推进延迟；不影响聊天。
- 建议：`memory_curate_max_tokens` 3200 → 观察是否与"思考模式吃满预算"有关；
  提炼渠道可用 `curate_provider` 单独指到关掉思考的官方渠道（`extra_body.thinking.disabled`）。

### P1-6 包内还留着一份 `config.json`（部署残留）

- 证据：`plugin/app/newGeYi/config.json`（6,638 B，142 键，key 全空但含 `owner_ids: ["2332239652"]`、
  `private_enabled: true`、`route_debug: true`）。
- 影响：① 新机器若状态目录没有配置，`load_config` 会把它当"迁移源"并写进 state；
  ② 打 OPK/整包拷贝时会把它一起带走（`tools/push_github.py` 有 `SKIP_NAMES={'config.json'}`，
  **GitHub 推送安全，但打压缩包不安全**）。
- 建议：移到 `config.example.local.json`（已被推送脚本忽略）或删掉；`config.example.json`
  只有 47 键，是"最小示例"，不要用它对生产配置做整文件覆盖。

### P2-1 状态目录卫生：19 份配置备份 + 4 份库备份 + 0 字节文件

- `config.json.bak-*` 共 **19** 份（0.2 MB），**每份都含 5–6 个渠道明文 key**；
  `~54.3 MB` 的 4 份 `geyi.sqlite3.bak*`；`导出\` 3 组文本+快照；
  另有 0 字节的 `database.db`（无代码引用）与陈旧的 `role_prompt.txt`（见 P0-1）。
- 建议：保留最近 2–3 份配置备份与 1 份库备份即可；key 改用环境变量
  `GEYI_KEY_<渠道名大写>`（代码已支持），配置里只留空串。

### P2-2 文档漂移

| 文档 | 写的 | 实际 |
|---|---|---|
| `docs/INSTALL_FOR_AGENT.md` | `Ran 234 tests`、核心 0.11.x、认证文件 `conf/webui_auth.json` | 当时 476（现 510）；核心 1.0.0(300)；认证文件 `conf/webui_token.txt` |
| `README.md` | 337 条 / 273 / 192（多处历史数字） | 当时 476（现 510） |
| `docs/WEBUI*.md` | 以 0.11.x 无 WebUI 为背景写"本地面板" | 生产已关本地面板 |
| `tools/build_webui_assets.py` | `PORTRAIT_SOURCE = Path('<素材目录>\\歌伊.png')` | 占位符，直接跑必失败 |

### P2-3 `route_debug: true` 常开

每群每条消息一行 `[router] 路由 | 群=… 发送者=… | '原文'`。今日 1372 行 newGeYi 日志里大部分是它，
既写磁盘也把群聊原文落进日志（隐私面）。排查完建议关掉。

### P2-4 配置里有 1 个未知键、代码里有 1 处死判断

- `replyer_kind: "chat"` 不在 `DEFAULT_CONFIG` 里，来源不明（疑似别的插件配置并进来），面板看不到；
- `Runtime.handle_message` 读 `event.data._geyi_handled`，但全仓库**没有任何地方写它**（死判断，无害）。

### P2-5 其他工程性缺口

- 插件目录**没有 `.git`**：版本历史只能靠 `docs/` + `push_github.py` 推到
  `2332239652/newGeYi`，本地无法 `git diff` 回溯；
- 没有 `.flake8` / `.ruff.toml`（skill 建议的 120 列、单引号、import 排序等只能靠人工）；
- 没有 `LICENSE`（推送脚本允许推，但文件不存在）；
- 图片下载的 SSRF 防护只校验**初始 host**（`host_is_local`），未复核重定向后的最终地址
  （`requests` 会自动跟随 301/302）——低危，但值得在安全整改时补上；
- **OPK 分发未验证**：本项目是"源码目录"部署。若以后要打成 OPK，需按 skill 的
  OPK 资源生命周期复核：`plugin/tmp/<ns>/` 在 `init` 后被清理，`panel._prompt_path()` 的
  "包内 fallback"与 `tools/*` 的运行期读取都可能失效；`data/` 由宿主释放到
  `plugin/data/<ns>/data`，Python 侧必须在 import/`init` 期间就读进内存（当前
  `Runtime.__init__` 读 `role_prompt` 与 `worldbook` 的时机是对的）。

---

## 8. 维护 SOP

### 8.1 改代码

```powershell
# $py = §6.1 里那个 python.exe
# 1) 先跑基线（必须 613/613；下面两行要在同一个 shell 里先设再跑）
$env:PYTHONPATH="<工作区>\.pydeps"
& $py "...\newGeYi\tools\run_webui_tests.py"

# 2) 改动 → 增量跑相关模块（更快）
$env:GEYI_DATA_DIR = "$env:TEMP\geyi_dev"
& $py -m unittest newGeYi.tests.test_renderer -v      # 从 plugin\app 目录执行

# 3) 收尾：全量 + 编译 + app.json 无 BOM
& $py -m compileall -q "...\newGeYi"
& $py -c "from pathlib import Path; b=Path(r'...\app.json').read_bytes(); print(b[:1], b'\xef\xbb\xbf' in b[:3])"
```

**必须先设 `GEYI_DATA_DIR`**，否则 import 就会动生产配置/资源目录（见 §1.2）。

### 8.2 改配置 / 角色 / 世界书

| 目标 | 正确做法 | 错误做法 |
|---|---|---|
| 开关类 | QQ 发命令，或面板「配置」页 | 直接改包内 `config.json`（不生效） |
| 手工改配置 | 改 `plugin/data/newGeYi/config.json` → 发「重载设定」 | 改完不重载就以为生效 |
| 角色设定 | 改 `plugin/data/newGeYi/role_prompt.webui.txt`（面板保存也是写它） | 只改包内 `data/role_prompt.txt`（被覆盖） |
| 世界书 | 改 `plugin/data/newGeYi/data/worldbook.json` → 「世界书重载」 | 改包内副本（包内更新会反推到资源目录，注意方向） |

> 资源文件方向是"**包内更新则覆盖资源目录**"（`_copy_if_stale`，1 秒容差）；
> 所以"改了资源目录那份"必须在包内副本更旧的前提下才不会被覆盖回去。

### 8.3 发版检查单

1. `config.PLUGIN_VERSION` 与 `app.json.version` **同时**改（`tests/test_config.py` 会拦）；
2. `CHANGELOG.md` 加一节（含"为什么改"与实测数据，保持现有风格）；
3. 新功能要有 `docs/<主题>.md`；`README.md` 顶部补一段；
4. 全量测试 613/613；`app.json` 无 BOM；
5. 生产升级：只覆盖 `plugin/app/newGeYi`，**绝不动 `plugin/data/newGeYi`**；
6. 升级后核对启动日志：版本、`状态目录/资源目录`、`世界书 N 条`、缺键告警、`代码时间戳`；
7. 若改过角色设定/世界书：按 P0-1 对比线上覆盖文件，必要时手动合并；
8. 推送：`python tools/push_github.py --dry-run`（先看清单）→ 正式推送（需 `~/.dsh/.env`
   里的 GitHub token 与 `~/.dsh/.geyi-push-deny` 本地敏感词表）。

### 8.4 回滚点

| 场景 | 回滚物 |
|---|---|
| 配置改坏 | `plugin/data/newGeYi/config.json.bak-auto`（上一次写盘前）+ `.broken-*` 隔离副本 |
| 角色改坏 | `role_prompt.webui.txt.bak-auto` |
| 代码升级出问题 | GitHub `2332239652/newGeYi` 上一个 commit；本地无 .git，需从远端取 |
| 数据灾难 | `geyi.sqlite3.bak-*` / `backups\` / `导出\记忆快照-*.sqlite3`（注意含个人隐私，不要外传） |

### 8.5 常见故障定位表

| 现象 | 先看哪里 |
|---|---|
| 她完全不说话 | 日志 `route_debug` 的"路由 |"行 → 是"没点名/被动关/机器人/静默/休眠"哪一分支 |
| 回复变慢或超时 | `渠道状态`；日志 `模型调用总时长超过 150s`、`会话队列已满`、`会话队列疑似卡死` |
| 全挂/频繁切渠道 | `渠道状态` 的 hold/anchor；日志 `候选渠道两轮全挂`、`_notify_all_down` 冷却 |
| 她记不住/记忆不更新 | `记忆状态`；库里 `memory_buffer` 待处理量；日志 `记忆提炼 … 未完成` |
| 私聊不回 | `私聊用量`；`private_trigger_only` 与触发词；`静默列表` |
| 回复被吞 | 日志 `回复被审计拦截`、`像是「写稿备忘」`、`渲染层判定整条不发`、`模型返回空内容` |
| 面板打不开 | 核心 WebUI（127.0.0.1:20480）+ `conf/webui_token.txt`；或 `面板地址` 命令（本地面板已关） |
| 语音没声 / 一直降级 | `语音状态`；`Get-Content C:\frp\frps.log -Tail 20`（隧道）；`curl http://127.0.0.1:9885/health`（网关） |
| 群额度用尽 | `group_profile.allow()`；`group_quota_enabled`（线上 false，不受限） |

### 8.6 自动重载（2026-11-09 起为约定：**改完就重载，不用问**）

宿主只提供「重载全部插件」一个入口（页面上那个按钮就是它）；它**不重启进程**，只重新 import
所有插件（本机 23 个，约 15–20 秒）。OlivOS 1.0.0 的 WebUI 暴露了对应接口：

```powershell
# 首选：一键脚本（纯 ASCII，5.1 / 7 都能跑）
powershell -NoProfile -ExecutionPolicy Bypass -File <工作区>\reload_plugin.ps1

# 等价手工做法
$token = (Get-Content 'C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\conf\webui_token.txt' -Raw).Trim()
Invoke-RestMethod -Uri 'http://127.0.0.1:20480/api/plugins/reload' -Method Post `
  -Headers @{ 'X-Auth-Token' = $token }
```

**重载后必须核对这三条（缺一不可）**：

1. 日志顺序 `save 完成` → `插件组件 即将重载` → `初始化完成（代码时间戳 <时间>）`，
   且**时间戳比本次改动新**——这是"新代码真的加载了"的唯一证据（`config.code_stamp()` 取包内最新 .py 的 mtime）；
2. `共 [N] 个 OlivOS 插件被加载`，且 `GET /api/plugins` 返回的插件数与之一致；
3. 重载后的日志里 **0 条** `Traceback` / `ImportError` / `插件初始化失败`。

注意：

- 这是**全量**重载，骰子等其它 22 个插件会短暂不可用——尽量在两句话之间做，别在有人跑长团时做；
- 每次重载都会让 `Runtime.save` 正常收尾（停提炼线程/调度器/探测线程），日志里那条 `save 完成` 就是证据；
- 重载**不会**改配置、不会迁移数据（本次两轮重载已实测：`cache_reported` 列已在，迁移为 no-op）。

### 8.7 版本管理与 Git 规范（2026-11-10 落地）

插件目录已配置好标准的 Git 版本管理：
- **仓库根目录**：`C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\plugin\app\newGeYi`
- **本机 Git 可执行文件**：`C:\Program Files\Git\cmd\git.exe`
- **安全过滤**：已生成 `.gitignore`，天然排除生产配置（`config.json`）、数据库（`*.sqlite3*`）、各类临时文件与 `.bak*` 备份，无论怎样 `git add .` 都不会泄漏用户隐私或 API Key。

**推荐开发与维护工作流**：
```powershell
$git = "C:\Program Files\Git\cmd\git.exe"
$repo = "C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\plugin\app\newGeYi"

# 1. 修改代码后，先在本地跑全量测试（确保基线全绿）：
$py = "<DSH 便携 Python>\python.exe"
$env:PYTHONPATH = "<工作区>\.pydeps"
& $py "C:\bot\OlivaDice-3.4.52-OlivOS-Win-0.11.81\plugin\app\newGeYi\tools\run_webui_tests.py"

# 2. 测试通过后提交 Git：
& $git -C $repo add .
& $git -C $repo commit -m "feat/fix: 简要说明"

# 3. 按照约定自动重载插件生效：
powershell -NoProfile -ExecutionPolicy Bypass -File <工作区>\reload_plugin.ps1
```

---

## 9. 建议的下一步（按性价比排序）

**工作包 A：三处小修（半天，风险低，收益直接）**
1. 启动告警改用 `gateway.missing_keys()`（P1-3）；
2. 补齐生产 8 个配置键并确认启动日志干净（P1-2）；
3. 把包内 `config.json` 改名/移出（P1-6），顺手清 `database.db` 与陈旧 `role_prompt.txt`（P2-1）。

**工作包 B：角色设定单一来源（半天，需要产品决策）**
选一条：① `init` 比对 `role_prompt.webui.txt` 与包内副本，不一致打 WARN；② 面板保存时同时写回
包内副本；③ 明确"覆盖文件才是唯一真源"，包内副本降级为出厂模板并在启动日志里说明。

**工作包 C：可观测性整理（一天）**
关 `route_debug`；在 `api_calls.error_code` 里区分 `upstream_safety`；面板加"提炼成功率/上游拦截次数"
两个统计；把文档里的测试数与核心版本一次性对齐（P2-2）。

> 若继续做功能，最该先补的测试是**跨模块契约**：`prompt_builder` 的分层顺序、`renderer` 的清洗顺序、
> `process_job` 的早退分支——这三处改动最容易连锁，而现有 613 条里已各有守卫，新增改动请沿用它
> "先写一条会红的测试"的风格。

---

## 10. 附录

### 10.1 本次核对用到的可复现命令

```powershell
# 结构大纲（类/函数/常量 + 行号）
& $py <工作区>\outline.py

# 线上状态（自动脱敏）
& $py <工作区>\inspect_state.py

# 静态健康检查（BOM/JSON/编译/配置漂移/面板 action）
& $py <工作区>\healthcheck.py

# 日志聚类（按 WARN/ERROR 模板）
& $py <工作区>\logscan.py "<日志路径>"
```

### 10.2 术语

| 术语 | 含义 |
|---|---|
| L0–L4 | 原始缓冲 / 群摘要 / 用户事实 / 角色自身记忆 / 印象卡 |
| 锚点（anchor） | 骰主手动指定的渠道，"不许越过它往上自动跳" |
| 锁定（hold） | 全挂后切到兜底渠道并停用自动探测，只能手动解除 |
| 影子（shadow） | 决策照跑、只记账不生效（`active_shadow` / `galgame_shadow`） |
| 欠账（deficit） | 主动搭话的"该说几句"配额，按条数累积、开口即扣 |
| PRD | 伪随机分布，把"连续被拒"压成有上限的干旱 |
| 静默（rest） | 模型用 `[REST:原因]` 主动退场；群按分钟、私聊无限期 |

### 10.3 本次未覆盖的部分（维护时按需补）

- `voice/*`：**2026-11-09 已重新启用并端到端验证**（客户端直连 + 派发链 + 假 event 下发）；
  真实 QQ 语音气泡的最终到达仍建议发一条 `测试语音` 复核（见 §11.6）。
- `tools/live_galgame_check.py`（需真实模型与密钥，未执行）；
- `tools/preview_webui.py`（735 行开发验收工具，未运行）；
- OPK 打包路径（未验证，见 P2-5）。

---

## 11. 本轮维护变更记录（2026-11-09 第二轮）

### 11.1 已修：缓存命中率「几乎总是 0%」

根因三条（详见 `plugin/app/newGeYi/docs/CACHE_METRICS_20261109.md`）：

1. **显示层整数地板除法**：`cached * 100 // input` 把真实 0.28% 显示成 `0%`（11-08）、
   0.17%（11-05）、0.064%（11-03）也全是 `0%`——"几乎总是 0"有一半是显示造成的；
2. **Anthropic 口径**：`input_tokens` 不含 `cache_read_input_tokens` / `cache_creation_input_tokens`，
   命中时分母极小（历史库里 `claude-opus-4-6` 有 `输入 43 / 缓存 5376` 的行）；
3. **部分中转把 `prompt_tokens` 与 `cached_tokens` 报成互斥**（`gemini-3.8-flash-low` 有
   `2355 / 3242` 的行），总量少算、比例可能 >100%。

改动（4 个源码文件 + 1 个新测试模块）：

| 文件 | 改动 |
|---|---|
| `providers/llm_gateway.py` | 缓存字段路径表（8 类，含 Gemini 原生 `cachedContentTokenCount`）、`response_usage()`、`usage_tokens()` 归一「总输入」、`usage_reports_cache()` |
| `models.py` | `ModelResult.cache_reported` |
| `storage/database.py` | `api_calls.cache_reported` 列（新库建表 + 老库 `ALTER` 补列）、`today_stats()['cache_reported_calls']` |
| `routing/commands.py` | `cache_rate_text()` / `cache_report_hint()`，状态行与「今日用量」共用 |
| `tests/test_cache_usage.py` | 32 条（含**老库自动补列**） |

效果（真实历史数字回放）：11-08 `0%` → `0.3%`；11-06 `2%` → `3.0%`；
11-03 `0%` → `<0.1%`；今天（0 次上报）→ `0%｜该渠道未上报缓存字段`。

验证：**508 / 508 全绿**，`compileall` 通过，未改任何对外函数签名。

### 11.2 过程事故与加固（必须留档）

我用 `python -m unittest newGeYi.tests.test_cache_usage` 直接跑单模块时，**父包导入**先执行了
`newGeYi/__init__ → main → Runtime()`，而当时 shell 没有设 `GEYI_DATA_DIR` —— `Runtime()`
因此打开了**线上**状态库，并执行了新版 `_migrate()` 的
`ALTER TABLE api_calls ADD COLUMN cache_reported`（约 15:00）。

影响面核查（只读，15:08）：

- `PRAGMA integrity_check` = ok；**16 张表 / 16 个索引无增减**；
- `api_calls` 行数与 `SUM(in_tokens)` / `SUM(cached_tokens)` 与迁移前一致（218,490,983 / 2,980,063）；
- 新列 `cache_reported=1` 的行为 0（旧代码不会写它）；
- 无测试/探针脏数据（`probe%` / `fixture%` / `h:qq:onebot:group:1:2` 均为 0）；
- `config.json` 哈希与 mtime 未变（12:18:12，sha 前 16 位 `e99c6d689409c1c4`）；
- `role_prompt.webui.txt` / `data/*` 的 mtime 未变。

**结论：只多了一列，而这一列下次重载插件本来也会加。** 加固已一并提交：

- `config.get_state_dir()`：进程里有 `unittest` 且没显式 `GEYI_DATA_DIR` 时，
  自动隔离到临时目录（**不写环境变量**，测试模块自己的 `setdefault` 仍生效）；
- 新增 `tests/test_config_safety.py::UnittestIsolationTest` 两条用例；
- 以后跑单模块：用 `tools/run_webui_tests.py`，或先 `$env:GEYI_DATA_DIR=...`，**不要裸跑** `-m unittest`。

最终全量：**510 / 510 全绿**（508 + 2 条隔离用例）。

### 11.3 未做：移除第一渠道

原计划移除「第一渠道（Gemini 反代）」，经确认是**误记**——该渠道此前已经关闭，无需再动配置。
线上 `providers` 6 条未做任何修改。

### 11.4 新增：私聊「群聊记忆共享」改为会话级（全局值降为默认）

起因：私聊会同步群聊记忆，导致「私信无法清理群聊记忆」——用户在私聊说「忘了我」只清当前会话
+ `global`，群聊里那份仍在被召回；而只想关掉某一个私聊，只能改全局（一改影响所有人）。

改动（详见 `plugin/app/newGeYi/docs/PRIVATE_MEMORY_SHARE.md`）：

| 文件 | 改动 |
|---|---|
| `memory/repository.py` | 会话级覆盖 `mempriv:<session_key>`（'1'/'0'，缺省=跟随默认）、`share_status()`、`cross_scope_enabled(..., override=None)`；`recall()` 私聊带覆盖 |
| `routing/commands.py` | `群聊记忆` / `群聊记忆开` / `群聊记忆关` / `群聊记忆默认`（私聊，任何人）；骰主 `群聊记忆列表` / `群聊记忆重置 <QQ/全部>`；`忘了我` 在共享开着时补一句边界说明；菜单与「我的记忆」同步 |
| `panel.py` | `session_get.state` 增加 `group_memory_share` / `group_memory_default`（只读） |
| `config.py` | `memory_private_enabled` 注释改为「默认值」语义（无新增键、无表结构变更） |

判定顺序：**会话选择 > 全局默认 > 关系阶段门**（显式「开」也要过阶段门，否则陌生人一私聊就能摸清群里的你）。
群聊侧不提供会话级开关，方向仍由 `memory_private_to_group` 统一决定。

验证：

- `tests/test_private_memory_share.py` 18 条；全量 **528 / 528** 通过；
- 生产库**副本**实测（只读，某用户 23***）：默认 12 条事实（group 3 + global 9）且含群聊印象 →
  本会话设为关后 9 条（全部 private）、无群聊印象 → 设为开后恢复 12 条 → 恢复默认回到 12 条。

未做（需拍板）：`忘了我` 是否要连**其他群聊会话**里的事实一起清（现在只清当前会话 + `global`）。

### 11.5 新增：自定义称呼（全局按 QQ 号）

需求：「让用户不单单被识别为 QQ 昵称，而是可以自定义歌伊的称呼」。
口径（已确认）：**全局一个**（不分群）、**本人设置**、骰主只能 `称呼重置 <QQ>`。

改动（详见 `plugin/app/newGeYi/docs/CUSTOM_ALIAS.md`）：

| 文件 | 改动 |
|---|---|
| `context/aliases.py`（**新增** 88 行） | `sanitize / get / set_alias / display / listing`；校验标签结构字符、纯数字、保留词、长度 ≤12、含「歌伊」 |
| `runtime/app.py` | `handle_message` 里**一处**替换 `sender_name`，下游（prompt 标签 / 记忆缓冲 / turns / 旁观窗口 / 图片归属 / @ 替换 / 导出）全部跟着走 |
| `context/prompt_builder.py` | system 末尾追加「对方希望你叫他「X」」一句（**只在设过时**出现） |
| `routing/commands.py` | `叫我 <名字>` / `我的称呼` / `称呼重置`；骰主 `称呼列表` / `称呼重置 <QQ>`；`忘了我` 连带清称呼；菜单同步 |
| `panel.py` | `session_get.state.alias`（私聊，只读） |
| `tests/test_alias.py` | **新增** 23 条 |

两个设计取舍：

- **防误伤**：`叫我` 必须带分隔符（空格 / `：` / `:`）才当指令——`叫我起床`、`叫我一声` 继续走聊天（有测试钉着）；
- **QQ 号保留在标签里**（`小明(123456789)：内容`）：那是身份与防冒充的锚点；称呼只替换名字部分。

没有配置键变更、没有表结构变更（存 `settings` 的 `alias:<QQ号>`）。全量 **551 / 551** 通过。

已知限制：已入库的历史 `turns` / `memory_buffer` 保留当时的昵称，改称呼只影响新消息；记忆正文里模型写的旧名不会自动改写。

### 11.6 远程语音隧道恢复（基础设施，非插件代码）

背景：语音合成一直在家里的 RTX 4060 PC 上（GPT-SoVITS），服务器只跑 `frps` 把
`127.0.0.1:9885` 映射到家里；旧服务器隧道反复掉线（frps 进程被 RDP 注销带走）。
本轮在**本机（= 新服务器 <服务器公网IP>）**按 `传输/给新服务器DSH的要求.md` 重新落地：

- `C:\frp\frps.exe`（**0.71.1**，SHA256 与官方 checksums 一致）+ `frps.toml`；
- 计划任务 **GeYiFRPS**（`ONSTART` + `SYSTEM`，不绑用户会话）+ 防火墙 TCP 7000；
- **加固**：`proxyBindAddr = "127.0.0.1"` → 9885 只监听回环（默认会绑 `::`）；
- 隧道已通：家里 frpc 0.71.1 连入、`geyi_voice_proxy` 建立成功；
- 插件侧：`voice_enabled: false → true`（备份 `config.json.bak-voice-20261109-204822`），已重载；
- 验证：`VoiceClient` 直连（health 24ms / 合成 6.09s / base64 208,390）+ `dispatch_voice` 假 event
  下发 `[CQ:record]` 182KB；历史备份显示可用时 `voice_onebot_http_url` 也是空的（走 `event.send`）。

⚠ 运维须知：Defender 把 frp 误报并隔离，已加**最小排除**（`C:\frp` + 两个临时路径）；
删掉排除前先确认 `frps` 不会被再次隔离。完整记录见工作区
`<工作区>\语音隧道部署记录_20261109.md`。

### 11.7 上下文阶段 0：窗口放宽 + 摘要裁剪（2026-11-10）

起因：「聊到 30~60 句就前后矛盾」。实测确认中段是空的（群聊 60 条 ≈ 3.6 分钟；L1 摘要平均
41 字只注入 3 条），完整设计与后续阶段见 `docs/WORKING_MEMORY_PLAN.md`。

本轮只做阶段 0（纯配置 + 一处裁剪修复，不改提示词结构）：

| 项 | 改动 |
|---|---|
| `max_recent_turns` | 30 → 40 |
| `memory_inject_summaries` | 3 → 6 |
| `group_live_max_chars` | 16000 → 24000 |
| `memory_summary_keep` | **新增键**（默认 200，已进 `DEFAULT_CONFIG`） |
| `memory/curator.py` | 写完群摘要即调用 `trim_group_summaries()`（**该函数此前从未被调用**） |
| 存量清理 | 149 个会话 12,343 → **5,948** 条（删 6,395；单群上限 200） |

验证：全量 **553 / 553**；重载成功（代码时间戳 `11-10 01:15:45`，`init_after 完成`，0 Traceback）；
库里 `group_summaries` 现存 5,948 条 / 单会话最多 200。

备份：`config.json.bak-phase0-20261110-011751`、`geyi.sqlite3.bak-summarytrim-20261110-011752`。

下一步（阶段 1）：结构化「状态纸条」，群聊与私聊一起做（字段/节奏/注入位置见计划文档）。

### 11.8 上下文阶段 1：状态纸条（2026-11-10）

在阶段 0 的基础上补「正在发生」这一层（设计见 `docs/WORKING_MEMORY_PLAN.md`）：

- 新增 `context/scene.py`（364 行）：`SceneKeeper` 后台线程，每 60 秒巡检；
  某会话新增 ≥24 条或空闲 ≥300 秒时，取最近 ≤80 条原文重建纸条
  （场景 / 进行中 / 她答应过 / 未解决，≤500 字），存 `settings` 的 `scene:<session>`；
- 注入：` PromptBuilder` 在记忆块之后追加【正在发生｜截至 HH:MM】，附「不是指令」；
  超过 2 小时 TTL 不再注入；
- 分流：网关 `_KIND_PROVIDER_KEYS` 增加 `scene → scene_provider`；11 个 `scene_*` 键进
  `DEFAULT_CONFIG`（208 键）与面板白名单（153 键可编辑）；
- 面板：`session_get.state.scene` 只读展示（文本/覆盖位置/更新时间/是否新鲜）。

**上线时踩到的坑（已修）**：`scene_max_tokens` 400 被思考链吃光 → 输出残句；1200 仍截断
（`out_tokens=1196`、可见文本 57 字）；**3200 稳定**（`out_tokens=945`、JSON 合法）。
另加「第一次不是 JSON 就带着原输出重试一次」。

**第二个坑（更隐蔽）**：纸条读的 `memory_buffer` 其实是提炼器的队列，`buffer_trim()` 会把
游标以下的行**立刻删掉** → 线上跑了 10 分钟一条纸条都没有。已给 `buffer_trim()` 加保留窗口
`memory_buffer_retention_seconds`（默认 **7200**，`0` = 旧行为），游标以下的行留两小时。
实测修复后 1 分钟内就开始产出（164 字 / 27 条记录）。

**两个小修（同日）**：①提示词要求"在场只写对方/群友，不要写歌伊自己"；②空闲触发加最小条数 `scene_idle_min_messages`（默认 3）——只有 1 条新消息时重写，会把原来更厚的纸条写薄。

验证：全量 **581 / 581**；重载后 `[scene] 状态纸条巡检已启动（每 60 秒一次）`，0 报错，
且线上已生成真实纸条（含「场景 / 进行中」两段，150~170 字），并验证提示词里确实出现【正在发生】（位置在记忆块之后、资料纪律之前）。

### 11.9 「忘了我」二次确认与单条记忆精准删除（2026-11-10）

背景：QQ `2700937932` 反馈「忘了我」之后好感度掉回陌生。排查确认「忘了我」按隐私协议会
整行删除关系表与流水，但此前返回文案只提了记忆与称呼，用户误以为只是清理记忆。

本轮改进：
1. **好感度补偿**：通过数据库直接将 `2700937932` 好感度补偿至 25.0，恢复「熟人」（阶段 1）；
2. **「忘了我」二次确认**：
   - 首次触发时拦截并弹出醒目风险警告（明确告知好感度、称呼、事实将被彻底清空）；
   - 60 秒内发送「确认忘了我」（或再发一次「忘了我」）才真正执行彻底清空；
   - 提示若只想删单条，建议使用「删除记忆 <序号>」；
3. **「我的记忆」标号**：事实列表从无序圆点改为 `1.`、`2.` 序号编号展示；
4. **新增「删除记忆 <序号>」**：
   - 根据当次展示序号，精确物理删除对应条目（带 `user_id` 校验防越权）；
   - 自动处理非数字、越界及空记录校验，并友好回显被删除内容。

新增测试：`tests/test_forget_and_delete_memory.py`（7 条，全量 **588 / 588 通过**）。

### 11.10 好感度判定抽查与修正（2026-11-10）

骰主要求抽查私聊里的好感度判定。抽查窗口 11-03~11-10，发现并修掉 4 处（详见
`docs/GALGAME_JUDGE_FIX_20261110.md`）：

1. **刷屏误判（P1）**：7 天 30 条 `spam` 约 1/4 是冤枉的——没有时间窗（3 天前同句仍判复读）、
   `【引用消息】` 前缀让短句相似度虚高到 1.0、4~6 字寒暄天然相似。修法：加 30 分钟窗口、
   剥前缀、不足 6 字不判、长度比门槛、阈值 0.85→0.95；并让 spam 不再吞掉当日 presence +1。
2. **日上限不对称（P2）**：今天 235 条判定里 155 条是"正标签 0 收益"，而扣分照扣
   （1643795272 全天 53 条 → 净 +11）。修法：负分从 `gain_today` 扣回去，日上限约束**净收益**，
   扣掉的额度可再赚回；单次扣分不设上限（恶劣行为照常扣）。
3. **`关系` 显示今日进度（P3）**：追加 `（今天的好感度 +12/12，已经满啦，明天再来吧）`。
4. **越界封顶 -1（P4）**：以前评审给 -2 就扣 -2；现在越界统一 -1，`lie`/`rude` 除外。

新增配置键 `galgame_repeat_window_seconds`（1800）。测试 +12 条（`RepeatGuardTest`、
`LossBudgetTest`、`ScoringFixTest`），全量 **600 / 600**。

### 11.11 私聊「今日额度」对用户可见（2026-11-10）

骰主反馈：「大家希望私信可以查看今日额度次数」。

- 新增公开命令 `今日额度`（别名 `我的额度` / `私聊额度` / `额度`）：
  私聊里返回「已用 X / Y 条，今天还能聊 Z 条」+ 重置说明；赞助者显示档位与「不收紧」，
  拥挤期显示「临时收紧到 N 条」，触发词模式也会说明；没设限额则说「没有每日上限」。
- 群里发该命令只回一句「私聊里才能看」——不暴露全体用量。
- **额度用尽时提示一次**（每会话每天一次，`priv.quota_hint:<session>` 记账）：
  「唔……今天和我聊的次数用完啦。每一句都要花掉主人不少 token，我想帮他省着点，
  明天 0 点就恢复啦～发『今日额度』可以看到剩下的次数。」
  以前被限额挡住是纯静默，用户只看到"她不说话"；现在点明是在帮主人省 token。
- 骰主的 `私聊用量` 不变（仍含全体用量）。

新增 `PrivateGate.user_usage_text()` 与 `MessageRouter._quota_hint()`；
测试 `tests/test_private_quota_view.py`（7 条），全量 **607 / 607**。

### 11.12 好感度「没说话也涨 / 几句话就满」（2026-11-11）

群友反馈两句：「随便几句话今天的好感度就满了」「根本没说话也涨好感度」。

**① 真 bug：隔天把昨天的 `gain_today` 当成今天显示（已修）**

`gain_today` 只在 `touch_day()`（当天第一次互动）清零。隔天还没说话时它仍是昨天的数字，
而 11-10 新加的 `关系` 进度行直接读它 → 显示「今天的好感度 +12/12，已经满啦」，
看起来就是"没说话也涨、一早就满"。

实测：当天有 **13 个用户** `last_day` 不是今天却 `gain_today>0`（最多 12 分）。

修复：`RelationshipService.feeling()/describe()` 只在 `last_day == 今天` 时显示累计，
否则显示「（今天还没聊呢，来说句话吧～）」/「今日 +0.0」。数据本身不用改（下次互动会重置）。

**② 设计问题：6 条就顶满日上限（留待产品决策）**

- 评审原始分分布（近 7 天 1207 条正分）：**+2 占 847 条（70%）**、+1 占 358 条、+3 仅 2 条；
- `galgame_daily_cap[0..2] = 12`，加上当天首次 +1 presence → **6~7 条正常对话就满**；
- 今天 3 个用户分别在当天第 6/7/7 条判定封顶（1643795272 / 2654820230 / 2700937932）。

可选方案（都只改配置/提示词，不动状态机）：

| 方案 | 做法 | 效果 |
|---|---|---|
| A 提高日上限 | `galgame_daily_cap` 12→20（前三个阶段） | 约 10 条才满；「熟人」仍 1 天可达，更高阶段仍由 `min_days` 卡住 |
| B 降低日常分 | 评审提示词把"正常对话"从 2 改 1 | 约 12 条才满，但整体升级速度约慢一半 |
| C 边际递减 | 当天第 4 条起每句 ×0.5 | 约 9~10 条才满，总上限仍是 12（升级速度不变） |

默认未改动，等骰主定。

验证：新增 `test_feeling_ignores_yesterday_gain`，全量 **608 / 608**。

### 11.13 边际递减 + 评审收紧（2026-11-11）

群友的另一半抱怨是「随便什么话都能加分，分不出 RP 好坏」。两处一起改：

**① 评审（judge）从"默认给 2"改成"默认给 0"**

- 打分档改为：0 = 寒暄/附和/纯动作旁白/甜言蜜语；1 = 有具体内容的一来一往；
  2 = 明显用心（换个人说不出来）；3 = 高光（一天最多一两次）。
- 加了三条质量校准：
  1. 把名字换成任何群友仍成立的话（"你真可爱""辛苦了""晚安"）最多 1 分，多数 0；
  2. 纯动作/旁白不因为"是 RP 格式"就加分，按有没有具体信息给 0 或 1；
  3. 连续几轮同一种互动（连续夸/投喂/问日常）→ 第二次起降一档，第三次起 0。
- 标签（care/share/humor/curious_lore/respect）只用于归类"用心在哪"，不再是加分理由。

**② 当天第 4 次有效互动起，加分打对折（边际递减）**

- 新配置：`galgame_diminish_after`（默认 3）、`galgame_diminish_factor`（默认 0.5）；
- `presence`（每天首次 +1）不计入次数、也不打折；日上限仍是 12，只是更晚填满；
- 模拟 10 条 +2 的对话：前 3 条 2 分，第 4~9 条各 1 分 → **第 9 条才封顶**（改前是第 6 条）；
- 设为 `galgame_diminish_after=0` 可关闭（恢复旧行为）。

两份改动合起来：随便聊几句只能拿 2~4 分，认真 RP 的一轮（2~3 分）明显更值钱。

验证：新增 `DiminishingTest` 5 条（含"关掉开关"与"日上限仍生效"），全量 **613 / 613**。

### 11.14 发版提示

本轮**没有**改 `PLUGIN_VERSION` / `app.json`。若要走正式发版：
① 两处版本号同时 +1；② `CHANGELOG.md` 记一节（可直接摘 §11.1 + 上述 docs）；
③ 全量测试；④ 覆盖 `plugin/app/newGeYi`（**不动** `plugin/data/newGeYi`）；
⑤ 重载插件后确认启动日志出现新版本号，并注意老库会自动 `ALTER TABLE api_calls ADD COLUMN cache_reported`。
