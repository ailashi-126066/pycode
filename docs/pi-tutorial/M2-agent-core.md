# M2 pi-agent-core：agent 循环

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

pi-agent-core（源码目录 `packages/agent`）实现 agent 循环：把 transcript 发给模型，接收流式回复，执行回复里的工具调用，把工具结果追加到 transcript，再请求模型，直到模型不再调用工具。

这个包不知道会话文件、扩展、系统提示词怎么拼、TUI 怎么画。它只认三样东西：

- 一组 Agent 消息（`AgentMessage[]`）和一组工具（`AgentTool[]`），合起来叫 `AgentContext`。
- 一个返回事件流的函数 `streamFn`。它由 M1 的 pi-ai 提供，本模块把它当成黑盒。
- 一组回调（`AgentLoopConfig` 里的 hook）。上层的 AgentSession（M8）通过这些回调接入会话存储、压缩、扩展和权限检查。

循环每走一步，就发出一个 `AgentEvent`。上层靠订阅这些事件来持久化消息和刷新界面。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M2 是从 `agent.prompt(messages)` 到 `agent_end` 的这一段：

```
session.prompt(text)                                   [M8]
└─ agent.prompt(messages)                              [M2] ← 本模块入口
    └─ runWithLifecycle() → runAgentLoop() → runLoop()
        ├─ declareToolChanges()
        ├─ prepareRequest()            ← hook，M8 在这里用会话文件重建上下文
        ├─ streamAssistantResponse()
        │   ├─ transformContext()      ← hook，M7
        │   ├─ convertToLlm()          ← hook，M8
        │   └─ streamFn()              → M1
        ├─ executeToolCalls()
        │   ├─ beforeToolCall()        ← hook，M7
        │   ├─ tool.execute()          → M3
        │   └─ afterToolCall()         ← hook，M7
        ├─ finishTurn()                ← hook，M8
        ├─ prepareNextTurn()           ← hook，M8 → M5
        └─ 每个事件 → Agent.processEvents() → 订阅者（M8 的 _handleAgentEvent）
```

图中标"hook"的地方，M2 只负责在正确的时机调用，具体做什么由上层决定。

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/agent/src/agent-loop.ts` | 959 | 无状态的循环函数：`runAgentLoop`、`runLoop`、工具执行流水线 | 精读，§4 的主体 |
| `packages/agent/src/agent.ts` | 623 | 有状态的 `Agent` 类：持有 transcript、队列、订阅者，管理一次运行的生命周期 | 精读 |
| `packages/agent/src/types.ts` | 539 | 全部公共类型和 hook 契约（注释里写明了每个 hook 的调用时机和"不许抛异常"等约定） | 精读，§3 和 §4.18 |
| `packages/agent/src/proxy.ts` | 395 | `streamProxy`：一个通过 HTTP 代理服务器转发请求的 `streamFn` 实现 | 通读，§4.19 |
| `packages/agent/src/stream-fn.ts` | 20 | 全局默认 `streamFn` 的设置和读取 | 通读，§4.19 |
| `packages/agent/src/index.ts` | 5 | 重新导出以上文件 | 略 |

行数用 `wc -l packages/agent/src/*.ts` 统计。

### 1.4 读完能回答的问题

1. 一次 `agent.prompt()` 会请求模型几次？循环在什么条件下停下来？
2. 模型一次返回两个工具调用时，它们是同时执行的吗？结果按什么顺序回到 transcript？
3. 用户在 agent 工作时插话（steering），插话在什么时刻被模型看到？
4. 模型输出被 token 上限截断时，里面的工具调用会被执行吗？
5. `agent_end` 事件发出之后，agent 就空闲了吗？

---

## 2. 先跑一次

### 2.1 主线场景里 M2 发出的事件

用 json 模式运行主线场景，事件流里除了第一行 `session` 和最后一行 `agent_settled`，其余事件都由 M2 发出：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m2-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m2-agent ./run.sh --mode json "读 README.md，总结成三句话" > /tmp/pi-lab/m2-events.jsonl
node summarize-events.mjs /tmp/pi-lab/m2-events.jsonl
```

```
session
agent_start
turn_start
message_start (system)
message_end (system)
message_start (user)
message_end (user)
message_start (assistant)
message_update [text_start]
message_update [text_delta] x2
message_update [text_end]
message_update [toolcall_start]
message_update [toolcall_delta] x2
message_update [toolcall_end]
message_end (assistant)
tool_execution_start read
tool_execution_end read
message_start (toolResult)
message_end (toolResult)
turn_end (assistant)
turn_start
message_start (assistant)
message_update [text_start]
message_update [text_delta] x3
message_update [text_end]
message_end (assistant)
turn_end (assistant)
agent_end
agent_settled
共 33 行（含第一行 session 头）
```

`text_delta` 和 `toolcall_delta` 的个数每次运行可能不同（见 README §5.2）。下表把每个事件对应到发出它的代码行。§4 会逐个读到这些位置。

| 事件 | 发出位置 | 说明 |
|---|---|---|
| `agent_start`、第 1 个 `turn_start` | `packages/agent/src/agent-loop.ts:126`、`packages/agent/src/agent-loop.ts:127` | `runAgentLoop` 开头 |
| `message_start/end (system)`、`(user)` | `packages/agent/src/agent-loop.ts:128` | prompt 消息。system 消息由 M8 构造，随 user 消息一起传进来 |
| `message_start (assistant)` | `packages/agent/src/agent-loop.ts:430` | 收到 provider 的 `start` 事件 |
| `message_update [...]` | `packages/agent/src/agent-loop.ts:445` | 每个流式片段包一层 |
| `message_end (assistant)` | `packages/agent/src/agent-loop.ts:464` | 收到 `done` |
| `tool_execution_start/end read` | `packages/agent/src/agent-loop.ts:607`、`packages/agent/src/agent-loop.ts:929` | 默认是并行执行模式 |
| `message_start/end (toolResult)` | `packages/agent/src/agent-loop.ts:956` | 工具结果作为消息 |
| `turn_end` | `packages/agent/src/agent-loop.ts:296` | 一轮结束 |
| 第 2 个 `turn_start` | `packages/agent/src/agent-loop.ts:216` | 因为有工具结果，循环继续 |
| `agent_end` | `packages/agent/src/agent-loop.ts:329` | 没有工具调用，也没有排队消息 |

### 2.2 工具调用的真实事件

```bash
grep -E '^\{"type":"(tool_execution_start|tool_execution_end)"' /tmp/pi-lab/m2-events.jsonl
grep -m1 '^{"type":"message_end","message":{"role":"toolResult"' /tmp/pi-lab/m2-events.jsonl
```

```
{"type":"tool_execution_start","toolCallId":"call_1","toolName":"read","args":{"path":"README.md"}}
{"type":"tool_execution_end","toolCallId":"call_1","toolName":"read","result":{"content":[{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}],"structuredContent":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"},"isError":false,"durationMs":1}
{"type":"message_end","message":{"role":"toolResult","toolCallId":"call_1","toolName":"read","content":[{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}],"isError":false,"durationMs":1,"timestamp":1793515172213}}
```

可以看到同一个工具调用有两种表示：

- `tool_execution_end` 带着工具返回的原始结果 `result`，里面有 `structuredContent`。
- `toolResult` 消息是要进 transcript、发给模型的版本，没有 `structuredContent`。§4.14 会看到 `createToolResultMessage` 只拷贝了部分字段。

第 1 轮 assistant 消息的 `message_end` 末尾有一个 `thinkingLevel` 字段。§4.6 会看到这个字段是 M2 在 `streamAssistantResponse` 里补上的：

```bash
grep -m1 '^{"type":"message_end","message":{"role":"assistant"' /tmp/pi-lab/m2-events.jsonl | grep -o '"stopReason".*'
```

```
"stopReason":"toolUse","timestamp":1793515172210,"durationMs":1,"thinkingLevel":"off"}}
```

json 模式输出 `message_update` 时，把累积的 `message` 换成了 `usage`，并去掉了 `assistantMessageEvent.partial`，以减少输出量（→ M9 `json-event.ts`），所以这里看不到流式过程中的 partial 消息。§4.6 用代码说明它的内容。

`agent_end` 带着本次运行新增的全部消息：

```bash
grep '^{"type":"agent_end"' /tmp/pi-lab/m2-events.jsonl | node -e 'const e=JSON.parse(require("fs").readFileSync(0,"utf8"));console.log(e.messages.map(m=>m.role).join(", "))'
```

```
system, user, assistant, toolResult, assistant
```

### 2.3 去掉 coding-agent，只留循环

[`lab/m2-loop.ts`](lab/m2-loop.ts) 直接调用 `runAgentLoop`，模型是一个按顺序返回预设回复的 `streamFn`，工具是两个只会 sleep 的假工具 `slow`（300ms）和 `fast`（50ms）。没有会话文件，没有扩展，没有系统提示词。`basic` 场景是"调用一次 fast，然后回答"：

```bash
node --conditions=source --experimental-strip-types m2-loop.ts basic
```

```
场景：basic（runAgentLoop + await emit）
   3ms  agent_start
   3ms  turn_start
   3ms  message_start (system)
   3ms  message_end (system)
   3ms  message_start (user) "开始"
   3ms  message_end (user) "开始"
   4ms  [模型] 收到 2 条消息，返回 stopReason=toolUse
   4ms  message_start (assistant)
   4ms  message_end (assistant)
   4ms  tool_execution_start fast
  56ms  tool_execution_end fast isError=false
  57ms  message_start (toolResult) fast isError=false
  57ms  message_end (toolResult) fast isError=false
  57ms  turn_end (assistant)
  57ms  turn_start
  57ms  [模型] 收到 4 条消息，返回 stopReason=stop
  57ms  message_start (assistant)
  57ms  message_end (assistant)
  57ms  turn_end (assistant)
  57ms  agent_end
结束：本次运行新增 5 条消息：system, user, assistant, toolResult, assistant
```

事件的形状和主线场景一样。区别有两处：

- 这里的 `system` 消息不是调用方传进来的。调用方只传了 user 消息，但 `context.tools` 里有两个工具，transcript 里还没有声明它们，于是循环自己插入一条 `system` 消息来声明工具（§4.4 `declareToolChanges`）。
- 假模型的回复没有流式片段，所以没有 `message_update`。

后面的实验都用这个脚本。它的其他场景见 §7。

---

## 3. 数据结构

本模块的代码大部分在做这些类型之间的转换。先认识它们。

### 3.1 AgentMessage：Agent 消息

README §3.1 的三层消息里，Agent 消息是中间那层。核心定义是下面最后一行：

源码：[`packages/agent/src/types.ts:365-374`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L365-L374)

```ts
export interface CustomAgentMessages {
	// Empty by default - apps extend via declaration merging
}

/**
 * AgentMessage: Union of LLM messages + custom messages.
 * This abstraction allows apps to add custom message types while maintaining
 * type safety and compatibility with the base LLM messages.
 */
