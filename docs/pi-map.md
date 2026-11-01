# Pi → pycode 对照

Pi 源码：https://github.com/earendil-works/pi/tree/2c8d1fe 。路径相对 `src/pycode/`。

## 对照表

| Pi 概念（文件 / 函数） | pycode（文件 / 函数） | 框架机制 |
|---|---|---|
| AgentMessage、内容块（`ai/src/types.ts`） | `kernel/domain/messages.py::Message`、`TextBlock`/`ThinkingBlock`/`ToolCall` | — |
| 会话树（`coding-agent/src/core/session-manager.ts`） | `kernel/domain/history.py::SessionEntry`、`session/adapters/storage/session.py` | LangGraph `SqliteStore` |
| `transformContext`（`agent/src/agent.ts`） | `context/services/context_projector.py::ContextProjector.transform_context` | — |
| `declareToolChanges`（`agent-loop.ts`） | `context_projector.py::declare_tool_changes` | — |
| `convertToLlm`（`coding-agent/src/core/messages.ts`） | `kernel/domain/messages.py::convert_to_llm` | — |
| `transformMessages`（`ai/src/api/transform-messages.ts`） | `models/adapters/messages.py::transform_messages`、`to_framework_messages` | LangChain 消息类型 |
| `AgentEvent`（`agent/src/types.ts`） | `kernel/domain/events.py`（18 种）、`kernel/services/events.py::EventStream` | — |
| `runLoop`（`agent/src/agent-loop.ts`） | `agent/adapters/agent.py::AgentRuntime`、`agent/domain/run_state.py::RunState` | `create_deep_agent` 图 |
| `prepareNextTurn` | `agent/adapters/history.py::HistoryMiddleware.abefore_model`（维护上下文、轮次预算、防打转） | middleware `before_model`，`jump_to="end"` |
| `prepareRequest` + `streamAssistantResponse` | `HistoryMiddleware.awrap_model_call` | middleware `wrap_model_call` |
| `finishTurn` | `HistoryMiddleware.aafter_model`（无工具调用的一轮）、`aafter_agent`（follow-up、任务板结束前检查） | middleware `after_model` / `after_agent`，`jump_to="model"` |
| `getSteeringMessages` 三处轮询（`agent-loop.ts` 185/214/304） | `history.py::poll_steering`：`abefore_agent`；轮末（`aafter_model` 或 tool 轮后的 `abefore_model`）；`abefore_model` 准备之后（轮末没取到时） | — |
| `executeToolCalls` | `tools/adapters/settlement.py::ToolSettlementMiddleware`（准备串行、执行并发） | tools 节点 + `wrap_tool_call` |
| `prepareToolCall` | `tools/services/tool_executor.py::ToolExecutor.execute`（prepare_arguments → 类型转换 → 校验 → `pre_tool_use` → 授权） | — |
| `executePreparedToolCall` 的 `acceptingUpdates` | `ToolExecutor.execute` 的 progress 闭包 | — |
| `finalizeExecutedToolCall` / `afterToolCall` | `settlement.py::after_tool_call` | — |
| `emitToolExecutionEnd`、`createToolResultMessage` | `ToolExecutor.emit_tool_execution_end`、`settlement.py::settle` | — |
| 审批等待 | `tools/adapters/permissions.py` 的 `interrupt`，`AgentRuntime.resume` | `interrupt` / `Command(resume)` |
| 内置工具（`coding-agent/src/core/tools/*.ts`） | `tools/adapters/builtin/{read,write,edit,grep,find,ls}.py`、`sandbox/adapters/tools/bash.py` | Deep Agents `CompositeBackend` |
| `withFileMutationQueue` | `tools/adapters/file_mutation_queue.py` | — |
| 大输出截断（`truncate.ts`、`output-accumulator.ts`） | `tools/adapters/output_store.py::TempOutputStore` | — |
| 压缩（`core/compaction/compaction.ts`、`utils.ts`） | `context/domain/{compaction,summary_source,file_ops}.py`、`context/adapters/summary.py` | — |
| 用量估算（`compaction.ts` estimateContextTokens） | `context/domain/context_meter.py` | — |
| Skills（`core/skills.ts`） | `skills/catalog.py` | Deep Agents `SkillsMiddleware` |
| 系统提示（`core/system-prompt.ts`） | `tools/adapters/discovery.py::tool_prompt`、`context/resources/system-prompt.txt` | — |
| todo 扩展（`examples/extensions/todo.ts`） | `plans/adapters/tools/task_board.py`、`context/domain/tasks.py`、`kernel/domain/work_graph.py` | — |

## 被框架取代、没有翻译的 Pi 部分

- **pi-ai 的 provider 流式实现**：由 LangChain 的 `ChatOpenAI` / `ChatAnthropic` 等负责 HTTP、SSE 和工具绑定，
  pycode 只在 `models/adapters/observation.py` 补回用量。
- **`runLoop` 作为单个函数**：由 LangGraph 图执行，循环里的各步拆进 middleware 钩子（见上表）。
- **扩展系统 `pi.on()`**：内部扩展点是 `tools/ports/extensions.py::ToolExtension`，用户可配置的是 Hooks（`hooks/`）。
- **pi-tui**：由 Textual 取代（`ui/adapters/tui.py`），TUI 只订阅事件流。
- **pi-durable**：不引入；持久化用 LangGraph checkpointer（`execution.sqlite`）加会话记录，只借用它的规则
  （先写记录再发事件、显式状态机）。

## pycode 自己加的功能

- 后台压缩：上下文到 80% 时后台生成摘要，到上限前采用；溢出时强制压缩重试。
- 旧工具结果驱逐：只改投影，原始结果留在会话里。
- MCP：远端工具并进统一的 ToolSearch 目录。
- ToolSearch 延迟加载：deferred 工具先只给名字，搜索后才给定义。
- 任务板：TaskWrite / TaskUpdate，DAG 校验、证据检查和三种出口。
- Plan 模式：只读规划，审批后计划变成执行任务板。
- Subagent：照 Claude Code 的 Agent / SendMessage / StopAgent，支持 worktree 和后台。
- 防打转：同一调用得到同样结果、文件改回旧内容，先提醒，再问用户继续还是停止。
- 长期记忆：一条一个 Markdown 文件，系统提示放索引，按需 read。
- 快照与回退：独立 Git 裸仓库记录文件版本，`/revert` 回退对话或代码。
- 审批与沙箱：Seatbelt、命令白名单、凭据过滤。
- Hooks：14 种生命周期 Hook，command 或 prompt 动作。

## 建议的阅读顺序

1. `kernel/domain/messages.py`、`events.py`、`history.py`、`state_machine.py`、`work_graph.py`
2. `agent/adapters/agent.py` 和 `agent/adapters/history.py`，对照 Pi 的 `agent-loop.ts`
3. `tools/adapters/settlement.py`、`tools/services/tool_executor.py`、`tools/adapters/builtin/`
4. `context/services/context_projector.py`、`context/adapters/summary.py`
5. 任选一个功能模块（例如 `plans/` 的任务板或 `subagents/`）
6. `app/bootstrap.py` 和 `app/composition/runtime.py`，看所有模块怎么组装
