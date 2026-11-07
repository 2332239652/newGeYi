# newGeYi 原生 WebUI 契约

页面注册 `webui/index.html`，只使用 OlivOS iframe 消息桥，不增加服务、端口、依赖或直接 fetch。

```js
window.parent.postMessage({type:'olivos:plugin_event', event:'newGeYi_WebUI',
  request_id:'唯一字符串', payload:{action:'overview'}}, '*');
```

宿主回包 `olivos:plugin_reply` 的 `payload` 为 `{ok:true,data:{...}}` 或 `{ok:false,error:string,code:string}`。Python 入口为 `Panel(runtime).handle_event(event,Proc)`，退出调用 `.close()`。校验命名空间、WebUI context、事件名、1–128 字符请求 ID 和对象 payload。普通菜单不处理，WebUI 没有机器人事件上下文；原事件 `send('webui',request_id,response)` 回包。

| action | 参数 | data |
|---|---|---|
| overview | 无 | version, uptime_seconds, bots:[{hash,id,platform,sdk}], paths:{config,state,database,role_prompt}, flags, providers, status |
| config_get | 无 | config（脱敏）, revision, editable_keys, redacted_editable_keys, secret_fields |
| config_save | patch, revision, confirm:true | 同 config_get；部分组件应用失败可有 warnings |
| config_reload | revision, confirm:true | 同 config_get；读盘错误保留原内存配置，返回 config_load_error |
| prompt_get | 无 | text, revision, path |
| prompt_save | text, revision, confirm:true | 同 prompt_get |
| sessions | limit:1–100（默认50）, offset:0–100000 | items:[{session_key,target_id,session_type,turn_count,last_ts}], total, limit, offset, scope:'persisted_sessions' |
| session_get | session_key, limit:1–100（默认50） | session_key, turns:[{role,content,sender_id,sender_name,created_at}], memories:[{id,layer,content,...}], state, usage |
| debug_preview | session_key,text（1–10000字）,sender_id?,sender_name?,is_group?,mentioned_bot?,is_active? | route, messages:[{role,content}], summary, effects:{network:false,send:false,persist:false} |
| logs | since:非负整数（默认0）,limit:1–200（默认100） | items:[{id,ts,level,message}], next_since, dropped |

- SessionKey 为 `bot_hash:platform:sdk:group或private:host_id:target_id`，空段为 `-`；ID 均为字符串。sessions 仅枚举 SQLite 已留存的会话，不将瞬时 live cache 冒充持久记录；列表没有正文。session_get 在用户选定会话后显式加载正文（含私聊和记忆），页面首页不自动加载。
- `state` 含 passive_enabled / active_enabled / rest_remaining_seconds / memory_pending / quota。
  **`rest_remaining_seconds == -1` 表示无限期静默**（v2.8.11 起私聊的 `[REST]` 退场不再自动恢复，
  只认骰主「解除静默」）；`0` 表示不在静默中，正数才是剩余秒数。页面不直接渲染这个字段，
  外部调用方必须按三种取值处理。
