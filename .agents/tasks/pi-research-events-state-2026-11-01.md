# Pi（earendil-works/pi）事件体系、工具生命周期与任务状态调查（对照 pycode）

只读调查。Pi 仓库克隆在 `/tmp/pi-research`（已保留），未执行其中任何脚本或安装命令。pycode 侧未修改任何文件，未读取 `.env`。

版本标记：
- **基线 42a3497** = `42a3497d03ad17e308a2299fa824727894f2c0ec`（2026-10-09，包版本 1.1.0），即 `docs/source-map.md:11` 记录的借鉴提交。
- **HEAD 2c8d1fe** = `2c8d1feb147f6b3d97e855da803bfe7b19f3c403`（2026-11-01，包版本 1.4.0），与基线相差 499 个提交。

文中 Pi 路径都相对于仓库根目录。若两个提交的行号不同，写成“42a3497 行号 / HEAD 行号”；只写一个行号时表示两个提交相同。pycode 路径相对于 `src/pycode/`。

---

## 0. 结论摘要

1. **Pi 是什么。** Pi 是一个 TypeScript monorepo，自称“极简、可扩展的 agent harness”。它明确不内置 sub-agent 和 plan mode，要求用户用扩展自行实现（`packages/coding-agent/README.md:15-19`，两个提交措辞相同）。核心循环 `packages/agent` 只有约 2500 行，唯一运行时依赖是 `pi-ai` 和 `typebox`。权限审批、todo 和计划这些功能都不在核心里。
2. **事件体系。** Pi 有三层事件，全部是以 `type` 为判别字段的 TypeScript 联合类型，每种事件的字段都有定义：
   - 核心 `AgentEvent`：**10 种**（`packages/agent/src/types.ts:516-537`）；
   - coding-agent 的 `AgentSessionEvent`：**23 种**（`packages/coding-agent/src/core/agent-session.ts:196/209`）；
   - 扩展事件 `ExtensionEvent`：**41 种**（`packages/coding-agent/src/core/extensions/types.ts:1377/1425`）。Pi 的“Hook”就是扩展事件，所有处理器都通过同一个 `pi.on()` 注册，有的事件只做通知，有的可以拦截或改写。
   - 会话记录 `SessionEntry`：**11 种**，另有一个文件头（`packages/coding-agent/src/core/session-manager.ts:183-194`）。

   从基线到 HEAD，这几层定义几乎没变，只有 `before_agent_start` 被拆成 user 和 custom 两个变体（提交 a447ba11f）。pycode 则是 25 种运行时事件、15 种 Hook、约 30 种记录类型，事件的 `data` 是无模式的 `dict`。
3. **工具生命周期。** Pi 核心没有显式的状态枚举，内部只有 4 个阶段类型（prepared / immediate / executed / finalized）和内存中的 `pendingToolCalls: Set`。它保证的是**一个统一的结算出口**：凡是发出了 `tool_execution_start` 的调用，无论是未知工具、参数无效、被拦截、被中止还是抛异常，都会恰好收到一次 `tool_execution_end`，随后写入一条 toolResult 消息（HEAD 文档 `packages/coding-agent/docs/extensions.md:357`）。核心没有审批：`docs/security.md:3` 明确说不会在每次工具调用前询问。显式工具状态机只存在于实验包 `pi-durable`：每个工具调用是一个持久化 Task，有 `call → execute` 两个 checkpoint 阶段，执行前先写入“意图”，并区分 `abandoned`（未开始）、`interrupted`（可能已部分执行）、`aborted`、`restart_limit` 四种结局（`packages/durable/src/harness/tool.ts:84-87, 135-247`）。
4. **Agent 状态。** coding-agent 用一组布尔量和 AbortController 表示运行状态（`isStreaming`、`isCompacting`、`isRetrying` 等），没有状态机。steering 和 follow-up 是两条内存队列，支持 `QueueMode`。压缩在 `prepareNextTurn` 中按阈值触发。会话是 JSONL 文件里由 `id/parentId` 组成的树。`pi-durable` 则有显式的 `TaskState`（6 种 status）和 `TaskOutcome`（5 种），并把运行状态作为持久化文档 `pi.live` 保存。
5. **任务状态板 / 防跑偏。** Pi 没有内置 todo、plan 或任务状态板，README 把 plan mode 列为刻意不做的功能。两个示例扩展给出了两种做法：
   - `examples/extensions/todo.ts` 用**工具调用**更新状态，状态存在 toolResult 的 `details` 里，按当前分支重建；
   - `examples/extensions/plan-mode/` 用**宽松的文本约定**（`Plan:` 编号列表加 `[DONE:n]` 标签），在 `turn_end` 时解析。格式不对时只是不更新，**从不让运行失败**。

   `pi-durable` 中的 “Task” 是执行层面的状态机，不是给模型用的任务板。它的防失控手段是 `maxTurns` 上限、`onYield` 续跑钩子和 `runFailure` 原因枚举。
6. **对 pycode 的建议。**
   - (A) 值得照搬：可辨识联合的事件类型、“每个 start 恰好一个 end”的单一结算出口、durable 的“意图先落盘 + 未开始/已开始两种中断结局”。可以回退简化的部分：把 25 种事件收敛到 12 种左右；把 `tool_action_*` 和 `tool_output_ready` 并入 `tool_execution_*` 的字段；把大部分自定义 entry 类型收进 `custom`。
   - (B) pycode 未提交的“强制 JSON 任务输出契约”（`agent/adapters/task_output.py`）比 Pi 的任何做法都严格，代价也更高：格式错误会让整个 run 失败、已发出的工具调用被丢弃（`tests/agent/adapters/test_task_context.py:110-122`）；它与原生工具调用和流式输出互相牵制；每轮回复都要加 JSON 外壳。**建议改为 Pi `todo.ts` 那样的专用工具**（例如 `task_update`），保留 pycode 自己的证据校验（`context/domain/tasks.py` `validate_task_action`）。校验失败时返回工具错误结果让模型重试，而不是让 run 失败。再加上 durable 式的 `maxTurns` 和有上限的结束前续跑提醒。

---

## 1. Pi 是什么

### 1.1 仓库结构

HEAD 2c8d1fe 的 `packages/` 下有 12 个包（版本均为 1.4.0）：

