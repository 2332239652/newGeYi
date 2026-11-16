# 「全部掉线」是怎么被放大出来的（v2.9.9）

## 问题

骰主反馈：最近显示「所有模型渠道都连不上」的次数有点多。要分清是**中转站真挂了**
还是**程序判定有误**。

## 先看事实：6 次全挂事件

```
11-05 12:13:56   http_503
11-05 12:15:05   http_503        ← 相隔 69 秒
11-06 10:41:06   http_429
11-06 13:44:11   timeout
11-07 18:37:03   timeout
11-07 19:37:27   timeout
11-07 19:38:00   timeout         ← 相隔 33 秒
11-07 19:39:56   timeout         ← 相隔 116 秒
```

11-07 晚上 19:37~19:40 那三次，把每一次前后的真实调用拉出来：

```
19:35:06  active    gemini-3.8-flash-tiered  ok=1    6349ms
19:35:37  active    gemini-3.8-flash-tiered  ok=1    6795ms
19:35:52  active    gemini-3.8-flash-tiered  ok=1   14976ms
19:35:55  passive   gemini-3.8-flash-tiered  ok=1    4810ms
19:37:27  passive   gemini-3.8-flash-tiered  ok=0   60530ms  timeout
19:38:00  curate    gemini-3.8-flash-tiered  ok=0  121194ms  timeout
19:38:28  passive   gemini-3.8-flash         ok=0   60537ms  timeout   ← 已切 ginka
19:39:56  active    gemini-3.8-flash         ok=0   60528ms  timeout
19:40:01  curate    gemini-3.8-flash         ok=0   60529ms  timeout
```

再看 18:37 那次：

```
18:37:03  curate    gemini-3.8-flash-tiered  ok=0  120547ms  timeout
18:37:26  passive   gemini-3.8-flash-tiered  ok=1   40366ms  ← 23 秒后同一线自己好了
18:37:47  active    gemini-3.8-flash         ok=1   19260ms
18:40:16  curate    gemini-3.8-flash         ok=1   66541ms  ← 成功的也要 66 秒
18:42:24  curate    gemini-3.8-flash         ok=1   67696ms
```

## 结论：两边都有问题，但**主要是程序放大**

### 上游确实慢（这一半是真的）

- star 站晚上会出现 40~70 秒的正常响应；
- 记忆提炼（`memory_curate`）本来就要 60 秒以上，P90 是 19.5 秒、有 66~67 秒的成功样本；
- 偶发 429（限流）、503/504（网关抖动）。

### 但「全部掉线」是被这四件事堆出来的（这一半是程序）

**① 提炼超时把整条渠道判死**

全量数据：

```
memory_curate  17409 次调用，失败 4908 = 28.2%
passive        15494 次，失败 309  = 2.0%
active         13834 次，失败 481  = 3.5%
affection       1028 次，失败 7    = 0.7%
```

提炼的失败率是聊天的 **14 倍**。原因很直接：提炼输出长、思考链吃 token，容易撞 60s 超时；
而 `max_retries=1` 让超时变成 **60s × 2 = 120s**——正好对上日志里的 `120547ms`。

以前这份超时和聊天共用渠道健康：**提炼慢一次，聊天线路就被判死 5 分钟**。

**② `fail_threshold = 1`：一次抖动就判死**

**③ `total_timeout_seconds = 60`：比单条渠道自己的超时还小**

provider.timeout 是 45~60 秒，60 秒的总预算意味着**第一条超时就把预算吃光**，
后面几条根本没机会试——"故障转移"形同虚设，直接判成全挂。

**④ 「切兜底」那条通知路径绕过了冷却**

```python
# 以前（_escalate_to_last_resort 里）
self._notify_cb(f'🚨 歌伊的模型渠道全挂了…')   # 裸调用，不查 notify_cooldown
```

`_notify_all_down` 有 600 秒冷却，但这条路径直接发。手动"切换渠道"会清掉 `_hold_name`，
下次再挂又走这条路径 → **19:37、19:38、19:39 连发三条**。

## 改了什么

| 项 | 旧 | 新 | 理由 |
|---|---|---|---|
| 提炼失败是否判死渠道 | 计入 | **超时/连接抖动不计入**（401/403/5xx 仍计入） | 提炼 28% 失败率 vs 聊天 2% |
| `fail_threshold` | 1 | **2** | 一次抖动不判死 |
| `total_timeout_seconds` | 60 | **150** | 让 failover 真的有机会试第二条 |
| 全挂通知 | 切兜底路径裸发 | **统一走 600 秒冷却** | 一晚收到三四条太吵 |

代码位置：`providers/llm_gateway.py`

```python
BACKGROUND_KINDS = frozenset({'memory_curate'})

def _mark_failure(self, provider, state, error_code, kind=''):
    ...
    if kind in BACKGROUND_KINDS and error_code in ('timeout', 'connection_error'):
        # 后台任务抖动：只记 last_error，不动健康状态
        return
```

## 预期效果

- 提炼超时不再连累聊天 → **「全挂」次数应该明显下降**；
- 真的全挂时，一晚最多收到 **1 条**通知（600 秒冷却），不再连发；
- 单次请求最多多等 90 秒（预算 60→150），但换来的是第二条线真的会被尝试；
- 上游真挂的时候仍然会切兜底 + 锁定，行为不变。

## 还能继续调（都在 config.json 的 `failover` 里，不用改代码）

| 键 | 现在 | 说明 |
|---|---|---|
| `fail_threshold` | 2 | 想更抗抖动就 3 |
| `total_timeout_seconds` | 150 | 别超 240（调度器看门狗 300 秒） |
| `notify_cooldown_seconds` | 600 | 嫌吵可以调到 1800 |
| `probe_interval_seconds` | 300 | 判死后的探活间隔 |

⚠️ 另一个更省事的方向：**给提炼单独配一个便宜、稳定的渠道**
（`providers[].kinds` 里只写 `memory_curate` 的那条）。提炼对模型质量不敏感，
但量大、易超时，跟聊天抢同一条线不划算。

## 回滚

`config.json.bak-failover-20261107`；代码改动集中在 `_mark_failure` 与 `_notify_all_down` 两处，
把 `BACKGROUND_KINDS` 判断和 `kind` 参数去掉即可恢复。