export type AgentMessage = Message | CustomAgentMessages[keyof CustomAgentMessages];
```

`Message` 是 M1 定义的 LLM 消息，有 `system`、`user`、`assistant`、`toolResult` 四种 role。`CustomAgentMessages` 默认是空接口，所以单独使用 pi-agent-core 时，`AgentMessage` 就等于 `Message`。

应用可以用 TypeScript 的声明合并往这个接口里加字段，每加一个字段，`AgentMessage` 的联合类型就多一个成员。coding-agent 就是这样加了四种消息（→ M8）：

源码：[`packages/coding-agent/src/core/messages.ts:70-77`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/messages.ts#L70-L77)

```ts
declare module "@earendil-works/pi-agent-core" {
	interface CustomAgentMessages {
		bashExecution: BashExecutionMessage;
		custom: CustomMessage;
		branchSummary: BranchSummaryMessage;
		compactionSummary: CompactionSummaryMessage;
	}
}
```

（`types.ts` 注释里的示例写的是 `declare module "@mariozechner/agent"`，这是旧包名。实际要写 `@earendil-works/pi-agent-core`，如上。）

这些自定义消息只在 transcript 里存在，不会直接发给模型。每次请求前，`convertToLlm` hook 负责把它们转成 LLM 消息或丢掉（§4.6）。`Agent` 类的默认实现是只保留四种标准 role：

源码：[`packages/agent/src/agent.ts:39-47`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L39-L47)

```ts
function defaultConvertToLlm(messages: AgentMessage[]): Message[] {
	return messages.filter(
		(message) =>
			message.role === "system" ||
			message.role === "user" ||
			message.role === "assistant" ||
			message.role === "toolResult",
	);
}
```

| 字段 | 说明 |
|---|---|
| `role` | 区分消息类型。标准 role 来自 M1；自定义 role 由应用定义 |
| 其余字段 | 取决于 role。M2 只读 `assistant` 消息的 `content`（找工具调用）、`stopReason`（决定循环走向）和 `errorMessage`（记录错误），以及 `system` 消息的 `toolsAdded`/`toolsRemoved`（工具清单） |

**真实实例。** [`lab/m2-agent.ts`](lab/m2-agent.ts) 用 `Agent` 类跑一次循环，在初始 transcript 里放了一条 `role: "note"` 的自定义消息。下面是完整输出，本节后面几个类型的实例都取自这次运行：

```bash
node --conditions=source --experimental-strip-types m2-agent.ts dump
```

```
[AgentLoopConfig] 已设置的回调：convertToLlm, prepareRequest, getSteeringMessages, getFollowUpMessages, beforeToolCall, afterToolCall
[AgentLoopConfig] 未设置的回调：transformContext, getApiKey, prepareNextTurn, finishTurn
[AgentLoopConfig] toolExecution： "parallel"
[AgentState] 运行前 messages： [{"role":"system","content":"You are a test agent.","toolsAdded":[{"name":"greet","description":"Say hello","parameters":{"type":"object","required":["name"],"properties":{"name":{"type":"string"}}}}],"timestamp":0},{"role":"note","text":"只给 UI 看的备注","timestamp":1700000000000}]
[AgentState] 运行前 systemPrompt： "You are a test agent."
[prepareRequest] AgentContext.messages role： ["system","note","user"]
[prepareRequest] AgentContext.tools： ["greet"]
[prepareRequest] thinkingLevel： "off"
[模型] 第 1 次请求，收到的 LLM 消息 role：system, user
[事件] tool_execution_start pendingToolCalls=["c1"]
[beforeToolCall] toolCall： {"type":"toolCall","id":"c1","name":"greet","arguments":{"name":"pi"}}
[beforeToolCall] args： {"name":"pi"}
[afterToolCall] AgentToolResult： {"content":[{"type":"text","text":"hello pi"}],"details":{"greeted":"pi"}}
[afterToolCall] isError： false
[事件] tool_execution_end pendingToolCalls=[]
[事件] tool_execution_end： {"type":"tool_execution_end","toolCallId":"c1","toolName":"greet","result":{"content":[{"type":"text","text":"hello pi"}],"details":{"greeted":"pi"}},"isError":false,"durationMs":0}
[模型] 第 2 次请求，收到的 LLM 消息 role：system, user, assistant, toolResult
[事件] agent_end，监听器开始等待 100ms，isStreaming=true
[事件] agent_end 监听器结束，isStreaming=true
[prompt 返回] isStreaming=false 模型请求次数=2
[AgentState] 运行后 messages role： ["system","note","user","assistant","toolResult","assistant"]
[AgentState] toolResult 消息： {"role":"toolResult","toolCallId":"c1","toolName":"greet","content":[{"type":"text","text":"hello pi"}],"details":{"greeted":"pi"},"isError":false,"durationMs":0,"timestamp":1793515172705}
```

看 `[prepareRequest]` 和 `[模型]` 两行：循环内部的 transcript 是 `system, note, user`，发给模型的只有 `system, user`。`note` 消息被 `defaultConvertToLlm` 过滤掉了。

### 3.2 AgentTool 和 AgentToolResult：工具和工具结果

源码：[`packages/agent/src/types.ts:465-499`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L465-L499)

```ts
/** Tool definition used by the agent runtime. */
export interface AgentTool<TParameters extends TSchema = TSchema, TDetails = any> extends Tool<TParameters> {
	/** Human-readable label for UI display. */
	label: string;
	/**
	 * Optional compatibility shim for raw tool-call arguments before schema validation.
	 * Must return an object that matches `TParameters`.
	 */
	prepareArguments?: (args: unknown) => Static<TParameters>;
	/**
	 * JSON Schema of `structuredContent` in successful results. Tools that declare it should always
	 * set `structuredContent`.
	 */
	outputSchema?: TSchema;
	/**
	 * Execute the tool call. Throw on failure, or return a result with `isError: true`; do not only
	 * describe the failure in `content`.
	 */
	execute: (
		toolCallId: string,
		params: Static<TParameters>,
		signal?: AbortSignal,
		onUpdate?: AgentToolUpdateCallback<TDetails>,
	) => Promise<AgentToolResult<TDetails>>;
	/** Recovery policy for an effect whose durable intent exists but whose outcome is unknown. */
	replay?: "never" | "safe";
	/**
	 * Per-tool execution mode override.
	 * - "sequential": this tool must execute one at a time with other tool calls.
	 * - "parallel": this tool can execute concurrently with other tool calls.
	 *
	 * If omitted, the default execution mode applies.
	 */
	executionMode?: ToolExecutionMode;
}
```

`AgentTool` 继承 M1 的 `Tool`（`name`、`description`、`parameters` 三个字段，见 `packages/ai/src/types.ts:769`）。`Tool` 是发给模型的声明，`AgentTool` 在它上面加了运行时需要的东西：

| 字段 | 说明 |
|---|---|
| `name`、`description`、`parameters` | 来自 `Tool`。`parameters` 是 TypeBox schema，既发给模型，也用来校验模型给的参数 |
| `label` | 界面显示用的名字 |
| `prepareArguments` | 可选。校验参数之前先改写原始参数，用于兼容旧的参数格式 |
| `outputSchema` | 可选。`structuredContent` 的 schema |
| `execute` | 执行工具。参数依次是工具调用 id、校验过的参数、中止信号、进度回调 |
| `replay` | 给 durable 用的恢复策略（→ M11），M2 的循环不读它 |
| `executionMode` | 可选。设为 `"sequential"` 时，整批工具调用都改为串行执行（§4.10） |

`execute` 返回 `AgentToolResult`：

源码：[`packages/agent/src/types.ts:423-446`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L423-L446)

```ts
/** Final or partial result produced by a tool. */
export interface AgentToolResult<T = JsonValue | undefined> {
	/** Text or image content returned to the model. */
	content: (TextContent | ImageContent)[];
	/** Arbitrary structured details for logs or UI rendering. */
	details: T;
	/**
	 * Machine-readable result matching the tool's `outputSchema`, for programmatic callers. Not sent
	 * to the model; `content` remains the model-facing result.
	 */
	structuredContent?: JsonValue;
	/** Usage from the final tool execution itself, if available. Not used for main LLM context accounting. */
	usage?: Usage;
	/**
	 * Report a failure without throwing. The model sees `content` as an error result, like a thrown
	 * error, but `details` and `structuredContent` are kept for the UI and programmatic callers.
	 */
	isError?: boolean;
	/**
	 * Hint that the agent should stop after the current tool batch.
	 * Early termination only happens when every finalized tool result in the batch sets this to true.
	 */
	terminate?: boolean;
}
```

| 字段 | 说明 |
|---|---|
| `content` | 给模型看的内容，文本或图片 |
| `details` | 给界面和日志看的任意数据，不发给模型 |
| `structuredContent` | 给程序调用方看的结构化结果，不发给模型 |
| `usage` | 工具自身消耗的 token（例如工具内部又调了模型），不计入主对话 |
| `isError` | 工具报告失败但不抛异常时设为 `true` |
| `terminate` | 请求在本批工具执行完后停止循环。只有整批结果都设了它才生效（§4.8） |

工具调用经过 hook 处理之后的最终结果叫 `AgentToolCallOutcome`：

源码：[`packages/agent/src/types.ts:448-455`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L448-L455)

```ts
/** Final outcome of a tool call after hooks ran. */
export interface AgentToolCallOutcome {
	toolCall: AgentToolCall;
	result: AgentToolResult<any>;
	isError: boolean;
	/** Milliseconds `execute()` took, measured with a monotonic clock; absent when the tool did not run. */
	durationMs?: number;
}
```

**真实实例。** 上面 `dump` 输出里，`greet` 工具返回的 `AgentToolResult` 是：

```
{"content":[{"type":"text","text":"hello pi"}],"details":{"greeted":"pi"}}
```

主线场景里 `read` 工具的结果见 §2.2 的 `tool_execution_end`，它有 `structuredContent` 而没有 `details`。

### 3.3 AgentContext 和 AgentState：循环的输入和 Agent 的状态

`AgentContext` 是传给循环的快照：

源码：[`packages/agent/src/types.ts:501-507`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L501-L507)

```ts
/** Context snapshot passed into the low-level agent loop. */
export interface AgentContext {
	/** Transcript visible to the model. */
	messages: AgentMessage[];
	/** Tools available for execution in this run. */
	tools?: AgentTool<any>[];
}
```

| 字段 | 说明 |
|---|---|
| `messages` | transcript。循环会直接往这个数组里 push 新消息 |
| `tools` | 可以执行的工具。注意它和 transcript 里 `system` 消息声明的工具清单是两回事，§4.4 会讲两者怎么保持一致 |

`AgentState` 是 `Agent` 类对外暴露的状态：

源码：[`packages/agent/src/types.ts:382-421`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L382-L421)

```ts
export interface AgentState {
	/**
	 * Current system prompt, replayed from the transcript's system messages.
	 *
	 * Read-only: to change the prompt, append a system message with `content` or `sections`.
	 * In `initialState`, this seeds the leading system message.
	 */
	readonly systemPrompt: string;
	/** Active model used for future turns. */
	model: Model<any>;
	/** Requested reasoning level for future turns. */
	thinkingLevel: ThinkingLevel;
	/**
	 * Executable tools. Assigning a new array copies the top-level array.
	 *
	 * Differences from the tools declared in the transcript are announced to the model
	 * with a system message before the next request.
	 */
	set tools(tools: AgentTool<any>[]);
	get tools(): AgentTool<any>[];
	/**
	 * Conversation transcript. Assigning a new array copies the top-level array.
	 *
	 * System messages in the transcript carry the prompt and tool declarations.
	 */
	set messages(messages: AgentMessage[]);
	get messages(): AgentMessage[];
	/**
	 * True while the agent is processing a prompt or continuation.
	 *
	 * This remains true until awaited `agent_end` listeners settle.
	 */
	readonly isStreaming: boolean;
	/** Partial assistant message for the current streamed response, if any. */
	readonly streamingMessage?: AgentMessage;
	/** Tool call ids currently executing. */
	readonly pendingToolCalls: ReadonlySet<string>;
	/** Error message from the most recent failed or aborted assistant turn, if any. */
	readonly errorMessage?: string;
}
```

| 字段 | 说明 |
|---|---|
| `systemPrompt` | 只读。从 transcript 里的 `system` 消息重放出来，不单独存储 |
| `model`、`thinkingLevel` | 下一次运行用的模型和思考级别 |
| `tools`、`messages` | 赋值时会拷贝顶层数组 |
| `isStreaming` | 一次运行进行中为 `true`，直到 `agent_end` 的监听器全部结束 |
| `streamingMessage` | 正在流式接收的 assistant 消息 |
| `pendingToolCalls` | 正在执行的工具调用 id |
| `errorMessage` | 最近一次失败或中止的 assistant 消息里的错误 |

`Agent` 构造时，`initialState` 里的 `systemPrompt` 和 `tools` 被折叠成 transcript 的第一条 `system` 消息，之后 `systemPrompt` 就只是一个 getter：

源码：[`packages/agent/src/agent.ts:83-112`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L83-L112)

```ts
function createMutableAgentState(initialState?: AgentInitialState): MutableAgentState {
	let tools = initialState?.tools?.slice() ?? [];
	let messages = initialState?.messages?.slice() ?? [];
	const initialMessage = createInitialSystemMessage(initialState?.systemPrompt, tools.map(toToolDeclaration));
	if (messages[0]?.role !== "system" && initialMessage) messages.unshift(initialMessage);

	return {
		get systemPrompt() {
			return getCurrentSystemPrompt(messages);
		},
		model: initialState?.model ?? DEFAULT_MODEL,
		thinkingLevel: initialState?.thinkingLevel ?? "off",
		get tools() {
			return tools;
		},
		set tools(nextTools: AgentTool<any>[]) {
			tools = nextTools.slice();
		},
		get messages() {
			return messages;
		},
		set messages(nextMessages: AgentMessage[]) {
			messages = nextMessages.slice();
		},
		isStreaming: false,
		streamingMessage: undefined,
		pendingToolCalls: new Set<string>(),
		errorMessage: undefined,
	};
}
```

`createInitialSystemMessage` 来自 M1（`packages/ai/src/utils/transcript.ts:10`），它生成的 `system` 消息 `timestamp` 固定为 0。

**真实实例。** `dump` 输出里：

- 运行前 `AgentState.messages` 是 `[{"role":"system","content":"You are a test agent.","toolsAdded":[…greet…],"timestamp":0},{"role":"note",…}]`，`systemPrompt` 是 `"You are a test agent."`。
- 传给 `prepareRequest` 的 `AgentContext` 里，`messages` 的 role 是 `["system","note","user"]`，`tools` 是 `["greet"]`。
- 运行后 `AgentState.messages` 的 role 是 `["system","note","user","assistant","toolResult","assistant"]`。
- 工具执行期间 `pendingToolCalls` 是 `["c1"]`，执行完变回 `[]`。

### 3.4 AgentLoopConfig：循环的配置和 hook

`AgentLoopConfig`（`packages/agent/src/types.ts:193`）继承了 M1 的 `SimpleStreamOptions`（温度、maxTokens、sessionId 等请求参数），自己加了 `model` 和一组回调。这组回调是 M2 和上层之间的全部接口。按它们在一轮中被调用的顺序排列：

| 回调 | 定义 | 调用时机 | 返回值的作用 | 主线场景里谁提供 |
|---|---|---|---|---|
| `prepareNextTurn` | `packages/agent/src/types.ts:278` | 第 2 轮起，每轮开始前 | 替换 context、模型、思考级别，或追加消息 | M8，轮间压缩检查（→ M5） |
| `prepareRequest` | `packages/agent/src/types.ts:271` | 每次请求模型前，包括第 1 次 | 替换 context、模型、思考级别 | M8，用会话文件重建 context |
| `transformContext` | `packages/agent/src/types.ts:244` | 每次请求前，`convertToLlm` 之前 | 改写 Agent 消息列表 | M8 转发给扩展的 `context` 事件（→ M7） |
| `convertToLlm` | `packages/agent/src/types.ts:222` | 每次请求前 | Agent 消息 → LLM 消息。唯一的必填回调 | M8 |
| `beforeToolCall` | `packages/agent/src/types.ts:326` | 每个工具调用，参数校验之后、执行之前 | 拦截执行 | M8 转发给扩展的 `tool_call` 事件（→ M7） |
| `afterToolCall` | `packages/agent/src/types.ts:341` | 每个工具执行之后 | 改写结果 | M8 转发给扩展（→ M7） |
| `finishTurn` | `packages/agent/src/types.ts:264` | 每轮的工具结果都发出之后、`turn_end` 之前 | 强制结束或强制再请求一次 | M8，`turn_end` 边界（→ M7） |

另外三个回调不改变流程，只是给循环提供数据：

| 回调 | 定义 | 作用 |
|---|---|---|
| `getApiKey` | `packages/agent/src/types.ts:254` | 每次请求前取 API key，用于会过期的 token |
| `getSteeringMessages` | `packages/agent/src/types.ts:293` | 取出排队的插话消息 |
| `getFollowUpMessages` | `packages/agent/src/types.ts:306` | 取出排队的后续消息 |

最后是 `toolExecution`（`packages/agent/src/types.ts:317`），取值 `"parallel"`（默认）或 `"sequential"`。

types.ts 对这些回调有一条共同约定：`convertToLlm`、`transformContext`、`getApiKey`、`getSteeringMessages`、`getFollowUpMessages` 的注释里都写着 "must not throw or reject"。`convertToLlm` 的注释（`packages/agent/src/types.ts:196`）还给出了一个示例：自定义消息转成 `user` 消息，只给界面看的消息返回空数组丢掉。§4.18 汇总了这些契约。

**真实实例。** `dump` 输出的前三行是 `Agent` 构造出的 `AgentLoopConfig`：设置了 `convertToLlm`（`Agent` 的默认实现）、`prepareRequest`、`beforeToolCall`、`afterToolCall`（脚本传入），以及两个队列回调（`Agent` 总会提供）。`toolExecution` 是 `"parallel"`。

### 3.5 AgentEvent：10 种事件

源码：[`packages/agent/src/types.ts:509-539`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L509-L539)

```ts
/**
 * Events emitted by the Agent for UI updates.
 *
 * `agent_end` is the last event emitted for a run, but awaited `Agent.subscribe()`
 * listeners for that event are still part of run settlement. The agent becomes
 * idle only after those listeners finish.
 */
export type AgentEvent =
	// Agent lifecycle
	| { type: "agent_start" }
	| { type: "agent_end"; messages: AgentMessage[] }
	// Turn lifecycle - a turn is one assistant response + any tool calls/results
	| { type: "turn_start" }
	| { type: "turn_end"; message: AgentMessage; toolResults: ToolResultMessage[] }
	// Message lifecycle - emitted for system, user, assistant, and toolResult messages
	| { type: "message_start"; message: AgentMessage }
	// Only emitted for assistant messages during streaming
	| { type: "message_update"; message: AgentMessage; assistantMessageEvent: AssistantMessageEvent }
	| { type: "message_end"; message: AgentMessage }
	// Tool execution lifecycle
	| { type: "tool_execution_start"; toolCallId: string; toolName: string; args: any }
	| { type: "tool_execution_update"; toolCallId: string; toolName: string; args: any; partialResult: any }
	| {
			type: "tool_execution_end";
			toolCallId: string;
			toolName: string;
			result: any;
			isError: boolean;
			/** Milliseconds `execute()` took, measured with a monotonic clock; absent when the tool did not run. */
			durationMs?: number;
	  };
