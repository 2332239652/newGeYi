# 歌伊调试台

为 `newGeYi` 增加 OlivOS 原生可视化管理页面。页面沿用宿主登录认证和消息桥，不启动第二个服务器，不需要 Node.js、npm 或新增 Python 运行依赖。

## 打开与部署

1. 保留服务器原有 `plugin/data/newGeYi/`，尤其是配置和 SQLite 数据库。更新插件代码即可，不用覆盖运行数据。
2. 本轮涉及 `app.json`、`main.py`、`runtime/app.py`，新增 `panel.py`、`panel_helpers.py`、`webui/index.html` 以及测试和本文档。部署时以本目录当前完整文件为准，保持 `namespace: newGeYi`。
3. 在 OlivOS 中执行「重载插件」。进入 **OlivOS 自身 WebUI → 插件页面 → 歌伊 / 歌伊调试台**。
4. 第一次打开应看到版本、在线机器人账号和实际状态路径；选择某个会话后可查看记录及调试预览。
5. 此页面与 DeepSeek Harness 的聊天 GUI 是两个应用；DSH 的端口不是 OlivOS 面板地址。服务器远程连接仍使用原先的 OlivOS WebUI 地址。

宿主需要支持 `webui_config` 和 `olivos:plugin_event` 消息桥。如果菜单中没有「插件页面」，先确认当前 OlivOS 核心具备这一功能。现有 QQ 对话与私聊菜单不依赖新页面，保留原先入口。

### 核心没有 WebUI 时：本地面板（v2.8.13）

**OlivOS 的「插件页面」是 1.0.x 才有的能力。** 0.11.x 的核心不认识 `webui_config`，
插件写得再对侧栏也不会出现入口。判断方法（2026-11-04 在一台 0.11.81 上实测）：

- 启动日志里没有 `WebUI 已启动`，也搜不到任何 `webui` 关键字；
- 没有 `conf/webui_auth.json`、`data/webui/static`；
- 没有 `plugin/cache/webui/`（宿主从未释放过页面资源）；
- `OlivOS.exe` 只监听一个旧端口，`/`、`/webui`、`/console` 都返回 404。

这时插件会自己起一个**只绑 `127.0.0.1`** 的极小服务，把同一份 `webui/index.html`
包在宿主壳里，桥接到**进程内同一个 `Panel`**（真配置、真数据库、真写入）。
取地址的方式：在 QQ 里发 **`面板地址`**（`歌伊面板` / `调试台` 等价），或用日志里的
`本地面板已启动：http://127.0.0.1:18765/p/<令牌>/`。

| 配置键 | 默认 | 说明 |
| --- | --- | --- |
| `panel_local_enabled` | `true` | 关掉本地面板（例如已升级到 1.0.x，走原生入口） |
| `panel_local_port` | `18765` | 端口；`0` 表示由系统挑一个可用端口。被占用时只打 warning，不影响机器人 |

安全边界：只监听回环；请求路径必须带每次启动随机生成的令牌，否则 404；
POST 必须带自定义头 `X-Geyi-Panel: 1`（跨站简单请求过不了预检）；`Origin` 只接受本机；
`Host` 只接受 `127.0.0.1` / `localhost`（挡 DNS rebinding）；静态资源做根目录包含检查。
它不是 `tools/preview_webui.py` 那种沙箱演示：这里是**真实运行态**，配置保存和角色保存都会落盘。

### 本机隔离验收

即使暂时没有连接 OlivOS，也可以先试实际页面与 Python 后端：

```powershell
python tools/preview_webui.py --port 0
```

在插件目录执行。终端会打印本机 URL。打开后显示黄色「隔离验收」条，使用三组虚构会话和临时配置，可以实际体验确认保存、角色编辑、上下文预览和记忆查看。工具只绑定 `127.0.0.1`，屏蔽模型请求、QQ 发送和后台任务启动，原有服务器数据独立保留。按 Ctrl+C 关闭；加 `--keep-data` 可保留本次临时诊断目录。

`python tools/preview_webui.py --self-test` 会短测所有已知 action、保存后回读和调试前后状态一致性，不启动长期服务。这个开发工具使用标准库 HTTP 服务，生产面板仍复用 OlivOS 宿主，不额外开服务。

### 角色素材与名称

插件显示名为「歌伊」，页面为「歌伊 · 调试台」。人物图与头像从用户提供的歌伊原素材派生，保留原画的蓝发、绿瞳和薄荷色；原素材不修改。图片压缩后内嵌在页面中，不请求本机素材路径或外部图片服务器，也不携带原图生成参数。来源与处理方法见 [素材记录](WEBUI_ASSETS.md)。

### 演示页面

单独用浏览器打开 `webui/index.html?demo=1` 可查看演示样本，页面会明确标记演示模式，保存操作禁用。演示内容不代表你的服务器状态。

直接打开文件或 `/plugin/newGeYi/webui/index.html` 不是正式入口：只有从宿主侧栏加载的 iframe 才有消息桥。未连接时页面会提示正确打开方式。

## 页面功能