| 包 | npm 名 | 职责（取自 package.json 的 description 和源码） |
|---|---|---|
| `ai` | `@earendil-works/pi-ai` | 统一的 LLM API 与 provider 层，负责流式事件、tool call 参数流和消息转换（`src/api/transform-messages.ts`） |
| `agent` | `@earendil-works/pi-agent-core` | 通用 agent 核心：`agent-loop.ts`（959 行）、`agent.ts`（623 行）、`types.ts`（539 行） |
| `coding-agent` | `@earendil-works/pi-coding-agent` | CLI 产品：read/bash/edit/write 工具、`AgentSession`、`SessionManager`（JSONL 树）、扩展系统、压缩、TUI/RPC/print 模式 |
| `tui` | `@earendil-works/pi-tui` | 差分渲染的终端 UI 库 |
| `durable` | `@earendil-works/pi-durable` | **实验性**的持久化 harness，把对话、任务和文档存入 SQLite/JSONL，可从崩溃中恢复。coding-agent **不依赖**它（`packages/coding-agent/package.json` 的 dependencies 里没有） |
| `chord` | `@earendil-works/chord` | 类型化宿主运行时：插件、契约、复制状态、JSON RPC。durable 用它做文档状态 |
| `codemode` | `pi-codemode` | QuickJS 沙箱，脚本只能调用注入的工具 |
| `mcp` | `pi-mcp` | 独立的 MCP 客户端 |
| `env` | `pi-env` | durable 的远程执行环境（SSH 守护进程） |
| `schema` | `pi-schema` | 不使用 eval 的 JSON Schema 校验。**基线之后新增**（fab2c5353） |
| `telemetry` | `pi-telemetry` | 遥测契约 |
| `evals` | `pi-evals` | 评测 |

基线 42a3497 时还有 `client`、`protocol`、`server` 三个包（远程会话用的 CBOR 协议和实验服务端），它们在提交 7472cda0d 中被整体删除。这也是 coding-agent 从基线到 HEAD 的 diff 为 `+3441 / −12379` 行的主要原因。

### 1.2 语言与依赖
- TypeScript（ESM），Node.js ≥ 22.19（`packages/coding-agent/README.md`，Getting started 一节）。
- `pi-agent-core` 的依赖只有 `@earendil-works/pi-ai` 和 `typebox`。`pi-coding-agent` 依赖 agent-core、ai、codemode、mcp、tui，外加 jiti（运行时加载 TS 扩展）、photon-node、quickjs-wasi、proper-lockfile 等。

### 1.3 设计哲学
- `packages/coding-agent/README.md:15`：“Pi is a minimal, extensible agent harness that you can make your own.”
- `packages/coding-agent/README.md:19`：自带强默认，但跳过 sub-agent 和 plan mode 这类功能，用户可以让 Pi 自己写一个，或安装第三方包（两个提交措辞相同）。
- 权限：`packages/coding-agent/docs/how-pi-works.md` 的 “Trust and permissions” 一节和 `docs/security.md:3` 说明，工具以 Pi 进程的操作系统权限运行，**不会在每次工具调用前请求批准**；需要隔离就用容器或虚拟机。确认提示只作为示例扩展提供（`examples/extensions/permission-gate.ts:13-22`，通过 `tool_call` 事件 `block`）。
- 扩展是进程内 TS 模块，用 `pi.on(...)`、`pi.registerTool(...)`、`pi.appendEntry(...)` 扩展行为（`docs/extensions.md`；HEAD 版本 3335 行，基线版本只有 274 行精简稿，提交 85c2ee3e7 恢复了完整指南。代码语义没有变化）。

---

## 2. 事件体系

### 2.1 核心 AgentEvent（10 种，可辨识联合）

`packages/agent/src/types.ts:516-537`（两个提交相同）：

| type | 字段 |
|---|---|
| `agent_start` | — |
| `agent_end` | `messages: AgentMessage[]` |
| `turn_start` | — |
| `turn_end` | `message`, `toolResults: ToolResultMessage[]` |
| `message_start` | `message` |
| `message_update` | `message`, `assistantMessageEvent`（pi-ai 的 text/thinking/toolcall 的 start/delta/end） |
| `message_end` | `message` |
| `tool_execution_start` | `toolCallId`, `toolName`, `args` |
| `tool_execution_update` | `toolCallId`, `toolName`, `args`, `partialResult` |
| `tool_execution_end` | `toolCallId`, `toolName`, `result`, `isError`, `durationMs?` |

要点：
- 错误不是单独的事件。失败的回合是一条 `stopReason: "error" | "aborted"` 的 assistant 消息，带 `errorMessage`。`Agent.handleRunFailure` 在异常时补发 `message_start/end → turn_end → agent_end`（`packages/agent/src/agent.ts:532/542`）。
- usage 也没有单独事件，它挂在 assistant 消息上。
- 流式增量只有一种事件 `message_update`，内部用 `assistantMessageEvent.type` 区分 text、thinking 和 toolcall。
- system、user、toolResult 消息也发 `message_start/end`（`agent-loop.ts` `emitToolResultMessage`）。

### 2.2 coding-agent 的 AgentSessionEvent（23 种）

`packages/coding-agent/src/core/agent-session.ts:196-238 / 209-251`（两个提交内容相同）：
- 核心 10 种中的 9 种（排除 `agent_end`），tool 事件额外带 `parentToolCallId?`（嵌套调用时设置）。
- 重定义的 `agent_end { messages, willRetry }`。
- 新增 13 种：`agent_settled{aborted}`、`queue_update{steering, followUp}`、`compaction_start{reason}`、`compaction_end{reason, result, aborted, willRetry, errorMessage?}`、`entry_appended{entry}`、`session_info_changed{name}`、`thinking_level_changed{level}`、`auto_retry_start{attempt, maxAttempts, delayMs, errorMessage}`、`auto_retry_end{success, attempt, finalError?}`、`summarization_retry_scheduled`、`summarization_retry_attempt_start`（compaction 和 branchSummary 两个变体）、`summarization_retry_finished`、`bash_execution_update{id?, delta}`。

JSON 模式（`pi --mode json`）把这些事件逐行写成 JSONL（`docs/how-pi-works.md` 的 Interfaces 一节）。

### 2.3 扩展 / Hook 事件（41 种）

`packages/coding-agent/src/core/extensions/types.ts`：`ExtensionEvent` 在 1377 / 1425 行，`SessionEvent` 在 848 / 882 行。每个事件都有独立的 interface。按类别：

