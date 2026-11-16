# 私聊好感度「全员刷屏误判」修复（2026-11-16 现场排查）

> 起因：骰主转达"有人今天一天都没加好感度"。
> 结论：**私聊的复读判定拿本轮这句话和它自己比**，相似度恒为 1.0 → 每条私聊都记 `spam` 扣分，
> 评审模型从不被调用，`bad_streak` 攒到 3 就把人锁 4 小时（锁住期间连「今日首次互动 +1」都不给）。
> 11-15 02:43 起累计 281 条误判、−188.85 分。已修代码、补测试、修数据并重载。

数据口径：`relationship_log` / `user_relationship` / `turns`（`plugin/data/newGeYi/geyi.sqlite3`），
日志 `OlivOS_logfile_2026-11-1[56].log`。

---

## 1. 现象

按天统计 `relationship_log.tag`：

| 日期 | 正标签（share / care / humor / curious_lore） | `spam` |
|---|---|---|
| 11-13 | 66 / 16 / 25 / 46 | 1 |
| 11-14 | 55 / 49 / 28 / — | 5 |
| 11-15 | 15 / 14 / — / — | **212** |
| 11-16 | 0 / 0 / 0 / 0 | **68** |

- 11-16 全天 `api_calls kind='affection'` **只有 1 次**（22:49 `deepseek-flash`）——因为 `spam` 在调用评审之前就短路了，
  11-15 也只有 7 次。
- 10 个用户今天 `gain_today=0.0`，其中 6 个 `locked_until` 落在 11-17 凌晨（4 小时锁）。
- 误判样本：`'歌伊，早安，像这样每天道一声早安，一天心情都会好很多呢'`、
  `'如果和歌伊一样会治疗术的话，会方便很多吧'`、`'＃第二天清晨和歌伊打招呼"早上好歌伊，我可以开始工作了吗"'`
  ——都是第一次说的话，也照样判成刷屏。

## 2. 根因

两个改动各自都对，凑在一起才炸：

1. `runtime/app.py:643-645`（`handle_message`）**私聊消息一到就落库**，并打上 `history_pre_recorded=True`：

   ```python
   if not msg.is_group:
       self._remember_user_turn(msg)
       msg = replace(msg, history_pre_recorded=True)
   ```

   这是 v2.8.10 为了"早退分支也不丢上下文"做的，本身没问题。

2. `runtime/app.py:926-934`（`process_job`）取历史快照时，注释写的是"在写本轮用户消息之前冻结前序历史"：

   ```python
   relationship_history = tuple(
       dict(turn) for turn in self.store.recent_turns(session_key, limit=8)
       if ((turn.get('metadata') or {}).get('space_id') or 'chat') == msg.memory_space_id
   )
   ```

   但本轮的用户轮**已经在第 1 步写进 `turns` 了**，所以这份"前序历史"里包含了本轮自己。

3. `relationship/service.py:364-403` 的复读判定 `is_repeat(text, recent, ...)`（`relationship/state.py:363-396`）
   拿 `msg.text` 和这份历史比：最后一条正是同一句话，去标点后 `old_norm == cur` → `spam`，
   `delta=-1`、`bad_streak += 1`，评审模型整段跳过。

群里不受影响（群聊 `remember_user=False`，历史来自 `live_context`），主人（`galgame_exempt_ids`）也不计入。

**为什么 11-10 那次抽查没发现**：那次修的是 `is_repeat` 自身的时间窗/前缀/阈值，
而这个自我匹配是**输入污染**，相似度是 1.000 的合法输入，任何阈值都拦不住。
离线测试也覆盖不到：`tests/test_relationship_flow.py` 的 `run_job` 直接调 `process_job`，
没有走"到达即落库"这一步。

## 3. 修法

- `runtime/private_turns.py` 新增 `strip_current_turn(history, msg)`：只从**尾部**按文本精确匹配
  剥掉本轮的 1~N 行（`coalesced_messages` 合轮时每句一行，剥除上限就是本轮消息数），
  所以"上一轮真的说过同一句"不会被连带删掉，下一句仍然会被正确判成复读。
- `runtime/app.py:926-938`：`history_pre_recorded` 为真时先 `strip_current_turn()`，否则照旧补记。
- 新增测试：
  - `tests/test_private_turns.py::StripCurrentTurnTest`（4 条：单条剥 1 / 上轮同句保留 / 合轮全剥 / 非本轮不动）；
  - `tests/test_relationship_flow.py::EndToEndTest::test_pre_recorded_private_turn_is_not_counted_as_repeat`
    （走真实 `process_job`，断言评审被调用 1 次、标签为 `care`、`delta>0`、`bad_streak=0`）。
  - 反向验证：把 `strip_current_turn` 换回恒等函数，该测试立刻失败（`judge_calls 0 != 1`），说明它真的守住了这条路径。

收益是双份的：不仅不再误判 `spam`，评审提示里的"最近对话"也不再重复出现当前这句。

## 4. 数据修复

备份：`plugin/data/newGeYi/geyi.sqlite3.bak-affrepair-20261116-232008`（`sqlite3.backup()` 一致性快照）。

