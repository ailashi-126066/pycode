# Pi → pycode 对照

Pi 源码：https://github.com/earendil-works/pi/tree/2c8d1fe 。路径相对 `src/`。

## 对照表

| Pi 概念（文件 / 函数） | pycode（文件 / 函数） | 框架机制 |
|---|---|---|
| AgentMessage、内容块（`ai/src/types.ts`） | `kernel/messages.py::Message`、`TextBlock`/`ThinkingBlock`/`ToolCall` | — |
| 会话树（`coding-agent/src/core/session-manager.ts`） | `kernel/history.py::SessionEntry`、`session/store.py` | LangGraph `SqliteStore` |
| `transformContext`（`agent/src/agent.ts`） | `context/projector.py::ContextProjector.transform_context` | — |
| `declareToolChanges`（`agent-loop.ts`） | `context/projector.py::declare_tool_changes` | — |
| `convertToLlm`（`coding-agent/src/core/messages.ts`） | `kernel/messages.py::convert_to_llm` | — |
| `transformMessages`（`ai/src/api/transform-messages.ts`） | `models/messages.py::transform_messages`、`to_framework_messages` | LangChain 消息类型 |
| `AgentEvent`（`agent/src/types.ts`） | `kernel/events.py`（18 种）、`kernel/events.py::EventStream` | — |
| `runLoop`（`agent/src/agent-loop.ts`） | `agent/runtime.py::AgentRuntime`、`agent/context.py::RunState` | `create_deep_agent` 图 |
| `prepareNextTurn` | `agent/loop.py::HistoryMiddleware.abefore_model`（维护上下文、轮次预算、防打转） | middleware `before_model`，`jump_to="end"` |
| `prepareRequest` + `streamAssistantResponse` | `HistoryMiddleware.awrap_model_call` | middleware `wrap_model_call` |
| `finishTurn` | `HistoryMiddleware.aafter_model`（无工具调用的一轮）、`aafter_agent`（follow-up、任务板结束前检查） | middleware `after_model` / `after_agent`，`jump_to="model"` |
| `getSteeringMessages` 三处轮询（`agent-loop.ts` 185/214/304） | `agent/loop.py::poll_steering`：`abefore_agent`；轮末（`aafter_model` 或 tool 轮后的 `abefore_model`）；`abefore_model` 准备之后（轮末没取到时） | — |
| `executeToolCalls` | `tools/settlement.py::ToolSettlementMiddleware`（准备串行、执行并发） | tools 节点 + `wrap_tool_call` |
| `prepareToolCall` | `tools/executor.py::ToolExecutor.execute`（prepare_arguments → 类型转换 → 校验 → 授权 → 执行前钩子） | — |
| `executePreparedToolCall` 的 `acceptingUpdates` | `ToolExecutor.execute` 的 progress 闭包 | — |
| `finalizeExecutedToolCall` / `afterToolCall` | `tools/executor.py::after_tool_call` | — |
| `emitToolExecutionEnd`、`createToolResultMessage` | `ToolExecutor.emit_tool_execution_end`、`settlement.py::settle` | — |
| 审批等待 | `tools/permissions.py` 的 `interrupt`，`AgentRuntime.resume` | `interrupt` / `Command(resume)` |
| 内置工具（`coding-agent/src/core/tools/*.ts`） | `tools/builtin/{read,write,edit,grep,find,ls}.py`、`sandbox/tools.py` | Deep Agents `CompositeBackend` |
| `withFileMutationQueue` | `tools/builtin/paths.py` | — |
| 大输出截断（`truncate.ts`、`output-accumulator.ts`） | `tools/output.py::TempOutputStore` | — |
| 压缩（`core/compaction/compaction.ts`、`utils.ts`） | `context/{compaction,summary}.py`、`context/summary.py` | — |
| 用量估算（`compaction.ts` estimateContextTokens） | `context/budget.py` | — |
| Skills（`core/skills.ts`） | `skills/store.py` | Deep Agents `SkillsMiddleware` |
| 系统提示（`core/system-prompt.ts`） | `tools/discovery.py::tool_prompt`、`context/resources/system-prompt.txt` | — |
| todo 扩展（`examples/extensions/todo.ts`） | `tasks/tools.py`、`tasks/types.py`、`kernel/work_graph.py` | — |

## 被框架取代、没有翻译的 Pi 部分

- **pi-ai 的 provider 流式实现**：由 LangChain 的 `ChatOpenAI` / `ChatAnthropic` 等负责 HTTP、SSE 和工具绑定，
  pycode 从 usage_metadata/response_metadata 读取用量；DeepSeek 子类只负责 reasoning_content 回传。