| 类别 | 事件 | 作用 |
|---|---|---|
| 启动/资源（3） | `project_trust`、`resources_discover`、`mcp_servers_change` | 可返回决定 |
| 会话（10） | `session_start{reason}`、`session_info_changed`、`session_before_switch`、`session_before_fork`、`session_before_compact`、`session_compact`、`session_compact_failed`、`session_shutdown{reason}`、`session_before_tree`、`session_tree` | `before_*` 可取消或自定义 |
| 请求（7） | `context`、`context_with_system`、`cache_warming_decision`、`before_provider_request`、`before_provider_headers`、`after_provider_response`、`provider_stream_event` | 改写消息、payload 或 headers |
| 运行（5） | `before_agent_start`（HEAD 拆成 user/custom 变体）、`agent_start`、`agent_end`、`agent_before_settle`、`agent_settled` | `agent_before_settle` 可以追加 entry 并请求一次续跑 |
| UI（2） | `ui_prompt_start`、`ui_prompt_end` | 通知 |
| 回合/消息（5） | `turn_start{turnIndex}`、`turn_end{…, messageEntryId, toolResultEntryIds}`、`message_start`、`message_update`、`message_end` | `turn_end` 可以追加 entry 并请求续跑；`message_end` 可做链式变换 |
| 工具（5） | `tool_execution_start/update/end`、`tool_call`（门控：可 `block` 或原地改 `input`）、`tool_result`（变换：可替换 content/details/isError） | `tool_call` 和 `tool_result` 按工具名细分为可窄化的联合（`ToolCallEvent` 在 1228 / 1276 行） |
| 其他（4） | `model_select`、`thinking_level_select`、`user_bash`（可接管）、`input`（可 transform 或 handled） | — |

处理器的组合规则（HEAD `docs/extensions.md:398-413`）分为：通知、原地修改、链式变换、门控、可操作边界、最后覆盖。其中“门控处理器在无法做出安全判断时应 fail closed”（`:413`）。实现上，`tool_call` 处理器抛出的异常会被 `prepareToolCall` 的 catch 捕获，变成错误结果，工具不执行（`agent-session.ts:658/674` `_beforeToolCall` → `agent-loop.ts:708/718`）。

### 2.4 会话持久化记录（SessionEntry，11 种 + 文件头）

`packages/coding-agent/src/core/session-manager.ts:43-194`（两个提交的定义相同；HEAD 的 `_appendEntry` 增加了写盘失败时回滚内存的逻辑）：

`SessionHeader{type:"session", version:3, id, cwd, parentSession?}`，以及 `message{message}`、`thinking_level_change`、`model_change`、`usage{kind,…}`、`compaction{summary, firstKeptEntryId, tokensBefore, details?, fromHook?, systemMessage?}`、`branch_summary{fromId, summary}`、`custom{customType, data}`（**不进入**模型上下文，用于扩展状态）、`custom_message{customType, content, display}`（**进入**上下文）、`context_edit{targetId, replacement|null}`、`label`、`session_info{name}`。

每条记录都有 `id`、`parentId` 和 `timestamp`，构成树。system prompt 和工具集变化不是单独的记录类型，而是 `message` 中 `role:"system"` 的消息加上 `toolsAdded/toolsRemoved`（`docs/session-format.md:83-84`；`agent-loop.ts` `declareToolChanges`）。

### 2.5 三者的关系

- 核心 `AgentEvent` 是唯一的事实来源。`AgentSession` 订阅它，在 `message_end` 时把消息持久化为 `message` entry，然后发 `entry_appended`。`turn_end` 扩展事件携带 `messageEntryId` 和 `toolResultEntryIds`（`extensions/types.ts:1037/1085`），把事件和记录关联起来。
- 扩展事件 = 核心事件的同名转发 + 会话/请求/工具的门控和变换钩子。Pi 没有两套独立的 hook 名称，`tool_call` 和 `tool_result` 由核心的 `beforeToolCall` / `afterToolCall` 回调驱动（`agent-session.ts:652-720 / 668-736`）。
- 持久化只记录“结果事实”（消息、模型变化、压缩、自定义状态）。运行过程（run start/end、等待、重试）不进入 coding-agent 的 JSONL。中断后重开会话时，没有结果的 tool call 由 pi-ai 在**发请求时**补一条合成结果 “No result provided”，这条结果**不落盘**（`packages/ai/src/api/transform-messages.ts:158-223 / 179-244`）。

### 2.6 基线到 HEAD 的变化

- 核心 `AgentEvent`、`AgentSessionEvent`、`SessionEntry`：无变化。`packages/agent/src` 只有 32 行 diff，内容是 client identity 和 tool call 参数流解析。
- 扩展事件：`BeforeAgentStartEvent` 拆成 `messageType:"user" | "custom"` 两个变体（a447ba11f，2026-10-12）；新增 `reachesTools` 和 `KeybindingHints` 等非事件 API。
- **pi-durable 删除了事件流**（f3255de76，2026-10-27）：去掉了 `watchEvents()` 和 `AgentEvent/SnapshotEvent/ToolEventCall`，改为“渲染器渲染状态而不是从事件拼装状态”。理由是渲染所需的信息都已在类型化的会话视图里，新信息应该作为视图字段加入，而不是新增一种每个客户端都要学习的事件类型（`packages/durable/CHANGELOG.md:55`）。这是基线之后最值得 pycode 注意的方向变化。

### 2.7 与 pycode 逐项对照

**运行时事件**（pycode `kernel/domain/events.py:8-34`，文件头注明来源是 Pi `packages/agent/src/types.ts`@42a3497）：

| pycode EventKind | Pi 对应 | 来源判断 |
|---|---|---|
| agent_start / agent_end | 同名 | 继承 |
| turn_start / turn_end | 同名 | 继承 |
| message_start / message_end | 同名 | 继承（pycode 对初始输入和持久化消息各发一次，见既有报告 §1.1） |
| message_delta / thinking_delta | `message_update`（一种事件，内部按子类型区分） | **拆分**：pycode 改名并拆成两种 |
| tool_execution_start / update / end | 同名 | 继承 |
| retry | `auto_retry_start/end`（AgentSessionEvent） | 近似继承，合并为一种 |
| summary | `compaction_start/end` | 近似继承 |
| model_change | `thinking_level_changed` / 扩展 `model_select` | 近似继承 |
| tool_action_start / tool_action_end | 无 | **pycode 自加** |
| tool_output_ready | 无（大输出截断在工具内部完成，结果放在 `details`） | **pycode 自加** |
| model_request_start / model_request_end | 无（扩展里有 `before_provider_request` / `after_provider_response`，但不进事件流） | **pycode 自加** |
| usage | 无（挂在 assistant 消息上） | **pycode 自加** |
| needs_input | 无（Pi 核心没有审批中断） | **pycode 自加** |
| notification（3 个子类） | 无 | **pycode 自加** |
| error | 无（错误是 stopReason=error 的消息） | **pycode 自加** |
| result | 近似 `agent_settled` | **pycode 自加**（语义接近） |
| team | 无（Pi 没有团队功能） | **pycode 自加** |