```

| 层次 | 事件 | 发出次数 |
|---|---|---|
| 运行 | `agent_start`、`agent_end` | 每次运行各 1 次。`agent_end.messages` 是本次运行新增的消息 |
| 轮 | `turn_start`、`turn_end` | 每轮各 1 次。一轮 = 一次模型回复 + 它引出的工具执行 |
| 消息 | `message_start`、`message_update`、`message_end` | 每条消息一对 start/end。只有流式接收的 assistant 消息有 update |
| 工具 | `tool_execution_start`、`tool_execution_update`、`tool_execution_end` | 每个工具调用一对 start/end。工具调用 `onUpdate` 时发 update |

这是 README §3.2 事件层次里的 AgentEvent 层。它下面是 M1 的 `AssistantMessageEvent`（被包进 `message_update.assistantMessageEvent`），上面是 M8 的 `AgentSessionEvent`。

**真实实例。** §2.1 的事件流就是主线场景的完整 AgentEvent 序列；§2.2 是 `tool_execution_start`、`tool_execution_end` 和 `message_end (toolResult)` 的原始 JSON。

### 3.6 类型关系图

```
 Agent（agent.ts，有状态）
 ├─ _state: AgentState ─────────┐ messages: AgentMessage[]   tools: AgentTool[]
 ├─ steeringQueue / followUpQueue│
 └─ listeners                   │ createContextSnapshot()（拷贝数组）
                                ▼
                     AgentContext ──────────┐   AgentLoopConfig（model + hook）
                                            ▼            │
                 runAgentLoop / runLoop（agent-loop.ts，无状态）◀┘
                     │                          │
       convertToLlm  │ AgentMessage[] → Message[]│ assistant.content 里的 toolCall
                     ▼                          ▼
              streamFn → M1             AgentTool.execute()
                     │                          │ AgentToolResult
                     │ AssistantMessage         ▼ + hook → AgentToolCallOutcome
                     │                     createToolResultMessage()
                     │                          │ ToolResultMessage
                     └──────────┬───────────────┘
                                ▼ 都 push 进 AgentContext.messages
                         AgentEvent ──emit──▶ Agent.processEvents()
                                              ├─ message_end → _state.messages.push
                                              └─ 订阅者（M8）
```

循环里有两份 transcript：循环自己的 `AgentContext.messages`（快照的拷贝），和 `Agent._state.messages`。前者由循环直接 push，后者只在收到 `message_end` 事件时 push（§4.17）。两者内容一致，是因为每条消息都会经过一次 `message_end`。

---

## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。

```
Agent.prompt()                                   §4.1
└─ runPromptMessages()                           §4.2
    ├─ createContextSnapshot() / createLoopConfig()
    └─ runWithLifecycle()                        §4.3
        └─ runAgentLoop()                        §4.4
            ├─ declareToolChanges()              §4.4
            └─ runLoop()                         §4.5 ①
                ├─ prepareNextTurn / 轮询 steering
                ├─ declareToolChanges()
                ├─ prepareRequest                §4.5 ②
                ├─ streamAssistantResponse()     §4.6
                │   └─ streamFn → M1
                ├─ error / aborted 退出           §4.5 ②
                ├─ failToolCallsFromTruncatedMessage()  §4.7
                ├─ executeToolCalls()            §4.8
                │   ├─ executeToolCallsSequential()  §4.9
                │   └─ executeToolCallsParallel()    §4.10
                │       ├─ prepareToolCall()         §4.11
                │       ├─ executePreparedToolCall() §4.12
                │       │   └─ tool.execute() → M3
                │       ├─ finalizeExecutedToolCall()§4.13
                │       └─ createToolResultMessage() §4.14
                ├─ finishTurn / 轮询 steering     §4.15 ③
                └─ followUp / 显式继续 / agent_end §4.16 ④
            每个 emit(event) → Agent.processEvents()  §4.17
        handleRunFailure() / finishRun()         §4.3
types.ts 的 hook 契约                             §4.18
旁支：agentLoop、runToolCall、proxy.ts、stream-fn.ts  §4.19
```

### 4.1 Agent.prompt()

**调用位置**：`AgentSession._startRun()` 调用 `agent.prompt(messages)`（→ M8）。在 `lab/m2-agent.ts` 里是 `await agent.prompt("打个招呼")`。

源码：[`packages/agent/src/agent.ts:379-390`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L379-L390)

```ts
	/** Start a new prompt from text, a single message, or a batch of messages. */
	async prompt(message: AgentMessage | AgentMessage[]): Promise<void>;
	async prompt(input: string, images?: ImageContent[]): Promise<void>;
	async prompt(input: string | AgentMessage | AgentMessage[], images?: ImageContent[]): Promise<void> {
		if (this.activeRun) {
			throw new Error(
				"Agent is already processing a prompt. Use steer() or followUp() to queue messages, or wait for completion.",
			);
		}
		const messages = this.normalizePromptInput(input, images);
		await this.runPromptMessages(messages);
	}
```

**讲解**

`prompt` 接受三种输入：一个字符串（可以带图片）、一条消息、一组消息。`normalizePromptInput`（`packages/agent/src/agent.ts:422`）把它们统一成 `AgentMessage[]`；字符串会变成一条 `user` 消息。主线场景里 M8 传入的是一组消息：一条 `system` 消息（系统提示词补丁）和一条 `user` 消息。

`activeRun` 不为空说明上一次运行还没结束，这时直接抛错。运行中想追加消息，要用 `steer()` 或 `followUp()` 排队（§4.15）。

`prompt` 返回的 Promise 在整次运行结束后才 resolve，包括所有 `agent_end` 监听器执行完毕（§4.3）。

**往哪走**：§4.2 `runPromptMessages`。

### 4.2 runPromptMessages()、createContextSnapshot()、createLoopConfig()

**调用位置**：`Agent.prompt()` → `runPromptMessages()`。

源码：[`packages/agent/src/agent.ts:441-455`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L441-L455)

```ts
	private async runPromptMessages(
		messages: AgentMessage[],
		options: { skipInitialSteeringPoll?: boolean } = {},
	): Promise<void> {
		await this.runWithLifecycle(async (signal) => {
			await runAgentLoop(
				messages,
				this.createContextSnapshot(),
				this.createLoopConfig(options),
				(event) => this.processEvents(event),
				signal,
				this.streamFunction,
			);
		});
	}
```

源码：[`packages/agent/src/agent.ts:469-515`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L469-L515)

```ts
	private createContextSnapshot(): AgentContext {
		return {
			messages: this._state.messages.slice(),
			tools: this._state.tools.slice(),
		};
	}

	private createLoopConfig(options: { skipInitialSteeringPoll?: boolean } = {}): AgentLoopConfig {
		let skipInitialSteeringPoll = options.skipInitialSteeringPoll === true;
		return {
			model: this._state.model,
			reasoning: this._state.thinkingLevel === "off" ? undefined : this._state.thinkingLevel,
			sessionId: this.sessionId,
			onPayload: this.onPayload,
			onResponse: this.onResponse,
			onProviderStreamEvent: this.onProviderStreamEvent,
			transport: this.transport,
			thinkingBudgets: this.thinkingBudgets,
			maxRetryDelayMs: this.maxRetryDelayMs,
			identity: this.identity,
			toolExecution: this.toolExecution,
			beforeToolCall: this.beforeToolCall,
			afterToolCall: this.afterToolCall,
			finishTurn: this.finishTurn,
			prepareRequest: this.prepareRequest,
			prepareNextTurn:
				this.prepareNextTurnWithContext || this.prepareNextTurn
					? async (context) => {
							if (this.prepareNextTurnWithContext) {
								return await this.prepareNextTurnWithContext(context, this.signal);
							}
							return await this.prepareNextTurn?.(this.signal);
						}
					: undefined,
			convertToLlm: this.convertToLlm,
			transformContext: this.transformContext,
			getApiKey: this.getApiKey,
			getSteeringMessages: async () => {
				if (skipInitialSteeringPoll) {
					skipInitialSteeringPoll = false;
					return [];
				}
				return this.steeringQueue.drain();
			},
			getFollowUpMessages: async () => this.followUpQueue.drain(),
		};
	}
```

**讲解**

`runPromptMessages` 把三样东西交给无状态的 `runAgentLoop`：

- `createContextSnapshot()` 拷贝了 `messages` 和 `tools` 两个数组。循环往拷贝里 push，不直接动 `Agent` 的状态。
- `createLoopConfig()` 把 `Agent` 的公开字段（`beforeToolCall`、`prepareRequest` 等）原样放进 `AgentLoopConfig`。所以 M8 只要给 `agent.prepareRequest` 等字段赋值，下一次运行就会用上。注意这是在运行开始时取值，运行中再改这些字段，本次运行看不到。
- 事件回调是 `(event) => this.processEvents(event)`，它返回 Promise，循环会 await 它（§4.17）。

`prepareNextTurn` 在 `Agent` 上有两个版本：旧的 `prepareNextTurn(signal)` 和新的 `prepareNextTurnWithContext(context, signal)`，这里统一成循环要的 `prepareNextTurn(context)`。

两个队列回调从 `PendingMessageQueue` 里取消息。`agent.steer(msg)` 和 `agent.followUp(msg)`（`packages/agent/src/agent.ts:308`、`packages/agent/src/agent.ts:313`）只是把消息放进对应的队列：

源码：[`packages/agent/src/agent.ts:149-180`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L149-L180)

```ts
class PendingMessageQueue {
	private messages: AgentMessage[] = [];
	public mode: QueueMode;

	constructor(mode: QueueMode) {
		this.mode = mode;
	}

	enqueue(message: AgentMessage): void {
		this.messages.push(message);
	}

	hasItems(): boolean {
		return this.messages.length > 0;
	}

	peek(): AgentMessage[] {
		if (this.mode === "all") return this.messages.slice();
		const first = this.messages[0];
		return first ? [first] : [];
	}

	drain(): AgentMessage[] {
		const drained = this.peek();
		this.messages = this.messages.slice(drained.length);
		return drained;
	}

	clear(): void {
		this.messages = [];
	}
}
```

`drain()` 取出并删除。`mode` 为 `"one-at-a-time"`（默认）时一次只取最早的一条，为 `"all"` 时全部取出。`skipInitialSteeringPoll` 只在 `continue()` 从 assistant 消息继续时使用：那时排队的 steering 消息已经作为 prompt 传入，循环开头的第一次轮询要跳过，否则会取到下一条。

**往哪走**：§4.3 `runWithLifecycle`。

### 4.3 runWithLifecycle()、handleRunFailure()、finishRun()

**调用位置**：`runPromptMessages()` 和 `runContinuation()`（`continue()` 用）都经过这里。

源码：[`packages/agent/src/agent.ts:517-566`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L517-L566)

```ts
	private async runWithLifecycle(executor: (signal: AbortSignal) => Promise<void>): Promise<void> {
		if (this.activeRun) {
			throw new Error("Agent is already processing.");
		}

		const abortController = new AbortController();
		let resolvePromise = () => {};
		const promise = new Promise<void>((resolve) => {
			resolvePromise = resolve;
		});
		this.activeRun = { promise, resolve: resolvePromise, abortController };

		this._state.isStreaming = true;
		this._state.streamingMessage = undefined;
		this._state.errorMessage = undefined;

		try {
			await executor(abortController.signal);
		} catch (error) {
			await this.handleRunFailure(error, abortController.signal.aborted);
		} finally {
			this.finishRun();
		}
	}

	private async handleRunFailure(error: unknown, aborted: boolean): Promise<void> {
		const failureMessage = {
			role: "assistant",
			content: [{ type: "text", text: "" }],
			api: this._state.model.api,
			provider: this._state.model.provider,
			model: this._state.model.id,
			usage: EMPTY_USAGE,
			stopReason: aborted ? "aborted" : "error",
			errorMessage: error instanceof Error ? error.message : String(error),
			timestamp: Date.now(),
		} satisfies AgentMessage;
		await this.processEvents({ type: "message_start", message: failureMessage });
		await this.processEvents({ type: "message_end", message: failureMessage });
		await this.processEvents({ type: "turn_end", message: failureMessage, toolResults: [] });
		await this.processEvents({ type: "agent_end", messages: [failureMessage] });
	}

	private finishRun(): void {
		this._state.isStreaming = false;
		this._state.streamingMessage = undefined;
		this._state.pendingToolCalls = new Set<string>();
		this.activeRun?.resolve();
		this.activeRun = undefined;
	}
