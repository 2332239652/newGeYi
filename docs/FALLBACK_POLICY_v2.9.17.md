# 兜底渠道换成官方 DeepSeek（v2.9.17，2026-11-12 16:23）

骰主原话：

> ginka-low 确实不稳定，不适合作为兜底，把它也安进轮转池最后一个好了，只剩下官方 ds 兜底

## 1. 改了什么

| 项 | 改前 | 改后 |
|---|---|---|
| `failover.last_resort_provider` | `ginka-low`（ginka 中转站） | **`official-chat`**（官方 DeepSeek 直连） |
| `ginka-low` 的 priority | 91（倒数第二） | **93（轮转池最后一个）** |
| `ginka-glm` 的 priority | 92 | 92（不动，现在排在 ginka-low 之前） |

只动 `config.json`，代码里的默认值 `config.py:167` 本来就是 `official-chat` ——
也就是说生产配置一直在覆盖默认值，这次是把它改回默认。

为什么：`ginka-low` 是 gemini-3.8-flash 的 ginka 中转，当天 **588/883 = 67%**，
夜里 00:09、00:12 两次全挂都有它；当兜底等于「全挂之后切到一条也常常挂的渠道」，
而兜底的含义恰恰是「其他全死了它得活着」。它现在只是普通轮转渠道，并且排在
`ginka-glm`（GLM-5.3，11-12 刚实测通过）之后。

## 2. 顺手修的一个坑：兜底渠道自己成功，不算链路恢复

`_mark_success` 原来只要**任何**渠道调用成功就撤掉「全挂待重试」闹钟，注释写的是
「冷却期内真的有调用通了：链路自己恢复了」。但 v2.9.14 之后，待重试期间机器人是
被**软切到兜底渠道**上的 —— 它成功是理所当然的，跟原渠道恢复没半点关系；
闹钟一撤，`probe_once()` 又因为「当前渠道是 `manual_only` 且不是锚点」完全不动手
（`providers/llm_gateway.py` 里 `probe_once` 的三种不介入情况之一）。

结果就是：一次夜间抖动 → 软切官方 DeepSeek → 它回得好好的 → 闹钟没了 → 探测不再动手 →
**机器人安静地一直挂在官方渠道上烧钱**，只有骰主自己想起来发「切换渠道 自动」才会回去。
兜底原来是中转站时这个坑也存在，只是烧的是中转额度；换成官方直连之后必须修。

改法（`providers/llm_gateway.py`）：

```python
def _last_resort_name(self) -> str:
    return str(self._failover_cfg.get('last_resort_provider', 'official-chat') or '').strip()

# _mark_success 里
if self._pending_hold_at and provider.name != self._last_resort_name():
```

`_escalate_to_last_resort` / `_soft_switch_to_last_resort_locked` 里重复的两行读配置
也顺手换成 `self._last_resort_name()`。

## 3. 现在的完整链路

```
正常轮转（自动）：star-tiered(3) → star-low(4) → argon-gemini4(5) → argon-opus46(6)
                  → ginka-glm(92) → ginka-low(93)
      ↓ 两轮（full_cycles=2）全挂
软降级：切 official-chat(7) 回话，不锁定、不通知，起 300 秒闹钟
      ↓ 300 秒后从锚点重试（探测期间骰主手动切换会取消重试）
   ├─ 通了：静默切回去，闹钟撤销            （retry_pending_once() == 'recovered'）
   └─ 没通：通知骰主 + 锁定 official-chat   （== 'held'，只能「切换渠道 X/自动」解除）
```

熔断冷却（v2.9.16）与这条链路是正交的：冷却决定「哪些渠道进候选」，兜底决定「一个候选都没有时用谁」。

## 4. 测试

`tests/test_gateway_failover.py::SoftHoldTest`：

- `test_success_during_cooldown_cancels_pending` —— 改写成「已切回普通渠道 p1 时成功」才撤闹钟；
- 新增 `test_last_resort_success_keeps_pending_retry` —— 兜底渠道成功时闹钟必须还在、且不锁定。

全量 **658/658** 通过（`tools/run_webui_tests.py`）。

## 5. 回滚

纯配置，改回去即可（无需重载代码，但配置是进程启动时读的，要重载才生效）：

```json
"failover": { "last_resort_provider": "ginka-low" },
"providers": [ { "name": "ginka-low", "priority": 91 } ]
```

生产改配置的脚本：工作区外的临时脚本 `geyi_set_fallback.py`
（默认 dry-run，`--apply` 才写，备份 `config.json.bak-fallback-<时间戳>`，原子替换）。
