# M7 扩展：事件派发与拦截

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

扩展（extension）是一个 TypeScript 文件，默认导出一个函数 `(pi: ExtensionAPI) => void`。pi 启动时加载这个文件、调用这个函数。函数里用 `pi.on(事件名, handler)` 订阅事件，用 `pi.registerTool()`、`pi.registerCommand()`、`pi.registerProvider()` 等方法注册东西。

`core/extensions` 目录做三件事：

- **加载**（`loader.ts`）：用 jiti 直接 import `.ts` 文件，拿到工厂函数，调用它，把它注册的 handler、工具、命令收进一个 `Extension` 对象。
- **派发**（`runner.ts`）：`ExtensionRunner` 持有所有 `Extension`，提供 `emit()` 和十几个 `emitXxx()` 方法。上层（M8 的 AgentSession）在合适的时刻调用它们。
- **契约**（`types.ts`）：事件类型、handler 返回值类型、`ExtensionAPI` 和 `ExtensionContext` 接口。

扩展本身不知道 agent 循环。它能影响循环，是因为 M8 把 M2 的 hook（`beforeToolCall`、`afterToolCall`、`transformContext`、`finishTurn`）接到了 `ExtensionRunner` 的方法上。本模块从其中最典型的一条读起：模型发起工具调用，`beforeToolCall` 调用 `ExtensionRunner.emitToolCall()`，扩展可以拦下这次调用。

主线场景用的假模型 `lab/faux-ext.ts` 本身就是一个扩展，它只调用了 `pi.registerProvider()`。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M7 出现在四处：

```
createAgentSessionServices()
└─ DefaultResourceLoader.reload()  加载扩展                      [M6 → M7 loader.ts]
new AgentSession()
└─ _buildRuntime(): new ExtensionRunner(...)、bindCore()        [M8 → M7 runner.ts]
runLoop()                                                        [M2]
├─ streamAssistantResponse()
│   └─ transformContext()  → runner.emitContext()               [M7] context 事件
├─ executeToolCalls()
│   ├─ prepareToolCall() → beforeToolCall()
│   │                     → runner.emitToolCall()              [M7] ← 本模块入口
│   └─ finalizeExecutedToolCall() → afterToolCall()
│                         → runner.emitToolResult()            [M7] tool_result 事件
├─ finishTurn() → runner.emitBoundary()                         [M7] turn_end 事件
└─ 每个 AgentEvent → _handleAgentEvent() → _emitExtensionEvent()
                    → runner.emit() / emitMessageEnd()          [M7] 通知类事件
```

### 1.3 文件清单

```
     226 src/core/extensions/index.ts
       3 src/core/extensions/jiti-loader.ts
       3 src/core/extensions/jiti-static-loader.ts
     879 src/core/extensions/loader.ts
    1565 src/core/extensions/runner.ts
    2328 src/core/extensions/types.ts
      41 src/core/extensions/virtual-modules.ts
      29 src/core/extensions/wrapper.ts
      34 examples/extensions/permission-gate.ts
    5108 total
```

| 文件 | 内容 | 阅读方式 |
|---|---|---|
| `src/core/extensions/runner.ts` | `ExtensionRunner`：派发事件、创建 `ExtensionContext`、把 `bindCore()` 传入的实现装进共享 runtime | 精读 `emit*` 方法和 `createContext`；快捷键冲突、命令重名处理略 |
| `src/core/extensions/loader.ts` | 用 jiti 加载扩展文件，创建 `ExtensionAPI`，发现扩展目录 | 精读 `createExtensionRuntime`、`createExtensionAPI`、`loadExtensionModule`、`loadExtension` |
| `src/core/extensions/types.ts` | 全部事件类型、结果类型、`ExtensionAPI`、`ExtensionContext` | 按需查，§3 和 §4.10 |
| `src/core/extensions/wrapper.ts` | 把扩展注册的 `ToolDefinition` 包成 `AgentTool`（→ M3 `tool-definition-wrapper.ts`） | 通读 |
| `src/core/extensions/jiti-loader.ts`、`jiti-static-loader.ts` | 各 3 行：两行注释，一行分别重新导出 `jiti` 和 `jiti/static` | 通读 |
| `src/core/extensions/virtual-modules.ts` | 扩展里 `import "@earendil-works/pi-ai"` 等包名对应的模块对象 | 通读 |
| `src/core/extensions/index.ts` | 重新导出 | 略 |
| `examples/extensions/permission-gate.ts` | 官方示例：拦截危险的 bash 命令 | 精读，§7 的实验对象 |

UI 相关的部分（`ExtensionUIContext` 的组件、渲染器、快捷键）属于 TUI，本模块不讲。

### 1.4 读完能回答的问题

1. 扩展的 `.ts` 文件没有经过编译，pi 是怎么加载它的？扩展里 `import "@earendil-works/pi-ai"` 拿到的是哪份代码？
2. 两个扩展都订阅了 `tool_call`，一个改参数、一个拦截，谁先执行？结果怎么合并？
3. 扩展的 handler 抛异常会怎样？为什么 `tool_call` 和 `tool_execution_start` 的处理不一样？
4. 41 个扩展事件名里，哪些只是通知，哪些能改变 agent 的流程？
5. print 模式没有界面，permission-gate 遇到 `rm -rf` 时会怎么做？

---

## 2. 先跑一次

### 2.1 主线场景里扩展收到的事件

[`lab/m7-trace-ext.ts`](lab/m7-trace-ext.ts) 订阅全部 41 个事件名，每收到一个事件就往 stderr 打一行，handler 都返回 `undefined`，不改变任何流程。和 `faux-ext.ts` 一起加载，运行主线场景：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m7-work-21 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-21 ./run.sh -e "$PWD/m7-trace-ext.ts" -p "读 README.md，总结成三句话" 2>&1
```

（`-e` 的相对路径按工作目录 `WORK` 解析，所以这里传绝对路径。）

```
Running without API keys...
[ext] session_start reason=startup mode=print hasUI=false
[ext] resources_discover reason=startup
[ext] input
[ext] before_agent_start messageType=user
[ext] agent_start
[ext] turn_start turnIndex=0
[ext] message_start system
[ext] message_end system
[ext] message_start user
[ext] message_end user
[ext] context messages=[user]
[ext] context_with_system messages=[system,user]
[ext] before_provider_headers
[ext] after_provider_response
[ext] message_start assistant
[ext] message_update text_start
[ext] message_update text_delta
[ext] message_update text_end
[ext] message_update toolcall_start
[ext] message_update toolcall_delta
[ext] message_update toolcall_delta
[ext] message_update toolcall_end
[ext] message_end assistant
[ext] tool_execution_start read
[ext] tool_call read
[ext] tool_result read
[ext] tool_execution_end read
[ext] message_start toolResult
[ext] message_end toolResult
[ext] turn_end turnIndex=0
[ext] turn_start turnIndex=1
[ext] context messages=[user,assistant,toolResult]
[ext] context_with_system messages=[system,user,assistant,toolResult]
[ext] before_provider_headers
[ext] after_provider_response
[ext] message_start assistant
[ext] message_update text_start
[ext] message_update text_delta
[ext] message_update text_delta
[ext] message_update text_delta
[ext] message_update text_end
[ext] message_end assistant
[ext] turn_end turnIndex=1
[ext] agent_end
[ext] agent_before_settle
[ext] agent_settled
[ext] session_shutdown reason=quit
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

`message_update` 的个数每次运行可能不同（见 README §5.2）。和 README §5.2 的 AgentEvent 序列对照，可以看到几件事：

- 扩展收到的事件比 AgentEvent 多。`session_start`、`input`、`before_agent_start` 在循环开始之前；`context`、`context_with_system`、`before_provider_headers`、`after_provider_response` 在每次请求模型时；`tool_call`、`tool_result` 夹在 `tool_execution_start` 和 `tool_execution_end` 之间；`agent_before_settle`、`agent_settled`、`session_shutdown` 在循环结束之后。
- `context` 事件里看不到 `system` 消息，`context_with_system` 里能看到。§4.7 会读到原因。
- `tool_call` 在 `tool_execution_start` **之后**。M2 的循环先发 `tool_execution_start`，再在 `prepareToolCall` 里调用 `beforeToolCall`（M2 §4.10、§4.11）。所以扩展拦截一个工具调用时，界面已经显示"开始执行"了。
- 41 个事件名里有一部分这次没有出现，例如 `session_before_compact`（没有压缩）、`tool_execution_update`（read 不报告进度）、`model_select`（没有切换模型）。

### 2.2 tool_call 和 tool_result 事件的真实内容

设置 `M7_DUMP` 后，`m7-trace-ext.ts` 把指定类型的事件对象打印成 JSON（`turn_end` 的 `context` 字段太长，只打印键名）：

```bash
M7_DUMP=tool_call,tool_result,turn_end WORK=/tmp/pi-lab/m7-work-22 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-22 ./run.sh -e "$PWD/m7-trace-ext.ts" -p "读 README.md，总结成三句话" 2>&1 | grep -A1 -E '^\[ext\] (tool_call|tool_result|turn_end)'
```

```
[ext] tool_call read
{"type":"tool_call","toolName":"read","toolCallId":"call_1","input":{"path":"README.md"}}
[ext] tool_result read
{"type":"tool_result","toolName":"read","toolCallId":"call_1","input":{"path":"README.md"},"content":[{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}],"structuredContent":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n","isError":false}
--
[ext] turn_end turnIndex=0
{"type":"turn_end","turnIndex":0,"message":{"role":"assistant","content":[{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_1","name":"read","arguments":{"path":"README.md"}}],"api":"faux-api","provider":"faux","model":"faux-1","usage":{"input":0,"output":10,"cacheRead":0,"cacheWrite":1311,"totalTokens":1321,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"toolUse","timestamp":1793528167443,"durationMs":1,"thinkingLevel":"off"},"toolResults":[{"role":"toolResult","toolCallId":"call_1","toolName":"read","content":[{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}],"isError":false,"durationMs":1,"timestamp":1793528167445}],"messageEntryId":"228815bd","toolResultEntryIds":["abb37a13"],"outcome":"completed","entries":[],"continue":false,"context":["contextEntries","contextMessages","llmMessages","pendingMessages","canContinue"]}
--
[ext] turn_end turnIndex=1
{"type":"turn_end","turnIndex":1,"message":{"role":"assistant","content":[{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}],"api":"faux-api","provider":"faux","model":"faux-1","usage":{"input":0,"output":9,"cacheRead":1311,"cacheWrite":36,"totalTokens":1356,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"stop","timestamp":1793528167446,"durationMs":0,"thinkingLevel":"off"},"toolResults":[],"messageEntryId":"8d799df3","toolResultEntryIds":[],"outcome":"completed","entries":[],"continue":false,"context":["contextEntries","contextMessages","llmMessages","pendingMessages","canContinue"]}
```

- `tool_call` 只有 `toolName`、`toolCallId`、`input` 三个有用字段。`input` 是**校验过的**参数，和 AgentEvent `tool_execution_start` 里的 `args`（模型给的原始参数）不是同一个对象。
- `tool_result` 带着工具的原始返回（`content`、`structuredContent`、`isError`），handler 可以改写它们。
- `turn_end` 比 AgentEvent 的 `turn_end` 多了 `turnIndex`、`messageEntryId`（assistant 消息在会话文件里的条目 id）、`entries`、`continue`、`context`。后三个字段让 handler 能往会话里追加条目、要求循环再请求一次（§4.9）。

---

## 3. 数据结构

### 3.1 Extension 和 LoadExtensionsResult：加载的产物

源码：[`packages/coding-agent/src/core/extensions/types.ts:2291-2317`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L2291-L2317)

```ts
/** Loaded extension with all registered items. */
export interface Extension {
	path: string;
	resolvedPath: string;
	hidden?: boolean;
	/** See {@link InlineExtension}. */
	replaceable?: boolean;
	sourceInfo: SourceInfo;
	handlers: Map<string, HandlerFn[]>;
	tools: Map<string, RegisteredTool>;
	messageRenderers: Map<string, MessageRenderer>;
	toolRenderers?: ToolRendererResolver[];
	markdownTransformer?: MarkdownTransformer;
	entryRenderers?: Map<string, EntryRenderer>;
	commands: Map<string, RegisteredCommand>;
	flags: Map<string, ExtensionFlag>;
	shortcuts: Map<KeyId, ExtensionShortcut>;
}

/** Result of loading extensions. */
export interface LoadExtensionsResult {
	extensions: Extension[];
	errors: Array<{ path: string; error: string }>;
	warnings?: Array<{ path: string; warning: string }>;
	/** Shared runtime - actions are throwing stubs until runner.initialize() */
	runtime: ExtensionRuntime;
}
```

