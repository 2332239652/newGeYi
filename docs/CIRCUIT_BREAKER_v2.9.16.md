# 渠道熔断 v2.9.16（2026-11-12）

## 1. 需求原话

> 你能不能设计一个多次失败就移出队列等一段时间，通了再回来的机制，免得队列里有几个渠道挂了歌伊一直等着。

## 2. 旧行为：排到队尾 ≠ 移出队列

`_ordered_pairs()` 的排序键里有一项 `1 if not state.healthy else 0`，
作用是让不可用渠道排到队尾。问题在于它们**仍然在候选里**：

```
[当前渠道] → 健康渠道 → 死渠道1 → 死渠道2 → 死渠道3 → 死渠道4
```

每条消息走完健康渠道后，还要依次在每条死渠道上各等一次 `timeout=60`，
直到 `total_timeout_seconds=150` 的总预算耗尽。用户体感就是「歌伊一直在等」。

当日生产数据（`api_calls`，11-12）：

| 渠道 | 成功/总数 | 成功率 |
| --- | --- | --- |
| `gemini-3.8-flash-tiered`（star-tiered） | 1309/1647 | 79% |
| `gemini-3.8-flash`（ginka-low） | 588/883 | 67% |
| `gemini-3.8-flash-low`（star-low） | 93/268 | 35% |
| `gemini-4-argon` | 0/34 | 0% |
| `claude-opus-4-6` | 0/30 | 0% |
| `deepseek-flash`（official-chat） | 36/36 | 100% |

两条 argon 渠道整天 0 成功，却一直待在队列里，就是「掉线太快」的直接原因。

## 3. 新行为：熔断冷却

`ProviderState` 新增 `cooldown_until`。渠道被判死的那一刻起，到该时刻之前**整条移出候选**。

冷却时长（`failover` 配置）：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `cooldown_base_seconds` | 120 | 首次冷却 2 分钟；再失败翻倍（120 → 240 → 480 …） |
| `cooldown_max_seconds` | 1800 | 上限 30 分钟；`401/403/no_api_key` 直接关满 |

判定与召回：

1. `_mark_failure`：连败达 `fail_threshold`（生产 = 2）→ `healthy=False` 且
   `cooldown_until = now + min(cap, base × 2^(连败-阈值))`。
2. `_mark_success`：清零冷却与连败。
3. `_ordered_pairs`：冷却中的渠道**不出现在候选里**；全部都在冷却时，只硬试
   **最早到期的那一条**（避免「一条都发不出去」，也避免五连等）。
4. `probe_once`：冷却未到期的渠道不白花探测；到期后照旧探测，成功切回并清零，
   失败则再关 `cooldown_base_seconds`（不翻倍，避免探测本身把冷却拉爆）。
5. `switch_to(渠道)`：骰主亲手点的渠道**清零冷却**并复位健康 —— 手动操作必须真去试，
   否则就又出现「我切了它还在用别人」（v2.9.15 同类事故）。
6. `status_text`：不可用渠道追加 `·冷却 Ns`。

冷却时刻**不落盘**（`gw.unhealthy` 仍然只记名单）：重载后冷却清零，被标记的渠道重新参与
队尾轮转，一次失败立刻重新关起来。这样做的理由与 v2.9.15 的「重载不回读不可用标记」一致 ——
内存状态比旧快照可信；而冷却的价值在**运行期**（避免每条消息空转），不在重启后。

## 4. 与软降级（v2.9.14）的关系

两者互不冲突，解决的是不同层次：

- **熔断**：单条渠道挂了 → 它自己退出队列，不再拖累每条消息；
- **软降级**：候选**全部**挂了 → 先等 300 秒从锚点重试，仍失败才锁定兜底 + 通知骰主。

全挂时由于所有渠道都在冷却，`_ordered_pairs` 只返回兜底那一条，消息**立刻**在兜底渠道上发出，
不再在四条死渠道上空转 150 秒。

## 5. 测试

`tests/test_gateway_failover.py::CoolingTest`（7 条）：

- `test_cooling_provider_leaves_the_queue`
- `test_expired_cooling_returns_to_queue`
- `test_all_cooling_still_returns_one_candidate`
- `test_success_clears_cooldown`
- `test_manual_switch_clears_cooldown`
- `test_failure_cools_then_escalates_on_retry`（60 → 120 秒翻倍）
- `test_zero_base_disables_circuit_breaker`

两个老用例（`test_manual_switch_unlocks`、`test_soft_switch_keeps_other_channels_candidates`）
原本断言「全挂后别的渠道仍留在候选里」，现在改为「冷却到期后回到候选里」——
这才是「没锁定」的实际含义。全量 **657 / 657**。

## 6. 回滚

`config.json` → `failover.cooldown_base_seconds = 0`：熔断关闭，退回「排到队尾」的老行为。
无需改代码、无需重载。

## 7. 同日新增渠道

`ginka-glm`：`https://api.ginka.cloud/v1/chat/completions`，模型 `glm-5.3`，
priority 92，`kinds = chat + memory_curate`，`timeout 60`，带与 `ginka-low` 相同的浏览器 User-Agent。
（16:23 又调了一次顺序：`ginka-low` 91 → 93 排到最后，`ginka-glm` 92 反而在它前面 ——
骰主原话「ginka-low 确实不稳定，不适合作为兜底」，见 `docs/FALLBACK_POLICY_v2.9.17.md`。）

上线前实测：`GET /v1/models` 返回 7 个模型（含 `glm-5.3`、`glm-5.3-flash`）；
`POST /v1/chat/completions` + `glm-5.3` 4.8 秒返回 200，OpenAI 兼容格式。
