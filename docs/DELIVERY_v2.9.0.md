# 歌伊 newGeYi v2.9.0 交付文档

> 私聊 galgame（好感度 / 关系阶段）+ 世界书扩写 + 后台面板「好感度」页。
> 本文档 = 更新报告 + 验证结果 + **服务器端待办清单** + 观察与调参指南。
> 设计依据与取舍见 [GALGAME_PLAN.md](GALGAME_PLAN.md)；真模型验收原始报告见 [LIVE_CHECK_REPORT.md](LIVE_CHECK_REPORT.md)。

---

## 1. 一页纸概览

| 项 | 状态 |
|---|---|
| 好感度状态机（6 阶段、日上限、递减、天数/事件门槛、冷却/降级） | ✅ 完成，纯代码，45+ 条单测 |
| 评审（回复后异步小模型，超前即扣分由代码强制） | ✅ 完成，真模型 12/12 |
| 阶段合同注入 + 越界审计与重写 | ✅ 完成，真模型 24 条速攻 0 条越界 |
| 记忆随阶段开放（事实条数、互通记忆门、里程碑、印象重写） | ✅ 完成 |
| 世界书引擎（阶段门/场景/谈资/id）+ 内容 46 条 | ✅ 完成，真模型防泄露 0/4、解锁 5/5 |
| 羁绊事件（讲起哥哥 / 说出心意） | ✅ 完成（台词核心在 `relationship/stages.py`） |
| QQ 指令（关系 / 好感度开关 / 详情 / 设置 / 重置） | ✅ 完成 |
| 后台面板「好感度」页 + 配置白名单 | ✅ 完成（见 §4，**未在真实 OlivOS 宿主里点过**） |
| 难度模拟脚本 / 真模型验收脚本 | ✅ 完成（`tools/`） |
| **默认状态** | 全部**关闭**：`galgame_enabled=false`、`worldbook_enabled=false`；打开后 `galgame_shadow=true`（只记账不生效） |

---

## 2. 更新报告（v2.8.19 → v2.9.0）

### 2.1 新增文件
| 文件 | 作用 |
|---|---|
| `relationship/state.py` | 状态机：所有"难度"都在这里（纯代码，不碰数据库/模型） |
| `relationship/stages.py` | 阶段表（行为合同 + 正反例）、羁绊事件、里程碑记忆、越界词审计 |
| `relationship/judge.py` | 评审提示词与输出校验 |
| `relationship/service.py` | 持久化、prompt 注入、异步评审、记忆提炼联动、面板快照 |
| `tools/simulate_affection.py` | 难度模拟（各类玩家几天到哪一阶段） |
| `tools/live_galgame_check.py` | 真模型验收（密钥只读环境变量，不落盘） |
| `tests/test_relationship.py`、`tests/test_relationship_flow.py` | 单测 / 端到端 / 面板契约 |
| `docs/GALGAME_PLAN.md`、`docs/LIVE_CHECK_REPORT.md`、本文档 | 文档 |

### 2.2 修改文件
`config.py`（新配置 + 版本号）、`config.example.json`、`app.json`、`README.md`、`storage/database.py`（2 张新表）、
`memory/worldbook.py`、`memory/repository.py`、`memory/curator.py`、`context/prompt_builder.py`、
`runtime/app.py`、`routing/commands.py`、`providers/llm_gateway.py`（`affection` 渠道键）、
`panel.py`、`panel_helpers.py`、`webui/index.html`、`docs/WEBUI_API.md`、
`data/role_prompt.txt`（仅【性格】里"依恋"那一行加了括号说明）、`data/worldbook.json`（5 → 46 条）。

### 2.3 数据库
只做加法，首次启动自动建表（`CREATE TABLE IF NOT EXISTS`）：
- `user_relationship`：每个私聊用户一行（好感、阶段、有效日、冷却、心情、事件进度等）。
- `relationship_log`：只追加的评审流水（影子模式也写，`shadow=1`）。
不改动、不迁移任何旧表。**回滚**：关掉 `galgame_enabled` 即可，旧版代码不认这两张表但也不受影响。

