# newGeYi（歌伊）可视化调试台 · 服务器端 Agent 安装说明

本压缩包为 **OlivOS 插件 `newGeYi`（v2.8.10 开发，已安装到 v2.8.11 基线上并升到 v2.8.12）原生 WebUI 可视化调试台部署包**。  
供服务器端 Agent 直接执行增量覆盖升级或完整目录部署。

---

## 一、核心原则（必读，严禁违反）

1. **绝不覆盖服务器生产状态目录**：
   - 服务器状态目录位于 `<OlivOS根目录>/plugin/data/newGeYi/`（包含线上 `config.json` API 密钥、渠道配置、`geyi.sqlite3` 长期记忆数据库、`role_prompt.webui.txt` 持久角色覆盖等）。
   - **严禁删除、清空或用模板覆盖 `<OlivOS根目录>/plugin/data/newGeYi/` 下的 `config.json` 与 `geyi.sqlite3`。**
   - 本压缩包已刻意剔除本地 `config.json`，仅包含 `config.example.json` 与插件代码/静态资源。
2. **保持 `app.json` 为 UTF-8 无 BOM**：
   - OlivOS 解析 `app.json` 时若遇到 `EF BB BF`（UTF-8 BOM）会直接导致插件加载失败。
   - 任何时候不要用 Windows PowerShell 5.1 的 `Set-Content -Encoding UTF8` 重写 `app.json`。
3. **零新增依赖、零额外端口**：
   - 本调试台采用 OlivOS 官方原生 `webui_config`（iframe + `window.parent.postMessage` 消息桥），复用 `Event.menu` 事件。
   - **不需要**安装任何新的 pip 包、不需要 Node.js/npm 构建，也**不需要**在服务器开放新的防火墙端口。

---

## 二、本次新增与修改的文件清单

相对于原版 `newGeYi`，本次变更仅涉及以下文件（其余业务模块原封不动）。安装到 v2.8.11 时，除这些文件外只额外并入 `runtime/app.py` 的角色设定覆盖与 `panel_helpers.py` 的新配置键（见文末「v2.8.11 适配」）：

| 路径（相对于 `plugin/app/newGeYi/`） | 操作类型 | 说明 |
| --- | --- | --- |
| `app.json` | 修改 | 插件名改为 `歌伊`（去除“歌伊圣堂”），新增 `webui_config` 注册 `webui/index.html` |
| `main.py` | 修改 | `Event.init` 惰性挂载 `Panel`；`Event.menu` 识别 `webui` 上下文并分流至 `Panel.handle_event`；`Event.save` 安全关闭日志监听 |
| `runtime/app.py` | 修改 | `load_role_prompt()` 优先读取 `plugin/data/newGeYi/role_prompt.webui.txt`（使网页修改角色设定在插件重载后持久保留） |
| `panel.py` | **新增** | 原生 WebUI 后端控制器（10 个白名单 Action、乐观锁 Revision、写操作幂等缓存、零副作用只读调试预览、800 条环形脱敏日志） |
| `panel_helpers.py` | **新增** | 配置字段白名单严格校验器、API Key / Bearer Token / URL 凭据递归脱敏器 |
| `webui/index.html` | **新增** | 单文件可视化调试台（内联 CSS/JS，已内嵌 Base64 歌伊水彩立绘与 Q 版比心导航头像，零外部请求） |
| `webui/assets/geyi-avatar.webp` | **新增** | 侧栏 Q 版比心导航头像（192×192 WebP，已剥离元数据） |
| `webui/assets/geyi-portrait.webp` | **新增** | 总览页水彩立绘（630×864 透明底 WebP，已剥离元数据） |
| `tools/run_webui_tests.py` | **新增** | 强制临时目录隔离的全量回归测试运行器（234 项测试） |
| `tools/preview_webui.py` | **新增** | 仅本机开发验收用的 127.0.0.1 隔离沙箱预览器（生产环境不调用） |
| `tools/build_webui_assets.py` | **新增** | 离线素材构建与 Base64 同步脚本（生产环境不调用） |
| `tests/test_panel*.py`、`tests/test_webui_assets.py` | **新增** | 42 项新增单元与契约测试 |
| `docs/WEBUI*.md`、`README.md` | 新增/更新 | 部署文档、API 契约、设计说明与验证报告 |

---

## 三、服务器端 Agent 标准安装步骤

假设服务器端 OlivOS 插件源码目录为 `<OLIVOS_ROOT>/plugin/app/newGeYi`：

### 步骤 1：备份线上现有插件代码（安全回滚点）

在覆盖前，将线上当前的 `plugin/app/newGeYi` 核心文件备份到 `<OLIVOS_ROOT>/plugin/_backups/newGeYi-pre-webui/`：
- 至少备份：`app.json`、`main.py`、`runtime/app.py`（若线上也有 `plugin/app/newGeYi/config.json` 历史遗留文件，务必一并备份并确认 `<OLIVOS_ROOT>/plugin/data/newGeYi/config.json` 已存在）。

### 步骤 2：解压并覆盖到 `plugin/app/newGeYi`

将本压缩包内的 `newGeYi/` 目录内容覆盖到 `<OLIVOS_ROOT>/plugin/app/newGeYi/`：

- **Linux / macOS 示例**：
  ```bash
  unzip -o newGeYi_webui_deploy.zip -d /tmp/newGeYi_deploy
  cp -rf /tmp/newGeYi_deploy/newGeYi/* "<OLIVOS_ROOT>/plugin/app/newGeYi/"
  ```
