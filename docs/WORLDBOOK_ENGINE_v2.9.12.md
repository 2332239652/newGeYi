# v2.9.12：世界书检索重做 + 剧情探索模式（2026-11-11）

这批改动在 11-11 下午到深夜连续提交（`f1be78e` / `9d00e8e` / `52dbbca`），
当时没有写在任何文档里，只有 commit message 和代码注释。这份文档是补记。

## 一、为什么改

线上观察到的两类问题：

1. **该出的出不来**：群友正常问「猎人公会怎么来的」，因为虚词二元组（你们/那边/怎么/来的）
   把 BM25 覆盖率的分母撑大，直接被 `MIN_COVERAGE=0.6` 挡死；
2. **不该出的乱出**：单个汉字关键词（怪、海、崇、电、雷、猫、龙…）命中日常词
   （怪累 / 上海 / 崇拜 / 充电 / 避雷 / 熊猫），把世界书条目硬塞进闲聊。

## 二、检索侧（`memory/worldbook.py`）

- **虚词剔除**：`_STOP_BIGRAMS`，覆盖率计算前先剔掉口语二元组；
- **碰撞遮罩**：`_DEFAULT_COLLISIONS`，单字关键词匹配前先把已知碰撞词从文本里遮掉；
- **噪声闸门**：`_AMBIENT_KEYWORDS`（泛化日常词）只有在句子里出现 `_PERSONA_ANCHORS`
  （歌伊/教会/清泉/猎人…）时才放行；若同时出现 `_SELF_OR_MODERN_DISTRACTORS`
  （我/游戏/考研/外卖/城市名…）则按弱关键词拦截；
- **追问继承**：`_FOLLOWUP_CUES`（后来/那他/在哪/……」）让当前句极短时继承最近几轮命中的条目；
- **关联扩散**：`links` 联动加成，剧情模式下支持**二跳**（`link2:<id>`）。

## 三、剧情探索模式（`story_mode`）

用户自己开，一个人一个状态：

```
剧情模式            # 看话题线索
剧情模式开 / 关
剧情模式列表        # 骰主
剧情模式重置 <QQ/全部>   # 骰主
```

- 持久化：`settings` 的 `story_mode_user:<QQ>`（按人）与 `story_mode:<session>`（私聊会话）；
- 开启后：世界书注入上限 3 → **6** 条、总字数 2400 → 4200、前文回溯 5 → 8 句、
  纯 BM25 覆盖率门槛 0.6 → 0.36（或命中高 IDF 核心设定词）、联动加成 5 → 7、允许二跳；
- 提示词追加 `STORY_MODE_CLAUSE`（可展开到 80~160 字，但仍守当前关系阶段的解锁边界）；
- 新配置键：`worldbook_max_entries`（默认 3）、`worldbook_story_max_entries`（默认 6），
  都已进面板白名单。

## 四、连带改动

- `relationship/service.py`：谈资「说了才算数」——抽中先登记，等歌伊回复里**真的提到**
  （至少 2 个非虚词二元组重合）才记 `told_topics`，避免白消耗；
- `panel.py`：`debug_preview` 改用只读代理（`_RelationshipPreview` / `preview_block_for`），
  预览不再写库、不变更状态；
- 世界书数据从 46 条扩到 **49 条**（跨世界观框定 + TRPG/骰娘 lore）。

## 五、验证

- 全量测试：本次发版一并跑的 **626 条**（`tools/run_webui_tests.py`）；
- 检索与阶段门的用例在 `tests/test_relationship.py`（`WorldbookStageGateTest` / `ShippedWorldbookTest`）
  与 `tests/test_panel.py`；
- ⚠️ 这批代码提交后**没有重载过线上**（线上仍是 11-11 14:07 的代码），
  v2.9.12 重载后才是第一次真机运行，观察点：`[router]` 日志里世界书注入条数、
  `剧情模式` 命令回包、面板 `debug_preview` 是否仍只读。