| 页面 | 操作 |
| --- | --- |
| 总览 | 查看版本、运行时长、机器人账号、功能开关、模型渠道状态和实际配置路径 |
| 配置 | 修改常用开关、数值和允许编辑的 JSON 字段；保存后热更新；从磁盘重载 |
| 角色设定 | 查看当前设定、修改并确认保存；自动备份，支持运行时热更新 |
| 会话与记忆 | 分页选择群聊 / 私聊会话，查看近期消息、长期记忆、状态和用量 |
| 调试台 | 输入样本，查看只读的触发分析、评分与上下文预览 |
| 运行日志 | 查看本页面日志监听启动后的插件日志；支持刷新和按级别查看 |

### 配置保存行为

- 页面只发送改动字段 `patch`，不会把脱敏后的整份配置写回去。
- API Key、请求头和模型渠道配置不在页面的编辑白名单内。渠道通过现有配置文件或原有机器人指令维护。
- 保存使用版本指纹：别的页面或 QQ 指令先改变配置时，旧草稿保存会提示冲突。请先重新读取，再合并改动。
- 写入前需确认；同一次写请求的重复投递进行幂等处理。超时后不要直接假定保存失败，先刷新确认状态。
- 配置对象原地更新，保持已装配组件引用一致；磁盘 JSON 损坏时保留原文件和当前运行值，显示具体诊断。
- 原有 `save_config` 的滚动备份为 `config.json.bak-auto`，与实际配置位于同一状态目录。

### 角色设定持久化

页面保存到状态目录的 `role_prompt.webui.txt`，不覆盖包内的 `data/role_prompt.txt`。运行时优先读取该覆盖文件；这样重载插件或替换 OPK 后，编辑内容仍然保留。页面「实际路径」显示当前生效文件。

要恢复原先资源文件的管理方式，先留好副本，再在停机维护时移走 `role_prompt.webui.txt` 并重载设定。原包中的角色底稿仍在。

### 离线调试的边界

调试预览 **不调用模型、不发送 QQ、不写入聊天记录或记忆**。图片下载、联网搜索、语音合成、真实调度提交也不会执行。

线上 `PromptBuilder.build` 会下载图片并更新记忆命中，主动闸门也会调整欠账与统计。因此面板使用只读材料和纯计算做预览，并标明省略项。预览用于解释触发条件和检查提示拼装，不是对下一次实际随机闸门结果或模型回复的承诺。

生产记录与记忆只提供查看，面板不加入清空会话、删除记忆或批量发群消息。

## 消息协议

网页请求：

```javascript
window.parent.postMessage({
  type: 'olivos:plugin_event',
  event: 'newGeYi_WebUI',
  request_id: '每次唯一的非空 ID',
  payload: { action: 'overview' }
}, '*');
```

宿主调用已有 `Event.menu`，页面分支在机器人账号 / 主人 QQ 检查之前处理。WebUI 事件没有 `bot_info`，后端使用现有 `_runtime`，以原事件 `send('webui', request_id, response)` 回包。

业务响应为 `{ok: true, data: {...}}` 或 `{ok: false, error: '诊断', code: '类型'}`。页面按来源窗口、类型和待处理 request ID 匹配响应。

主要 action：`overview`、`config_get`、`config_save`、`config_reload`、`prompt_get`、`prompt_save`、`sessions`、`session_get`、`debug_preview`、`logs`。

页面不用 `fetch`、WebSocket、父页面存储或 iframe 的 `localStorage`，也没有外部 CDN。收到 `olivos:plugin_visibility` 后暂停隐藏页轮询，切回再恢复。表单草稿不由轮询覆盖。

## 测试

推荐直接执行独立测试入口（会先强制创建临时目录，再导入包）：

```powershell
python newGeYi/tools/run_webui_tests.py
```

或在 `plugin/app` 目录下，先隔离运行数据再执行：

```powershell
$env:GEYI_DATA_DIR = Join-Path $env:TEMP ('geyi_panel_test_' + [guid]::NewGuid().ToString('N'))
$env:PYTHONIOENCODING = 'utf-8'
python -m unittest discover -s newGeYi/tests -t .
```

不要在设置隔离目录之前导入 `newGeYi`：包的 `main.py` 会立即构造运行时并加载配置和数据库。

`app.json` 必须 UTF-8 无 BOM，开头应是 `7B` 而非 `EF BB BF`。静态检查、桥接回包测试与服务端在线验证是不同层级；上线后仍应实际确认页面总览、读取会话和保存后状态变化。

## 排查

- **侧栏没有入口**：核对 manifest 的 `webui_config`、入口是否随代码/OPK 部署、插件是否成功加载、宿主是否支持插件页面，随后重载。
- **401**：重新登录 OlivOS WebUI，再从侧栏进入。
- **404**：核对部署中的 `webui/index.html`。OPK 根目录应直接含 `app.json`，别再套一层 `newGeYi`。
- **请求超时**：确认从宿主打开、插件运行正常；写请求超时先重新读状态核实，不自动重试。
- **版本冲突**：页面草稿已过期，重新读取后合并修改再保存。
- **日志为空**：日志只记录 Panel 监听启用后的 `newGeYi` 日志，不回读服务器全部历史日志。
- **重载后角色变化**：以面板展示的实际生效路径为准；检查状态目录覆盖文件是否随服务器数据保留。

## 本轮回滚副本

改动前入口和版本配置已复制到 `plugin/_backups/newGeYi-webui-20261104-1710/`，另含 `runtime/app.py`。恢复时仅替换对应代码文件；保留运行数据。新增页面及 Panel 可留置，旧入口未注册时不会被调用。