结论：25 种中，9 种直接继承，2 种由 `message_update` 拆分而来，3 种近似对应 AgentSessionEvent，11 种是 pycode 自己加的。Pi 的每种事件都有字段类型；pycode 的 `data: dict` 没有模式（`kernel/domain/events.py:37-44`）。

**Hook 事件**：pycode 的 15 种 Hook（`hooks/types.py:16-34`）来自 MewCode（文件头注明 MewCode `hooks/models.py`），属于 Claude Code 风格的配置化 command/prompt Hook，**不是 Pi 的模型**。大致对应关系：`pre_tool_use` ≈ `tool_call`，`post_tool_use` ≈ `tool_result`，`pre_send` ≈ `context`/`before_provider_request`，`post_receive` ≈ `message_end`，`compact` ≈ `session_before_compact`，`session_start/end` ≈ `session_start/shutdown`，`turn_start/end` 同名。`permission_request`、`command_execute`、`file_change`、`error` 在 Pi 中没有对应。

**持久化记录**：pycode `kernel/domain/history.py` 的文件头注明来源是 Pi `session-manager.ts`。继承的类型有 `message`、`model_change`、`thinking_level_change`、`compaction`、`branch_summary`、`custom`、`custom_message`、`context_edit`，`session_metadata` 约等于 `session_info`。pycode 自加约 20 种：`input_queued/expired`、`system_prompt`（Pi 用 system 消息）、`tool_set_change`（Pi 用 `toolsAdded/Removed`）、`mcp_catalog`、`skill_*`、`memory_*`、`plan_*`、`hook_*`、`team_*`、`fork_context`、`session_deleted/restored`，以及 `custom.data.kind` 下约 15 种（`run_start/resume/wait/end`、`tool_started`、`file_snapshot` 等）。

---

## 3. 工具生命周期

### 3.1 Pi 核心（`packages/agent/src/agent-loop.ts`）

**调度**（`executeToolCalls` 在 508 / 518 行）：如果 `config.toolExecution === "sequential"`，或者本批中任一工具声明了 `executionMode: "sequential"`（`types.ts:498`），整批串行；否则并行。默认是并行（`agent.ts` 构造函数，`toolExecution ?? "parallel"`）。

**串行**（530 / 540 行）：对每个 call 依次执行 `start → prepareToolCall → execute → finalize → end → toolResult 消息`。如果 `signal.aborted`，在当前调用之后 `break`，后面的调用不会发 start、end，也没有结果消息。

**并行**（586 / 596 行）：
1. 逐个 call **串行地**发 `tool_execution_start`，并执行 `prepareToolCall`（查找工具、`prepareArguments`、schema 校验、`beforeToolCall`）。
2. `immediate` 结果（未知工具、参数无效、被拦截、被中止）立即发 `tool_execution_end`。
3. 准备好的调用包装成闭包，再用 `Promise.all` 并发执行（646 / 656 行）。执行前如果已经中止，返回 "Operation aborted" 并发 end。
4. 每个调用完成后立即发 `tool_execution_end`，因此 **end 按完成顺序发出**。
5. 最后**按 assistant 中的原始顺序**发 toolResult 的 `message_start/end`（`types.ts:40-45` 注释也写明了这一点）。

**错误处理**：
- `executePreparedToolCall`（821 / 831 行）捕获 `execute()` 抛出的任何异常，转成 `isError` 结果。工具也可以返回 `isError: true`。
- `afterToolCall` 抛异常时同样转成错误结果（`finalizeExecutedToolCall`，859 / 869 行）。
- 回调契约要求 `convertToLlm`、`getSteeringMessages` 等**不能抛异常**（`types.ts` AgentLoopConfig 各字段的注释）。如果真的抛了，只能走 `Agent.handleRunFailure`。

**前后钩子**：
- `beforeToolCall(ctx) → {block, reason, terminate}`（`types.ts:66-77, 326`）。
- `afterToolCall(ctx) → {content, details, isError, usage, terminate, structuredContent}`，按字段整体替换，不做深合并（`types.ts:92-105, 341`）。
- 如果本批所有结果都设置了 `terminate`，run 结束（`shouldTerminateToolBatch`，690 / 700 行）。

**截断保护**：`stopReason === "length"` 时，本条消息的所有 tool call 都不执行，而是返回错误结果，同样发 start 和 end（`failToolCallsFromTruncatedMessage`，478 / 488 行）。pycode 的 `tools/domain/turn_guard.py` 是同样的做法。

**审批/权限**：核心没有。coding-agent 也没有内置审批，只能由扩展在 `tool_call` 中 `block`（`permission-gate.ts`）。因此 Pi 中**没有“等待审批”这种中间状态**，也没有图中断后重新进入执行的情况。

**是否有显式状态机**：没有。运行期只有：
- `AgentState.pendingToolCalls: ReadonlySet<string>`，由 `processEvents` 根据 start 和 end 增删（`agent.ts:565-600 / 575-610`）；
- 循环内部的阶段类型 `PreparedToolCall | ImmediateToolCallOutcome → ExecutedToolCallOutcome → FinalizedToolCallOutcome`（`agent-loop.ts` 中 `PreparedToolCall` 等 type 定义，位于 `shouldTerminateToolBatch` 之前）。这些类型不对外暴露，也不持久化。

**start/end 是否成对**：
- 保证的部分：只要发了 `tool_execution_start`，在正常、错误、拦截、中止各条路径上都会**恰好一次** `tool_execution_end`。所有结局都经过同一个函数 `emitToolExecutionEnd`（919 / 929 行），再经过 `createToolResultMessage`（930 / 940 行）。HEAD 文档 `docs/extensions.md:357` 写明：不执行的调用会提前结束，但每个调用仍然会收到带错误结果的 `tool_execution_end`，应该用这个事件观察每个已结算的调用。
- 有缺口的部分：
  1. 串行模式中止后，剩余调用没有 start、end 和结果，靠 pi-ai 在请求时补 “No result provided”（`transform-messages.ts:179-244`），且不落盘；
  2. 监听器或回调违反契约抛异常时，`handleRunFailure` 不会为进行中的工具补 end，只在 `finishRun` 中清空 `pendingToolCalls`（`agent.ts:550-556 / 560-566`）。