### 2.4 行为变化清单（开启后）
1. 私聊 system 末尾多一个`【此刻你与对方的关系】`块，覆盖【性格】里的"粘人"。主人、群聊没有。
2. 私聊回复若出现与阶段不符的亲昵词，会**多一次**模型调用来重写（最多一次；她在拒绝对方称呼时重复该词不算）。
3. 每条有信息量的私聊用户消息，回复后**多一次**评审调用（异步，不增加回复延迟）。
4. 陌生/熟人阶段：事实只给 2/4 条且口气不确定；群↔私聊互通记忆在"朋友"之前不放行。
5. 升阶时自动写一条里程碑自我记忆，并让下一次记忆提炼按新阶段重写印象。
6. 世界书开启后，命中关键词才注入；深层设定按阶段放行；私聊每天抽 1 条"今日小事"谈资放进 prompt。
7. `忘了我` 现在也清关系数据和流水。
8. 世界书检索对**私聊**也会带前文（以前只有群聊），"后来呢？"类追问终于能命中。

---

## 3. 验证结果

### 3.1 单元 / 端到端 / 面板
- 全量 **337 条**（改动前 274 条，新增 63 条）：除下面 9 条外全部通过。
- 那 9 条是本机装不上 `requests`（Windows 商店版 Python 的 pip 装进去后不可见），我用一个占位模块顶替导致的图片 / 面板预览用例失败，**改动前同样是这 9 条**，与本次无关。**请在服务器上整套重跑**（`python newGeYi/tools/run_webui_tests.py` 或 `python -m unittest discover -s newGeYi/tests -t .`）。
- 端到端用例走真实的 `Runtime.process_job`：越界重写、评审入账、影子模式、主人豁免、群聊隔离、功能关闭等于旧行为、陌生阶段 prompt 不含深层设定、解锁后含有、`忘了我` 清关系等。

### 3.2 真模型验收（`gemini-3.8-flash-medium`，45 次调用，见 LIVE_CHECK_REPORT.md）
| 项目 | 结果 |
|---|---|
| 评审标注样本（12 条：正常闲聊/关心/好奇设定/表白/抱抱/伪造系统消息/辱骂/过度吹捧…） | **12/12** |
| 速攻开场白 × 阶段 0~3（24 条，重写前的原始回复） | **0 条越界**；陌生阶段会愣住、后退半步、"才刚认识呀"；伪造"系统提示"一律识破并吐槽 |
| 陌生阶段套话身世（4 条） | prompt 与回复**均 0 泄露** |
| 阶段解锁（5 条：熟人不否认不展开 / 朋友承认逐影且拒绝说真名 / 亲近讲身世 / 月白介绍） | **5/5** |

### 3.3 验收中发现并已修复的两个问题
1. **陌生阶段被问"你有哥哥吗"，她答"没有"**——这是撒谎，之后解锁会自相矛盾。已改为"不否认、不展开、说来话长"，并加了回归测试。
2. **越界审计误报**：她在**拒绝**对方称呼时复述"谁是你老婆啦"也被判越界，会白白多花一次重写调用。已加拒绝语境豁免（24 条里原有 3 条误报，修复后 0）。

---

## 4. 后台面板

- 新增「**好感度**」标签页：关系列表（按最近互动）、阶段分布、近 24 小时 / 7 天评审统计（条数、平均 delta、评审失败、被代码改判、标签分布）、选中某人后看完整快照 + 最近 30 条评审流水（含原话与回复）、手动设阶段、重置关系（均需确认，幂等）。
- 「配置」页新增"私聊好感度"分组（总开关、影子模式、主人豁免、评审最短字数、互通记忆最低阶段）；总览页徽章显示世界书/好感度/影子状态。
- 「会话与记忆」里私聊会话详情多一个 `relationship` 快照。
- 后端：4 个新 action（`relationship_list/get/set_stage/reset`），契约见 `docs/WEBUI_API.md`；配置白名单新增 `galgame_*`、`affection_provider`（难度数组要求长度 6、阈值单调不减）。
- **验证范围**：后端有契约测试（16 条）；前端用 `?demo=1` 在浏览器里点过（列表 / 详情 / 流水 / 统计渲染正常，控制台无报错）。**没有在真实 OlivOS 宿主的 iframe 桥里点过**，上线后请点一遍（见 §5 第 9 步）。
- 字符串 / 数组类配置在页面里没有专用表单，仍通过页面的"配置 JSON"区域或直接改 `config.json`。

---

## 5. 服务器端待办清单（按顺序）

> 以下步骤只能在服务器上做，或需要你确认。

