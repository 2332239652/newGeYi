# newGeYi (歌伊圣堂)

[![OlivOS Plugin](https://img.shields.io/badge/OlivOS-Plugin-blue.svg)](https://github.com/OlivOS-Team/OlivOS)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**newGeYi** 是一款为 [OlivOS](https://github.com/OlivOS-Team/OlivOS) 深度定制的模块化角色扮演（RP）AI 群聊插件。

它不是"单轮调用 + 提示词包装"，而是把长期群聊实战里踩过的坑一条条写进代码：**多渠道故障转移与手动锚点**、**四层自动记忆**、**多模态图片理解与发送者归属**、**数据驱动表情包**、**日语原声伴生语音**、**输出安全审计硬拦截**、**自我保护静默**、**自适应主动搭话**。

---

## 核心特性

- 🔄 **多渠道智能路由 + 手动锚点**
  - 支持 OpenAI / Anthropic 两种协议，渠道可各配模型、超时、优先级；
  - 一条请求内按顺序级联：当前渠道失败就依次往下试，`fail_threshold` 默认 1（失败一次即判死，下一条直接走备份）；
  - **手动锚点**：`切换渠道 X` 会把 X 设为"顶"——故障时只在其下方找备份，后台探测也只爬回它为止，绝不会自作主张回到额度已用完的免费渠道；
  - 后台每 5 分钟探测一次，逐级爬回，并顺手复位"挂着不可用标签"的低优先渠道；
  - `manual_only` 可选渠道（如昂贵模型）永不自动启用，只能手动选中；
  - 支持 `kinds` 分流：聊天与记忆提炼可以走不同渠道，兼顾速度与成本。
- 🧠 **四层自动记忆**
  - 异步提炼：**群摘要 / 群友事实 / 角色自我记忆 / 用户长期画像**；
  - 重要度 + 时间衰减 + 命中次数打分，按需注入，拒绝长上下文臃肿；
  - 记忆按 QQ 号隔离，同名群友不会串味。
- 🖼️ **多模态图片理解（含发送者归属）**
  - 群友发图智能理解，带相关性闸门，杜绝"见图就评"；
  - 回溯他人旧图时会在正文里写明「附图来源：图1 是 小明(123) 刚刚发的」，模型不会把别人的图认成当前说话人发的；
  - 她自己发出的立绘也进上下文，群友追问"你刚发的图"时能认出来。
- 🎭 **数据驱动表情包**
  - 语义化 `[EMOTE:名称]`，名称/文件/适用时机全部由 `emotes.json` 声明；
  - 代码层三重节流（单条上限、滑动窗口、每小时硬顶）。
- 🎙️ **日语原声伴生语音**
  - 模型在回复末尾写 `[VOICE:情绪|日文台词]`，插件调用自建 TTS 网关合成并随消息发出；
  - 15 个情绪卡槽（日常/撒娇/吐槽/晚安…），台词长度、标点、称呼都有约束；
  - 自动重采样到 24000Hz 并裁掉首部静音，避免 QQ 端"点了没声音"。
  - 需要自备本机 TTS 服务（见下方「伴生语音网关」）。
- 🛑 **自我保护静默**
  - 遇到持续谩骂、过当调戏、针对角色人格的羞辱、被同一人反复纠缠时，模型可以输出 `[REST:原因]` 主动退场；
  - 该会话静默 10 分钟（可配），并私聊通知骰主（附触发者与原话）；
  - 静默期内被动、主动、戳一戳全部停止，但消息照常进旁观窗口与记忆缓冲；到期自动恢复，骰主可 `解除静默` 提前结束。
- 🛡️ **回复安全审计（代码层硬拦截）**
  - 拦截本机盘符路径、密钥凭据、厂商安全报错、**模型思考过程（含英文 `**My Response**` 式前言）**；
  - 命中即整条静默丢弃；思考前言会先尝试精确剥离，剥不干净才整条丢弃。
- 💬 **真人群聊体验细节**
  - 短句自动折叠、话没说完先按住、AI 味黑名单；
  - 一条消息一段、不换行不空行；`（）` 只写动作，禁止内心戏。

---

## 目录结构

```text
newGeYi/
├── main.py                 # OlivOS 事件入口与消息转发
├── config.py               # 默认配置、环境变量、动态路径解析
├── models.py               # 数据结构
├── app.json                # OlivOS 插件清单
├── config.example.json     # 标准配置示例
├── runtime/                # 装配、会话调度、自我保护静默（rest.py）
├── routing/                # 消息路由、指令、权限、主动搭话闸门
├── context/                # Prompt 构建、实时旁观窗口、图片、跨插件感知
├── providers/              # LLM 网关（多协议 / 故障转移 / 手动锚点）
├── memory/                 # 记忆提炼、检索、世界书、导出
├── storage/                # SQLite 持久化
├── presentation/           # 渲染、审计、表情包组装
├── voice/                  # 伴生语音：客户端、管理器、情绪预设
└── data/                   # 静态资源（角色提示词、世界书、表情包表、立绘）
```

---

## 快速上手

### 1. 安装

把仓库放进 OlivOS 的插件目录，结构如下：

```text
OlivOS/plugin/app/newGeYi/
```

启动 OlivOS 后插件会自动把 `data/` 同步到 `plugin/data/newGeYi/data/`（该目录每次加载都会被重建，**不要**往里放需要保留的东西）。

### 2. 配置

配置读的是 **`plugin/data/newGeYi/config.json`**（状态目录，重载不丢）。首次可直接把 `config.example.json` 复制过去再改。

> `config.example.json` 里的值**等于代码里的默认值**（只有 api_key / endpoint / 语音口令做了脱敏，语音默认关）。
> 也就是说：配置里漏掉某个键，行为与示例一致，不会突然跳一个数量级。
> 默认值偏"放开"（`daily_call_budget` 很大、全天可主动搭话），要省钱请自行调小。

最小可用配置（单渠道）：

```json
{
  "api_endpoint": "https://api.openai.com/v1/chat/completions",
  "api_key": "sk-你的Key",
  "fast_model": "gpt-4o-mini",
  "triggers": ["歌伊"],
  "owner_ids": ["你的QQ号"]
}
```

多渠道 + 故障转移：

```json
{
  "providers": [
    { "name": "primary", "endpoint": "https://api.example.com/v1/chat/completions",
      "api_key": "sk-你的Key", "model": "gemini-3.8-flash", "protocol": "openai",
      "priority": 1, "timeout": 60, "kinds": ["chat", "memory_curate"],
      "extra_body": { "reasoning_effort": "low" } },
    { "name": "backup", "endpoint": "https://api.example.com/v1/chat/completions",
      "api_key": "sk-你的Key", "model": "deepseek-flash", "protocol": "openai",
      "priority": 2, "timeout": 60, "kinds": ["chat", "memory_curate"] },
    { "name": "opt-expensive", "endpoint": "https://api.example.com/v1/chat/completions",
      "api_key": "sk-你的Key", "model": "glm-5", "protocol": "openai",
      "priority": 3, "manual_only": true }
  ],
  "owner_ids": ["你的QQ号"]
}
```

`priority` 越小越优先；`manual_only: true` 表示只能被 `切换渠道` 选中，永不自动启用。

### 3. 环境变量（可选，避免明文 Key）

| 变量 | 作用 |
|---|---|
| `GEYI_API_KEY` | 单渠道模式的通用 Key |
| `GEYI_KEY_<渠道名大写>` | 指定渠道的 Key，如 `GEYI_KEY_PRIMARY` |
| `GEYI_OWNER_IDS` | 骰主 QQ 号，逗号分隔 |

> ⚠️ `owner_ids` 必须配（或设 `GEYI_OWNER_IDS`），否则「所有渠道都挂了」「自我保护静默」这类通知发不出去——启动日志里会明确警告。

---

## 常用指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `歌伊菜单` | 全部 | 功能菜单 |
| `歌伊状态` | 全部 | 运行状态、当前渠道、记忆概况、静默剩余时间 |
| `渠道状态` | 骰主 | 渠道健康度、手动锚点、最近切换记录 |
| `切换渠道 <名称/序号>` | 骰主 | 手动切换并**设为锚点** |
| `切换渠道 自动` | 骰主 | 取消锚点，恢复按优先级自动恢复 |
| `重载设定` | 骰主 | 重新读取配置与角色提示词 |
| `记忆状态` / `导出记忆` | 骰主 | 查看/导出已提炼的记忆 |
| `解除静默` | 骰主 | 提前结束本会话的自我保护静默 |
| `测试通知` | 骰主 | 自查骰主私聊通道是否通 |
| `语音状态` / `测试语音 [情绪] [日文]` | 骰主 | 伴生语音状态与试听 |
| `开启语音` / `关闭语音` / `语音重载` | 骰主 | 语音开关与重载 |
| `主动搭话开/关`、`本群主动开/关` | 骰主 / 群管 | 主动搭话开关 |
| `表情包状态`、`开启/关闭表情包` | 骰主 | 表情包开关 |
| `图片理解开/关` | 骰主 | 多模态识图开关 |

---

## 伴生语音网关

`voice/` 只负责**调用**，TTS 本体由你自备（推荐本机跑 GPT-SoVITS / Genie-TTS 之类的服务）。接口约定：

```http
POST /v1/voice/synthesize      # Bearer <voice_auth_token>
{ "text": "日文台词", "emotion": "撒娇", "speed": 1.0 }
→ 200 { "audio_base64": "..." }   # wav，任意采样率（插件会重采样到 24000Hz）

GET  /health                   # 存活探测
GET  /v1/voice/stats           # 可选：控制台统计
```

配置项：`voice_enabled`、`voice_gateway_url`、`voice_health_url`、`voice_auth_token`、`voice_timeout`、`voice_group_cooldown`、`voice_poke_rate`。

不发语音也能正常聊天——语音只是可选增强。网关掉线时插件会**熔断**：回复路径只读健康缓存（不做网络 IO），
探测退避 60s→300s，**只在日志里留一行**（不发通知——TTS 通常就跑在你自己的机器上，关机是常态）；
用户主动索要语音或骰主手动「测试语音」时仍会硬试一次。

---

## 数据持久化：动静分离

OlivOS 每次加载插件都会 `rmtree` + `copytree` 插件包里的 `data/`，所以必须分开存：

| 目录 | 内容 | 说明 |
|---|---|---|
| `plugin/data/newGeYi/` | `config.json`、`geyi.sqlite3`、导出的记忆 | 永久保留 |
| `plugin/data/newGeYi/data/` | `role_prompt.txt`、`worldbook.json`、`emotes.json`、`emote/` | 由 OlivOS 释放；加载后插件会自愈补齐（只补不删，包内文件更新时才覆盖） |
| `plugin/app/newGeYi/data/` | 同上，**源头** | 想改提示词/世界书就改这里，重载后自动同步 |

> v2.6.1 起：若 OlivOS 的 `release_data` 失败（部分版本会报 `WinError 183`，`data/` 被清空却不回填），
> 插件会在 `init_after` 里从 `plugin/app/newGeYi/data/` 补齐缺失或过期的文件，
> 启动日志会打印「资源目录自愈：从插件包补齐 N 个文件」。

---

## 安全与开源规范

- 仓库内 **不含**任何真实 API Key、QQ 号、群号、聊天记录或数据库文件；
- `.gitignore` 已排除 `config.json`、`*.sqlite3`、`*.bak`、`导出/` 等运行时产物；
- `data/role_prompt.txt` 里的主人 QQ 号是占位符 `123456789`，部署后请改成你自己的账号，并把 `owner_ids` 一起配上；
- 插件里所有"绝对不能发生"的事（泄露本机路径、把思考过程发进群）都有代码层兜底，不只靠提示词。

---

## 更新日志

### v2.6.2 (2026-10-30)

外部代码审计反馈的一批隐患，逐条核实后修复：

- **配置浅合并** → 递归深合并。写半个 `failover` 不再抹掉其余 7 个键（以前 `fail_threshold`
  会静默退回 2，等于"失败一次就换备用"变成"失败两次"）。
- **27 处兜底值与默认值打架** → 全部统一（`active_base_cooldown` 曾差 10 倍、
  `active_daily_cap` 差 7.5 倍、`group_quota_enabled` 语义相反），并加**静态测试**锁死。
- **6 个孤儿配置键**（`assistant_name`、`extra_headers`、`memory_inject_*`、
  `memory_curate_max_tokens_cap`）补进默认值与示例 —— 以前它们被代码读、却查不到也配不到。
- **两个暗键**：`auto_memory_enabled`（零消费）改为迁移兼容，`active_stop_strikes`（零消费）删除。
- **图片预算**：单张 4 MB / 单轮 7 MB，明确"首张超总量也保留"的策略并留日志。
- **audioop 兜底**：Python 3.13 移除该模块后，纯 Python 重采样顶上 + 打一次 warning
  （以前会静默丢 24kHz 重采样与静音裁剪，表现为"语音没声"）。
- **跨插件钩子**：幂等安装 + `save()` 还原 + 异常升到 warning。
- **戳一戳回复入账**：她开始记得自己戳回去说过什么（仍不进长期记忆）。
- 清理三处死代码；新增 `tests/test_config.py`（10 条离线断言）。

跑测试：

```bash
cd plugin/app
python -m unittest discover -s newGeYi/tests -t . -v
```

### v2.6.3 (2026-10-30)

- **语音降级不再发通知**：TTS 一般跑在部署者自己的机器上，晚上关机是常态 ——
  每次掉线都私聊「语音降级」等于每天半夜收一条已知的噪声。现在只在日志里记一行状态跃变，
  详情用 `语音状态` 指令查。熔断、退避、主动索要时硬试一次等行为都保留。

### v2.6.1 (2026-10-30)

- **语音熔断**：`can_send_voice()` 不再同步探测健康（以前缓存一过期就发一次 2 秒超时的 GET，
  主力机一关机就每隔 45 秒卡一条回复）；失败后退避窗口 60s→120s→240s→300s 封顶；
  `语音状态` 显示熔断次数/窗口/剩余重探时间。
  （v2.6.3 起**取消了私聊通知**，见上。）
- **主动索要绕过熔断**：`synthesize(force=True)`——用户明确要语音、或骰主「测试语音」时硬试一次。
- **资源目录自愈**：修复部分 OlivOS 版本 `release_data` 失败（`WinError 183`）导致
  `plugin/data/<ns>/data/` 被清空却不回填的问题（详见下方「数据持久化」）。
- **版本号对齐**：`config.PLUGIN_VERSION` 与 `app.json` 统一为 2.6.1，启动时不一致会打 warning。

### v2.6.0 (2026-10-29)

- 日语原声伴生语音（15 个情绪卡槽、重采样到 24kHz、首部静音裁剪）；
- 自我保护静默 `[REST:原因]`（静默当前会话并通知骰主）；
- 图片带发送者归属，回溯他人旧图不再认错人；
- 渠道手动锚点 + 逐级爬回 + 低优先渠道体检；
- 剥离英文推理前言、去重复行、思考前言硬拦截。

---

## License

本项目遵循 [MIT License](LICENSE)。