**嵌套调用**：`runToolCall`（811 / 821 行）走同样的 prepare/before/execute/after 流程，但不发事件。coding-agent 的 `ctx.executeTool()` 为嵌套调用发带 `parentToolCallId` 的事件，`toolCallId` 为 `<parent>/<n>`（HEAD `docs/extensions.md:148`）。

### 3.2 pi-durable 的显式工具状态机（实验包）

- `packages/durable/README.md:277`（HEAD）：每个工具调用是一个独立的持久化 Task；`execute()` 运行之前先提交“意图”；进程中途死掉时，只有声明了 `replay: "safe"` 的工具会在重开时重跑，其他工具给模型一个带已提交输出的 `interrupted` 错误结果；重启被切断达到 `restartLimit` 次后，返回 `restart_limit` 错误结果。
- checkpoint 类型：`{phase:"call"} | {phase:"execute"; arguments; replay:"safe"|"unsafe"}`（`packages/durable/src/harness/tool.ts:84-87`；基线在 65-67 行）。
- `ToolTask`（`tool.ts:135-247`）：
  - `call` 阶段在同一个处理器里依次完成：解析工具 → 校验 → `beforeTool` → 再次校验 → **提交意图**（`status:"running", checkpoint:{phase:"execute"}`）→ 执行 → `afterTool` → 结算。注释说明这样做是为了让解析和结算之间没有任何间隙。
  - `execute` 阶段**只在恢复时进入**：如果存储的和当前的 replay 策略都是 safe，就重跑；否则结算为 `interrupted`，消息是 “may have partially run”。
  - `abort` 处理器区分四种结局：`tool_unavailable`；`aborted`（用户中止）；`interrupted`（重启时已处于 execute 阶段）；`abandoned`（`"was not started: its caller ended with a restart"`，`tool.ts:229-238`）。
- 通用 Task 状态（`packages/durable/src/types.ts:621-702`）：
  - `TaskState.status ∈ {pending, running, waiting(on, policy), sleeping(until), completing(outcome), terminal(outcome)}`；
  - `TaskOutcome.status ∈ {completed, failed, aborted, orphaned, faulted}`。
- UI 用的槽位状态：`pi.live` 文档中 tool slot 的 `status: "pending" | "running" | "done"`（`packages/durable/src/harness/live.ts:23`）。
- 钩子：`hook(ToolTask, { beforeTool, afterTool })`，`beforeTool` 返回 `{block}` 即可做审批门（`README.md:235, 646-700`）。

这套语义在基线 42a3497 已经存在（基线 `tool.ts` 中有同样的 `call/execute` 阶段、replay 规则和 `abandoned/interrupted` 区分）。HEAD 的变化是：`ToolTask` 升到 version 3，加入了嵌套调用文档 `NestedCallDoc` 和 `restart_limit` 切断逻辑。

---

## 4. Agent 循环与状态

### 4.1 循环结构

`runLoop`（`agent-loop.ts:163 / 172`）是两层 while：
- 内层：只要还有工具调用或 pending 消息就继续。每一轮依次执行：`prepareNextTurn`（压缩在这一步发生）→ 补一次 steering 轮询 → `turn_start` → 注入 pending 消息 → `prepareRequest` → 流式 assistant → 遇到 error/aborted 直接退出 → 执行工具 → `finishTurn` → `turn_end` → 轮询 steering。
- 外层：内层结束后，如果 `getFollowUpMessages()` 返回非空就继续（301 / 310 行）；如果 `finishTurn` 返回 `{action:"continue"}`，再补一轮只用当前上下文的请求；否则发 `agent_end`。

### 4.2 运行状态的表示

- 核心 `AgentState`（`types.ts:382-421`）：`isStreaming: boolean`、`streamingMessage?`、`pendingToolCalls: Set`、`errorMessage?`；`Agent.activeRun?: {promise, abortController}`。`prompt()` 在运行中被调用时直接抛错，提示改用 `steer()` 或 `followUp()`（`agent.ts:376 / 385`）。
- coding-agent `AgentSession` 在此之上叠加了一组字段（`agent-session.ts:395-440`，HEAD）：`_isAgentRunActive`、`_agentRunAbortRequested`、`_compactionAbortController`、`_autoCompactionAbortController`、`_retryAbortController`、`_branchSummaryAbortController`、`_overflowRecoveryAttempted`；对外暴露 `isStreaming`（1460 / 1495 行）、`isIdle`、`isCompacting`、`isRetrying`（HEAD 3893 行）。**全部是布尔量或 AbortController 是否存在，没有状态枚举。**
- 运行的可观察阶段靠事件序列表达：`agent_start → … → agent_end{willRetry} → (重试/压缩/队列) → agent_before_settle → agent_settled`（HEAD `docs/extensions.md:300-325`；`agent_end` 不代表最终空闲，要用 `agent_settled`）。
- durable 的做法不同：运行状态写进持久化文档 `pi.live`（`run`、`run.turns`、`generation`、`tools`、`compactions`、`runFailure`），失败原因是一个枚举：`model_error | no_model | aborted | reset | interrupted | faulted | orphaned | restart_limit | turn_limit | nothing_to_answer`（`packages/durable/README.md:563`，HEAD，由 f3255de76 引入）。

### 4.3 steering / follow-up 队列

- `PendingMessageQueue`（`agent.ts:143 / 149`），`QueueMode = "all" | "one-at-a-time"`（`types.ts:55`），默认是 one-at-a-time。`steer()` 和 `followUp()` 分别在 299/308 行和 304/313 行。
- 时机：steering 在当前 assistant 回合的工具全部执行完之后注入，不会跳过本回合的工具调用；follow-up 在 agent 本该停止时注入（`types.ts:283-306`）。
- coding-agent 另外维护 `_steeringMessages/_followUpMessages` 文本副本供 UI 显示，并发 `queue_update` 事件；中止时把队列消息退回编辑器（`docs/how-pi-works.md` 的 Agent loop 一节）。
- pycode 的 `session/services/input_queue.py`、`durable_inputs.py`、`session/domain/inputs.py` 都注明来源是 Pi `agent.ts`，并改为持久化队列。

### 4.4 上下文压缩