`Extension` 是一个扩展文件加载后的全部注册内容。工厂函数调用 `pi.on()`、`pi.registerTool()` 等方法时，只是往这些 Map 里写东西。

| 字段 | 说明 |
|---|---|
| `path`、`resolvedPath` | 命令行或配置里写的路径，和解析后的绝对路径。内置扩展和 inline 扩展的 path 是 `builtin:mcp`、`<inline:name>` 这样的合成路径 |
| `hidden`、`replaceable` | 内置扩展用：不在启动列表里显示；别的扩展注册了同名工具或命令时让位 |
| `handlers` | 事件名 → handler 数组。同一个扩展可以对同一事件注册多个 handler，按注册顺序执行 |
| `tools` | 工具名 → `RegisteredTool`（`ToolDefinition` + 来源信息），之后由 `wrapper.ts` 包成 `AgentTool`（→ M3） |
| `commands`、`flags`、`shortcuts` | 斜杠命令、命令行参数、快捷键 |
| `messageRenderers` 等 | 界面渲染用，本模块不讲 |

`LoadExtensionsResult` 是一次加载的结果：成功的扩展、失败的路径和错误信息、所有扩展共享的一个 `runtime`。

**真实实例。** [`lab/m7-runner.ts`](lab/m7-runner.ts) 不启动 pi，直接调用 `loader.ts` 和 `runner.ts`。它需要 pi 自带的源码解析器（`pi-test.sh` 用的同一个）来解析 `@earendil-works/*` 包名。`load` 场景加载三个路径：permission-gate.ts、一个不存在的文件、一个没有默认导出函数的文件：

```bash
node --import "$PI/packages/coding-agent/src/experimental/source-resolver.ts" m7-runner.ts load
```

```
extensions:
  {"path":"/tmp/pi-mono/packages/coding-agent/examples/extensions/permission-gate.ts","handlers":{"tool_call":1},"tools":[],"commands":[],"flags":[],"shortcuts":[]}
errors:
  {"path":"/tmp/pi-lab/m7-runner/missing.ts","error":"Failed to load extension: Cannot find module '/tmp/pi-lab/m7-runner/missing.ts'\nRequire stack:\n- /private/tmp/pi-mono/packages/coding-agent/src/core/extensions/loader.ts"}
  {"path":"/tmp/pi-lab/m7-runner/no-factory.ts","error":"Extension does not export a valid factory function: /tmp/pi-lab/m7-runner/no-factory.ts"}
runtime 的键: sendMessage, sendUserMessage, appendEntry, setSessionName, getSessionName, setLabel, getActiveTools, getAllTools, getSettings, setActiveTools, refreshTools, getCommands, setModel, getThinkingLevel, setThinkingLevel, flagValues, pendingProviderRegistrations, pendingNativeProviderRegistrations, mcpServers, pendingVirtualModelRegistrations, createContext, assertActive, invalidate, trackEventBusSubscription, registerProvider, registerNativeProvider, unregisterProvider, registerVirtualModel, unregisterVirtualModel
bindCore 之前调用 runtime.sendMessage: Extension runtime not initialized. Action methods cannot be called during extension loading.
```

permission-gate 只注册了一个 `tool_call` handler。两个失败的路径进了 `errors`，没有抛异常，其余扩展照常加载。

### 3.2 ExtensionRuntime：所有扩展共享的动作表

`ExtensionRuntime`（`packages/coding-agent/src/core/extensions/types.ts:2289`）= `ExtensionRuntimeState` + `ExtensionActions`。它是一张函数表：`sendMessage`、`appendEntry`、`setActiveTools`、`setModel`……扩展调用 `pi.sendMessage()` 时，`ExtensionAPI` 转发给 `runtime.sendMessage()`。

关键在于它分两个阶段：

- **加载阶段**：`loader.ts` 用 `createExtensionRuntime()` 创建 runtime，动作全是抛错的桩函数（§4.4）。上面实例最后一行就是在这个阶段调用 `runtime.sendMessage` 的结果。`registerProvider` 这类调用不抛错，先排进 `pendingProviderRegistrations` 队列。
- **绑定之后**：AgentSession 创建 `ExtensionRunner` 后调用 `runner.bindCore(actions, contextActions)`，用真实实现覆盖这些桩函数，并把排队的 provider 注册一次性提交（`packages/coding-agent/src/core/extensions/runner.ts:417`）。

`faux-ext.ts` 在工厂函数里调用 `pi.registerProvider()` 能生效，靠的就是这个队列。

### 3.3 ExtensionEvent：事件联合类型

源码：[`packages/coding-agent/src/core/extensions/types.ts:1424-1457`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L1424-L1457)

```ts
/** Union of all event types */
export type ExtensionEvent =
	| ProjectTrustEvent
	| ResourcesDiscoverEvent
	| McpServersChangeEvent
	| SessionEvent
	| ContextEvent
	| ContextWithSystemEvent
	| CacheWarmingDecisionEvent
	| BeforeProviderRequestEvent
	| BeforeProviderHeadersEvent
	| AfterProviderResponseEvent
	| ProviderStreamEvent
	| BeforeAgentStartEvent
	| AgentStartEvent
	| AgentEndEvent
	| AgentBeforeSettleEvent
	| AgentSettledEvent
	| UIPromptStartEvent
	| UIPromptEndEvent
	| TurnStartEvent
	| TurnEndEvent
	| MessageStartEvent
	| MessageUpdateEvent
	| MessageEndEvent
	| ToolExecutionStartEvent
	| ToolExecutionUpdateEvent
	| ToolExecutionEndEvent
	| ModelSelectEvent
	| ThinkingLevelSelectEvent
	| UserBashEvent
	| InputEvent
	| ToolCallEvent
	| ToolResultEvent;
```

联合类型有 32 个成员。其中 `SessionEvent` 本身是 10 种 session 事件的联合，`ToolCallEvent` 和 `ToolResultEvent` 按工具名各细分出 9 种（`BashToolCallEvent`、`ReadToolCallEvent`……`CustomToolCallEvent`），但事件名都只有一个。`ExtensionAPI.on()` 的重载（`packages/coding-agent/src/core/extensions/types.ts:1621` 起）列出了全部 41 个事件名。README §3.2 写的"32 种事件类型"就是这个成员数。§4.10 按"能不能改变流程"把 41 个事件名分类。

以本模块入口的 `tool_call` 为例：

源码：[`packages/coding-agent/src/core/extensions/types.ts:1213-1223`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L1213-L1223)

```ts
interface ToolCallEventBase {
	type: "tool_call";
	/**
	 * The call's id. For calls another tool made (with `parentToolCallId` set), pi assigns
	 * `<parent id>/<n>`; such ids never appear as tool calls or tool results in the transcript, only
	 * in the parent result's `nestedCalls` record.
	 */
	toolCallId: string;
	/** Set when another tool (for example a codemode script) issued this call. */
	parentToolCallId?: string;
}
```

源码：[`packages/coding-agent/src/core/extensions/types.ts:1270-1285`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L1270-L1285)

```ts
/**
 * Fired before a tool executes. Can block.
 *
 * `event.input` is mutable. Mutate it in place to patch tool arguments before execution.
 * Later `tool_call` handlers see earlier mutations. No re-validation is performed after mutation.
 */
export type ToolCallEvent =
	| BashToolCallEvent
	| PowerShellToolCallEvent
	| ReadToolCallEvent
	| EditToolCallEvent
	| WriteToolCallEvent
	| GrepToolCallEvent
	| FindToolCallEvent
	| LsToolCallEvent
	| CustomToolCallEvent;
```

| 字段 | 说明 |
|---|---|
| `type` | 固定 `"tool_call"` |
| `toolCallId` | 工具调用 id。工具内部通过 `ctx.executeTool()` 发起的嵌套调用是 `<父 id>/<n>`（→ M3 嵌套调用） |
| `parentToolCallId` | 嵌套调用才有 |
| `toolName`、`input` | 工具名和校验过的参数。按 `toolName` 区分出 9 种子类型，内置工具的 `input` 有精确类型 |

注释里写着：`event.input` 可以原地修改，后面的 handler 看得到修改，**修改后不再校验**。§7.2 用实验验证这一点。

### 3.4 handler 和结果类型

源码：[`packages/coding-agent/src/core/extensions/types.ts:1609-1611`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L1609-L1611)

```ts
/** Handler function type for events */
// biome-ignore lint/suspicious/noConfusingVoidType: void allows bare return statements
export type ExtensionHandler<E, R = undefined> = (event: E, ctx: ExtensionContext) => Promise<R | void> | R | void;
```

handler 收到两个参数：事件和 `ExtensionContext`。返回值的类型 `R` 由事件决定，默认是 `undefined`，即通知类事件的返回值被忽略。`tool_call` 的返回值类型是：

源码：[`packages/coding-agent/src/core/extensions/types.ts:1474-1483`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L1474-L1483)

```ts
export interface ToolCallEventResult {
	/** Block tool execution. To modify arguments, mutate `event.input` in place instead. */
	block?: boolean;
	reason?: string;
	/**
	 * Hint that the agent should stop after the current tool batch when this call is blocked.
	 * Early termination only happens when every finalized tool result in the batch sets this to true.
	 */
	terminate?: boolean;
}
```

| 字段 | 说明 |
|---|---|
| `block` | `true` 时工具不执行 |
| `reason` | 拦截原因，会成为工具结果的文本，模型能看到 |
| `terminate` | 拦截时顺便请求本批工具执行完后停止循环。和 M2 的 `AgentToolResult.terminate` 一样，整批都设置才生效 |

注意这里没有"改参数"的字段。注释说得很明白：要改参数，原地修改 `event.input`。

其他会改变流程的事件也各有结果类型，§4.10 的表格列出了它们。

### 3.5 ExtensionContext：handler 的第二个参数

源码：[`packages/coding-agent/src/core/extensions/types.ts:345-385`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L345-L385)

```ts
export interface ExtensionContext {
	/** UI methods for user interaction */
	ui: ExtensionUIContext;
	/** Current run mode. Use "tui" to guard terminal-only UI such as custom components. */
	mode: ExtensionMode;
	/** Whether dialog-capable UI is available (true in TUI and RPC modes) */
	hasUI: boolean;
	/** Current working directory */
	cwd: string;
	/** Session manager (read-only) */
	sessionManager: ReadonlySessionManager;
	/** Model registry for API key resolution */
	modelRegistry: ModelRegistry;
	/** Current model (may be undefined) */
	model: Model<any> | undefined;
	/** Models scoped to this session (resolved from `--models` /
	 *  `enabledModels` settings against the available catalogue). Same set
	 *  the `/scoped-models` command shows. Empty when no scoping is
	 *  configured (all available models are usable). Read-only snapshot. */
	scopedModels: readonly ScopedModel[];
	/** Current thinking level, when provided by the session runtime. */
	thinkingLevel?: ThinkingLevel;
	/** Whether the agent is idle (not streaming) */
	isIdle(): boolean;
	/** Whether project-local trust is active for this context. */
	isProjectTrusted(): boolean;
	/** The current abort signal, or undefined when the agent is not streaming. */
	signal: AbortSignal | undefined;
	/** Abort the current agent operation */
	abort(): void;
	/** Whether there are queued messages waiting */
	hasPendingMessages(): boolean;
	/** Gracefully shutdown pi and exit. Available in all contexts. */
	shutdown(): void;
	/** Get current context usage for the active model. */
	getContextUsage(): ContextUsage | undefined;
	/** Trigger compaction without awaiting completion. */
	compact(options?: CompactOptions): void;
	/** Get the current effective system prompt. */
	getSystemPrompt(): string;
}
```

handler 通过 `ctx` 读取会话状态（`model`、`sessionManager`、`getContextUsage()`）和执行操作（`abort()`、`compact()`）。permission-gate 用到了其中两个：`ctx.hasUI` 和 `ctx.ui.select()`。

| 字段 | print 模式下的值 |
|---|---|
| `mode` | `"print"`（§2.1 的 `session_start` 行打印了它） |
| `hasUI` | `false`。只有 TUI 和 RPC 模式为 `true` |
| `ui` | 一组空实现：`select` 返回 `undefined`，`confirm` 返回 `false`（`noOpUIContext`，`packages/coding-agent/src/core/extensions/runner.ts:331`） |
| `sessionManager` | 只读视图 `ReadonlySessionManager`（→ M4） |