对 11-15 00:00 之后 `spam` 造成的实际扣分**原额退回**，同时清 `bad_streak` / `locked_until`，
并在 `relationship_log` 补一条 `note='repair_spam_selfmatch'` 的流水（tag=`neutral`、`delta_raw=0`），
让流水累计与好感度数值对得上：

| 用户 | 误判条数 | 退回分数 | 好感度 | 阶段 |
|---|---|---|---|---|
| 3977859449 | 64 | +63.10 | 30.75 → 93.85 | 2 |
| 1643795272 | 46 | +43.95 | 19.00 → 62.95 | 1 → 2（被误降级，复原） |
| 2654820230 | 36 | +35.10 | 64.20 → 99.30 | 2 |
| 3638805504 | 25 | +23.50 | 3.00 → 26.50 | 0 → 1（被误降级，复原） |
| 3573522352 | 13 | +12.10 | 81.65 → 93.75 | 2 |
| 3254825603 | 10 | +9.00 | 13.00 → 22.00 | 1 |
| 2535065924 | 2 | +1.10 | 55.90 → 57.00 | 2 |
| 3880987578 | 46 | +1.00 | 0.00 → 1.00 | 0（`bad_streak` 46 → 0） |
| 2700937932 | 38 | 0 | 0.00 | 0（分数在 0 底，只清 `bad_streak` 9 → 0、解锁） |
| 748048217 | 1 | 0 | 9.00 | 0（只清 `bad_streak`） |

### 补发漏加分（第二批，骰主同意后执行）

第一轮只退实际损失、不补"这两天本该拿到"的分；骰主确认后补做：

- 口径：`credit = clamp(该用户健康期 11-10~11-14 日均正收益, 6.0, daily_cap[stage])`，
  今天（11-16）再夹紧到 `daily_cap[stage] - 当前 gain_today`，避免突破当日上限。
- 封顶走插件自己的 `_gate_cap()`、升级走 `_try_advance()`，与线上规则完全一致；只补"有私聊活跃的那几天"。
- 脚本：工作区外的临时修复脚本 `_geyi_comp_missed_gain.py`（默认 dry-run，`--apply` 才写盘）。
- 备份：`geyi.sqlite3.bak-comp-20261116-232933`；流水 `tag='neutral'`、`note='repair_missed_gain day=<日期>'`。

| 用户 | 健康期日均 | 每日补 | 合计 | 好感度 | 阶段 |
|---|---|---|---|---|---|
| 1643795272 | 10.00 | 10.0 | +20.00 | 62.95 → 82.95 | 2 |
| 2654820230 | 9.12 | 9.1 | +18.20 | 99.30 → 117.50 | 2 → 3（亲近） |
| 3977859449 | 8.79 | 8.8 | +17.60 | 93.85 → 111.45 | 2 → 3（亲近） |
| 3573522352 | 8.07 | 8.1 | +16.20 | 95.55 → 111.75 | 2 → 3（亲近） |
| 2535065924 | 5.28 | 6.0 | +12.00 | 57.00 → 69.00 | 2 |
| 3254825603 | 6.00 | 6.0 | +12.00 | 22.00 → 34.00 | 1 |
| 3638805504 | 5.62 | 6.0 | +12.00 | 26.50 → 38.50 | 1 |
| 2700937932 | —（无健康期数据） | 6.0 | +12.00 | 0.00 → 12.00 | 0 |
| 3880987578 | —（无健康期数据） | 6.0 | +12.00 | 1.00 → 13.00 | 0 |
| 748048217 | 2.25 | 6.0 | +6.00 | 9.00 → 15.00 | 0 → 1（熟人） |

合计 +138.00。三名用户越过 110 分门槛升到「亲近」，与"没有这次 bug 时本来会走到的位置"一致
（他们 11-14 就已在 93~100 分，被吞掉的正是 11-15/16 两天的加分）。`748048217` 在 15 分门槛上升「熟人」同理。

## 5. 验证

- `tools/run_webui_tests.py`：`Ran 894 tests ... OK (skipped=5)`（原 889 + 新增 5）。
- 重载：`23:20:34` 请求 → `23:20:56 初始化完成（代码时间戳 11-16 23:14:31）` → `23:21:07 init_after 完成`，
  之后日志 `0 Traceback / 0 [ERROR]`。
- 线上第一轮私聊（重载后 2 分钟）：`23:22:09 3573522352 +1.80 tag=humor aff 93.75→95.55`，
  与修复前同样输入必被判 `spam -1` 形成对照。

## 6. 回滚

1. 代码：还原 `runtime/app.py`（`process_job` 里那段 `strip_current_turn` 调用）与 `runtime/private_turns.py`（删掉新增函数）。
   两处改动互不影响其他逻辑，单独回退即可。
2. 数据：停插件后 `copy /Y geyi.sqlite3.bak-affrepair-20261116-232008 geyi.sqlite3`（注意同时删掉同名 `-wal`/`-shm`）。

## 7. 遗留

- `spam` 判定依旧只管"同一句话在 30 分钟内重复"；本轮自我污染已消除，但**同一句在 30 分钟内真的发两次仍会扣分**
  （这是设计意图，测试 `test_previous_identical_message_is_kept` 守住这条）。
- `runtime/app.py` 里 `relationship_history` 的注释已同步改写，避免下一个人再按旧假设改回去。
