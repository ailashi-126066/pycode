# M8 AgentSession：把循环接成产品

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

M2 的 `Agent` 只会跑循环：拿一组 Agent 消息和工具，请求模型，执行工具，发事件。它不知道会话文件、扩展、系统提示词、压缩和重试。

`AgentSession`（`packages/coding-agent/src/core/agent-session.ts`）把这些产品功能接到 `Agent` 上。它做四件事：

- **安装 hook。** 构造时给 `agent.prepareRequest`、`agent.finishTurn`、`agent.beforeToolCall` 等字段赋值。之后每次请求前，它用会话文件重建上下文；每轮结束时，它派发扩展的 `turn_end`；每次工具调用前后，它派发扩展的 `tool_call`、`tool_result`。
- **订阅事件。** 它订阅 `Agent` 的每个 AgentEvent：先转给扩展，再转给自己的订阅者，最后在 `message_end` 时把消息写进会话文件。
- **管一次运行的前后。** `prompt()` 在调用 `agent.prompt()` 之前处理扩展命令、模板展开、`before_agent_start`、系统提示词补丁；`agent.prompt()` 返回之后，它判断要不要自动重试、要不要压缩，必要时调用 `agent.continue()` 再跑一次，最后发出 `agent_settled`。
- **对外提供一个统一的接口。** print、json、rpc、TUI 和 SDK 都只和 `AgentSession` 打交道（→ M9）。

`sdk.ts` 的 `createAgentSession()` 负责把 `Agent` 和 `AgentSession` 组装起来，是本模块的入口。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M8 占了两段：创建会话，以及一次 `session.prompt()` 在 `agent.prompt()` 外面包的那一层。

```
createAgentSessionRuntime(createRuntime)                      [M9]
├─ createAgentSessionServices()                                [M8] §4.18
└─ createAgentSessionFromServices() → createAgentSession()     [M8] ← 本模块入口 §4.1
    ├─ new Agent({ systemPrompt: "", tools: [], convertToLlm… }) → M2
    └─ new AgentSession()                                      §4.3
        ├─ _install*()  6 组 hook                              §4.4–4.7
        └─ _buildRuntime()  工具、扩展运行器                    §4.8 → M3 / M7
session.prompt(text)                                           §4.9
└─ _startRun()                                                 §4.10
    ├─ _prepareRun()  before_agent_start、系统提示词补丁         §4.11 → M6 / M7
    ├─ agent.prompt(messages)                                  → M2
    │   └─ 每个事件 → _handleAgentEvent()  转发 + 持久化        §4.12 → M4 / M7
    ├─ _handlePostAgentRun()                                   §4.13
    │   ├─ _isRetryableError() / _prepareRetry()  自动重试      §4.14
    │   └─ _checkCompaction()                                  §4.15 → M5
    │   需要时 agent.continue()，回到 _handlePostAgentRun
    └─ _emitAgentSettled()                                     §4.16
steer() / followUp() / clearQueue() / abort()                  §4.17
```

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/coding-agent/src/core/agent-session.ts` | 4479 | `AgentSession` 类 | 精读 hook 安装、运行生命周期、事件处理、重试、队列和中止（约 1000 行）；模型切换、会话切换、导出、bash 执行、工具注册表的细节略读 |
| `packages/coding-agent/src/core/sdk.ts` | 481 | `createAgentSession()`：解析模型和思考级别，创建 `Agent` 和 `AgentSession` | 精读 |
| `packages/coding-agent/src/core/messages.ts` | 196 | coding-agent 的 4 种自定义 Agent 消息和 `convertToLlm` | 精读 |
| `packages/coding-agent/src/core/agent-session-services.ts` | 233 | 和 cwd 绑定的服务（模型运行时、设置、资源加载器） | 通读 |
| `packages/coding-agent/src/core/agent-session-runtime.ts` | 447 | `AgentSessionRuntime`：持有当前会话，`/new`、`/resume`、`/fork` 时整体替换 | 通读创建和替换流程 |

行数用 `wc -l` 统计。

`agent-session.ts` 很长，但大部分代码属于其他模块的话题：工具注册表和系统提示词（M6）、压缩的执行（M5）、扩展的事件（M7）、会话树导航（M4）。本模块只读它把这些部分串起来的骨架。

### 1.4 读完能回答的问题

1. `createAgentSession()` 创建 `Agent` 时传的 `systemPrompt` 是空串、`tools` 是空数组。模型收到的系统提示词和工具清单是从哪里来的？
2. 模型请求的上下文以谁为准：`agent.state.messages`，还是会话文件？
3. 一条消息在什么时刻写进会话文件？扩展、订阅者和会话文件，谁先看到它？
4. 模型返回 `503 overloaded` 时，失败的回复会留在会话文件里吗？下一次请求还能看到它吗？
5. `agent_end` 和 `agent_settled` 有什么区别？`isStreaming` 什么时候变回 `false`？

---

## 2. 先跑一次

### 2.1 主线场景里 M8 加的事件

用 json 模式运行主线场景：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m8-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent ./run.sh --mode json "读 README.md，总结成三句话" > /tmp/pi-lab/m8-events.jsonl
node summarize-events.mjs /tmp/pi-lab/m8-events.jsonl
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
message_update [text_delta]
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
message_update [text_delta] x2
message_update [text_end]
message_end (assistant)
turn_end (assistant)
agent_end
agent_settled
共 31 行（含第一行 session 头）
```

这个事件流已经过 `AgentSession` 一层。和 M2 的 AgentEvent 相比，M8 改了两处：

- `agent_end` 多了一个 `willRetry` 字段（§4.12）。
- 最后多了一个 `agent_settled`，它是 `AgentSession` 自己发的（§4.16）。

```bash
grep -E '^\{"type":"agent_(end|settled)"' /tmp/pi-lab/m8-events.jsonl | node -e 'for (const l of require("fs").readFileSync(0,"utf8").trim().split("\n")) { const e = JSON.parse(l); console.log(e.type, JSON.stringify({ ...e, messages: e.messages && e.messages.map((m) => m.role) })); }'
```

```
agent_end {"type":"agent_end","messages":["system","user","assistant","toolResult","assistant"],"willRetry":false}
agent_settled {"type":"agent_settled","aborted":false}
```

第一行 `session` 也不是事件，它是会话文件头，由 print/json 模式输出（→ M9）。

### 2.2 用 SDK 直接创建会话

[`lab/m8-session.ts`](lab/m8-session.ts) 不经过命令行，直接调用 `createAgentSession()`。模型是 faux 假模型，回复和主线场景一样：第 1 次调用 read，第 2 次输出总结。脚本订阅 `session.subscribe()`，每个事件打一行；`message_update` 合并成一行；`message_end` 后面附上"此刻会话文件里有几个条目"。

```bash
WORK=/tmp/pi-lab/m8-work-1 PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-1 node --experimental-strip-types m8-session.ts dump
```

```
[Agent] 已设置的 hook：convertToLlm, transformContext, prepareRequest, prepareNextTurnWithContext, finishTurn, beforeToolCall, afterToolCall
[Agent] state.systemPrompt=""
[Agent] state.tools=["read","bash","edit","write"]
[Agent] state.messages.length=0
[AgentSession] isStreaming=false isCompacting=false isRetrying=false isBashRunning=false isIdle=true
[SessionManager] 内存中的条目：model_change, thinking_level_change；会话文件已存在=false
场景：dump
  26ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  26ms  turn_start
  26ms  message_start (system)
  26ms  message_end (system)  [此刻会话条目数=2]
  27ms  message_start (user)
  27ms  message_end (user)  [此刻会话条目数=3]
  28ms  [模型] 第 1 次请求，收到 2 条 LLM 消息：system, user
  29ms  message_start (assistant)
  29ms  message_update x8
  29ms  message_end (assistant) stopReason=toolUse  [此刻会话条目数=4]
  29ms  tool_execution_start read
  35ms  tool_execution_end read
  35ms  message_start (toolResult)
  35ms  message_end (toolResult)  [此刻会话条目数=5]
  35ms  turn_end (assistant)
  36ms  turn_start
  37ms  [模型] 第 2 次请求，收到 4 条 LLM 消息：system, user, assistant, toolResult
  37ms  message_start (assistant)
  37ms  message_update x4
  37ms  message_end (assistant) stopReason=stop  [此刻会话条目数=6]
  37ms  turn_end (assistant)
  37ms  agent_end willRetry=false messages=[system, user, assistant, toolResult, assistant]  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  37ms  agent_settled aborted=false  {isStreaming=false isCompacting=false isRetrying=false isBashRunning=false}
  37ms  [prompt 返回] isStreaming=false isCompacting=false isRetrying=false isBashRunning=false isIdle=true 模型请求次数=2
[AgentState] messages=system, user, assistant, toolResult, assistant
[会话文件] $PI_CODING_AGENT_DIR/sessions/2026-11-01T07-19-41-936Z_01a195fa-7670-77f1-b31e-3bce7302a5b0.jsonl
  session                 id=01a195fa
  model_change            id=13bbc050
  thinking_level_change   id=a7e8d3fb
  message               system     ""  id=c674508a
  message               user       [{"type":"text","text":"读 README.md，总结成三句话"}]  id=ae986db1
  message               assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall"," stop=toolUse  id=ec805b1b
  message               toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nI  id=deea1bca
  message               assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。" stop=stop  id=051e9e80
```

前 6 行是 `createAgentSession()` 返回后、调用 `prompt()` 之前的状态，值得逐行看：

- `Agent` 上 7 个 hook 都已设置。`sdk.ts` 只传了 `convertToLlm` 和 `transformContext`，其余 5 个由 `AgentSession` 构造函数装上（§4.3）。
- `state.systemPrompt` 是空串。系统提示词不放在这里，而是作为一条 `system` 消息进 transcript（§4.11）。
- `state.tools` 已经是 `read, bash, edit, write`。`sdk.ts` 传进去的是空数组，是 `_buildRuntime()` 填上的（§4.8）。
- 会话管理器的内存里已有 `model_change` 和 `thinking_level_change` 两个条目，但会话文件还不存在。M4 讲过，文件要等第一条 assistant 消息出现才创建。

再看事件部分：每个 `message_end` 之后，会话条目数都加 1。`message_end (system)` 时是 2，说明处理到这个事件时 system 消息还没写入（写入发生在转发之后，§4.12）。下一个事件 `message_end (user)` 时是 3，这时 system 已经写入。

最后，`agent_end` 时 `isStreaming` 仍是 `true`，`agent_settled` 时才变成 `false`。

---

## 3. 数据结构

### 3.1 AgentSessionConfig：构造参数

`createAgentSession()` 把解析好的东西装进这个对象，交给 `new AgentSession()`。

源码：[`packages/coding-agent/src/core/agent-session.ts:265-313`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L265-L313)

```ts
export interface AgentSessionConfig {
	agent: Agent;
	sessionManager: SessionManager;
	settingsManager: SettingsManager;
	cwd: string;
	/** Models to cycle through with Ctrl+P (from --models flag) */
	scopedModels?: Array<{ model: Model<any>; thinkingLevel?: ThinkingLevel }>;
	/** Resource loader for extensions, skills, prompts, themes, context files, and system prompt */
	resourceLoader: ResourceLoader;
	/** SDK custom tools registered outside extensions */
	customTools?: ToolDefinition[];
	/** Canonical model/auth runtime used by coding-agent internals. */
	modelRuntime: ModelRuntime;
	/** Keeps the prompt cache entry of the last session request warm. */
	cacheWarmer?: Pick<CacheWarmer, "cancel" | "status" | "onAgentSettled" | "onModeChanged" | "onWarmed">;
	/** Initial active built-in tool names. Default: [read, bash, edit, write] */
	initialActiveToolNames?: string[];
	/**
	 * Whether the initial tools come from the `defaultTools` setting. When true, a resumed session
	 * replaces them with the loadout its transcript records, with `defaultToolModifiers` applied, and
	 * reload activates tools newly added to the setting. Tools removed from it stay active.
	 */
	usesDefaultTools?: boolean;
	/**
	 * `+name`/`-name` entries applied on top of the `defaultTools` setting, from `--tools`. Reload
	 * applies them to the reloaded setting too, so a removed tool stays removed.
	 */
	defaultToolModifiers?: string[];
	/**
	 * Optional allowlist of tool names or patterns (`*` matches any characters). When provided, only
	 * matching tools are exposed. A non-empty list without `mcp__` entries also keeps MCP tools
	 * registered for codemode and tool_search; only tool_search can declare them. An empty list
	 * exposes no tools.
	 */
	allowedToolNames?: string[];
	/** Optional denylist of tool names or patterns. When provided, matching tools are not exposed. */
	excludedToolNames?: string[];
	/**
	 * Override base tools (useful for custom runtimes).
	 *
	 * These are synthesized into minimal ToolDefinitions internally so AgentSession can keep
	 * a definition-first registry even when callers provide plain AgentTool instances.
	 */
	baseToolsOverride?: Record<string, AgentTool>;
	/** Mutable ref used by Agent to access the current ExtensionRunner */
	extensionRunnerRef?: { current?: ExtensionRunner };
	/** Session start event metadata emitted when extensions bind to this runtime. */
	sessionStartEvent?: SessionStartEvent;
}
```

| 字段 | 说明 |
|---|---|
| `agent` | M2 的 `Agent`。`AgentSession` 不创建它，只往它身上装 hook |
| `sessionManager` | M4 的会话存储。会话文件是上下文的唯一来源（§4.5） |
| `settingsManager` | M6 的设置。重试、压缩、图片等开关都从这里读 |
| `cwd` | 工作目录，工具和资源发现都相对它 |
| `resourceLoader` | M6 的资源加载器：扩展、skills、AGENTS.md、提示词模板 |
| `modelRuntime` | M1 的模型运行时，负责认证和请求 |
| `customTools` | SDK 调用方额外注册的工具 |
| `initialActiveToolNames` | 初始激活的工具名，默认 `read, bash, edit, write` |
| `usesDefaultTools`、`defaultToolModifiers` | 恢复旧会话时，是否按 transcript 里记录的工具清单恢复（§4.3） |
| `allowedToolNames`、`excludedToolNames` | `--tools`、`--exclude-tools` 的白名单和黑名单 |
| `extensionRunnerRef` | 一个可变引用。`sdk.ts` 里的 `transformContext` 等闭包通过它拿到当前的扩展运行器，扩展重载后不用重建闭包 |
| `sessionStartEvent` | 扩展绑定时发出的 `session_start` 事件 |
| `cacheWarmer`、`scopedModels`、`baseToolsOverride` | 缓存预热、模型轮换列表、替换内置工具。本模块不展开 |

**真实实例。** §2.2 的 `dump` 场景里，`sdk.ts` 传入的是：`initialActiveToolNames` 为 `["read","bash","edit","write"]`（没有设置 `tools` 选项），`usesDefaultTools` 为 `true`，`allowedToolNames`、`excludedToolNames` 都是 `undefined`。构造完成后 `state.tools` 就是这 4 个工具。

### 3.2 AgentSession 的运行状态字段

`AgentSession` 有几十个私有字段。和运行生命周期有关的是这一段：

源码：[`packages/coding-agent/src/core/agent-session.ts:399-435`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L399-L435)