`ExtensionContext` 不是一个存好数据的对象，而是一组 getter，每次读取时才去调用 `bindCore()` 装进来的函数（§4.3）。

### 3.6 类型关系图

```
 扩展文件 permission-gate.ts
   export default (pi: ExtensionAPI) => { pi.on("tool_call", handler) }
        │ loader.ts: jiti.import() 取得 ExtensionFactory
        │ createExtensionAPI(extension, runtime) → 调用工厂
        ▼
 Extension { handlers: Map<事件名, handler[]>, tools, commands, flags… }
        │ 多个扩展 + 一个共享的 ExtensionRuntime
        ▼
 LoadExtensionsResult { extensions[], errors[], runtime }
        │ AgentSession._buildRuntime(): new ExtensionRunner(...) + bindCore()
        ▼
 ExtensionRunner ──createContext()──▶ ExtensionContext（getter，读 bindCore 装入的函数）
        │ emitToolCall(ToolCallEvent)
        │   按扩展加载顺序 → 按注册顺序调用 handler(event, ctx)
        ▼
 ToolCallEventResult { block, reason, terminate } | undefined
        │ AgentSession._beforeToolCall 原样返回
        ▼
 M2 的 BeforeToolCallResult → prepareToolCall 得到 immediate 错误结果或 prepared
```

---

## 4. 从入口读起

调用树如下。先从入口往下读派发，再回头读扩展是怎么加载进来的，最后看其他事件的派发方式。

```
AgentSession._installAgentToolHooks()            §4.1   安装 beforeToolCall
└─ _beforeToolCall()                             §4.1
    ├─ runner.hasHandlers("tool_call")           §4.2
    └─ runner.emitToolCall(event)                §4.2 ← 入口
        ├─ createContext()                       §4.3
        └─ snapshotEventHandlers() → handler(event, ctx)
    → 结果回到 M2 prepareToolCall()              §4.2
扩展从哪里来
└─ DefaultResourceLoader → loadExtensionsCached()       [M6]
    └─ loadExtensionsInternal()                  §4.4
        ├─ createExtensionRuntime()              §4.4
        └─ loadExtension()                       §4.5
            ├─ loadExtensionModule()  jiti       §4.5
            └─ initializeExtension()             §4.6
                └─ createExtensionAPI()  pi.on() §4.6
AgentSession._buildRuntime(): new ExtensionRunner + bindCore()   §4.6
其他派发方法
├─ emit()  通知类和可取消的 session_before_*      §4.7
├─ emitContext()  context / context_with_system  §4.8
├─ emitToolResult()、emitMessageEnd()、emitBoundary()、_emitExtensionEvent()  §4.9
└─ 41 个事件名的分类                              §4.10
```

### 4.1 _installAgentToolHooks() 和 _beforeToolCall()

**调用位置**：`AgentSession` 构造函数（`packages/coding-agent/src/core/agent-session.ts:527`）→ `_installAgentToolHooks()`。之后每个工具调用：M2 `prepareToolCall()` → `config.beforeToolCall(...)` → `_beforeToolCall()`。

源码：[`packages/coding-agent/src/core/agent-session.ts:660-697`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L660-L697)

```ts
	/**
	 * Install tool hooks once on the Agent instance.
	 *
	 * The callbacks read `this._extensionRunner` at execution time, so extension reload swaps in the
	 * new runner without reinstalling hooks. Extension-specific tool wrappers are still used to adapt
	 * registered tool execution to the extension context. Tool call and tool result interception now
	 * happens here instead of in wrappers.
	 */
	private _installAgentToolHooks(): void {
		this.agent.beforeToolCall = (context) => this._beforeToolCall(context);
		this.agent.afterToolCall = (context) => this._afterToolCall(context);
	}

	/** `tool_call` handlers. `parentToolCallId` is set for calls another tool made. */
	private async _beforeToolCall(
		{ toolCall, args }: BeforeToolCallContext,
		parentToolCallId?: string,
	): Promise<BeforeToolCallResult | undefined> {
		const runner = this._extensionRunner;
		if (!runner.hasHandlers("tool_call")) {
			return undefined;
		}

		try {
			return await runner.emitToolCall({
				type: "tool_call",
				toolName: toolCall.name,
				toolCallId: toolCall.id,
				...(parentToolCallId ? { parentToolCallId } : {}),
				input: args as Record<string, unknown>,
			});
		} catch (err) {
			if (err instanceof Error) {
				throw err;
			}
			throw new Error(`Extension failed, blocking execution: ${String(err)}`);
		}
	}
```

**讲解**

`_installAgentToolHooks` 只做一件事：把 `Agent` 的两个公开字段换成调用 AgentSession 方法的箭头函数。注释点出了一个细节：回调在执行时才读 `this._extensionRunner`，所以 `/reload` 重新加载扩展、换了一个新 runner 之后，hook 不需要重装。

`_beforeToolCall` 把 M2 的 `BeforeToolCallContext`（`assistantMessage`、`toolCall`、`args`、`context`）翻译成扩展的 `ToolCallEvent`，只取了三样：

| M2 传入 | ToolCallEvent 字段 |
|---|---|
| `toolCall.name` | `toolName` |
| `toolCall.id` | `toolCallId` |
| `args`（校验过的参数） | `input` |

`input` 直接用 `args` 这个对象，没有拷贝。这就是"原地修改 `event.input` 能改掉工具参数"的原因：M2 的 `prepareToolCall` 把同一个 `validatedArgs` 对象放进 `prepared.args`，随后传给 `tool.execute()`。

没有任何扩展订阅 `tool_call` 时直接返回 `undefined`，不创建事件对象。

`catch` 里的处理值得注意：handler 抛出的异常**不会被吞掉**。`Error` 原样重抛，非 `Error` 的值包成 `Error` 再抛。异常一路传回 M2 的 `prepareToolCall`，被它的 `try/catch` 接住，变成一个错误工具结果（M2 §4.11）：

源码：[`packages/agent/src/agent-loop.ts:735-787`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L735-L787)

```ts
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

所以 `_beforeToolCall` 返回的值有三种结局：

| `_beforeToolCall` 的结果 | `prepareToolCall` 的结果 | 工具 |
|---|---|---|
| `undefined` 或 `{ block: false }` | `kind: "prepared"`（774 行） | 执行 |
| `{ block: true, reason }` | `kind: "immediate"`，文本是 `reason` 或 `"Tool execution was blocked"`（755-765 行） | 不执行，`isError: true` |
| 抛异常 | `kind: "immediate"`，文本是异常消息（780-786 行） | 不执行，`isError: true` |

异常也会拦下工具调用。权限扩展出 bug 时，宁可不执行，也不放行，这是 fail-closed 的做法。§7.2 用实验验证。

**往哪走**：§4.2 `emitToolCall`。

### 4.2 ExtensionRunner.emitToolCall() 和 hasHandlers()

**调用位置**：`_beforeToolCall()` → `runner.hasHandlers("tool_call")`、`runner.emitToolCall(event)`。

源码：[`packages/coding-agent/src/core/extensions/runner.ts:772-780`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L772-L780)

```ts
	hasHandlers(eventType: string): boolean {
		for (const ext of this.extensions) {
			const handlers = ext.handlers.get(eventType);
			if (handlers && handlers.length > 0) {
				return true;
			}
		}
		return false;
	}
```

源码：[`packages/coding-agent/src/core/extensions/runner.ts:276-278`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L276-L278)

```ts
function snapshotEventHandlers(extensions: Extension[], event: ExtensionEvent["type"]) {
	return extensions.map((ext) => ({ ext, handlers: ext.handlers.get(event)?.slice() ?? [] }));
}
```

源码：[`packages/coding-agent/src/core/extensions/runner.ts:1249-1267`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L1249-L1267)

```ts
	async emitToolCall(event: ToolCallEvent): Promise<ToolCallEventResult | undefined> {
		const ctx = this.createContext();
		let result: ToolCallEventResult | undefined;

		for (const { handlers } of snapshotEventHandlers(this.extensions, "tool_call")) {
			for (const handler of handlers) {
				const handlerResult = await handler(event, ctx);

				if (handlerResult) {
					result = handlerResult as ToolCallEventResult;
					if (result.block) {
						return result;
					}
				}
			}
		}

		return result;
	}
