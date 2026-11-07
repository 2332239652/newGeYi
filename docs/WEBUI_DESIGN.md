# 歌伊 · 调试台

核心交付：[单文件页面](../webui/index.html)。不引入前端框架、CDN、网络客户端或浏览器存储。

## 视觉决策

- 以用户提供的蓝发绿眼角色原图为依据，统一名称「歌伊 · 调试台」，不添加新的角色世界观名称。
- 薄荷灰白 `#f1f6f5` 为背景，白 `#ffffff` 为工作面板与表单；正文深灰蓝 `#293f46`，操作灰蓝 `#527a8a`。
- 绿眼色 `#397563` 表示连接与选中；少量金色 `#c9ad67` 细线呼应原图法杖，暖棕 `#a46b5e` 用于待确认与异常，不做满屏背景纹样。
- 中文系统 sans 统一字标、标题和正文；系统等宽字体用于键、路径、JSON、日志。移除书法章印式标题。
- 侧栏使用角色小头像；总览顶部为约 230px 白色人物状态区，左侧清晰说明页面职责与真实运行状态，右侧约 200px 上身角色图；移动端人物缩小。编辑页面不铺角色图。
- 「聊天 → 闸门 → 上下文 → 回复」观察流水降为下方功能结构，最近一次只读检查继续更新流水内容，不做仪表盘大数字。
- 桌面：左导航 + header + 工作面板 + 固定底栏。760px 以下导航横滑；430px 以下表单单列。
- 轻量 Figma 布局稿：[歌伊 · 调试台](https://www.figma.com/design/oFvzq5ds2dfcRLBdYbcWDr)。已按资源技能加载 create-new-file / figma-use / generate-design、whoami、检索组件与样式；创建成功，无权限错误。稿件为可编辑文字和结构，不是整页截图。

## 原始素材与嵌入

使用用户提供的歌伊原图，派生头像和总览人物 WebP，仅裁切/缩放压缩，未调用生图。最终图片以 `data:image/webp;base64` 内联在 HTML，保持单文件、opaque-origin sandbox 与 CSP 兼容；页面没有本机素材路径。派生资源保留在 `webui/assets/`，用于后续替换，不作为页面请求依赖。

## 六个页面

| 页面 | 首次按需读取 | 主要控制 ID |
|---|---|---|
| 总览 | `overview` | `overview-to-debug`, `runtime-status-json`, `providers-json` |
| 配置 | `config_get` | `config-save`, `config-discard`, `config-json`, `config-json-apply`, `config-reload` |
| 角色设定 | `prompt_get` | `prompt-text`, `prompt-save`, `prompt-discard` |
| 会话与记忆 | `sessions`，选中后 `session_get` | `sessions-list`, `sessions-prev`, `sessions-next`, `session-to-debug` |
| 调试台 | 不自动请求；点击后 `debug_preview` | `debug-session`, `debug-text`, `debug-preview`, `debug-pick-session`, `debug-effects` |
| 运行日志 | `logs` | `logs-list`, `logs-level`, `logs-follow`, `logs-clear` |

导航 ID 为 `tab-{overview,config,prompt,sessions,debug,logs}`；页容器为 `page-*`。公共刷新为 `refresh-current`。确认对话框为 `confirm-dialog` / `confirm-cancel` / `confirm-accept`。

## 消息桥与数据纪律

请求通过 `window.parent.postMessage`：

```js
{ type: 'olivos:plugin_event', event: 'newGeYi_WebUI', request_id, payload: { action, ...params } }
```

只接受父窗口发来的 `olivos:plugin_reply`，并核对非空 pending 请求 ID；同时处理宿主顶层 `error` 与业务 `{ok:false,error,code}`。多 pending 使用 Map，10 秒超时；写请求不自动重试。提交超时后锁住相关写按钮，需手动刷新核对宿主值。

- 配置保存：仅白名单公开差异 `patch + revision + confirm:true`。重载同样发送 `revision + confirm:true`。
- 高级 JSON 从 `editable_keys` 投影，额外排除 `redacted_keys` / `redacted_editable_keys`、凭据键、providers、URL 和请求头。每次应用先验证类型，再写到草稿，保存仍需确认。
- providers 只读，展示 `healthy/configured/current/hold` 与脱敏 JSON。
- 保存回包的 `warnings` 会显式展示，避免把部分组件应用失败写成完全成功。
- 角色提示词保存到宿主状态目录的持久覆盖，重载后保留，原始包文件保持原样。带脱敏标记的底稿锁住保存，避免把脱敏视图覆回原文。
- 会话 history / memories / state / usage 全只读，无删除、清空或改写宿主记忆操作。
- 调试默认只读，严格只发 `debug_preview`。展示 branch/reason/score/threshold、本地提示序列、summary 与 effects；没有模型调用或消息发送入口。
- 调试空会话键使用 `preview:qq:onebot:group:-:preview`，私聊用 `private`；选择已有会话则带入宿主完整六段键。空发言者 ID 默认 `preview`。正文上限 10000 字，ID/称呼 80 字。
- 主动影子模式和本页预览不同：影子模式仍可能调用模型并消耗 API token，仅停止外发 QQ。配置旁明确说明。
- 每 5 秒只刷新 overview；日志页当前可见且勾选跟随时额外刷新 logs。官方 `olivos:plugin_visibility` 和 `document.hidden` 任一隐藏均暂停轮询。
- 配置和角色页不轮询，手动刷新遇到草稿先确认；后台状态刷新不覆盖用户输入。
- 回包由 `createElement/textContent/replaceChildren` 渲染，无 HTML 注入。日志最多保留 500 条；dropped 游标空窗会提示。清空日志仅清当前页面视图。

## 演示与离线

直接打开页面，无父宿主时显示离线说明，不向网络发请求。地址末尾加 `?demo=1` 可体验内置示例：渠道状态、公开配置、角色文本、三条会话、三层记忆、预览和日志。演示横幅始终醒目，全部写按钮关闭；可修改草稿体验编辑，但不保存。

## 验证与可访问性

- Node `vm.Script` 对唯一 inline JS 完整语法检查；同时查重复 DOM ID、静态 `$()` 引用与禁止 API。
- 全页按钮类型明确；表单 labels、原生有效性校验、aria-live 状态、loading/error/empty 与 disabled 均有处理。
- 主导航和会话详情采用 tab/tabpanel；方向键、Home/End 切换，tabIndex 跟随；跳到主面板链接、键盘焦点样式和减少动画偏好均支持。
- 写确认使用原生 dialog，初始焦点在取消；Escape 取消，关闭后恢复触发控件焦点。
- 实际 iframe sandbox、桌面/窄屏截图与 mock bridge 交互由父代理统一验证；前端代理不启动额外服务、不触碰生产 state 或密钥。