```ts
	// Event subscription state
	private _unsubscribeAgent?: () => void;
	private _eventListeners: AgentSessionEventListener[] = [];
	private _isAgentRunActive = false;
	private _agentRunAbortRequested = false;
	private _idleWaitPromise: Promise<void> | undefined;
	private _resolveIdleWait: (() => void) | undefined;

	/** Tracks pending steering messages for UI display. Removed when delivered. */
	private _steeringMessages: string[] = [];
	/** Tracks pending follow-up messages for UI display. Removed when delivered. */
	private _followUpMessages: string[] = [];
	/** Messages queued to be included with the next user prompt as context ("asides"). */
	private _pendingNextTurnMessages: CustomMessage[] = [];
	/** Context-only custom messages queued during a run, flushed once the current turn's tool results are in. */
	private _pendingCustomMessages: CustomMessage[] = [];

	// Compaction state
	private _compactionAbortController: AbortController | undefined = undefined;
	private _autoCompactionAbortController: AbortController | undefined = undefined;
	private _overflowRecoveryAttempted = false;

	// Branch summarization state
	private _branchSummaryAbortController: AbortController | undefined = undefined;

	// Retry state
	private _retryAbortController: AbortController | undefined = undefined;
	private _retryAttempt = 0;
	/**
	 * Failed response that the next request repeats, set by auto-retry and overflow recovery. The
	 * retry is routed with it as `failed`, since the context no longer contains it.
	 */
	private _failedResponse: AssistantMessage | undefined;

	// Bash execution state
	private readonly _bashAbortControllers = new Set<AbortController>();
	private _pendingBashMessages: BashExecutionMessage[] = [];
```

| 字段 | 说明 |
|---|---|
| `_isAgentRunActive` | 从 `_startRun()` 开头到 `_emitAgentSettled()` 为止都是 `true`。公开的 `isStreaming` 就是它（§4.17） |
| `_agentRunAbortRequested` | `abort()` 设置。`_startRun()` 的后处理循环看到它就停 |
| `_idleWaitPromise`、`_resolveIdleWait` | `waitForIdle()` 等待的 Promise |
| `_steeringMessages`、`_followUpMessages` | 排队消息的文本，只给界面显示用。真正的队列在 `Agent` 里（M2） |
| `_pendingNextTurnMessages` | 扩展用 `deliverAs: "nextTurn"` 发的消息，下一次 `prompt()` 时一起发出 |
| `_pendingCustomMessages` | 运行中不触发新轮次的自定义消息，等这一轮的工具结果都写完再追加 |
| `_compactionAbortController`、`_autoCompactionAbortController`、`_branchSummaryAbortController` | 不为空表示正在压缩或生成分支摘要。公开的 `isCompacting` 看这三个 |
| `_overflowRecoveryAttempted` | 上下文溢出后"压缩再重试"只做一次 |
| `_retryAbortController` | 不为空表示正在重试的退避等待中。公开的 `isRetrying` 看它 |
| `_retryAttempt` | 当前是第几次重试，0 表示没有在重试 |
| `_failedResponse` | 重试前失败的那条回复，交给虚拟模型路由参考 |
| `_bashAbortControllers` | 正在执行的用户 bash 命令（`!` 前缀）。公开的 `isBashRunning` 看它是否非空 |
| `_pendingBashMessages` | 运行中执行的 bash 命令结果，等运行结束再写入 |

**真实实例。** §2.2 输出里每个 `{…}` 是 4 个公开标志在那一刻的值。`isRetrying`、`isCompacting` 变成 `true` 的实例见 §7.2、§7.3。

### 3.3 AgentSessionEvent：订阅者看到的事件

源码：[`packages/coding-agent/src/core/agent-session.ts:209-250`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L209-L250)

```ts
export type AgentSessionEvent =
	| WithParentToolCallId<Exclude<AgentEvent, { type: "agent_end" }>>
	| {
			type: "agent_end";
			messages: AgentMessage[];
			willRetry: boolean;
	  }
	| { type: "agent_settled"; aborted: boolean }
	| {
			type: "queue_update";
			steering: readonly string[];
			followUp: readonly string[];
	  }
	| { type: "compaction_start"; reason: "manual" | "threshold" | "overflow" }
	| { type: "entry_appended"; entry: SessionEntry }
	| { type: "session_info_changed"; name: string | undefined }
	| { type: "thinking_level_changed"; level: ThinkingLevel }
	| {
			type: "compaction_end";
			reason: "manual" | "threshold" | "overflow";
			result: CompactionResult | undefined;
			aborted: boolean;
			willRetry: boolean;
			errorMessage?: string;
	  }
	| { type: "auto_retry_start"; attempt: number; maxAttempts: number; delayMs: number; errorMessage: string }
	| { type: "auto_retry_end"; success: boolean; attempt: number; finalError?: string }
	| {
			type: "summarization_retry_scheduled";
			attempt: number;
			maxAttempts: number;
			delayMs: number;
			errorMessage: string;
	  }
	| { type: "summarization_retry_attempt_start"; source: "branchSummary" }
	| {
			type: "summarization_retry_attempt_start";
			source: "compaction";
			reason: "manual" | "threshold" | "overflow";
	  }
	| { type: "summarization_retry_finished" }
	| { type: "bash_execution_update"; id?: string; delta: string };
```

这是 README §3.2 事件层次里 AgentEvent 上面的一层。它由三部分组成：

| 来源 | 事件 | 说明 |
|---|---|---|
| AgentEvent 原样转发 | `agent_start`、`turn_*`、`message_*`、`tool_execution_*` | 工具事件可能多一个 `parentToolCallId`，表示这是另一个工具通过 `ctx.executeTool()` 发起的嵌套调用（M3） |
| AgentEvent 加字段 | `agent_end` | 加了 `willRetry`：这次运行结束后会不会自动重试 |
| AgentSession 自己发 | `agent_settled` | 整个 `prompt()` 结束，会话空闲。`aborted` 表示是否被中止 |
| | `queue_update` | 排队消息变化，带当前两个队列的文本 |
| | `compaction_start`、`compaction_end` | 自动或手动压缩。`reason` 为 `manual`、`threshold`、`overflow` |
| | `auto_retry_start`、`auto_retry_end` | 自动重试的开始和结束 |
| | `entry_appended` | 不是由消息产生的会话条目被追加，例如重试时的 `context_edit` |
| | `session_info_changed`、`thinking_level_changed` | 会话名、思考级别变化 |
| | `summarization_retry_*` | 压缩或分支摘要请求的重试 |
| | `bash_execution_update` | 用户 bash 命令的输出片段 |

订阅函数的类型是 `(event) => void`（`packages/coding-agent/src/core/agent-session.ts:253`），返回值不被 await。这一点和 M2 的 `Agent.subscribe()` 不同（§6.3）。

**真实实例。** §2.2 的输出是一次完整运行的 AgentSessionEvent 序列。`queue_update` 见 §4.17，`auto_retry_*` 和 `entry_appended` 见 §7.2，`compaction_*` 见 §7.3。

### 3.4 AgentSessionServices 和 CreateAgentSessionResult

M9 的命令行入口不直接调用 `createAgentSession()`，而是先建一组和 cwd 绑定的服务，再用服务创建会话：

