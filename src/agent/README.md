# Agent

本包负责一次任务的执行；app 只创建具体服务，通过 create_run 交给运行层。

## 阅读主线

```text
AgentSession.run                         session：接收消息、去重、保存输入
  → RunLauncher.execute                  agent/launcher.py：创建、恢复或保留一次运行
  → AgentRuntime.run → invoke            agent/runtime.py：启动框架执行图
  → HistoryMiddleware.awrap_model_call   agent/loop.py：一轮模型调用
      prepare_request → request_model → save_response
  → ToolSettlementMiddleware             tools/settlement.py：执行并保存工具结果
  → 下一轮模型调用，或 AgentRuntime.finish 结束
```

- `launcher.py`：运行生命周期，含 RunOptions、RuntimeContainer。只持有运行创建函数和提示服务，不查找业务服务。
- `runtime.py`：创建执行图、启动、恢复、结束。直接使用模型消息转换和工具适配函数。
- `loop.py`：`abefore_model` 只管理轮数和状态；`prepare_request` 统一决定工具与预算、按需采用摘要、接入引导消息、生成最终消息。随后 `request_model → save_response`。
- `boundaries.py`：工具补齐、筛选和延迟披露；以及流式事件与消息保存。不在这里压缩或重建历史。
- `calibration.py`：只记录实际工具和系统提示占用，不采用摘要、不修改请求消息。
- `context.py`：RunContext 提供本次运行的数据和服务引用，`history` 是唯一的会话历史存储引用。执行轮数、阶段和结束原因统一保存在 PycodeState 的 `turn`、`phase`、`reason`，`ended_turn` 防止重复发布轮次结束事件。暂停信息由框架检查点的 interrupts 保存。

没有 RuntimeBindings、消息发布转发类或结算中间件工厂。固定实现直接导入；run_hook、
context_transforms、tool_filters、before_finish 保留为确实随业务能力变化的回调。

中间件由框架按生命周期调用，不需要 app 调度。审批、同文件修改串行、工具结果去重及同步检查点仍由各自边界保证。
