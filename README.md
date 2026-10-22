# newGeYi (歌伊ai）

[![OlivOS Plugin](https://img.shields.io/badge/OlivOS-Plugin-blue.svg)](https://github.com/OlivOS-Team/OlivOS)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**newGeYi** 是一款专为 [OlivOS](https://github.com/OlivOS-Team/OlivOS) 框架深度定制的模块化角色扮演（RP）AI 群聊插件。

区别于简单的“单轮调用 / 提示词包装”，本项目基于长期的群聊实战调优，内置了**多渠道故障转移**、**四层自动记忆提炼**、**多模态图片理解**、**数据驱动表情包**、**输出安全审计硬拦截**与**自适应主动搭话**机制，打造自然、灵动且具备长期群友认知的拟人陪伴体验。

---

## 核心特性

- 🔄 **多渠道智能路由与故障转移**：
  - 支持 OpenAI / Anthropic 等多协议规范；
  - 渠道支持按优先级故障转移（Failover），掉线自动探测并自动切回；
  - 支持聊天（Chat）与记忆提炼（Memory Curate）按模型类型分流，兼顾速度与成本效益。
- 🧠 **四层自动记忆系统**：
  - 自动从群聊对话中异步提炼：**群组全局认知 / 群友专属事实 / 角色自我记忆**；
  - 引入半衰期衰减与相关性打分机制，按需精准注入上下文，彻底告别单轮遗忘与长上下文臃肿。
- 🛡️ **严格的回复安全审计（代码层硬拦截）**：
  - 自动识别并拦截模型可能吐出的**本地盘符路径**（`C:\...`、`/home/...`）、**密钥凭据**（`sk-...`、`api_key`）、厂商安全过滤报错等；
  - 拦截命中时整条静默丢弃，杜绝向公开群聊泄露服务器内部信息。
- 🖼️ **多模态图片理解与立绘出站感知**：
  - 群友发图智能理解，设置图片相关性闸门，防止“见图就评”的机械感；
  - 自身发出的表情立绘自动进入上下文队列，群友追问立绘表情时可准确识别。
- 🎭 **数据驱动表情包系统**：
  - 语义化 `[EMOTE:名称]` 机制，由配置文件声明名称、文件映射与适用时机；
  - 代码层强制节流（单条上限、发图冷却、每小时频次硬顶），杜绝刷图。
- 💬 **真人群聊体验细节打磨**：
  - **短句自动折叠**：自动将分行短句合并，避免连发刷屏；
  - **话未说完防抢答**：检测到“然后、但是、你知道”等尾缀时延迟按住，给人类打字留出时间；
  - **AI 味黑名单**：彻底杜绝“希望这个回答对您有帮助”、“作为一个人工智能”等客服腔。

---

## 目录结构

```text
newGeYi/
├── main.py                 # OlivOS 事件入口与消息转发
├── config.py               # 默认配置、环境变量、动态路径解析
├── models.py               # 数据结构与类型定义
├── config.example.json     # 标准配置文件示例
├── runtime/                # 运行时调度器、会话状态与限流
├── routing/                # 消息路由、指令解析与权限控制
├── context/                # Prompt 构建器、多模态解析、实时会话窗口
├── providers/              # LLM 网关（OpenAI / Anthropic 协议与故障切换）
├── memory/                 # 记忆提炼器、SQLite 向量/事实检索、世界书
├── storage/                # SQLite 持久化存储与安全备份
├── presentation/           # 输出渲染器、敏感词/路径审计、表情包组装
└── data/                   # 默认静态资源（角色提示词、世界书、表情包表）
```

---

## 快速上手

### 1. 安装插件
将本仓库克隆或解压至 OlivOS 的 `plugin/app/` 目录下，确保目录结构为：
```text
OlivOS/plugin/app/newGeYi/
```

### 2. 配置模型服务

#### 方式 A：配置文件（推荐）
在 OlivOS 的 `plugin/data/newGeYi/config.json`（首次启动会自动生成，亦可参考 `config.example.json`）中配置：

```json
{
  "api_endpoint": "https://api.openai.com/v1/chat/completions",
  "api_key": "sk-your-openai-api-key",
  "fast_model": "gpt-4o-mini",
  "providers": [
    {
      "name": "openai-primary",
      "endpoint": "https://api.openai.com/v1/chat/completions",
      "api_key": "sk-your-openai-api-key",
      "model": "gpt-4o-mini",
      "protocol": "openai",
      "priority": 1,
      "kinds": ["chat", "memory_curate"]
    },
    {
      "name": "openai-backup",
      "endpoint": "https://api.openai.com/v1/chat/completions",
      "api_key": "sk-your-openai-api-key",
      "model": "gpt-4o",
      "protocol": "openai",
      "priority": 2,
      "kinds": ["chat"]
    }
  ],
  "triggers": ["歌伊"]
}
```

#### 方式 B：环境变量
可通过环境变量直接传入 API Key，避免配置文件明文暴露：
* `GEYI_API_KEY`：全局通用 API Key
* `GEYI_KEY_<渠道名大写>`：针对特定渠道配置（例如 `GEYI_KEY_OPENAI_PRIMARY`）
* `GEYI_OWNER_IDS`：设置管理员 QQ 号（英文逗号分隔，如 `10000001,10000002`）

#### 方式 C：聊天指令
机器人启动上线后，Master/骰主私聊机器人发送：
```text
设置key sk-你的APIKey
```

---

## 常用指令

在群聊中 @机器人 或私聊机器人发送：

| 指令 | 权限 | 说明 |
|---|---|---|
| `歌伊菜单` / `歌伊帮助` | 全部 | 查看功能与交互菜单 |
| `歌伊状态` | 全部 | 查看运行状态、Token 消耗与当前渠道 |
| `渠道状态` / `切换渠道` | Master / Admin | 查看多渠道运行健康度，或手动切换渠道 |
| `重载设定` | Master | 重新加载配置与角色提示词 |
| `记忆状态` / `导出记忆` | Master | 查看已提炼的记忆条目，或导出为 SQLite 快照 |
| `主动搭话开` / `主动搭话关` | Master | 全局控制是否开启主动参与群聊 |
| `本群主动开` / `本群主动关` | 群管 / Master | 控制当前群的主动搭话开关 |
| `开启表情包` / `关闭表情包` | Master | 表情包功能全局开关 |
| `图片理解开` / `图片理解关` | Master | 多模态识图功能开关 |

---

## 数据持久化与目录说明

OlivOS 的插件机制在每次启动时都会对 `plugin/app/<插件名>/data/` 进行同步。为防止用户运行产生的数据被意外覆盖，本项目严格实行**动静分离**：

| 目录位置 | 存放内容 | 说明 |
|---|---|---|
| `plugin/data/newGeYi/` | **动态可变状态**：`config.json`、`geyi.sqlite3`、导出的记忆备份等 | 永久保留，重启不丢失 |
| `plugin/app/newGeYi/data/` | **只读基础资源**：`role_prompt.txt`、`worldbook.json`、`emotes.json`、`emote/` | 随插件源码分发，用户自定义提示词请在此修改 |

---

## 安全与开源规范

- 严禁在公开提交中附带真实的 `api_key` 或群聊数据库文件；
- 本仓库已内置 `.gitignore`，自动忽略运行时生成的本地数据库、日志及私有密钥；
- 开源版本中的 `role_prompt.txt` 中的主人 QQ 号默认设置为示例占位符，部署后请按需修改为您自己的数字账号。

---

## License

本项目遵循 [MIT License](LICENSE) 开源协议。