- 阈值：`contextTokens > contextWindow - reserveTokens`，`reserveTokens` 默认 16384。多回合运行中，在工具结果追加之后、下一次 assistant 请求之前，于 `prepareNextTurn` 中压缩；provider 上下文溢出或过早的 `length` 停止会触发一次“压缩后重试”（HEAD `docs/compaction.md` 的 “When It Triggers” 一节）。
- 记录：`compaction{summary, firstKeptEntryId, tokensBefore}`；扩展事件 `session_before_compact` 可以取消或替换摘要。

### 4.5 会话分支

- JSONL 文件内用 `id/parentId` 构成树，当前 leaf 决定活动分支（`docs/session-format.md:3`；`docs/how-pi-works.md` 的 Sessions 一节）。
- `/tree` 切换分支时可以生成 `branch_summary`；`/fork` 和 `/clone` 把历史复制到新文件，文件头带 `parentSession`。
- 扩展状态必须**从当前分支 `getBranch()` 重建**，不能读全部 entry（HEAD `docs/extensions.md:2112-2146`）。

---

## 5. 任务 / 防跑偏机制

### 5.1 核心与 coding-agent：没有内置功能

- 核心循环和 coding-agent 中没有 todo、plan 或 task 的状态和工具；`grep maxTurns` 在 `packages/coding-agent/src` 中没有结果，即**没有回合上限**。
- README 的说明只有一句：跳过 plan mode 和 sub-agent，交给用户用扩展或第三方包实现（`packages/coding-agent/README.md:19`）。文档没有逐条论证为什么不做 todo。`docs/extensions.md:28`（HEAD）把 “Stateful tools (todo lists, connection pools)” 列为扩展的典型用途。

### 5.2 示例扩展

**`examples/extensions/todo.ts`（工具调用模式）**
- 文件头注释（1-11 行）：注册 `todo` 工具供 LLM 管理待办，注册 `/todos` 命令供用户查看；状态存放在工具结果的 details 里，而不是外部文件，因此分支时 todo 状态自动与该历史点一致。
- 参数：`action ∈ {list, add, toggle, clear}`，可选 `text` 和 `id`；每次返回完整快照 `details: {action, todos, nextId, error?}`。
- 重建：`reconstructState`（114-130 行）遍历 `getBranch()` 中 `toolName === "todo"` 的 toolResult，取最后一条的 details；在 `session_start` 和 `session_tree` 时重建（132-133 行）。
- 错误处理：参数缺失时返回正常结果，text 写 `"Error: …"`，details 带 `error`。**不抛异常，不影响 run。**
- 基线到 HEAD 的唯一变化：`renderResult` 增加了 default 分支。

**`examples/extensions/plan-mode/`（文本约定模式）**
- `index.ts:1-13`（HEAD）描述功能：只读探索；从 “Plan:” 小节提取编号步骤；执行时用 `[DONE:n]` 标记完成；进度 widget。
- 状态：内存中的 `planModeEnabled / executionMode / todoItems`，通过 `pi.appendEntry("plan-mode", {...})` 持久化为 `custom` entry（116-123 行）；`session_start` 时从最后一条 `plan-mode` entry 恢复，并只扫描最后一个 `plan-mode-execute` 之后的 assistant 文本来重建完成状态（344-392 行）。
- 门控：plan 模式下用 `tool_call` 拦截不在 allowlist 中的 bash 命令（164-174 行，`utils.ts:97` `isSafeCommand`）；用 `setActiveTools` 去掉 edit/write。
- 注入：`before_agent_start` 注入 `[PLAN MODE ACTIVE]` 指令，或在执行模式下注入“剩余步骤，完成后写 `[DONE:n]`”（203-251 行；HEAD 增加了 `messageType !== "user"` 时跳过，避免后台通知重复注入）；`context` 事件在离开 plan 模式后过滤掉旧的 plan 上下文（177-200 行）。
- 更新：`turn_end` 时用正则 `\[DONE:(\d+)\]` 解析 assistant 文本（254-264 行；`utils.ts:152-168`）；`agent_end` 时从最后一条 assistant 消息提取 `Plan:` 下的编号列表（`utils.ts:129-150`），再弹出 `ui.select` 让用户选择执行、停留或细化（266-341 行）。
- 容错：解析不到就什么也不做，不报错、不重试、不影响 run。

两种模式的区别：`todo.ts` 是**工具调用 + 结构化参数**，可校验，能保证分支正确；plan-mode 是**宽松的文本标记**，模型不配合时只会漏记进度。两者都不强制每轮输出格式，也不会因为格式问题让 run 失败。

### 5.3 pi-durable

- 这里的 “Task” 是执行层面的持久化状态机（`README.md:99`），例如 generation、tool、compaction 和用户自定义任务；`watchTaskGraph` 给出所有活跃任务的状态树，可用于任务面板或调试（`README.md:786-801`）。**它不是给模型维护的计划或任务板。**
- 用户自己的状态用 `defineDoc` 保存，README 恰好用 `Todos` 文档作为示例（`README.md:803-830`，HEAD 在 810 行）。
- 防失控手段：
  - `maxTurns`：限制一个 run 的工具回合数；到达上限的那一轮仍会执行完工具，然后 run 以 `turn_limit` 结束，答案永远不会被截断（`README.md:404`）；
  - `onYield` 钩子：模型给出答案时可以返回 `{continue: "Keep going."}` 让 run 继续，但扩展必须自己限制循环次数（`README.md:404, 657-662`）；
  - `afterRun` 任务；
  - `runFailure` 原因枚举。

---

## 6. 对 pycode 的启示

### 6.1 针对 (A) 简化事件体系与工具生命周期

**值得照搬**
1. **单一可辨识联合**：仿照 `types.ts:516-537`，pycode 用 Python 的 `TypedDict` 或 frozen dataclass 按 `kind` 定义每种事件的 payload，替换 `data: dict`（`kernel/domain/events.py:37-44`）。stream-json 输出和 TUI 渲染都可以用类型检查。
2. **单一结算出口 + 恰好一次 end**：Pi 的每条路径（`immediate`、执行、中止、截断）都经过 `emitToolExecutionEnd → createToolResultMessage`。pycode 的 G2 缺口（既有报告 §4：handoff 取消、用户取消、重启恢复都不发 end，也不触发 post Hook）应该收进同一个 `settle(call, outcome)`。`recover_conversation` 和 handoff 分支也要调用它。
3. **durable 的“意图先落盘 + 两种中断结局”**：pycode 已有 `custom{kind:"tool_started"}`，它就是 Pi durable 的 `{phase:"execute"}` 意图。缺的是用它在结局上区分 `abandoned`（未开始，无副作用）和 `interrupted`（可能已部分执行），正好修复 G3（`session/services/restart.py:57-87` 把未开始的调用也写成 “Effects may have occurred”）。再加上可选的 `replay: "safe"` 声明，就可以让 read 类工具在重启后重跑，与 `restore_task` 现有行为一致，并把这条规则写明。
4. **显式 ToolCall 状态枚举**：Pi 核心没有对外的枚举，durable 有 `pending/running/done` 槽位和 `call/execute` 阶段。pycode 需要的比 Pi 多一个 `awaiting_approval`，建议状态为 `issued → validating → awaiting_approval → running → settled(outcome)`，其中 `outcome ∈ {completed, error, denied, blocked, abandoned, interrupted, aborted}`。状态转移按 `operation_id` 做幂等：`tool_execution_start` 只在第一次进入 `running` 或 `validating` 时发出，这样就消除了审批恢复和重启导致的重复 start（G2）。