```

**讲解**

这是一次运行的生命周期：

1. 创建 `AbortController` 和一个 Promise，存进 `activeRun`。`agent.abort()` 调用的就是这个 controller；`agent.waitForIdle()` 返回的就是这个 Promise。
2. `isStreaming = true`。
3. 执行循环。
4. 循环抛异常时，`handleRunFailure` 合成一条 `stopReason` 为 `"error"`（或已中止时为 `"aborted"`）的空 assistant 消息，并补发 `message_start`、`message_end`、`turn_end`、`agent_end`。订阅者因此总能看到完整的事件序列，以 `agent_end` 结尾。
5. 无论成功失败，`finishRun()` 都会把 `isStreaming` 改回 `false`，清空 `activeRun`，resolve 第 1 步的 Promise。

循环正常情况下不会抛异常：模型错误编码在事件流里（§4.18 的 StreamFn 契约），工具错误变成 `isError` 结果（§4.11–4.13）。`handleRunFailure` 处理的是违反契约的情况，例如 `convertToLlm` 抛错，或者某个订阅者抛错。

`agent_end` 是循环发出的最后一个事件，但 `isStreaming` 要等到第 5 步才变回 `false`。`dump` 输出里的这三行验证了这一点：

```
[事件] agent_end，监听器开始等待 100ms，isStreaming=true
[事件] agent_end 监听器结束，isStreaming=true
[prompt 返回] isStreaming=false 模型请求次数=2
```

M8 的订阅者在 `agent_end` 时会把事件转发给扩展的 `agent_end` 处理函数（→ M7、M8）。这些处理函数执行期间，agent 仍算"忙"，新的 `prompt()` 会被拒绝。

**往哪走**：§4.4 `runAgentLoop`。

### 4.4 runAgentLoop() 和 declareToolChanges()

**调用位置**：`Agent.runPromptMessages()` → `runWithLifecycle()` → `runAgentLoop()`。

源码：[`packages/agent/src/agent-loop.ts:111-135`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L111-L135)

```ts
export async function runAgentLoop(
	prompts: AgentMessage[],
	context: AgentContext,
	config: AgentLoopConfig,
	emit: AgentEventSink,
	signal: AbortSignal | undefined,
	streamFn: StreamFn,
): Promise<AgentMessage[]> {
	const initialMessages = declareToolChanges(context, prompts);
	const newMessages: AgentMessage[] = [...initialMessages];
	const currentContext: AgentContext = {
		...context,
		messages: [...context.messages, ...initialMessages],
	};

	await emit({ type: "agent_start" });
	await emit({ type: "turn_start" });
	for (const message of initialMessages) {
		await emit({ type: "message_start", message });
		await emit({ type: "message_end", message });
	}

	await runLoop(currentContext, newMessages, config, signal, emit, streamFn ?? getDefaultStreamFn());
	return newMessages;
}
```

**讲解**

`runAgentLoop` 做三件事：

1. 用 `declareToolChanges` 处理 prompt 消息，必要时插入一条声明工具的 `system` 消息。
2. 把处理后的 prompt 拼到 context 后面，得到本次运行的 `currentContext`；`newMessages` 记录本次运行新增的消息，最后作为返回值和 `agent_end.messages`。
3. 发出 `agent_start`、第 1 个 `turn_start`，以及每条 prompt 消息的 `message_start`/`message_end`，然后进入 `runLoop`。

第 1 轮的 `turn_start` 在这里发，第 2 轮起在 `runLoop` 里发（`packages/agent/src/agent-loop.ts:216`）。

`declareToolChanges` 负责让 transcript 里声明的工具和 `context.tools` 保持一致：

源码：[`packages/agent/src/agent-loop.ts:342-372`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L342-L372)

```ts
function declareToolChanges(context: AgentContext, pendingMessages: AgentMessage[]): AgentMessage[] {
	let systemIndex = -1;
	for (let i = pendingMessages.length - 1; i >= 0; i--) {
		if (pendingMessages[i].role === "system") {
			systemIndex = i;
			break;
		}
	}
	const pending = pendingMessages[systemIndex] as SystemMessage | undefined;
	const baseline = pending
		? pendingMessages.map((message, index) =>
				index === systemIndex ? withToolChanges(pending, NO_CHANGES) : message,
			)
		: pendingMessages;
	const changes = getToolStateChanges(
		getCurrentTools([...context.messages, ...baseline]),
		(context.tools ?? []).map(toToolDeclaration),
	);
	const unchanged = changes.toolsAdded.length === 0 && changes.toolsRemoved.length === 0;

	if (pending) {
		// Keep the caller's message object when it already declares no tool changes.
		if (unchanged && !pending.toolsAdded?.length && !pending.toolsRemoved?.length) return pendingMessages;
		return baseline.map((message, index) => (index === systemIndex ? withToolChanges(pending, changes) : message));
	}
	if (unchanged) return pendingMessages;
	const update = withToolChanges({ role: "system", content: "", timestamp: Date.now() }, changes);
	const insertIndex = pendingMessages.findIndex((message) => message.role !== "system");
	const index = insertIndex === -1 ? pendingMessages.length : insertIndex;
	return [...pendingMessages.slice(0, index), update, ...pendingMessages.slice(index)];
}
```

pi 不在请求里单独传工具列表。工具声明写在 transcript 的 `system` 消息里：`toolsAdded` 是新增的工具定义，`toolsRemoved` 是移除的工具名。M1 的 `getCurrentTools`（`packages/ai/src/utils/transcript.ts:58`）按顺序重放所有 `system` 消息，得到当前声明的工具集合。

`declareToolChanges` 比较两个集合：transcript（加上待发送的消息）重放出来的工具，和 `context.tools` 的声明。`getToolStateChanges`（`packages/ai/src/utils/transcript.ts:150`）算出差异：

- 没有差异：原样返回待发送的消息。
- 有差异，并且待发送的消息里已经有一条 `system` 消息：把这条消息的 `toolsAdded`/`toolsRemoved` 换成算出来的差异。调用方写在里面的工具字段只被当作意图，以 `context.tools` 为准。
- 有差异，没有 `system` 消息：新建一条 `content` 为空的 `system` 消息，插在第一条非 system 消息前面。

§2.3 的 `basic` 场景属于第三种：context 是空的，`tools` 有 `slow` 和 `fast`，所以 prompt 前面多了一条 `system` 消息。§3.1 的 `dump` 场景属于第一种：初始 `system` 消息已经声明了 `greet`，所以没有插入新消息，传给 `prepareRequest` 的 role 是 `system, note, user`，其中的 `system` 是构造时生成的那条。

这样做的好处是工具清单的每次变化都留在 transcript 里，会话文件可以完整重放出任意时刻模型看到的工具（→ M4）。

**往哪走**：§4.5 `runLoop`。

### 4.5 runLoop() ①②：双层循环、请求模型、错误退出

**调用位置**：`runAgentLoop()` 和 `runAgentLoopContinue()` 的最后一步。

`runLoop` 有 160 行，按逻辑分四段读：① 循环结构和轮的开始，② 请求模型和错误退出（本节），③ 执行工具和轮的结束（§4.15），④ 外层循环和退出（§4.16）。

#### ① 双层循环和轮的开始

源码：[`packages/agent/src/agent-loop.ts:172-226`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L172-L226)

```ts
async function runLoop(
	initialContext: AgentContext,
	newMessages: AgentMessage[],
	initialConfig: AgentLoopConfig,
	signal: AbortSignal | undefined,
	emit: AgentEventSink,
	streamFunction: StreamFn,
): Promise<void> {
	let currentContext = initialContext;
	let config = initialConfig;
	let lastCompletedTurn: PrepareNextTurnContext | undefined;
	let explicitContinuation = false;
	// Check for steering messages at start (user may have typed while waiting)
	let pendingMessages: AgentMessage[] = (await config.getSteeringMessages?.()) || [];

	// Outer loop: continues when queued follow-up messages arrive after agent would stop
	while (true) {
		let hasMoreToolCalls = true;

		// Inner loop: process tool calls and steering messages
		while (hasMoreToolCalls || pendingMessages.length > 0) {
			let preparedMessages: AgentMessage[] = [];
			if (lastCompletedTurn) {
				const nextTurnSnapshot = await config.prepareNextTurn?.(lastCompletedTurn);
				if (nextTurnSnapshot) {
					currentContext = nextTurnSnapshot.context ?? currentContext;
					preparedMessages = nextTurnSnapshot.messages ?? [];
					config = {
						...config,
						model: nextTurnSnapshot.model ?? config.model,
						reasoning:
							nextTurnSnapshot.thinkingLevel === undefined
								? config.reasoning
								: nextTurnSnapshot.thinkingLevel === "off"
									? undefined
									: nextTurnSnapshot.thinkingLevel,
					};
				}
				// Preparation can be long-running (for example, compaction). Pick up steering
				// queued while it ran. Only poll again if the earlier poll returned nothing;
				// otherwise one-at-a-time mode would deliver two messages in this turn.
				if (pendingMessages.length === 0) {
					pendingMessages = (await config.getSteeringMessages?.()) || [];
				}
				await emit({ type: "turn_start" });
			}

			// Process prepared and queued messages before the next assistant response.
			for (const message of declareToolChanges(currentContext, [...preparedMessages, ...pendingMessages])) {
				await emit({ type: "message_start", message });
				await emit({ type: "message_end", message });
				currentContext.messages.push(message);
				newMessages.push(message);
			}
			pendingMessages = [];
```

**讲解**

先看循环结构：

- 外层 `while (true)`（188 行）：内层循环结束后，检查有没有 follow-up 消息，有就再进内层循环。
- 内层 `while (hasMoreToolCalls || pendingMessages.length > 0)`（192 行）：只要上一轮有工具调用（需要把结果交给模型），或者有排队的消息，就再请求一次模型。

`hasMoreToolCalls` 初始化为 `true`，所以内层循环至少执行一次。

进入循环前，185 行先轮询一次 steering 队列：用户可能在 agent 启动前就输入了插话。

每轮开始时，如果不是第 1 轮（`lastCompletedTurn` 有值），依次做：

1. 调用 `prepareNextTurn` hook（195 行）。返回值可以替换 context、模型和思考级别，也可以附带要追加的消息。M8 在这里检查是否需要压缩（→ M5）。
2. 如果还没有待处理的消息，再轮询一次 steering 队列（213–215 行）。注释说明了原因：`prepareNextTurn` 可能很慢（例如压缩），用户在这期间的插话应该在本轮被看到。已经有待处理消息时不再轮询，否则 `one-at-a-time` 模式会在一轮里塞进两条。
3. 发出 `turn_start`。

然后，`prepareNextTurn` 追加的消息和排队的消息一起经过 `declareToolChanges`，逐条发出 `message_start`/`message_end`，push 进 context（220–225 行）。

`config` 和 `currentContext` 在循环里会被整体替换（`config = { ...config, ... }`），所以 hook 对模型和 context 的修改从这一轮起持续生效。

**往哪走**：继续读 ②。

#### ② 请求模型和错误退出

源码：[`packages/agent/src/agent-loop.ts:228-265`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L228-L265)

```ts
			const requestUpdate = await config.prepareRequest?.(
				{
					context: currentContext,
					model: config.model,
					thinkingLevel: config.reasoning ?? "off",
				},
				signal,
			);
			if (requestUpdate) {
				currentContext = requestUpdate.context ?? currentContext;
				config = {
					...config,
					model: requestUpdate.model ?? config.model,
					reasoning:
						requestUpdate.thinkingLevel === undefined
							? config.reasoning
							: requestUpdate.thinkingLevel === "off"
								? undefined
								: requestUpdate.thinkingLevel,
				};
			}

			// Stream assistant response
			const message = await streamAssistantResponse(currentContext, config, signal, emit, streamFunction);
			newMessages.push(message);

			if (message.stopReason === "error" || message.stopReason === "aborted") {
				lastCompletedTurn = {
					message,
					toolResults: [],
					context: currentContext,
					newMessages,
				};
				await config.finishTurn?.(lastCompletedTurn, signal);
				await emit({ type: "turn_end", message, toolResults: [] });
				await emit({ type: "agent_end", messages: newMessages });
				return;
			}
```

**讲解**

`prepareRequest`（228 行）在每次请求模型前调用，包括第 1 次。它和 `prepareNextTurn` 的区别是：

- `prepareNextTurn` 只在第 2 轮起调用，可以追加消息。
- `prepareRequest` 每次请求都调用，不能追加消息，此时待处理的消息已经 push 进 context 了。

M8 在 `prepareRequest` 里用 `sessionManager.buildSessionProjection()` 的结果替换整个 `context.messages`（→ M8 `_installAgentRequestProjection`，`packages/coding-agent/src/core/agent-session.ts:803`）。也就是说，在 pi 里，循环自己维护的 transcript 每次请求前都会被会话文件的投影覆盖，会话文件才是上下文的唯一来源。

`thinkingLevel` 在 config 里存成 `reasoning`，`"off"` 对应 `undefined`，241–246 行做这个转换。

251 行调用 `streamAssistantResponse` 拿到完整的 assistant 消息（§4.6）。注意 252 行只 push 进 `newMessages`：`streamAssistantResponse` 内部已经把它 push 进 `context.messages` 了。

254 行是第一个出口：`stopReason` 为 `"error"` 或 `"aborted"` 时，调用 `finishTurn`（忽略返回值），发出 `turn_end` 和 `agent_end`，直接返回。不检查工具调用，不轮询任何队列。错误后要不要重试，由 M8 在 `agent.prompt()` 返回之后决定（→ M8 `_handlePostAgentRun`）。

**往哪走**：§4.6 `streamAssistantResponse`，然后回到 §4.7 处理工具调用。

### 4.6 streamAssistantResponse()

**调用位置**：`runLoop()` 251 行。

**前半段，准备请求。**

源码：[`packages/agent/src/agent-loop.ts:390-422`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L390-L422)

```ts
async function streamAssistantResponse(
	context: AgentContext,
	config: AgentLoopConfig,
	signal: AbortSignal | undefined,
	emit: AgentEventSink,
	streamFunction: StreamFn,
): Promise<AssistantMessage> {
	// Apply context transform if configured (AgentMessage[] → AgentMessage[])
	let messages = context.messages;
	if (config.transformContext) {
		messages = await config.transformContext(messages, signal);
	}

	// Convert to LLM-compatible messages (AgentMessage[] → Message[])
	const llmMessages = await config.convertToLlm(messages);

	const llmContext = normalizeContext({ messages: llmMessages });

	// Resolve API key (important for expiring tokens)
	const resolvedApiKey =
		(config.getApiKey ? await config.getApiKey(config.model.provider) : undefined) || config.apiKey;

	const response = await streamFunction(config.model, llmContext, {
		...config,
		apiKey: resolvedApiKey,
		identity: mergeClientIdentity(DEFAULT_AGENT_IDENTITY, config.identity),
		signal,
	});
	// Record the requested level, whichever stream function answered.
	const result = async () => Object.assign(await response.result(), { thinkingLevel: config.reasoning ?? "off" });

	let partialMessage: AssistantMessage | null = null;
	let addedPartial = false;
```

**讲解**

这是 Agent 消息变成 LLM 消息的地方，也是 README §3.1 第二个箭头的位置：

1. `transformContext`（可选）：`AgentMessage[] → AgentMessage[]`。
2. `convertToLlm`（必填）：`AgentMessage[] → Message[]`。
3. `normalizeContext`（→ M1）：把 `Message[]` 包成 `TranscriptContext`。这里没有传 `systemPrompt` 和 `tools`，系统提示词和工具都已经在 `system` 消息里了。
4. `getApiKey`：每次请求都重新取 key。注释说明了原因：工具可能执行很久，OAuth token 会在这期间过期。
5. 调用 `streamFunction`，把整个 config 作为 options 传下去，加上 key、身份信息和中止信号。

`result()` 在 provider 返回的最终消息上补一个 `thinkingLevel` 字段，记录本次请求的思考级别。§2.2 里 assistant 消息末尾的 `"thinkingLevel":"off"` 就是这里加的。

**后半段，消费事件流。**

源码：[`packages/agent/src/agent-loop.ts:424-479`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L424-L479)

```ts
	for await (const event of response) {
		switch (event.type) {
			case "start":
				partialMessage = event.partial;
				context.messages.push(partialMessage);
				addedPartial = true;
				await emit({ type: "message_start", message: { ...partialMessage } });
				break;

			case "text_start":
			case "text_delta":
			case "text_end":
			case "thinking_start":
			case "thinking_delta":
			case "thinking_end":
			case "toolcall_start":
			case "toolcall_delta":
			case "toolcall_end":
				if (partialMessage) {
					partialMessage = event.partial;
					context.messages[context.messages.length - 1] = partialMessage;
					await emit({
						type: "message_update",
						assistantMessageEvent: event,
						message: { ...partialMessage },
					});
				}
				break;

			case "done":
			case "error": {
				const finalMessage = await result();
				if (addedPartial) {
					context.messages[context.messages.length - 1] = finalMessage;
				} else {
					context.messages.push(finalMessage);
				}
				if (!addedPartial) {
					await emit({ type: "message_start", message: { ...finalMessage } });
				}
				await emit({ type: "message_end", message: finalMessage });
				return finalMessage;
			}
		}
	}

	const finalMessage = await result();
	if (addedPartial) {
		context.messages[context.messages.length - 1] = finalMessage;
	} else {
		context.messages.push(finalMessage);
		await emit({ type: "message_start", message: { ...finalMessage } });
	}
	await emit({ type: "message_end", message: finalMessage });
	return finalMessage;
}
```

**讲解**

M1 的 `AssistantMessageEvent` 每个都带着 `partial`，即到目前为止拼好的 assistant 消息。这段代码把它们翻译成 AgentEvent：

| provider 事件 | 对 `context.messages` 的操作 | 发出的 AgentEvent |
|---|---|---|
| `start` | push partial | `message_start` |
| `text_*`、`thinking_*`、`toolcall_*` | 用新 partial 替换最后一条 | `message_update`，原事件放在 `assistantMessageEvent` |
| `done`、`error` | 用最终消息替换最后一条 | `message_end` |

所以流式接收期间，context 的最后一条消息一直是"正在生长"的 assistant 消息。发给监听器的是 `{ ...partialMessage }` 浅拷贝。

没收到 `start` 就直接收到 `done`/`error` 时（`addedPartial` 为 `false`），补发 `message_start`。事件流结束时既没有 `done` 也没有 `error`，470–478 行做同样的收尾。这两处保证每条 assistant 消息都有一对 `message_start`/`message_end`。

注意 `error` 事件不会抛异常：它和 `done` 走同一个分支，返回一条 `stopReason` 为 `"error"` 的消息，交给 §4.5 ② 的 254 行处理。

**往哪走**：streamFn 内部 → M1。回到 `runLoop`，读 §4.7。

### 4.7 failToolCallsFromTruncatedMessage()

**调用位置**：`runLoop()` 277–278 行，`stopReason` 为 `"length"` 且有工具调用时。

源码：[`packages/agent/src/agent-loop.ts:488-513`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L488-L513)

```ts
async function failToolCallsFromTruncatedMessage(
	toolCalls: AgentToolCall[],
	emit: AgentEventSink,
): Promise<ExecutedToolCallBatch> {
	const messages: ToolResultMessage[] = [];
	for (const toolCall of toolCalls) {
		await emit({
			type: "tool_execution_start",
			toolCallId: toolCall.id,
			toolName: toolCall.name,
			args: toolCall.arguments,
		});
		const finalized: FinalizedToolCallOutcome = {
			toolCall,
			result: createErrorToolResult(
				`Tool call "${toolCall.name}" was not executed: the response hit the output token limit, so its arguments may be truncated. Re-issue the tool call with complete arguments.`,
			),
			isError: true,
		};
		await emitToolExecutionEnd(finalized, emit);
		const toolResultMessage = createToolResultMessage(finalized);
		await emitToolResultMessage(toolResultMessage, emit);
		messages.push(toolResultMessage);
	}
	return { messages, terminate: false };
}
```

**讲解**

`"length"` 表示模型输出撞到了 token 上限被截断。工具调用的参数是流式拼出来的 JSON，M1 会尽力解析不完整的 JSON（函数上方 481–487 行的注释称之为 "best-effort JSON salvage parser"），所以截断后的参数可能能解析、能通过校验，但内容不完整。例如 `write` 工具的文件内容只写了一半。

这里的做法是一个都不执行。每个工具调用照常发出 `tool_execution_start`/`tool_execution_end`，结果是一条错误，让模型重新发起。`terminate: false`，所以循环会继续，把错误结果交给模型。

§7.2 的实验验证了这个行为。

**往哪走**：§4.8 `executeToolCalls`。

### 4.8 executeToolCalls()

**调用位置**：`runLoop()` 279 行，有工具调用且 `stopReason` 不是 `"length"` 时。

源码：[`packages/agent/src/agent-loop.ts:518-538`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L518-L538)

```ts
async function executeToolCalls(
	currentContext: AgentContext,
	assistantMessage: AssistantMessage,
	config: AgentLoopConfig,
	signal: AbortSignal | undefined,
	emit: AgentEventSink,
): Promise<ExecutedToolCallBatch> {
	const toolCalls = assistantMessage.content.filter((c) => c.type === "toolCall");
	const hasSequentialToolCall = toolCalls.some(
		(tc) => currentContext.tools?.find((t) => t.name === tc.name)?.executionMode === "sequential",
	);
	if (config.toolExecution === "sequential" || hasSequentialToolCall) {
		return executeToolCallsSequential(currentContext, assistantMessage, toolCalls, config, signal, emit);
	}
	return executeToolCallsParallel(currentContext, assistantMessage, toolCalls, config, signal, emit);
}

type ExecutedToolCallBatch = {
	messages: ToolResultMessage[];
	terminate: boolean;
};
```

源码：[`packages/agent/src/agent-loop.ts:700-702`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L700-L702)

```ts
function shouldTerminateToolBatch(finalizedCalls: FinalizedToolCallOutcome[]): boolean {
	return finalizedCalls.length > 0 && finalizedCalls.every((finalized) => finalized.result.terminate === true);
}
```

**讲解**

选择执行模式：config 设为 `"sequential"`，或者这批调用里有任何一个工具声明了 `executionMode: "sequential"`，就整批串行；否则并行。工具的声明是"一票否决"：一个工具要求串行，同批的其他工具也跟着串行。§7.1 的 `mixed` 场景验证了这一点。

返回的 `ExecutedToolCallBatch` 有两个字段：`messages` 是 `toolResult` 消息，`terminate` 由 `shouldTerminateToolBatch` 计算，只有整批结果的 `terminate` 都为 `true` 时才为 `true`。`runLoop` 281 行用它设置 `hasMoreToolCalls = !terminate`。

**往哪走**：§4.9 串行，§4.10 并行。

### 4.9 executeToolCallsSequential()

**调用位置**：`executeToolCalls()`。

源码：[`packages/agent/src/agent-loop.ts:540-594`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L540-L594)

```ts
async function executeToolCallsSequential(
	currentContext: AgentContext,
	assistantMessage: AssistantMessage,
	toolCalls: AgentToolCall[],
	config: AgentLoopConfig,
	signal: AbortSignal | undefined,
	emit: AgentEventSink,
): Promise<ExecutedToolCallBatch> {
	const finalizedCalls: FinalizedToolCallOutcome[] = [];
	const messages: ToolResultMessage[] = [];

	for (const toolCall of toolCalls) {
		await emit({
			type: "tool_execution_start",
			toolCallId: toolCall.id,
			toolName: toolCall.name,
			args: toolCall.arguments,
		});

		const preparation = await prepareToolCall(currentContext, assistantMessage, toolCall, config, signal);
		let finalized: FinalizedToolCallOutcome;
		if (preparation.kind === "immediate") {
			finalized = {
				toolCall,
				result: preparation.result,
				isError: preparation.isError,
			};
		} else {
			const executed = await executePreparedToolCall(preparation, signal, emitToolExecutionUpdate(toolCall, emit));
			finalized = await finalizeExecutedToolCall(
				currentContext,
				assistantMessage,
				preparation,
				executed,
				config,
				signal,
			);
		}

		await emitToolExecutionEnd(finalized, emit);
		const toolResultMessage = createToolResultMessage(finalized);
		await emitToolResultMessage(toolResultMessage, emit);
		finalizedCalls.push(finalized);
		messages.push(toolResultMessage);

		if (signal?.aborted) {
			break;
		}
	}

	return {
		messages,
		terminate: shouldTerminateToolBatch(finalizedCalls),
	};
}
```

**讲解**

每个工具调用依次走完整条流水线，再开始下一个：

```
tool_execution_start → prepareToolCall → executePreparedToolCall → finalizeExecutedToolCall
→ tool_execution_end → toolResult 的 message_start/message_end
```

`prepareToolCall` 返回 `immediate` 时（工具不存在、参数无效、被拦截），跳过执行和 `afterToolCall`，直接用它的结果。

每个调用结束后检查中止信号，已中止就不再执行后面的调用。被跳过的调用没有 `toolResult` 消息。

**往哪走**：§4.11 `prepareToolCall`。

### 4.10 executeToolCallsParallel()

**调用位置**：`executeToolCalls()`，默认模式。

**第一段，逐个准备。**

源码：[`packages/agent/src/agent-loop.ts:596-627`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L596-L627)

```ts
async function executeToolCallsParallel(
	currentContext: AgentContext,
	assistantMessage: AssistantMessage,
	toolCalls: AgentToolCall[],
	config: AgentLoopConfig,
	signal: AbortSignal | undefined,
	emit: AgentEventSink,
): Promise<ExecutedToolCallBatch> {
	const finalizedCalls: FinalizedToolCallEntry[] = [];

	for (const toolCall of toolCalls) {
		await emit({
			type: "tool_execution_start",
			toolCallId: toolCall.id,
			toolName: toolCall.name,
			args: toolCall.arguments,
		});

		const preparation = await prepareToolCall(currentContext, assistantMessage, toolCall, config, signal);
		if (preparation.kind === "immediate") {
			const finalized = {
				toolCall,
				result: preparation.result,
				isError: preparation.isError,
			} satisfies FinalizedToolCallOutcome;
			await emitToolExecutionEnd(finalized, emit);
			finalizedCalls.push(finalized);
			if (signal?.aborted) {
				break;
			}
			continue;
		}
```

**第二段，并发执行，按原顺序收集。**

源码：[`packages/agent/src/agent-loop.ts:629-670`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L629-L670)

```ts
		finalizedCalls.push(async () => {
			if (signal?.aborted) {
				const finalized = {
					toolCall,
					result: createErrorToolResult("Operation aborted"),
					isError: true,
				} satisfies FinalizedToolCallOutcome;
				await emitToolExecutionEnd(finalized, emit);
				return finalized;
			}
			const executed = await executePreparedToolCall(preparation, signal, emitToolExecutionUpdate(toolCall, emit));
			const finalized = await finalizeExecutedToolCall(
				currentContext,
				assistantMessage,
				preparation,
				executed,
				config,
				signal,
			);
			await emitToolExecutionEnd(finalized, emit);
			return finalized;
		});
		if (signal?.aborted) {
			break;
		}
	}

	const orderedFinalizedCalls = await Promise.all(
		finalizedCalls.map((entry) => (typeof entry === "function" ? entry() : Promise.resolve(entry))),
	);
	const messages: ToolResultMessage[] = [];
	for (const finalized of orderedFinalizedCalls) {
		const toolResultMessage = createToolResultMessage(finalized);
		await emitToolResultMessage(toolResultMessage, emit);
		messages.push(toolResultMessage);
	}

	return {
		messages,
		terminate: shouldTerminateToolBatch(orderedFinalizedCalls),
	};
}
```

**讲解**

并行模式分三步：

1. **逐个准备**（606–654 行的 for 循环）：对每个调用发出 `tool_execution_start`，调用 `prepareToolCall`。`beforeToolCall` hook 在这一步按顺序执行，所以权限确认之类的交互不会同时弹出多个。`immediate` 的结果立即发出 `tool_execution_end`；`prepared` 的调用被包成一个闭包放进 `finalizedCalls`，还没开始执行。
2. **并发执行**（656–658 行）：`Promise.all` 同时调用所有闭包。每个闭包执行完立即发出自己的 `tool_execution_end`，所以 `tool_execution_end` 的顺序是完成顺序。
3. **按原顺序发出结果消息**（659–664 行）：`Promise.all` 返回的数组保持 `finalizedCalls` 的顺序，也就是 assistant 消息里工具调用的顺序。`toolResult` 消息按这个顺序发出。

这样，界面能尽早看到每个工具的完成，而 transcript 里结果的顺序是确定的，和执行快慢无关。

闭包开头再检查一次中止信号（630 行）：准备阶段中止后，已准备的调用不再执行，直接得到 "Operation aborted" 错误。

`ToolExecutionMode`（`packages/agent/src/types.ts:47`）上方的注释写的就是这个顺序约定。§7.1 的 `parallel` 场景验证了它。

**往哪走**：§4.11 `prepareToolCall`。

### 4.11 prepareToolCall()

**调用位置**：串行和并行执行都调用它；`runToolCall`（§4.19）也调用它。

**第一段，找工具、校验参数、调用 beforeToolCall。**

源码：[`packages/agent/src/agent-loop.ts:718-747`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L718-L747)

```ts
async function prepareToolCall(
	currentContext: AgentContext,
	assistantMessage: AssistantMessage,
	toolCall: AgentToolCall,
	config: ToolCallHooks,
	signal: AbortSignal | undefined,
	tools: readonly AgentTool<any>[] = currentContext.tools ?? [],
): Promise<PreparedToolCall | ImmediateToolCallOutcome> {
	const tool = tools.find((t) => t.name === toolCall.name);
	if (!tool) {
		return {
			kind: "immediate",
			result: createErrorToolResult(`Tool ${toolCall.name} not found`),
			isError: true,
		};
	}

	try {
		const preparedToolCall = prepareToolCallArguments(tool, toolCall);
		const validatedArgs = validateToolArguments(tool, preparedToolCall);
		if (config.beforeToolCall) {
			const beforeResult = await config.beforeToolCall(
				{
					assistantMessage,
					toolCall,
					args: validatedArgs,
					context: currentContext,
				},
				signal,
			);
```

**第二段，根据 hook 结果和中止信号决定 prepared 还是 immediate。**

源码：[`packages/agent/src/agent-loop.ts:748-787`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L748-L787)

```ts
			if (signal?.aborted) {
				return {
					kind: "immediate",
					result: createErrorToolResult("Operation aborted"),
					isError: true,
				};
			}
			if (beforeResult?.block) {
				const result = createErrorToolResult(beforeResult.reason || "Tool execution was blocked");
				if (beforeResult.terminate === true) {
					result.terminate = true;
				}
				return {
					kind: "immediate",
					result,
					isError: true,
				};
			}
		}
		if (signal?.aborted) {
			return {
				kind: "immediate",
				result: createErrorToolResult("Operation aborted"),
				isError: true,
			};
		}
		return {
			kind: "prepared",
			toolCall,
			tool,
			args: validatedArgs,
		};
	} catch (error) {
		return {
			kind: "immediate",
			result: createErrorToolResult(error instanceof Error ? error.message : String(error)),
			isError: true,
		};
	}
}
```

**讲解**

`prepareToolCall` 的结果有两种（类型定义在 `packages/agent/src/agent-loop.ts:672` 和 `packages/agent/src/agent-loop.ts:679`）：

- `prepared`：可以执行，带着找到的工具和校验过的参数。
- `immediate`：不执行，直接带着结果。

依次检查：

1. 按名字找工具（726 行）。找不到 → `immediate`，"Tool xxx not found"。
2. `prepareArguments` 改写参数（`prepareToolCallArguments`，`packages/agent/src/agent-loop.ts:704`；工具没有定义 `prepareArguments` 时原样返回），`validateToolArguments`（→ M1，`packages/ai/src/utils/validation.ts:317`）按 schema 校验（736–737 行）。校验失败会抛异常，被 780 行的 catch 变成 `immediate` 错误。
3. 调用 `beforeToolCall` hook（738–747 行）。hook 返回 `{ block: true }` → `immediate` 错误，`reason` 是错误文本。`terminate: true` 会被带到结果上。
4. 每个 await 之后都检查中止信号。

所有失败都被编码成 `isError: true` 的结果，不抛异常。模型下一轮会看到这个错误，自己决定怎么办。

**真实运行。** `m2-agent.ts block` 场景让 `beforeToolCall` 返回 `{ block: true, reason: "被 beforeToolCall 拦截", terminate: true }`：

```bash
node --conditions=source --experimental-strip-types m2-agent.ts block
```

```
[prepareRequest] AgentContext.messages role： ["system","note","user"]
[prepareRequest] AgentContext.tools： ["greet"]
[prepareRequest] thinkingLevel： "off"
[模型] 第 1 次请求，收到的 LLM 消息 role：system, user
[事件] tool_execution_start pendingToolCalls=["c1"]
[beforeToolCall] toolCall： {"type":"toolCall","id":"c1","name":"greet","arguments":{"name":"pi"}}
[beforeToolCall] args： {"name":"pi"}
[事件] tool_execution_end pendingToolCalls=[]
[事件] tool_execution_end： {"type":"tool_execution_end","toolCallId":"c1","toolName":"greet","result":{"content":[{"type":"text","text":"被 beforeToolCall 拦截"}],"details":{},"terminate":true},"isError":true}
[事件] agent_end，监听器开始等待 100ms，isStreaming=true
[事件] agent_end 监听器结束，isStreaming=true
[prompt 返回] isStreaming=false 模型请求次数=1
[AgentState] 运行后 messages role： ["system","note","user","assistant","toolResult"]
[AgentState] toolResult 消息： {"role":"toolResult","toolCallId":"c1","toolName":"greet","content":[{"type":"text","text":"被 beforeToolCall 拦截"}],"details":{},"isError":true,"timestamp":1793515172991}
```

工具没有执行（没有 `[afterToolCall]` 行），`tool_execution_end` 的结果是错误，没有 `durationMs`。因为这一批只有一个调用，并且设了 `terminate`，`shouldTerminateToolBatch` 返回 `true`，循环没有再请求模型（模型请求次数=1），transcript 以 `toolResult` 结尾。

**往哪走**：§4.12 `executePreparedToolCall`。

### 4.12 executePreparedToolCall()

**调用位置**：串行模式 `packages/agent/src/agent-loop.ts:568`，并行模式的闭包 `packages/agent/src/agent-loop.ts:639`，以及 `runToolCall`。

源码：[`packages/agent/src/agent-loop.ts:831-867`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L831-L867)

```ts
async function executePreparedToolCall(
	prepared: PreparedToolCall,
	signal: AbortSignal | undefined,
	onUpdate: ToolUpdateSink,
): Promise<ExecutedToolCallOutcome> {
	const updateEvents: Promise<void>[] = [];
	let acceptingUpdates = true;
	const startedAt = performance.now();
	const elapsed = () => Math.round(performance.now() - startedAt);

	try {
		const result = await prepared.tool.execute(
			prepared.toolCall.id,
			prepared.args as never,
			signal,
			(partialResult) => {
				if (!acceptingUpdates) return;
				updateEvents.push(Promise.resolve(onUpdate(partialResult)));
			},
		);
		const durationMs = elapsed();
		acceptingUpdates = false;
		await Promise.all(updateEvents);
		return { result, isError: result.isError === true, durationMs };
	} catch (error) {
		const durationMs = elapsed();
		acceptingUpdates = false;
		await Promise.all(updateEvents);
		return {
			result: createErrorToolResult(error instanceof Error ? error.message : String(error)),
			isError: true,
			durationMs,
		};
	} finally {
		acceptingUpdates = false;
	}
}
```

**讲解**

842 行调用 `tool.execute()`，这是 M3 的入口（→ M3）。

传给工具的进度回调把每次调用包成 `tool_execution_update` 事件（`emitToolExecutionUpdate`，`packages/agent/src/agent-loop.ts:789`），并收集这些事件的 Promise。工具结束后先 `acceptingUpdates = false`，丢弃迟到的进度更新，再等所有已发出的更新事件处理完。这保证 `tool_execution_update` 一定在 `tool_execution_end` 之前被处理完。

工具抛异常 → catch 成错误结果。工具返回 `isError: true` → 同样是错误结果，但保留了 `details`。两种情况都记录 `durationMs`，用 `performance.now()` 计时。

**往哪走**：§4.13 `finalizeExecutedToolCall`。

### 4.13 finalizeExecutedToolCall()

**调用位置**：紧跟在 `executePreparedToolCall()` 之后。

源码：[`packages/agent/src/agent-loop.ts:869-920`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L869-L920)

```ts
async function finalizeExecutedToolCall(
	currentContext: AgentContext,
	assistantMessage: AssistantMessage,
	prepared: PreparedToolCall,
	executed: ExecutedToolCallOutcome,
	config: ToolCallHooks,
	signal: AbortSignal | undefined,
): Promise<FinalizedToolCallOutcome> {
	let result = executed.result;
	let isError = executed.isError;

	if (config.afterToolCall) {
		try {
			const afterResult = await config.afterToolCall(
				{
					assistantMessage,
					toolCall: prepared.toolCall,
					args: prepared.args,
					result,
					isError,
					context: currentContext,
				},
				signal,
			);
			if (afterResult) {
				// Structured content not replaced along with the content may no longer match it.
				const structuredContent =
					afterResult.structuredContent ?? (afterResult.content ? undefined : result.structuredContent);
				result = {
					...result,
					content: afterResult.content ?? result.content,
					details: afterResult.details ?? result.details,
					usage: afterResult.usage ?? result.usage,
					terminate: afterResult.terminate ?? result.terminate,
				};
				if (structuredContent === undefined) delete result.structuredContent;
				else result.structuredContent = structuredContent;
				isError = afterResult.isError ?? isError;
			}
		} catch (error) {
			result = createErrorToolResult(error instanceof Error ? error.message : String(error));
			isError = true;
		}
	}

	return {
		toolCall: prepared.toolCall,
		result,
		isError,
		durationMs: executed.durationMs,
	};
}
```

**讲解**

调用 `afterToolCall` hook，按字段覆盖结果：hook 返回了哪个字段就替换哪个，没返回的保留。`content`、`details`、`usage` 都是整体替换，不做深合并。

`structuredContent` 有一条特殊规则（894–896 行）：hook 替换了 `content` 却没给 `structuredContent` 时，丢弃原来的 `structuredContent`，因为它可能和新的 `content` 对不上了。

hook 抛异常时，结果变成错误。和 `prepareToolCall` 一样，失败不会传到循环外面。

**往哪走**：§4.14 `createToolResultMessage`。

### 4.14 createToolResultMessage() 和两个 emit 辅助函数

**调用位置**：串行模式每个调用结束后，并行模式全部结束后按顺序调用。

源码：[`packages/agent/src/agent-loop.ts:929-959`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L929-L959)

```ts
async function emitToolExecutionEnd(finalized: FinalizedToolCallOutcome, emit: AgentEventSink): Promise<void> {
	await emit({
		type: "tool_execution_end",
		toolCallId: finalized.toolCall.id,
		toolName: finalized.toolCall.name,
		result: finalized.result,
		isError: finalized.isError,
		...(finalized.durationMs === undefined ? {} : { durationMs: finalized.durationMs }),
	});
}

function createToolResultMessage(finalized: FinalizedToolCallOutcome): ToolResultMessage {
	return {
		role: "toolResult",
		toolCallId: finalized.toolCall.id,
		toolName: finalized.toolCall.name,
		// Untyped tools (JS extensions) can return results without content; normalize
		// so the null never enters session history or provider payloads.
		content: finalized.result.content ?? [],
		details: finalized.result.details,
		usage: finalized.result.usage,
		isError: finalized.isError,
		...(finalized.durationMs === undefined ? {} : { durationMs: finalized.durationMs }),
		timestamp: Date.now(),
	};
}

async function emitToolResultMessage(toolResultMessage: ToolResultMessage, emit: AgentEventSink): Promise<void> {
	await emit({ type: "message_start", message: toolResultMessage });
	await emit({ type: "message_end", message: toolResultMessage });
}
```

**讲解**

`tool_execution_end` 事件带着完整的 `result`（`AgentToolResult`）。`toolResult` 消息只取 `content`、`details`、`usage`、`isError`、`durationMs`，不带 `structuredContent` 和 `terminate`。§2.2 里两者的差别就来自这里。

`content ?? []` 处理 JS 扩展写的工具不返回 `content` 的情况，保证 transcript 和发给 provider 的请求里不出现 `null`。

`toolResult` 消息是 M1 的 `ToolResultMessage`，它和 assistant 消息里的 `toolCall` 通过 `toolCallId` 对应。

前面各处出现的错误结果都由 `createErrorToolResult`（`packages/agent/src/agent-loop.ts:922`）生成：一段文本 `content` 加空的 `details`。`isError` 不写在结果里，而是放在外层的 outcome 上。

**往哪走**：回到 `runLoop`，§4.15。

### 4.15 runLoop() ③ 工具执行和轮的结束

源码：[`packages/agent/src/agent-loop.ts:267-308`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L267-L308)

```ts
			// Check for tool calls
			const toolCalls = message.content.filter((c) => c.type === "toolCall");

			const toolResults: ToolResultMessage[] = [];
			hasMoreToolCalls = false;
			if (toolCalls.length > 0) {
				// A "length" stop means the output was cut off by the token limit, so
				// every tool call in the message may carry truncated arguments. Fail
				// them all instead of executing potentially borked calls.
				const executedToolBatch =
					message.stopReason === "length"
						? await failToolCallsFromTruncatedMessage(toolCalls, emit)
						: await executeToolCalls(currentContext, message, config, signal, emit);
				toolResults.push(...executedToolBatch.messages);
				hasMoreToolCalls = !executedToolBatch.terminate;

				for (const result of toolResults) {
					currentContext.messages.push(result);
					newMessages.push(result);
				}
			}

			lastCompletedTurn = {
				message,
				toolResults,
				context: currentContext,
				newMessages,
			};
			const decision = await config.finishTurn?.(lastCompletedTurn, signal);
			await emit({ type: "turn_end", message, toolResults });

			if (decision?.action === "end") {
				await emit({ type: "agent_end", messages: newMessages });
				return;
			}

			explicitContinuation = decision?.action === "continue";
			pendingMessages = (await config.getSteeringMessages?.()) || [];
			if (hasMoreToolCalls || pendingMessages.length > 0) {
				explicitContinuation = false;
			}
		}
```

**讲解**

268 行从 assistant 消息里找出所有 `toolCall` 块。有工具调用时，按 `stopReason` 选择 §4.7 或 §4.8，把结果 push 进 context 和 `newMessages`。没有工具调用时，`hasMoreToolCalls` 保持 `false`。

一轮结束时：

1. 调用 `finishTurn` hook（295 行）。此时 assistant 消息和所有工具结果都已发出。
2. 发出 `turn_end`。
3. `finishTurn` 返回 `{ action: "end" }` → 立即发出 `agent_end` 并返回，不再轮询任何队列。
4. 轮询 steering 队列（304 行）。这是第二个 steering 轮询点：工具执行完、`turn_end` 之后。用户在本轮执行期间的插话，在这里被取出，下一轮开始时（220 行）作为 user 消息加入 transcript，然后请求模型。
5. `finishTurn` 返回 `{ action: "continue" }` 时，记下 `explicitContinuation`。如果已经有工具结果或 steering 消息要交给模型，这次请求本来就会发生，所以清掉标记（305–307 行），不会多请求一次。

steering 消息不会打断正在执行的工具。`AgentLoopConfig.getSteeringMessages` 的注释写明了 "Tool calls from the current assistant message are not skipped"。

`Agent` 一侧，`steer()` 把消息放进 steering 队列，这里的轮询通过 `createLoopConfig` 里的 `getSteeringMessages` 把它取出来（§4.2）。

**往哪走**：内层循环条件不满足时，§4.16。

### 4.16 runLoop() ④ follow-up、显式继续和退出

源码：[`packages/agent/src/agent-loop.ts:310-330`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L310-L330)

```ts
		// Agent would stop here. Check for follow-up messages.
		const followUpMessages = (await config.getFollowUpMessages?.()) || [];
		if (followUpMessages.length > 0) {
			// Set as pending so inner loop processes them
			explicitContinuation = false;
			pendingMessages = followUpMessages;
			continue;
		}

		// No natural request was selected, so fulfill the continuation decision with one context-only turn.
		if (explicitContinuation) {
			explicitContinuation = false;
			continue;
		}

		// No more messages, exit
		break;
	}

	await emit({ type: "agent_end", messages: newMessages });
}
```

**讲解**

内层循环退出意味着：上一轮没有工具调用（或被 terminate），也没有 steering 消息。agent 本来要停了。这时：

1. 轮询 follow-up 队列（311 行）。有消息就设为待处理，`continue` 回到外层循环开头，`hasMoreToolCalls` 重新设为 `true`，再进内层循环。follow-up 和 steering 的区别就在轮询时机：steering 每轮结束都取，follow-up 只在 agent 要停时取。
2. 没有 follow-up，但 `finishTurn` 要求过继续（320 行）：不加任何消息，再请求一次模型。注释叫它 "context-only turn"。
3. 都没有，跳出外层循环，发出 `agent_end`（329 行）。

到这里，`runLoop` 的三个出口都读完了：

| 出口 | 行号 | 条件 |
|---|---|---|
| 错误或中止 | 263 | `stopReason` 为 `"error"` 或 `"aborted"` |
| hook 结束 | 299 | `finishTurn` 返回 `{ action: "end" }` |
| 自然结束 | 329 | 没有工具调用、没有 steering、没有 follow-up、没有显式继续 |

三个出口都发出 `agent_end`，带着 `newMessages`。

**往哪走**：循环发出的每个事件都会进入 `Agent.processEvents()`，§4.17。

### 4.17 Agent.processEvents()

**调用位置**：`runPromptMessages()` 传给循环的事件回调 `(event) => this.processEvents(event)`。循环里每个 `await emit(...)` 都会等它返回。

源码：[`packages/agent/src/agent.ts:575-622`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L575-L622)

```ts
	private async processEvents(event: AgentEvent): Promise<void> {
		switch (event.type) {
			case "message_start":
				this._state.streamingMessage = event.message;
				break;

			case "message_update":
				this._state.streamingMessage = event.message;
				break;

			case "message_end":
				this._state.streamingMessage = undefined;
				this._state.messages.push(event.message);
				break;

			case "tool_execution_start": {
				const pendingToolCalls = new Set(this._state.pendingToolCalls);
				pendingToolCalls.add(event.toolCallId);
				this._state.pendingToolCalls = pendingToolCalls;
				break;
			}

			case "tool_execution_end": {
				const pendingToolCalls = new Set(this._state.pendingToolCalls);
				pendingToolCalls.delete(event.toolCallId);
				this._state.pendingToolCalls = pendingToolCalls;
				break;
			}

			case "turn_end":
				if (event.message.role === "assistant" && event.message.errorMessage) {
					this._state.errorMessage = event.message.errorMessage;
				}
				break;

			case "agent_end":
				this._state.streamingMessage = undefined;
				break;
		}

		const signal = this.activeRun?.abortController.signal;
		if (!signal) {
			throw new Error("Agent listener invoked outside active run");
		}
		for (const listener of this.listeners) {
			await listener(event, signal);
		}
	}
