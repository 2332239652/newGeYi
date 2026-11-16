# v2.9.15 手动切换优先于后台重试

> 现场：2026-11-12 15:47:50 骰主手动把渠道切到 `official-chat`（官方 DeepSeek），
> 13 秒后收到「所有模型渠道全挂，已切到兜底渠道 ginka-low 并锁定」，
> 渠道状态里官方渠道仍显示不可用。用户原话：「我刚因为模型掉线太快直接切到 deepseek 官方模型了，结果还是显示模型挂了」。

## 1. 现场证据

`settings.gw.history`（生产库，只读导出）：

```
15:47:50  ginka-low      -> official-chat   [manual] 锚点
15:48:03  official-chat  -> ginka-low       [all_down] timeout
```

同时间段的 `logfile/OlivOS_logfile_2026-11-12.log`：

```
15:47:40 [gateway] 全挂重试：从锚点开始试 5 个渠道（star-tiered、star-low、argon-gemini4、argon-opus46、ginka-low）
15:47:49 [gateway] 候选渠道全挂（http_503）：先不锁定，300 秒后从锚点重试；期间继续用兜底渠道回话
15:48:03 [gateway] 全挂重试仍然失败（timeout），开始通知骰主并锁定兜底渠道
15:48:03 [gateway] 候选渠道两轮全挂（timeout），已切到兜底渠道 ginka-low 并锁定；「切换渠道 X」可解除
```

注意时序：**重试（15:47:40）比手动切换（15:47:50）早 10 秒启动**，探测 5 个渠道每个 10 秒超时，
跑完已经 15:48:03 —— 手动切换正好落在这一轮的中间。重试结束后照旧走
`_escalate_to_last_resort`，把 `current`/`hold` 一起改成了 `last_resort_provider`（生产配的是
`ginka-low`），骰主的选择被后台悄悄覆盖。

## 2. 三条根因

| # | 位置 | 旧行为 | 后果 |
|---|------|--------|------|
| 1 | `providers/llm_gateway.py:861` `retry_pending_once` | 探测循环跑完就 escalate，不检查期间骰主有没有手动切过 | 手动选择被覆盖 + 被锁定 + 收到全挂通知 |
| 2 | `providers/llm_gateway.py:840` `_anchor_retry_targets` | `if not p.manual_only` 把 `manual_only` 渠道全部过滤 | 锚点是 `official-chat`（manual_only）时，「从锚点重试」**永远不试锚点** |
| 3 | `providers/llm_gateway.py:905` `_escalate_to_last_resort` | 锁定时 `_anchor_name = ''` | 解锁后发现骰主设的锚点没了 |

## 3. 改法

1. **手动代数（epoch）**：`__init__` 新增 `self._manual_epoch = 0`（`llm_gateway.py:372`）；
   `switch_to()` 一进门就 `+= 1`（`llm_gateway.py:632`，两个分支都覆盖）。
   `retry_pending_once()` 在开头记下 `epoch`，**每条渠道探测前后各核对一次**：
   变了就 `return 'cancelled'` —— 不锁定、不通知、不写 history，按骰主选的渠道继续跑。
2. **锚点无条件入选重试列表**：`(not p.manual_only) or p.name == anchor`（`llm_gateway.py:851`）。
   优先级天花板逻辑不变（仍绝不越过锚点往上），只是不再把锚点自己筛掉。
3. **锁定不再抹锚点**：去掉 `self._anchor_name = ''`，改为注释说明；锁定期 `_ordered_pairs`
   只看 `hold`，锚点本来就无效，保留它只是让解锁后骰主的选择还在。
   顺手把 `_pending_hold_at` 收掉，避免「已经锁了还显示冷却中」。

## 4. 测试

`tests/test_gateway_failover.py::ManualPriorityTest`（3 条）：

- `test_retry_aborts_when_manual_switch_happens_midway`：探测回调里调 `switch_to('p3')`，
  断言返回 `'cancelled'`、`_hold_name == ''`、没有通知、当前渠道是 `p3`。
- `test_retry_targets_include_manual_only_anchor`：`switch_to('official-chat')` 后
  `_anchor_retry_targets()[0].name == 'official-chat'`。
- `test_escalation_keeps_manual_anchor`：真锁定后 `_anchor_name` 仍是 `p1`。

全量 **650 / 650**。

## 5. 部署时同步做的运维

- 生产状态当时是 `gw.hold = ginka-low`、`gw.anchor = ''`（历史 `all_down` 留下的锁）。
  部署前先备份 `geyi.sqlite3`，再把 `gw.hold` 清空、`gw.anchor`/`gw.current` 恢复成
  `official-chat`、`gw.unhealthy` 清空（那些条目是 15:20–15:48 那波 503/timeout 的残留，
  渠道早就恢复了），然后重载插件让它按新状态起。
- 生产 `failover.last_resort_provider` 当时是 **`ginka-low`**（中转站，priority 91），
  不是官方渠道。若希望「全挂两轮后锁到官方 DeepSeek」，把这一项改成 `official-chat`；
  不改也不影响本次修复 —— 只是锁定目标不同。
  **已在 v2.9.17 改掉**：兜底 = `official-chat`，`ginka-low` 降到 priority 93 当普通轮转渠道，
  见 `docs/FALLBACK_POLICY_v2.9.17.md`。

## 6. 回滚

三条改动彼此独立，可单独回滚：

- 只想去掉「手动优先」：把 `retry_pending_once` 里的两处 epoch 判断删掉即可（其它两条保留）。
- 想完全回到 v2.9.14：`git revert` 本提交，或把 `failover.retry_before_hold_seconds` 设为 `0`
  回到 v2.9.13 的「一次全挂立刻锁」。