源码：[`packages/coding-agent/src/core/agent-session-services.ts:67-80`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-services.ts#L67-L80)

```ts
/**
 * Coherent cwd-bound runtime services for one effective session cwd.
 *
 * This is infrastructure only. The AgentSession itself is created separately so
 * session options can be resolved against these services first.
 */
export interface AgentSessionServices {
	cwd: string;
	agentDir: string;
	modelRuntime: ModelRuntime;
	settingsManager: SettingsManager;
	resourceLoader: ResourceLoader;
	diagnostics: AgentSessionRuntimeDiagnostic[];
}
```

源码：[`packages/coding-agent/src/core/sdk.ts:108-116`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L108-L116)

```ts
/** Result from createAgentSession */
export interface CreateAgentSessionResult {
	/** The created session */
	session: AgentSession;
	/** Extensions result (for UI context setup in interactive mode) */
	extensionsResult: LoadExtensionsResult;
	/** Warning if session was restored with a different model than saved */
	modelFallbackMessage?: string;
}
```

| 类型 | 字段 | 说明 |
|---|---|---|
| `AgentSessionServices` | `cwd`、`agentDir` | 工作目录和配置目录（`PI_CODING_AGENT_DIR`） |
| | `modelRuntime`、`settingsManager`、`resourceLoader` | 三个服务，见 §3.1 |
| | `diagnostics` | 创建过程中收集的警告和错误，例如扩展注册 provider 失败。由调用方决定是否打印、是否退出 |
| `CreateAgentSessionResult` | `session` | 创建好的 `AgentSession` |
| | `extensionsResult` | 加载的扩展，交互模式用它设置界面 |
| | `modelFallbackMessage` | 恢复会话时，原来的模型不可用而换成了别的模型，这里是提示文字 |

**真实实例。** `m8-session.ts services` 走 M9 的这条路径，用 `additionalExtensionPaths` 加载 `lab/faux-ext.ts` 注册假模型：

```bash
WORK=/tmp/pi-lab/m8-work-svc PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-svc node --experimental-strip-types m8-session.ts services
```

```
[AgentSessionServices] 字段：cwd, agentDir, modelRuntime, settingsManager, resourceLoader, diagnostics
[AgentSessionServices] cwd=$WORK agentDir=$PI_CODING_AGENT_DIR
[AgentSessionServices] diagnostics=[]
[AgentSessionServices] 扩展：faux-ext.ts
[AgentSessionServices] faux 模型已注册=true
[CreateAgentSessionResult] 字段：session, extensionsResult, modelFallbackMessage
[AgentSession] model=faux/faux-1 tools=["read","bash","edit","write"]
```

### 3.5 coding-agent 的自定义 Agent 消息

M2 §3.1 讲过，coding-agent 用声明合并往 `AgentMessage` 里加了 4 种消息（`packages/coding-agent/src/core/messages.ts:70-77`）。它们的定义在同一个文件：

源码：[`packages/coding-agent/src/core/messages.ts:26-68`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/messages.ts#L26-L68)

```ts
/**
 * Message type for bash executions via the ! command.
 */
export interface BashExecutionMessage {
	role: "bashExecution";
	command: string;
	output: string;
	exitCode: number | undefined;
	cancelled: boolean;
	truncated: boolean;
	fullOutputPath?: string;
	timestamp: number;
	/** If true, this message is excluded from LLM context (!! prefix) */
	excludeFromContext?: boolean;
}

/**
 * Message type for extension-injected messages via sendMessage().
 * These are custom messages that extensions can inject into the conversation.
 */
export interface CustomMessage<T = unknown> {
	role: "custom";
	customType: string;
	content: string | (TextContent | ImageContent)[];
	display: boolean;
	details?: T;
	timestamp: number;
}

export interface BranchSummaryMessage {
	role: "branchSummary";
	summary: string;
	fromId: string | null;
	timestamp: number;
}

export interface CompactionSummaryMessage {
	role: "compactionSummary";
	summary: string;
	tokensBefore: number;
	timestamp: number;
}

```

| role | 谁产生 | 怎么持久化 | 发给模型时 |
|---|---|---|---|
| `bashExecution` | 用户在输入框里用 `!cmd` 执行的命令（`executeBash`） | `sessionManager.appendMessage()`，在运行之外写入 | 转成 user 消息；`excludeFromContext`（`!!` 前缀）为真时丢掉 |
| `custom` | 扩展的 `pi.sendMessage()` | `appendCustomMessageEntry()`，成为 `custom_message` 条目 | 转成 user 消息 |
| `branchSummary` | 会话树导航时生成的分支摘要（M4） | 由 `branch_summary` 条目投影出来 | 加前缀包成 user 消息 |
| `compactionSummary` | 压缩（M5） | 由 `compaction` 条目投影出来 | 加前缀包成 user 消息 |

**真实实例。** §7.3 的压缩实验结束后，`agent.state.messages` 的 role 是 `system, compactionSummary, assistant`。`compactionSummary` 不是哪个事件写进去的，它是 `compaction` 条目经过 `buildSessionProjection()` 投影出来的。

### 3.6 类型关系图

```
 createAgentSession(options)                      sdk.ts
   │ AgentSessionServices 的三个服务 + sessionManager
   ├─▶ new Agent({ systemPrompt: "", tools: [], messages: 会话里已有的消息,
   │              convertToLlm, streamFn, transformContext })      → M2
   └─▶ new AgentSession(AgentSessionConfig)
          │
          │ 构造时给 agent 的 hook 字段赋值
          ├─ agent.prepareRequest  ──读──▶ sessionManager.buildSessionProjection()
          ├─ agent.prepareNextTurnWithContext / finishTurn / before/afterToolCall
          ├─ agent.transformContext（两层包装）
          │
          │ agent.subscribe(_handleAgentEvent)
          │   AgentEvent ──▶ 扩展事件（M7）
          │             ──▶ AgentSessionEvent ──▶ session.subscribe() 的订阅者（M9）
          │             ──▶ message_end: sessionManager.appendMessage()  → 会话条目（M4）
          │
          └─ 运行状态字段（§3.2）──▶ isStreaming / isCompacting / isRetrying / isBashRunning

 会话条目 ──buildSessionProjection()──▶ Agent 消息（含 4 种自定义）──convertToLlm()──▶ LLM 消息
```

这张图的关键是一个环：`Agent` 发出的消息在 `message_end` 时写进会话文件，下一次请求前 `prepareRequest` 又从会话文件读出来。`agent.state.messages` 只是这个环上的一份缓存。

---

## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。

```
createAgentSession()                               §4.1
├─ convertToLlm()（作为 agent.convertToLlm）        §4.2
├─ new Agent(...)                                  → M2
└─ new AgentSession(config)                        §4.3
    ├─ agent.subscribe(_handleAgentEvent)
    ├─ _installAgentToolHooks()                    §4.4
    ├─ _installAgentNextTurnRefresh()              §4.6
    ├─ _installAgentRequestProjection()            §4.5
    ├─ _installAgentBoundaryHooks()                §4.6
    ├─ _installHiddenDeclarationsProjection()      §4.7
    ├─ _installAgentForcedPromptProjection()       §4.7
    └─ _buildRuntime()                             §4.8
session.prompt(text)                               §4.9
└─ _startRun(trigger)                              §4.10
    ├─ _prepareRun(trigger)                        §4.11
    ├─ agent.prompt(messages)                      → M2
    │   └─ _handleAgentEvent(event)                §4.12
    ├─ while: _handlePostAgentRun()                §4.13
    │   ├─ _isRetryableError() / _prepareRetry()   §4.14
    │   └─ _checkCompaction()                      §4.15 → M5
    │   └─ agent.continue()                        → M2
    └─ finally: _emitAgentSettled()                §4.16
steer() / followUp() / clearQueue() / abort()      §4.17
createAgentSessionServices() / AgentSessionRuntime §4.18
```

### 4.1 createAgentSession()

**调用位置**：M9 的 `main()` → `createAgentSessionRuntime()` → 工厂函数 → `createAgentSessionFromServices()`（§4.18）→ `createAgentSession()`。SDK 用户直接调用它，例如 `packages/coding-agent/examples/sdk/01-minimal.ts`。

函数有 290 行，分三段读。第一段准备服务和已有会话：

源码：[`packages/coding-agent/src/core/sdk.ts:191-212`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L191-L212)

```ts
export async function createAgentSession(options: CreateAgentSessionOptions = {}): Promise<CreateAgentSessionResult> {
	const cwd = resolvePath(options.cwd ?? options.sessionManager?.getCwd() ?? process.cwd());
	const agentDir = options.agentDir ? resolvePath(options.agentDir) : getDefaultAgentDir();
	let resourceLoader = options.resourceLoader;

	const authPath = options.agentDir ? join(agentDir, "auth.json") : undefined;
	const modelsPath = options.agentDir ? join(agentDir, "models.json") : undefined;
	const modelRuntime = options.modelRuntime ?? (await ModelRuntime.create({ authPath, modelsPath }));

	const settingsManager = options.settingsManager ?? SettingsManager.create(cwd, agentDir);
	const sessionManager = options.sessionManager ?? SessionManager.create(cwd, getDefaultSessionDir(cwd, agentDir));

	if (!resourceLoader) {
		resourceLoader = new DefaultResourceLoader({ cwd, agentDir, settingsManager });
		await resourceLoader.reload();
		time("resourceLoader.reload");
	}

	// Check if session has existing data to restore
	const existingSession = sessionManager.buildSessionContext();
	const hasExistingSession = existingSession.messages.length > 0;
	const hasThinkingEntry = sessionManager.getBranch().some((entry) => entry.type === "thinking_level_change");
```

**讲解**

每个服务都可以由调用方传入，没传就用默认值创建。M9 传入了全部服务，所以 203 行的 `resourceLoader.reload()` 不会在这里执行。

`sessionManager.buildSessionContext()` 把会话文件里已有的条目投影成 Agent 消息（→ M4）。新会话的 `messages` 是空数组；`pi -c` 恢复会话时，这里就是之前的对话。

213–295 行解析模型和思考级别：调用方指定的优先，然后是会话里记录的，然后是设置里的默认值；最后用 `clampThinkingLevel` 裁到模型支持的范围。296–336 行定义 `convertToLlmWithBlockImages`（§4.2）。337–402 行准备 `streamFn` 用到的几个闭包。

第二段创建 `Agent`：

源码：[`packages/coding-agent/src/core/sdk.ts:403-440`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L403-L440)

```ts
	const agent = new Agent({
		initialState: {
			systemPrompt: "",
			model,
			thinkingLevel,
			tools: [],
			messages: existingSession.messages,
		},
		convertToLlm: convertToLlmWithBlockImages,
		streamFn: async (model, context, options) => {
			const requestOptions = buildRequestOptions(options);
			// Compaction and summaries use their own routing ids; only session requests
			// replace the cache entry, so warming restarts from them. Keep warming while
			// the current transcript still extends the request's prefix. Agent state may
			// shallow-copy the messages array or refresh the model object without changing
			// the provider request, so top-level object identity is not a valid cache key.
			if (options?.sessionId === sessionManager.getSessionId()) {
				cacheWarmer.start({ model, context, options: requestOptions }, cacheContextIsCurrent(model));
			}
			return modelRuntime.streamSimple(model, context, requestOptions);
		},
		onPayload: transformProviderPayload,
		onResponse: handleProviderResponse,
		onProviderStreamEvent: handleProviderStreamEvent,
		sessionId: sessionManager.getSessionId(),
		transformContext: async (messages) => {
			const runner = extensionRunnerRef.current;
			if (!runner) return messages;
			return runner.emitContext(messages);
		},
		steeringMode: settingsManager.getSteeringMode(),
		followUpMode: settingsManager.getFollowUpMode(),
		transport: settingsManager.getTransport(),
		thinkingBudgets: settingsManager.getThinkingBudgets(),
		maxRetryDelayMs: settingsManager.getProviderRetrySettings().maxRetryDelayMs,
	});
	// This stream resolves auth through modelRuntime, so summaries must not pre-resolve it (#11520).
	markRuntimeStreamFunction(agent.streamFunction, modelRuntime);
```

**讲解**

这是 M2 的 `Agent` 构造函数。几个值得注意的参数：

- `systemPrompt: ""`、`tools: []`。`Agent` 的这两个字段在 coding-agent 里不用来存真正的值。工具在 `AgentSession` 构造时由 `_buildRuntime()` 设置（§4.8）；系统提示词以 `system` 消息的形式进入 transcript（§4.11）。§2.2 的输出 `state.systemPrompt=""` 就是这个空串。
- `messages: existingSession.messages`。恢复会话时，`Agent` 一开始就带着之前的消息。
- `streamFn` 包了一层 `modelRuntime.streamSimple()`（→ M1）。包装做了两件事：用设置补全超时、重试次数等请求参数；对属于本会话的请求启动缓存预热。
- `transformContext` 通过 `extensionRunnerRef.current` 调用扩展的 `context` 事件（→ M7）。`AgentSession` 构造时会再给它包两层（§4.7）。
- `onPayload`、`onResponse`、`onProviderStreamEvent` 分别对应扩展的 `before_provider_request`、`after_provider_response`、`provider_stream_event`。

`markRuntimeStreamFunction` 在一个 `WeakMap` 里记下"这个 `streamFn` 自己通过 `modelRuntime` 解析认证"。压缩生成摘要时会查这个表，避免把预先解析的凭据传进去（`packages/coding-agent/src/core/agent-session.ts:606`）。

第三段写初始条目、创建 `AgentSession`：

源码：[`packages/coding-agent/src/core/sdk.ts:442-481`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L442-L481)

```ts
	// Restore missing settings metadata for older sessions.
	if (hasExistingSession) {
		if (!hasThinkingEntry) {
			sessionManager.appendThinkingLevelChange(thinkingLevel);
		}
	} else {
		// Save initial model and thinking level for new sessions so they can be restored on resume
		if (model) {
			sessionManager.appendModelChange(model.provider, model.id);
		}
		sessionManager.appendThinkingLevelChange(thinkingLevel);
	}

	const session = new AgentSession({
		agent,
		sessionManager,
		settingsManager,
		cwd,
		scopedModels: options.scopedModels,
		resourceLoader,
		customTools: options.customTools,
		modelRuntime,
		cacheWarmer,
		initialActiveToolNames,
		usesDefaultTools: (options.tools === undefined || toolModifiers !== undefined) && !options.noTools,
		defaultToolModifiers: toolModifiers,
		allowedToolNames,
		excludedToolNames,
		extensionRunnerRef,
		sessionStartEvent: options.sessionStartEvent,
	});

	const extensionsResult = resourceLoader.getExtensions();

	return {
		session,
		extensionsResult,
		modelFallbackMessage,
	};
}
```

**讲解**

新会话先写两个条目：`model_change` 和 `thinking_level_change`。§2.2 的输出里，`prompt()` 之前内存中已经有这两个条目，就是这里写的。它们在会话文件里排在 `session` 头之后（README §5.3）。

`usesDefaultTools` 在没有显式工具列表时为 `true`。这时 `AgentSession` 会在构造末尾按 transcript 里记录的工具清单恢复工具（§4.3）。

**往哪走**：`convertToLlmWithBlockImages` 调用的 `convertToLlm` 在 §4.2；`new AgentSession` 在 §4.3。

### 4.2 convertToLlm()

**调用位置**：M2 的 `streamAssistantResponse()` 在每次请求前调用 `config.convertToLlm(messages)`。在 coding-agent 里这个 hook 是 `sdk.ts` 的 `convertToLlmWithBlockImages`（`packages/coding-agent/src/core/sdk.ts:297`），它先调用 `convertToLlm`，再在 `blockImages` 设置打开时把图片替换成文字 `Image reading is disabled.`。

源码：[`packages/coding-agent/src/core/messages.ts:148-196`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/messages.ts#L148-L196)

```ts
export function convertToLlm(messages: AgentMessage[]): Message[] {
	return messages
		.map((m): Message | undefined => {
			switch (m.role) {
				case "bashExecution":
					// Skip messages excluded from context (!! prefix)
					if (m.excludeFromContext) {
						return undefined;
					}
					return {
						role: "user",
						content: [{ type: "text", text: bashExecutionToText(m) }],
						timestamp: m.timestamp,
					};
				case "custom": {
					const content = typeof m.content === "string" ? [{ type: "text" as const, text: m.content }] : m.content;
					return {
						role: "user",
						content,
						timestamp: m.timestamp,
					};
				}
				case "branchSummary":
					return {
						role: "user",
						content: [{ type: "text" as const, text: BRANCH_SUMMARY_PREFIX + m.summary + BRANCH_SUMMARY_SUFFIX }],
						timestamp: m.timestamp,
					};
				case "compactionSummary":
					return {
						role: "user",
						content: [
							{ type: "text" as const, text: COMPACTION_SUMMARY_PREFIX + m.summary + COMPACTION_SUMMARY_SUFFIX },
						],
						timestamp: m.timestamp,
					};
				case "system":
				case "user":
				case "assistant":
				case "toolResult":
					return m;
				default:
					// biome-ignore lint/correctness/noSwitchDeclarations: fine
					const _exhaustiveCheck: never = m;
					return undefined;
			}
		})
		.filter((m) => m !== undefined);
}
```

**讲解**

这是 README §3.1 三层消息里"Agent 消息 → LLM 消息"那一步。输入是 Agent 消息数组，输出是 LLM 消息数组：

- 4 种标准 role 原样返回。
- `bashExecution` 用 `bashExecutionToText`（`packages/coding-agent/src/core/messages.ts:82`）转成一段 `Ran \`cmd\`` 开头的文本，作为 user 消息。
- `custom` 的内容直接作为 user 消息。
- `branchSummary`、`compactionSummary` 用前缀和后缀包成 `<summary>…</summary>`，作为 user 消息。前缀是 `The conversation history before this point was compacted into the following summary:`（`packages/coding-agent/src/core/messages.ts:11`）。

`default` 分支里的 `const _exhaustiveCheck: never = m` 是编译期检查：以后有人往 `CustomAgentMessages` 里加了新 role 却忘了在这里处理，类型检查会报错。

注释第一条写的是 "Agent's transormToLlm option"，这是笔误，指的就是 `convertToLlm` 选项。

除了 `Agent`，压缩生成摘要（M5）和 `_buildBoundaryContext`（`packages/coding-agent/src/core/agent-session.ts:1015`）也直接调用这个函数。

**往哪走**：§4.3。

### 4.3 AgentSession 构造函数

**调用位置**：`createAgentSession()` 的 455 行。

源码：[`packages/coding-agent/src/core/agent-session.ts:498-539`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L498-L539)

```ts
	constructor(config: AgentSessionConfig) {
		this.agent = config.agent;
		this.sessionManager = config.sessionManager;
		this.settingsManager = config.settingsManager;
		this._scopedModels = config.scopedModels ?? [];
		this._resourceLoader = config.resourceLoader;
		this._customTools = config.customTools ?? [];
		this._cwd = config.cwd;
		this._modelRuntime = config.modelRuntime;
		this._cacheWarmer = config.cacheWarmer;
		if (this._cacheWarmer) {
			this._cacheWarmer.onWarmed = (entry) => this._emit({ type: "entry_appended", entry });
		}
		this._extensionRunnerRef = config.extensionRunnerRef;
		this._initialActiveToolNames = config.initialActiveToolNames;
		this._usesDefaultTools = config.usesDefaultTools ?? false;
		this._defaultToolModifiers = config.defaultToolModifiers ?? [];
		if (config.allowedToolNames) {
			this._allowedTools = createToolNameMatcher(config.allowedToolNames);
			this._allowlistFiltersMcp =
				config.allowedToolNames.length === 0 || config.allowedToolNames.some((entry) => entry.startsWith("mcp__"));
		}
		this._excludedTools = config.excludedToolNames ? createToolNameMatcher(config.excludedToolNames) : undefined;
		this._baseToolsOverride = config.baseToolsOverride;
		this._sessionStartEvent = config.sessionStartEvent ?? { type: "session_start", reason: "startup" };

		// Always subscribe to agent events for internal handling
		// (session persistence, extensions, auto-compaction, retry logic)
		this._unsubscribeAgent = this.agent.subscribe(this._handleAgentEvent);
		this._installAgentToolHooks();
		this._installAgentNextTurnRefresh();
		this._installAgentRequestProjection();
		this._installAgentBoundaryHooks();
		this._installHiddenDeclarationsProjection();
		this._installAgentForcedPromptProjection();

		this._buildRuntime({
			activeToolNames: this._initialActiveToolNames,
			includeAllExtensionTools: true,
		});
		if (this._usesDefaultTools) this._restoreToolsFromTranscript(this._defaultToolModifiers);
	}
```

**讲解**

前半段把 `config` 的字段存到私有字段上。`allowedToolNames` 和 `excludedToolNames` 被编译成匹配函数（支持 `*` 通配）。

526 行 `this.agent.subscribe(this._handleAgentEvent)` 是 `AgentSession` 接收 AgentEvent 的唯一入口（§4.12）。它是 `Agent` 的第一个订阅者，M2 §4.17 讲过 `Agent.processEvents` 按顺序 await 每个订阅者，所以 `_handleAgentEvent` 做完之前，循环不会继续。

527–532 行安装 6 组 hook。安装顺序有意义：每个 `_install*` 都先保存字段的旧值，新函数里再调用旧值。`transformContext` 先被 §4.7 的第一个函数包一层，再被第二个函数包一层，调用时从外往里执行。

534 行 `_buildRuntime()` 创建工具和扩展运行器（§4.8）。

538 行 `_restoreToolsFromTranscript()`（`packages/coding-agent/src/core/agent-session.ts:1851`）处理恢复的会话：如果会话文件的 `system` 消息里记录了工具清单（`toolsAdded`/`toolsRemoved`），就按记录恢复，而不是用默认工具。新会话没有 `system` 消息，这一步什么也不做。

**往哪走**：按 hook 的作用分组读。工具 hook 在 §4.4，请求上下文在 §4.5，轮间和轮末 hook 在 §4.6，`transformContext` 的两层在 §4.7。

### 4.4 _installAgentToolHooks()

**调用位置**：构造函数 527 行。

源码：[`packages/coding-agent/src/core/agent-session.ts:668-697`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L668-L697)

```ts
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

`agent.beforeToolCall` 和 `agent.afterToolCall` 是 M2 §4.11、§4.13 里工具执行流水线的两个 hook。这里把它们接到扩展的 `tool_call` 和 `tool_result` 事件上（→ M7）。

两点设计：

- hook 在调用时才读 `this._extensionRunner`。扩展重载（`reload()`）会换一个新的运行器，hook 不用重装。
- 没有扩展监听 `tool_call` 时直接返回 `undefined`，不创建事件对象。

`_afterToolCall`（`packages/coding-agent/src/core/agent-session.ts:699`）在扩展处理之后还做一件事：按模型的图片限制缩放工具结果里的图片。扩展没有改结果、图片也不用缩放时返回 `undefined`，M2 就保留原结果。

两个函数都有一个可选参数 `parentToolCallId`。工具通过 `ctx.executeTool()` 调用别的工具时（`_executeNestedToolCall`，`packages/coding-agent/src/core/agent-session.ts:746`），嵌套调用也走这两个 hook，扩展能从事件里看出是谁发起的（→ M3）。

**往哪走**：§4.5。

### 4.5 _installAgentRequestProjection()：会话文件是上下文的唯一来源

**调用位置**：构造函数 529 行。安装的 `agent.prepareRequest` 在 M2 `runLoop` 每次请求模型之前被调用（`packages/agent/src/agent-loop.ts:228`）。

源码：[`packages/coding-agent/src/core/agent-session.ts:803-830`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L803-L830)

```ts
	private _installAgentRequestProjection(): void {
		const previousPrepareRequest = this.agent.prepareRequest;
		this.agent.prepareRequest = async (request, signal) => {
			const failed = this._failedResponse;
			this._failedResponse = undefined;
			const prepare = async () => {
				const projection = this.sessionManager.buildSessionProjection();
				const canonicalContext = {
					...request.context,
					messages: projection.messages,
					// Messages declare the provider-visible loadout; context.tools keeps executable implementations.
					tools: this.agent.state.tools.slice(),
				};
				const previous = await previousPrepareRequest?.(
					{
						...request,
						context: canonicalContext,
						model: this.agent.state.model,
						thinkingLevel: this.agent.state.thinkingLevel,
					},
					signal,
				);
				return { previous, context: previous?.context ?? canonicalContext, projection };
			};
			let { previous, context, projection } = await prepare();
			const model = previous?.model ?? this.agent.state.model;
			const thinkingLevel = previous?.thinkingLevel ?? this.agent.state.thinkingLevel;
			if (!isVirtualModel(model)) return { ...previous, context, model, thinkingLevel };
```

**讲解**

M2 §4.5 讲过，`prepareRequest` 可以返回一个新的 `context`，循环就用新 context 请求模型，并且把它记为循环自己的 transcript。

这里的 `prepare()` 做的事是：**丢掉循环传进来的 `request.context.messages`，换成 `sessionManager.buildSessionProjection().messages`**。也就是说，模型每次看到的上下文都从会话文件重新投影出来，而不是循环内存里累积的数组（→ M4 讲 `buildSessionProjection`）。

这样做之所以成立，是因为 M2 的事件回调被 await（M2 §6.3）：循环发出 `message_end` 后，`_handleAgentEvent` 把消息写进会话文件（§4.12），然后循环才继续，才走到下一次 `prepareRequest`。所以请求前的那一刻，会话文件里一定已经有循环产生的所有消息。

`tools` 取 `agent.state.tools`，这是可执行的工具实现。模型看到的工具声明在 transcript 的 `system` 消息里（M2 §6.2），两者分开。

`previousPrepareRequest` 是安装前 `agent.prepareRequest` 的旧值。`sdk.ts` 没有设置它，所以这里是 `undefined`。

830 行：模型不是虚拟模型时直接返回。831–859 行处理虚拟模型（一个按规则把请求路由到不同物理模型的"模型"），路由前要再检查一次压缩阈值，因为路由后的模型上下文窗口可能更小。本教程不展开虚拟模型。

**实验验证**。`m8-session.ts projection` 在 `prompt()` 之前往 `agent.state.messages` 里直接塞一条 user 消息，这条消息不在会话文件里。再包一层 `prepareRequest`，打印它的输入和输出：

```bash
WORK=/tmp/pi-lab/m8-work-proj PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-proj node --experimental-strip-types m8-session.ts projection
```

```
  35ms  [projection] prompt 前 agent.state.messages=user
场景：projection
  38ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  38ms  turn_start
  38ms  message_start (system)
  38ms  message_end (system)  [此刻会话条目数=2]
  38ms  message_start (user)
  38ms  message_end (user)  [此刻会话条目数=3]
  38ms  [prepareRequest] 循环传入的 context.messages=user, system, user
  39ms  [prepareRequest] 返回的 context.messages=system, user
  40ms  [模型] 第 1 次请求，收到 2 条 LLM 消息：system, user
  41ms  message_start (assistant)
…省略…
```

循环传进来的是 `user, system, user`：第一条是塞进去的消息，后两条是本次 prompt 的。`prepareRequest` 返回 `system, user`：塞进去的消息不见了，模型也只收到 2 条。运行结束后：

```
…省略…
[AgentState] messages=user, system, user, assistant, toolResult, assistant
…省略…
```

`agent.state.messages` 里那条消息还在（M2 的 `Agent` 在 `message_end` 时往自己的数组里 push，不受 `prepareRequest` 影响），但它从没发给模型。

**往哪走**：§4.6。

### 4.6 _installAgentNextTurnRefresh() 和 _installAgentBoundaryHooks()

**调用位置**：构造函数 528 行、530 行。`prepareNextTurnWithContext` 在 M2 `runLoop` 第 2 轮起、每轮开始前调用（`packages/agent/src/agent-loop.ts:194`）；`finishTurn` 在每轮 `turn_end` 之前调用（`packages/agent/src/agent-loop.ts:295`）。

源码：[`packages/coding-agent/src/core/agent-session.ts:914-948`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L914-L948)

```ts
	private _installAgentNextTurnRefresh(): void {
		const previousPrepareNextTurnWithContext =
			this.agent.prepareNextTurnWithContext ??
			(this.agent.prepareNextTurn
				? async (_turn: PrepareNextTurnContext, signal?: AbortSignal) => await this.agent.prepareNextTurn?.(signal)
				: undefined);
		this.agent.prepareNextTurnWithContext = async (turn, signal) => {
			const context = await this._compactBeforeNextAssistantResponse(turn.context);
			const previousSnapshot = await previousPrepareNextTurnWithContext?.({ ...turn, context }, signal);
			const nextContext = previousSnapshot?.context ?? context;
			const runOptions = this._runSystemPromptOptions ?? this._baseSystemPromptOptions;
			const options = normalizeBuildSystemPromptOptions({
				...runOptions,
				selectedTools: this.getActiveToolNames(),
				toolSnippets: { ...this._baseSystemPromptOptions.toolSnippets, ...runOptions.toolSnippets },
				toolGuidelines: { ...this._baseSystemPromptOptions.toolGuidelines, ...runOptions.toolGuidelines },
			});
			const updateMessage = this._preparePromptAndToolLoadout(options, nextContext.messages);
			// Keep session.systemPrompt and ctx.getSystemPrompt() in step with what the provider sees.
			this._runSystemPromptOptions = options;

			return {
				...previousSnapshot,
				context: {
					...nextContext,
					tools: this.agent.state.tools.slice(),
				},
				messages: updateMessage
					? [...(previousSnapshot?.messages ?? []), updateMessage]
					: previousSnapshot?.messages,
				model: this.agent.state.model,
				thinkingLevel: this.agent.state.thinkingLevel,
			};
		};
	}
```

**讲解**

一轮结束、下一轮开始之前，这个 hook 做三件事：

1. `_compactBeforeNextAssistantResponse()`：检查压缩阈值，超过就压缩（§4.15 的同一套逻辑）。返回的 context 同样是从会话文件投影的。
2. 用当前激活的工具重新算系统提示词。`_preparePromptAndToolLoadout()`（→ M6）比较 transcript 里当前的 `system` 消息和新算出的各节内容，有差异就返回一条只含差异的 `system` 补丁消息，否则返回 `undefined`。
3. 把补丁放进返回值的 `messages`。M2 的循环会在下一次请求前发出这些消息（`packages/agent/src/agent-loop.ts:220`），它们经过 `message_end` 进会话文件。

第 2 步的意义：上一轮里扩展可能调用了 `setActiveTools()`，或者工具 `prepareLoadout` 改了描述。新的系统提示词要在下一次请求之前进入 transcript。

`_compactBeforeNextAssistantResponse` 的源码：

源码：[`packages/coding-agent/src/core/agent-session.ts:792-801`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L792-L801)

```ts
	private async _compactBeforeNextAssistantResponse(context: AgentContext): Promise<AgentContext> {
		const projection = this.sessionManager.buildSessionProjection();
		// A virtual selection is checked in prepareRequest, against the model the request is routed to.
		const model = this.model;
		if (!model || isVirtualModel(model) || !this._exceedsCompactionThreshold(model, projection)) {
			return { ...context, messages: projection.messages };
		}
		await this._runAutoCompaction("threshold", false);
		return { ...context, messages: this.sessionManager.buildSessionProjection().messages };
	}
```

`finishTurn` 的安装在 M2 §6.1 已经全文引用过（`packages/coding-agent/src/core/agent-session.ts:902-912`）。它把 `turn_end` 边界交给扩展：`_dispatchTurnEndBoundary()`（`packages/coding-agent/src/core/agent-session.ts:862`）发出扩展的 `turn_end` 事件，扩展可以在这里追加会话条目（自定义条目、上下文编辑、压缩），也可以要求循环继续（返回 `{ action: "continue" }`）。

`finishTurn` 记下 `turn.message`（`_boundaryDispatchedMessages`）。之后 AgentEvent `turn_end` 到达 `_emitExtensionEvent` 时，看到这条消息已经派发过，就不再派发一次（`packages/coding-agent/src/core/agent-session.ts:1334`）。只有没走 `finishTurn` 的路径（例如出错退出）才由事件补发。

**往哪走**：§4.7。

### 4.7 _installHiddenDeclarationsProjection() 和 _installAgentForcedPromptProjection()

**调用位置**：构造函数 531、532 行。两者都包装 `agent.transformContext`，M2 在每次请求前、`convertToLlm` 之前调用它。

源码：[`packages/coding-agent/src/core/agent-session.ts:1808-1843`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1808-L1843)

```ts
	private _installHiddenDeclarationsProjection(): void {
		const previousTransformContext = this.agent.transformContext;
		this.agent.transformContext = async (messages, signal) => {
			const transformed = previousTransformContext ? await previousTransformContext(messages, signal) : messages;
			const hidden = this._hiddenDeclarations;
			if (hidden.size === 0) return transformed;
			return transformed.map((message) => {
				if (message.role !== "system" || (!message.toolsAdded && !message.toolsRemoved)) return message;
				const { toolsAdded, toolsRemoved, ...rest } = message;
				const added = toolsAdded?.filter((tool) => !hidden.has(tool.name)) ?? [];
				const removed = toolsRemoved?.filter((tool) => !hidden.has(tool.name)) ?? [];
				return {
					...rest,
					...(added.length > 0 ? { toolsAdded: added } : {}),
					...(removed.length > 0 ? { toolsRemoved: removed } : {}),
				};
			});
		};
	}

	private _installAgentForcedPromptProjection(): void {
		const previousTransformContext = this.agent.transformContext;
		this.agent.transformContext = async (messages, signal) => {
			const transformed = previousTransformContext ? await previousTransformContext(messages, signal) : messages;
			const forced = this._runSystemPromptOptions?.forceSystemPrompt;
			if (forced === undefined) return transformed;
			const current = getCurrentSystemMessage(transformed);
			const head: SystemMessage = {
				role: "system",
				content: forced,
				...(current?.toolsAdded ? { toolsAdded: current.toolsAdded } : {}),
				timestamp: current?.timestamp ?? Date.now(),
			};
			return [head, ...transformed.filter((message) => message.role !== "system")];
		};
	}
```

**讲解**

调用顺序是：外层（forced prompt）→ 中层（hidden declarations）→ 内层（`sdk.ts` 的扩展 `context` 事件）。每层先 await 里面那层，再处理结果。所以实际的处理顺序是先扩展、再去掉隐藏声明、最后替换系统提示词。

- **隐藏声明。** 工具定义的 `prepareLoadout` hook 可以把一些工具从发给模型的声明里隐藏起来（例如 MCP 的 tool-search 模式，→ M10）。这一层从每条 `system` 消息的 `toolsAdded`/`toolsRemoved` 里删掉这些工具。会话文件里的记录不变，只改请求。
- **强制系统提示词。** 扩展在 `before_agent_start` 里返回 `systemPrompt` 时，要求请求开头是这段原文。这一层把所有 `system` 消息合并成开头一条，内容是强制文本，工具是当前工具。同样只改请求，不写进 transcript。

两者都是 `transformContext` 而不是 `prepareRequest`，因为它们只影响"这一次发给模型的样子"，不该被循环记为 transcript（M2 §4.6：`transformContext` 的结果只用于本次请求）。

**往哪走**：构造函数的最后一步，§4.8。

### 4.8 _buildRuntime()

**调用位置**：构造函数 534 行；`reload()` 也会调用它（`packages/coding-agent/src/core/agent-session.ts:3741`）。

源码：[`packages/coding-agent/src/core/agent-session.ts:3687-3739`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3687-L3739)

```ts
	private _buildRuntime(options: {
		activeToolNames?: string[];
		flagValues?: Map<string, boolean | string>;
		includeAllExtensionTools?: boolean;
	}): void {
		const autoResizeImages = this.settingsManager.getImageAutoResize();
		const shellCommandPrefix = this.settingsManager.getShellCommandPrefix();
		const shellPath = this.settingsManager.getShellPath();
		const baseToolDefinitions = this._baseToolsOverride
			? Object.fromEntries(
					Object.entries(this._baseToolsOverride).map(([name, tool]) => [
						name,
						createToolDefinitionFromAgentTool(tool),
					]),
				)
			: createAllToolDefinitions(this._cwd, {
					read: { autoResizeImages },
					bash: { commandPrefix: shellCommandPrefix, shellPath },
				});

		this._baseToolDefinitions = new Map(
			Object.entries(baseToolDefinitions).map(([name, tool]) => [name, tool as ToolDefinition]),
		);

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

		const defaultActiveToolNames = this._baseToolsOverride
			? Object.keys(this._baseToolsOverride)
			: ["read", "bash", "edit", "write"];
		const baseActiveToolNames = options.activeToolNames ?? defaultActiveToolNames;
		this._refreshToolRegistry({
			activeToolNames: baseActiveToolNames,
			includeAllExtensionTools: options.includeAllExtensionTools,
		});
	}
```

**讲解**

三步：

1. **内置工具定义。** `createAllToolDefinitions(cwd, …)` 创建 read、bash、edit、write 等工具的 `ToolDefinition`（→ M3）。
2. **扩展运行器。** `new ExtensionRunner(...)` 用资源加载器已经加载的扩展创建运行器，并写进 `extensionRunnerRef.current`，`sdk.ts` 里的闭包从此能用它（→ M7）。`_bindExtensionCore()`（`packages/coding-agent/src/core/agent-session.ts:3441`）把 `sendMessage`、`setActiveTools`、`compact`、`abort` 等会话操作交给扩展 API。
3. **工具注册表。** `_refreshToolRegistry()`（`packages/coding-agent/src/core/agent-session.ts:3576`）把内置工具和扩展工具都用 `wrapRegisteredTools` 包成 `AgentTool`，放进 `_toolRegistry`，再按激活名单调用 `_setActiveTools()`。`_setActiveTools` 经 `_applyToolLoadout()`（`packages/coding-agent/src/core/agent-session.ts:1614`）设置 `agent.state.tools`，并经 `_rebuildSystemPrompt()`（`packages/coding-agent/src/core/agent-session.ts:1741`）重算 `_baseSystemPromptOptions`（→ M6）。

`_buildRuntime()` 结束时，`agent.state.tools` 已经是 4 个工具，`_baseSystemPromptOptions` 已经算好，但 transcript 里还没有 `system` 消息。它要等第一次 `prompt()` 才写入（§4.11）。

注意这里不发 `session_start` 事件。扩展的 `session_start` 在 `bindExtensions()`（`packages/coding-agent/src/core/agent-session.ts:3339`）里发，由 M9 的各个模式在设置好界面之后调用。

**往哪走**：会话创建完了。下面从 `session.prompt()` 读一次运行，§4.9。

### 4.9 prompt()

**调用位置**：print 模式的 `runPrintMode()` 调用 `session.prompt(initialMessage, { images })`（`packages/coding-agent/src/modes/print-mode.ts:132`）。

前半段处理输入：

源码：[`packages/coding-agent/src/core/agent-session.ts:2113-2156`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L2113-L2156)

```ts
	async prompt(text: string, options?: PromptOptions): Promise<void> {
		if (this._isEmittingAgentSettled) {
			this._deferredSettledActions.push(async () => await this.prompt(text, options));
			return;
		}
		const expandPromptTemplates = options?.expandPromptTemplates ?? true;
		const preflightResult = options?.preflightResult;
		// Handle extension commands first (execute immediately, even during streaming)
		// Extension commands manage their own LLM interaction via pi.sendMessage()
		if (expandPromptTemplates && text.startsWith("/")) {
			const handled = await this._tryExecuteExtensionCommand(text);
			if (handled) {
				// Extension command executed, no prompt to send
				preflightResult?.("handled");
				return;
			}
		}

		if (this._compactionAbortController !== undefined) {
			throw new Error(
				"Cannot submit a prompt while compaction is in progress. Wait for compaction to finish and retry.",
			);
		}

		// Emit input event for extension interception (before skill/template expansion)
		const processedInput = await this._runInputHandlers(
			text,
			options?.images,
			options?.source ?? "interactive",
			this.isStreaming ? options?.streamingBehavior : undefined,
		);
		if (!processedInput) {
			preflightResult?.("handled");
			return;
		}
		const { text: currentText, images: currentImages } = processedInput;

		// Expand skill commands (/skill:name args) and prompt templates (/template args)
		let expandedText = currentText;
		if (expandPromptTemplates) {
			expandedText = this._expandSkillCommand(expandedText);
			expandedText = expandPromptTemplate(expandedText, [...this.promptTemplates]);
		}

```

**讲解**

`prompt()` 的前半段是输入预处理，每一步都可能让它提前返回：

1. 正在发 `agent_settled`（§4.16）时，把这次调用推迟到发完之后。这样 `agent_settled` 的监听器里再调用 `prompt()` 也是安全的。
2. 以 `/` 开头并且匹配扩展注册的命令：执行命令，返回（→ M7）。
3. 手动压缩正在进行：抛错。
4. 扩展的 `input` 事件可以拦截（`handled`）或改写（`transform`）输入（→ M7）。
5. 展开 `/skill:name` 和提示词模板（→ M6）。

后半段决定"排队"还是"开始一次运行"：

源码：[`packages/coding-agent/src/core/agent-session.ts:2157-2196`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L2157-L2196)

```ts
		// If streaming, queue via steer() or followUp() based on option
		if (this.isStreaming) {
			if (!options?.streamingBehavior) {
				throw new Error(
					"Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the message.",
				);
			}
			if (options.streamingBehavior === "followUp") {
				await this._queueFollowUp(expandedText, currentImages);
			} else {
				await this._queueSteer(expandedText, currentImages);
			}
			preflightResult?.("queued");
			return;
		}

		// Validate model
		if (!this.model) {
			throw new Error(formatNoModelSelectedMessage());
		}

		const hasConfiguredAuth =
			this._modelRuntime.hasConfiguredAuth(this.model.provider) ||
			(await this._modelRuntime.checkAuth(this.model.provider)) !== undefined;
		if (!hasConfiguredAuth) {
			const isOAuth = this._modelRuntime.isUsingOAuth(this.model.provider);
			if (isOAuth) {
				throw new Error(
					`Authentication failed for "${this.model.provider}". ` +
						`Credentials may have expired or network is unavailable. ` +
						`Run '/login ${this.model.provider}' to re-authenticate.`,
				);
			}
			throw new Error(formatNoApiKeyFoundMessage(this.model.provider));
		}

		await this._startRun({ messageType: "user", prompt: expandedText, images: currentImages }, () =>
			preflightResult?.("started"),
		);
	}
```

**讲解**

`isStreaming` 为 `true` 时不能开始新运行，只能排队，并且调用方必须说明是 `steer` 还是 `followUp`（§4.17）。M2 的 `Agent.prompt()` 在运行中被调用会直接抛错；`AgentSession.prompt()` 把这种情况变成排队。

否则检查模型和认证，然后调用 `_startRun()`。`preflightResult` 是给 rpc 模式的回调，告诉它这次输入是被处理了、排队了还是开始运行了（→ M9）。

**往哪走**：§4.10。

### 4.10 _startRun()

**调用位置**：`prompt()` 的 2193 行；扩展用 `sendCustomMessage(..., { triggerTurn: true })` 触发新轮次时也会调用（`packages/coding-agent/src/core/agent-session.ts:2378`）。

源码：[`packages/coding-agent/src/core/agent-session.ts:1894-1939`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1894-L1939)

```ts
	private async _startRun(trigger: BeforeAgentStartTrigger, onStarted?: () => void): Promise<void> {
		this._isAgentRunActive = true;
		this._agentRunAbortRequested = false;
		let messages: AgentMessage[];
		try {
			messages = await this._prepareRun(trigger);
		} catch (error) {
			this._isAgentRunActive = false;
			this._flushPendingBashMessages();
			this._flushPendingCustomMessages();
			this._resolveIdleWaitIfIdle();
			throw error;
		}
		// Messages held while the run was starting go before its starting messages, as they would
		// have while idle. No run exists yet, so they cannot split a tool call from its result.
		this._flushPendingBashMessages();
		this._flushPendingCustomMessages();
		onStarted?.();

		// Compaction before the prompt may have scheduled a retry; the new prompt replaces it.
		this._failedResponse = undefined;
		this._recordSelection();
		// The run records the loadout in the transcript; restored tools that did not register by now
		// are dropped, so a tool that never registers does not stay pending.
		this._pendingToolNames.clear();
		try {
			await this.agent.prompt(messages);
			while (!this._agentRunAbortRequested) {
				if (await this._handlePostAgentRun()) {
					if (this._agentRunAbortRequested) break;
					await this.agent.continue();
					continue;
				}
				if (this._agentRunAbortRequested || !(await this._runBeforeSettleBoundary())) break;
				if (this._agentRunAbortRequested) break;
				await this.agent.continue();
			}
		} finally {
			if (this._agentRunAbortRequested) this._finishCancelledRetry();
			this._failedResponse = undefined;
			this._runSystemPromptOptions = undefined;
			this._flushPendingBashMessages();
			this._flushPendingCustomMessages();
			await this._emitAgentSettled();
		}
	}
```

**讲解**

这是一次 `session.prompt()` 的骨架，分四步：

1. **1895 行把 `_isAgentRunActive` 设为 `true`。** 从这一刻起 `isStreaming` 为 `true`。`_prepareRun()` 里的 `before_agent_start` 扩展可能要花时间，这期间再来的输入会排队进这次运行，而不会开始另一次运行。
2. **`_prepareRun()` 准备起始消息**（§4.11）。失败就把标志复位并抛出。
3. **`agent.prompt(messages)` 跑 M2 的循环。** 循环里每个事件都经过 `_handleAgentEvent`（§4.12）。
4. **后处理循环（1921–1930 行）。** `agent.prompt()` 返回后，`_handlePostAgentRun()` 判断要不要再跑一次：重试、溢出后的压缩再试、或者有排队消息。要就调用 `agent.continue()`（M2 §4.1 旁的 `continue()`，从当前 transcript 继续，不加新的 prompt 消息），然后再判断一次。不要了就进入 `_runBeforeSettleBoundary()`：扩展的 `agent_before_settle` 事件可以追加条目、要求再继续一次。没有扩展监听时它只看有没有排队消息。

`finally` 里一定会调用 `_emitAgentSettled()`（§4.16）。不论运行正常结束、出错还是被中止，订阅者最后都会收到一个 `agent_settled`。

这里能看出 M2 的 `agent_end` 和 M8 的 `agent_settled` 的区别：一次 `session.prompt()` 可能包含多次 `agent.prompt()`/`agent.continue()`，每次都有自己的 `agent_start`…`agent_end`；但 `agent_settled` 只有一个。§7.2 的重试实验里能看到两对 `agent_start`/`agent_end` 和一个 `agent_settled`。

`_flushPendingBashMessages()` 和 `_flushPendingCustomMessages()` 在开始前和结束后各调用一次。运行中执行的 `!cmd` 和不触发轮次的扩展消息不能直接写入，否则可能插在 assistant 的工具调用和它的工具结果之间，有的 provider 会拒绝这样的 transcript。

**往哪走**：§4.11。

### 4.11 _prepareRun()

**调用位置**：`_startRun()` 1899 行。

源码：[`packages/coding-agent/src/core/agent-session.ts:1947-1998`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1947-L1998)

```ts
	private async _prepareRun(trigger: BeforeAgentStartTrigger): Promise<AgentMessage[]> {
		// Check if we need to compact before sending (catches aborted responses).
		// The starting message is sent below, so do not call agent.continue() here.
		const lastAssistant = this._findLastAssistantMessage();
		if (lastAssistant) {
			await this._checkCompaction(lastAssistant, false);
		}

		const selectedToolsBefore = this._baseSystemPromptOptions.selectedTools;
		const result = await this._extensionRunner.emitBeforeAgentStart(trigger, this._baseSystemPromptOptions);
		// Handlers may edit event.systemPromptOptions.selectedTools or call setActiveTools(),
		// which updates the live loadout instead. An explicit edit wins; otherwise the live
		// loadout is authoritative, so a setActiveTools() call is not undone here.
		const handlerEditedTools =
			result.systemPromptOptions.selectedTools.length !== selectedToolsBefore.length ||
			result.systemPromptOptions.selectedTools.some((name, index) => name !== selectedToolsBefore[index]);
		if (!handlerEditedTools) result.systemPromptOptions.selectedTools = this.getActiveToolNames();

		const messages: AgentMessage[] = [];
		if (trigger.messageType === "custom") {
			messages.push(trigger.message);
		} else {
			// Normalize images after before_agent_start so extension-driven model selection
			// determines the resize profile used for the request and history.
			const normalized = await this._normalizePromptImages(trigger.images);
			const text =
				normalized.hints.length > 0 ? `${trigger.prompt}\n\n${normalized.hints.join("\n")}` : trigger.prompt;
			messages.push({
				role: "user",
				content: [{ type: "text", text }, ...normalized.images],
				timestamp: Date.now(),
			});
		}
		// Inject any pending "nextTurn" messages as context alongside the starting message
		messages.push(...this._pendingNextTurnMessages);
		this._pendingNextTurnMessages = [];
		for (const msg of result.messages) {
			messages.push({
				role: "custom",
				customType: msg.customType,
				// Untyped extensions can pass null/missing content; normalize at ingestion.
				content: msg.content ?? [],
				display: msg.display,
				details: msg.details,
				timestamp: Date.now(),
			});
		}
		const updateMessage = this._preparePromptAndToolLoadout(result.systemPromptOptions);
		this._runSystemPromptOptions = result.systemPromptOptions;
		if (updateMessage) messages.unshift(updateMessage);
		return messages;
	}
```

**讲解**

返回值是交给 `agent.prompt()` 的消息数组。按顺序：

1. **压缩检查。** 上一次运行的最后一条 assistant 消息如果显示上下文已超阈值，先压缩（§4.15）。参数 `false` 表示被中止的回复也要检查。
2. **`before_agent_start`。** 扩展可以改系统提示词选项、注入消息、强制系统提示词（→ M7）。
3. **起始消息。** 用户输入变成一条 `user` 消息（图片先按模型限制处理）；扩展触发的运行则是一条 `custom` 消息。
4. **附带消息。** `_pendingNextTurnMessages` 和 `before_agent_start` 返回的消息，都作为 `custom` 消息跟在后面。
5. **系统提示词补丁。** `_preparePromptAndToolLoadout()` 比较 transcript 当前的 `system` 消息和新算出的系统提示词，有差异时返回一条 `system` 消息，放在数组最前面（→ M6）。

新会话的 transcript 里还没有 `system` 消息，所以第一次 `prompt()` 一定会产生一条完整的 `system` 消息。这就是主线场景里 `message_start (system)` 的来源，也是 README §5.3 会话文件里那条 `content` 为空串、带 `sections` 的 `system` 条目。工具声明 `toolsAdded` 不是这里加的，而是 M2 的 `declareToolChanges()` 在请求前补到同一条消息上（M2 §4.4）。

`_runSystemPromptOptions` 记下本次运行用的系统提示词选项，`_startRun()` 的 `finally` 清掉它。

**往哪走**：`agent.prompt(messages)` 进入 M2。循环发出的每个事件到 §4.12。

### 4.12 _handleAgentEvent()

**调用位置**：M2 `Agent.processEvents()` → 订阅者 `_handleAgentEvent(event)`，循环 await 它。

函数有 96 行，分两段。第一段是转发：

源码：[`packages/coding-agent/src/core/agent-session.ts:1119-1161`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1119-L1161)

```ts
	private _handleAgentEvent = async (event: AgentEvent): Promise<void> => {
		// An abort requested while the run was starting, e.g. during before_agent_start, stops it
		// here. agent_start is awaited before the first request.
		if (event.type === "agent_start" && this._agentRunAbortRequested) this.agent.abort();
		// Record the calls a tool made through ctx.executeTool() and their usage on its result message.
		if (this._nestedToolCalls) {
			if (event.type === "message_start" && event.message.role === "toolResult") {
				const message = event.message;
				const summary = this._nestedToolCalls.takeRecord(message.toolCallId);
				if (summary?.calls) message.nestedCalls = summary.calls;
				if (summary?.usage) {
					message.usage = message.usage ? combineUsage(message.usage, summary.usage) : summary.usage;
				}
			} else if (event.type === "agent_end") {
				this._nestedToolCalls.clear();
			}
		}
		// When a user message starts, check if it's from either queue and remove it BEFORE emitting
		// This ensures the UI sees the updated queue state
		if (event.type === "message_start" && event.message.role === "user") {
			this._overflowRecoveryAttempted = false;
			const messageText = contentText(event.message.content, "");
			if (messageText) {
				// Check steering queue first
				const steeringIndex = this._steeringMessages.indexOf(messageText);
				if (steeringIndex !== -1) {
					this._steeringMessages.splice(steeringIndex, 1);
					this._emitQueueUpdate();
				} else {
					// Check follow-up queue
					const followUpIndex = this._followUpMessages.indexOf(messageText);
					if (followUpIndex !== -1) {
						this._followUpMessages.splice(followUpIndex, 1);
						this._emitQueueUpdate();
					}
				}
			}
		}

		// Emit to extensions first, then notify public listeners.
		await this._emitExtensionEvent(event);
		this._emit(event.type === "agent_end" ? { ...event, willRetry: this._willRetryAfterAgentEnd(event) } : event);

```

**讲解**

1. `agent_start` 时如果已经请求过中止（例如在 `before_agent_start` 期间调用了 `abort()`），立刻中止 `Agent`。
2. 嵌套工具调用的记录挂到工具结果消息上（→ M3）。
3. user 消息开始时，如果它的文本在 `_steeringMessages` 或 `_followUpMessages` 里，从显示队列中删掉并发 `queue_update`（§4.17）。
4. **1159 行先转给扩展。** `_emitExtensionEvent()`（`packages/coding-agent/src/core/agent-session.ts:1319`）把 AgentEvent 转成对应的扩展事件并 await。其中 `message_end` 用 `emitMessageEnd`，扩展可以返回一条替换消息，`_replaceMessageInPlace()` 原地改写消息对象，于是 `Agent` 的状态、后面的订阅者和会话文件看到的都是改写后的版本。
5. **1160 行再转给 `session.subscribe()` 的订阅者。** `agent_end` 在这里加上 `willRetry`，由 `_willRetryAfterAgentEnd()`（`packages/coding-agent/src/core/agent-session.ts:1216`）计算：重试开着、次数没用完、最后一条 assistant 消息是可重试的错误。

第二段是持久化：

源码：[`packages/coding-agent/src/core/agent-session.ts:1162-1214`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1162-L1214)

```ts
		// Handle session persistence
		if (event.type === "message_end") {
			let entryId: string | undefined;
			// Check if this is a custom message from extensions
			if (event.message.role === "custom") {
				// Persist as CustomMessageEntry
				entryId = this.sessionManager.appendCustomMessageEntry(
					event.message.customType,
					event.message.content,
					event.message.display,
					event.message.details,
				);
			} else if (
				event.message.role === "system" ||
				event.message.role === "user" ||
				event.message.role === "assistant" ||
				event.message.role === "toolResult"
			) {
				// Regular LLM message - persist as SessionMessageEntry
				entryId = this.sessionManager.appendMessage(event.message);
			}
			if (entryId) this._entryIdsByMessage.set(event.message, entryId);
			// Other message types (bashExecution, compactionSummary, branchSummary) are persisted elsewhere

			if (event.message.role === "assistant") {
				const assistantMsg = event.message as AssistantMessage;
				this._lastAssistantMessage = assistantMsg;
				if (assistantMsg.stopReason !== "error" && assistantMsg.stopReason !== "length") {
					this._overflowRecoveryAttempted = false;
				}

				// Reset retry counter immediately on successful assistant response
				// This prevents accumulation across multiple LLM calls within a turn
				if (assistantMsg.stopReason !== "error" && this._retryAttempt > 0) {
					this._emit({
						type: "auto_retry_end",
						success: true,
						attempt: this._retryAttempt,
					});
					this._retryAttempt = 0;
				}
			}
		}

		// A turn ends after its assistant message and every tool result has been appended,
		// so this is the first point in the run where a context-only custom message can be
		// inserted without landing between a tool call and its result. Flushing after the
		// extension and listener dispatch above also picks up messages that turn_end
		// handlers queued.
		if (event.type === "turn_end") {
			this._flushPendingCustomMessages();
		}
	};
```

**讲解**

`message_end` 时写会话文件：`custom` 消息写成 `custom_message` 条目，4 种标准 role 写成 `message` 条目（→ M4 `appendMessage`）。其余自定义消息不在这里写，`bashExecution` 在 `recordBashResult()` 写，两种摘要消息本身就是从条目投影出来的。

写完后把"消息对象 → 条目 id"记进 `_entryIdsByMessage`。重试时要靠它找到失败回复对应的条目（§4.14）。

assistant 消息的 `message_end` 还要更新重试状态：记下 `_lastAssistantMessage` 给 `_handlePostAgentRun()` 用；如果这条回复不是错误而 `_retryAttempt > 0`，说明重试成功了，立刻发 `auto_retry_end success=true`。

`turn_end` 时追加运行中积压的扩展消息。这时这一轮的 assistant 消息和工具结果都已写入，追加不会把它们隔开。

**顺序。** 对每个事件，处理顺序是扩展 → 订阅者 → 会话文件。§2.2 的 `[此刻会话条目数]` 就是在订阅者里读的，所以 `message_end (system)` 时还是 2，下一个事件时才看到 3。

**往哪走**：`agent.prompt()` 返回后回到 `_startRun()`，进入 §4.13。

### 4.13 _handlePostAgentRun()

**调用位置**：`_startRun()` 1922 行，每次 `agent.prompt()`/`agent.continue()` 返回后调用一次。返回 `true` 表示要调用 `agent.continue()`。

源码：[`packages/coding-agent/src/core/agent-session.ts:2000-2036`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L2000-L2036)

```ts
	private async _handlePostAgentRun(): Promise<boolean> {
		const message = this._lastAssistantMessage;
		this._lastAssistantMessage = undefined;
		if (this._agentRunAbortRequested) {
			this._finishCancelledRetry();
			return false;
		}
		if (!message) return this.agent.hasQueuedMessages();

		if (this._isRetryableError(message) && (await this._prepareRetry(message))) {
			if (this._agentRunAbortRequested) this._finishCancelledRetry();
			this._failedResponse = message;
			return !this._agentRunAbortRequested;
		}
		if (this._agentRunAbortRequested) {
			this._finishCancelledRetry();
			return false;
		}

		if (message.stopReason === "error" && this._retryAttempt > 0) {
			this._emit({
				type: "auto_retry_end",
				success: false,
				attempt: this._retryAttempt,
				finalError: message.errorMessage,
			});
			this._retryAttempt = 0;
		}

		if (await this._checkCompaction(message)) {
			return !this._agentRunAbortRequested;
		}

		// The low-level loop drains both queues before agent_end. Messages queued by
		// agent_end handlers require a fresh run before pre-settlement handlers fire.
		return !this._agentRunAbortRequested && this.agent.hasQueuedMessages();
	}
```

**讲解**

判断顺序：

1. 已请求中止：结束。
2. 这次运行没有 assistant 消息：有排队消息就继续。
3. **可重试的错误**，并且 `_prepareRetry()` 同意（次数没用完，等待没被打断）：继续（§4.14）。
4. 错误但不重试，而之前在重试：发 `auto_retry_end success=false`，计数归零。
5. **压缩检查**：`_checkCompaction()` 返回 `true`（溢出后压缩完要重试，或压缩完有排队消息）：继续（§4.15）。
6. 有排队消息：继续。M2 的循环在 `agent_end` 之前已经取空了两个队列，这里的排队消息是 `agent_end` 监听器里新加的。

重试先于压缩判断。上下文溢出的错误在 `_isRetryableError()` 里被排除，交给压缩处理。

**往哪走**：§4.14 重试，§4.15 压缩。

### 4.14 _isRetryableError() 和 _prepareRetry()

**调用位置**：`_handlePostAgentRun()` 2009 行。

源码：[`packages/coding-agent/src/core/agent-session.ts:3786-3794`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3786-L3794)

```ts
	/**
	 * Check if an error is retryable (overloaded, rate limit, server errors).
	 * Context overflow errors are NOT retryable (handled by compaction instead).
	 */
	private _isRetryableError(message: AssistantMessage): boolean {
		// Context overflow is handled by compaction, not retry.
		if (isContextOverflow(message, (this._modelForMessage(message) ?? this.model)?.contextWindow ?? 0)) return false;
		return isRetryableAssistantError(message);
	}
```

**讲解**

`isContextOverflow()` 和 `isRetryableAssistantError()` 都在 pi-ai（→ M1）。后者要求 `stopReason === "error"`，并且 `errorMessage` 匹配一组表示临时故障的模式（`overloaded`、限流、5xx 等，`packages/ai/src/utils/retry.ts:37`）。上下文溢出不重试，因为原样重发一定还会溢出。

源码：[`packages/coding-agent/src/core/agent-session.ts:3843-3883`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3843-L3883)

```ts
	private async _prepareRetry(message: AssistantMessage): Promise<boolean> {
		const settings = this.settingsManager.getRetrySettings();
		if (!settings.enabled) {
			return false;
		}

		this._retryAttempt++;

		if (this._retryAttempt > settings.maxRetries) {
			// Preserve the completed attempt count so post-run handling can emit the final failure.
			this._retryAttempt--;
			return false;
		}

		const delayMs = retryDelayMs(settings, this._retryAttempt);

		this._emit({
			type: "auto_retry_start",
			attempt: this._retryAttempt,
			maxAttempts: settings.maxRetries,
			delayMs,
			errorMessage: message.errorMessage || "Unknown error",
		});

		// Keep the failed attempt in raw history while durably omitting it from model projection.
		this._omitRecoveryAttempt(message);

		// Wait with exponential backoff (abortable)
		this._retryAbortController = new AbortController();
		try {
			await sleep(delayMs, this._retryAbortController.signal);
		} catch {
			// Aborted during sleep - emit end event so UI can clean up
			this._finishCancelledRetry();
			return false;
		} finally {
			this._retryAbortController = undefined;
		}

		return true;
	}
```

**讲解**

1. 读重试设置。默认 `enabled: true`、`maxRetries: 3`、`baseDelayMs: 2000`（`packages/coding-agent/src/core/settings-defaults.ts:18`）。
2. 计数加 1，超过上限就退回并返回 `false`。`_handlePostAgentRun()` 接着会发 `auto_retry_end success=false`。
3. 等待时间是 `baseDelayMs * 2^(attempt-1)`，上限默认 60 秒（`retryDelayMs`，`packages/ai/src/utils/retry.ts:146`）。
4. 发 `auto_retry_start`。
5. **`_omitRecoveryAttempt(message)`**（`packages/coding-agent/src/core/agent-session.ts:1260`）：给失败的回复（和它的工具结果，如果有）各追加一条 `context_edit` 条目，`replacement: null`，然后用会话文件刷新 `agent.state.messages`。失败的回复仍在会话文件里，但投影时被去掉，下一次请求看不到它（→ M4 `context_edit`）。
6. 用一个 `AbortController` 睡眠。等待期间 `_retryAbortController` 不为空，`isRetrying` 为 `true`。`abortRetry()` 或 `abort()` 会打断睡眠。

返回 `true` 后，`_startRun()` 调用 `agent.continue()`。transcript 最后一条是 user 消息（失败回复已被去掉），所以循环直接重新请求模型。

**往哪走**：§4.15。

### 4.15 _checkCompaction()

**调用位置**：`_handlePostAgentRun()` 2029 行（运行后），`_prepareRun()` 1952 行（运行前）。轮与轮之间的检查走 `_compactBeforeNextAssistantResponse()`（§4.6），只看阈值。

函数 135 行。开头是几个跳过条件：

源码：[`packages/coding-agent/src/core/agent-session.ts:3032-3056`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3032-L3056)

```ts
	private async _checkCompaction(assistantMessage: AssistantMessage, skipAbortedCheck = true): Promise<boolean> {
		const settings = this.settingsManager.getCompactionSettings(this.model);
		if (!settings.enabled) return false;

		// Skip if message was aborted (user cancelled) - unless skipAbortedCheck is false
		if (skipAbortedCheck && assistantMessage.stopReason === "aborted") return false;

		// Skip overflow check if the message came from a different model.
		// This handles the case where user switched from a smaller-context model (e.g. opus)
		// to a larger-context model (e.g. codex) - the overflow error from the old model
		// shouldn't trigger compaction for the new model. Under a virtual selection, the
		// physical model that produced the message supplies the limits.
		const messageModel = this._modelForMessage(assistantMessage);
		const sameModel = messageModel !== undefined;
		const contextWindow = (messageModel ?? this.model)?.contextWindow ?? 0;

		// Skip compaction checks if this assistant message is older than the latest
		// compaction boundary. This prevents a stale pre-compaction usage/error
		// from retriggering compaction on the first prompt after compaction.
		const compactionEntry = getLatestCompactionEntry(this.sessionManager.getBranch());
		const assistantIsFromBeforeCompaction =
			compactionEntry !== null && assistantMessage.timestamp <= new Date(compactionEntry.timestamp).getTime();
		if (assistantIsFromBeforeCompaction) {
			return false;
		}
```

**讲解**

- 压缩关闭：跳过。
- 被中止的回复（运行后检查时）：跳过。
- 回复来自另一个模型：溢出判断用的上下文窗口不对，所以溢出检查要求 `sameModel`。
- 回复的时间戳不晚于最新的压缩条目：它的 usage 反映的是压缩前的上下文，跳过。§7.3 会碰到这个条件。

接下来分两类情况：

源码：[`packages/coding-agent/src/core/agent-session.ts:3091-3128`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3091-L3128)

```ts
		if (contextOverflow || recoverableLength) {
			const willRetry = assistantMessage.stopReason !== "stop";

			// Case 2: the response completed successfully. Compact, but do not retry because
			// agent.continue() cannot continue from a completed assistant response.
			if (!willRetry) {
				return await this._runAutoCompaction("overflow", false);
			}

			if (this._overflowRecoveryAttempted) {
				const errorMessage = contextOverflow
					? "Context overflow recovery failed after one compact-and-retry attempt. Try reducing context or switching to a larger-context model."
					: "Truncated response recovery failed after one compact-and-retry attempt.";
				this._emit({
					type: "compaction_end",
					reason: "overflow",
					result: undefined,
					aborted: false,
					willRetry: false,
					errorMessage,
				});
				await this._emitSessionCompactFailed({
					reason: "overflow",
					errorMessage,
					aborted: false,
					willRetry: false,
					fromExtension: false,
				});
				return false;
			}

			// Persistently omit the selected final attempt before post-run recovery compaction.
			this._overflowRecoveryAttempted = true;
			this._omitRecoveryAttempt(assistantMessage);
			const retry = await this._runAutoCompaction("overflow", willRetry);
			if (retry) this._failedResponse = assistantMessage;
			return retry;
		}
```

**讲解**

**溢出。** 模型报了上下文溢出错误，或者 usage 显示已超出窗口，或者输出被截断但还没到模型的 `maxTokens`。如果回复本身是成功的（`stop`），只压缩、不重试。否则去掉失败回复、压缩、返回 `true` 让 `_startRun()` 调用 `agent.continue()`。"压缩再重试"只做一次，第二次就报 `compaction_end` 带错误信息。

3130–3166 行是第三类：

源码：[`packages/coding-agent/src/core/agent-session.ts:3155-3166`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3155-L3166)

```ts
					return false;
				}
			}
			contextTokens = estimate.tokens;
		} else {
			contextTokens = directContextTokens;
		}
		if (shouldCompact(contextTokens, contextWindow, settings)) {
			return await this._runAutoCompaction("threshold", false);
		}
		return false;
	}
```

**阈值。** 从回复的 `usage` 取上下文 token 数（错误回复或 usage 为 0 时估算），用 M5 的 `shouldCompact()` 判断：`contextTokens > contextWindow - reserveTokens`。超过就压缩，不重试。

三种情况都调用 `_runAutoCompaction()`（`packages/coding-agent/src/core/agent-session.ts:3178`）：发 `compaction_start`，派发扩展的 `session_before_compact`，调用 M5 的 `compact()` 生成摘要，`appendCompaction()` 写条目，刷新 `agent.state.messages`，发 `compaction_end`。压缩期间 `_autoCompactionAbortController` 不为空，`isCompacting` 为 `true`。

**往哪走**：后处理循环结束后，§4.16。

### 4.16 _emitAgentSettled()

**调用位置**：`_startRun()` 的 `finally`，1937 行。

源码：[`packages/coding-agent/src/core/agent-session.ts:1094-1116`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1094-L1116)

```ts
	private async _emitAgentSettled(): Promise<void> {
		this._cacheWarmer?.onAgentSettled();
		this._isAgentRunActive = false;
		this._isEmittingAgentSettled = true;
		try {
			const aborted = this._agentRunAbortRequested;
			await this._extensionRunner.emit({ type: "agent_settled", aborted });
			this._emit({ type: "agent_settled", aborted });
		} finally {
			this._isEmittingAgentSettled = false;
		}

		const deferred = this._deferredSettledActions.splice(0);
		if (deferred.length > 0) {
			try {
				for (const action of deferred) await action();
			} finally {
				this._resolveIdleWaitIfIdle();
			}
			return;
		}
		this._resolveIdleWaitIfIdle();
	}
```

**讲解**

1096 行把 `_isAgentRunActive` 设为 `false`，所以订阅者收到 `agent_settled` 时 `isStreaming` 已经是 `false`（§2.2 最后一行的标志）。

先发给扩展（await），再发给订阅者。`aborted` 取 `_agentRunAbortRequested`。

发事件期间 `_isEmittingAgentSettled` 为 `true`。这时调用 `prompt()` 或触发轮次的 `sendCustomMessage()` 会被推进 `_deferredSettledActions`（§4.9 第 1 步），等事件发完再依次执行。否则监听器里开始的新运行会和本次运行的收尾交错。

最后 `_resolveIdleWaitIfIdle()` 唤醒 `waitForIdle()` 的等待者，`abort()` 就是在等这个。

**往哪走**：一次运行读完了。§4.17 读运行中的外部操作。

### 4.17 steer()、followUp()、clearQueue()、abort() 和状态标志

**调用位置**：运行中由界面或扩展调用。`prompt()` 在 `isStreaming` 时按 `streamingBehavior` 转到这里（§4.9）。

`steer()` 和 `followUp()`（`packages/coding-agent/src/core/agent-session.ts:2296`、`packages/coding-agent/src/core/agent-session.ts:2312`）都调用 `_queueUserInput()`：拒绝扩展命令，派发 `input` 事件，展开模板，然后进入下面两个函数之一：

源码：[`packages/coding-agent/src/core/agent-session.ts:2323-2348`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L2323-L2348)

```ts
	private async _queueSteer(text: string, images?: ImageContent[]): Promise<void> {
		this._steeringMessages.push(text);
		this._emitQueueUpdate();
		const content: (TextContent | ImageContent)[] = [{ type: "text", text }];
		if (images) {
			content.push(...images);
		}
		this.agent.steer({
			role: "user",
			content,
			timestamp: Date.now(),
		});
	}

	/**
	 * Internal: Queue a follow-up message (already expanded, no extension command check).
	 */
	private async _queueFollowUp(text: string, images?: ImageContent[]): Promise<void> {
		this._followUpMessages.push(text);
		this._emitQueueUpdate();
		const content: (TextContent | ImageContent)[] = [{ type: "text", text }];
		if (images) {
			content.push(...images);
		}
		this.agent.followUp({ role: "user", content, timestamp: Date.now() });
	}
```

**讲解**

每个排队操作做两件事：把文本放进 `AgentSession` 自己的显示队列并发 `queue_update`；把一条 user 消息放进 M2 `Agent` 的队列。真正决定消息何时进入 transcript 的是 M2 的队列：steering 在每轮结束后被取出，follow-up 在循环没有工具调用时才取出（M2 §4.15、§4.16）。

两个队列对应得上，靠的是 §4.12 第 3 步：user 消息 `message_start` 时，按文本从显示队列里删掉，再发一次 `queue_update`。

`clearQueue()` 两边一起清：

源码：[`packages/coding-agent/src/core/agent-session.ts:2482-2495`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L2482-L2495)

```ts
	/**
	 * Clear all queued messages and return them.
	 * Useful for restoring to editor when user aborts.
	 * @returns Object with steering and followUp arrays
	 */
	clearQueue(): { steering: string[]; followUp: string[] } {
		const steering = [...this._steeringMessages];
		const followUp = [...this._followUpMessages];
		this._steeringMessages = [];
		this._followUpMessages = [];
		this.agent.clearAllQueues();
		this._emitQueueUpdate();
		return { steering, followUp };
	}
```

`abort()` 和 `waitForIdle()`：

源码：[`packages/coding-agent/src/core/agent-session.ts:2516-2536`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L2516-L2536)

```ts
	/**
	 * Abort current operation and wait for agent to become idle.
	 */
	async abort(): Promise<void> {
		if (this._isAgentRunActive) {
			this._agentRunAbortRequested = true;
		}
		this.abortRetry();
		this.abortCompaction();
		this.abortBranchSummary();
		if (this._isBeforeSettle) this._abortDuringBeforeSettle = true;
		this.agent.abort();
		await this.waitForIdle();
	}

	async waitForIdle(): Promise<void> {
		if (this.isIdle) {
			return;
		}
		await this._getIdleWaitPromise();
	}
```

`abort()` 设置 `_agentRunAbortRequested`，打断重试等待、压缩、分支摘要，中止 `Agent`（M2 的 `abort()` 触发 `AbortSignal`，传给 `streamFn` 和工具），然后等到会话空闲。后处理循环看到中止标志就不再继续（§4.10），`agent_settled` 的 `aborted` 为 `true`。

**4 个状态标志：**

源码：[`packages/coding-agent/src/core/agent-session.ts:1494-1502`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1494-L1502)

```ts
	/** Whether the session is currently processing an agent run or post-run continuation. */
	get isStreaming(): boolean {
		return this._isAgentRunActive;
	}

	/** Whether the session has no active agent run, compaction, branch summary, retry, or queued continuation. */
	get isIdle(): boolean {
		return !this._isAgentRunActive && !this.isCompacting;
	}
```

源码：[`packages/coding-agent/src/core/agent-session.ts:1663-1670`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1663-L1670)

```ts
	/** Whether compaction or branch summarization is currently running */
	get isCompacting(): boolean {
		return (
			this._autoCompactionAbortController !== undefined ||
			this._compactionAbortController !== undefined ||
			this._branchSummaryAbortController !== undefined
		);
	}
```

`isRetrying` 和 `isBashRunning` 在 `packages/coding-agent/src/core/agent-session.ts:3893` 和 `packages/coding-agent/src/core/agent-session.ts:3993`，分别是 `_retryAbortController !== undefined` 和 `_bashAbortControllers.size > 0`。

| 标志 | 为 `true` 的区间 | 设置位置 |
|---|---|---|
| `isStreaming` | `_startRun()` 开头到 `_emitAgentSettled()`，包括 `_prepareRun`、所有 `agent.continue()`、重试等待和运行后的压缩 | 1895 行设、1096 行清 |
| `isCompacting` | 自动压缩、手动压缩或分支摘要进行中 | `_runAutoCompaction` 等函数设置 AbortController |
| `isRetrying` | 重试的退避睡眠期间。发 `auto_retry_start` 时还是 `false` | `_prepareRetry` |
| `isBashRunning` | 用户 `!cmd` 命令执行中，与 agent 运行无关 | `executeBash`（`packages/coding-agent/src/core/agent-session.ts:3922`） |

`isIdle` 是 `!isStreaming && !isCompacting`。

**实验**：`m8-session.ts steer` 在第 1 次请求里调用 `session.steer()`：

```bash
WORK=/tmp/pi-lab/m8-work-steer PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-steer node --experimental-strip-types m8-session.ts steer
```

```
场景：steer
  15ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  15ms  turn_start
  15ms  message_start (system)
  15ms  message_end (system)  [此刻会话条目数=2]
  15ms  message_start (user)
  15ms  message_end (user)  [此刻会话条目数=3]
  17ms  [模型] 第 1 次请求，收到 2 条 LLM 消息：system, user
  17ms  [steer] 调用 session.steer()
  17ms  queue_update steering=["插话：顺便看下测试"] followUp=[]
  18ms  message_start (assistant)
  18ms  message_update x8
  18ms  message_end (assistant) stopReason=toolUse  [此刻会话条目数=4]
  18ms  tool_execution_start read
  22ms  tool_execution_end read
  22ms  message_start (toolResult)
  22ms  message_end (toolResult)  [此刻会话条目数=5]
  22ms  turn_end (assistant)
  22ms  turn_start
  23ms  queue_update steering=[] followUp=[]
  23ms  message_start (user)
  23ms  message_end (user)  [此刻会话条目数=6]
  23ms  [模型] 第 2 次请求，收到 5 条 LLM 消息：system, user, assistant, toolResult, user
  23ms  message_start (assistant)
  23ms  message_update x4
  23ms  message_end (assistant) stopReason=stop  [此刻会话条目数=7]
  23ms  turn_end (assistant)
…省略…
```

`queue_update` 出现两次：`steer()` 时队列里有插话；第 2 轮 `turn_start` 后、插话的 `message_start (user)` 之前，队列变空。第 2 次请求收到 5 条消息，最后一条就是插话。

`m8-session.ts abort` 在第 1 次请求里调用 `session.abort()`：

```bash
WORK=/tmp/pi-lab/m8-work-abort PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-abort node --experimental-strip-types m8-session.ts abort
```

```
…省略…
  24ms  [模型] 第 1 次请求，收到 2 条 LLM 消息：system, user
  24ms  [abort] 调用 session.abort()
  25ms  message_start (assistant)
  25ms  message_end (assistant) stopReason=aborted errorMessage="Request was aborted"  [此刻会话条目数=4]
  25ms  turn_end (assistant)
  25ms  agent_end willRetry=false messages=[system, user, assistant]  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  25ms  agent_settled aborted=true  {isStreaming=false isCompacting=false isRetrying=false isBashRunning=false}
  25ms  [prompt 返回] isStreaming=false isCompacting=false isRetrying=false isBashRunning=false isIdle=true 模型请求次数=1
…省略…
```

faux provider 发现信号已中止，返回 `stopReason=aborted` 的回复。这条回复照样写进会话文件。`_handlePostAgentRun()` 看到中止标志直接结束，`agent_settled aborted=true`。

**往哪走**：§4.18 补上创建会话的外层。

### 4.18 createAgentSessionServices() 和 AgentSessionRuntime

**调用位置**：M9 的 `main()` 通过 `createAgentSessionRuntime(createRuntime, …)` 创建运行时，工厂函数 `createRuntime` 里依次调用这两个函数（→ M9）。

源码：[`packages/coding-agent/src/core/agent-session-services.ts:135-154`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-services.ts#L135-L154)

```ts
export async function createAgentSessionServices(
	options: CreateAgentSessionServicesOptions,
): Promise<AgentSessionServices> {
	const cwd = resolvePath(options.cwd);
	const agentDir = options.agentDir ? resolvePath(options.agentDir) : getAgentDir();
	const modelRuntime =
		options.modelRuntime ??
		(await ModelRuntime.create({
			authPath: join(agentDir, "auth.json"),
			modelsPath: join(agentDir, "models.json"),
			signal: options.modelRuntimeSignal,
		}));
	const settingsManager = options.settingsManager ?? SettingsManager.create(cwd, agentDir);
	const resourceLoader = new DefaultResourceLoader({
		...(options.resourceLoaderOptions ?? {}),
		cwd,
		agentDir,
		settingsManager,
	});
	await resourceLoader.reload(options.resourceLoaderReloadOptions);
```

**讲解**

`createAgentSessionServices()` 创建和 cwd 绑定的三个服务，并加载资源（扩展、skills、AGENTS.md）。之后（155–195 行）把扩展在加载时登记的 provider、虚拟模型注册进 `modelRuntime`，错误放进 `diagnostics` 而不是直接退出。`lab/faux-ext.ts` 的 `pi.registerProvider()` 就是在这里真正生效的。

`createAgentSessionFromServices()`（`packages/coding-agent/src/core/agent-session-services.ts:214`）只是把服务的字段原样传给 `createAgentSession()`。

为什么分两步：服务要先建好，才能用它们解析模型名、工具列表等会话参数（例如 `--model` 要在已注册的模型里查）。

`AgentSessionRuntime` 持有当前的会话和服务。`/new`、`/resume`、`/fork` 不在旧会话上修改，而是拆掉旧的、用同一个工厂建新的：

源码：[`packages/coding-agent/src/core/agent-session-runtime.ts:167-178`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-runtime.ts#L167-L178)

```ts
	private async teardownCurrent(reason: SessionShutdownEvent["reason"], targetSessionFile?: string): Promise<void> {
		// Settle any active response first so the aborted turn (including tool
		// results) is persisted to the outgoing session before it is replaced.
		await this.session.abort();
		await emitSessionShutdownEvent(this.session.extensionRunner, {
			type: "session_shutdown",
			reason,
			targetSessionFile,
		});
		this.beforeSessionInvalidate?.();
		this.session.dispose();
	}
```

**讲解**

拆除顺序：先 `abort()` 等运行结束，这样被中止的那一轮会写进旧会话文件；再发扩展的 `session_shutdown`；最后 `dispose()`，它会中止所有进行中的操作、让扩展上下文失效、取消订阅。之后 `newSession()` 等方法调用 `this.createRuntime(...)` 建一个全新的 `AgentSession`（`packages/coding-agent/src/core/agent-session-runtime.ts:226`）。

所以一个 `AgentSession` 对象始终对应一个会话文件和一个 cwd。换会话就换对象。

**往哪走**：读完了。§5 把流程画成状态机。

---

## 5. 状态机

### 5.1 一次 session.prompt() 的生命周期

```
            prompt()，isStreaming=false
  ┌──────┐  agent-session.ts:2193                ┌──────────────────────┐
  │ idle │ ─────────────────────────────────────▶│ preparing            │
  └──────┘  _isAgentRunActive=true (1895)        │ _prepareRun (1947)   │
     ▲                                           │ before_agent_start   │
     │                                           └──────────┬───────────┘
     │                                     agent.prompt (1920)│
     │                                                      ▼
     │                                           ┌──────────────────────┐
     │      ┌──────────────────────────────────▶ │ agent running (M2)   │
     │      │ agent.continue() (1924 / 1929)     │ _handleAgentEvent    │
     │      │                                    └──────────┬───────────┘
     │      │                                  agent_end，prompt 返回
     │      │                                               ▼
     │      │      ┌────────────────┐  可重试错误  ┌──────────────────────┐
     │      ├──────│ retry waiting  │◀────────────│ post-run             │
     │      │ 睡完 │ isRetrying     │ (2009)       │ _handlePostAgentRun  │
     │      │      └────────────────┘              │ (2000)               │
     │      │      ┌────────────────┐  超阈值/溢出  │                      │
     │      ├──────│ compacting     │◀────────────│                      │
     │      │溢出重试│ isCompacting   │ (2029)       │                      │
     │      │/有排队 └────────────────┘              └──────────┬───────────┘
     │      │                                     返回 false │
     │      │                                               ▼
     │      │  agent_before_settle 要求继续       ┌──────────────────────┐
     │      └─────────────────────────────────────│ before settle (2038) │
     │                                  (1927)    └──────────┬───────────┘
     │                                                      │ 不继续 / 中止 break
     │                                                      ▼
     │  _isAgentRunActive=false (1096)          ┌──────────────────────┐
     └──────────────────────────────────────────│ settling             │
        resolve waitForIdle (1115)              │ agent_settled (1101) │
                                                └──────────────────────┘

 任意状态 abort()：_agentRunAbortRequested=true (2521)，后处理循环 break (1921)
 preparing 期间再 prompt()：isStreaming=true → 排队 (2158)
 settling 期间再 prompt()：推迟到 agent_settled 发完 (2114-2116)
```

### 5.2 重试计数

```
               可重试错误，_prepareRetry：
               _retryAttempt++ (3849)，≤ maxRetries
  ┌───────────┐ ─────────────────────────────▶ ┌──────────────────┐
  │ attempt=0 │   auto_retry_start (3859)      │ attempt=n        │
  └───────────┘                                │ 失败回复被 context_ │
     ▲   ▲                                     │ edit 去掉 (3868)   │
     │   │ 下一条回复成功：                      └────┬─────────┬───┘
     │   │ auto_retry_end success=true (1196)      │         │ 又是可重试错误，
     │   └─────────────────────────────────────────┘         │ attempt+1 > max：
     │                                                       │ 计数退回 (3853)
     │  auto_retry_end success=false (2019)，attempt=0 (2026)   │
     └───────────────────────────────────────────────────────┘
     中止或睡眠被打断：_finishCancelledRetry (3827)，finalError="Retry cancelled"
```

### 5.3 一个事件在 _handleAgentEvent 里的路径

```
 AgentEvent ─▶ [agent_start 且已请求中止 → agent.abort()] (1122)
          ─▶ [user message_start → 从显示队列删除，queue_update] (1138-1156)
          ─▶ _emitExtensionEvent (1159)    扩展，await；message_end 可被原地替换
          ─▶ _emit (1160)                  订阅者，不 await；agent_end 加 willRetry
          ─▶ [message_end → appendMessage / appendCustomMessageEntry] (1163-1185)
          ─▶ [assistant message_end → _lastAssistantMessage，重试成功判定] (1188-1203)
          ─▶ [turn_end → 追加积压的扩展消息] (1211-1213)
```

---

## 6. 设计要点

### 6.1 会话文件是上下文的唯一来源

**做法。** 每次请求前，`prepareRequest` 用 `sessionManager.buildSessionProjection()` 替换循环传入的 messages（§4.5）。压缩、重试、扩展的 context_edit 都只往会话文件追加条目，然后用 `_refreshFinalizedContext()`（`packages/coding-agent/src/core/agent-session.ts:954`）从会话文件刷新 `agent.state.messages`。

**理由。** 会改上下文的地方很多：压缩、重试去掉失败回复、扩展编辑、分支导航。如果它们各自改内存数组，再同步到文件，两边早晚会不一致。现在只有一个写入方向（追加条目）和一个读取方向（投影），模型看到的上下文永远能从会话文件复现，`pi -c` 恢复后的第一次请求和不中断时一样。§4.5 的实验说明了这一点：不在会话文件里的消息，即使在 `agent.state.messages` 里，也不会发给模型。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:803`。

### 6.2 通过给字段赋值来串联 hook

**做法。** 每个 `_install*` 都先保存 `agent` 字段的旧值，新函数里调用旧值再加自己的逻辑（§4.3、§4.7）。`transformContext` 被包了三层：`sdk.ts` 的扩展事件、隐藏声明、强制提示词。

**理由。** M2 的 `Agent` 不需要知道有多少个上层功能想插一脚，它只认一个字段。每个功能独立安装，互不知道对方。代价是执行顺序取决于安装顺序，要读构造函数才知道。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:526-532`。

### 6.3 扩展先于订阅者，订阅者先于持久化，订阅者不被 await

**做法。** `_handleAgentEvent` 对每个事件依次：await 扩展、同步调用订阅者、写会话文件（§4.12）。`session.subscribe()` 的监听器类型返回 `void`。

**理由。** 扩展可以改写 `message_end` 的消息，所以要在任何人看到之前运行；写进会话文件的必须是改写后的版本，所以持久化放最后。订阅者是界面和输出，它们不应该拖慢循环；需要背压的地方（print 模式的 json 输出）另外订阅 `session.agent`（`packages/coding-agent/src/modes/print-mode.ts:115`）。M2 的 `Agent` 订阅者都被 await，M8 只 await 扩展和自己。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:1158-1163`。

### 6.4 失败的回复留在文件里，从上下文中去掉

**做法。** 重试前，`_omitRecoveryAttempt()` 给失败的回复追加一条 `replacement: null` 的 `context_edit`（§4.14）。§7.2 的会话文件里，失败的 `assistant` 条目还在，后面紧跟一条指向它的 `context_edit`。

**理由。** 会话文件是追加写的日志，不改已写的行。失败回复要从模型的上下文里消失，否则重试请求的结尾是一条错误回复，模型和 provider 都会出问题。用 `context_edit` 表达"投影时去掉"，文件仍然完整记录发生过什么，重启后的投影结果也一样。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:1260`。

### 6.5 运行的后处理放在 Agent 外面

**做法。** 重试和运行后的压缩不在 M2 的循环里，而在 `_startRun()` 的 while 循环里：`agent.prompt()` 返回后判断，需要就 `agent.continue()`（§4.10）。整件事结束时发 `agent_settled`。

**理由。** M2 的循环只管一次运行的规则。重试要等待、压缩要调另一个模型，这些是产品策略，放在外层可以单独开关和配置。对订阅者来说，`agent_end` 只表示"一次循环结束"，`agent_settled` 才表示"可以接受下一个输入了"。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:1919-1938`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行。毫秒数、`message_update` 个数和 id 每次运行都不同。7.3 的 `tokensBefore` 是估算值，会随 WORK 路径长度相差几个 token。

### 7.1 绕过会话文件和经过会话文件的消息

§4.5 的 `projection` 场景已经说明：直接塞进 `agent.state.messages` 的 user 消息不会发给模型，因为 `prepareRequest` 用会话文件重建了上下文。

**问题。** 改成在 `prompt()` 前调用 `session.sendCustomMessage({ customType: "note", content: "…", display: false })`（不触发轮次），模型第 1 次请求会收到几条消息？顺序如何？

**预测。** 不在运行中时，`sendCustomMessage` 走 `_appendCustomMessage()`（`packages/coding-agent/src/core/agent-session.ts:2416`）：先 `appendCustomMessageEntry()` 写进会话文件，再从会话文件刷新 `agent.state.messages`。投影出的 `custom` 消息经 `convertToLlm` 变成 user 消息。它排在本次 prompt 的 `system` 补丁和 user 消息前面，所以模型收到 3 条：`user, system, user`。会话条目数比 §2.2 多 1。

**运行。**

```bash
WORK=/tmp/pi-lab/m8-work-custom PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-custom node --experimental-strip-types m8-session.ts custom
```

```
  26ms  [custom] prompt 前 agent.state.messages=custom 会话条目=model_change, thinking_level_change, custom_message
场景：custom
  28ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  29ms  turn_start
  29ms  message_start (system)
  29ms  message_end (system)  [此刻会话条目数=3]
  29ms  message_start (user)
  29ms  message_end (user)  [此刻会话条目数=4]
  31ms  [模型] 第 1 次请求，收到 3 条 LLM 消息：user, system, user
…省略…
```

**结论。** 预测成立。两种做法的差别只在于消息有没有进会话文件。`system` 消息在 `custom` 后面，M1 的 `resolveTranscript` 会为不支持中途 system 消息的 provider 处理这种顺序（→ M1）。

### 7.2 自动重试

**问题。** 第 1 次请求抛出 `503 Service Unavailable: overloaded`。会有几个 `agent_start`、几个 `agent_settled`？失败的回复会进会话文件吗？重试请求里能看到它吗？`isRetrying` 什么时候为 `true`？

**预测。**

- `overloaded` 匹配可重试模式。第 1 次运行的 `agent_end willRetry=true`。
- `auto_retry_start attempt=1/3 delayMs=10`（脚本把 `baseDelayMs` 设成 10），随后 `entry_appended context_edit replacement=null`。
- 发 `auto_retry_start` 时 `isRetrying` 还是 `false`，睡眠期间为 `true`。
- `agent.continue()` 开始第 2 次运行：又一个 `agent_start`，但没有 `message_* (system/user)`，因为 `continue()` 不加新消息。模型收到 2 条消息，失败回复被去掉了。
- 第 2 次请求成功，它的 `message_end` 之后立刻 `auto_retry_end success=true`。
- 只有一个 `agent_settled`。会话文件里失败的 `assistant` 条目后面跟一条 `context_edit`。

**运行。**

```bash
WORK=/tmp/pi-lab/m8-work-retry PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-retry node --experimental-strip-types m8-session.ts retry
```

```
场景：retry
  18ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  18ms  turn_start
  18ms  message_start (system)
  18ms  message_end (system)  [此刻会话条目数=2]
  18ms  message_start (user)
  18ms  message_end (user)  [此刻会话条目数=3]
  20ms  [模型] 第 1 次请求，收到 2 条 LLM 消息：system, user
  20ms  message_start (assistant)
  20ms  message_end (assistant) stopReason=error errorMessage="503 Service Unavailable: overloaded"  [此刻会话条目数=4]
  20ms  turn_end (assistant)
  21ms  agent_end willRetry=true messages=[system, user, assistant]  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  22ms  auto_retry_start attempt=1/3 delayMs=10 errorMessage="503 Service Unavailable: overloaded"  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  22ms  entry_appended context_edit targetId=0dfdac96 replacement=null
  27ms  [5ms 后] {isStreaming=true isCompacting=false isRetrying=true isBashRunning=false}
  33ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  33ms  turn_start
  33ms  [模型] 第 2 次请求，收到 2 条 LLM 消息：system, user
  33ms  message_start (assistant)
  33ms  message_update x6
  33ms  message_end (assistant) stopReason=toolUse  [此刻会话条目数=6]
  34ms  auto_retry_end success=true attempt=1
  34ms  tool_execution_start read
  35ms  tool_execution_end read
  35ms  message_start (toolResult)
  35ms  message_end (toolResult)  [此刻会话条目数=7]
  35ms  turn_end (assistant)
  36ms  turn_start
  36ms  [模型] 第 3 次请求，收到 4 条 LLM 消息：system, user, assistant, toolResult
  36ms  message_start (assistant)
  36ms  message_update x4
  36ms  message_end (assistant) stopReason=stop  [此刻会话条目数=8]
  36ms  turn_end (assistant)
  36ms  agent_end willRetry=false messages=[assistant, toolResult, assistant]  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  36ms  agent_settled aborted=false  {isStreaming=false isCompacting=false isRetrying=false isBashRunning=false}
  36ms  [prompt 返回] isStreaming=false isCompacting=false isRetrying=false isBashRunning=false isIdle=true 模型请求次数=3
[AgentState] messages=system, user, assistant, toolResult, assistant
[会话文件] $PI_CODING_AGENT_DIR/sessions/2026-11-01T07-19-46-249Z_01a195fa-8749-70e4-b7a0-7a13c9a6ae9d.jsonl
  session                 id=01a195fa
  model_change            id=046cb021
  thinking_level_change   id=6979d9fb
  message               system     ""  id=d49f407a
  message               user       [{"type":"text","text":"读 README.md，总结成三句话"}]  id=26185f97
  message               assistant  [] stop=error  id=0dfdac96
  context_edit          targetId=0dfdac96 replacement=null  id=15ae011c
  message               assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall"," stop=toolUse  id=b5bde480
  message               toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nI  id=341a6468
  message               assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。" stop=stop  id=ce1e4bf8
```

**结论。** 预测都成立。第 2 次运行的 `agent_end` 里 `messages` 只有 `assistant, toolResult, assistant`，因为 M2 的 `agent_end.messages` 只含本次运行新增的消息。`[此刻会话条目数]` 从 4 跳到 6，中间那个是 `context_edit`。

**再试：重试用完。** `retry-fail` 把 `maxRetries` 设为 1，前两次请求都失败：

```bash
WORK=/tmp/pi-lab/m8-work-rf PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-rf node --experimental-strip-types m8-session.ts retry-fail
```

```
…省略…
  36ms  entry_appended context_edit targetId=eb915943 replacement=null
  40ms  [5ms 后] {isStreaming=true isCompacting=false isRetrying=true isBashRunning=false}
  47ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  47ms  turn_start
  47ms  [模型] 第 2 次请求，收到 2 条 LLM 消息：system, user
  48ms  message_start (assistant)
  48ms  message_end (assistant) stopReason=error errorMessage="503 Service Unavailable: overloaded"  [此刻会话条目数=6]
  48ms  turn_end (assistant)
  48ms  agent_end willRetry=false messages=[assistant]  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  48ms  auto_retry_end success=false attempt=1 finalError="503 Service Unavailable: overloaded"
…省略…
```

第 2 次的 `agent_end willRetry=false`，因为 `_retryAttempt`（1）已经等于 `maxRetries`。`_prepareRetry()` 计数加到 2 超过上限，退回 1 并返回 `false`；`_handlePostAgentRun()` 于是发 `auto_retry_end success=false`，`finalError` 是最后一次的错误信息。第 2 条失败回复没有被 `context_edit` 去掉，它留在上下文里，用户能看到错误。

### 7.3 阈值压缩发生在哪里

**问题。** faux-1 的 `contextWindow` 是 128000。`compact` 场景把 `compaction.reserveTokens` 设为 127000，阈值就是 1000 token；faux 的 usage 估算让每条回复都超过 1000。压缩会在哪些时刻发生？第 3 次请求（总结回复）里模型收到的 4 条消息分别是什么？

**预测。**

- 第 1 轮结束后，M2 调用 `prepareNextTurnWithContext`，`_compactBeforeNextAssistantResponse()` 发现超过阈值，在第 2 轮 `turn_start` 之前压缩（§4.6）。这次压缩要请求模型生成摘要，脚本把第 2 个 faux 回复留给它。
- 摘要条目写入后，第 3 次请求的上下文是 `system`、`compactionSummary`（`convertToLlm` 转成 user）、被保留的 assistant 和 toolResult。
- `agent_end` 之后，`_handlePostAgentRun()` → `_checkCompaction()` 用第 2 条回复的 usage 再判断一次，又超过阈值，再压缩一次。返回值是 `hasQueuedMessages()`，为 `false`，所以不 `continue()`。
- 两次压缩期间 `isCompacting=true`。

**运行。**

```bash
WORK=/tmp/pi-lab/m8-work-compact PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-compact node --experimental-strip-types m8-session.ts compact
```

```
场景：compact
  24ms  agent_start  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  24ms  turn_start
  24ms  message_start (system)
  24ms  message_end (system)  [此刻会话条目数=2]
  24ms  message_start (user)
  24ms  message_end (user)  [此刻会话条目数=3]
  26ms  [模型] 第 1 次请求，收到 2 条 LLM 消息：system, user
  26ms  message_start (assistant)
  26ms  message_update x7
  26ms  message_end (assistant) stopReason=toolUse  [此刻会话条目数=4]
  26ms  tool_execution_start read
  39ms  tool_execution_end read
  39ms  message_start (toolResult)
  39ms  message_end (toolResult)  [此刻会话条目数=5]
  40ms  turn_end (assistant)
  41ms  compaction_start reason=threshold  {isStreaming=true isCompacting=true isRetrying=false isBashRunning=false}
  41ms  [模型] 第 2 次请求，收到 2 条 LLM 消息：system, user
  42ms  compaction_end reason=threshold willRetry=false aborted=false tokensBefore=1340
  42ms  turn_start
  42ms  [模型] 第 3 次请求，收到 4 条 LLM 消息：system, user, assistant, toolResult
  48ms  message_start (assistant)
  48ms  message_update x5
  48ms  message_end (assistant) stopReason=stop  [此刻会话条目数=7]
  48ms  turn_end (assistant)
  48ms  agent_end willRetry=false messages=[system, user, assistant, toolResult, assistant]  {isStreaming=true isCompacting=false isRetrying=false isBashRunning=false}
  49ms  compaction_start reason=threshold  {isStreaming=true isCompacting=true isRetrying=false isBashRunning=false}
  49ms  [模型] 第 4 次请求，收到 2 条 LLM 消息：system, user
  49ms  compaction_end reason=threshold willRetry=false aborted=false tokensBefore=1399
  49ms  agent_settled aborted=false  {isStreaming=false isCompacting=false isRetrying=false isBashRunning=false}
  49ms  [prompt 返回] isStreaming=false isCompacting=false isRetrying=false isBashRunning=false isIdle=true 模型请求次数=4
[AgentState] messages=system, compactionSummary, assistant
[usage] 各条 assistant 消息的 usage.totalTokens=[1399]，阈值=128000-127000=1000
[会话文件] $PI_CODING_AGENT_DIR/sessions/2026-11-01T07-19-47-271Z_01a195fa-8b47-756a-abd4-4a2788d7a7d8.jsonl
  session                 id=01a195fa
  model_change            id=8a2ee4d0
  thinking_level_change   id=68c0d060
  message               system     ""  id=39a4a0ee
  message               user       [{"type":"text","text":"读 README.md，总结成三句话"}]  id=c7268516
  message               assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall"," stop=toolUse  id=ae672a48
  message               toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nI  id=8ea3002f
  compaction            firstKeptEntryId=ae672a48 tokensBefore=1340 summary="No prior history.\n\n---\n\n**Turn Cont  id=aae36731
  message               assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。" stop=stop  id=53a7a549
  compaction            firstKeptEntryId=53a7a549 tokensBefore=1399 summary="[摘要 2] agent 读了 README.md 并给出总结。\n\n<re  id=f0051b4a
```

**结论。** 两次压缩的位置和预测一致：一次在 `turn_end` 和第 2 个 `turn_start` 之间（`reason=threshold`，在循环内部），一次在 `agent_end` 和 `agent_settled` 之间（在后处理循环）。第 2、4 次请求是生成摘要的请求，收到的 `system, user` 是 M5 的摘要提示词，不是对话。第 3 次请求的 4 条消息里第 2 条 user 是压缩摘要。

脚本里第 3 个回复故意晚 5ms 生成。没有这 5ms 时，回复和第一个压缩条目可能落在同一毫秒，`_checkCompaction()` 的"不晚于最新压缩条目就跳过"条件（`packages/coding-agent/src/core/agent-session.ts:3053`）会成立，运行后的第二次压缩就不会发生。编写本文时不加等待的版本确实出现过只压缩一次的结果。

---

## 8. 自测与衔接

### 8.1 自测

1. 一个扩展在 `message_end` 里把 assistant 回复的文本改成全大写。会话文件里存的是原文还是大写？`session.subscribe()` 的订阅者看到的呢？
2. 主线场景里，用户在 read 工具执行期间调用 `session.prompt("再看下 package.json")`，没有传 `streamingBehavior`。会发生什么？
3. 模型连续两次返回 `503 overloaded`（`maxRetries` 为默认的 3），第 3 次返回一段纯文本、不调用工具。会话文件里有几个 assistant 条目、几个 `context_edit` 条目？订阅者会收到几个 `agent_end`、几个 `agent_settled`？

<details>
<summary>答案</summary>

1. 都是大写。`_handleAgentEvent` 先 await 扩展（`packages/coding-agent/src/core/agent-session.ts:1159`），`_replaceMessageInPlace()` 原地改写消息对象；之后才调用订阅者（1160 行）和 `appendMessage`（1181 行），它们拿到的是同一个已被改写的对象。
2. 抛错 `Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the message.`（`packages/coding-agent/src/core/agent-session.ts:2161`）。`isStreaming` 为 `true` 时 `prompt()` 只能排队，必须说明排队方式。
3. 会话文件里 3 个 assistant 条目（2 个失败、1 个成功），2 个 `context_edit`（每次重试前给失败回复各加一个）。`agent_end` 有 3 个（一次 `prompt` + 两次 `continue`），前两个 `willRetry=true`；`agent_settled` 只有 1 个。

</details>

### 8.2 衔接

本模块从 `createAgentSession()` 读起，但没有读谁调用它、事件最后去了哪里。M9 从 `main()`（`packages/coding-agent/src/main.ts:574`）开始，读命令行参数怎么变成 `createAgentSessionRuntime()` 的参数，以及 print、json、rpc 模式怎么订阅 `AgentSessionEvent` 并输出。所有前端共享的就是本模块的 `AgentSession`。

本模块标了"→ M4"的 `buildSessionProjection`、`appendMessage`、`context_edit`，标了"→ M5"的 `compact()`，标了"→ M6"的 `_preparePromptAndToolLoadout`，标了"→ M7"的扩展事件派发，分别在对应模块从入口读起。