```

**讲解**

`processEvents` 先更新 `Agent` 自己的状态，再依次 await 每个订阅者：

| 事件 | 状态变化 |
|---|---|
| `message_start`、`message_update` | `streamingMessage` = 当前消息 |
| `message_end` | 清空 `streamingMessage`，消息 push 进 `_state.messages` |
| `tool_execution_start` / `_end` | 往 `pendingToolCalls` 加入/删除 id。每次换一个新 Set，方便界面框架发现变化 |
| `turn_end` | assistant 消息带 `errorMessage` 时记到 `_state.errorMessage` |
| `agent_end` | 清空 `streamingMessage` |

所以 `_state.messages` 只在 `message_end` 时增长。流式接收期间，正在生长的消息在 `streamingMessage` 里，不在 `messages` 里。

订阅者按订阅顺序串行 await（619–621 行），而循环又 await `processEvents`。合起来，一个慢的订阅者会让整个循环停下来等它。M8 的 `_handleAgentEvent` 在 `message_end` 时写会话文件（→ M8 → M4），这个 await 链保证了消息先落盘，循环才继续下一步。§7.3 对比了不 await 的情况。

订阅者拿到的第二个参数是本次运行的中止信号。

**往哪走**：§4.18 hook 契约。

### 4.18 types.ts 的 hook 契约

§4.5–4.16 读到了每个 hook 的调用位置。types.ts 的注释规定了每个 hook 的契约，上层（M8、M7）必须遵守。下面是最影响循环流程的三个：`finishTurn`、`prepareRequest`、`prepareNextTurn`。

源码：[`packages/agent/src/types.ts:256-280`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L256-L280)

```ts
	/**
	 * Called after the assistant message and all tool-result messages have been emitted, immediately before `turn_end`.
	 * `{ action: "end" }` ends the run without polling queues or preparing another request.
	 * On a normal turn, `{ action: "continue" }` ensures one next provider request. Tool-result, steering, or
	 * follow-up scheduling can satisfy that request and adds no extra request; otherwise the loop continues once
	 * with the current context. Returning undefined preserves normal scheduling. Error and aborted responses remain
	 * hard exits.
	 */
	finishTurn?: FinishTurn;

	/**
	 * Called immediately before every conversational provider request, including the first.
	 * Pending messages have already been appended. The returned context, model, and thinking level
	 * replace the runtime values for this and later requests in the run. This hook does not poll queues.
	 */
	prepareRequest?: PrepareRequest;

	/**
	 * Called after `turn_end` when the loop will continue, immediately before the next turn starts.
	 * Return replacement context/model/thinking state or messages to append to affect that turn.
	 * Return undefined to keep using the current context/config.
	 */
	prepareNextTurn?: (
		context: PrepareNextTurnContext,
	) => AgentLoopTurnUpdate | undefined | Promise<AgentLoopTurnUpdate | undefined>;