**pycode 比 Pi 复杂、可以回退简化的部分**
- `tool_action_start/end` 和 `tool_output_ready`：Pi 没有这几种事件。可以并入 `tool_execution_update`（进度）和 `tool_execution_end` 的字段（`output_id`、`duration_ms`）。注意 Team 的 evidence 依赖 `tool_execution_end` 中的 `output_id`（`team/engine.py:628-643`），合并后需要保留这个字段。
- `message_delta` 和 `thinking_delta` 可以合并为 `message_update{channel}`，也可以保留现状；这只是命名问题，影响不大。
- `usage` 和 `model_request_start/end` 可以挂到 assistant 的 `message_end` 上（Pi 的做法）。只有在 TUI 需要显示“请求中”状态时才保留一种 `model_request` 事件。
- `notification` 的 3 个子类和 `error`：参照 Pi，运行级错误用 `agent_end{outcome, error}` 表达，或者用一个 `result` 事件，不再单独设 `error` 事件。
- 收敛后约 12 种：`agent_start/agent_end`、`turn_start/turn_end`、`message_start/update/end`、`tool_execution_start/update/end`、`needs_input`、`result`（约等于 Pi 的 `agent_settled`），`team` 另算。`retry` 和 `summary` 可以作为 `result` 之前的状态字段，或者保留为 2 种辅助事件。
- 持久化记录：Pi 只有 11 种，扩展状态统一用 `custom{customType}`，需要进入上下文的用 `custom_message`。pycode 的 `skill_load/skill_catalog`、`memory_load`、`mcp_catalog`、`team_*` 等可以收进 `custom{kind}`，只把影响模型上下文的类型保留为一级类型。pycode 已经有 `custom.data.kind` 机制，可以直接复用。
- Hook：Pi 只有一个 `pi.on`，hook 就是事件。pycode 的配置化 Hook 名称面向用户，可以保持兼容，但触发点应该挂在 ToolCall 状态转移上：`pre_tool_use` 对应进入 running 之前，`post_tool_use` 和 `error` 对应 `settle` 内部。这样各条路径都会触发，不再有遗漏。

**需要避免的 Pi 缺点**
- 运行状态用一组布尔量和 AbortController 表示（`agent-session.ts:395-440`），所以“是否空闲”需要组合 `isStreaming && !isCompacting && !isRetrying …` 才能判断；`agent_end` 也不代表空闲，Pi 只好补了 `agent_settled`。pycode 应该直接用一个 `RunState` 枚举，例如 `idle | running | awaiting_input | settling | compacting | failed`。pycode 已有持久化的 `run_start/run_wait/run_end`，可以直接映射到这个枚举。
- 孤儿 tool call 在请求时合成结果、不落盘（`transform-messages.ts:179-244`）：pycode 已经把 `tool_interrupted` 落盘，这比 Pi 好，应该保留。
- Pi 核心没有审批，所以 pycode 的 `needs_input` 状态和 LangGraph interrupt 重入问题在 Pi 中**找不到参考实现**。durable 的 `waiting` 状态（`types.ts:675`）加上 `beforeTool` 的 `block` 是最接近的抽象。
- durable 已经放弃事件流、改为渲染状态（`CHANGELOG.md:55`）。pycode 的 TUI 可以考虑从“ToolCall 表 + RunState”渲染，只给 stream-json 保留精简的事件，从而减少事件种类的增长。这是可选方向，不要求一次做完。

### 6.2 针对 (B) 任务状态机 / Agent 状态机（任务状态板）

**Pi 的立场**：不内置。示例给出两种做法，都只是“软约束”：

| 方式 | 更新途径 | 状态存放 | 格式错误时 | 分支正确性 |
|---|---|---|---|---|
| Pi `todo.ts` | 专用工具调用（结构化参数） | toolResult `details`，按分支重建 | 返回错误文本，run 继续 | 是 |
| Pi plan-mode | assistant 文本中的 `Plan:` 列表和 `[DONE:n]` | `custom` entry 加文本扫描 | 忽略，run 继续 | 部分（依赖 execute 标记） |
| durable | 用户 `defineDoc` + `maxTurns` / `onYield` | 持久化文档 | 不涉及 | 由 fork 策略决定 |
| **pycode 未提交方案** | **每条非空 assistant 文本都必须是 JSON 外壳 `{task_action, message}`** | `provider_metadata` 加投影出的 `task_state` | **抛 `ModelError("invalid_task_output")`，run 失败，已发出的工具调用不执行** | 是（按 path 计算证据） |

**评估 pycode 的“强制 JSON 任务输出契约”**（`agent/adapters/task_output.py:11-81`；接入位置 `agent/adapters/history.py:215-220`、`agent/adapters/boundaries.py:7,15-17,116`、`models/adapters/messages.py:53-61`；同时在 `agent/adapters/agent.py` 的未提交 diff 中移除了 Deep Agents 的 `TodoListMiddleware` 和 `write_todos`）：

合理的部分：
- 证据约束是 Pi 没有的有价值设计：完成的步骤必须带 `evidence_refs`，退出码为 0 不等于目标达成，不能声称没有执行过的检查已通过（`context/domain/tasks.py:66-126` `validate_task_action`）。“new / continue / switch” 三种关系也为防跑偏提供了明确语义。
- 状态从当前分支投影得到（`context/services/task_projection.py`），与 Pi 的“按 `getBranch()` 重建”原则一致。

