# v2.9.16：渠道熔断冷却

## 背景

将不可用渠道排到队尾仍会让每次请求逐一等待其超时。本版本增加冷却期，使持续失败的渠道暂时退出候选集。

## 行为

- 连续失败达到 `fail_threshold` 后，设置 `cooldown_until`，从候选渠道中暂时移除。
- 首次冷却时长为 `cooldown_base_seconds`；连续失败时按 2 的幂递增，最大不超过 `cooldown_max_seconds`。
- 冷却期到期后渠道重新参与探测和路由；成功会清除冷却及连续失败计数。
- 若所有渠道都在冷却，仅尝试最早到期的一条，避免所有候选同时被过滤。
- 手动选择渠道会清除该渠道的冷却状态，确保手动操作立即生效。
- 冷却状态仅保存在进程内；进程重启后由健康探测和实际调用重新评估。

## 默认值与回滚

| 配置 | 默认值 | 用途 |
| --- | ---: | --- |
| `cooldown_base_seconds` | 120 | 初始冷却时长 |
| `cooldown_max_seconds` | 1800 | 冷却时长上限 |

将 `failover.cooldown_base_seconds` 设为 `0` 可关闭熔断，恢复不可用渠道仅排至队尾的行为。覆盖用例位于 `tests/test_gateway_failover.py::CoolingTest`。
