# v2.9.17：兜底渠道与恢复探测

## 渠道策略

默认兜底渠道为官方 DeepSeek 直连接口。`failover.last_resort_provider` 可在本地运行配置中指定其他渠道；渠道清单与优先级由 `providers` 配置决定。请根据可用性、成本及服务条款选择合适的渠道。

## 兜底成功与恢复判断

软降级期间，机器人会使用兜底渠道继续响应。兜底渠道成功只代表兜底可用，不代表原候选链路已恢复。因此兜底调用成功时继续保留待恢复重试；只有非兜底渠道成功，才撤销待恢复状态。重试成功后自动切回；重试失败则按配置通知并锁定兜底渠道。

核心判断位于 `providers/llm_gateway.py` 的 `_mark_success()`，覆盖用例位于 `tests/test_gateway_failover.py::SoftHoldTest`。

## 配置与回滚

配置示例：

```json
{
  "failover": {
    "last_resort_provider": "official-chat"
  }
}
```

将 `official-chat` 替换成 `providers` 中定义的渠道名称即可。该配置在插件启动时加载，修改后需按宿主流程重载插件。