- **`runLoop` 作为单个函数**：由 LangGraph 图执行，循环里的各步拆进 middleware 钩子（见上表）。
- **扩展系统 `pi.on()`**：没有移植 Pi 的扩展加载/注册系统。内置服务由 app 显式组装，工具前后钩子仅为实际共享边界；用户可配置 Hooks（`hooks/`）。
- **pi-tui**：由 Textual 取代（`ui/tui.py`），TUI 只订阅事件流。
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

1. kernel/messages.py、events.py、history.py、work_graph.py：共享事实和事件。
2. agent/runtime.py、loop.py：对照 Pi agent-loop，读执行图、模型边界与恢复。
3. tools/executor.py、settlement.py、permissions.py：工具调用和持久化顺序。
4. 任一业务模块，例如 memory/memory.py 或 tasks/tasks.py：看真实业务行为。
5. app/composition.py：看具体服务、回调与资源生命周期如何接到核心。

## 本轮验证说明

DeepSeek 真机正向/反向对照经用户豁免，未验证；离线 payload/usage 测试不能替代真机结论。
当前 LangChain Anthropic 流式适配只从最终 `message_delta` 暴露用量；若服务仅在 `message_start` 提供输入与缓存用量，pycode 保留上游缺字段结果，不补算或另建 SDK 代理。真实 SSE fixture 覆盖此边界。
本文件是源码映射，最终 Pi 对齐审计须在全量验收后另行完成，不能将这次路径更新当作审计通过。


## 2026-11-02 Pi 对齐审查与修复

独立 GPT-6.1 Sol agent 对集成 worktree（审查起点 f1267e5 加当时磁盘改动）和 Pi `2c8d1feb1` 做只读比较，依据旧 `457c8b9:11.1.md` 第 0～20 节及本轮已确认变更。审查本身没有运行真机或完整测试；下列修复由主 agent 实施并做定向回归。静态“符合”不意味着所有场景已穷尽。

| 核查项 | 结论 | 依据与边界 |
|---|---|---|
| 消息边界与 thinking 来源判断 | 符合 | 原生记录、投影、自定义 role 转换和框架转换分开；跨模型丢弃不适用 thinking |
| Anthropic 有序内容块 | 审查发现后修复 | models/messages.py 按 text/thinking/tool_use 原顺序重放；test_interleaved_provider_blocks_keep_their_order 验证原生、LangChain 和实际 wire 格式 |
| 工具准备参数 | 审查发现后修复 | executor.py 向框架 invoke 和执行后钩子传 prepared，避免审批与执行使用不同参数；真实 graph 测试验证修复参数和字符串数字转换 |
| 部分流失败后成功重试 | 审查发现后修复 | agent 在模型重试回调中保存已经开始的失败回复，并先发 message_end 再发 retry；失败回复不参与后续模型上下文 |
| 初始输入结束事件 | 审查发现后修复 | runtime 保存 append 返回的 ID，初始 user message_end 带 entry_id；不重复写入消息 |
| 审批、意图、持久化及取消 | 符合／允许增加 | 准备串行、工具并发、结果先落盘再结束事件；未执行与执行中取消区分，持久审批是用户保留功能 |
| 循环、steering、follow-up | 符合（静态核查） | 开始、轮末与长准备后轮询；follow-up 在结束边界处理 |
| 会话树与恢复 | 允许偏离 | SQLite 和 LangGraph checkpoint；利用已落盘结果防止恢复时重复执行 |
| Skills 延迟读取和作用范围 | 符合／允许增加 | 普通 read 读取说明；allowed-tools 是保留功能 |
| 压缩切割、摘要和文件跟踪 | 符合（静态核查） | 保留调用/结果配对、历史原文，摘要不加入 thinking |
| 动态提示预算 | 符合近似策略 | 无有效 usage 时传入动态 tail；有 usage 时避免重复加入上一轮已计费提示，本轮提示增长不单独精确计算，是现有近似限制 |
| 普通服务、任务板、记忆、防打转 | 允许偏离 | 用户已取消统一 Feature／Pi 扩展系统要求，保留这些内置业务能力 |
| DeepSeek thinking 真机 | 未验证，用户豁免 | 离线 payload 与用量测试不等于真机验证 |

参考 Pi：`packages/agent/src/agent-loop.ts` 的 prepared.args 执行与失败消息收尾，`packages/ai/src/api/anthropic-messages.ts` 的逐块消息转换。此次报告发现项均已落实为针对行为的回归测试；不把框架名称或文件组织差异自动认定为偏差。