- **Windows PowerShell 示例**：
  ```powershell
  Expand-Archive -LiteralPath .\newGeYi_webui_deploy.zip -DestinationPath $env:TEMP\newGeYi_deploy -Force
  Copy-Item -Path "$env:TEMP\newGeYi_deploy\newGeYi\*" -Destination "<OLIVOS_ROOT>\plugin\app\newGeYi" -Recurse -Force
  ```

### 步骤 3：安装后静态自检（必做）

1. **检查 `app.json` 无 BOM 且 JSON 合法**：
   ```bash
   python3 -c "from pathlib import Path; import json; b=Path('<OLIVOS_ROOT>/plugin/app/newGeYi/app.json').read_bytes(); assert not b.startswith(b'\xef\xbb\xbf'), 'BOM detected!'; d=json.loads(b); assert d['name']=='歌伊' and d['webui_config'][0]['path']=='webui/index.html'; print('app.json OK')"
   ```
2. **运行隔离回归测试（自动使用临时目录，绝不触碰线上数据库）**：
   ```bash
   python3 "<OLIVOS_ROOT>/plugin/app/newGeYi/tools/run_webui_tests.py"
   ```
   期望输出：`Ran 234 tests ... OK`。

### 步骤 4：重载插件并验证生效

1. 在 OlivOS 控制台或 WebUI 中点击 **「重载插件」**。
2. 查看 OlivOS 启动日志，确认出现：
   - `状态目录: .../plugin/data/newGeYi`
   - 无 `插件初始化失败` 或 `调试页面初始化失败` 报错。
3. 登录 **OlivOS WebUI → 左侧栏「插件页面」→ 点击「歌伊 / 歌伊调试台」**：
   - 顶栏右侧徽章应显示绿色 **「桥接已连接」**；
   - **总览页**应显示歌伊立绘、左上角 Q 版比心头像、当前在线 Bot 账号、渠道健康状态与今日 Token 用量；
   - **调试台**输入任意测试语句点击「运行只读预览」，应返回路由分支判断与拼装后的 Prompt 消息序列，且徽章显示 `无网络 · 无发送 · 无写入`。

---

## 四、常见问题与回滚方法

- **侧栏看不到「插件页面」或「歌伊调试台」**：
  请确认服务器端 OlivOS 核心版本支持插件原生 WebUI（`webui_config`），且修改 `app.json` 后已执行过「重载插件」。
- **页面提示「当前为独立页面 / 离线」**：
  不要直接双击打开 `index.html` 或裸访问静态路径，必须从 **OlivOS WebUI 左侧栏 → 插件页面** 点进，宿主才会注入 `postMessage` 通信桥。
- **如何快速回滚**：
  将步骤 1 备份的 `app.json`、`main.py`、`runtime/app.py` 覆盖回 `<OLIVOS_ROOT>/plugin/app/newGeYi/` 并重载插件即可恢复至安装前状态；`panel.py` 与 `webui/` 留置在目录中不会被旧入口调用。

---

## 五、v2.8.11 适配（安装时补的）

v2.8.10 → v2.8.11 之间插件本体有 4 处变化，安装时必须一并处理，否则面板会与新行为不一致：

1. **`app.json` 版本与品牌**：`version` 保持 `2.8.11`（不要退回 2.8.10），`name` 用 `歌伊`
   （本包的 `tests/test_webui_assets.py` 会校验，且校验 `docs/WEBUI*.md`、`README.md`、
   `webui/index.html` 不得出现旧品牌串）。
2. **`runtime/app.py::load_role_prompt()`**：v2.8.11 的 `_enter_rest()` 改过（私聊无限期静默），
   不能整文件覆盖 v2.8.10 版本；只并入"优先读 `state/role_prompt.webui.txt`"这一段。
3. **新配置键 `rest_private_forever`**：私聊退场是否无限期（v2.8.11 新增，默认 `true`）。
   已加入 `panel_helpers.BOOL_KEYS`，因此在网页「配置」页可见、可热更新。
4. **`RestGuard.remaining()` 现在可能返回 `-1`**：表示无限期静默（私聊）。
   `session_get.state.rest_remaining_seconds == -1` 就是这个含义，页面不直接渲染，
   API 调用方需按"无限期"处理。
---

## 六、核心版本对照（2026-11-04 实测补充）

「插件页面」这条入口需要 **OlivOS 1.0.x** 的核心：

| 现象 | 0.11.x | 1.0.x |
| --- | --- | --- |
| `webui_config` 注册的入口 | 不认识，侧栏无「插件页面」 | 生效（`title`/`type`/`path`） |
| 启动日志 `WebUI 已启动` | 没有 | 有，默认 `http://127.0.0.1:20480` |
| `conf/webui_auth.json`、`data/webui/static` | 不生成 | 生成 |
| `plugin/cache/webui/`（资源释放） | 不生成 | 生成 |

时间线：`0.11.81` 发布于 2025-12-28；`1.0.0` 发布于 2026-10-08；
插件页面文档（OlivOSDoc `docs/DevPlugin/WebUI.md`）的提交是 2026-09/10。

**核心太老时不用改插件**：v2.8.13 起自带 `panel_server.py`，只绑 `127.0.0.1`，
QQ 里发「面板地址」取 URL 就能用同一份页面（真运行态）。升级到 1.0.x 之后把
`panel_local_enabled` 设为 `false` 即可切回原生入口。