```

`StreamFn` 的契约最重要，它决定了错误怎么在系统里流动：

源码：[`packages/agent/src/types.ts:19-37`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/types.ts#L19-L37)

```ts
/**
 * Stream function used by the agent loop. `Models.streamSimple` satisfies
 * this shape.
 *
 * The loop passes a normalized transcript: the system prompt and tool
 * declarations are carried by the transcript's system messages, never by
 * `context.systemPrompt` or `context.tools`.
 *
 * Contract:
 * - Must not throw or return a rejected promise for request/model/runtime failures.
 * - Must return an AssistantMessageEventStream.
 * - Failures must be encoded in the returned stream via protocol events and a
 *   final AssistantMessage with stopReason "error" or "aborted" and errorMessage.
 */
export type StreamFn = (
	model: Model<Api>,
	context: TranscriptContext,
	options?: SimpleStreamOptions,
) => AssistantMessageEventStream | Promise<AssistantMessageEventStream>;
```

把各处的契约汇总起来：

| hook | 契约 | 违反时的后果 |
|---|---|---|
| `streamFn` | 不抛异常；失败编码为 `stopReason: "error"/"aborted"` 的最终消息 | 异常冒出循环，`handleRunFailure` 合成一条错误消息 |
| `convertToLlm`、`transformContext`、`getApiKey`、队列回调 | 不抛异常，返回安全的默认值 | 同上，注释说 "interrupts the low-level agent loop without producing a normal event sequence" |
| `beforeToolCall`、`afterToolCall` | 自己处理中止信号 | 抛异常会被 catch 成工具错误（`packages/agent/src/agent-loop.ts:780`、`packages/agent/src/agent-loop.ts:908`） |
| `finishTurn` | 错误和中止的轮次里返回值被忽略 | 无 |
| `prepareRequest` | 不轮询队列，只替换 context/模型/思考级别 | 无 |

循环里的 `try/catch` 只包住了工具相关的 hook。其他 hook 抛异常时，循环中断，由 `runWithLifecycle` 兜底（§4.3）。

**往哪走**：§4.19 读完剩余的代码。

### 4.19 旁支：agentLoop、runToolCall、stream-fn.ts、proxy.ts

这些代码不在主线场景的调用路径上，但属于本模块。

**agentLoop()**：`runAgentLoop` 的事件流版本。

源码：[`packages/agent/src/agent-loop.ts:47-70`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L47-L70)

```ts
export function agentLoop(
	prompts: AgentMessage[],
	context: AgentContext,
	config: AgentLoopConfig,
	signal: AbortSignal | undefined,
	streamFn: StreamFn,
): EventStream<AgentEvent, AgentMessage[]> {
	const stream = createAgentStream();

	void runAgentLoop(
		prompts,
		context,
		config,
		async (event) => {
			stream.push(event);
		},
		signal,
		streamFn,
	).then((messages) => {
		stream.end(messages);
	});

	return stream;
}
```

它把 `emit` 换成"push 进 `EventStream`"（M1 的 `packages/ai/src/utils/event-stream.ts:26`），立即返回这个流，调用方用 `for await` 消费。push 不等消费者处理，所以循环不会被慢的消费者拖住，但消费者看到的事件会落后于循环的实际进度。`Agent` 类用的是 `runAgentLoop`，coding-agent 没有用 `agentLoop`。§7.3 对比了两者。`agentLoopContinue` 和 `runAgentLoopContinue`（`packages/agent/src/agent-loop.ts:80`、`packages/agent/src/agent-loop.ts:137`）是不带新 prompt、从当前 transcript 继续的版本，用于重试。它们要求最后一条消息不是 assistant 消息。

**runToolCall()**：让工具内部发起的工具调用也走同一条流水线。

源码：[`packages/agent/src/agent-loop.ts:821-829`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L821-L829)

```ts
export async function runToolCall(toolCall: AgentToolCall, options: RunToolCallOptions): Promise<AgentToolCallOutcome> {
	const { assistantMessage, context, signal } = options;
	const preparation = await prepareToolCall(context, assistantMessage, toolCall, options, signal, options.tools);
	if (preparation.kind === "immediate") {
		return { toolCall, result: preparation.result, isError: preparation.isError };
	}
	const executed = await executePreparedToolCall(preparation, signal, options.onUpdate ?? (() => {}));
	return finalizeExecutedToolCall(context, assistantMessage, preparation, executed, options, signal);
}
```

它依次调用 `prepareToolCall`、`executePreparedToolCall`、`finalizeExecutedToolCall`，所以 `beforeToolCall` 里的权限检查对嵌套调用同样有效。它不发事件，也不加消息。M3 的嵌套工具调用会用到它（→ M3）。

**stream-fn.ts**：全局默认 `streamFn`。文件只有一个模块级变量和两个函数：`setDefaultStreamFn`（`packages/agent/src/stream-fn.ts:11`）设置它，`getDefaultStreamFn`（`packages/agent/src/stream-fn.ts:15`）读取它，没设置时抛错。`runAgentLoop` 的 `streamFn` 参数为空时用它（`packages/agent/src/agent-loop.ts:133`），`Agent` 构造时没传 `streamFn` 也用它。

pi-agent-core 不依赖任何 provider，所以默认 `streamFn` 由宿主设置。coding-agent 在 `packages/coding-agent/src/core/sdk.ts:46` 调用 `setDefaultStreamFn(streamSimple)`。不过主线场景用不到这个默认值：coding-agent 创建 `Agent` 时显式传了 `streamFn`（`packages/coding-agent/src/core/sdk.ts:412`），它最终调用 `modelRuntime.streamSimple()`（→ M8、M1）。

**proxy.ts**：通过 HTTP 代理调用模型的 `streamFn`。

`streamProxy`（`packages/agent/src/proxy.ts:106`）把 model、context 和可序列化的 options POST 到 `${proxyUrl}/api/stream`，读取 SSE 响应。服务器为节省带宽，发来的事件不带 `partial`（`ProxyAssistantMessageEvent`，`packages/agent/src/proxy.ts:22`），客户端在 `processProxyEvent`（`packages/agent/src/proxy.ts:258`）里维护一个本地的 `partial` 消息，每收到一个事件就更新它，再把它附在事件上。文本增量直接拼到对应的 `text` 块上；工具调用参数的增量用 M1 的 `ToolCallArgumentsStream` 拼接解析：

源码：[`packages/agent/src/proxy.ts:341-355`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/proxy.ts#L341-L355)

```ts
		case "toolcall_delta": {
			const content = partial.content[proxyEvent.contentIndex];
			const args = toolArguments.get(proxyEvent.contentIndex);
			if (content?.type === "toolCall" && args !== undefined) {
				// A new block object, so reactive consumers see the change.
				partial.content[proxyEvent.contentIndex] = { ...content, arguments: args.append(proxyEvent.delta) };
				return {
					type: "toolcall_delta",
					contentIndex: proxyEvent.contentIndex,
					delta: proxyEvent.delta,
					partial,
				};
			}
			throw new Error("Received toolcall_delta for non-toolCall content");
		}