- 渠道状态为脱敏列表：name/model/priority/protocol/kinds/manual_only/configured/healthy/fail_count/last_error/last_ok_at/last_fail_at/last_probe_at/current/anchor/hold；status 另含 current/anchor/hold/history/queue/usage/voice。只看缓存，不探测渠道或 TTS。
- editable_keys 是显式允许列表，布尔严格、数字 finite/range、列表元素严格字符串且有容量上限、嵌套权重字段白名单。providers/endpoint/key/header 均不开放编辑；未知或秘密字段返回 unknown_key。若原值因凭据或 URL 查询被脱敏，该字段从 editable_keys 移入 redacted_editable_keys，写入返回 sensitive_config_field，防止把脱敏视图误存。只 patch 既有配置，保留全部未改字段与秘密，不允许整份覆盖。数值范围集中在 panel_helpers.py。
- 密钥字段从 config 回包剔除；真实配置/环境渠道 key、URL credentials/query 与文本中命中的凭据统一脱敏，不回传头尾。secret_fields 是字段名称，不是值。密码不进入页面存储、日志或静态资源目录。
- config revision 包含磁盘内容与当前内存内容；保存时匹配后调用既有原子保存/滚动备份，再原地更新共享 dict。重载磁盘文件若已编辑，应先 config_get 取得最新 revision。写 action 只按浏览器 session+request ID 幂等（最多256条）；同 ID 不同 payload 返回 request_conflict。写超时不要换 ID 自动重试。
- 角色保存唯一目标为 state/role_prompt.webui.txt，UTF-8、滚动备份、原子替换，然后更新内存提示词。既有包内 role_prompt.txt 保留；宿主运行时已优先读取该持久覆盖。包含被脱敏凭据的底稿返回 sensitive_prompt，避免把脱敏视图误保存为原文。
- debug 是**文本样本的简化只读预览**：route={branch,reason,directed,passive_allowed,score,threshold,contributions,model_evaluated:false,preview_mode:'simplified_read_only'}。summary 包含 message_count/history_count/chars/omitted/note；没有 token 估算。复用纯算术评分与文本 PromptBuilder，使用独立历史副本和不 touch 的 MemoryRepository；省略图片下载、video/web/voice、完整主动闸门和模型。既有 active_shadow 不是 dry-run，预览不会调用真实 route/evaluate/chat/submit/reply。
- 日志为800条内存 ring、每条最多2000字符、无堆栈正文。业务模块保留固定格式模板，参数和自由正文默认省略；保留来源/级别/异常类型。隐藏页面暂停轮询；next_since 游标继续读取，dropped 标明旧游标已被覆盖。
- 默认没有渠道切换/健康测试/语音发送/通知/导出/记忆编辑或清理/后台任务启停接口。所有已提供写操作需 confirm:true，按钮显式确认。单请求业务 payload 256 KiB、回包约900 KiB；较大查询返回 response_too_large 而非无限输出。

测试：导入前设 `GEYI_DATA_DIR` 为临时目录，再从 plugin/app 执行 `python -m unittest newGeYi.tests.test_panel -v`。测试包含无 bot 回包、普通菜单隔离、密钥与环境 key 脱敏、确认/版本冲突/幂等、配置失败回滚、角色持久覆盖、分页与零网络/发送/落库预览。

## v2.9.0 新增：私聊好感度（galgame）

页面新增「好感度」标签页（只在核心提供 WebUI 或本地面板里可见）。新增 action：

| action | 参数 | data |
|---|---|---|
| relationship_list | limit:1–100（默认50）, offset | items:[关系快照], total, stage_counts, enabled, shadow, stats_24h, stats_7d（评审条数/平均 delta/评审失败/被代码改判/标签分布） |
| relationship_get | user_id:数字字符串, limit:1–100（默认30） | 关系快照 + log:[{id,ts,delta_raw,delta_applied,tag,level,aff_after,stage_after,shadow,note,user_text,reply_text}] |
| relationship_set_stage | user_id, stage:0–5, confirm:true | 关系快照（分数与有效日对齐到该阶段，已过的羁绊事件视为完成） |
| relationship_reset | user_id, confirm:true | 关系快照（流水保留） |

- 关系快照字段：user_id, stage, stage_name, affection, next_threshold, days_active, next_min_days, gain_today, daily_cap, bad_streak, locked_until, pending_event, events_done, mood, impression_dirty, exempt, updated_at。
- 两个写 action 同样按浏览器 session+request ID 幂等；`session_get` 对私聊会话多返回一个 `relationship` 快照（群聊为 null）；`overview` 多返回 `galgame:{enabled,shadow,worldbook}`。
- 配置白名单新增：`galgame_enabled / galgame_shadow / galgame_owner_exempt / galgame_judge_min_chars / galgame_cross_memory_min_stage`（布尔、数字），`galgame_judge_model / affection_provider`（文本），`galgame_exempt_ids`（数字字符串列表），`galgame_thresholds / galgame_min_days / galgame_daily_cap`（长度必须为 6 的数组；阈值须单调不减）。
- 流水里的 `user_text / reply_text` 各截取前 200 字，与会话页的历史同属私人内容，面板仅限骰主使用。