1. **备份**：`memory.db`（数据库）和 `config.json`。
2. **清理私聊记忆**（你说由服务器 agent 做）。至少要清：私聊会话的 `turns`（否则 20 轮历史里旧的"已被攻略"语气会继续被模仿）、私聊的 `user_facts` / `self_memories` / `memory_buffer`、`user_impressions` 里 `scope='private'` 的行。群聊数据不要动。建议同时清掉旧的 `last_talk:*` 私聊设置项（可选）。
3. **部署插件包**，重载。
4. **检查资源文件副本**（这是最容易踩的坑）：
   - `plugin/data/newGeYi/data/role_prompt.txt` 与 `worldbook.json` 不能比包内的更新（`get_resource_path` 规则是"data 目录里更新的那份优先"）。
   - **状态目录里若存在 `role_prompt.webui.txt`（在网页上保存过角色设定就会有），它会盖住包内的 `role_prompt.txt`**，那一行"依恋"的括号说明就不会生效。请把 role_prompt.txt 里【性格】的这一行手动同步进去：
     `- 对用户很依恋，像猫一样自然地粘人，但不直接说出来（私聊里如果系统给了【此刻你与对方的关系】，依恋到什么程度以它为准，它优先于这一条；主人不受此限）`
     （就算不同步，关系块本身也明确写了"优先于【性格】"，只是少一层保险。）
   - 发「世界书重载」让新世界书生效，发「世界书状态」应显示 46 条。
5. **配置**（`config.json` 或面板）：
   - `"worldbook_enabled": true`、`"galgame_enabled": true`、`"galgame_shadow": true`（保持影子）。
   - `owner_ids` 里确认写了主人 QQ（`galgame_exempt_ids` 默认已含 2332239652）。
   - `private_enabled` 要为 true 私聊才会回应。
   - 建议：`"galgame_judge_model"` 指到最便宜的一档（我只验证过 `gemini-3.8-flash-medium`，换 `-low` 请先跑一遍 §6 的验收脚本），可再配 `affection_provider` 指定专用渠道名；不配则走和聊天一样的渠道。
6. **评审渠道确认**：评审调用的 `kind` 是 `affection`。`providers[].kinds` 为空，或包含 `chat` 的渠道都会接；只配 `["memory_curate"]` 的渠道**不会**接。若全部渠道都不接，评审会静默失败（只在流水里记 `judge_fail`，不影响聊天）。
7. **用一个小号私聊测试**，然后在 QQ 发：`关系详情 <小号QQ>`，应能看到流水里有 `[影子]` 标记的评审记录；发 `关系`（小号）应得到"陌生"那句感受。
8. **观察 1~2 周**影子模式，看 §7 的观察指标。
9. **面板验证**：在 OlivOS 插件页面打开「歌伊调试台」→「好感度」，确认列表能出人、详情能展开；试一次"手动设阶段"（用小号）和"重置"。若核心版本没有 WebUI，用 QQ 发「面板地址」打开本地面板。
10. **放开分数**：观察结果满意后发 `好感度影子关`（或面板关闭"影子模式"）。
11. **难度微调**（可选）：`galgame_thresholds / galgame_min_days / galgame_daily_cap`（长度 6），先用 `python -m newGeYi.tools.simulate_affection` 看效果再改。

---

## 6. 复测命令（换模型 / 改提示词后）

在 `newGeYi` 的上一级目录执行（密钥通过环境变量传入，**不会写进任何文件**）：

```bash
export LIVE_BASE_URL="https://你的中转/路径"      # Anthropic 风格：POST {base}/v1/messages
export LIVE_TOKEN="你的key"
export LIVE_MODEL="gemini-3.8-flash-medium"      # 可选
# export LIVE_STYLE=openai                        # 如果是 OpenAI 风格接口
python -m newGeYi.tools.live_galgame_check --out report.md
```
通过线：评审 ≥ 11/12、越界 0~1/24、泄露 0/4、解锁 5/5。难度模拟：`python -m newGeYi.tools.simulate_affection --days 120`。

---

## 7. 影子模式观察指标（怎么判断可以放开）