```

`streamProxy` 遵守 `StreamFn` 契约：HTTP 错误、解析错误、中止都在 catch 里变成 `error` 事件；连接在没有 `done`/`error` 的情况下关闭，也补发一个 `error` 事件：

源码：[`packages/agent/src/proxy.ts:220-244`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/proxy.ts#L220-L244)

```ts
			if (!sawTerminalEvent) {
				// A clean EOF without a done/error event means the server dropped the
				// response mid-stream. Surface it as an error instead of leaving
				// consumers waiting on a result that never arrives.
				partial.stopReason = "error";
				partial.errorMessage = "Connection closed by proxy server before the response completed";
				stream.push({
					type: "error",
					reason: "error",
					error: partial,
				});
			}

			stream.end();
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			const reason = options.signal?.aborted ? "aborted" : "error";
			partial.stopReason = reason;
			partial.errorMessage = errorMessage;
			stream.push({
				type: "error",
				reason,
				error: partial,
			});
			stream.end();
```

（文件里 `streamProxy` 的 JSDoc 注释放在了 `buildProxyRequestOptions`（`packages/agent/src/proxy.ts:90`）上面，读源码时不要被误导。）

---

## 5. 状态机

### 5.1 一次运行的生命周期

```
            prompt() / continue()
  ┌──────┐  activeRun 为空            ┌─────────────────────────┐
  │ idle │ ─────────────────────────▶ │ running                 │
  │      │  agent.ts:527 设 activeRun  │ isStreaming = true      │
  └──────┘  agent.ts:529              │ （循环在执行，事件陆续发出）│
     ▲                                └────────────┬────────────┘
     │                                             │ 循环发出 agent_end
     │                                             │ agent-loop.ts:263 / 299 / 329
     │                                             │ 或循环抛异常 → handleRunFailure
     │                                             │ agent.ts:536、557
     │                                             ▼
     │                                ┌─────────────────────────┐
     │  finishRun()                   │ settling                │
     └─────────────────────────────── │ agent_end 监听器在执行    │
        agent.ts:560-566              │ isStreaming 仍为 true    │
        isStreaming = false           │ agent.ts:619-621        │
        activeRun = undefined         └─────────────────────────┘
        resolve waitForIdle()

  running / settling 期间再调用 prompt()：抛错 agent.ts:383-387
  running 期间 abort()：agent.ts:350-352，信号传给 streamFn、工具和 hook
```

### 5.2 双层循环

```
 runAgentLoop: agent_start, turn_start, prompt 消息 (agent-loop.ts:126-131)
        │
        ▼  轮询 steering (185)
 ┌─ 外层 while(true) (188) ── hasMoreToolCalls = true (189) ─────────────────────────┐
 │  ┌─ 内层 while (hasMoreToolCalls || pending) (192) ────────────────────────────┐ │
 │  │  [第 2 轮起] prepareNextTurn (195) → 轮询 steering (214) → turn_start (216) │ │
 │  │  发出待处理消息 (220)                                                       │ │
 │  │  prepareRequest (228)                                                       │ │
 │  │  streamAssistantResponse (251)                                              │ │
 │  │     ├─ error / aborted ───────────────────────────▶ turn_end, agent_end (263) │
 │  │     ├─ 有 toolCall，stopReason=length → 全部报错 (278)                       │ │
 │  │     ├─ 有 toolCall → executeToolCalls (279)；hasMoreToolCalls = !terminate   │ │
 │  │     └─ 无 toolCall → hasMoreToolCalls = false (271)                          │ │
 │  │  finishTurn (295) → turn_end (296)                                          │ │
 │  │     ├─ action=end ─────────────────────────────────────▶ agent_end (299)      │
 │  │  轮询 steering (304)                                                        │ │
 │  └── 条件成立 → 下一轮 ─────────────────────────────────────────────────────────┘ │
 │  条件不成立：                                                                     │
 │    轮询 follow-up (311)，有 → pending = followUp，continue (316) ──▶ 外层开头       │
 │    explicitContinuation (320) → continue (322) ─────────────────▶ 外层开头          │
 │    否则 break (326)                                                              │
 └──────────────────────────────────────────────────────────────────────────────────┘
        ▼
 agent_end (329)
```

### 5.3 一个工具调用的结果

```
              tool_execution_start (agent-loop.ts:552 / 607)
                         │
                   prepareToolCall (718)
        ┌────────────────┴────────────────────────────┐
        │ 工具不存在 (727)                              │ 找到工具、参数通过校验、
        │ 参数校验失败 → catch (780)                    │ 未被拦截、未中止
        │ beforeToolCall 返回 block (755)              │ (774)
        │ 已中止 (748 / 767)                           ▼
        ▼                                   ┌─────────────────┐
 ┌─────────────┐                            │ prepared        │
 │ immediate   │                            └────────┬────────┘
 │ isError=true│                                     │ executePreparedToolCall (831)
 └──────┬──────┘                                     ▼ tool.execute (842)
        │                                   ┌─────────────────┐
        │                                   │ executed        │ 抛异常 → isError (855)
        │                                   │ 带 durationMs   │ 返回 isError (854)
        │                                   └────────┬────────┘
        │                                            │ finalizeExecutedToolCall (869)
        │                                            ▼ afterToolCall 覆盖字段 (882-906)
        │                                   ┌─────────────────┐
        │                                   │ finalized       │
        │                                   └────────┬────────┘
        └──────────────────┬─────────────────────────┘
                           ▼
           tool_execution_end (929) → createToolResultMessage (940)
           → message_start / message_end (956-959)

 stopReason=length 时不经过 prepareToolCall：直接得到错误结果 (agent-loop.ts:500-506)
 并行模式下，准备阶段之后中止：闭包直接返回 "Operation aborted" (630-638)
```

### 5.4 一条消息的事件

```
 assistant 消息（流式）:
   provider start ──▶ message_start (430)
        │ text_* / thinking_* / toolcall_*
        ▼
   message_update × N (445)       Agent: streamingMessage = partial (agent.ts:582)
        │ provider done / error
        ▼
   message_end (464)              Agent: messages.push, streamingMessage = undefined (agent.ts:586-587)

 assistant 消息（没收到 start 就收到 done/error）:
   message_start (462) ──▶ message_end (464)

 system / user / toolResult 消息:
   message_start ──▶ message_end，中间没有 update
   (agent-loop.ts:129-130、221-222、957-958)
```

---

## 6. 设计要点

### 6.1 循环和产品分离：所有产品逻辑通过 hook 接入

**做法。** `agent-loop.ts` 是一组无状态函数，不读文件、不知道扩展。会话持久化、压缩、系统提示词重建、权限检查都在 M8 里实现，通过 `AgentLoopConfig` 的 hook 和事件订阅接入。M8 安装 hook 的方式是给 `Agent` 的公开字段赋值，并保留之前的值以便串联，例如 `_installAgentBoundaryHooks`：

源码：[`packages/coding-agent/src/core/agent-session.ts:902-912`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L902-L912)

```ts
	private _installAgentBoundaryHooks(): void {
		const previousFinishTurn = this.agent.finishTurn;
		this.agent.finishTurn = async (turn, signal) => {
			this._boundaryDispatchedMessages.add(turn.message);
			const extensionContinue = await this._dispatchTurnEndBoundary(turn.message, turn.toolResults);
			const previousDecision = await previousFinishTurn?.(turn, signal);
			if (previousDecision?.action === "end") return previousDecision;
			if (extensionContinue || previousDecision?.action === "continue") return { action: "continue" };
			return undefined;
		};
	}