不合理或代价过高的部分：
1. **容错方向反了**：格式不符或多出一个字段就让整个 run 失败（`tests/agent/adapters/test_task_context.py:110-122` 断言 `outcome == "error"`、`not tool.executed`），而 Pi 的两种做法在格式错误时都让 run 继续。弱模型、部分 provider 和启用 thinking 的模型偶尔不遵守格式是常态，这会把“偶尔记账失败”放大成“任务中断”。
2. **与原生工具调用互相牵制**：仅调用工具的回复必须文本为空；`switch` 时禁止同时发工具调用，并要额外发一次模型请求（`task_output.py:20-22, 63-68`）；流式显示需要 `visible_partial` 做部分 JSON 解析；被截断的回复只能取 `message` 字段（`:55-56`）。Pi 用工具调用表达状态变化，天然与工具通道一致，不需要这些特殊处理。
3. **每轮都有成本**：约 25 行规则加上完整 JSON Schema 被拼接进**每次** system prompt（`boundaries.py:15-17`）；历史中的 assistant 消息被重新序列化成 JSON 外壳回放给模型（`models/adapters/messages.py:53-61`），普通闲聊也要套外壳。
4. **移除了现成的工具式方案**：Deep Agents 的 `write_todos` 就是 Pi `todo.ts` 那种模式，现在被排除（`agent/adapters/agent.py` 的 diff 中 `excluded_tools={"write_todos"}`）。

**建议**（只给建议，不实施）
1. 把契约改成**专用工具**，例如 `task_update(relation, task_id, update)`，参数直接复用现有的 `TaskAction` pydantic schema。工具内部调用 `validate_task_action`；校验失败时返回 `is_error` 的工具结果，让模型修正后重试，不再 `raise ModelError`。状态放在工具结果的 details 或现有的投影中，按分支重建（Pi `todo.ts:114-133` 的模式）。assistant 文本恢复为普通文本。
2. 保留“每次请求注入当前任务状态”（pycode 已有 `task_context_text`；这相当于 Pi plan-mode 的 `before_agent_start` 注入），让模型始终看到目标、约束和当前步骤，这才是防跑偏的主要手段。
3. 加一个**有上限的结束前检查**：当模型给出最终答案、但活跃任务还有未完成步骤或 `current_step` 时，注入一次提醒并续跑一轮（durable 的 `onYield`、Pi 的 `finishTurn`/`agent_before_settle` 续跑）。每个 run 最多续跑 N 次，并配合 `maxTurns`（durable `README.md:404`）防止死循环。
4. 如果坚持用强制格式，至少把失败降级处理：保留文本、不改动任务状态、记录一个 notification，并允许执行本轮工具调用；或者只在“声明任务完成”的回合要求结构化输出。
5. Agent 状态机：参照 durable 的 `TaskState` 和 `runFailure` 枚举，给 pycode 定义 `RunState` 加 `run_end.reason` 枚举，例如 `completed | aborted | error | interrupted | turn_limit | awaiting_input`。任务状态机（`TaskStep.status`：pending / in_progress / blocked / completed，`context/domain/tasks.py:17-33`）与 RunState 分开维护，不要把它们耦合在 assistant 的文本格式里。

---

## 附：主要证据索引

| 主题 | Pi 路径:行（42a3497 / HEAD 2c8d1fe） |
|---|---|
| 设计哲学 | `packages/coding-agent/README.md:15,19`（相同） |
| AgentEvent | `packages/agent/src/types.ts:516-537`（相同） |
| AgentState / isStreaming | `packages/agent/src/types.ts:382-421`（相同） |
| before/afterToolCall | `packages/agent/src/types.ts:66-105, 326, 341`（相同） |
| replay / executionMode | `packages/agent/src/types.ts:490, 498`（相同） |
| runLoop | `packages/agent/src/agent-loop.ts:163 / 172` |
| 串行/并行执行 | `agent-loop.ts:530 / 540`，`586 / 596`，`Promise.all` 在 `646 / 656` |
| 截断 | `agent-loop.ts:478 / 488` |
| 单一 end 出口 | `agent-loop.ts:919 / 929`，`930 / 940` |
| 队列 | `packages/agent/src/agent.ts:143 / 149`，`299 / 308`，`304 / 313` |
| 失败兜底 | `agent.ts:532 / 542`，`550 / 560`，`565 / 575` |
| AgentSessionEvent | `packages/coding-agent/src/core/agent-session.ts:196 / 209` |
| tool_call 接线 | `agent-session.ts:652-720 / 668-736`；`extensions/runner.ts:1242 / 1249` |
| ExtensionEvent | `packages/coding-agent/src/core/extensions/types.ts:1377 / 1425`；SessionEvent `848 / 882` |
| SessionEntry | `packages/coding-agent/src/core/session-manager.ts:43-194`（相同） |
| 生命周期文档 | HEAD `packages/coding-agent/docs/extensions.md:300-357, 398-413, 2112-2146` |
| 孤儿调用合成 | `packages/ai/src/api/transform-messages.ts:158-223 / 179-244` |
| todo 示例 | `packages/coding-agent/examples/extensions/todo.ts:1-11, 114-133`（HEAD） |
| plan-mode 示例 | `examples/extensions/plan-mode/index.ts:116-123, 164-264, 266-341, 344-392`；`utils.ts:97, 129-168`（HEAD） |
| durable ToolTask | `packages/durable/src/harness/tool.ts:84-87, 135-247`（HEAD；基线 `65-67, 108-190`） |
| durable TaskState / Outcome | `packages/durable/src/types.ts:621-702`（HEAD） |
| durable 删除事件流 | 提交 f3255de76；`packages/durable/CHANGELOG.md:55` |
| durable maxTurns / runFailure | `packages/durable/README.md:404, 563`（HEAD） |

| pycode 对应 | 路径 |
|---|---|
| 运行时事件 | `kernel/domain/events.py:8-44`（来源注明 Pi types.ts） |
| Hook 事件 | `hooks/types.py:16-34`（来源注明 MewCode） |
| 会话记录 | `kernel/domain/history.py`（来源注明 Pi session-manager.ts） |
| 工具执行器 / 结算 | `tools/services/tool_executor.py`（来源注明 Pi agent-loop.ts）、`tools/adapters/settlement.py` |
| 回合策略 | `agent/domain/turn_policy.py`（来源注明 Pi agent-loop.ts） |
| 输入队列 | `session/services/input_queue.py`、`durable_inputs.py`（来源注明 Pi agent.ts） |
| 重启恢复 | `session/services/restart.py` |
| 强制 JSON 任务契约（未提交） | `agent/adapters/task_output.py`、`context/domain/tasks.py`、`context/services/task_projection.py`、`tests/agent/adapters/test_task_context.py` |