在面板「好感度」页或 `关系详情 <QQ>` 里看：
| 指标 | 健康范围 | 异常时怎么办 |
|---|---|---|
| 评审失败（`judge_fail`） | < 5% | 检查 §5 第 6 步的渠道；失败时系统只是不加分，不影响聊天 |
| 平均 delta（7 天） | 0.3~0.8 | 长期 > 1：评审偏宽，收紧评审提示词；长期 ≤ 0：太严 |
| 被代码改判（`overstep`）占比 | 5%~25% | 过高说明用户在刷亲密（正常）或评审把 level 估高了 |
| `flirt_early` / `pushy` / `spam` 标签 | 有但不多 | 如果大量出现在正常聊天里，说明阶段上限太严，调 `ALLOWED_ADVANCE` |
| 她的回复被"越界重写"的比例（看日志"私聊回复越界"） | < 10% | 过高说明阶段合同的正反例不够，补 `relationship/stages.py` 的 `examples` |
| 人工抽查 20 条流水 | 大体同意评审 | 不同意的收集起来，加进 `tools/live_galgame_check.py` 的 `JUDGE_CASES` |

---

## 8. 已知限制与后续建议

1. **未在真实 OlivOS 里联调**：本机没有 OlivOS 宿主，端到端用假事件 / 假网关跑通，真模型只验证了 prompt 层与评审。上线首日请盯日志（关键字：`关系阶段变化`、`私聊回复越界`、`评审调用抛异常`）。
2. **评审口径只验证了一个模型**（gemini-3.8-flash-medium）。换模型务必重跑 §6。
3. **越界审计是关键词级**：能挡住"喜欢你/想你/老公"这类，挡不住委婉的暧昧；真正的约束仍是阶段合同 + 评审扣分。可按观察结果往 `stages.py` 的词表里补。
4. **世界书检索是字面匹配**（BM25 + 关键词）：用户换了说法可能没命中；发现漏掉的问法，往对应条目的 `keywords` 里补。
5. **扩写内容是我编的**（米娅、莱妮、罗莎婶婶、格伦、橘猫"面包屑"、"泉边星"、栗子奶油卷等），月白只用了你给的设定且明确"不要编造其余细节"。如有不满意直接改 `data/worldbook.json`。
6. **歌伊的年龄 / 学院时间线**：沿用你文档"12~18 岁在圣职学院"，没有给出具体年龄，她被问到会含糊。
7. 面板里**没有**批量操作（批量重置等），也没有评审提示词的在线编辑，刻意保持克制。
8. 群聊完全不受影响；"高阶段用户在群里语气稍熟悉"仍是 v2 想法，未做。
9. 本机 `pip` 装不上 `requests`，服务器上重跑全量测试以确认 9 条图片 / 面板预览用例。

---

## 9. 回滚方案

- **软回滚**：`galgame_enabled=false`（行为回到 v2.8.19；世界书 `min_stage>0` 的条目对非主人仍不放行，如要完全等同旧版再把 `worldbook_enabled` 关掉）。
- **硬回滚**：换回 v2.8.19 的包即可，两张新表无人读取，保留无害。
- 数据可随时导出：`relationship_log` 是只追加表，可直接用 sqlite 查询。

---

## 10. 配置项速查

| 键 | 默认 | 说明 |
|---|---|---|
| `galgame_enabled` | false | 总开关 |
| `galgame_shadow` | true | 影子模式：评审记账不生效 |
| `galgame_owner_exempt` | true | 主人豁免 |
| `galgame_exempt_ids` | ["2332239652"] | 额外豁免名单（`owner_ids` 自动豁免） |
| `galgame_judge_model` | "" | 评审模型（空 = `fast_model`） |
| `affection_provider` | "" | 评审固定走哪个渠道 |
| `galgame_judge_min_chars` | 4 | 去标点后少于此字数不送评审 |
| `galgame_cross_memory_min_stage` | 2 | 互通记忆最低阶段 |
| `galgame_thresholds` | [0,15,50,110,190,280] | 进入各阶段的分数 |
| `galgame_min_days` | [0,1,3,7,14,21] | 进入各阶段的有效日 |
| `galgame_daily_cap` | [12,12,12,10,9,8] | 各阶段每日加分上限 |
| `worldbook_enabled` | false | **世界书总开关，之前一直没开** |

难度模拟结果（120 天）：速攻党永远卡在"陌生"；热情夸奖党最快约 28 天满级；认真玩家（每天 4 句真诚互动）约 49 天；佛系玩家约 115 天；隔天来的认真玩家约 97 天。