```

**理由。** 循环的规则（什么时候请求、什么时候停、工具怎么执行）可以单独测试，§2.3 的实验就是例子：不需要会话文件和扩展，几十行代码就能驱动整个循环。上层换实现，例如 durable 包（→ M11），也能复用同一套规则。

**代码位置。** `packages/agent/src/types.ts:193`（hook 定义），`packages/agent/src/agent.ts:476`（`Agent` 把字段装进 config）。

### 6.2 工具清单写进 transcript

**做法。** 工具声明不作为请求的单独参数，而是写在 `system` 消息的 `toolsAdded`/`toolsRemoved` 里。`declareToolChanges` 在每次请求前比较 transcript 声明的工具和 `context.tools`，有差异就写一条新的声明。

**理由。** 工具清单的变化和消息一样有了时间顺序，会话文件能重放出任意时刻模型看到的工具。工具清单在中途变化时，只追加一条新的 `system` 消息，之前的 transcript 不需要改写。各家 provider 怎么把这些增量变成请求里的工具列表，见 M1 的 `resolveTranscriptTools`。

**代码位置。** `packages/agent/src/agent-loop.ts:342`。

### 6.3 事件回调被 await

**做法。** 循环里每个 `emit` 都是 `await emit(...)`，`Agent.processEvents` 又依次 await 每个订阅者。`agent_end` 的订阅者执行完之前，`isStreaming` 一直是 `true`。

**理由。** 上层在事件里做的事有先后要求：消息要先写进会话文件，下一次请求才能从会话文件重建上下文（§4.5 ②）。await 让循环等上层做完，这个顺序就不需要额外的同步机制。代价是慢的订阅者会拖慢循环。需要不阻塞的场合可以用 `agentLoop` 的事件流版本（§4.19）。

**代码位置。** `packages/agent/src/agent.ts:619`，`packages/agent/src/agent-loop.ts:47`（`agentLoop` 的不阻塞版本）。

### 6.4 并行执行，按原顺序写结果

**做法。** 并行模式先按顺序准备（`beforeToolCall` 依次执行），再并发执行，`tool_execution_end` 按完成顺序发出，`toolResult` 消息按 assistant 消息里的顺序发出。任一工具声明 `executionMode: "sequential"`，整批串行。

**理由。** 按顺序准备，权限确认不会同时弹出多个；完成就发 `tool_execution_end`，界面反馈及时；结果按原顺序写入 transcript，同样的模型输出总是产生同样的 transcript，和工具执行快慢无关。

**代码位置。** `packages/agent/src/agent-loop.ts:596`。

### 6.5 失败是数据，不是异常

**做法。** 模型错误编码为 `stopReason: "error"` 的消息；工具找不到、参数无效、被拦截、抛异常，都变成 `isError: true` 的工具结果；输出被截断时工具调用全部报错。唯一的异常兜底是 `handleRunFailure`，它也把异常变成一条错误消息和完整的事件序列。

**理由。** 错误留在 transcript 里，模型下一轮能看到并自行纠正，例如参数校验失败后换个参数重试。订阅者看到的事件序列总是完整的，总以 `agent_end` 结束，不需要单独处理异常路径。

**代码位置。** `packages/agent/src/types.ts:19`（StreamFn 契约），`packages/agent/src/agent-loop.ts:718`，`packages/agent/src/agent.ts:542`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行。时间戳（毫秒）每次运行会有几毫秒的差别。

### 7.1 并行、串行和工具自己要求串行

**问题。** 模型一次返回两个工具调用 `slow`（300ms）和 `fast`（50ms）。三种配置下，`tool_execution_end` 和 `toolResult` 消息的顺序分别是什么？总耗时大约多少？

- `parallel`：`toolExecution` 为默认的 `"parallel"`。
- `sequential`：`toolExecution` 设为 `"sequential"`。
- `mixed`：`toolExecution` 仍为 `"parallel"`，但 `fast` 工具声明了 `executionMode: "sequential"`（`m2-loop.ts` 里只改了工具定义这一处）。

**预测。**

- `parallel`：两个 `tool_execution_start` 紧挨着发出；`fast` 的 `tool_execution_end` 先到（约 50ms），`slow` 后到（约 300ms）；`toolResult` 消息按源顺序 `slow, fast`（§4.10）。总耗时约 300ms。
- `sequential`：`slow` 的 start、end、toolResult 全部结束后才开始 `fast`（§4.9）。总耗时约 350ms。
- `mixed`：`executeToolCalls` 的一票否决规则（§4.8）让整批串行，结果和 `sequential` 一样。

**运行。**

```bash
node --conditions=source --experimental-strip-types m2-loop.ts parallel
```

```
场景：parallel（runAgentLoop + await emit）
   3ms  agent_start
   3ms  turn_start
   3ms  message_start (system)
   3ms  message_end (system)
   3ms  message_start (user) "开始"
   3ms  message_end (user) "开始"
   3ms  [模型] 收到 2 条消息，返回 stopReason=toolUse
   4ms  message_start (assistant)
   4ms  message_end (assistant)
   4ms  tool_execution_start slow
   5ms  tool_execution_start fast
  57ms  tool_execution_end fast isError=false
 306ms  tool_execution_end slow isError=false
 306ms  message_start (toolResult) slow isError=false
 306ms  message_end (toolResult) slow isError=false
 306ms  message_start (toolResult) fast isError=false
 306ms  message_end (toolResult) fast isError=false
 306ms  turn_end (assistant)
 306ms  turn_start
 306ms  [模型] 收到 5 条消息，返回 stopReason=stop
 306ms  message_start (assistant)
 306ms  message_end (assistant)
 306ms  turn_end (assistant)
 306ms  agent_end
结束：本次运行新增 6 条消息：system, user, assistant, toolResult, toolResult, assistant
```

```bash
node --conditions=source --experimental-strip-types m2-loop.ts sequential
```

```
…省略…
   3ms  message_end (assistant)
   3ms  tool_execution_start slow
 306ms  tool_execution_end slow isError=false
 306ms  message_start (toolResult) slow isError=false
 306ms  message_end (toolResult) slow isError=false
 306ms  tool_execution_start fast
 361ms  tool_execution_end fast isError=false
 361ms  message_start (toolResult) fast isError=false
 361ms  message_end (toolResult) fast isError=false
…省略…
```

```bash
node --conditions=source --experimental-strip-types m2-loop.ts mixed
```

```
…省略…
   5ms  message_end (assistant)
   5ms  tool_execution_start slow
 307ms  tool_execution_end slow isError=false
 307ms  message_start (toolResult) slow isError=false
 307ms  message_end (toolResult) slow isError=false
 307ms  tool_execution_start fast
 359ms  tool_execution_end fast isError=false
 359ms  message_start (toolResult) fast isError=false
 359ms  message_end (toolResult) fast isError=false
…省略…
```

**结论。** 三个预测都成立。`parallel` 里 `tool_execution_end` 的顺序是 `fast, slow`，`toolResult` 消息的顺序是 `slow, fast`。`mixed` 和 `sequential` 的输出相同，只是 `mixed` 的 config 里写的是并行。

### 7.2 输出被截断时的工具调用

**问题。** 模型回复的 `stopReason` 是 `"length"`，回复里有一个 `fast` 工具调用。工具会执行吗？循环会停吗？

**预测。** 工具不执行，`tool_execution_start` 和 `tool_execution_end` 之间没有 50ms 的间隔；结果 `isError=true`，错误文本里有 "was not executed: the response hit the output token limit"（§4.7）；`terminate` 为 `false`，循环继续，第 2 次请求带着错误结果，模型收到 4 条消息。

**运行。**

```bash
node --conditions=source --experimental-strip-types m2-loop.ts length
```

```
场景：length（runAgentLoop + await emit）
   3ms  agent_start
   3ms  turn_start
   3ms  message_start (system)
   3ms  message_end (system)
   3ms  message_start (user) "开始"
   3ms  message_end (user) "开始"
   6ms  [模型] 收到 2 条消息，返回 stopReason=length
   6ms  message_start (assistant)
   6ms  message_end (assistant)
   6ms  tool_execution_start fast
   6ms  tool_execution_end fast isError=true "Tool call \"fast\" was not executed: the response hit the outp"…
   6ms  message_start (toolResult) fast isError=true
   6ms  message_end (toolResult) fast isError=true
   6ms  turn_end (assistant)
   6ms  turn_start
   6ms  [模型] 收到 4 条消息，返回 stopReason=stop
   6ms  message_start (assistant)
   6ms  message_end (assistant)
   6ms  turn_end (assistant)
   6ms  agent_end
结束：本次运行新增 5 条消息：system, user, assistant, toolResult, assistant
```

**结论。** `tool_execution_start` 和 `tool_execution_end` 在同一毫秒，工具没有执行。错误文本是 `Tool call "fast" was not executed: the response hit the output token limit, …`（脚本只打印前 60 个字符）。第 2 次请求收到 4 条消息：system、user、被截断的 assistant、错误的 toolResult。

### 7.3 steering 的轮询时机，以及不 await 的事件消费

**问题 1。** `steer` 场景里，`getSteeringMessages` 第 1 次返回空，第 2 次返回一条插话。轮询发生在哪些时刻？插话在什么时候被模型看到？

**预测。** 轮询点有三个：`runLoop` 开头（185 行）、每轮 `turn_end` 之后（304 行）、第 2 轮起 `prepareNextTurn` 之后但仅在没有待处理消息时（214 行）。

- 第 1 次轮询在 `runLoop` 开头，在 prompt 消息发出之后、第 1 次请求之前。
- 第 2 次在第 1 轮 `turn_end` 之后，取到插话。因为已经有待处理消息，214 行不再轮询。
- 第 2 轮 `turn_start` 之后先发出插话的 user 消息，再请求模型。模型收到 5 条消息：system、user、assistant、toolResult、插话。
- 第 2 轮没有工具调用，`turn_end` 后第 3 次轮询（304 行）返回空，循环结束。脚本里的第 3 条回复"多余"不会被用到。

**运行。**

```bash
node --conditions=source --experimental-strip-types m2-loop.ts steer
```

```
场景：steer（runAgentLoop + await emit）
   3ms  agent_start
   3ms  turn_start
   3ms  message_start (system)
   3ms  message_end (system)
   3ms  message_start (user) "开始"
   3ms  message_end (user) "开始"
   3ms  [steering] 第 1 次轮询
   4ms  [模型] 收到 2 条消息，返回 stopReason=toolUse
   4ms  message_start (assistant)
   4ms  message_end (assistant)
   4ms  tool_execution_start slow
 307ms  tool_execution_end slow isError=false
 307ms  message_start (toolResult) slow isError=false
 307ms  message_end (toolResult) slow isError=false
 307ms  turn_end (assistant)
 307ms  [steering] 第 2 次轮询
 307ms  turn_start
 307ms  message_start (user) "插话：顺便看下测试"
 307ms  message_end (user) "插话：顺便看下测试"
 307ms  [模型] 收到 5 条消息，返回 stopReason=stop
 307ms  message_start (assistant)
 307ms  message_end (assistant)
 307ms  turn_end (assistant)
 307ms  [steering] 第 3 次轮询
 307ms  agent_end
结束：本次运行新增 6 条消息：system, user, assistant, toolResult, user, assistant
```

**结论。** 三次轮询的位置和预测一致。插话没有打断 `slow` 工具（它照常执行了 300ms），在下一次请求之前进入 transcript。

**问题 2。** `STREAM=1` 时脚本改用 `agentLoop` + `for await` 消费事件（§4.19），其余不变。和 `basic` 场景的默认输出（§2.3）相比，打印顺序有什么不同？

**预测。** `agentLoop` 的 emit 只是 push 进队列，不等消费者。循环会先跑到 `streamFn`，打印 `[模型] 收到 2 条消息`，消费者才打印出排在它前面的 `message_start (user)`。在默认模式（`runAgentLoop` + await emit）下，`[模型]` 一行一定在 `message_end (user)` 之后。

**运行。**

```bash
STREAM=1 node --conditions=source --experimental-strip-types m2-loop.ts basic
```

```
场景：basic（agentLoop + for await）
   3ms  agent_start
   3ms  turn_start
   3ms  message_start (system)
   3ms  message_end (system)
   4ms  [模型] 收到 2 条消息，返回 stopReason=toolUse
   4ms  message_start (user) "开始"
   4ms  message_end (user) "开始"
   4ms  message_start (assistant)
   4ms  message_end (assistant)
   5ms  tool_execution_start fast
  56ms  tool_execution_end fast isError=false
  57ms  message_start (toolResult) fast isError=false
  57ms  message_end (toolResult) fast isError=false
  57ms  turn_end (assistant)
  57ms  turn_start
  57ms  [模型] 收到 4 条消息，返回 stopReason=stop
  57ms  message_start (assistant)
  57ms  message_end (assistant)
  57ms  turn_end (assistant)
  57ms  agent_end
结束：本次运行新增 5 条消息：system, user, assistant, toolResult, assistant
```

**结论。** `[模型] 收到 2 条消息` 出现在 `message_start (user)` 之前，而 §2.3 的默认模式里它在 `message_end (user)` 之后。消费者看到的事件落后于循环的实际进度。如果消费者要在 `message_end` 时写会话文件，用 `agentLoop` 就无法保证"先落盘、再请求"。这就是 `Agent` 类用 `runAgentLoop` 的原因（§6.3）。

---

## 8. 自测与衔接

### 8.1 自测

1. 主线场景里，`finishTurn` 被调用几次？`prepareNextTurn` 被调用几次？`prepareRequest` 呢？
2. 一次请求里模型返回了 3 个工具调用，`beforeToolCall` 拦截了其中 1 个并设置 `terminate: true`，另外 2 个正常执行。循环会停吗？
3. 用户在工具执行期间调用 `agent.followUp(msg)`，而这一轮工具执行完后模型又发起了新的工具调用。`msg` 什么时候进入 transcript？

<details>
<summary>答案</summary>

1. 主线场景有 2 轮。`finishTurn` 每轮调用一次（`packages/agent/src/agent-loop.ts:295`），共 2 次。`prepareNextTurn` 只在第 2 轮起调用（194 行的 `if (lastCompletedTurn)`），共 1 次。`prepareRequest` 每次请求前调用（228 行），共 2 次。
2. 不会停。`shouldTerminateToolBatch`（`packages/agent/src/agent-loop.ts:700`）要求整批结果的 `terminate` 都为 `true`。另外 2 个正常执行的结果没有设置 `terminate`，所以 `hasMoreToolCalls` 为 `true`，循环把 3 个结果（含 1 个拦截错误）交给模型。
3. 只要还有工具调用或 steering 消息，内层循环就继续，follow-up 队列不会被轮询。`msg` 要等到某一轮模型不再调用工具、steering 队列也为空时，在 311 行被取出，然后作为新一轮的消息发给模型。

</details>

### 8.2 衔接

本模块读到 `executePreparedToolCall` 里的 `prepared.tool.execute(...)`（`packages/agent/src/agent-loop.ts:842`）就停了。M3 从 `tool.execute()` 开始，读 coding-agent 的内置工具（read、bash、edit、write）怎么实现 `AgentTool` 接口，以及 `ToolDefinition` 怎么包装成 `AgentTool`。

本模块里标了"→ M1"的 `streamFn`、`normalizeContext`、`validateToolArguments`，在 M1 从 `ModelRuntime.streamSimple()` 读起；标了"→ M8"的 hook 安装，在 M8 从 `createAgentSession()` 读起。
