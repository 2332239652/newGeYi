# 缓存命中率：为什么「几乎总是 0%」，以及改成了什么（2026-11-09）

## 0. 结论

「`歌伊状态` 里缓存命中几乎总是接近 0」是**三件事叠加**，不是单一 bug：

| # | 原因 | 属于 | 状态 |
|---|---|---|---|
| 1 | 显示层用整数地板除法：0.28% → `0%`、1.2% → `1%` | 插件 | 已修 |
| 2 | Anthropic 口径的 `input_tokens` **不含**缓存，分母不对（会出现 12500%） | 插件 | 已修 |
| 3 | 渠道（star 系中转）**根本没透传**缓存字段 | 渠道 | 无法修，但现在会**直说** |

改完之后：低于 10% 保留一位小数、低于 0.1% 显示 `<0.1%`、10% 以上向下取整、上限 100%；
当天没有任何响应带缓存字段时，状态行会补一句「该渠道未上报缓存字段」。

## 1. 线上证据（只读查询 `geyi.sqlite3`，53,321 行 api_calls）

按天回放（同一批真实数字，新旧显示对比）：

| 日期 | 输入 token | 命中缓存 | 真实比率 | 旧显示 | 新显示 |
|---|---|---|---|---|---|
| 2026-11-09 | 2,291,575 | 0 | 0 % | 0% | 0%｜未上报 |
| 2026-11-08 | 19,716,501 | 55,799 | 0.28 % | **0%** | 0.3% |
| 2026-11-07 | 16,954,038 | 200,327 | 1.18 % | 1% | 1.2% |
| 2026-11-06 | 20,561,987 | 611,435 | 2.97 % | 2% | 3.0% |
| 2026-11-05 | 14,300,313 | 24,297 | 0.17 % | **0%** | 0.2% |
| 2026-11-03 | 11,674,253 | 7,528 | 0.064 % | **0%** | <0.1% |
| 2026-11-02 | 9,194,737 | 43,179 | 0.47 % | **0%** | 0.5% |

按模型（全部历史，`ok=1`）：

| 模型 | ok 调用 | 带缓存字段的调用 | 总输入 | 命中 | 说明 |
|---|---|---|---|---|---|
| gemini-3.8-flash-tiered | 14,659 | 31 (0.2%) | 84.9 M | 104,808 | 中转极少上报 |
| gemini-3.8-flash-low | 13,174 | 14 (0.1%) | 64.2 M | 51,451 | 同上；今日 0 次 |
| deepseek-flash（官方） | 6,275 | 1,250 (**19.9%**) | 21.5 M | 2,813,056 | 官方口径正常，命中 13% |
| claude-opus-4-6 | 288 | 2 | 2.25 M | 10,748 | Anthropic 口径问题（见下） |
| gemini-4-argon / 其余 | — | 0 | — | 0 | 从未上报 |

两处「不可能的比例」（分母口径不对的实锤）：

- `claude-opus-4-6`：`in_tokens=43, cached=5376` —— Anthropic 的 `input_tokens` 不含缓存读；
- `gemini-3.8-flash-low`：`in_tokens=2355, cached=3242` —— 该中转把 `prompt_tokens` 与
  `cached_tokens` 报成**互斥**（prompt 只给未命中部分）。

## 2. 改了什么

### 2.1 `providers/llm_gateway.py`

- 新增 `_CACHE_TOKEN_PATHS` 字段表，覆盖：`prompt_tokens_details.cached_tokens`、
  `input_tokens_details.cached_tokens`、`cached_tokens`、`cached_input_tokens`、
  `prompt_cache_hit_tokens`（DeepSeek）、`cache_read_input_tokens`（Anthropic）、
  `cachedContentTokenCount` / `cached_content_token_count` 及其在 `usageMetadata` 下的形态（Gemini 原生）。
- 新增 `response_usage(data)`：`usage` 为空时退回 `usageMetadata`。
- 新增 `usage_tokens(usage, protocol)`：**总输入口径只在这里定义一次**。
  - Anthropic：`总输入 = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`；
  - OpenAI 兼容：`prompt_tokens` 已含缓存时直接当总量；`cached > prompt` 时判为互斥口径并相加；
  - 兜底：`cached` 永远 ≤ 总输入。
- 新增 `usage_reports_cache(usage)`：字段**存在**即为已上报（显式 0 也算）。
- `_parse_response()` 三个分支不再各算一套 token；返回值仍是 6 元组（测试里有 monkeypatch，签名不能变）。
- `_call_provider()` 在 200 分支计算 `cache_reported` 并放进 `ModelResult`。

