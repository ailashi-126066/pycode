# Kernel

共享的原生类型、纯函数和工具接入契约。kernel 不 import 其他 pycode 包，也不 import 框架；
其他包只从 `pycode.kernel` 包根导入。

- `runtime.py`：`SessionScope`/`RunScope` 范围数据，不查找或管理服务。
- `messages.py`：Message、ToolCall、ToolResult、UsageRecord、`convert_to_llm`。
- `events.py`：事件类型、`RunResult`、`EventConsumer`（`on_event`）、`EventStream`、`MemoryEventJournal`。
- `history.py`：SessionEntry/SessionSnapshot、`SessionTree`、`ProjectionContribution`。
- `tools.py`：ToolSpec、ToolContext、`AgentTool`、`ToolGate`、异步工具钩子和结果类型、`CommandRunner`。
- `work_graph.py`：任务板（Board/Step、转移表、validate/apply/derived）和通用 `StateMachine`。
- `errors.py`：DomainError 及各边界错误。
- `values.py`：冻结/复制/序列化、`data_hash`、`SECRET_PATTERNS`、模型身份。
- `text.py`：按行/字节截断、token 估算。
- `io.py`：kernel 里唯一做 I/O 的文件：原子写、文件锁、`SystemTiming`、保留目录名判断。

Skill/MCP 规则、模型展示、消息计费和压缩边界属于对应功能，不放进 kernel。