```

**讲解**

`hasHandlers` 遍历所有扩展，只要有一个扩展注册了这个事件就返回 `true`。上层几乎在每个派发点前都先调用它，没人订阅时连事件对象都不创建。

`snapshotEventHandlers` 先把每个扩展的 handler 数组拷贝一份（`.slice()`）。handler 执行过程中如果有扩展调用 `pi.on()` 新增 handler，或调用 `on()` 返回的函数取消订阅，不会影响这一次派发。

`emitToolCall` 的派发规则：

- **顺序**：外层按扩展的加载顺序，内层按同一扩展里的注册顺序。加载顺序就是 `-e` 参数和配置里路径的顺序（§4.4）。
- **短路**：某个 handler 返回 `{ block: true }`，立刻返回，后面的 handler 不再执行。
- **最后一个非空结果**：没有 `block` 时，`result` 记录最后一个返回真值的结果。由于 `ToolCallEventResult` 只有 `block` 有作用，非 `block` 的结果实际上等同于放行。
- **共享事件对象**：所有 handler 收到的是同一个 `event`。前面的 handler 原地改了 `event.input`，后面的 handler 看到的就是改过的参数。
- **不捕获异常**：这是 `runner.ts` 里唯一一个循环体没有 `try/catch` 的 `emit*` 方法（`emitUserBash` 也重抛，但先报告错误）。异常直接抛给 `_beforeToolCall`，结果见 §4.1 的表格。

`createContext()` 在派发开始时调用一次，所有 handler 共用同一个 `ctx`。

**真实实例。** `m7-runner.ts gate` 用 permission-gate 创建一个 runner（没有调用 `bindCore`，也没有 UI），直接调用三次 `emitToolCall`：

```bash
node --import "$PI/packages/coding-agent/src/experimental/source-resolver.ts" m7-runner.ts gate
```

```
hasUI: false  hasHandlers(tool_call): true
emitToolCall(read {"path":"README.md"}) → undefined
emitToolCall(bash {"command":"ls"}) → undefined
emitToolCall(bash {"command":"rm -rf build"}) → {"block":true,"reason":"Dangerous command blocked (no UI for confirmation)"}
```

`read` 和安全的 bash 命令返回 `undefined`（放行）；`rm -rf build` 在没有 UI 时直接返回 `{ block: true, reason }`。

**往哪走**：handler 收到的 `ctx` 从哪来，见 §4.3。

### 4.3 createContext()：每次读取都去问 runner

**调用位置**：每个 `emit*` 方法开头 `const ctx = this.createContext()`。

源码：[`packages/coding-agent/src/core/extensions/runner.ts:880-932`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L880-L932)

```ts
	/**
	 * Create an ExtensionContext for use in event handlers and tool execution.
	 * Context values are resolved at call time, so changes via bindCore/bindUI are reflected.
	 */
	createContext(): ExtensionContext {
		const runner = this;
		const getModel = this.getModel;
		const getScopedModels = this.getScopedModels;
		return {
			get ui() {
				runner.assertActive();
				return runner.uiContext;
			},
			get mode() {
				runner.assertActive();
				return runner.mode;
			},
			get hasUI() {
				runner.assertActive();
				return runner.hasUI();
			},
			get cwd() {
				runner.assertActive();
				return runner.cwd;
			},
			get sessionManager() {
				runner.assertActive();
				return runner.sessionManager;
			},
			get modelRegistry() {
				runner.assertActive();
				return runner.modelRegistry;
			},
			get model() {
				runner.assertActive();
				return getModel();
			},
			get scopedModels() {
				runner.assertActive();
				return getScopedModels();
			},
			get thinkingLevel() {
				runner.assertActive();
				return runner.runtime.getThinkingLevel();
			},
			isIdle: () => {
				runner.assertActive();
				return runner.isIdleFn();
			},
			isProjectTrusted: () => {
				runner.assertActive();
				return runner.isProjectTrustedFn();
			},
```

（961 行结束，后面的 `signal`、`abort`、`compact`、`getSystemPrompt` 写法相同。）

**讲解**

返回的对象里每个字段都是 getter 或函数，读取时才调用 runner 的私有字段。这些私有字段在构造时是默认值（`isIdleFn` 返回 `true`、`getModel` 返回 `undefined`），`bindCore()` 和 `setUIContext()` 再换成 AgentSession 提供的真实实现。所以：

- handler 读到的 `ctx.model`、`ctx.isIdle()` 永远是当前值，不是 `ctx` 创建时的快照。
- `m7-runner.ts` 没有调用 `bindCore()`，`ctx.hasUI` 是 `false`（`uiContext` 仍是 `noOpUIContext`），permission-gate 走了"无 UI 直接拦截"的分支。

每个访问器都先调用 `runner.assertActive()`。`/reload` 或切换会话时旧 runner 被 `invalidate()`（`packages/coding-agent/src/core/extensions/runner.ts:729`），之后扩展如果还拿着旧的 `ctx` 去读字段，就会抛出一个解释原因的错误，而不是悄悄读到已经失效的会话。

**往哪走**：派发读完了。下面回头看 `Extension` 对象和 handler 是怎么来的，§4.4。

### 4.4 loadExtensionsInternal() 和 createExtensionRuntime()

**调用位置**：`DefaultResourceLoader.reload()` → `loadExtensionsCached(extensionPaths, this.cwd, this.eventBus)`（`packages/coding-agent/src/core/resource-loader.ts:735`）→ `loadExtensionsInternal(..., useCache = true)`。扩展路径怎么收集（`-e` 参数、`settings.json` 的 `extensions`、`.pi/extensions/` 目录、包管理器安装的包），属于 M6 的资源发现。

源码：[`packages/coding-agent/src/core/extensions/loader.ts:676-719`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L676-L719)

```ts
/**
 * Load extensions from paths.
 */
async function loadExtensionsInternal(
	paths: string[],
	cwd: string,
	eventBus?: EventBus,
	runtime?: ExtensionRuntime,
	useCache = false,
): Promise<LoadExtensionsResult> {
	const extensions: Extension[] = [];
	const errors: Array<{ path: string; error: string }> = [];
	const warnings: Array<{ path: string; warning: string }> = [];
	const cacheToken = useCache ? useExtensionCacheCwd(cwd) : undefined;
	const resolvedCwd = cacheToken?.cwd ?? resolvePath(cwd);
	const resolvedEventBus = eventBus ?? createEventBus();
	const resolvedRuntime = runtime ?? createExtensionRuntime();

	for (const extPath of paths) {
		const { extension, error } = await loadExtension(
			extPath,
			resolvedCwd,
			resolvedEventBus,
			resolvedRuntime,
			cacheToken,
		);

		if (error) {
			errors.push({ path: extPath, error });
			continue;
		}

		if (extension) {
			extensions.push(extension);
		}
	}

	return {
		extensions,
		errors,
		warnings,
		runtime: resolvedRuntime,
	};
}
```

**讲解**

逐个加载路径，串行 `await`。一个路径失败只记进 `errors`，继续下一个。所有扩展共享同一个 `eventBus`（扩展之间通信用的 `pi.events`）和同一个 `runtime`。

`useCache` 为 `true` 时，同一个 cwd 下第二次加载同一路径会直接复用缓存的工厂函数（`useExtensionCacheCwd`，`packages/coding-agent/src/core/extensions/loader.ts:147`）。cwd 一变，缓存清空。

共享的 runtime 来自 `createExtensionRuntime()`：

源码：[`packages/coding-agent/src/core/extensions/loader.ts:156-188`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L156-L188)

```ts
/**
 * Create a runtime with throwing stubs for action methods.
 * Runner.bindCore() replaces these with real implementations.
 */
export function createExtensionRuntime(): ExtensionRuntime {
	const notInitialized = () => {
		throw new Error("Extension runtime not initialized. Action methods cannot be called during extension loading.");
	};
	const state: { staleMessage?: string } = {};
	const eventBusUnsubscribers = new Set<() => void>();
	const assertActive = () => {
		if (state.staleMessage) {
			throw new Error(state.staleMessage);
		}
	};

	const runtime: ExtensionRuntime = {
		sendMessage: notInitialized,
		sendUserMessage: notInitialized,
		appendEntry: notInitialized,
		setSessionName: notInitialized,
		getSessionName: notInitialized,
		setLabel: notInitialized,
		getActiveTools: notInitialized,
		getAllTools: notInitialized,
		getSettings: notInitialized,
		setActiveTools: notInitialized,
		// registerTool() is valid during extension load; refresh is only needed post-bind.
		refreshTools: () => {},
		getCommands: notInitialized,
		setModel: () => Promise.reject(new Error("Extension runtime not initialized")),
		getThinkingLevel: notInitialized,
		setThinkingLevel: notInitialized,
```

**讲解**

`notInitialized` 是一个只会抛错的函数，加载阶段所有"动作"都指向它。错误信息直接告诉作者原因：工厂函数里不能调用动作方法。§3.1 实例的最后一行就是它。

注释里写的是 "Runner.bindCore() replaces these with real implementations"。`bindCore` 的开头正是一串赋值（`packages/coding-agent/src/core/extensions/runner.ts:428-444`）：从 `this.runtime.sendMessage = actions.sendMessage;` 到 `this.runtime.createContext = () => this.createContext();`。因为所有扩展的 `ExtensionAPI` 都持有同一个 runtime 对象，一次赋值就对全部扩展生效。

**往哪走**：§4.5 单个扩展的加载。

### 4.5 loadExtension() 和 loadExtensionModule()：jiti

**调用位置**：`loadExtensionsInternal()` → `loadExtension(extPath, ...)`。

源码：[`packages/coding-agent/src/core/extensions/loader.ts:637-660`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L637-L660)

```ts
async function loadExtension(
	extensionPath: string,
	cwd: string,
	eventBus: EventBus,
	runtime: ExtensionRuntime,
	cacheToken?: ExtensionCacheToken,
): Promise<{ extension: Extension | null; error: string | null }> {
	const resolvedPath = resolvePath(extensionPath, cwd, { normalizeUnicodeSpaces: true });

	try {
		const factory = await loadExtensionModule(resolvedPath, cacheToken);
		time(`${extensionPath} module import`, "extensions");
		if (!factory) {
			return { extension: null, error: `Extension does not export a valid factory function: ${extensionPath}` };
		}

		const extension = await initializeExtension(factory, extensionPath, resolvedPath, cwd, eventBus, runtime);

		return { extension, error: null };
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		return { extension: null, error: `Failed to load extension: ${message}` };
	}
}
```

**讲解**

一个扩展的加载分两步：`loadExtensionModule` 拿到工厂函数，`initializeExtension` 调用它。两步的失败都被 `catch` 转成 `error` 字符串返回，§3.1 实例里的两条错误分别来自 `Failed to load extension: ...`（文件不存在，jiti 抛异常）和 `Extension does not export a valid factory function`（默认导出不是函数）。

源码：[`packages/coding-agent/src/core/extensions/loader.ts:41-54`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L41-L54)

```ts
const isNodeSeaBinary =
	("sea" in process.features && process.features.sea === true) ||
	process.getBuiltinModule("node:sea")?.isSea() === true;
const isTypeScriptSourceRuntime = !isBunBinary && path.extname(fileURLToPath(import.meta.url)) === ".ts";
const usesEmbeddedModules = isBunBinary || isNodeSeaBinary || isBundledNode;

let createJitiPromise: Promise<typeof createJiti> | undefined;

function getCreateJiti(): Promise<typeof createJiti> {
	createJitiPromise ??= (usesEmbeddedModules ? import("./jiti-static-loader.ts") : import("./jiti-loader.ts")).then(
		(module) => module.createJiti,
	);
	return createJitiPromise;
}
```

源码：[`packages/coding-agent/src/core/extensions/loader.ts:562-593`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L562-L593)

```ts
async function loadExtensionModule(extensionPath: string, cacheToken?: ExtensionCacheToken) {
	if (isCurrentCacheToken(cacheToken)) {
		const cachedFactory = extensionCache.get(extensionPath);
		if (cachedFactory) {
			return cachedFactory;
		}
	}

	const createJitiImpl = await getCreateJiti();
	// Compiled binaries and the bundled Node distribution use embedded modules.
	// Source TypeScript reuses host modules and root tsconfig paths. Unbundled
	// Node builds use dist aliases and do not need the bundled virtual modules.
	const resolutionOptions = usesEmbeddedModules
		? { virtualModules: await getVirtualModules(), tryNative: false }
		: isTypeScriptSourceRuntime
			? { virtualModules: await getVirtualModules(), tsconfigPaths: true }
			: { alias: getAliases() };
	const jiti = createJitiImpl(import.meta.url, {
		moduleCache: false,
		...resolutionOptions,
	});

	const module = await jiti.import(extensionPath, { default: true });
	const factory = module as ExtensionFactory;
	if (typeof factory !== "function") {
		return undefined;
	}
	if (isCurrentCacheToken(cacheToken)) {
		extensionCache.set(extensionPath, factory);
	}
	return factory;
}
```

**讲解**

[jiti](https://github.com/unjs/jiti) 是一个运行时模块加载器：给它一个 `.ts` 文件路径，它在内存里把 TypeScript 转成 JavaScript 再执行，不需要事先编译。这就是 `pi -e ./my-ext.ts` 能直接加载 TypeScript 文件的原因。

`getCreateJiti` 按运行形态选择两个一行文件之一：普通 Node 用 `jiti`（Babel 只在需要转换时才加载），编译成单文件二进制时用 `jiti/static`（让打包器把 Babel 打进去）。

更关键的是 `resolutionOptions`：扩展里写 `import { fauxProvider } from "@earendil-works/pi-ai"`，这个包名解析到哪里？有三种情况：

| 运行形态 | 条件 | 解析方式 |
|---|---|---|
| 编译后的二进制 / 打包的 Node 发行版 | `usesEmbeddedModules`（45 行） | `virtualModules`：直接用 pi 进程里已加载的模块对象，`tryNative: false` |
| 从 TypeScript 源码运行（本教程的情况） | `isTypeScriptSourceRuntime`（44 行，`loader.ts` 自己的扩展名是 `.ts`） | `virtualModules` + `tsconfigPaths` |
| 未打包的 Node 构建 | 其余 | `alias`：包名映射到 `dist/*.js` 文件（`getAliases`，69 行起） |

`virtualModules` 的内容在 `virtual-modules.ts` 里：`"@earendil-works/pi-ai"` 对应 `import * as bundledPiAiCompat from "@earendil-works/pi-ai/compat"`，`"@earendil-works/pi-coding-agent"` 对应 pi 自己的 `index.ts`，还有 `typebox` 和旧包名 `@mariozechner/*`。所以扩展拿到的是**和 pi 进程同一份**模块实例，而不是磁盘上另一份副本。扩展导入的类、单例、`instanceof` 判断都和 pi 内部一致。

`moduleCache: false` 关掉 jiti 自己的模块缓存。缓存由 pi 自己管理：`extensionCache` 按路径缓存工厂函数，并用 `cacheToken`（cwd + 代数）判断缓存是否还有效。

**往哪走**：§4.6 调用工厂函数。

### 4.6 initializeExtension()、createExtensionAPI() 和绑定

**调用位置**：`loadExtension()` → `initializeExtension(factory, ...)`。内置扩展和 SDK 传入的 inline 工厂走 `loadExtensionFromFactory()`（`packages/coding-agent/src/core/extensions/loader.ts:665`），直接调用同一个 `initializeExtension`。

源码：[`packages/coding-agent/src/core/extensions/loader.ts:616-635`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L616-L635)

```ts
async function initializeExtension(
	factory: ExtensionFactory,
	extensionPath: string,
	resolvedPath: string,
	cwd: string,
	eventBus: EventBus,
	runtime: ExtensionRuntime,
): Promise<Extension> {
	const extension = createExtension(extensionPath, resolvedPath);
	const load = createExtensionAPI(extension, runtime, cwd, eventBus);
	try {
		await factory(load.api);
		load.commit();
	} catch (error) {
		load.discard();
		throw error;
	}
	time(`${extensionPath} factory`, "extensions");
	return extension;
}
```

**讲解**

先创建一个空的 `Extension`（各个 Map 都是空的），再为它创建一个专属的 `ExtensionAPI`，然后 `await factory(api)`。工厂可以是 async 函数，加载器会等它完成。成功则 `commit()`，抛异常则 `discard()` 并把异常交给 `loadExtension` 的 `catch`。

源码：[`packages/coding-agent/src/core/extensions/loader.ts:242-290`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L242-L290)

```ts
/**
 * Create the ExtensionAPI for an extension.
 * Registration methods write to the extension object.
 * Action methods delegate to the shared runtime.
 */
function createExtensionAPI(
	extension: Extension,
	runtime: ExtensionRuntime,
	cwd: string,
	eventBus: EventBus,
): { api: ExtensionAPI; commit: () => void; discard: () => void } {
	const pendingFlagValues = new Map<string, boolean | string>();
	const pendingRuntimeChanges: Array<() => void> = [];
	const loadingUnsubscribers: Array<() => void> = [];
	let state: "loading" | "active" | "failed" = "loading";
	const assertActive = () => {
		if (state === "failed") {
			throw new Error(`Extension "${extension.path}" failed to load and its API is no longer active.`);
		}
		runtime.assertActive();
	};
	const applyRuntimeChange = (change: () => void) => {
		if (state === "loading") pendingRuntimeChanges.push(change);
		else change();
	};
	const clearPending = () => {
		pendingFlagValues.clear();
		pendingRuntimeChanges.length = 0;
		loadingUnsubscribers.length = 0;
	};

	const api = {
		// Registration methods - write to extension
		on(event: string, handler: HandlerFn): () => void {
			assertActive();
			const registeredHandler: HandlerFn = (...args) => handler(...args);
			const list = extension.handlers.get(event) ?? [];
			list.push(registeredHandler);
			extension.handlers.set(event, list);

			return () => {
				const handlers = extension.handlers.get(event);
				if (!handlers) return;
				const handlerIndex = handlers.indexOf(registeredHandler);
				if (handlerIndex === -1) return;
				handlers.splice(handlerIndex, 1);
				if (handlers.length === 0) extension.handlers.delete(event);
			};
		},
```

**讲解**

每个扩展有自己的 `api` 对象，但它们共享同一个 `runtime`。`api` 的方法分两类（函数开头的注释说明了这一点）：

- **注册方法**写进这个扩展的 `Extension` 对象。`on()` 把 handler 包一层再 push 进 `extension.handlers`，返回一个取消订阅的函数。包一层是为了让同一个函数注册两次也能分别取消。
- **动作方法**转发给共享的 `runtime`，例如 `sendMessage(message, options)` 调用 `runtime.sendMessage(...)`。加载阶段 runtime 里是 §4.4 的桩函数，调用就抛错。

`state` 有三个值：`loading`、`active`、`failed`。加载期间对 runtime 的修改（注册 provider、MCP 服务器、虚拟模型）不立刻执行，而是由 `applyRuntimeChange` 放进 `pendingRuntimeChanges`：

源码：[`packages/coding-agent/src/core/extensions/loader.ts:459-467`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L459-L467)

```ts
		registerProvider(providerOrName: Provider | string, config?: ProviderConfig) {
			assertActive();
			if (typeof providerOrName === "string") {
				if (!config) throw new Error("Provider config is required when registering by name");
				applyRuntimeChange(() => runtime.registerProvider(providerOrName, config, extension.path));
				return;
			}
			applyRuntimeChange(() => runtime.registerNativeProvider(providerOrName, extension.path));
		},
```

源码：[`packages/coding-agent/src/core/extensions/loader.ts:533-551`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/loader.ts#L533-L551)

```ts
	return {
		api,
		commit: () => {
			if (state !== "loading") return;
			runtime.assertActive();
			for (const [name, value] of pendingFlagValues) {
				if (!runtime.flagValues.has(name)) runtime.flagValues.set(name, value);
			}
			for (const apply of pendingRuntimeChanges) apply();
			state = "active";
			clearPending();
		},
		discard: () => {
			if (state !== "loading") return;
			state = "failed";
			for (const unsubscribe of loadingUnsubscribers) unsubscribe();
			clearPending();
		},
	};
```

**讲解**

工厂成功返回才 `commit()`，把排队的修改应用到 runtime。工厂中途抛异常则 `discard()`：排队的修改全部丢弃，`state` 变成 `failed`，之后再调用这个 `api` 的任何方法都会抛错。一个扩展加载失败，不会留下"注册了一半"的 provider。

以 `faux-ext.ts` 的 `pi.registerProvider("faux", {...})` 为例，它经过两层排队：

1. 工厂执行中：`applyRuntimeChange` 把调用放进这个扩展的 `pendingRuntimeChanges`。
2. 工厂返回、`commit()`：执行 `runtime.registerProvider(...)`。这时 runtime 还没绑定，它只是 push 进 `runtime.pendingProviderRegistrations`（`packages/coding-agent/src/core/extensions/loader.ts:217`）。
3. AgentSession 调用 `bindCore()`：遍历 `pendingProviderRegistrations`，注册进 `ModelRegistry`（`packages/coding-agent/src/core/extensions/runner.ts:470-486`），之后把 `runtime.registerProvider` 换成立即生效的版本。

绑定发生在 AgentSession 的 `_buildRuntime()` 里（→ M8）：

源码：[`packages/coding-agent/src/core/agent-session.ts:3711-3729`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3711-L3729)

```ts
		const extensionsResult = this._resourceLoader.getExtensions();
		if (options.flagValues) {
			for (const [name, value] of options.flagValues) {
				extensionsResult.runtime.flagValues.set(name, value);
			}
		}

		this._extensionRunner = new ExtensionRunner(
			extensionsResult.extensions,
			extensionsResult.runtime,
			this._cwd,
			this.sessionManager,
			new ModelRegistry(this._modelRuntime),
		);
		if (this._extensionRunnerRef) {
			this._extensionRunnerRef.current = this._extensionRunner;
		}
		this._bindExtensionCore(this._extensionRunner);
		this._applyExtensionBindings(this._extensionRunner);
```

**讲解**

`_resourceLoader.getExtensions()` 返回 M6 加载好的 `LoadExtensionsResult`。命令行传入的扩展 flag 值先写进 `runtime.flagValues`。然后用扩展列表和共享 runtime 创建 `ExtensionRunner`，`_extensionRunnerRef.current` 让 `sdk.ts` 里的 `transformContext`、`onPayload` 等回调也能找到它。最后两步：

- `_bindExtensionCore()` 调用 `runner.bindCore(actions, contextActions)`，`actions` 里是 `sendMessage: (m, o) => this.sendCustomMessage(m, o)`、`appendEntry: ... this.sessionManager.appendCustomEntry(...)` 这样的真实实现（`packages/coding-agent/src/core/agent-session.ts:3467` 起）。
- `_applyExtensionBindings()` 装入 UI 上下文和错误监听器（`packages/coding-agent/src/core/agent-session.ts:3417-3425`）。print 模式的错误监听器把错误打到 stderr：`console.error(\`Extension error (${err.extensionPath}): ${err.error}\`)`（`packages/coding-agent/src/modes/print-mode.ts:102`）。

绑定完成后，AgentSession 发出第一个扩展事件 `session_start`（`packages/coding-agent/src/core/agent-session.ts:3360`），接着是 `resources_discover`。这就是 §2.1 输出的前两行。

**往哪走**：加载和绑定读完了。回到 runner，看 `emitToolCall` 以外的派发方法，§4.7。

### 4.7 emit()：通用派发

**调用位置**：大部分事件都通过它派发。例如 AgentSession 把每个 AgentEvent 转给扩展的 `_emitExtensionEvent()`（§4.9），`session_before_compact`（`packages/coding-agent/src/core/agent-session.ts:2878`），`after_provider_response`（`packages/coding-agent/src/core/sdk.ts:382`）。

源码：[`packages/coding-agent/src/core/extensions/runner.ts:179-200`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L179-L200)

```ts
/**
 * Events handled by the generic emit() method.
 * Events with dedicated emitXxx() methods are excluded for stronger type safety.
 */
type RunnerEmitEvent = Exclude<
	ExtensionEvent,
	| ToolCallEvent
	| ProjectTrustEvent
	| ToolResultEvent
	| UserBashEvent
	| ContextEvent
	| ContextWithSystemEvent
	| CacheWarmingDecisionEvent
	| BeforeProviderRequestEvent
	| BeforeProviderHeadersEvent
	| BeforeAgentStartEvent
	| MessageEndEvent
	| ResourcesDiscoverEvent
	| InputEvent
	| TurnEndEvent
	| AgentBeforeSettleEvent
>;
```

源码：[`packages/coding-agent/src/core/extensions/runner.ts:1087-1125`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L1087-L1125)

```ts
	private isSessionBeforeEvent(event: RunnerEmitEvent): event is SessionBeforeEvent {
		return (
			event.type === "session_before_switch" ||
			event.type === "session_before_fork" ||
			event.type === "session_before_compact" ||
			event.type === "session_before_tree"
		);
	}

	async emit<TEvent extends RunnerEmitEvent>(event: TEvent): Promise<RunnerEmitResult<TEvent>> {
		const ctx = this.createContext();
		let result: SessionBeforeEventResult | undefined;

		for (const { ext, handlers } of snapshotEventHandlers(this.extensions, event.type)) {
			for (const handler of handlers) {
				try {
					const handlerResult = await handler(event, ctx);

					if (this.isSessionBeforeEvent(event) && handlerResult) {
						result = handlerResult as SessionBeforeEventResult;
						if (result.cancel) {
							return result as RunnerEmitResult<TEvent>;
						}
					}
				} catch (err) {
					const message = err instanceof Error ? err.message : String(err);
					const stack = err instanceof Error ? err.stack : undefined;
					this.emitError({
						extensionPath: ext.path,
						event: event.type,
						error: message,
						stack,
					});
				}
			}
		}

		return result as RunnerEmitResult<TEvent>;
	}
```

**讲解**

`RunnerEmitEvent` 把有专用 `emitXxx()` 方法的 15 类事件排除在外，剩下的都用 `emit()`。类型系统保证不会有人用 `emit()` 派发 `tool_call`，从而绕过它的特殊处理。

`emit()` 和 `emitToolCall()` 有两处不同：

- **捕获异常**：每个 handler 包在 `try/catch` 里，异常交给 `emitError()`，派发继续。`emitError()` 遍历 `onError()` 注册的监听器（`packages/coding-agent/src/core/extensions/runner.ts:744-753`）。一个通知类 handler 出错不影响 agent 和其他扩展。
- **只有 `session_before_*` 的返回值有用**：`isSessionBeforeEvent` 判断事件是不是四个 `session_before_*` 之一。是的话记录最后一个结果，`cancel: true` 时短路返回。其余事件的返回值被丢弃。

**真实实例。** `m7-runner.ts errors` 用一个 inline 扩展分别在 `tool_execution_start` 和 `tool_call` 的 handler 里抛异常：

```bash
node --import "$PI/packages/coding-agent/src/experimental/source-resolver.ts" m7-runner.ts errors
```

```
[onError] {"extensionPath":"<inline:thrower>","event":"tool_execution_start","error":"通知类事件里的异常"}
emit(tool_execution_start) 正常返回: undefined
emitToolCall 抛出: tool_call 里的异常
```

`emit(tool_execution_start)` 正常返回，异常变成一次 `onError` 回调；`emitToolCall` 把异常抛给了调用者。

**往哪走**：§4.8 请求前改写上下文的 `emitContext`。

### 4.8 emitContext()：context 和 context_with_system

**调用位置**：M2 `streamAssistantResponse()` → `config.transformContext(messages)` → `sdk.ts` 安装的回调：

源码：[`packages/coding-agent/src/core/sdk.ts:428-432`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L428-L432)

```ts
		transformContext: async (messages) => {
			const runner = extensionRunnerRef.current;
			if (!runner) return messages;
			return runner.emitContext(messages);
		},
```

源码：[`packages/coding-agent/src/core/extensions/runner.ts:1300-1334`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L1300-L1334)

```ts
	/**
	 * Run the request-time transforms in two phases. `context` handlers see the conversation
	 * only and Pi restores the prompt and tool state after each; `context_with_system`
	 * handlers then see the full transcript and their output is used as returned.
	 */
	async emitContext(messages: AgentMessage[]): Promise<AgentMessage[]> {
		const ctx = this.createContext();
		let currentMessages = structuredClone(messages);

		for (const { ext, handlers } of snapshotEventHandlers(this.extensions, "context")) {
			for (const handler of handlers) {
				try {
					const visibleMessages = currentMessages.filter((message) => message.role !== "system");
					const visibleSnapshot = visibleMessages.slice();
					const event: ContextEvent = { type: "context", messages: visibleMessages };
					const handlerResult = (await handler(event, ctx)) as ContextEventResult | undefined;

					// Handlers may return a new list or edit event.messages in place.
					const returned =
						handlerResult?.messages ??
						(sameMessages(visibleMessages, visibleSnapshot) ? undefined : visibleMessages);
					if (!returned) continue;
					currentMessages = restoreSystemMessages(currentMessages, visibleSnapshot, returned);
				} catch (err) {
					const message = err instanceof Error ? err.message : String(err);
					const stack = err instanceof Error ? err.stack : undefined;
					this.emitError({
						extensionPath: ext.path,
						event: "context",
						error: message,
						stack,
					});
				}
			}
		}
```

源码：[`packages/coding-agent/src/core/extensions/runner.ts:280-300`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L280-L300)

```ts
function sameMessages(left: AgentMessage[], right: AgentMessage[]): boolean {
	return left.length === right.length && left.every((message, index) => message === right[index]);
}

/**
 * Re-attach the prompt and tool state after a `context` handler. Handlers only see the
 * conversation; the system messages belong to Pi. An unchanged conversation keeps every
 * system message in place, so models with mid-conversation support keep their cached
 * prefix. A changed one gets the replayed prompt sections and tool declarations as one
 * leading system message, so pruning, windowing, or slicing from a compaction summary
 * cannot drop them.
 */
function restoreSystemMessages(
	current: AgentMessage[],
	visible: AgentMessage[],
	returned: AgentMessage[],
): AgentMessage[] {
	if (sameMessages(returned, visible)) return current;
	const head = getCurrentSystemMessage(current);
	return head ? [head, ...returned] : returned;
}
```

**讲解**

`emitContext` 每次请求模型前调用，分两个阶段：

1. **`context` 阶段**：handler 看到的 `event.messages` 是**去掉 system 消息之后**的列表（1312 行）。§2.1 里 `context messages=[user]`、`context_with_system messages=[system,user]` 就是这个区别。handler 可以返回新列表，也可以原地修改 `event.messages`（1318-1320 行用 `sameMessages` 判断数组有没有被改）。之后 `restoreSystemMessages` 把 system 消息接回去：
   - 列表没变：用原来的完整列表，所有 system 消息保持原位。支持中途 system 消息的 provider 能继续命中 prompt cache（→ M1）。
   - 列表变了：用 `getCurrentSystemMessage(current)`（→ M1，把所有 system 消息重放成一条当前的系统提示词和工具声明）放在最前面，后面接 handler 返回的消息。handler 做了裁剪，也不会把系统提示词和工具声明裁掉。
2. **`context_with_system` 阶段**（1336-1363 行）：handler 看到完整列表，返回什么就用什么。如果它删掉了开头的 system 消息，`emitError` 报告一条错误，但仍然照它的结果发送。

开头的 `structuredClone(messages)` 让 handler 改的是副本。M2 循环里的 `AgentContext.messages` 和会话文件都不受影响，改写只作用于这一次请求。§7.3 用实验验证。

**往哪走**：§4.9 其余派发方法和事件分类。

### 4.9 其他专用派发：tool_result、message_end、turn_end 和 AgentEvent 转发

`emitToolCall` 的姊妹方法是 `emitToolResult`，由 `afterToolCall` hook 调用（M2 `finalizeExecutedToolCall()` → `config.afterToolCall` → `AgentSession._afterToolCall`，`packages/coding-agent/src/core/agent-session.ts:700`）。

源码：[`packages/coding-agent/src/core/extensions/runner.ts:1190-1247`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L1190-L1247)

```ts
	async emitToolResult(event: ToolResultEvent): Promise<ToolResultEventResult | undefined> {
		const ctx = this.createContext();
		const currentEvent: ToolResultEvent = { ...event };
		let modified = false;

		for (const { ext, handlers } of snapshotEventHandlers(this.extensions, "tool_result")) {
			for (const handler of handlers) {
				try {
					const handlerResult = (await handler(currentEvent, ctx)) as ToolResultEventResult | undefined;
					if (!handlerResult) continue;

					if (handlerResult.content !== undefined) {
						currentEvent.content = handlerResult.content;
						// Structured content that is not replaced along with the content may no longer match it.
						if (handlerResult.structuredContent === undefined) delete currentEvent.structuredContent;
						modified = true;
					}
					if (handlerResult.details !== undefined) {
						currentEvent.details = handlerResult.details;
						modified = true;
					}
					if (handlerResult.structuredContent !== undefined) {
						currentEvent.structuredContent = handlerResult.structuredContent;
						modified = true;
					}
					if (handlerResult.isError !== undefined) {
						currentEvent.isError = handlerResult.isError;
						modified = true;
					}
					if (handlerResult.usage !== undefined) {
						currentEvent.usage = handlerResult.usage;
						modified = true;
					}
				} catch (err) {
					const message = err instanceof Error ? err.message : String(err);
					const stack = err instanceof Error ? err.stack : undefined;
					this.emitError({
						extensionPath: ext.path,
						event: "tool_result",
						error: message,
						stack,
					});
				}
			}
		}

		if (!modified) {
			return undefined;
		}

		return {
			content: currentEvent.content,
			details: currentEvent.details,
			structuredContent: currentEvent.structuredContent,
			isError: currentEvent.isError,
			usage: currentEvent.usage,
		};
	}
```

**讲解**

所有 handler 共享一个 `currentEvent` 副本，每个 handler 返回的字段覆盖进去，后面的 handler 看到的是覆盖后的结果。规则：

- 只返回了 `content` 而没有返回 `structuredContent` 时，删掉 `structuredContent`（1204 行），因为改过的文本和旧的结构化结果可能对不上。
- 异常被捕获并报告，不影响工具结果。和 `tool_call` 相反：工具已经执行过了，拦截已无意义。
- 没有任何修改时返回 `undefined`。`_afterToolCall` 收到后，再做一步图片尺寸归一化，把结果交给 M2，M2 用它覆盖 `AgentToolResult` 的对应字段（M2 §4.13）。

`message_end` 用 `emitMessageEnd`（`packages/coding-agent/src/core/extensions/runner.ts:1151-1188`）：handler 返回 `{ message }` 可以替换刚完成的消息，但 role 必须相同，否则报错并忽略。AgentSession 拿到替换结果后，用 `_replaceMessageInPlace` 原地改写原对象（删掉所有字段再 `Object.assign`），这样 Agent 的状态、后续事件和随后写入会话文件的都是替换后的消息（→ M8）。

`turn_end` 和 `agent_before_settle` 用 `emitBoundary`：

源码：[`packages/coding-agent/src/core/extensions/runner.ts:1036-1085`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L1036-L1085)

```ts
	async emitBoundary(
		baseEvent: BoundaryBaseEvent,
		buildContext: (entries: SessionBoundaryDraft[]) => BoundaryContextPreview | Promise<BoundaryContextPreview>,
	): Promise<BoundaryDispatchResult> {
		const ctx = this.createContext();
		let entries: SessionBoundaryDraft[] = [];
		let shouldContinue = false;
		let context = await buildContext(entries);
		let valid = true;

		for (const { ext, handlers } of snapshotEventHandlers(this.extensions, baseEvent.type)) {
			for (const handler of handlers) {
				const event = {
					...baseEvent,
					entries,
					continue: shouldContinue,
					context,
				} as TurnEndEvent | AgentBeforeSettleEvent;
				try {
					const handlerResult = (await handler(event, ctx)) as BoundaryResult | undefined;
					if (handlerResult?.entries !== undefined) entries = handlerResult.entries;
					if (handlerResult?.continue !== undefined) shouldContinue = handlerResult.continue;
				} catch (err) {
					this.emitError({
						extensionPath: ext.path,
						event: baseEvent.type,
						error: err instanceof Error ? err.message : String(err),
						stack: err instanceof Error ? err.stack : undefined,
					});
				}

				try {
					context = await buildContext(entries);
					valid = true;
				} catch (err) {
					valid = false;
					this.emitError({
						extensionPath: ext.path,
						event: baseEvent.type,
						error: `Invalid boundary entries: ${err instanceof Error ? err.message : String(err)}`,
						stack: err instanceof Error ? err.stack : undefined,
					});
				}
			}
		}

		return valid
			? { entries, continue: shouldContinue, context, valid: true }
			: { entries: [], continue: false, context, valid: false };
	}
```

**讲解**

每个 handler 都能看到前面 handler 累积的 `entries`（要追加到会话的条目草稿：自定义条目、自定义消息、context_edit、compaction）和 `continue` 标志，还有 `context`：如果把这些草稿提交，下一次请求的上下文会是什么样（`buildContext`，由 AgentSession 提供）。每个 handler 之后都重新计算一次 `context`，草稿无效时 `valid = false`，最终丢弃全部草稿。

`turn_end` 的派发由 M2 的 `finishTurn` hook 触发（M2 §6.1 引用过 `_installAgentBoundaryHooks`）。`_dispatchTurnEndBoundary`（`packages/coding-agent/src/core/agent-session.ts:862`）先提交草稿，再把 `continue` 转成 `finishTurn` 的返回值 `{ action: "continue" }`，让 M2 的循环在没有工具调用时也再请求一次模型。

最后，M2 的每个 AgentEvent 都经 `_handleAgentEvent` 转给扩展，**先给扩展，再给其他订阅者，最后持久化**：

源码：[`packages/coding-agent/src/core/agent-session.ts:1158-1160`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1158-L1160)

```ts
		// Emit to extensions first, then notify public listeners.
		await this._emitExtensionEvent(event);
		this._emit(event.type === "agent_end" ? { ...event, willRetry: this._willRetryAfterAgentEnd(event) } : event);
```

源码：[`packages/coding-agent/src/core/agent-session.ts:1318-1350`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1318-L1350)

```ts
	/** Emit extension events based on agent events */
	private async _emitExtensionEvent(event: AgentEvent): Promise<void> {
		if (event.type === "agent_start") {
			this._turnIndex = 0;
			await this._extensionRunner.emit({ type: "agent_start" });
		} else if (event.type === "agent_end") {
			await this._extensionRunner.emit({ type: "agent_end", messages: event.messages });
		} else if (event.type === "turn_start") {
			const extensionEvent: TurnStartEvent = {
				type: "turn_start",
				turnIndex: this._turnIndex,
				timestamp: Date.now(),
			};
			await this._extensionRunner.emit(extensionEvent);
		} else if (event.type === "turn_end") {
			if (event.message.role === "assistant" && !this._boundaryDispatchedMessages.delete(event.message)) {
				await this._dispatchTurnEndBoundary(event.message, event.toolResults);
			}
			this._turnIndex++;
		} else if (event.type === "message_start") {
			const extensionEvent: MessageStartEvent = {
				type: "message_start",
				message: event.message,
			};
			await this._extensionRunner.emit(extensionEvent);
		} else if (event.type === "message_update") {
			const extensionEvent: MessageUpdateEvent = {
				type: "message_update",
				message: event.message,
				assistantMessageEvent: event.assistantMessageEvent,
			};
			await this._extensionRunner.emit(extensionEvent);
		} else if (event.type === "message_end") {
```

**讲解**

`_emitExtensionEvent` 是一张 AgentEvent → 扩展事件的翻译表。大部分只是拷贝字段后调用 `emit()`；`turn_start` 多了 `turnIndex` 和 `timestamp`；`turn_end` 如果已经由 `finishTurn` 派发过（`_boundaryDispatchedMessages` 记录了这条消息）就不再派发；`message_end` 走 `emitMessageEnd`（1355 行）。

因为 `_handleAgentEvent` 被 M2 await（M2 §6.3），扩展的通知类 handler 也是被 await 的。一个慢的 `message_update` handler 会拖慢整个循环。

**往哪走**：§4.10 把 41 个事件名分类。

### 4.10 41 个事件名：通知类和改变流程类

判断标准是：**handler 的返回值（或对事件对象的原地修改）会不会被 pi 读取并改变接下来发生的事**。按这个标准，41 个事件名里 19 个能改变流程，22 个只是通知。

**能改变流程的 19 个**

| 事件 | 派发方法 | 派发位置 | handler 能做什么 | 多个 handler 的合并方式 |
|---|---|---|---|---|
| `project_trust` | `emitProjectTrustEvent` | `packages/coding-agent/src/core/project-trust.ts:56` | 决定是否信任项目目录（→ M6） | 第一个 `yes`/`no` 生效，`undecided` 往下传 |
| `resources_discover` | `emitResourcesDiscover` | `packages/coding-agent/src/core/agent-session.ts:3370` | 追加 skill、prompt 模板、主题的路径 | 全部累加 |
| `session_before_switch`、`session_before_fork`、`session_before_tree` | `emit` | `packages/coding-agent/src/core/agent-session-runtime.ts:142`、`:159`，`packages/coding-agent/src/core/agent-session.ts:4107` | `cancel: true` 取消切换、分叉、树导航；fork 可跳过恢复对话；tree 可提供分支摘要 | 第一个 `cancel` 短路，否则取最后一个结果 |
| `session_before_compact` | `emit` | `packages/coding-agent/src/core/agent-session.ts:2878`、`:3206` | 取消压缩，或返回自己生成的 `compaction`（→ M5） | 同上 |
| `input` | `emitInput` | `packages/coding-agent/src/core/agent-session.ts:2072` | `transform` 改写用户输入，`handled` 吞掉输入 | 改写串联，`handled` 短路 |
| `before_agent_start` | `emitBeforeAgentStart` | `packages/coding-agent/src/core/agent-session.ts:1956` | 注入一条自定义消息，替换本轮系统提示词，修改 `systemPromptOptions`（→ M6） | 消息累加，系统提示词后者覆盖 |
| `context` | `emitContext` | `packages/coding-agent/src/core/sdk.ts:431` | 改写发给模型的消息（不含 system） | 串联 |
| `context_with_system` | `emitContext` | 同上 | 改写完整 transcript | 串联 |
| `before_provider_request` | `emitBeforeProviderRequest` | `packages/coding-agent/src/core/sdk.ts:377` | 替换发给 provider 的请求体 | 串联 |
| `before_provider_headers` | `emitBeforeProviderHeaders` | `packages/coding-agent/src/core/sdk.ts:355` | 原地修改请求头（返回值被忽略） | 共享同一个对象 |
| `cache_warming_decision` | `emitCacheWarmingDecision` | `packages/coding-agent/src/core/sdk.ts:338` | 覆盖缓存预热的决定 | 最后一个覆盖 |
| `tool_call` | `emitToolCall` | `packages/coding-agent/src/core/agent-session.ts:684` | `block` 拦截；原地改 `input` | `block` 短路；共享事件对象；**异常拦截工具** |
| `tool_result` | `emitToolResult` | `packages/coding-agent/src/core/agent-session.ts:706` | 改写工具结果 | 字段逐个覆盖 |
| `message_end` | `emitMessageEnd` | `packages/coding-agent/src/core/agent-session.ts:1355` | 替换刚完成的消息（同 role） | 串联 |
| `turn_end` | `emitBoundary` | `packages/coding-agent/src/core/agent-session.ts:882` | 追加会话条目，`continue` 要求再请求一次 | 草稿和标志串联 |
| `agent_before_settle` | `emitBoundary` | `packages/coding-agent/src/core/agent-session.ts:2043` | 同上，在整次运行结束之前 | 同上 |
| `user_bash` | `emitUserBash` | `packages/coding-agent/src/modes/interactive/interactive-mode.ts:7095`、`packages/coding-agent/src/modes/rpc/rpc-mode.ts:562` | 用户输入 `!cmd` 时替换执行方式或直接给出结果 | 第一个非空结果生效 |

**只做通知的 22 个**

| 分组 | 事件 |
|---|---|
| 会话 | `session_start`、`session_info_changed`、`session_compact`、`session_compact_failed`、`session_tree`、`session_shutdown` |
| 运行 | `agent_start`、`agent_end`、`agent_settled` |
| 轮和消息 | `turn_start`、`message_start`、`message_update` |
| 工具执行 | `tool_execution_start`、`tool_execution_update`、`tool_execution_end` |
| provider | `after_provider_response`、`provider_stream_event` |
| 选择 | `model_select`、`thinking_level_select` |
| 其他 | `mcp_servers_change`（MCP 扩展靠它得知新服务器，→ M10）、`ui_prompt_start`、`ui_prompt_end` |

两点补充：

- "通知类"指返回值被忽略。handler 仍然可以通过 `ctx` 和 `pi` 的动作方法影响 agent，例如在 `tool_execution_end` 里调用 `ctx.abort()`，或在 `agent_end` 里调用 `pi.sendUserMessage()` 开启下一次运行。这些是调用 AgentSession 的公开方法，和派发规则无关。
- 能改变流程的 19 个里，只有 `tool_call` 的 handler 抛异常会改变结果（拦下工具），`emitUserBash` 报告后重抛。其余都是"报告错误，忽略这个 handler，继续"。

**往哪走**：代码读完了，§5 画状态机。

---

## 5. 状态机

### 5.1 一个扩展的生命周期

```
                createExtensionAPI()  loader.ts:256  state = "loading"
                         │
                         │ await factory(api)  loader.ts:627
                         │   pi.on()/registerTool() 写进 Extension 的 Map
                         │   registerProvider() 等 → pendingRuntimeChanges  loader.ts:264
                         │   动作方法（sendMessage…）→ notInitialized 抛错  loader.ts:162
          ┌──────────────┴──────────────┐
  工厂抛异常 │                              │ 工厂返回
  discard() │ loader.ts:545-550            │ commit() loader.ts:535-544
          ▼                              ▼ 应用排队的修改
   ┌────────────┐                 ┌────────────┐
   │ failed     │                 │ active     │  进入 LoadExtensionsResult.extensions
   │ 进 errors  │                 │ 动作仍是桩  │
   └────────────┘                 └─────┬──────┘
                                        │ new ExtensionRunner + bindCore()
                                        │ agent-session.ts:3718、3728；runner.ts:428-444
                                        ▼
                                 ┌────────────┐
                                 │ bound      │  动作方法生效，事件开始派发
                                 │            │  第一个事件 session_start agent-session.ts:3360
                                 └─────┬──────┘
                                       │ /reload 或切换会话：session_shutdown
                                       │ → runner.invalidate()  agent-session.ts:3744-3745；runner.ts:729
                                       ▼
                                 ┌────────────┐
                                 │ stale      │  ctx/pi 的任何访问都抛错 runner.ts:738
                                 └────────────┘
```

### 5.2 一个 tool_call 的派发

```
 M2 prepareToolCall：参数校验通过 agent-loop.ts:737
        │ config.beforeToolCall  agent-loop.ts:739
        ▼
 _beforeToolCall  agent-session.ts:674
        ├─ 没有 tool_call handler ───────────────────────────▶ undefined (680)
        ▼
 emitToolCall  runner.ts:1249
   for 扩展（加载顺序）for handler（注册顺序） (1253-1254)
        │ handler(event, ctx)，event.input 是 validatedArgs 本身
        ├─ 返回 { block: true } ─────────────────────────────▶ 立即返回 (1259-1260)
        ├─ 抛异常 ──────────▶ _beforeToolCall 重抛 (692-695) ──▶ prepareToolCall catch (780)
        └─ 返回 undefined / 非 block 结果 → 下一个 handler
        ▼ 全部执行完
   返回最后一个非空结果或 undefined (1266)
        │
        ▼
 prepareToolCall
   ├─ block → immediate，isError，文本 = reason (755-765)
   ├─ catch → immediate，isError，文本 = 异常消息 (780-786)
   └─ 否则 → prepared，args = 可能被改过的 validatedArgs (774-779) → tool.execute → M3
```

---

## 6. 设计要点

### 6.1 扩展只通过 hook 接入循环，runner 读的是"当前"值

**做法。** M2 不知道扩展的存在。AgentSession 安装一次 hook，hook 在执行时读 `this._extensionRunner`；`ExtensionContext` 是一组 getter，读取时才去问 runner。

**理由。** `/reload` 只需要换一个 runner，循环、hook、扩展手里的 `ctx` 都不用重建。旧 runner 被 `invalidate()` 后，扩展误用旧 `ctx` 会立刻得到错误，而不是读到另一个会话的数据。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:660-671`，`packages/coding-agent/src/core/extensions/runner.ts:884`。

### 6.2 加载和运行分两个阶段，加载期的副作用先排队

**做法。** 工厂执行期间，动作方法抛错，注册 provider 之类的修改进队列；工厂成功才提交，失败则整体丢弃；`bindCore()` 之后动作才真正生效。

**理由。** 加载扩展时会话还没创建，`sendMessage` 没有地方可发；先报错比悄悄丢掉调用更容易排查。队列保证一个扩展要么完整注册，要么什么都不留下。provider 注册排队到 `bindCore()`，是因为 `ModelRegistry` 在那时才可用。

**代码位置。** `packages/coding-agent/src/core/extensions/loader.ts:160`、`:263`、`:533`，`packages/coding-agent/src/core/extensions/runner.ts:470`。

### 6.3 一个事件一种合并规则

**做法。** `emit()` 只处理通知和可取消事件，其余 15 类事件有专用方法，各自定义怎么合并多个 handler 的结果：短路、串联、累加、字段覆盖。`RunnerEmitEvent` 类型禁止用 `emit()` 派发这些事件。

**理由。** 不同事件的语义不同：权限检查要"任何一个拒绝就拒绝"，上下文改写要"一个接一个地改"，资源发现要"全部收集"。用一个通用的 `emit()` 加约定无法表达这些差异；专用方法把规则写死在一处，类型检查保证没人绕过。

**代码位置。** `packages/coding-agent/src/core/extensions/runner.ts:183`（类型），`:1096`、`:1249`、`:1305`。

### 6.4 tool_call 失败时关闭，其他事件失败时放过

**做法。** `emitToolCall` 不捕获异常，异常经 M2 变成错误工具结果，工具不执行。其他派发方法捕获异常，报告后继续。

**理由。** `tool_call` 是权限检查点。权限扩展出 bug 时放行，等于安全检查失效；拦下最多让模型多试一次。通知类和改写类事件出错时，最安全的做法是当这个 handler 不存在，不让一个扩展的 bug 影响整个会话。

**代码位置。** `packages/coding-agent/src/core/extensions/runner.ts:1249-1267`，`packages/coding-agent/src/core/agent-session.ts:691-696`，`packages/agent/src/agent-loop.ts:780`。

### 6.5 扩展和 pi 共享同一份模块

**做法。** jiti 加载扩展时用 `virtualModules` 把 `@earendil-works/*` 映射到 pi 进程里已经加载的模块对象。

**理由。** 扩展不需要 `npm install` pi 的包就能 import。扩展里的 `fauxProvider`、`Type`、`isToolCallEventType` 和 pi 内部用的是同一份代码，类型和运行时对象一致。

**代码位置。** `packages/coding-agent/src/core/extensions/loader.ts:574-582`，`packages/coding-agent/src/core/extensions/virtual-modules.ts`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行，`GATE` 指向官方示例：

```bash
GATE=$PI/packages/coding-agent/examples/extensions/permission-gate.ts
```

模型换成 [`lab/m7-faux-bash.ts`](lab/m7-faux-bash.ts)：第 1 次请求调用 `bash`，命令取自 `M7_CMD`（默认 `rm -rf build`）；第 2 次请求把收到的工具结果原样复述，这样 print 模式的最后一行就能显示工具结果。用环境变量 `EXT` 让 `run.sh` 加载它代替 `faux-ext.ts`。

先读一下 permission-gate：

源码：[`packages/coding-agent/examples/extensions/permission-gate.ts:1-34`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/examples/extensions/permission-gate.ts#L1-L34)

```ts
/**
 * Permission Gate Extension
 *
 * Prompts for confirmation before running potentially dangerous bash commands.
 * Patterns checked: rm -rf, sudo, chmod/chown 777
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const dangerousPatterns = [/\brm\s+(-rf?|--recursive)/i, /\bsudo\b/i, /\b(chmod|chown)\b.*777/i];

	pi.on("tool_call", async (event, ctx) => {
		if (event.toolName !== "bash") return undefined;

		const command = event.input.command as string;
		const isDangerous = dangerousPatterns.some((p) => p.test(command));

		if (isDangerous) {
			if (!ctx.hasUI) {
				// In non-interactive mode, block by default
				return { block: true, reason: "Dangerous command blocked (no UI for confirmation)" };
			}

			const choice = await ctx.ui.select(`⚠️ Dangerous command:\n\n  ${command}\n\nAllow?`, ["Yes", "No"]);

			if (choice !== "Yes") {
				return { block: true, reason: "Blocked by user" };
			}
		}

		return undefined;
	});
}
```

### 7.1 permission-gate 在 print 模式下

**问题。** 工作目录里有一个 `build` 目录，模型要执行 `rm -rf build`。print 模式没有界面，permission-gate 会怎么做？`build` 还在吗？模型看到什么？换成 `ls` 呢？

**预测。** print 模式 `ctx.hasUI` 为 `false`（§3.5），permission-gate 走第 20-23 行，返回 `{ block: true, reason: "Dangerous command blocked (no UI for confirmation)" }`。M2 把它变成 `isError: true` 的工具结果（§4.1 的表格），`build` 仍在。`ls` 不匹配任何模式，handler 返回 `undefined`，正常执行。

**运行。**

```bash
mkdir -p /tmp/pi-lab/m7-work-23/build
EXT=$PWD/m7-faux-bash.ts WORK=/tmp/pi-lab/m7-work-23 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-23 ./run.sh -e "$GATE" -p "清理构建目录" 2>&1; ls /tmp/pi-lab/m7-work-23
```

```
Running without API keys...
工具结果（isError=true）：Dangerous command blocked (no UI for confirmation)
README.md
build
```

```bash
M7_CMD=ls EXT=$PWD/m7-faux-bash.ts WORK=/tmp/pi-lab/m7-work-24 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-24 ./run.sh -e "$GATE" -p "列出文件" 2>&1
```

```
Running without API keys...
工具结果（isError=false）：README.md
```

再用 json 模式看工具事件：

```bash
mkdir -p /tmp/pi-lab/m7-work-25/build
EXT=$PWD/m7-faux-bash.ts WORK=/tmp/pi-lab/m7-work-25 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-25 ./run.sh -e "$GATE" --mode json "清理构建目录" > /tmp/pi-lab/m7-gate.jsonl
grep -E '^\{"type":"(tool_execution_start|tool_execution_end)"' /tmp/pi-lab/m7-gate.jsonl
grep -m1 '^{"type":"message_end","message":{"role":"toolResult"' /tmp/pi-lab/m7-gate.jsonl
```

```
{"type":"tool_execution_start","toolCallId":"call_1","toolName":"bash","args":{"command":"rm -rf build"}}
{"type":"tool_execution_end","toolCallId":"call_1","toolName":"bash","result":{"content":[{"type":"text","text":"Dangerous command blocked (no UI for confirmation)"}],"details":{}},"isError":true}
{"type":"message_end","message":{"role":"toolResult","toolCallId":"call_1","toolName":"bash","content":[{"type":"text","text":"Dangerous command blocked (no UI for confirmation)"}],"details":{},"isError":true,"timestamp":1793528170142}}
```

**结论。** 预测成立。`rm -rf build` 被拦下，`build` 还在，模型收到的工具结果就是 `reason` 文本。和 M2 §2.2 里正常执行的 read 相比，被拦截的 `tool_execution_end` 没有 `durationMs`（工具没有运行），`result.details` 是 `createErrorToolResult` 给的空对象。`tool_execution_start` 照常发出了，所以界面会先显示"开始执行"，再显示错误。

在 TUI 或 RPC 模式下，`ctx.hasUI` 为 `true`，permission-gate 会调用 `ctx.ui.select()` 弹出选择框。handler 被 await，循环停在 `prepareToolCall` 里等用户选择（`runner.ts` 的 `withUIPrompt` 同时发出 `ui_prompt_start`/`ui_prompt_end`）。

### 7.2 handler 的顺序、原地改参数和异常

**问题。** [`lab/m7-handlers.ts`](lab/m7-handlers.ts) 的 `rewrite` 变体在 `tool_call` 里把命令中的 `rm -rf build` 原地改成 `ls`。和 permission-gate 一起加载，`-e` 的顺序分别是 (a) rewrite 在前、(b) gate 在前，结果有什么不同？会话文件里记录的 assistant 工具调用参数是哪一个？`throw` 变体让 `tool_execution_start` 和 `tool_call` 的 handler 各抛一个异常，bash 会执行吗？

**预测。**

- (a)：rewrite 先执行，把 `event.input.command` 改成 `ls`；gate 拿到同一个事件对象，看到 `ls`，放行；bash 执行 `ls`（§4.2 "共享事件对象"）。
- (b)：gate 先执行，看到 `rm -rf build`，返回 `block`，短路，rewrite 根本不会被调用，stderr 里没有 `[m7-handlers]` 行。
- 会话文件里 assistant 消息的 `toolCall.arguments` 仍是 `rm -rf build`：被修改的是校验后的 `args` 对象，不是 assistant 消息（§4.1）。json 模式的 `tool_execution_start.args` 也是原始参数，因为它在 `tool_call` 之前发出。
- `throw`：`tool_execution_start` 的异常被 `emit()` 捕获，print 模式打印 `Extension error (...)`，继续；`tool_call` 的异常传到 `prepareToolCall` 的 `catch`，工具不执行，结果文本是异常消息，`build` 仍在。

**运行。**

```bash
mkdir -p /tmp/pi-lab/m7-work-26/build /tmp/pi-lab/m7-work-27/build
M7_VARIANT=rewrite EXT=$PWD/m7-faux-bash.ts WORK=/tmp/pi-lab/m7-work-26 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-26 ./run.sh -e "$PWD/m7-handlers.ts" -e "$GATE" -p "清理构建目录" 2>&1
```

```
Running without API keys...
[m7-handlers] tool_call 收到 {"command":"rm -rf build"}
[m7-handlers] tool_call 改为 {"command":"ls"}
工具结果（isError=false）：README.md
build
```

```bash
M7_VARIANT=rewrite EXT=$PWD/m7-faux-bash.ts WORK=/tmp/pi-lab/m7-work-27 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-27 ./run.sh -e "$GATE" -e "$PWD/m7-handlers.ts" -p "清理构建目录" 2>&1
```

```
Running without API keys...
工具结果（isError=true）：Dangerous command blocked (no UI for confirmation)
```

(a) 的会话文件最后三个条目：

```bash
node show-session.mjs "$(ls -t /tmp/pi-lab/m7-agent-26/sessions/*/*.jsonl | head -1)" | tail -3
```

```
message                id=09a5d30a parent=82407695 assistant  [{"type":"toolCall","id":"call_1","name":"bash","arguments":{"command":"rm -rf build"}}] stop=toolUse
message                id=d4105d91 parent=09a5d30a toolResult [{"type":"text","text":"README.md\nbuild\n"}]
message                id=ca6b167e parent=d4105d91 assistant  [{"type":"text","text":"工具结果（isError=false）：README.md\nbuild"}] stop=stop
```

`throw` 变体（输出里 lab 目录的绝对路径缩写为 `…省略…/lab`）：

```bash
mkdir -p /tmp/pi-lab/m7-work-28/build
M7_VARIANT=throw EXT=$PWD/m7-faux-bash.ts WORK=/tmp/pi-lab/m7-work-28 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-28 ./run.sh -e "$PWD/m7-handlers.ts" -p "清理构建目录" 2>&1; ls /tmp/pi-lab/m7-work-28
```

```
Running without API keys...
[m7-handlers] tool_execution_start 抛异常
Extension error (…省略…/lab/m7-handlers.ts): tool_execution_start handler 出错
[m7-handlers] tool_call 抛异常
工具结果（isError=true）：tool_call handler 出错
README.md
build
```

**结论。** 四个预测都成立。(a) 中 bash 实际执行了 `ls`，而 assistant 条目记录的仍是 `rm -rf build`：transcript 里的调用参数和实际执行的参数可以不一致，读会话文件时要注意这一点。(b) 中没有 `[m7-handlers]` 输出，说明 `block` 短路了后面的扩展。所以扩展的加载顺序决定了权限检查和参数改写谁先生效。`throw` 中两个异常的去向不同：一个变成 stderr 上的 `Extension error`，运行继续；另一个变成工具的错误结果，`build` 没被删。

### 7.3 context 事件只改请求，不改会话

**问题。** `trim` 变体在 `context` 里只保留最后 2 条消息。主线场景第 2 次请求时，模型收到几条消息？system 消息还在吗？会话文件里少了消息吗？

**预测。** 第 1 次请求 `context` 只看到 `[user]`，保留 2 条后列表不变，`restoreSystemMessages` 原样返回，模型收到 2 条。第 2 次看到 `[user, assistant, toolResult]`，返回 `[assistant, toolResult]`；列表变了，`getCurrentSystemMessage` 放回最前面，模型收到 3 条：system、assistant、toolResult。会话文件不受影响，仍是 README §5.3 的完整 5 条消息（`structuredClone`，§4.8）。

**运行。**

```bash
M7_VARIANT=trim PI_TRACE=1 WORK=/tmp/pi-lab/m7-work-29 PI_CODING_AGENT_DIR=/tmp/pi-lab/m7-agent-29 ./run.sh -e "$PWD/m7-handlers.ts" -p "读 README.md，总结成三句话" 2>&1
node show-session.mjs "$(ls -t /tmp/pi-lab/m7-agent-29/sessions/*/*.jsonl | head -1)" | cut -c1-90
```

```
Running without API keys...
[m7-handlers] context 收到 [user]，返回 [user]
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
[m7-handlers] context 收到 [user,assistant,toolResult]，返回 [assistant,toolResult]
[faux] 第 2 次请求，收到 3 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

```
session                id=01a1969c parent=-        {"version":3,"cwd":"/private/tmp/pi-lab
model_change           id=e9bcb2d9 parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=dcb7e8b1 parent=e9bcb2d9 {"thinkingLevel":"off"}
message                id=73855f6f parent=dcb7e8b1 system     "" sections=preamble,tools,r
message                id=802ed6fc parent=73855f6f user       [{"type":"text","text":"读 RE
message                id=d9c385f7 parent=802ed6fc assistant  [{"type":"text","text":"我先读一
message                id=7a2a0bd7 parent=d9c385f7 toolResult [{"type":"text","text":"# De
message                id=dccf7982 parent=7a2a0bd7 assistant  [{"type":"text","text":"这是一个
```

**结论。** 预测成立。第 2 次请求少了 user 消息，但 system 消息被放回了开头。会话文件仍有 system、user、assistant、toolResult、assistant 五条消息。`context` 事件适合做"每次请求前的裁剪"，不需要也不能修改会话历史；要永久改变上下文，应该在 `turn_end` 里追加 `context_edit` 或 `compaction` 草稿（§4.9）。

---

## 8. 自测与衔接

### 8.1 自测

1. 一个扩展在工厂函数里直接调用 `pi.sendMessage(...)`，会发生什么？如果改成在 `session_start` handler 里调用呢？
2. 扩展 A 订阅了 `tool_result`，返回 `{ content: [...] }` 替换了 read 工具的文本，没有返回 `structuredContent`。M2 最终发出的 `tool_execution_end` 里还有 `structuredContent` 吗？
3. 两个扩展都订阅了 `session_before_compact`，第一个返回 `{ compaction: ... }`，第二个返回 `{ cancel: true }`。压缩会执行吗？

<details>
<summary>答案</summary>

1. 工厂函数执行时 runtime 还没绑定，`sendMessage` 是 `notInitialized` 桩函数，抛出 `Extension runtime not initialized...`（`packages/coding-agent/src/core/extensions/loader.ts:161-163`）。异常让 `initializeExtension` 调用 `discard()`，这个扩展加载失败，进入 `errors`。`session_start` 在 `bindCore()` 之后发出（`packages/coding-agent/src/core/agent-session.ts:3360`），这时 `sendMessage` 已经是 AgentSession 的真实实现，可以正常调用。
2. 没有。`emitToolResult` 在只替换 `content` 时删掉 `structuredContent`（`packages/coding-agent/src/core/extensions/runner.ts:1204`），返回值里 `structuredContent` 为 `undefined`；`_afterToolCall` 把它原样交给 M2（`packages/coding-agent/src/core/agent-session.ts:736`）。M2 的 `finalizeExecutedToolCall` 发现返回了 `content` 而 `structuredContent` 为空，也删掉 `structuredContent`（`packages/agent/src/agent-loop.ts:904`）。
3. 不会。`emit()` 对 `session_before_*` 事件记录每个非空结果，遇到 `cancel: true` 立即返回（`packages/coding-agent/src/core/extensions/runner.ts:1105-1108`）。第二个扩展的取消覆盖了第一个扩展的结果，AgentSession 看到 `result.cancel` 就放弃压缩（`packages/coding-agent/src/core/agent-session.ts:2888`）。

</details>

### 8.2 衔接

本模块从 `emitToolCall` 出发，读了扩展怎么加载、事件怎么派发。扩展的事件是由 AgentSession 在各个时刻调用 runner 发出的：`_installAgentToolHooks`、`_installAgentBoundaryHooks`、`_handleAgentEvent`、`_prepareRun` 里的 `before_agent_start`、`_buildRuntime` 里的 `bindCore`。这些方法属于 M8。M8 从 `createAgentSession()`（`packages/coding-agent/src/core/sdk.ts:191`）读起，看 AgentSession 怎么把会话存储、压缩、扩展和 M2 的循环组装在一起。

本模块标了"→ M3"的扩展工具包装（`wrapper.ts` → `wrapToolDefinition`）在 M3 读；MCP 本身就是一个内置扩展，它怎样通过 `tool_call` 做权限检查、怎样用 `mcp_servers_change` 连接服务器，在 M10 读。