### 2.2 `models.py`

`ModelResult` 增加 `cache_reported: bool = False`（加在末尾，位置参数不受影响）。

### 2.3 `storage/database.py`

- `api_calls` 加列 `cache_reported INTEGER DEFAULT 0`（`_SCHEMA` 新建库 + `_migrate` 老库 `ALTER TABLE` 补列）；
- `add_api_call(..., cache_reported=False)`（新参数在最后，旧调用方式不变）；
- `today_stats()` 多返回 `cache_reported_calls`。

### 2.4 `routing/commands.py`

- `cache_rate_text(cached, prompt_total)`：小数/`<0.1%`/向下取整/封顶；
- `cache_report_hint(today)`：只有「有输入、零命中、且当天没有任何一次上报」才提示「未上报」；
- `_cmd_status` 的 `_token_line` 与 `_cmd_usage` 共用这两个函数（以前两处各写一遍算术）。

## 3. 验证

- 全量回归：**508 / 508 通过**（`tools/run_webui_tests.py`，新增 `tests/test_cache_usage.py` 32 条）；
- 新测试覆盖：8 种字段形态、3 种口径（Anthropic / 子集 / 互斥）、Gemini 原生 usageMetadata、
  显式 0 与缺失的区分、显示函数边界（0.3% / <0.1% / 99% / 100%）、**老库自动补列且不丢行**；
- 兼容性：`tests/test_usage.py` 原有 5 条字段用例原样通过；`_parse_response` 签名未变；
- 用真实历史数字回放，即 §1 的表。

## 4. 已知限制（别当成 bug 又改一遍）

1. **互斥口径只能靠 `cached > prompt` 判定**。若某中转既互斥、又恰好 `cached ≤ prompt`，
   仍按「prompt 已含缓存」处理，会少算一点总输入（比例偏低）。无法从单条响应区分。
2. **老数据不会重算**：历史行保留当时的数字；新列对老行是 0，按「未上报」显示。
3. **Anthropic 协议没有发 `cache_control` 断点**，所以该协议下读缓存理论上恒为 0。
   要开是一个**独立的成本决定**（缓存写入按 1.25× 计费，且有 1024 token 起、最多 4 个断点的限制），
   本次没动。
4. **渠道不报就是没有**：star 系中转的 0.1–0.2% 上报率是它自己的行为，
   插件这边只能如实显示「未上报」，不能凭空造数字。

## 5. 回滚

- 代码：`llm_gateway.py`（usage 段）→ `models.py`（一个字段）→ `database.py`（一列 + 两个方法）→
  `commands.py`（两个纯函数 + 两处调用）；
- 数据库：`api_calls.cache_reported` **只加不删**，回滚代码后这一列留着无害（旧代码不读它）；
- 行为回滚的最小集：只把 `cache_rate_text` 改回 `cached * 100 // inp` 即可恢复旧显示，
  其余口径修正可以保留。

## 6. 附：这次改动顺带发现并修掉的一个"导入即迁移"问题

验证过程中，直接用 `python -m unittest newGeYi.tests.test_cache_usage` 跑单模块时，
父包 `newGeYi` 的导入会先执行 `__init__ → main → Runtime()`，而测试模块顶上那句
"先设 `GEYI_DATA_DIR` 再导入"要等**包导入之后**才生效 —— 于是 `Runtime()` 打开了
**线上**状态目录，并对生产库执行了本文件新增的 `ALTER TABLE api_calls ADD COLUMN cache_reported`。

事后核查（只读，2026-11-09 15:08）：`PRAGMA integrity_check` = ok；16 张表/16 个索引无增减；
`api_calls` 行数与 `SUM(in_tokens)`、`SUM(cached_tokens)` 与迁移前一致；新列 0 行标记；
无测试脏数据；`config.json` 哈希与 mtime 未变。**结论：只多了一列，下次重载本来也会加。**

加固（本次一并提交）：`config.get_state_dir()` 在「进程里有 unittest 且没有显式
`GEYI_DATA_DIR`」时自动隔离到临时目录（不写环境变量，测试模块自己的 `setdefault` 仍生效），
并加了 `tests/test_config_safety.py::UnittestIsolationTest` 两条用例。
以后跑单个测试模块，要么用 `tools/run_webui_tests.py`，要么先设 `GEYI_DATA_DIR`。
