# M1 pi-ai：统一的 LLM 接口

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

pi-ai（源码目录 `packages/ai`）把十几家 LLM 服务的 API 统一成一个接口：输入一组 LLM 消息（`Message[]`），输出一个事件流（`AssistantMessageEventStream`），事件流结束时给出一条完整的 `AssistantMessage`。

上层（M2 的 agent 循环）只认这个接口。它不知道请求最后发给了 Anthropic 还是 OpenAI，也不知道对方用的是 SSE 还是 WebSocket。各家的差异都在 pi-ai 内部消化：

- 请求方向：把统一的 LLM 消息翻译成各家的请求体。这里要处理消息格式、工具声明、思考级别、提示词缓存，以及"这家 API 不支持某种消息"时的降级。
- 响应方向：把各家的流式响应翻译成同一套 12 种 `AssistantMessageEvent`。
- 失败：网络错误、HTTP 错误、流被截断都不抛异常，而是作为流里最后一个 `error` 事件交给调用方。

另外，pi-ai 管理"有哪些模型、每个模型属于哪个 provider、用哪个 API 实现"。coding-agent 在它上面包了一层 `ModelRuntime`（`packages/coding-agent/src/core/model-runtime.ts`），加入了配置文件、凭证存储和扩展注册的 provider。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M1 出现在两个地方：

```
createAgentSessionServices()                              [M8]
└─ ModelRuntime.create()                                  [M1] 加载内置 provider 和模型
   …扩展加载时 pi.registerProvider() → ModelRuntime.registerProvider()  [M7 → M1]
…
streamAssistantResponse()                                 [M2]
└─ streamFn() → ModelRuntime.streamSimple()               [M1] ← 本模块入口
    ├─ normalizeContext()
    └─ lazyStream → prepareRequest()（解析凭证）
        └─ provider.streamSimple()                        provider 的 API 实现
            ├─ resolveTranscript() / transformMessages()  请求方向的转换
            ├─ HTTP 请求 → SSE
            └─ push(AssistantMessageEvent) …→ done / error
```

`streamFn` 是 M2 的 `Agent` 构造时传入的回调。coding-agent 在 `packages/coding-agent/src/core/sdk.ts:412` 里把它设成调用 `modelRuntime.streamSimple()`：

源码：[`packages/coding-agent/src/core/sdk.ts:412-423`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L412-L423)

```ts
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
```

M2 那一侧的调用点是 `packages/agent/src/agent-loop.ts:412`。M2 在调用前已经调用过一次 `normalizeContext`（`packages/agent/src/agent-loop.ts:406`），传进来的 `Context` 只有 `messages`。

### 1.3 文件清单

```
    1049 packages/coding-agent/src/core/model-runtime.ts
    1300 packages/ai/src/models.ts
     915 packages/ai/src/types.ts
     263 packages/ai/src/utils/transcript.ts
     123 packages/ai/src/api/lazy.ts
     732 packages/ai/src/providers/faux.ts
    1686 packages/ai/src/api/anthropic-messages.ts
     135 packages/ai/src/utils/event-stream.ts
     559 packages/ai/src/utils/streaming-json.ts
     256 packages/ai/src/api/transform-messages.ts
     109 packages/ai/src/api/simple-options.ts
    7127 total
```

| 文件 | 内容 | 阅读方式 |
|---|---|---|
| `coding-agent/src/core/model-runtime.ts` | `ModelRuntime`：coding-agent 的模型集合，入口 `streamSimple` | 精读 `streamSimple`、`prepareRequest`、`registerProvider`、provider 组合；凭证同步、可用性刷新、虚拟模型略过 |
| `ai/src/models.ts` | `Provider` 接口、`createProvider`、`Models` 实现、思考级别工具函数 | 精读 `Provider`、`createProvider`；`ModelsImpl` 只看 `streamSimple`，刷新和登录略过 |
| `ai/src/types.ts` | 全部公共类型 | 只读本模块用到的类型（§3） |
| `ai/src/utils/transcript.ts` | `normalizeContext`、system 消息的重放、工具清单的拆分 | 精读 |
| `ai/src/api/lazy.ts` | `lazyStream`：同步返回流，把异步准备阶段的失败变成 `error` 事件 | 精读 |
| `ai/src/providers/faux.ts` | 假 provider，本教程实验都用它 | 精读 `stream`、`streamWithDeltas`、`fauxProvider`；deferred 部分略过 |
| `ai/src/api/anthropic-messages.ts` | Anthropic Messages API 的实现 | 精读 `streamSimple`、`stream`、`buildParams`、`convertMessages` 的关键部分；OAuth、Copilot、联邦认证、strict 工具略过 |
| `ai/src/utils/event-stream.ts` | `EventStream`、`AssistantMessageEventStream` | 精读 |
| `ai/src/utils/streaming-json.ts` | 流式 JSON 解析器，`ToolCallArgumentsStream` | 只读 `ToolCallArgumentsStream`，解析器内部略过 |
| `ai/src/api/transform-messages.ts` | 跨 provider 的历史消息修正 | 精读 |
| `ai/src/api/simple-options.ts` | `streamSimple` 选项到各家选项的公共换算 | 读思考预算部分 |

其余 provider（`api/openai-*.ts`、`api/google-*.ts`、`api/bedrock-*.ts` 等）、OAuth、图片生成、分类器、模型目录生成不在本模块范围内。§4.14 只在说明"provider 差异点"时引用其他 API 实现里的一两行。

### 1.4 读完能回答的问题

1. `streamSimple()` 是同步返回的，可凭证解析是异步的。凭证缺失时调用方在哪里看到错误？
2. Anthropic 的 SSE 是按"内容块"组织的，pi 的事件是按 text/thinking/toolCall 组织的。两者怎么对应？流式的工具参数在结束前就能看到一部分吗？
3. 一段对话前半段用 OpenAI 模型、后半段换成 Claude 模型，前半段的 thinking 和工具调用 id 会怎样？
4. 系统提示词在对话中途改了，不支持中途 system 消息的 API 会收到什么？
5. 扩展用 `pi.registerProvider()` 注册的假模型，是怎么接到 `streamSimple()` 上的？

---

## 2. 先跑一次

### 2.1 主线场景里 M1 产生的事件

主线场景的模型是 `lab/faux-ext.ts` 注册的 faux provider。json 模式输出的事件里，`message_update` 的 `assistantMessageEvent` 字段就是 M1 产生的 `AssistantMessageEvent`（M2 只是在外面包了一层）：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m1-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m1-agent ./run.sh --mode json "读 README.md，总结成三句话" > /tmp/pi-lab/m1-events.jsonl
node summarize-events.mjs /tmp/pi-lab/m1-events.jsonl
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
message_update [toolcall_delta]
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
共 30 行（含第一行 session 头）
```

取出每个 `message_update` 里的 `AssistantMessageEvent`：

```bash
grep '^{"type":"message_update"' /tmp/pi-lab/m1-events.jsonl | node -e 'for (const l of require("fs").readFileSync(0,"utf8").trim().split("\n")) console.log(JSON.stringify(JSON.parse(l).assistantMessageEvent))'
```

```
{"type":"text_start","contentIndex":0}
{"type":"text_delta","contentIndex":0,"delta":"我先读一下 README。"}
{"type":"text_end","contentIndex":0,"content":"我先读一下 README。"}
{"type":"toolcall_start","contentIndex":1,"id":"call_1","toolName":"read"}
{"type":"toolcall_delta","contentIndex":1,"delta":"{\"path\":\"README.md\"}"}
{"type":"toolcall_end","contentIndex":1,"toolCall":{"type":"toolCall","id":"call_1","name":"read","arguments":{"path":"README.md"}}}
{"type":"text_start","contentIndex":0}
{"type":"text_delta","contentIndex":0,"delta":"这是一个演示项目。它只有一个 READM"}
{"type":"text_delta","contentIndex":0,"delta":"E。它用来跟踪 pi 的运行。"}
{"type":"text_end","contentIndex":0,"content":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}
```

第 1 次请求的回复有两个内容块：下标 0 是文本，下标 1 是工具调用。每个块都有 `*_start`、若干 `*_delta`、`*_end`。`*_end` 带着这个块的完整内容。faux provider 把文本按随机长度切片，所以 delta 的个数每次可能不同。

`toolcall_start` 里的 `id`、`toolName` 是 json 模式去掉 `partial` 时补上的（→ M9 `json-event.ts`），M1 的事件本身只有 `contentIndex` 和 `partial`。这里也看不到 `start` 和 `done` 两个事件：M2 把 `start` 变成了 `message_start`，把 `done` 变成了 `message_end`（`packages/agent/src/agent-loop.ts:430`、`packages/agent/src/agent-loop.ts:464`）。§2.2 的实验直接看 M1 的原始事件。

两条 assistant 消息最终的 `usage`：

```bash
grep '^{"type":"message_end","message":{"role":"assistant"' /tmp/pi-lab/m1-events.jsonl | node -e 'for (const l of require("fs").readFileSync(0,"utf8").trim().split("\n")) { const m=JSON.parse(l).message; console.log(m.stopReason, JSON.stringify(m.usage)); }'
```

```
toolUse {"input":0,"output":10,"cacheRead":0,"cacheWrite":1311,"totalTokens":1321,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}}
stop {"input":0,"output":9,"cacheRead":1311,"cacheWrite":36,"totalTokens":1356,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}}
```

faux provider 会模拟提示词缓存（§4.5）：同一个 `sessionId` 的第 2 次请求，和第 1 次相同的前缀算作 `cacheRead`，新增部分算作 `cacheWrite`。

### 2.2 去掉 agent 循环，直接调用 streamSimple

[`lab/m1-runtime.ts`](lab/m1-runtime.ts) 创建一个 `ModelRuntime`，用和 `faux-ext.ts` 相同的参数调用 `registerProvider()`，然后直接调用 `runtime.streamSimple()`。它在 provider 外面包了一层，打印 provider 实际收到的参数。faux 每个片段固定 4 个字符，所以 delta 是确定的：

```bash
PI_CODING_AGENT_DIR=/tmp/pi-lab/m1-agent node --conditions=source --experimental-strip-types m1-runtime.ts events
```

```
[runtime] getModel("faux","faux-1")：{"id":"faux-1","name":"Faux Model","api":"faux-api","provider":"faux","baseUrl":"http://localhost:0","reasoning":false,"input":["text","image"],"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0},"contextWindow":128000,"maxTokens":16384}
[调用方] Context 的键：systemPrompt, messages, tools
[调用方] streamSimple 已同步返回：AssistantMessageEventStream
[provider] 收到 TranscriptContext.messages：
  {"role":"system","content":"You are a test agent.","toolsAdded":[{"name":"read","description":"Read a file","parameters":{"type":"object","properties"…
  {"role":"user","content":"读 README.md","timestamp":1}
[provider] 收到 options 的键：apiKey, env, headers, sessionId
[provider] options.apiKey="faux-key" options.reasoning=undefined
[事件] start          partial.content=[] stopReason=pending
[事件] text_start     [0]
[事件] text_delta     [0] delta="先读文件"       partial 文本="先读文件"
[事件] text_delta     [0] delta="。"          partial 文本="先读文件。"
[事件] text_end       [0] content="先读文件。"
[事件] toolcall_start [1]
[事件] toolcall_delta [1] delta="{\"pa"      arguments={}
[事件] toolcall_delta [1] delta="th\":"      arguments={}
[事件] toolcall_delta [1] delta="\"REA"      arguments={"path":"REA"}
[事件] toolcall_delta [1] delta="DME."       arguments={"path":"README."}
[事件] toolcall_delta [1] delta="md\","      arguments={"path":"README.md"}
[事件] toolcall_delta [1] delta="\"lim"      arguments={"path":"README.md"}
[事件] toolcall_delta [1] delta="it\":"      arguments={"path":"README.md"}
[事件] toolcall_delta [1] delta="20}"        arguments={"path":"README.md","limit":20}
[事件] toolcall_end   [1] toolCall={"type":"toolCall","id":"call_1","name":"read","arguments":{"path":"README.md","limit":20}}
[事件] done           reason=toolUse
[result] {"role":"assistant","content":[{"type":"text","text":"先读文件。"},{"type":"toolCall","id":"call_1","name":"read","arguments":{"path":"README.md","limit":20}}],"api":"faux-api","provider":"faux","model":"faux-1","usage":{"input":0,"output":11,"cacheRead":0,"cacheWrite":42,"totalTokens":53,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"toolUse","timestamp":"<ts>","durationMs":"<ms>"}
```

几处要注意：

- 调用方传入的 `Context` 有 `systemPrompt`、`messages`、`tools` 三个字段，provider 收到的只有 `messages`，系统提示词和工具清单变成了开头一条 `system` 消息（§4.2 `normalizeContext`）。
- `streamSimple` 同步返回了流，`[provider]` 那几行在它之后才打印。真正的请求是在异步的准备阶段之后才发出的（§4.3 `lazyStream`）。
- provider 收到的 `options` 多了 `apiKey`、`headers`、`env`。`apiKey` 的值 `"faux-key"` 来自 `registerProvider` 时的配置（§4.3 `prepareRequest`）。
- `toolcall_delta` 每来一片，`partial` 里工具调用的 `arguments` 就是"到目前为止能解析出的对象"：`{}` → `{"path":"REA"}` → … → 完整对象（§4.9 `ToolCallArgumentsStream`）。

---

## 3. 数据结构

### 3.1 LLM 消息：Message

README §3.1 三层消息的最下面一层。四种 role：

源码：[`packages/ai/src/types.ts:551-583`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L551-L583)

```ts
/**
 * System instructions and tool declarations at one point in the transcript.
 *
 * The leading system message is the system prompt. Later system messages change it:
 * `content` adds instructions from that point on, `sections` replace or remove named
 * prompt sections, and `toolsAdded`/`toolsRemoved` change the tool set. Replaying
 * every system message in order yields the current prompt and tools. Providers that
 * accept system messages mid-conversation send each one in place; other providers
 * rebuild the leading system message from the replayed state.
 */
export interface SystemMessage {
	role: "system";
	/** Instruction text. On the leading message this is the base prompt; later, additional instructions. */
	content: string | TextContent[];
	/**
	 * Named, ordered prompt sections rendered verbatim after `content`. The leading message
	 * declares them; later messages replace sections by name, and `null` removes one. Keep
	 * each section self-delimiting (a tag, a heading) so the model can relate an update to
	 * the original. Avoid integer-like names; JSON objects reorder those.
	 */
	sections?: Record<string, string | null>;
	/** Complete definitions of tools that become available at this point. */
	toolsAdded?: Tool[];
	/** Tools that stop being available at this point. */
	toolsRemoved?: ToolReference[];
	timestamp: number; // Unix timestamp in milliseconds
}

export interface UserMessage {
	role: "user";
	content: string | (TextContent | ImageContent)[];
	timestamp: number; // Unix timestamp in milliseconds
}
```

源码：[`packages/ai/src/types.ts:585-616`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L585-L616)

```ts
export interface AssistantMessage {
	role: "assistant";
	content: (TextContent | ThinkingContent | ToolCall)[];
	api: Api;
	provider: ProviderId;
	model: string;
	responseModel?: string; // Concrete model reported by the provider, when reported. May equal the requested `model`
	responseId?: string; // Provider-specific response/message identifier when the upstream API exposes one
	/** Exact provider-native effort level used for this response. Absent for legacy or unmanaged responses. */
	providerThinkingLevel?: string;
	/** Pi thinking level the agent loop requested for this response. Absent outside the agent loop and for legacy responses. */
	thinkingLevel?: ModelThinkingLevel;
	diagnostics?: AssistantMessageDiagnostic[]; // Redacted provider/runtime diagnostics for failures and recoveries.
	usage: Usage;
	stopReason: StopReason;
	deferred?: DeferredHandle;
	errorMessage?: string;
	rawStopReason?: string;
	/**
	 * Provider indication of whether the model explicitly ended its turn.
	 * Preserved for debugging and does not currently affect agent control flow.
	 */
	endTurn?: boolean;
	/** Unix timestamp in milliseconds when the request started. */
	timestamp: number;
	/**
	 * Milliseconds from `timestamp` until the response ended, measured with a monotonic clock. Set by
	 * `AssistantMessageEventStream` on the final message of a response it saw start; absent for legacy messages and
	 * for deferred results fetched later.
	 */
	durationMs?: number;
}
```

源码：[`packages/ai/src/types.ts:640-659`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L640-L659)

```ts
export type ToolResultMessage<TDetails = JsonValue> = IsJsonCompatible<TDetails> extends true
	? {
			role: "toolResult";
			toolCallId: string;
			toolName: string;
			content: (TextContent | ImageContent)[]; // Supports text and images
			details?: JsonRepresentation<TDetails>;
			/** Usage from the tool execution itself, if available. Not part of main LLM context accounting. */
			usage?: Usage;
			/** Calls this tool made to other tools. Kept for the session record; not sent to the model. */
			nestedCalls?: NestedToolCalls;
			isError: boolean;
			/** Unix timestamp in milliseconds when the result was created. */
			timestamp: number;
			/** Milliseconds the tool's execution took, measured with a monotonic clock. Absent for legacy results. */
			durationMs?: number;
		}
	: never;

export type Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage;
```

`SystemMessage` 不只是"系统提示词"。它可以出现在对话中间，携带四类变化：

| 字段 | 在开头一条 system 消息里 | 在后面的 system 消息里 |
|---|---|---|
| `content` | 基础提示词 | 追加的指令 |
| `sections` | 声明有名字的段落 | 按名字替换段落，`null` 表示删除 |
| `toolsAdded` | 初始工具清单 | 从这里开始可用的工具 |
| `toolsRemoved` | 不用 | 从这里开始不可用的工具 |

按顺序重放所有 system 消息，就得到当前的提示词和工具。支持中途 system 消息的 API 原样发送，其余 API 由 pi-ai 重放成开头一条（§4.14）。

`AssistantMessage` 是模型的回复。M2 读它的 `content`、`stopReason`、`errorMessage`；M4 把它整条写进会话文件。它的 `content` 有三种块：

源码：[`packages/ai/src/types.ts:422-457`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L422-L457)

```ts
export interface TextContent {
	type: "text";
	text: string;
	textSignature?: string; // e.g., for OpenAI responses, message metadata (legacy id string or TextSignatureV1 JSON)
}

export interface ThinkingContent {
	type: "thinking";
	thinking: string;
	thinkingSignature?: string; // Provider-specific opaque or serialized reasoning replay data
	/** When true, the thinking content was redacted by safety filters. The opaque
	 *  encrypted payload is stored in `thinkingSignature` so it can be passed back
	 *  to the API for multi-turn continuity. */
	redacted?: boolean;
}

export interface ImageContent {
	type: "image";
	data: string; // base64 encoded image data
	mimeType: string; // e.g., "image/jpeg", "image/png"
}

export interface ToolCall {
	type: "toolCall";
	id: string;
	name: string;
	arguments: JsonObject;
	thoughtSignature?: string; // Google-specific: opaque signature for reusing thought context
	/** OpenAI Responses namespace for calls to dynamically loaded or namespaced tools. */
	namespace?: string;
	/**
	 * Set when the model's complete arguments text was not a valid JSON object. `arguments` is then `{}`.
	 * Describes the problem and includes the text the model sent; tool argument validation fails with it.
	 */
	argumentsError?: string;
}
```

| 块 | 关键字段 | 说明 |
|---|---|---|
| `TextContent` | `text`、`textSignature` | 签名是 OpenAI Responses 用来回放消息的元数据 |
| `ThinkingContent` | `thinking`、`thinkingSignature`、`redacted` | 签名是 provider 的加密数据，只对产生它的模型有效（§4.13） |
| `ToolCall` | `id`、`name`、`arguments`、`argumentsError` | `arguments` 是解析好的对象。模型给的参数不是合法 JSON 时 `arguments` 为 `{}`，错误写在 `argumentsError` |

`usage` 和 `stopReason`：

源码：[`packages/ai/src/types.ts:459-489`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L459-L489)

```ts
export interface Usage {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	/** Subset of `cacheWrite` written with 1h retention. Only Anthropic reports this split. */
	cacheWrite1h?: number;
	/**
	 * Reasoning/thinking tokens, when the provider reports them. This is a subset of
	 * `output`: `output` already includes these tokens. Set to a number (possibly 0) by
	 * providers that expose a reasoning breakdown; left undefined by providers that don't.
	 */
	reasoning?: number;
	totalTokens: number;
	cost: {
		/** Catalog-derived input cost. */
		input: number;
		/** Catalog-derived output cost. */
		output: number;
		/** Catalog-derived cache-read cost. */
		cacheRead: number;
		/** Catalog-derived cache-write cost. */
		cacheWrite: number;
		/** Best available total: provider-reported billed cost when supported, otherwise the catalog estimate. */
		total: number;
		/** True when `total` is the billed cost reported by the provider. False when it is a catalog estimate; absent also means false. */
		totalFromProvider?: boolean;
	};
}

export type StopReason = "pending" | "stop" | "length" | "toolUse" | "error" | "aborted" | "deferred";
```

`stopReason` 的取值决定 M2 循环的走向：`toolUse` 执行工具再请求，`stop` 结束，`length` 让工具调用失败（M2 §4.7），`error`/`aborted` 退出循环。`pending` 只在流式过程中出现在 `partial` 上。

**真实实例。** §2.2 的 `[result]` 行就是一条完整的 `AssistantMessage`；§2.2 的 `[provider]` 行是 `SystemMessage` 和 `UserMessage`。`ToolResultMessage` 的实例见 M2 §2.2。

### 3.2 Context 和 TranscriptContext：请求的输入

源码：[`packages/ai/src/types.ts:780-803`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L780-L803)

```ts
/**
 * Request input accepted by the public stream entry points (`Models.stream()`,
 * `streamSimple()`, ...). `systemPrompt` and `tools` are shorthand for a leading
 * system message; `normalizeContext()` folds them into one before the request
 * reaches a provider.
 */
export interface Context {
	systemPrompt?: string;
	messages: Message[];
	tools?: Tool[];
}

declare const transcriptContextBrand: unique symbol;

/**
 * Normalized request context passed to providers and API implementations. The
 * prompt and tool declarations are carried by the transcript's system messages.
 * Only `normalizeContext()` produces this type, so a raw `Context` cannot reach
 * provider code by accident.
 */
export type TranscriptContext = {
	messages: Message[];
	readonly [transcriptContextBrand]: true;
};
```

两个类型的区别：

| 类型 | 谁使用 | 系统提示词和工具在哪里 |
|---|---|---|
| `Context` | 调用方（`streamSimple` 的参数） | 可以写在 `systemPrompt`、`tools` 字段，也可以写在 `messages` 里的 system 消息 |
| `TranscriptContext` | provider（`provider.streamSimple` 的参数） | 只在 `messages` 里的 system 消息 |

`TranscriptContext` 带一个只在类型层面存在的标记字段 `[transcriptContextBrand]`。普通对象字面量没有这个字段，所以把 `Context` 直接传给 provider 会编译失败，只有 `normalizeContext()`（§4.2）能产生它。

**真实实例。** §2.2 里，`[调用方] Context 的键：systemPrompt, messages, tools` 是 `Context`；`[provider] 收到 TranscriptContext.messages` 下面两行是对应的 `TranscriptContext`。

### 3.3 AssistantMessageEvent：12 种流式事件

源码：[`packages/ai/src/types.ts:805-837`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L805-L837)

```ts
/**
 * Event protocol for AssistantMessageEventStream.
 *
 * Successful streams emit `start` before partial updates and terminate with
 * `done`. A stream may terminate directly with `error` when request setup fails
 * before generation starts; after `start`, failures also terminate with `error`.
 * Direct `streamSimple()` calls throw synchronously when request auth is missing.
 * Updates and `done` must never appear before `start`.
 *
 * `partial` is the shared live response-so-far helper, not an event-time
 * snapshot. Text and thinking blocks are empty when their `*_start` event is
 * emitted and grow only through their corresponding `*_delta` events until the
 * authoritative `*_end`. Redacted thinking may be complete at start and emit no
 * deltas. Tool-call arguments at `toolcall_start` are provider-specific;
 * `toolcall_delta` carries subsequent JSON updates.
 */
export type AssistantMessageEvent =
	| { type: "start"; partial: AssistantMessage }
	| { type: "text_start"; contentIndex: number; partial: AssistantMessage }
	| { type: "text_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
	| { type: "text_end"; contentIndex: number; content: string; partial: AssistantMessage }
	| { type: "thinking_start"; contentIndex: number; partial: AssistantMessage }
	| { type: "thinking_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
	| { type: "thinking_end"; contentIndex: number; content: string; partial: AssistantMessage }
	| { type: "toolcall_start"; contentIndex: number; partial: AssistantMessage }
	| { type: "toolcall_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
	| { type: "toolcall_end"; contentIndex: number; toolCall: ToolCall; partial: AssistantMessage }
	| {
			type: "done";
			reason: Extract<StopReason, "stop" | "length" | "toolUse" | "deferred">;
			message: AssistantMessage;
	  }
	| { type: "error"; reason: Extract<StopReason, "aborted" | "error">; error: AssistantMessage };
```

| 事件 | 字段 | 说明 |
|---|---|---|
| `start` | `partial` | 开始。之后的事件都在它后面 |
| `text_start` / `thinking_start` / `toolcall_start` | `contentIndex` | `partial.content[contentIndex]` 新增一个块 |
| `text_delta` / `thinking_delta` / `toolcall_delta` | `contentIndex`、`delta` | 这个块新增的文本。工具调用的 `delta` 是一段原始 JSON 文本 |
| `text_end` / `thinking_end` / `toolcall_end` | `content` 或 `toolCall` | 这个块的最终内容 |
| `done` | `reason`、`message` | 成功结束，`reason` 是 `stop`/`length`/`toolUse`/`deferred` |
| `error` | `reason`、`error` | 失败结束，`reason` 是 `error`/`aborted`；`error` 也是一条 `AssistantMessage` |

注释里有两条约定，后面读代码时会反复看到：

- 失败也走流。准备阶段失败时，流可以只有一个 `error` 事件，没有 `start`。
- `partial` 是"同一个正在增长的对象"，不是事件发生时的快照。消费者如果落后于生产者，读到的 `partial` 可能已经包含后面的内容。§7.1 的实验演示这一点。

**真实实例。** §2.2 的 `[事件]` 行。

### 3.4 Model 和 Provider：模型目录

`Model` 描述一个模型：

源码：[`packages/ai/src/types.ts:853-887`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L853-L887)

```ts
/** Chat model: usable with `stream()` and friends. */
export interface Model<TApi extends Api> extends BaseModel<TApi> {
	/**
	 * Optional: chat is the default model type, so models without `type` are chat
	 * models. Narrow mixed model lists with `isModelType()` instead of comparing
	 * `type` directly.
	 */
	type?: "chat";
	reasoning: boolean;
	/**
	 * Maps pi thinking levels to provider/model-specific values.
	 * Missing keys use provider defaults. null marks a level as unsupported.
	 */
	thinkingLevelMap?: ThinkingLevelMap;
	/** Prompt cache lifetimes per retention tier. Unset when the provider's cache behavior is unknown. */
	promptCache?: ModelPromptCache;
	contextWindow: number;
	maxTokens: number;
	/** Default sampling parameters for this model. See {@link StreamOptions.samplingParams}; per-request keys override these. */
	samplingParams?: SamplingParams;
	/** Sampling parameter overrides selected by the effective pi thinking level. */
	samplingParamsByThinkingLevel?: SamplingParamsByThinkingLevel;
	/** Compatibility overrides for OpenAI-compatible APIs. If not set, auto-detected from baseUrl. */
	compat?: TApi extends "openai-completions"
		? OpenAICompletionsCompat
		: TApi extends "openai-responses" | "azure-openai-responses" | "openai-codex-responses"
			? OpenAIResponsesCompat
			: TApi extends "anthropic-messages"
				? AnthropicMessagesCompat
				: TApi extends "bedrock-converse-stream"
					? BedrockCompat
					: TApi extends "mistral-conversations"
						? MistralConversationsCompat
						: never;
}
```

本模块会用到的字段：

| 字段 | 用途 |
|---|---|
| `provider` | 归属的 provider id，`ModelRuntime` 按它找 provider |
| `api` | 用哪个 API 实现，一个 provider 可以有多种 API 的模型 |
| `reasoning`、`thinkingLevelMap` | 能否思考，思考级别怎么映射到 provider 的取值（§4.14） |
| `promptCache` | 提示词缓存在 short/long 两档下的寿命（秒） |
| `contextWindow`、`maxTokens` | 上下文窗口和输出上限，用于裁剪 `max_tokens` |
| `compat` | 按 API 分类的兼容开关，例如 Anthropic 的 `supportsMidConvoSystemMessages` |

`Provider` 是一个 provider 的全部能力：

源码：[`packages/ai/src/models.ts:158-180`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/models.ts#L158-L180)

```ts
export interface Provider<TApi extends Api = Api> {
	readonly id: string;
	readonly name: string;

	readonly baseUrl?: string;
	readonly headers?: ProviderHeaders;

	/**
	 * Required: at least one of `apiKey`/`oauth`. Every provider has auth
	 * semantics — even providers with only ambient credentials (env vars, AWS
	 * profiles, ADC files) and keyless local servers provide `apiKey` auth
	 * whose `resolve()` reports whether the provider is configured.
	 * `Models.getAuth()` returns undefined when the provider is unconfigured.
	 */
	readonly auth: ProviderAuth;

	/**
	 * Current known chat models, sync. Static providers return their catalog;
	 * dynamic providers return the list as of the last `refreshModels()` (empty
	 * before the first). Must not throw; `Models` treats a throwing
	 * implementation as having no models.
	 */
	getModels(): readonly Model<TApi>[];
```

后面的 `stream`、`streamSimple` 两个方法（`packages/ai/src/models.ts:216`、`packages/ai/src/models.ts:222`）接收 `TranscriptContext`，返回 `AssistantMessageEventStream`。`auth` 字段说明怎样取得凭证；`getModels()` 同步返回模型列表。

**真实实例。** §2.2 第一行 `getModel("faux","faux-1")` 是 faux 的 `Model`。Anthropic 内置模型的 `compat` 见 §4.14 的实验输出。

### 3.5 AssistantMessageEventStream：事件流

它是一个异步可迭代对象，再加一个 `result()` 方法。定义在 §4.6 精读，这里只列接口：

| 方法 | 谁调用 | 作用 |
|---|---|---|
| `push(event)` | provider | 送出一个事件。`done`/`error` 之后再 push 会被忽略 |
| `end(result?)` | provider | 关闭流 |
| `for await (const e of stream)` | 消费者（M2） | 按顺序取事件 |
| `result()` | 消费者 | 一个 Promise，在 `done`/`error` 事件被 push 时 resolve 成最终消息 |

### 3.6 类型关系图

```
 调用方（M2 streamFn）
   Context { systemPrompt?, messages: Message[], tools? }
        │ normalizeContext()
        ▼
   TranscriptContext { messages: Message[] }   ← system 消息携带提示词和工具
        │ ModelRuntime.streamSimple(model: Model, …)
        │   model.provider ──查表──▶ Provider
        ▼
   Provider.streamSimple(model, TranscriptContext, SimpleStreamOptions)
        │ 按 model.api 选 API 实现（faux / anthropic-messages / …）
        │ 请求方向：resolveTranscript / transformMessages → 各家请求体
        │ 响应方向：各家流式响应 → AssistantMessageEvent
        ▼
   AssistantMessageEventStream ──for await──▶ AssistantMessageEvent × N
        │                                       start, *_start/_delta/_end, done|error
        └──result()──▶ AssistantMessage { content: (Text|Thinking|ToolCall)[], usage, stopReason }
```

---
## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。

```
ModelRuntime.streamSimple()                         §4.1
├─ normalizeContext()                               §4.2
└─ lazyStream(setup)                                §4.3
    ├─ prepareRequest()  解析凭证、合并请求头          §4.3
    └─ provider.streamSimple()
        ├─ 扩展注册的 provider：composeModelProvider → streamWith   §4.4
        ├─ 内置 provider：createProvider → dispatch → lazyApi      §4.4
        │
        ├─ faux：stream() → streamWithDeltas()      §4.5
        └─ anthropic-messages：
            ├─ streamSimple()  思考级别 → 选项        §4.7
            └─ stream()                             §4.8
                ├─ resolveTranscript()              §4.14
                ├─ buildParams() → transformMessages() / convertMessages()  §4.12 §4.13
                ├─ iterateAnthropicEvents()  SSE 解码 §4.8
                ├─ 内容块 → 统一事件                  §4.9
                │   └─ ToolCallArgumentsStream      §4.9
                ├─ message_delta → stopReason/usage §4.10
                └─ try/catch → error 事件            §4.11
        每个 push → AssistantMessageEventStream     §4.6
provider 差异点                                      §4.14
模型注册：ModelRuntime.create() / registerProvider()  §4.15
```

### 4.1 ModelRuntime.streamSimple()

**调用位置**：`streamAssistantResponse()`（M2）→ `streamFn` → `modelRuntime.streamSimple(model, context, requestOptions)`（`packages/coding-agent/src/core/sdk.ts:422`）。

源码：[`packages/coding-agent/src/core/model-runtime.ts:731-757`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/model-runtime.ts#L731-L757)

```ts
	streamSimple(model: Model<Api>, context: Context, options?: ModelsSimpleStreamOptions): AssistantMessageEventStream {
		const transcript = normalizeContext(context);
		if (isVirtualModel(model)) {
			// Requests outside the agent loop are routed here. Callers sized them before routing, so
			// cap the output budget to the routed model.
			return lazyStream(model, async () => {
				const route = await this.resolveModel(model, transcript.messages, {
					reason: "direct",
					thinkingLevel: options?.reasoning ?? "off",
					signal: options?.signal,
				});
				const { maxTokens: limit } = route.model;
				const maxTokens = options?.maxTokens && limit > 0 ? Math.min(options.maxTokens, limit) : options?.maxTokens;
				const reasoning = route.thinkingLevel === "off" ? undefined : route.thinkingLevel;
				// Caller credentials were resolved for the virtual model's provider. Another provider
				// resolves its own, so they are not sent to the wrong vendor.
				const { apiKey, headers, env, ...rest } = options ?? {};
				const auth = route.model.provider === model.provider ? { apiKey, headers, env } : {};
				return this.streamSimple(route.model, context, { ...rest, ...auth, maxTokens, reasoning });
			});
		}
		return lazyStream(model, async () => {
			assertChatModel(model);
			const prepared = await this.prepareRequest(model, options);
			return prepared.provider.streamSimple(prepared.model, transcript, prepared.options as SimpleStreamOptions);
		});
	}
```

**讲解**

函数分两支。`isVirtualModel(model)` 为真时，模型是一个"虚拟模型"：它本身没有 API 实现，每次请求前由一个路由函数选出真正的模型，再递归调用 `streamSimple`。主线场景不走这一支，本模块不展开。

普通模型走最后一个 `return`。它做三件事：

1. 先同步调用 `normalizeContext`，得到 `TranscriptContext`。
2. 调用 `lazyStream`，同步返回一个 `AssistantMessageEventStream`。
3. 交给 `lazyStream` 的异步函数在后台运行：检查是聊天模型（`assertChatModel`），`prepareRequest` 解析凭证，最后调用 `provider.streamSimple`。

入参和出参：

| | 形状 |
|---|---|
| `model` | `Model<Api>`，主线场景是 `{ provider: "faux", api: "faux-api", id: "faux-1", … }` |
| `context` | `Context`，M2 传入时只有 `messages` |
| `options` | `ModelsSimpleStreamOptions`：`SimpleStreamOptions`（`reasoning`、`sessionId`、`maxTokens`、`signal` 等）加一个 `transformHeaders` 回调 |
| 返回 | `AssistantMessageEventStream`，同步返回 |

pi-ai 自己的 `Models` 实现（`packages/ai/src/models.ts:929`）几乎一样：`normalizeContext` → `lazyStream` → `applyAuth` → `provider.streamSimple`。`ModelRuntime` 实现了同一个 `Models` 接口，所以用 pi-ai 的 SDK 用户和 coding-agent 走的是同一条路径。

**往哪走**：§4.2 `normalizeContext`，然后 §4.3 `lazyStream`。

### 4.2 normalizeContext()

**调用位置**：`ModelRuntime.streamSimple()` 第一行。M2 的 `streamAssistantResponse()` 在调用 `streamFn` 之前也调用过一次（`packages/agent/src/agent-loop.ts:406`）。

源码：[`packages/ai/src/utils/transcript.ts:6-34`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/transcript.ts#L6-L34)

```ts
/**
 * Build the leading system message for a prompt and tool set. Returns undefined when
 * both are empty, so an empty transcript stays empty.
 */
export function createInitialSystemMessage(
	systemPrompt: string | undefined,
	tools: Tool[] | undefined,
): SystemMessage | undefined {
	const hasSystemPrompt = systemPrompt !== undefined && systemPrompt.length > 0;
	const hasTools = tools !== undefined && tools.length > 0;
	if (!hasSystemPrompt && !hasTools) return undefined;
	return {
		role: "system",
		content: systemPrompt ?? "",
		...(hasTools ? { toolsAdded: tools } : {}),
		timestamp: 0,
	};
}

/**
 * Fold `Context.systemPrompt` and `Context.tools` into a leading system message.
 * This is the only entry point that produces a {@link TranscriptContext}; every
 * provider-facing function expects the result.
 */
export function normalizeContext(context: Context): TranscriptContext {
	const initialMessage = createInitialSystemMessage(context.systemPrompt, context.tools);
	const messages = initialMessage ? [initialMessage, ...context.messages] : context.messages;
	return { messages } as TranscriptContext;
}
```

**讲解**

`normalizeContext` 把 `Context.systemPrompt` 和 `Context.tools` 折叠成一条 `timestamp: 0` 的 system 消息，放在 `messages` 最前面。两者都为空时不加消息，空对话保持为空。

它对已经规范化的输入是无害的：M2 传进来的 `Context` 只有 `messages`，`createInitialSystemMessage` 返回 `undefined`，`messages` 原样返回。所以两次调用不会产生两条 system 消息。

注释说"每个面向 provider 的函数都期待这个结果"。这条规则让所有 provider 只需要处理一种输入形状：系统提示词和工具都在 system 消息里。下游需要"当前工具清单"时，调用 `getCurrentTools()` 重放所有 system 消息：

源码：[`packages/ai/src/utils/transcript.ts:57-66`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/transcript.ts#L57-L66)

```ts
/** Resolve the tools available after applying every transcript delta in order. */
export function getCurrentTools(messages: TranscriptMessages): Tool[] {
	const tools = new Map<string, Tool>();
	for (const message of messages) {
		if (!isSystemMessage(message)) continue;
		for (const tool of message.toolsRemoved ?? []) tools.delete(tool.name);
		for (const tool of message.toolsAdded ?? []) tools.set(tool.name, tool);
	}
	return [...tools.values()];
}
```

先删后加：同一条 system 消息里同时出现 `toolsRemoved` 和 `toolsAdded` 同名工具时，表示"重新定义"。

**往哪走**：§4.3 `lazyStream`。

### 4.3 lazyStream() 和 prepareRequest()

**调用位置**：`ModelRuntime.streamSimple()` → `lazyStream(model, setup)`。`setup` 里调用 `this.prepareRequest(model, options)`。

源码：[`packages/ai/src/api/lazy.ts:37-86`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/lazy.ts#L37-L86)

```ts
async function forwardStream(
	target: AssistantMessageEventStream,
	source: AsyncIterable<AssistantMessageEvent>,
	observer: LazyStreamObserver | undefined,
	observeResult: (result: AssistantMessage | undefined) => void,
): Promise<void> {
	let result: AssistantMessage | undefined;
	for await (const event of source) {
		target.push(event);
		observer?.onEvent?.(event);
		if (event.type === "done") result = event.message;
		else if (event.type === "error") result = event.error;
	}
	if (hasResult(source)) result = await source.result();
	target.end(result);
	observeResult(result);
}

/**
 * Returns a stream synchronously while running async setup (auth resolution,
 * lazy module loading) behind it. Setup failures terminate the stream with an
 * error event.
 */
export function lazyStream(
	model: Model<Api>,
	setup: () => Promise<AsyncIterable<AssistantMessageEvent>>,
	observer?: LazyStreamObserver,
): AssistantMessageEventStream {
	const startedAt = Date.now();
	const outer = new AssistantMessageEventStream();
	let observedResult = false;
	const observeResult = (result: AssistantMessage | undefined): void => {
		if (observedResult) return;
		observedResult = true;
		observer?.onResult?.(result);
	};

	setup()
		.then((inner) => forwardStream(outer, inner, observer, observeResult))
		.catch((error) => {
			const message = createSetupErrorMessage(model, error, startedAt);
			const event: AssistantMessageEvent = { type: "error", reason: "error", error: message };
			outer.push(event);
			observer?.onEvent?.(event);
			outer.end(message);
			observeResult(message);
		});

	return outer;
}
```

**讲解**

`lazyStream` 解决一个接口问题：`streamSimple` 要同步返回流（这是 pi-ai `Models` 和 `ProviderStreams` 接口的签名，见 `packages/ai/src/models.ts:336`、`packages/ai/src/types.ts:319`；M2 的 `StreamFn` 其实允许返回 Promise，pi-ai 的公共接口却是同步的），但发请求前有异步的准备工作（读凭证文件、OAuth 刷新 token、动态 import API 模块）。

做法是先创建一个外层流 `outer` 并立即返回，再在后台运行 `setup()`：

- `setup()` 成功，得到内层流 `inner`（provider 返回的流）。`forwardStream` 把 `inner` 的每个事件原样 push 到 `outer`，最后 `outer.end(result)`。
- `setup()` 抛异常（凭证缺失、provider 不存在、模块加载失败），`catch` 构造一条 `stopReason: "error"`、`content: []` 的 assistant 消息，push 一个 `error` 事件，然后关闭 `outer`。

所以调用方永远拿到一个流，失败也在流里。这就是 `types.ts` 注释说的"准备阶段失败时，流可以只有一个 `error` 事件"。`lab/m1-runtime.ts unknown` 场景把模型的 `provider` 改成不存在的 `"nope"`：

```bash
PI_CODING_AGENT_DIR=/tmp/pi-lab/m1-agent node --conditions=source --experimental-strip-types m1-runtime.ts unknown
```

```
[调用方] streamSimple 没有抛异常，返回了一个流
[事件] error          reason=error errorMessage="Unknown provider: nope"
[result] stopReason=error errorMessage="Unknown provider: nope" content=[]
```

错误来自 `prepareRequest` 的第一行检查。`prepareRequest` 的全文：

源码：[`packages/coding-agent/src/core/model-runtime.ts:659-705`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/model-runtime.ts#L659-L705)

```ts
	private async prepareRequest<
		TModel extends AnyModel,
		TOptions extends ProviderRequestOptions<TModel> & ModelsRequestTransforms,
	>(
		model: TModel,
		options: TOptions | undefined,
	): Promise<{
		provider: Provider;
		model: TModel;
		options: Omit<TOptions, "transformHeaders"> & ProviderRequestOptions<TModel>;
	}> {
		const provider = this.models.getProvider(model.provider);
		if (!provider) throw new ModelsError("provider", `Unknown provider: ${model.provider}`);
		const resolution = await this.getAuth(model, {
			apiKey: options?.apiKey,
			env: options?.env,
			signal: options?.signal,
		});
		if (!resolution) throw new ModelsError("auth", `Provider is not configured: ${model.provider}`);

		const { transformHeaders, ...rawProviderOptions } = options ?? {};
		const providerOptions = rawProviderOptions as Omit<TOptions, "transformHeaders"> & ProviderRequestOptions<TModel>;
		const requestModel: TModel = resolution.auth.baseUrl ? { ...model, baseUrl: resolution.auth.baseUrl } : model;
		const sessionId =
			"sessionId" in providerOptions && typeof providerOptions.sessionId === "string"
				? providerOptions.sessionId
				: undefined;
		let headers = mergeHeaders(
			mergeHeaders(getOpenCodeSessionHeaders(requestModel, sessionId), resolution.auth.headers),
			providerOptions.headers,
		);
		if (transformHeaders) headers = await transformHeaders(headers ?? {});
		const env =
			resolution.env || providerOptions.env
				? { ...(resolution.env ?? {}), ...(providerOptions.env ?? {}) }
				: undefined;
		return {
			provider,
			model: requestModel,
			options: {
				...providerOptions,
				apiKey: providerOptions.apiKey ?? resolution.auth.apiKey,
				headers,
				env,
			} as Omit<TOptions, "transformHeaders"> & ProviderRequestOptions<TModel>,
		};
	}
```

它按顺序做了这些事：

1. 按 `model.provider` 查 provider，查不到抛 `ModelsError("provider", …)`。
2. `getAuth(model, …)` 解析凭证。来源依次可能是：调用方传入的 `apiKey`、运行时设置的 key、`auth.json` 里存的凭证、环境变量、`models.json` 配置、扩展注册时给的 `apiKey`。返回 `undefined` 表示这个 provider 没有配置，抛 `ModelsError("auth", …)`。凭证解析的细节属于 OAuth 和配置，本模块不展开。
3. 凭证里带 `baseUrl` 时，复制一份 model 并替换 `baseUrl`。
4. 合并请求头：OpenCode 的会话头 < 凭证里的头 < 调用方的头，再交给 `transformHeaders` 改写。
5. 返回 provider、请求用的 model、补上 `apiKey`/`headers`/`env` 的选项。

§2.2 里 provider 收到的 `options` 有 `apiKey, env, headers, sessionId`，就是这一步的结果。`apiKey` 是 `"faux-key"`，来自 `registerProvider` 的配置。

**往哪走**：§4.4，`provider.streamSimple` 是哪个函数。

### 4.4 provider.streamSimple() 指向哪里

**调用位置**：`lazyStream` 的 `setup` 里，`prepared.provider.streamSimple(prepared.model, transcript, prepared.options)`。

`prepared.provider` 来自 `this.models.getProvider(model.provider)`。`ModelRuntime` 往 `this.models` 里放的 provider 是"组合"出来的：

源码：[`packages/coding-agent/src/core/model-runtime.ts:298-332`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/model-runtime.ts#L298-L332)

```ts
	/** Returns the provider without virtual models, or undefined when only virtual models define it. */
	private recomposeProvider(providerId: string): Provider | undefined {
		const provider = this.composeProvider(providerId);
		const virtualModels = [...(this.virtualModels.get(providerId)?.values() ?? [])].map((entry) => entry.model);
		if (virtualModels.length > 0) this.models.setProvider(withVirtualModels(providerId, provider, virtualModels));
		else if (provider) this.models.setProvider(provider);
		else this.models.deleteProvider(providerId);
		return provider;
	}

	/** The provider without virtual models, or undefined when nothing defines it. */
	private composeProvider(providerId: string): Provider | undefined {
		const base = this.nativeExtensionProviders.get(providerId) ?? this.builtins.get(providerId);
		const extension = this.extensionProviders.get(providerId);
		if (!this.config.getProvider(providerId) && !extension) {
			// No overlays: use the builtin untouched so its auth/login/stream behavior is exact.
			this.compositionErrors.delete(providerId);
			return base;
		}
		try {
			const provider = composeModelProvider(providerId, base, this.config, extension);
			this.compositionErrors.delete(providerId);
			return provider;
		} catch (error) {
			this.compositionErrors.set(providerId, error instanceof Error ? error.message : String(error));
			return base;
		}
	}

	private rebuildProviders(): void {
		this.models.clearProviders();
		this.compositionErrors.clear();
		for (const providerId of this.providerIds()) this.recomposeProvider(providerId);
		this.updateModelSnapshot();
	}
```

一个 provider id 可能有四层来源：内置 provider（`builtins`）、扩展注册的原生 provider（`nativeExtensionProviders`）、`models.json` 里的配置、扩展用 `registerProvider` 注册的配置（`extensionProviders`）。没有配置层也没有扩展层时，直接用内置 provider，不做包装。否则调用 `composeModelProvider` 把几层合起来。

主线场景的 faux 来自扩展的 `pi.registerProvider()`，会走 `composeModelProvider`。它的 `streamSimple` 是：

源码：[`packages/coding-agent/src/core/provider-composer.ts:587-608`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/provider-composer.ts#L587-L608)

```ts
	const supportsBaseApi = (model: Model<Api>) => base?.getModels().some((entry) => entry.api === model.api) ?? false;
	const streamWith = (
		model: Model<Api>,
		context: TranscriptContext,
		options: StreamOptions | undefined,
		simple: boolean,
	): AssistantMessageEventStream =>
		lazyStream(model, async () => {
			if (extension?.streamSimple && model.api === extension.api) {
				return extension.streamSimple(model, context, options as SimpleStreamOptions);
			}
			if (base && supportsBaseApi(model)) {
				return simple
					? base.streamSimple(model, context, options as SimpleStreamOptions)
					: base.stream(model, context, options);
			}
			const api = getApiProvider(model.api);
			if (!api) throw new Error(`No API provider registered for api: ${model.api}`);
			return simple
				? api.streamSimple(model, context, options as SimpleStreamOptions)
				: api.stream(model, context, options);
		});
```

选择 API 实现的顺序：

1. 扩展注册时给了 `streamSimple`，并且模型的 `api` 等于扩展声明的 `api`：用扩展的函数。faux 走这里，最终调用 `faux.provider.streamSimple`。
2. 内置 provider 有这个 `api` 的模型：用内置 provider 的实现。
3. 都没有：按 `model.api` 在全局 API 注册表里查（`getApiProvider`）。这让 `models.json` 可以定义"用 `openai-completions` API 的自定义 provider"。

内置 provider 由 `createProvider` 构造。Anthropic 的工厂函数（`packages/ai/src/providers/anthropic.ts:74`）传入 `api: anthropicMessagesApi()`，即 `lazyApi(() => import("./anthropic-messages.ts"))`（`packages/ai/src/api/anthropic-messages.lazy.ts:4`）。`createProvider` 的分发部分：

源码：[`packages/ai/src/models.ts:1106-1119`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/models.ts#L1106-L1119)

```ts
	const apiFor = (model: Model<Api>): ProviderStreams | undefined => single ?? byApi?.[model.api];

	const dispatch = (
		model: Model<Api>,
		run: (streams: ProviderStreams) => AssistantMessageEventStream,
	): AssistantMessageEventStream => {
		const streams = apiFor(model);
		if (!streams) {
			return lazyStream(model, async () => {
				throw new ModelsError("stream", `Provider ${input.id} has no API implementation for "${model.api}"`);
			});
		}
		return run(streams);
	};
```

源码：[`packages/ai/src/models.ts:1159-1162`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/models.ts#L1159-L1162)

```ts
		stream: (model, context, options) => dispatch(model, (streams) => streams.stream(model, context, options)),
		streamSimple: (model, context, options) =>
			dispatch(model, (streams) => streams.streamSimple(model, context, options)),
	};
```

`input.api` 可以是一个实现（所有模型共用），也可以是按 `model.api` 索引的表。找不到实现时同样用 `lazyStream` 返回一个 `error` 流，而不是抛异常。

`lazyApi`（`packages/ai/src/api/lazy.ts:98`）又包了一层 `lazyStream`：第一次请求时才 `import` API 模块。Anthropic SDK 体积较大，不用 Anthropic 的用户不会加载它。

所以主线场景里一个事件要经过三层流才到达 M2：faux 的流 → `composeModelProvider` 的 `lazyStream` → `ModelRuntime.streamSimple` 的 `lazyStream`。每一层的 `forwardStream` 只做原样转发。

**往哪走**：§4.5 faux 的实现。

### 4.5 faux：最简单的 provider

**调用位置**：`streamWith` → 扩展的 `streamSimple`（`lab/faux-ext.ts` 传入的 `faux.provider.streamSimple`）→ `createProvider` 的 `dispatch` → `core.streamSimple` → `core.stream`。

faux 的 `streamSimple` 直接调用 `stream`（`packages/ai/src/providers/faux.ts:588`）：

源码：[`packages/ai/src/providers/faux.ts:527-586`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/providers/faux.ts#L527-L586)

```ts
	const stream: StreamFunction<string, SimpleStreamOptions> = (requestModel, context, streamOptions) => {
		const outer = createAssistantMessageEventStream();
		const step = pendingResponses.shift();
		state.callCount++;

		queueMicrotask(async () => {
			try {
				await streamOptions?.onResponse?.({ status: 200, headers: {} }, requestModel);
				if (!step) {
					let message = createErrorMessage(
						new Error("No more faux responses queued"),
						api,
						provider,
						requestModel.id,
					);
					message = withUsageEstimate(message, context, streamOptions, promptCache);
					outer.push({ type: "error", reason: "error", error: message });
					outer.end(message);
					return;
				}

				if (streamOptions?.deferred) {
					const handle: DeferredHandle = {
						provider: requestModel.provider,
						modelId: requestModel.id,
						api: requestModel.api,
						id: randomId("deferred"),
						...(options.deferred?.pollAfterMs !== undefined ? { pollAfterMs: options.deferred.pollAfterMs } : {}),
					};
					deferredResponses.set(handle.id, {
						handle,
						step,
						context,
						options: streamOptions,
						model: requestModel,
						pendingFetches: Math.max(0, Math.floor(options.deferred?.pendingFetches ?? 0)),
						cancelled: false,
					});
					await streamWithDeltas(
						outer,
						createDeferredMessage(requestModel, handle),
						minTokenSize,
						maxTokenSize,
						tokensPerSecond,
						streamOptions.signal,
					);
					return;
				}

				const message = await resolveResponse(step, context, streamOptions, requestModel);
				await streamWithDeltas(outer, message, minTokenSize, maxTokenSize, tokensPerSecond, streamOptions?.signal);
			} catch (error) {
				const message = createErrorMessage(error, api, provider, requestModel.id);
				outer.push({ type: "error", reason: "error", error: message });
				outer.end(message);
			}
		});

		return outer;
	};
```

**讲解**

`stream` 是所有 provider 都要遵守的模式的最小版本：

1. 同步创建 `outer` 流，取出下一条预设回复 `step`，同步返回 `outer`。
2. 在 `queueMicrotask` 里异步生成事件。整个异步体包在 `try/catch` 里，任何异常都变成一个 `error` 事件加 `outer.end(message)`。
3. 没有预设回复时，不抛异常，而是 push 一个 `errorMessage: "No more faux responses queued"` 的 `error` 事件。

`resolveResponse`（`packages/ai/src/providers/faux.ts:512`）在 `step` 是函数时调用它，把 `context` 传进去。`lab/faux-ext.ts` 的 `PI_TRACE` 打印就是在这个函数里做的。然后 `withUsageEstimate`（`packages/ai/src/providers/faux.ts:246`）按"4 个字符一个 token"估算 usage。有 `sessionId` 时，它记住上一次的提示词，和这次的提示词比较公共前缀，前缀部分算 `cacheRead`，其余算 `cacheWrite`。§2.1 第 2 次请求的 `cacheRead: 1311` 就是第 1 次请求的全部提示词。

生成事件的是 `streamWithDeltas`：

源码：[`packages/ai/src/providers/faux.ts:358-383`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/providers/faux.ts#L358-L383)

```ts
async function streamWithDeltas(
	stream: AssistantMessageEventStream,
	message: AssistantMessage,
	minTokenSize: number,
	maxTokenSize: number,
	tokensPerSecond: number | undefined,
	signal: AbortSignal | undefined,
): Promise<void> {
	const partial: AssistantMessage = { ...message, content: [], stopReason: "pending" };
	if (signal?.aborted) {
		const aborted = createAbortedMessage(partial);
		stream.push({ type: "error", reason: "aborted", error: aborted });
		stream.end(aborted);
		return;
	}

	stream.push({ type: "start", partial: { ...partial } });

	for (let index = 0; index < message.content.length; index++) {
		if (signal?.aborted) {
			const aborted = createAbortedMessage(partial);
			stream.push({ type: "error", reason: "aborted", error: aborted });
			stream.end(aborted);
			return;
		}

```

源码：[`packages/ai/src/providers/faux.ts:409-457`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/providers/faux.ts#L409-L457)

```ts
		if (block.type === "text") {
			partial.content = [...partial.content, { type: "text", text: "" }];
			stream.push({ type: "text_start", contentIndex: index, partial: { ...partial } });
			for (const chunk of splitStringByTokenSize(block.text, minTokenSize, maxTokenSize)) {
				await scheduleChunk(chunk, tokensPerSecond);
				if (signal?.aborted) {
					const aborted = createAbortedMessage(partial);
					stream.push({ type: "error", reason: "aborted", error: aborted });
					stream.end(aborted);
					return;
				}
				(partial.content[index] as TextContent).text += chunk;
				stream.push({ type: "text_delta", contentIndex: index, delta: chunk, partial: { ...partial } });
			}
			stream.push({ type: "text_end", contentIndex: index, content: block.text, partial: { ...partial } });
			continue;
		}

		partial.content = [...partial.content, { type: "toolCall", id: block.id, name: block.name, arguments: {} }];
		stream.push({ type: "toolcall_start", contentIndex: index, partial: { ...partial } });
		// Partial arguments come from the same parser real providers use, so consumers see them grow.
		const toolArguments = new ToolCallArgumentsStream();
		for (const chunk of splitStringByTokenSize(JSON.stringify(block.arguments), minTokenSize, maxTokenSize)) {
			await scheduleChunk(chunk, tokensPerSecond);
			if (signal?.aborted) {
				const aborted = createAbortedMessage(partial);
				stream.push({ type: "error", reason: "aborted", error: aborted });
				stream.end(aborted);
				return;
			}
			(partial.content[index] as ToolCall).arguments = toolArguments.append(chunk);
			stream.push({ type: "toolcall_delta", contentIndex: index, delta: chunk, partial: { ...partial } });
		}
		(partial.content[index] as ToolCall).arguments = block.arguments;
		stream.push({ type: "toolcall_end", contentIndex: index, toolCall: block, partial: { ...partial } });
	}

	if (message.stopReason === "pending") {
		throw new Error("Faux response ended without a stop reason");
	}
	if (message.stopReason === "error" || message.stopReason === "aborted") {
		stream.push({ type: "error", reason: message.stopReason, error: message });
		stream.end(message);
		return;
	}

	stream.push({ type: "done", reason: message.stopReason, message });
	stream.end(message);
}
```

（中间省略的 386-407 行处理 thinking 块，和 text 块的写法相同。）

它把一条完整的 assistant 消息"倒放"成事件：

- `partial` 从 `content: []`、`stopReason: "pending"` 开始。
- 对每个内容块：先往 `partial.content` 加一个空块，发 `*_start`；再把内容切成片，每片追加到块里，发 `*_delta`；最后发 `*_end`。
- 工具调用的参数先 `JSON.stringify`，再切片，每片喂给 `ToolCallArgumentsStream`，得到"到目前为止的参数对象"（§4.9）。真实 provider 用的也是这个类，所以消费者看到的参数增长方式一致。
- 每片之前调用 `scheduleChunk` 等待。默认等一个微任务；设置了 `tokensPerSecond` 时按速度等待。每次等待后检查 `signal`，中止时发 `stopReason: "aborted"` 的 `error`。
- 最后按 `stopReason` 发 `done` 或 `error`。

每个事件的 `partial` 是 `{ ...partial }`，一个浅拷贝。浅拷贝复制了 `content` 数组的引用，块对象本身是共享的，后续的 `+=` 会改到之前事件里的块。这就是 `types.ts` 说"`partial` 不是快照"的原因。

faux 对外的形状由 `fauxProvider` 组装：

源码：[`packages/ai/src/providers/faux.ts:709-732`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/providers/faux.ts#L709-L732)

```ts
export function fauxProvider(options: RegisterFauxProviderOptions = {}): FauxProviderHandle {
	const core = createFauxCore(options);
	const provider = createProvider({
		id: core.provider,
		auth: { apiKey: { name: "Faux", resolve: async () => ({ auth: {} }) } },
		models: core.models,
		api: {
			stream: core.stream,
			streamSimple: core.streamSimple,
			fetchDeferred: core.fetchDeferred,
			cancelDeferred: core.cancelDeferred,
		},
	});
	return {
		provider,
		api: core.api,
		models: core.models,
		getModel: core.getModel,
		state: core.state,
		setResponses: core.setResponses,
		appendResponses: core.appendResponses,
		getPendingResponseCount: core.getPendingResponseCount,
	};
}
```

`auth.apiKey.resolve` 总是返回 `{ auth: {} }`，表示"已配置"，不需要真的 key。

**往哪走**：§4.6，`outer.push()` 做了什么。

### 4.6 EventStream 和 AssistantMessageEventStream

**调用位置**：每个 provider 都调用 `createAssistantMessageEventStream()` 或 `new AssistantMessageEventStream()`，然后 `push()`、`end()`。M2 用 `for await` 和 `result()` 消费（`packages/agent/src/agent-loop.ts:424`、`packages/agent/src/agent-loop.ts:419`）。

源码：[`packages/ai/src/utils/event-stream.ts:26-70`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/event-stream.ts#L26-L70)

```ts
export class EventStream<T, R = T> implements AsyncIterable<T> {
	private queue = new FifoQueue<T>();
	private waiting = new FifoQueue<(value: IteratorResult<T>) => void>();
	protected done = false;
	private finalResultPromise: Promise<R>;
	private resolveFinalResult!: (result: R) => void;
	private isComplete: (event: T) => boolean;
	private extractResult: (event: T) => R;

	constructor(isComplete: (event: T) => boolean, extractResult: (event: T) => R) {
		this.isComplete = isComplete;
		this.extractResult = extractResult;
		this.finalResultPromise = new Promise((resolve) => {
			this.resolveFinalResult = resolve;
		});
	}

	push(event: T): void {
		if (this.done) return;

		if (this.isComplete(event)) {
			this.done = true;
			this.resolveFinalResult(this.extractResult(event));
		}

		// Deliver to waiting consumer or queue it
		const waiter = this.waiting.dequeue();
		if (waiter) {
			waiter({ value: event, done: false });
		} else {
			this.queue.enqueue(event);
		}
	}

	end(result?: R): void {
		this.done = true;
		if (result !== undefined) {
			this.resolveFinalResult(result);
		}
		// Notify all waiting consumers that we're done
		while (this.waiting.length > 0) {
			const waiter = this.waiting.dequeue()!;
			waiter({ value: undefined as any, done: true });
		}
	}
```

源码：[`packages/ai/src/utils/event-stream.ts:72-89`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/event-stream.ts#L72-L89)

```ts
	async *[Symbol.asyncIterator](): AsyncIterator<T> {
		while (true) {
			if (this.queue.length > 0) {
				yield this.queue.dequeue()!;
			} else if (this.done) {
				return;
			} else {
				const result = await new Promise<IteratorResult<T>>((resolve) => this.waiting.enqueue(resolve));
				if (result.done) return;
				yield result.value;
			}
		}
	}

	result(): Promise<R> {
		return this.finalResultPromise;
	}
}
```

**讲解**

`EventStream<T, R>` 是一个单生产者、单消费者的异步队列，加一个最终结果：

- `queue` 存放还没被取走的事件；`waiting` 存放正在等事件的消费者（`for await` 卡在 `await new Promise` 上时，它的 `resolve` 就在这里）。两者都是 `FifoQueue`（`packages/ai/src/utils/event-stream.ts:3`），用两个栈实现的队列，出队是均摊 O(1)。
- `push(event)`：有消费者在等，直接交给它；没有就放进 `queue`。如果 `isComplete(event)` 为真（对 assistant 流来说是 `done` 或 `error`），标记 `done`，并用 `extractResult(event)` resolve 最终结果。`done` 之后的 push 直接丢弃。
- `end(result?)`：标记 `done`，唤醒所有等待的消费者，让它们的 `for await` 结束。
- 迭代器：先把 `queue` 里的事件取完，再看是否已结束，否则等待。所以 `push` 和消费可以任意交错，事件不会丢，也不会乱序。
- `result()`：返回同一个 Promise。它在终止事件被 push 的那一刻 resolve，和有没有人迭代无关。

`AssistantMessageEventStream` 在上面加了两件事：

源码：[`packages/ai/src/utils/event-stream.ts:91-130`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/event-stream.ts#L91-L130)

```ts
/**
 * Event stream of one assistant response. It also times the response: the final message (`done` or `error` event, or
 * the result passed to `end()`) gets `durationMs`, measured with a monotonic clock from the stream's creation, unless
 * the message already has one or its `timestamp` predates the stream. A stream that forwards a response which started
 * elsewhere, such as a deferred result fetched later, therefore leaves it untimed.
 */
export class AssistantMessageEventStream extends EventStream<AssistantMessageEvent, AssistantMessage> {
	readonly #startedAt = Date.now();
	readonly #startedAtMonotonic = performance.now();

	constructor() {
		super(
			(event) => event.type === "done" || event.type === "error",
			(event) => {
				if (event.type === "done") {
					return event.message;
				} else if (event.type === "error") {
					return event.error;
				}
				throw new Error("Unexpected event type for final result");
			},
		);
	}

	override push(event: AssistantMessageEvent): void {
		if (event.type === "done") this.#time(event.message);
		else if (event.type === "error") this.#time(event.error);
		super.push(event);
	}

	override end(result?: AssistantMessage): void {
		if (result !== undefined) this.#time(result);
		super.end(result);
	}

	#time(message: AssistantMessage): void {
		if (this.done || message.durationMs !== undefined || message.timestamp < this.#startedAt) return;
		message.durationMs = Math.max(0, Math.round(performance.now() - this.#startedAtMonotonic));
	}
}
```

1. 构造时指定"`done` 或 `error` 是终止事件"，最终结果分别取 `event.message` 和 `event.error`。
2. 计时：创建时记下单调时钟，终止事件到达时给最终消息写上 `durationMs`。如果消息的 `timestamp` 早于流的创建时间（例如 `lazyStream` 转发的内层流里的消息），就不改。§2.1 里 `message_end` 带的 `durationMs` 就来自这里。

`lab/m1-runtime.ts eventstream` 场景直接操作一个流：push `start`、`text_start`、`done`，再 push 一个迟到的 `text_delta`，然后 `end()`。之后才开始读：

```bash
PI_CODING_AGENT_DIR=/tmp/pi-lab/m1-agent node --conditions=source --experimental-strip-types m1-runtime.ts eventstream
```

```
[result] 还没有人迭代，result() 已经 resolve：stopReason=stop durationMs 是数字=true
[for await] 之后再迭代，拿到排队的事件：start, text_start, done
[for await] 第二次迭代拿到 0 个事件（队列已被取空）
```

三个结论：`result()` 不依赖迭代；`done` 之后的事件被丢弃；事件只能被消费一次，第二个 `for await` 什么也拿不到。

**往哪走**：faux 到此读完。§4.7 开始读一个真实的 provider：Anthropic。

### 4.7 Anthropic：streamSimple()

**调用位置**：`anthropicProvider()` 的 `createProvider` → `dispatch` → `lazyApi` 的 `lazyStream` → `import("./anthropic-messages.ts")` → `streamSimple(model, context, options)`。

每个 API 模块都导出 `stream` 和 `streamSimple` 两个函数（`ProviderStreams` 接口，`packages/ai/src/types.ts:319`）。`stream` 接收这家 API 自己的选项（`AnthropicOptions`），`streamSimple` 接收统一的 `SimpleStreamOptions`，把它换算成 `AnthropicOptions` 后调用 `stream`：

源码：[`packages/ai/src/api/anthropic-messages.ts:954-1002`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L954-L1002)

```ts
export const streamSimple: StreamFunction<"anthropic-messages", SimpleStreamOptions> = (
	model: Model<"anthropic-messages">,
	context: TranscriptContext,
	options?: SimpleStreamOptions,
): AssistantMessageEventStream => {
	if (!getAnthropicFederation(model, options?.apiKey, options?.headers, options?.env)) {
		assertRequestAuth(model.provider, options?.apiKey, options?.headers);
	}

	const base = {
		...buildBaseOptions(model, context, options, options?.apiKey),
		toolChoice: options?.toolChoice,
	} satisfies AnthropicOptions;
	if (!options?.reasoning) {
		return stream(model, context, {
			...base,
			thinkingEnabled: false,
		} satisfies AnthropicOptions);
	}

	// For models with adaptive thinking: use an effort level.
	// For older models: use budget-based thinking.
	if (model.compat?.forceAdaptiveThinking === true) {
		const effort = mapThinkingLevelToEffort(model, options.reasoning);
		return stream(model, context, {
			...base,
			thinkingEnabled: true,
			effort,
		} satisfies AnthropicOptions);
	}

	// Undefined means the caller did not request an output cap; let the helper use the model cap.
	// Do not coerce to 0 here, or the thinking budget would become the entire max_tokens value.
	const adjusted = adjustMaxTokensForThinking(
		base.maxTokens,
		model.maxTokens,
		options.reasoning,
		options.thinkingBudgets,
	);

	const maxTokens = clampMaxTokensToContext(model, context, adjusted.maxTokens);

	return stream(model, context, {
		...base,
		maxTokens,
		thinkingEnabled: true,
		thinkingBudgetTokens: Math.min(adjusted.thinkingBudget, Math.max(0, maxTokens - 1024)),
	} satisfies AnthropicOptions);
};
```

**讲解**

第一步是检查请求凭证。`assertRequestAuth`（`packages/ai/src/api/anthropic-messages.ts:331`）在既没有 `apiKey`、也没有 `authorization`/`x-api-key` 请求头时同步抛出 `No API key for provider: anthropic`。这是 `types.ts` 注释里"直接调用 `streamSimple()` 时，凭证缺失可以同步抛出"的那种情况。经过 provider（`lazyApi` 的 `lazyStream`）调用时，这个异常会变成流里的 `error` 事件。§4.11 的实验对比了两种调用方式。

`buildBaseOptions`（`packages/ai/src/api/simple-options.ts:36`）把通用选项原样搬过去，并把 `maxTokens` 裁剪到上下文窗口里：`contextWindow - 估算的上下文 token 数 - 4096`。

然后按思考级别 `options.reasoning` 分三种情况：

| 情况 | 条件 | 生成的 `AnthropicOptions` |
|---|---|---|
| 不思考 | `reasoning` 为空（M2 把 `"off"` 变成 `undefined`，见 M2 §4.2） | `thinkingEnabled: false` |
| 自适应思考 | `model.compat.forceAdaptiveThinking === true`（较新的 Claude 模型） | `thinkingEnabled: true`，`effort` 由 `mapThinkingLevelToEffort` 换算 |
| 预算思考 | 其余模型 | `thinkingEnabled: true`，`thinkingBudgetTokens` 由思考级别查表；`maxTokens` 加上预算 |

两种思考方式的换算细节在 §4.14 讲。

**往哪走**：§4.8 `stream`。

### 4.8 Anthropic：stream() 的骨架和 SSE 解码

**调用位置**：`streamSimple()` 的三个 `return stream(model, context, {...})` 之一。

源码：[`packages/ai/src/api/anthropic-messages.ts:577-604`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L577-L604)

```ts
export const stream: StreamFunction<"anthropic-messages", AnthropicOptions> = (
	model: Model<"anthropic-messages">,
	context: TranscriptContext,
	options?: AnthropicOptions,
): AssistantMessageEventStream => {
	const stream = new AssistantMessageEventStream();
	const normalizedContext = resolveTranscript(context, getAnthropicCompat(model).supportsMidConvoSystemMessages);
	const currentTools = getCurrentTools(normalizedContext.messages);

	(async () => {
		const output: AssistantMessage = {
			role: "assistant",
			content: [],
			api: model.api as Api,
			provider: model.provider,
			model: model.id,
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "pending",
			timestamp: Date.now(),
		};

```

**讲解**

骨架和 faux 一样：同步创建流、同步返回，在一个立即执行的 async 函数里干活。不同之处有三点：

- 开头调用 `resolveTranscript`（§4.14）：模型不支持对话中途的 system 消息时，把所有 system 消息合并成开头一条。`currentTools` 是重放后的工具清单，后面把 OAuth 模式下改过名的工具名映射回来时要用。
- `output` 是这次回复的累积对象，也是每个事件的 `partial`。它从 `stopReason: "pending"` 开始，`timestamp` 是请求开始的时间。
- 整个 async 体在一个 `try` 里（605 行开始，§4.11 读 `catch`）。

605-643 行创建 Anthropic SDK 客户端，处理 OAuth、GitHub Copilot、联邦认证几种情况，本模块略过。之后是发请求：

源码：[`packages/ai/src/api/anthropic-messages.ts:644-693`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L644-L693)

```ts
			const requestOptions = {
				...(options?.signal ? { signal: options.signal } : {}),
				...(options?.timeoutMs !== undefined ? { timeout: options.timeoutMs } : {}),
				maxRetries: 0,
			};
			let disablePreferredStrictTools = false;
			let response: Response;
			for (;;) {
				options?.signal?.throwIfAborted();
				const built = buildParams(model, normalizedContext, isOAuth, options, disablePreferredStrictTools);
				if (built.providerThinkingLevel) output.providerThinkingLevel = built.providerThinkingLevel;
				let params = built.params;
				const nextParams = await options?.onPayload?.(params, model);
				if (nextParams !== undefined) {
					params = { ...(nextParams as MessageCreateParamsStreaming), stream: true };
				}
				try {
					response = await retryProviderRequest(
						() => client.beta.messages.create(params, requestOptions).asResponse(),
						{
							maxRetries: options?.maxRetries,
							maxRetryDelayMs: options?.maxRetryDelayMs,
							signal: options?.signal,
						},
					);
					break;
				} catch (error) {
					// Only downgrade a rejected request, never an already-started stream.
					if (
						options?.signal?.aborted ||
						disablePreferredStrictTools ||
						!built.hasPreferredStrictTools ||
						!(error instanceof Anthropic.APIError) ||
						error.status !== 400 ||
						error.type !== "invalid_request_error" ||
						!isStrictToolLimitErrorMessage(error.message)
					) {
						throw error;
					}
					disablePreferredStrictTools = true;
					appendAssistantMessageDiagnostic(output, {
						type: "anthropic_strict_tools_fallback",
						timestamp: Date.now(),
						error: { message: error.message },
						details: { reason: "strict_tool_limits_exceeded" },
					});
				}
			}
			await options?.onResponse?.({ status: response.status, headers: headersToRecord(response.headers) }, model);
			stream.push({ type: "start", partial: output });
```

- `maxRetries: 0` 关掉 SDK 自己的重试，改用 `retryProviderRequest`（`packages/ai/src/utils/provider-retry.ts`）。它对 408、409、429、5xx 重试，遵守 `retry-after` 头，但服务器要求等待的时间超过 `maxRetryDelayMs`（默认 60 秒）时直接失败，交给上层（M8 的自动重试）处理。
- `buildParams`（§4.12）构造请求体。`onPayload` 回调可以查看或替换请求体，扩展的 `before_provider_request` 事件就接在这里（→ M7）。
- 内层的 `for (;;)` 只为一种情况重试：请求因为 strict 工具超出限制被拒绝（HTTP 400），去掉 strict 再发一次。
- `.asResponse()` 拿到原始 `Response`，不用 SDK 的流解析。收到响应后先调用 `onResponse`，再 push `start` 事件。

响应体由 `iterateAnthropicEvents` 解码：

源码：[`packages/ai/src/api/anthropic-messages.ts:536-575`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L536-L575)

```ts
async function* iterateAnthropicEvents(
	response: Response,
	signal?: AbortSignal,
): AsyncGenerator<RawMessageStreamEvent> {
	if (!response.body) {
		throw new Error("Attempted to iterate over an Anthropic response with no body");
	}

	let sawMessageStart = false;
	let sawMessageEnd = false;

	for await (const sse of iterateSseMessages(response.body, signal)) {
		if (sse.event === "error") {
			throw new Error(sse.data);
		}

		if (!ANTHROPIC_MESSAGE_EVENTS.has(sse.event ?? "")) {
			continue;
		}

		try {
			const event = parseJsonWithRepair<RawMessageStreamEvent>(sse.data);
			if (event.type === "message_start") {
				sawMessageStart = true;
			} else if (event.type === "message_stop") {
				sawMessageEnd = true;
			}
			yield event;
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			throw new Error(
				`Could not parse Anthropic SSE event ${sse.event}: ${message}; data=${sse.data}; raw=${sse.raw.join("\\n")}`,
			);
		}
	}

	if (sawMessageStart && !sawMessageEnd) {
		throw new Error("Anthropic stream ended before message_stop");
	}
}
```

`iterateSseMessages`（`packages/ai/src/api/anthropic-messages.ts:477`）把字节流按行拆开，按 SSE 规则拼出 `{ event, data }`：`event:` 行设置事件名，`data:` 行追加数据，空行表示一个事件结束，`:` 开头的是注释。它能处理一行被拆在两个网络包里的情况（`buffer` 留住不完整的行）。

`iterateAnthropicEvents` 在这上面加了三条规则：

- `event: error` 直接抛异常。
- 只保留 6 种消息事件（`ANTHROPIC_MESSAGE_EVENTS`，`packages/ai/src/api/anthropic-messages.ts:397`），`ping` 等其他事件跳过。
- 看到了 `message_start` 却没看到 `message_stop`，就抛 `Anthropic stream ended before message_stop`。连接中途断开时，不会把半截回复当成完整回复。

**往哪走**：§4.9，`for await` 循环怎样处理这 6 种事件。

### 4.9 内容块 → 统一事件，以及流式工具参数

**调用位置**：`stream()` 里的 `for await (const event of iterateAnthropicEvents(response, options?.signal))`（`packages/ai/src/api/anthropic-messages.ts:702`）。

Anthropic 的回复由若干"内容块"组成，每个块有一个 `index`。一个块的生命周期是 `content_block_start` → 若干 `content_block_delta` → `content_block_stop`，正好对应 pi 的 `*_start` → `*_delta` → `*_end`。

块开始：

源码：[`packages/ai/src/api/anthropic-messages.ts:728-770`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L728-L770)

```ts
				} else if (event.type === "content_block_start") {
					if (event.content_block.type === "fallback") {
						if (output.content.length > 0) {
							throw new Error("Anthropic performed an unsupported mid-output model fallback");
						}
						continue;
					}
					if (event.content_block.type === "text") {
						output.content.push({ type: "text", text: event.content_block.text ?? "" });
						openBlocks.set(event.index, { contentIndex: output.content.length - 1 });
						stream.push({ type: "text_start", contentIndex: output.content.length - 1, partial: output });
					} else if (event.content_block.type === "thinking") {
						output.content.push({
							type: "thinking",
							thinking: event.content_block.thinking ?? "",
							thinkingSignature: event.content_block.signature ?? "",
						});
						openBlocks.set(event.index, { contentIndex: output.content.length - 1 });
						stream.push({ type: "thinking_start", contentIndex: output.content.length - 1, partial: output });
					} else if (event.content_block.type === "redacted_thinking") {
						output.content.push({
							type: "thinking",
							thinking: "[Reasoning redacted]",
							thinkingSignature: event.content_block.data,
							redacted: true,
						});
						openBlocks.set(event.index, { contentIndex: output.content.length - 1 });
						stream.push({ type: "thinking_start", contentIndex: output.content.length - 1, partial: output });
					} else if (event.content_block.type === "tool_use") {
						output.content.push({
							type: "toolCall",
							id: event.content_block.id,
							name: isOAuth
								? fromClaudeCodeName(event.content_block.name, currentTools)
								: event.content_block.name,
							arguments: (event.content_block.input as Record<string, any>) ?? {},
						});
						openBlocks.set(event.index, {
							contentIndex: output.content.length - 1,
							arguments: new ToolCallArgumentsStream(),
						});
						stream.push({ type: "toolcall_start", contentIndex: output.content.length - 1, partial: output });
					}
```

块增量：

源码：[`packages/ai/src/api/anthropic-messages.ts:771-808`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L771-L808)

```ts
				} else if (event.type === "content_block_delta") {
					const open = openBlock(event.index);
					if (event.delta.type === "text_delta") {
						if (open?.block.type === "text") {
							open.block.text += event.delta.text;
							stream.push({
								type: "text_delta",
								contentIndex: open.contentIndex,
								delta: event.delta.text,
								partial: output,
							});
						}
					} else if (event.delta.type === "thinking_delta") {
						if (open?.block.type === "thinking") {
							open.block.thinking += event.delta.thinking;
							stream.push({
								type: "thinking_delta",
								contentIndex: open.contentIndex,
								delta: event.delta.thinking,
								partial: output,
							});
						}
					} else if (event.delta.type === "input_json_delta") {
						if (open?.block.type === "toolCall" && open.arguments !== undefined) {
							open.block.arguments = open.arguments.append(event.delta.partial_json);
							stream.push({
								type: "toolcall_delta",
								contentIndex: open.contentIndex,
								delta: event.delta.partial_json,
								partial: output,
							});
						}
					} else if (event.delta.type === "signature_delta") {
						if (open?.block.type === "thinking") {
							open.block.thinkingSignature = open.block.thinkingSignature || "";
							open.block.thinkingSignature += event.delta.signature;
						}
					}
```

块结束：

源码：[`packages/ai/src/api/anthropic-messages.ts:809-837`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L809-L837)

```ts
				} else if (event.type === "content_block_stop") {
					const open = openBlock(event.index);
					openBlocks.delete(event.index);
					if (open) {
						const { block, contentIndex: index } = open;
						if (block.type === "text") {
							stream.push({
								type: "text_end",
								contentIndex: index,
								content: block.text,
								partial: output,
							});
						} else if (block.type === "thinking") {
							stream.push({
								type: "thinking_end",
								contentIndex: index,
								content: block.thinking,
								partial: output,
							});
						} else if (block.type === "toolCall" && open.arguments !== undefined) {
							Object.assign(block, open.arguments.finish());
							stream.push({
								type: "toolcall_end",
								contentIndex: index,
								toolCall: block,
								partial: output,
							});
						}
					}
```

**讲解**

`openBlocks` 是一张表：Anthropic 的块 `index` → pi 的 `contentIndex`，再加上工具调用的参数解析器（695-700 行）。两者不一定相等：`fallback` 块（服务端换了模型）不会进入 `output.content`。这张表放在内容块之外，注释说"所以它不会出现在 partial 里"。

| Anthropic 块类型 | pi 内容块 | 说明 |
|---|---|---|
| `text` | `TextContent` | `text_delta` 追加到 `text` |
| `thinking` | `ThinkingContent` | `thinking_delta` 追加到 `thinking`；`signature_delta` 追加到 `thinkingSignature`，不发事件 |
| `redacted_thinking` | `ThinkingContent`，`redacted: true` | 内容是加密的，`thinking` 填占位文本，密文存进 `thinkingSignature` |
| `tool_use` | `ToolCall` | `input_json_delta` 交给 `ToolCallArgumentsStream` |

三种块的处理方式相同：直接修改 `output.content` 里的块，再 push 事件，`partial` 就是 `output` 本身。签名不发事件，因为界面不需要显示它，但它必须完整保存，下一轮请求要原样发回（§4.12）。

工具参数是最特别的。Anthropic 用 `input_json_delta` 一段一段发送 JSON 文本，每段不一定是完整的 JSON。`ToolCallArgumentsStream` 负责把它们变成对象：

源码：[`packages/ai/src/utils/streaming-json.ts:447-500`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/streaming-json.ts#L447-L500)

```ts
/**
 * The arguments of one streaming tool call: the raw text, for the one full parse when the call ends, and a
 * `StreamingJsonParser` for the partial arguments in between. Providers keep one per call in their own stream state,
 * never on the content block, so the raw text never reaches partial messages or transcripts.
 */
export class ToolCallArgumentsStream {
	#text = "";
	#parser = new StreamingJsonParser();
	#snapshot: JsonValue | undefined = undefined;
	#arguments: ToolCall["arguments"] = {};

	/** The raw argument text received so far. */
	get text(): string {
		return this.#text;
	}

	/** Append a piece of argument text; returns the partial arguments, a new object whenever they changed. */
	append(delta: string): ToolCall["arguments"] {
		this.#text += delta;
		this.#parser.append(delta);
		return this.#partial();
	}

	/**
	 * Take the provider's complete argument text. When it continues the text so far, returns the missing piece as
	 * `delta`; otherwise starts over with `text`. Either way returns the partial arguments of `text`.
	 */
	replace(text: string): { readonly delta: string | undefined; readonly arguments: ToolCall["arguments"] } {
		if (text.startsWith(this.#text)) {
			const delta = text.slice(this.#text.length);
			return { delta, arguments: delta === "" ? this.#partial() : this.append(delta) };
		}
		this.#text = "";
		this.#parser = new StreamingJsonParser();
		return { delta: undefined, arguments: this.append(text) };
	}

	/** The final arguments: one full parse of the complete text, which reports malformed arguments. */
	finish(): Pick<ToolCall, "arguments" | "argumentsError"> {
		return parseToolCallArguments(this.#text);
	}

	#partial(): ToolCall["arguments"] {
		const snapshot = this.#parser.snapshot();
		if (snapshot === this.#snapshot) return this.#arguments;
		this.#snapshot = snapshot;
		// Tool arguments are an object; any other partial value shows as no arguments yet.
		this.#arguments =
			typeof snapshot === "object" && snapshot !== null && !Array.isArray(snapshot)
				? (snapshot as ToolCall["arguments"])
				: {};
		return this.#arguments;
	}
}
```

- `append(delta)`：记下原始文本，交给增量解析器 `StreamingJsonParser`，返回"到目前为止能确定的对象"。未闭合的字符串显示已收到的字符，还没开始的值不出现。解析器每段只读一次，总代价和文本长度成正比。
- `finish()`：在 `content_block_stop` 时调用，对完整文本做一次正式解析（`parseToolCallArguments`，`packages/ai/src/utils/json-parse.ts:106`）。结果不是 JSON 对象时，`arguments` 为 `{}`，错误和原文写进 `argumentsError`。M2 校验参数时会用这个错误生成工具结果（M2 §4.11）。
- 原始文本只存在解析器里，不会写到内容块上，所以不会进入 transcript 和会话文件。

`lab/m1-anthropic.ts` 用一个假的 `fetch` 运行这个真实的适配器：请求不发出去，响应是一段预先写好的 SSE 文本，按 37 字节切块返回，模拟网络分包。脚本在 `push()` 被调用的那一刻打印事件：

```bash
node --conditions=source --experimental-strip-types m1-anthropic.ts sse
```

```
[SSE] 服务器发出的事件：message_start ping content_block_start content_block_delta(thinking_delta) content_block_delta(signature_delta) content_block_stop content_block_start content_block_delta(text_delta) content_block_delta(text_delta) content_block_stop content_block_start content_block_delta(input_json_delta) content_block_delta(input_json_delta) content_block_delta(input_json_delta) content_block_stop message_delta message_stop
[事件] start           responseId=undefined usage.input=0
[事件] thinking_start  [0]
[事件] thinking_delta  [0] delta="需要先读文件。"
[事件] thinking_end    [0] signature="sig-abc"
[事件] text_start      [1]
[事件] text_delta      [1] delta="我先读"
[事件] text_delta      [1] delta="一下。"
[事件] text_end        [1] content="我先读一下。"
[事件] toolcall_start  [2] id=toolu_01 name=read arguments={}
[事件] toolcall_delta  [2] delta="{\"pa"          arguments={}
[事件] toolcall_delta  [2] delta="th\": \"READ"   arguments={"path":"READ"}
[事件] toolcall_delta  [2] delta="ME.md\"}"       arguments={"path":"README.md"}
[事件] toolcall_end    [2] toolCall={"type":"toolCall","id":"toolu_01","name":"read","arguments":{"path":"README.md"}}
[事件] done            reason=toolUse
[result] {"role":"assistant","content":[{"type":"thinking","thinking":"需要先读文件。","thinkingSignature":"sig-abc"},{"type":"text","text":"我先读一下。"},{"type":"toolCall","id":"toolu_01","name":"read","arguments":{"path":"README.md"}}],"api":"anthropic-messages","provider":"anthropic","model":"claude-sonnet-4-5","usage":{"input":120,"output":42,"cacheRead":0,"cacheWrite":1000,"totalTokens":1162,"cost":{"input":0.00036,"output":0.00063,"cacheRead":0,"cacheWrite":0.00375,"total":0.004739999999999999,"totalFromProvider":false},"cacheWrite1h":0},"stopReason":"toolUse","timestamp":"<ts>","responseId":"msg_01","rawStopReason":"tool_use","durationMs":"<ms>"}
```

对照第一行的 SSE 事件和下面的 pi 事件：

- `ping` 被跳过；`signature_delta` 没有对应的事件，但签名出现在 `thinking_end` 和最终消息里。
- `toolcall_start` 时 `arguments` 是 `{}`。第一段 `{"pa` 之后仍是 `{}`（键还没读完）；第二段之后是 `{"path":"READ"}`；第三段之后完整。
- `start` 时 `responseId` 还没有，`usage.input` 是 0。它们由 `message_start` 填入，见 §4.10。

**往哪走**：§4.10，`message_start` 和 `message_delta`。

### 4.10 message_start / message_delta：usage 和 stopReason

**调用位置**：同一个 `for await` 循环的另外两个分支。

源码：[`packages/ai/src/api/anthropic-messages.ts:695-727`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L695-L727)

```ts
			// Stream state per Anthropic block index, kept off the content blocks so it never reaches partials.
			const openBlocks = new Map<number, { contentIndex: number; arguments?: ToolCallArgumentsStream }>();
			const openBlock = (anthropicIndex: number) => {
				const open = openBlocks.get(anthropicIndex);
				return open === undefined ? undefined : { ...open, block: output.content[open.contentIndex]! };
			};

			for await (const event of iterateAnthropicEvents(response, options?.signal)) {
				await options?.onProviderStreamEvent?.(event, model);
				if (event.type === "message_start") {
					output.responseId = event.message.id;
					const transformations = event.message.input_transformations;
					if (Array.isArray(transformations)) inputTransformations = transformations;
					const responseModel = event.message.model;
					if (responseModel !== model.id) output.responseModel = responseModel;
					const fallbackCost =
						responseModel === model.id
							? undefined
							: model.compat?.allowedFallbackModels?.find(
									(fallback) => fallback.provider === model.provider && fallback.model === responseModel,
								)?.cost;
					usageModel = fallbackCost ? { ...model, id: responseModel, cost: fallbackCost } : model;
					// Capture initial token usage from message_start event
					// This ensures we have input token counts even if the stream is aborted early
					output.usage.input = event.message.usage.input_tokens || 0;
					output.usage.output = event.message.usage.output_tokens || 0;
					output.usage.cacheRead = event.message.usage.cache_read_input_tokens || 0;
					output.usage.cacheWrite = event.message.usage.cache_creation_input_tokens || 0;
					output.usage.cacheWrite1h = event.message.usage.cache_creation?.ephemeral_1h_input_tokens || 0;
					// Anthropic doesn't provide total_tokens, compute from components
					output.usage.totalTokens =
						output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite;
					calculateCost(usageModel, output.usage);
```

源码：[`packages/ai/src/api/anthropic-messages.ts:838-880`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L838-L880)

```ts
				} else if (event.type === "message_delta") {
					const transformations = event.input_transformations;
					if (Array.isArray(transformations)) inputTransformations = transformations;
					if (event.delta.stop_reason) {
						output.rawStopReason = event.delta.stop_reason;
						const stopReasonResult = mapStopReason(event.delta.stop_reason, event.delta.stop_details);
						output.stopReason = stopReasonResult.stopReason;
						if (stopReasonResult.errorMessage) {
							output.errorMessage = stopReasonResult.errorMessage;
						}
					}
					// Only update usage fields if present (not null).
					// Preserves input_tokens from message_start when proxies omit it in message_delta.
					if (event.usage) {
						if (event.usage.input_tokens != null) {
							output.usage.input = event.usage.input_tokens;
						}
						if (event.usage.output_tokens != null) {
							output.usage.output = event.usage.output_tokens;
						}
						if (event.usage.cache_read_input_tokens != null) {
							output.usage.cacheRead = event.usage.cache_read_input_tokens;
						}
						if (event.usage.cache_creation_input_tokens != null) {
							output.usage.cacheWrite = event.usage.cache_creation_input_tokens;
						}
						// Vercel AI Gateway includes the TTL breakdown in deltas, though the SDK only types it on message_start.
						const cacheCreation = (
							event.usage as typeof event.usage & { cache_creation?: { ephemeral_1h_input_tokens?: number } }
						).cache_creation;
						if (cacheCreation?.ephemeral_1h_input_tokens != null) {
							output.usage.cacheWrite1h = cacheCreation.ephemeral_1h_input_tokens;
						}
						// Anthropic reports reasoning tokens as a subset of output tokens.
						const thinkingTokens = event.usage.output_tokens_details?.thinking_tokens;
						if (thinkingTokens != null) {
							output.usage.reasoning = thinkingTokens;
						}
					}
					// Anthropic doesn't provide total_tokens, compute from components
					output.usage.totalTokens =
						output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite;
					calculateCost(usageModel, output.usage);
```

**讲解**

Anthropic 把 usage 分两次给：`message_start` 带着输入 token 数（含缓存读写），`message_delta` 带着最终的输出 token 数。代码在两处都更新 `output.usage`，重新计算 `totalTokens`，并调用 `calculateCost`（`packages/ai/src/models.ts:1236`）按模型价格算出费用。`message_start` 时就记录输入 token，是为了在流被中止时也有输入用量。`message_delta` 只更新不为 `null` 的字段，因为有些代理在 `message_delta` 里不带 `input_tokens`。

`stopReason` 只在 `message_delta` 里出现。原始值存进 `rawStopReason`，换算后的值存进 `stopReason`：

源码：[`packages/ai/src/api/anthropic-messages.ts:1660-1686`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L1660-L1686)

```ts
function mapStopReason(
	reason: BetaStopReason | string,
	stopDetails?: RefusalStopDetails | null,
): { stopReason: StopReason; errorMessage?: string } {
	switch (reason) {
		case "end_turn":
			return { stopReason: "stop" };
		case "max_tokens":
			return { stopReason: "length" };
		case "tool_use":
			return { stopReason: "toolUse" };
		case "refusal":
			return {
				stopReason: "error",
				errorMessage: stopDetails?.explanation || `The model refused to complete the request`,
			};
		case "pause_turn": // Stop is good enough -> resubmit
			return { stopReason: "stop" };
		case "stop_sequence":
			return { stopReason: "stop" }; // We don't supply stop sequences, so this should never happen
		case "sensitive": // Content flagged by safety filters (not yet in SDK types)
			return { stopReason: "error", errorMessage: "Provider stopped with: sensitive" };
		default:
			// Handle unknown stop reasons gracefully (API may add new values)
			throw new Error(`Unhandled stop reason: ${reason}`);
	}
}
```

`refusal` 和 `sensitive` 被换成 `error` 并带上 `errorMessage`，未知的值直接抛异常。抛出的异常会被 §4.11 的 `catch` 变成 `error` 事件。

§4.9 实验的 `[result]` 行里：`usage.input: 120`、`cacheWrite: 1000` 来自 `message_start`，`output: 42` 来自 `message_delta`；`cost` 是按 `claude-sonnet-4-5` 的价格（输入 3 美元、输出 15 美元、缓存写 3.75 美元每百万 token）算出来的；`rawStopReason: "tool_use"` 换成了 `stopReason: "toolUse"`。

**往哪走**：§4.11，循环结束之后。

### 4.11 结束：done，或者 try/catch 变成 error

**调用位置**：`for await` 循环结束之后，以及包住整个 async 体的 `try`。

源码：[`packages/ai/src/api/anthropic-messages.ts:892-927`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L892-L927)

```ts
			if (options?.signal?.aborted) {
				throw new Error("Request was aborted");
			}

			if (output.stopReason === "pending") {
				throw new Error("Anthropic stream ended without a stop reason");
			}
			if (output.stopReason === "aborted" || output.stopReason === "error") {
				throw new Error(output.errorMessage || "An unknown error occurred");
			}
			if (inputTransformations && inputTransformations.length > 0) {
				appendAssistantMessageDiagnostic(output, {
					type: "anthropic_input_transformations",
					timestamp: Date.now(),
					details: {
						transformations: inputTransformations.map((transformation) => ({
							type: transformation.type ?? undefined,
							path: transformation.path ?? undefined,
							reason: transformation.reason ?? undefined,
						})),
					},
				});
			}

			stream.push({ type: "done", reason: output.stopReason, message: output });
			stream.end();
		} catch (error) {
			output.stopReason = options?.signal?.aborted ? "aborted" : "error";
			output.errorMessage = error instanceof Error ? error.message : JSON.stringify(error);
			stream.push({ type: "error", reason: output.stopReason, error: output });
			stream.end();
		}
	})();

	return stream;
};
```

**讲解**

循环正常结束后还有三道检查，任何一道不通过都抛异常：被中止了；没有收到 `stop_reason`（`stopReason` 仍是 `pending`）；`stopReason` 是 `error`（来自 `refusal`）。通过了才 push `done`。

`catch` 处理这个 async 体里的全部异常：请求失败、HTTP 错误、SSE 解码错误、上面的检查。它把 `output` 改成 `stopReason: "aborted"`（`signal` 已中止）或 `"error"`，写上 `errorMessage`，push `error` 事件。注意它用的是同一个 `output`：已经收到的内容块会保留在错误消息里。M2 不会执行错误消息里的工具调用（M2 §4.5），M1 的 `transformMessages` 在下一次请求时会丢掉这条消息（§4.13）。

`lab/m1-anthropic.ts error` 场景演示了四种失败：

```bash
node --conditions=source --experimental-strip-types m1-anthropic.ts error
```

```
--- 1. SSE 在 message_stop 之前断开
[事件] start           responseId=undefined usage.input=0
[事件] thinking_start  [0]
[事件] thinking_delta  [0] delta="需要先读文件。"
[事件] thinking_end    [0] signature="sig-abc"
[事件] text_start      [1]
[事件] text_delta      [1] delta="我先读"
[事件] text_delta      [1] delta="一下。"
[事件] error           reason=error errorMessage="Anthropic stream ended before message_stop"
--- 2. 服务器返回 HTTP 400
[事件] error           reason=error errorMessage="400 {\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\",\"message\":\"max_tokens: too large\"}}"
[result] stopReason=error content=[]
--- 3. 直接调用 api 模块的 streamSimple，没有 API key
同步抛出：No API key for provider: anthropic
--- 4. 同样没有 API key，但经过 anthropicProvider()（lazyApi 包了一层 lazyStream）
[事件] error           reason=error errorMessage="No API key for provider: anthropic"
```

1. SSE 在 `message_stop` 之前结束：前面的事件照常发出，最后一个是 `error`。
2. HTTP 400：SDK 抛出 `APIError`，没有 `start`，直接 `error`，`content` 为空。
3. 直接调用 API 模块的 `streamSimple`，没有 key：同步抛出。
4. 经过 `anthropicProvider()` 调用：同样的错误变成了流里的 `error` 事件。`ModelRuntime` 总是经过 provider 调用，所以 M2 只会遇到第 4 种。

**往哪走**：§4.12，请求体怎么构造。

### 4.12 buildParams()：统一消息 → Anthropic 请求体

**调用位置**：`stream()` 发请求的循环里，`buildParams(model, normalizedContext, isOAuth, options, disablePreferredStrictTools)`（`packages/ai/src/api/anthropic-messages.ts:653`）。

源码：[`packages/ai/src/api/anthropic-messages.ts:1153-1170`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L1153-L1170)

```ts
function buildParams(
	model: Model<"anthropic-messages">,
	context: TranscriptContext,
	isOAuthToken: boolean,
	options?: AnthropicOptions,
	disablePreferredStrictTools = false,
): {
	params: MessageCreateParamsStreaming;
	hasPreferredStrictTools: boolean;
	/** Effort of the final effort marker, recorded on the reply so later requests can replay it. */
	providerThinkingLevel: AnthropicEffort | undefined;
} {
	const { cacheControl } = getCacheControl(model, options?.cacheRetention, options?.env);
	const compat = getAnthropicCompat(model);
	const initialSystemMessage = getInitialSystemMessage(context.messages);
	const initialSystemText = initialSystemMessage ? getSystemMessageText(initialSystemMessage) : "";
	const transformedMessages = transformMessages(context.messages, model, normalizeToolCallId);
	const conversationMessages = initialSystemMessage ? transformedMessages.slice(1) : transformedMessages;
```

源码：[`packages/ai/src/api/anthropic-messages.ts:1194-1217`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L1194-L1217)

```ts
	const initialTools = initialSystemMessage?.toolsAdded ?? [];
	const nativeToolChanges =
		compat.supportsMidConvoSystemMessages && compat.supportsMidConvoToolChanges && initialTools.length > 0;
	const thinkingDisabled =
		model.reasoning && options?.thinkingEnabled === false && model.thinkingLevelMap?.off !== null;
	// Effort cannot change between messages while thinking is disabled, so "off" sends no effort markers.
	const effortMessages = model.compat?.supportsMidConvoEffort === true && !thinkingDisabled;
	const converted = convertMessages(
		conversationMessages,
		isOAuthToken,
		cacheControl,
		compat.allowEmptySignature,
		effortMessages ? model.provider : undefined,
		nativeToolChanges ? convertToolDefinitions : undefined,
	);
	const activeEffort = options?.effort ?? "high";
	const betaFeatures = getBetaFeatures(model, context, isOAuthToken, nativeToolChanges, options);
	const params: MessageCreateParamsStreaming = {
		model: model.id,
		messages: effortMessages ? insertThinkingLevelMessages(converted, activeEffort) : converted.messages,
		max_tokens: options?.maxTokens ?? model.maxTokens,
		stream: true,
		...(betaFeatures.length > 0 ? { betas: betaFeatures } : {}),
	};
```

**讲解**

请求体的三个主要部分分别来自 transcript 的不同位置：

| 请求体字段 | 来源 |
|---|---|
| `system` | 开头那条 system 消息的文本（`getSystemMessageText`：`content` 加上所有 `sections`），1235-1244 行 |
| `messages` | 开头 system 消息之后的全部消息，先经过 `transformMessages`（§4.13），再由 `convertMessages` 逐条翻译 |
| `tools` | 1256-1268 行。支持原生工具变更时是初始工具加一个占位工具；否则是 `getCurrentTools` 重放出的当前工具 |

`convertMessages`（`packages/ai/src/api/anthropic-messages.ts:1348`）逐条翻译：

- `user` 消息 → `role: "user"`，空白文本块丢掉。
- `assistant` 消息 → `role: "assistant"`。`text` 块保留；`thinking` 块带签名时原样回传，没有签名时（例如流被中止）变成普通文本；`toolCall` → `tool_use`。全部块都被丢掉时，整条消息不发。
- 连续的 `toolResult` 消息 → 合并成一条 `role: "user"` 消息，里面是多个 `tool_result` 块。Anthropic 要求工具结果紧跟在对应的 `tool_use` 之后。
- 后面的 `system` 消息（只有模型支持时才会留到这里）→ `role: "system"` 消息，文本由 `renderSystemMessageUpdate` 生成，例如 `Updated system prompt section "rules":…`。它们会被推迟到下一条 assistant 消息之前或末尾再输出，不插在 `tool_use` 和 `tool_result` 之间。

**往哪走**：§4.13 `transformMessages`，然后 §4.14 的 provider 差异点。

### 4.13 transformMessages()：跨 provider 的历史修正

**调用位置**：`buildParams()` 第 1169 行，`transformMessages(context.messages, model, normalizeToolCallId)`。每个 API 实现都在转换消息前调用它，各自传入自己的 `normalizeToolCallId`。

transcript 里的 assistant 消息可能来自别的模型：用户中途 `/model` 换了模型，或者压缩、分支之后继续。别的模型产生的内容块，当前 API 不一定接受。

第一遍，逐条修正：

源码：[`packages/ai/src/api/transform-messages.ts:91-125`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/transform-messages.ts#L91-L125)

```ts
export function transformMessages<TApi extends Api>(
	messages: Message[],
	model: Model<TApi>,
	normalizeToolCallId?: (id: string, model: Model<TApi>, source: AssistantMessage) => string,
): Message[] {
	// Build a map of original tool call IDs to normalized IDs
	const toolCallIdMap = new Map<string, string>();
	// Normalize null/undefined content from untyped callers (custom tools, hand-built
	// histories, old session files) so downstream code can rely on the type contract.
	const normalizedMessages = messages.map((msg) => (msg.content == null ? { ...msg, content: [] } : msg));
	const imageAwareMessages = downgradeUnsupportedImages(normalizedMessages, model);

	// First pass: transform messages (unsupported image downgrade, thinking blocks, tool call ID normalization)
	const transformed = imageAwareMessages.map((msg) => {
		// System and user messages pass through unchanged
		if (msg.role === "system" || msg.role === "user") {
			return msg;
		}

		// Handle toolResult messages - normalize toolCallId if we have a mapping
		if (msg.role === "toolResult") {
			const normalizedId = toolCallIdMap.get(msg.toolCallId);
			if (normalizedId && normalizedId !== msg.toolCallId) {
				return { ...msg, toolCallId: normalizedId };
			}
			return msg;
		}

		// Assistant messages need transformation check
		if (msg.role === "assistant") {
			const assistantMsg = msg as AssistantMessage;
			const isSameModel =
				assistantMsg.provider === model.provider &&
				assistantMsg.api === model.api &&
				assistantMsg.model === model.id;
```

源码：[`packages/ai/src/api/transform-messages.ts:127-177`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/transform-messages.ts#L127-L177)

```ts
			const transformedContent = assistantMsg.content.flatMap((block) => {
				if (block.type === "thinking") {
					// Thinking from another model is dropped. Signatures and encrypted reasoning are
					// only valid for the model that produced them, and sending readable thinking as
					// plain text makes the new model treat it as part of the visible reply.
					if (!isSameModel) return [];
					// Keep redacted and signed blocks even if the thinking text is empty
					// (OpenAI encrypted reasoning); they are needed for replay.
					if (block.redacted || block.thinkingSignature) return block;
					if (!block.thinking || block.thinking.trim() === "") return [];
					return block;
				}

				if (block.type === "text") {
					if (isSameModel) return block;
					return {
						type: "text" as const,
						text: block.text,
					};
				}

				if (block.type === "toolCall") {
					const toolCall = block as ToolCall;
					let normalizedToolCall: ToolCall = toolCall;

					if (!isSameModel && toolCall.thoughtSignature) {
						normalizedToolCall = { ...toolCall };
						delete (normalizedToolCall as { thoughtSignature?: string }).thoughtSignature;
					}

					if (!isSameModel && normalizeToolCallId) {
						const normalizedId = normalizeToolCallId(toolCall.id, model, assistantMsg);
						if (normalizedId !== toolCall.id) {
							toolCallIdMap.set(toolCall.id, normalizedId);
							normalizedToolCall = { ...normalizedToolCall, id: normalizedId };
						}
					}

					return normalizedToolCall;
				}

				return block;
			});

			return {
				...assistantMsg,
				content: transformedContent,
			};
		}
		return msg;
	});
```

**讲解**

判断依据是 `isSameModel`：assistant 消息的 `provider`、`api`、`model` 三者都和当前模型相同。

| 内容 | 同一个模型 | 不同模型 |
|---|---|---|
| `thinking` 块 | 有签名或 `redacted` 就保留，空的丢掉 | 丢掉。签名只对产生它的模型有效；把别人的思考文本当普通文本发出去，新模型会把它当成可见回复的一部分 |
| `text` 块 | 原样 | 去掉 `textSignature`，只留 `type` 和 `text` |
| `toolCall` 块 | 原样 | 去掉 Google 的 `thoughtSignature`；用 `normalizeToolCallId` 改写 id，并记进 `toolCallIdMap` |
| `toolResult` 消息 | 原样 | 按 `toolCallIdMap` 改写 `toolCallId`，和改写后的工具调用保持一致 |

id 改写的原因写在函数注释里：OpenAI Responses 生成的工具调用 id 有 450 多个字符，含 `|` 等字符；Anthropic 要求 id 匹配 `^[a-zA-Z0-9_-]+$` 且不超过 64 个字符。Anthropic 的 `normalizeToolCallId`（`packages/ai/src/api/anthropic-messages.ts:1330`）把非法字符换成 `_` 再截断。

第一遍之前还有一步 `downgradeUnsupportedImages`（`packages/ai/src/api/transform-messages.ts:62`）：模型不支持图片输入时，把图片换成一行说明文字。

第二遍，补齐工具结果：

源码：[`packages/ai/src/api/transform-messages.ts:179-207`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/transform-messages.ts#L179-L207)

```ts
	// Second pass: insert synthetic empty tool results for orphaned tool calls
	// This preserves thinking signatures and satisfies API requirements
	const result: Message[] = [];
	let pendingToolCalls: ToolCall[] = [];
	let existingToolResultIds = new Set<string>();
	// System messages are transparent to tool-call accounting: one that lands between a tool
	// call and its results is held back and emitted after the results (synthetic ones
	// included), so it never causes a duplicate result for a call that is answered later.
	const heldSystemMessages: Message[] = [];
	const closePendingToolCalls = () => {
		if (pendingToolCalls.length > 0) {
			for (const tc of pendingToolCalls) {
				if (!existingToolResultIds.has(tc.id)) {
					result.push({
						role: "toolResult",
						toolCallId: tc.id,
						toolName: tc.name,
						content: [{ type: "text", text: "No result provided" }],
						isError: true,
						timestamp: Date.now(),
					} as ToolResultMessage);
				}
			}
			pendingToolCalls = [];
			existingToolResultIds = new Set();
		}
		result.push(...heldSystemMessages);
		heldSystemMessages.length = 0;
	};
```

源码：[`packages/ai/src/api/transform-messages.ts:209-256`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/transform-messages.ts#L209-L256)

```ts
	for (let i = 0; i < transformed.length; i++) {
		const msg = transformed[i];

		if (msg.role === "assistant") {
			// If we have pending orphaned tool calls from a previous assistant, insert synthetic results now
			closePendingToolCalls();

			// Skip errored/aborted assistant messages entirely.
			// These are incomplete turns that shouldn't be replayed:
			// - May have partial content (reasoning without message, incomplete tool calls)
			// - Replaying them can cause API errors (e.g., OpenAI "reasoning without following item")
			// - The model should retry from the last valid state
			const assistantMsg = msg as AssistantMessage;
			if (assistantMsg.stopReason === "error" || assistantMsg.stopReason === "aborted") {
				continue;
			}

			// Track tool calls from this assistant message
			const toolCalls = assistantMsg.content.filter((b) => b.type === "toolCall") as ToolCall[];
			if (toolCalls.length > 0) {
				pendingToolCalls = toolCalls;
				existingToolResultIds = new Set();
			}

			result.push(msg);
		} else if (msg.role === "toolResult") {
			existingToolResultIds.add(msg.toolCallId);
			result.push(msg);
		} else if (msg.role === "system") {
			if (pendingToolCalls.length > 0) {
				heldSystemMessages.push(msg);
			} else {
				result.push(msg);
			}
		} else if (msg.role === "user") {
			// A new user turn interrupts tool flow - insert synthetic results for orphaned calls
			closePendingToolCalls();
			result.push(msg);
		} else {
			result.push(msg);
		}
	}

	// If the conversation ends with unresolved tool calls, synthesize results now.
	closePendingToolCalls();

	return result;
}
```

**讲解**

几乎所有 API 都要求"每个工具调用后面都有对应的工具结果"。第二遍保证这一点：

- 遇到 assistant 消息，记下它的工具调用，作为"待回答"。
- 遇到下一条 assistant 或 user 消息，或者到了末尾，就给还没有结果的工具调用补一条 `isError: true`、内容为 `"No result provided"` 的结果。进程在工具执行时退出、用户在工具执行时插话，都会留下这种没有结果的调用。
- `stopReason` 是 `error` 或 `aborted` 的 assistant 消息整条跳过。它们可能只有半截内容，回放会让某些 API 报错，模型应该从上一个完整状态重新开始。
- 夹在工具调用和工具结果之间的 system 消息先扣住，等工具结果（包括补上的）都输出后再放出来。

`lab/m1-transcript.ts transform` 构造了一段包含所有这些情况的历史，发给 `claude-sonnet-4-5`：

```bash
node --conditions=source --experimental-strip-types m1-transcript.ts transform
```

```
输入：
  system     content="You are pi."
  user       "读 README.md"
  assistant  openai/gpt-5 stop=toolUse [thinking+sig, text+sig, toolCall(call_1|fc_abc)]
  toolResult toolCallId=call_1|fc_abc isError=false "# Demo"
  assistant  anthropic/claude-sonnet-4-5 stop=toolUse [thinking+sig, toolCall(toolu_2), toolCall(toolu_3)]
  system     content="" sections={"rules":"x"}
  toolResult toolCallId=toolu_2 isError=false "A"
  user       "继续"
  assistant  anthropic/claude-sonnet-4-5 stop=error [text]
  user       "再试一次"
transformMessages(messages, anthropic/claude-sonnet-4-5, normalizeToolCallId) 输出：
  system     content="You are pi."
  user       "读 README.md"
  assistant  openai/gpt-5 stop=toolUse [text, toolCall(call_1_fc_abc)]
  toolResult toolCallId=call_1_fc_abc isError=false "# Demo"
  assistant  anthropic/claude-sonnet-4-5 stop=toolUse [thinking+sig, toolCall(toolu_2), toolCall(toolu_3)]
  toolResult toolCallId=toolu_2 isError=false "A"
  toolResult toolCallId=toolu_3 isError=true "No result provided"
  system     content="" sections={"rules":"x"}
  user       "继续"
  user       "再试一次"
```

逐条对照：

- `openai/gpt-5` 的回复：`thinking+sig` 被丢掉，`text+sig` 的签名被去掉，`call_1|fc_abc` 变成 `call_1_fc_abc`，它的工具结果也跟着改了。
- `anthropic/claude-sonnet-4-5` 的回复和目标模型相同，`thinking+sig` 保留。
- `toolu_3` 没有结果，在 `user "继续"` 之前补了一条 `isError=true "No result provided"`；原来夹在中间的 system 消息被移到了补上的结果之后。
- `stop=error` 的回复被删掉了。

**往哪走**：§4.14。

### 4.14 provider 差异点

前面读的是一条请求怎么走完。这一节换个角度：同一份 transcript 发给不同的模型，pi-ai 在哪几处按模型的能力做了不同处理。共有五处。

#### 4.14.1 跨 provider 的 thinking 签名和工具调用 id

见 §4.13。总结成一句：别的模型留下的 thinking 和签名一律丢掉，工具调用 id 按当前 API 的规则改写，工具结果的 id 同步改写。每个 API 实现传入自己的 id 规则，Anthropic 的是"只留 `[a-zA-Z0-9_-]`，最长 64"。

#### 4.14.2 对话中途的 system 消息：resolveTranscript 和 collapseSystemMessages

M6 和 M8 会在对话中途追加 system 消息，例如切换 skill 后更新某个提示词段落，或者 MCP 服务器连上后新增工具。有的 API 接受对话中间的 system 消息，有的只接受开头一条。

源码：[`packages/ai/src/utils/transcript.ts:68-120`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/transcript.ts#L68-L120)

```ts
/**
 * Replay every system message into one leading system message holding the current
 * prompt and tools. Later `content` is appended to the base prompt, `sections` are
 * patched by name, and tools are resolved with {@link getCurrentTools}.
 */
export function getCurrentSystemMessage(messages: TranscriptMessages): SystemMessage | undefined {
	const content: string[] = [];
	const sections = new Map<string, string>();
	let timestamp: number | undefined;
	for (const message of messages) {
		if (!isSystemMessage(message)) continue;
		timestamp ??= message.timestamp;
		const text = contentText(message.content);
		if (text.length > 0) content.push(text);
		for (const [name, value] of Object.entries(message.sections ?? {})) {
			if (value === null) sections.delete(name);
			else sections.set(name, value);
		}
	}
	const tools = getCurrentTools(messages);
	if (timestamp === undefined && tools.length === 0) return undefined;
	return {
		role: "system",
		content: content.join("\n\n"),
		...(sections.size > 0 ? { sections: Object.fromEntries(sections) } : {}),
		...(tools.length > 0 ? { toolsAdded: tools } : {}),
		timestamp: timestamp ?? 0,
	};
}

/** Render the current system prompt text after replaying every system message. */
export function getCurrentSystemPrompt(messages: TranscriptMessages): string {
	const message = getCurrentSystemMessage(messages);
	return message ? getSystemMessageText(message) : "";
}

/**
 * Rebuild the transcript for APIs without mid-conversation system messages: the replayed
 * system message leads, and every later system message is dropped.
 */
export function collapseSystemMessages(context: TranscriptContext): TranscriptContext {
	const head = getCurrentSystemMessage(context.messages);
	const messages = context.messages.filter((message) => message.role !== "system");
	return { messages: head ? [head, ...messages] : messages } as TranscriptContext;
}

/** Keep later system messages in place when the model accepts them; otherwise collapse them. */
export function resolveTranscript(
	context: TranscriptContext,
	supportsMidConvoSystemMessages: boolean | undefined,
): TranscriptContext {
	return supportsMidConvoSystemMessages ? context : collapseSystemMessages(context);
}
```

`getCurrentSystemMessage` 重放所有 system 消息：`content` 依次追加，`sections` 按名字覆盖（`null` 删除），工具用 `getCurrentTools` 计算，`timestamp` 取第一条的。`collapseSystemMessages` 用重放结果替换开头一条，删掉其余的 system 消息。`resolveTranscript` 按模型的 `supportsMidConvoSystemMessages` 选择保留还是合并。

```bash
node --conditions=source --experimental-strip-types m1-transcript.ts collapse
```

```
normalizeContext 之后（provider 收到的 TranscriptContext）：
  system     content="You are pi." toolsAdded=[read,write]
  user       "第一个问题"
  system     content="" sections={"rules":"<rules>Answer in English.</rules>"} toolsAdded=[grep] toolsRemoved=[write]
  user       "第二个问题"
getCurrentTools：[read,grep]
resolveTranscript(context, true)，模型接受中途 system 消息，原样保留：
  system     content="You are pi." toolsAdded=[read,write]
  user       "第一个问题"
  system     content="" sections={"rules":"<rules>Answer in English.</rules>"} toolsAdded=[grep] toolsRemoved=[write]
  user       "第二个问题"
resolveTranscript(context, false)，即 collapseSystemMessages，重放成开头一条：
  system     content="You are pi." sections={"rules":"<rules>Answer in English.</rules>"} toolsAdded=[read,grep]
  user       "第一个问题"
  user       "第二个问题"
```

合并后，开头一条的 `rules` 段落是新值，工具清单是 `read,grep`（`write` 已删除）。代价是：模型看到的"过去的提示词"也被改成了新值，而且开头一条一变，提示词缓存从头失效。所以支持中途 system 消息的 API 会原样保留，只在变化发生的位置插入一条更新。

不同 API 实现的选择（在 `packages/ai/src/api/` 下 grep 得到）：Anthropic、OpenAI Completions/Responses、Mistral 按模型的 `compat.supportsMidConvoSystemMessages` 调用 `resolveTranscript`；Google 和 Bedrock 总是调用 `collapseSystemMessages`。Anthropic 的内置模型里，`claude-sonnet-4-5` 没有这个开关，`claude-opus-4-8` 打开了它（`packages/ai/src/providers/data/anthropic.json`）。§7.3 的实验把同一份历史发给这两个模型。

#### 4.14.3 工具清单的增量变更：resolveTranscriptTools

工具清单的变化也有同样的问题。大多数 API 的工具清单是请求顶层的一个字段 `tools`。每次工具变了就改这个字段，缓存从 `tools` 开始失效，而 `tools` 通常在请求的最前面。

源码：[`packages/ai/src/utils/transcript.ts:221-263`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/transcript.ts#L221-L263)

```ts
/**
 * How a transport can express tool changes after the first request.
 *
 * - `"none"`: only the top-level tool list exists; it always holds the current tools.
 * - `"additions"`: tools can be added at a later message, but not removed or redefined.
 * - `"changes"`: every tool, including the initial ones, is declared in place, and a later
 *   declaration under the same name replaces the earlier one. Removals are not expressible on
 *   the wire, so the transport announces them as text and the removed tools stay declared.
 */
export type TranscriptToolSupport = "none" | "additions" | "changes";

export interface TranscriptTools {
	/** Tools sent in the top-level request field. */
	requestTools: Tool[];
	/**
	 * Number of leading system messages whose tool changes `requestTools` already holds. Every
	 * later system message carries its own tool changes in place.
	 */
	foldedSystemMessages: number;
}

/**
 * Split tool declarations between the top-level request field and in-place changes.
 *
 * With `"changes"`, the top-level list stays empty and every system message, including the
 * leading one, declares its tools in place: an in-place declaration can replace an earlier
 * in-place one, but not a top-level one. With `"additions"`, a removal or redefinition cannot be
 * expressed in place, so the top-level list becomes the tool state after the last such change;
 * later additions stay anchored, and the cached prefix is invalidated once per removal instead
 * of on every later addition. With `"none"`, the top-level list is the current tool set.
 */
export function resolveTranscriptTools(messages: TranscriptMessages, support: TranscriptToolSupport): TranscriptTools {
	const systemMessages = messages.filter(isSystemMessage);
	if (support === "none") {
		return { requestTools: getCurrentTools(systemMessages), foldedSystemMessages: systemMessages.length };
	}
	if (support === "changes") return { requestTools: [], foldedSystemMessages: 0 };
	const folded = Math.max(
		getInitialSystemMessage(messages) ? 1 : 0,
		findLastNonAdditiveToolChange(systemMessages) + 1,
	);
	return { requestTools: getCurrentTools(systemMessages.slice(0, folded)), foldedSystemMessages: folded };
}
```

三种能力：

| 能力 | 含义 | `requestTools` |
|---|---|---|
| `"none"` | 只有顶层工具清单 | 当前的全部工具 |
| `"additions"` | 可以在后面的消息里新增工具，不能删除或重定义 | 截止到"最后一次删除或重定义"时的工具；之后的新增在原位置声明 |
| `"changes"` | 所有工具（包括初始工具）都在 system 消息里原位声明，后来的同名声明覆盖前面的 | 空 |

`foldedSystemMessages` 表示"前面几条 system 消息的工具已经包含在 `requestTools` 里了"，API 实现从第 `foldedSystemMessages` 条之后才原位声明工具。`"additions"` 遇到一次删除，就把顶层清单推进到删除之后的状态：缓存只在删除时失效一次，之后的新增不再影响它。

```bash
node --conditions=source --experimental-strip-types m1-transcript.ts tools
```

```
=== 只有新增：system 消息的工具变更 +[read,write] → +[grep]
  none      requestTools=[read,write,grep] foldedSystemMessages=2
  additions requestTools=[read,write] foldedSystemMessages=1
  changes   requestTools=[] foldedSystemMessages=0
=== 中间有一次删除：system 消息的工具变更 +[read,write] → +[grep] → +[] -[write] → +[ls]
  none      requestTools=[read,grep,ls] foldedSystemMessages=4
  additions requestTools=[read,grep] foldedSystemMessages=3
  changes   requestTools=[] foldedSystemMessages=0
```

OpenAI Completions 在模型同时支持中途 system 消息和中途新增工具时用 `"additions"`，否则用 `"none"`（`packages/ai/src/api/openai-completions.ts:796`）。Anthropic 没有调用这个函数，它有自己的原生方式：支持中途工具变更的模型，顶层 `tools` 只放初始工具加一个永远不会被调用的占位工具 `__pi_deferred_placeholder__`，之后的变化用 `tool_addition`/`tool_removal` 块写在中途的 system 消息里（`packages/ai/src/api/anthropic-messages.ts:1257`、`packages/ai/src/api/anthropic-messages.ts:1381`）。占位工具的作用写在它的注释里：

源码：[`packages/ai/src/api/anthropic-messages.ts:207-219`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L207-L219)

```ts
/**
 * Stable deferred tool declared whenever native tool changes are in use. Anthropic adds
 * hidden prompt scaffolding for mid-conversation tool changes; declaring this placeholder
 * from the first request keeps that scaffolding in the cached prefix, so the first tool
 * change does not invalidate the cache (measured: full miss without it). It is never
 * activated and the model cannot see it.
 */
const DEFERRED_TOOL_PLACEHOLDER: BetaTool = {
	name: "__pi_deferred_placeholder__",
	description: "Reserved placeholder. Never available. Never call this.",
	input_schema: { type: "object", properties: {}, required: [] },
	defer_loading: true,
};
```

#### 4.14.4 思考级别的映射

pi 的思考级别有 7 个：`off`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`（`packages/ai/src/providers/model-schema.ts:3`）。每家 API 的取值不同。映射分两层。

第一层在 pi-ai 通用代码里：模型不支持的级别先夹到最近的支持级别。

源码：[`packages/ai/src/models.ts:1259-1291`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/models.ts#L1259-L1291)

```ts
const EXTENDED_THINKING_LEVELS: ModelThinkingLevel[] = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

export function getSupportedThinkingLevels<TApi extends Api>(model: Model<TApi>): ModelThinkingLevel[] {
	if (!model.reasoning) return ["off"];

	return EXTENDED_THINKING_LEVELS.filter((level) => {
		const mapped = model.thinkingLevelMap?.[level];
		if (mapped === null) return false;
		if (level === "xhigh" || level === "max") return mapped !== undefined;
		return true;
	});
}

export function clampThinkingLevel<TApi extends Api>(
	model: Model<TApi>,
	level: ModelThinkingLevel,
): ModelThinkingLevel {
	const availableLevels = getSupportedThinkingLevels(model);
	if (availableLevels.includes(level)) return level;

	const requestedIndex = EXTENDED_THINKING_LEVELS.indexOf(level);
	if (requestedIndex === -1) return availableLevels[0] ?? "off";

	for (let i = requestedIndex; i < EXTENDED_THINKING_LEVELS.length; i++) {
		const candidate = EXTENDED_THINKING_LEVELS[i];
		if (availableLevels.includes(candidate)) return candidate;
	}
	for (let i = requestedIndex - 1; i >= 0; i--) {
		const candidate = EXTENDED_THINKING_LEVELS[i];
		if (availableLevels.includes(candidate)) return candidate;
	}
	return availableLevels[0] ?? "off";
}
```

`getSupportedThinkingLevels`：不能思考的模型只有 `off`；`thinkingLevelMap` 里值为 `null` 的级别不支持；`xhigh` 和 `max` 必须在 `thinkingLevelMap` 里显式出现才支持。`clampThinkingLevel` 先往高找，找不到再往低找。思考级别的设置和夹取在 coding-agent 的 `AgentSession` 里完成（→ M8）。

第二层在每个 API 实现里。Anthropic 有两种思考方式，`streamSimple`（§4.7）按 `compat.forceAdaptiveThinking` 选择。

自适应思考，级别换成 `effort`：

源码：[`packages/ai/src/api/anthropic-messages.ts:929-952`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L929-L952)

```ts
/**
 * Map ThinkingLevel to Anthropic effort levels for adaptive thinking.
 * Note: effort "max" is available on all adaptive-thinking Claude models, while native
 * "xhigh" is only available on Opus 4.7/4.8, Sonnet 5, and Fable 5.
 */
function mapThinkingLevelToEffort(
	model: Model<"anthropic-messages">,
	level: SimpleStreamOptions["reasoning"],
): AnthropicEffort {
	const mapped = level ? model.thinkingLevelMap?.[level] : undefined;
	if (typeof mapped === "string") return mapped as AnthropicEffort;

	switch (level) {
		case "minimal":
		case "low":
			return "low";
		case "medium":
			return "medium";
		case "high":
			return "high";
		default:
			return "high";
	}
}
```

`thinkingLevelMap` 里有字符串值时直接用（例如 `xhigh → "xhigh"`），否则 `minimal`/`low` → `low`，`medium` → `medium`，其余 → `high`。

预算思考，级别换成 token 预算：

源码：[`packages/ai/src/api/simple-options.ts:68-109`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/simple-options.ts#L68-L109)

```ts
/** Tokens always left for the answer when a thinking budget shares the response ceiling. */
export const MIN_ANSWER_TOKENS = 1024;

export const DEFAULT_THINKING_BUDGETS: ThinkingBudgets = {
	minimal: 1024,
	low: 2048,
	medium: 8192,
	high: 16384,
};

export function clampReasoning(effort: ThinkingLevel | undefined): Exclude<ThinkingLevel, "xhigh" | "max"> | undefined {
	return effort === "xhigh" || effort === "max" ? "high" : effort;
}

export function thinkingBudgetForLevel(reasoningLevel: ThinkingLevel, customBudgets?: ThinkingBudgets): number {
	const budgets = { ...DEFAULT_THINKING_BUDGETS, ...customBudgets };
	const level = clampReasoning(reasoningLevel)!;
	return budgets[level]!;
}

/** Cap a thinking budget so at least MIN_ANSWER_TOKENS remain under a shared response ceiling. */
export function clampThinkingBudgetToAnswerRoom(thinkingBudget: number, ceiling: number): number {
	return Math.min(thinkingBudget, Math.max(0, ceiling - MIN_ANSWER_TOKENS));
}

export function adjustMaxTokensForThinking(
	// Undefined means no explicit caller cap. Use the model cap and fit thinking inside it.
	baseMaxTokens: number | undefined,
	modelMaxTokens: number,
	reasoningLevel: ThinkingLevel,
	customBudgets?: ThinkingBudgets,
): { maxTokens: number; thinkingBudget: number } {
	let thinkingBudget = thinkingBudgetForLevel(reasoningLevel, customBudgets);
	const maxTokens =
		baseMaxTokens === undefined ? modelMaxTokens : Math.min(baseMaxTokens + thinkingBudget, modelMaxTokens);

	if (maxTokens <= thinkingBudget) {
		thinkingBudget = clampThinkingBudgetToAnswerRoom(thinkingBudget, maxTokens);
	}

	return { maxTokens, thinkingBudget };
}
```

默认预算是 `minimal` 1024、`low` 2048、`medium` 8192、`high` 16384，`xhigh`/`max` 按 `high` 算。用户可以通过 `thinkingBudgets` 设置覆盖（M2 的 `AgentLoopConfig` 透传）。调用方给了 `maxTokens` 时，`max_tokens` = `maxTokens` + 预算，再被模型上限截断；预算太大时缩小预算，至少给回答留 1024 个 token。

两种方式最后在 `buildParams` 里变成请求字段：

源码：[`packages/ai/src/api/anthropic-messages.ts:1270-1304`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L1270-L1304)

```ts
	if (thinkingDisabled) {
		// No effort is sent, so the model default applies. Anthropic rejects `block_binding`
		// with disabled thinking.
		params.thinking = { type: "disabled" };
	} else if (effortMessages) {
		// Managed effort models use adaptive thinking so prefix mismatches can
		// be dropped instead of surfacing as persistent 400 responses.
		params.thinking = {
			type: "adaptive",
			display: options?.thinkingDisplay ?? "summarized",
			block_binding: { prefix_mismatch_behavior: "drop_block" },
		};
		// Effort markers set the level for every reply. The top-level effort is part of the cached
		// prompt, so it stays fixed. It is "low" because organizations can cap effort and reject
		// requests containing any level above the cap.
		params.output_config = { effort: "low" };
	} else if (model.reasoning && options?.thinkingEnabled) {
		// Default to "summarized" so Opus 4.7 and Mythos Preview behave like
		// older Claude 4 models (whose API default is also "summarized").
		const display: AnthropicThinkingDisplay = options.thinkingDisplay ?? "summarized";
		if (model.compat?.forceAdaptiveThinking === true) {
			// Adaptive thinking: Claude decides when and how much to think.
			params.thinking = { type: "adaptive", display };
			if (options.effort) {
				params.output_config = { effort: options.effort };
			}
		} else {
			// Budget-based thinking for older models
			params.thinking = {
				type: "enabled",
				budget_tokens: options.thinkingBudgetTokens || 1024,
				display,
			};
		}
	}
```

第一个分支值得注意：`reasoning` 为空时，能思考的模型会收到 `thinking: { type: "disabled" }`，而不是不发 `thinking` 字段。第二个分支是"可以在对话中途改变 effort"的模型（`supportsMidConvoEffort`）：顶层 `output_config.effort` 固定为 `"low"`，真正的级别写在消息列表末尾一条 `output_config` system 消息里（`insertThinkingLevelMessages`，`packages/ai/src/api/anthropic-messages.ts:1561`）。这样改思考级别不会改变请求的前缀，缓存不失效。这次用的级别写进回复的 `providerThinkingLevel`，下次请求时插在这条回复之前，历史上的每条回复都保留当时的级别。§7.2 的实验比较了三类模型。

#### 4.14.5 提示词缓存

提示词缓存让 provider 复用相同前缀的计算结果，命中部分按 `cacheRead` 计费，比普通输入便宜很多。调用方用 `StreamOptions.cacheRetention` 表达偏好：`"none"`、`"short"`（默认）、`"long"`。每家 API 自己决定怎样实现。

Anthropic 需要在请求里标出缓存断点 `cache_control`：

源码：[`packages/ai/src/api/anthropic-messages.ts:71-99`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L71-L99)

```ts
/**
 * Resolve cache retention preference.
 * Defaults to "short" and uses PI_CACHE_RETENTION for backward compatibility.
 */
function resolveCacheRetention(cacheRetention?: CacheRetention, env?: ProviderEnv): CacheRetention {
	if (cacheRetention) {
		return cacheRetention;
	}
	if (getProviderEnvValue("PI_CACHE_RETENTION", env) === "long") {
		return "long";
	}
	return "short";
}

function getCacheControl(
	model: Model<"anthropic-messages">,
	cacheRetention?: CacheRetention,
	env?: ProviderEnv,
): { retention: CacheRetention; cacheControl?: CacheControlEphemeral } {
	const retention = resolveCacheRetention(cacheRetention, env);
	if (retention === "none") {
		return { retention };
	}
	const ttl = retention === "long" && getAnthropicCompat(model).supportsLongCacheRetention ? "1h" : undefined;
	return {
		retention,
		cacheControl: { type: "ephemeral", ...(ttl && { ttl }) },
	};
}
```

`long` 在模型支持时加上 `ttl: "1h"`，`none` 时不加任何断点。环境变量 `PI_CACHE_RETENTION=long` 是旧的设置方式。断点放在三个位置：系统提示词（1237-1243 行）、最后一个工具（`convertTools` 1655 行）、最后一条 user 或 system 消息的最后一个块：

源码：[`packages/ai/src/api/anthropic-messages.ts:1526-1552`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/api/anthropic-messages.ts#L1526-L1552)

```ts
	// Add cache_control to the last user or system message to cache conversation history
	if (cacheControl && params.length > 0) {
		const lastMessage = params[params.length - 1];
		if (lastMessage.role === "user" || lastMessage.role === "system") {
			if (Array.isArray(lastMessage.content)) {
				const lastBlock = lastMessage.content[lastMessage.content.length - 1];
				if (
					lastBlock &&
					(lastBlock.type === "text" ||
						lastBlock.type === "image" ||
						lastBlock.type === "tool_result" ||
						lastBlock.type === "tool_addition" ||
						lastBlock.type === "tool_removal")
				) {
					(lastBlock as any).cache_control = cacheControl;
				}
			} else if (typeof lastMessage.content === "string") {
				lastMessage.content = [
					{
						type: "text",
						text: lastMessage.content,
						cache_control: cacheControl,
					},
				] as any;
			}
		}
	}
```

最后一个断点每次请求都往后移，所以下一次请求能读到这一次写入的整个前缀。

```bash
node --conditions=source --experimental-strip-types m1-anthropic.ts cache
```

```
cacheRetention=undefined cache_control 位置=["system[0]","tools.read","messages[0].content[0]"] 取值={"type":"ephemeral"}
  usage（来自 message_start/message_delta）：{"input":120,"output":42,"cacheRead":0,"cacheWrite":1000,"totalTokens":1162,"cost":{"input":0.00036,"output":0.00063,"cacheRead":0,"cacheWrite":0.00375,"total":0.004739999999999999,"totalFromProvider":false},"cacheWrite1h":0}
cacheRetention=long      cache_control 位置=["system[0]","tools.read","messages[0].content[0]"] 取值={"type":"ephemeral","ttl":"1h"}
cacheRetention=none      cache_control 位置=[] 取值=-
```

其他 API 的做法不同，例如 OpenAI Completions 不标断点，而是发送 `prompt_cache_key`（取自 `sessionId`）和 `prompt_cache_retention`（`packages/ai/src/api/openai-completions.ts:811`）。

`Model.promptCache` 记录了每一档缓存的寿命（Anthropic 模型是 `short: 300`、`long: 3600` 秒）。pi-ai 自己不读它；coding-agent 的缓存预热（`packages/coding-agent/src/core/cache-warmer.ts:39`）用它决定在缓存过期前多久发一次保温请求，这属于 M8 的产品功能。

前面 §4.14.2、§4.14.3、§4.14.4 的设计都和缓存有关：中途 system 消息、`"additions"` 工具声明、占位工具、固定的顶层 effort，目的都是让请求的前缀尽量不变。

**往哪走**：§4.15，最后看模型和 provider 是怎么注册进来的。

### 4.15 模型注册：ModelRuntime.create() 和 registerProvider()

**调用位置**：`createAgentSessionServices()` 调用 `ModelRuntime.create()`（→ M8）；扩展的 `pi.registerProvider()` 最终调用 `modelRuntime.registerProvider()`（→ M7）。

源码：[`packages/coding-agent/src/core/model-runtime.ts:218-252`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/model-runtime.ts#L218-L252)

```ts
	static async create(options: CreateModelRuntimeOptions = {}): Promise<ModelRuntime> {
		const credentials = new RuntimeCredentials(options.credentials ?? DefaultAuthStorage.create(options.authPath));
		const modelsPath =
			options.modelsPath === null ? undefined : (options.modelsPath ?? join(getAgentDir(), "models.json"));
		const config = await ModelConfig.load(modelsPath);
		const modelsStore =
			options.modelsStore ??
			(modelsPath
				? new FileModelsStore(options.modelsStorePath ?? join(dirname(modelsPath), "models-store.json"))
				: new InMemoryCodingAgentModelsStore());
		const builtinModelDataGeneratedAt = builtinProviderCatalog.getBuiltinModelDataGeneratedAt();
		const providers = builtinProviderCatalog.builtinProviders().map((provider) => {
			if (provider.id === "radius") return provider;
			// OpenRouter narrows the catalog to the models, limits and prices of the user's key.
			if (provider.id === "openrouter") {
				return withPersonalizedCatalog(
					provider,
					personalizeOpenRouterCatalog,
					options.catalogBaseUrl,
					builtinModelDataGeneratedAt,
				);
			}
			return withRemoteCatalog(provider, options.catalogBaseUrl, builtinModelDataGeneratedAt);
		});
		const runtime = new ModelRuntime(
			credentials,
			config,
			modelsPath,
			modelsStore,
			providers,
			process.env.PI_OFFLINE === undefined,
		);
		runtime.configureRadiusProviders();
		runtime.rebuildProviders();
		const refreshFromNetwork = runtime.modelNetworkEnabled && options.allowModelNetwork === true;
```

**讲解**

`create()` 做了这些事：

1. 准备凭证存储（默认 `~/.pi/agent/auth.json`，实验里由 `PI_CODING_AGENT_DIR` 改到 `/tmp`）、`models.json` 配置、模型目录缓存。
2. 取出全部内置 provider（`builtinProviderCatalog.builtinProviders()`，即 `packages/ai/src/providers/all.ts`）。除 radius 外，每个 provider 都包上远程目录（`withRemoteCatalog`），可以从网络刷新模型列表。模型目录的生成和刷新不在本模块范围内。
3. 构造 `ModelRuntime`，`rebuildProviders()` 把每个 provider id 组合好放进 `this.models`（§4.4）。
4. 之后（253-268 行）默认调用一次 `refresh()`，恢复缓存的目录并检查哪些 provider 已配置凭证。只有没设置 `PI_OFFLINE` 并且 `allowModelNetwork` 为 `true` 时才联网（252 行）。

扩展注册 provider 时传入的配置是 `ProviderConfigInput`：

源码：[`packages/coding-agent/src/core/provider-composer.ts:92-110`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/provider-composer.ts#L92-L110)

```ts
/** Input type for the extension registerProvider API. */
export interface ProviderConfigInput {
	name?: string;
	baseUrl?: string;
	apiKey?: string;
	api?: Api;
	streamSimple?: (
		model: Model<Api>,
		context: TranscriptContext,
		options?: SimpleStreamOptions,
	) => AssistantMessageEventStream;
	images?: Partial<Record<ImageApi, ProviderImages>>;
	classifiers?: Partial<Record<ClassifierApi, ProviderClassifier>>;
	headers?: Record<string, string>;
	authHeader?: boolean;
	oauth?: ExtensionOAuthConfig;
	models?: ProviderModelConfig[];
	refreshModels?(context: RefreshModelsContext): Promise<ProviderModelConfig[]>;
}
```

`lab/faux-ext.ts` 传了 `api`、`baseUrl`、`apiKey`、`models`、`streamSimple` 五项。`registerProvider` 的处理：

源码：[`packages/coding-agent/src/core/model-runtime.ts:936-957`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/model-runtime.ts#L936-L957)

```ts
	registerProvider(providerId: string, config: ProviderConfigInput): void {
		// Validate the incoming registration on its own, like the legacy registry:
		// a broken re-registration must throw without touching the stored config.
		validateExtensionProvider(providerId, this.builtins.get(providerId), this.config.getProvider(providerId), config);
		this.nativeExtensionProviders.delete(providerId);
		// Re-registration merges defined values over the previous registration and
		// preserves undefined ones, matching the legacy ModelRegistry contract.
		const previous = this.extensionProviders.get(providerId);
		const effective: ProviderConfigInput = { ...previous };
		for (const [key, value] of Object.entries(config)) {
			if (value !== undefined) (effective as Record<string, unknown>)[key] = value;
		}
		this.extensionProviders.set(providerId, effective);
		this.recomposeProvider(providerId);
		this.updateModelSnapshot();
		this.markProvisionallyConfigured(
			providerId,
			configuredRequestAuthStatus(this.config.getProvider(providerId), effective),
			effective.oauth && !effective.apiKey ? "oauth" : "api_key",
		);
		void this.refresh({ allowNetwork: false });
	}
```

1. `validateExtensionProvider`：给了 `streamSimple` 就必须给 `api`（`composeModelProvider` 靠 `api` 判断哪些模型用这个函数）；再试着组合一次模型列表，结构错误立即抛出。
2. 和之前的注册合并：新配置里不是 `undefined` 的字段覆盖旧值。
3. `recomposeProvider`：重新组合这个 provider，替换 `this.models` 里的旧对象。之后 `getModel("faux", "faux-1")` 就能找到它，`streamSimple` 走 §4.4 的 `streamWith`。
4. `markProvisionallyConfigured`：配置里有 `apiKey`，就先把这个 provider 标记为已配置。真正的可用性检查是异步的，而 pi 启动时可能在检查完成前就要选默认模型，例如 `--provider faux --model faux-1`。
5. 后台调用一次不联网的 `refresh()`。

**往哪走**：M1 的代码读完了。M2 从 `streamFn` 拿到这个流之后怎样消费，见 M2 §4.6。

---

## 5. 状态机

### 5.1 一个 AssistantMessageEventStream 的一生

```
                  streamSimple() 同步返回
                          │
                          ▼
                     ┌─────────┐  setup() 抛异常（lazy.ts:76）
                     │  创建   │──────────────────────────────┐
                     └────┬────┘                              │
            HTTP 响应到达   │ push(start)                       │
       anthropic-messages.ts:693 / faux.ts:374                 │
                          ▼                                   │
                     ┌─────────┐  任何异常 → catch            │
                     │ 流式中  │  anthropic-messages.ts:918   │
                     │ (块事件)│──────────────────────┐       │
                     └────┬────┘                      │       │
       检查通过 push(done) │ anthropic-messages.ts:916 │       │
                          ▼                           ▼       ▼
                     ┌─────────┐                  ┌─────────────┐
                     │  done   │                  │    error    │  reason = aborted | error
                     └────┬────┘                  └──────┬──────┘
                          └──── push 时 resolve result()（event-stream.ts:46-49），
                                之后的 push 被忽略（event-stream.ts:44）
                                end() 唤醒等待的消费者（event-stream.ts:60-70）
```

`error` 可以从"创建"直接到达（准备阶段失败、HTTP 错误，没有 `start`），也可以从"流式中"到达（已经发出的事件不会撤回）。

### 5.2 一个内容块

```
   content_block_start ──▶ *_start      anthropic-messages.ts:735-770
          │                 output.content.push(空块)，openBlocks.set(index)
          ▼
   content_block_delta ──▶ *_delta      anthropic-messages.ts:771-808
          │ (0..N 次)       块内容 += delta；工具调用 arguments = ToolCallArgumentsStream.append()
          │                 signature_delta 只改 thinkingSignature，不发事件（803-807）
          ▼
   content_block_stop  ──▶ *_end        anthropic-messages.ts:809-837
                            openBlocks.delete(index)；工具调用 Object.assign(block, finish())（829）
```

### 5.3 AssistantMessage.stopReason

```
   "pending"（anthropic-messages.ts:601）
      │ message_delta.stop_reason（841-848，mapStopReason 1660）
      ├── end_turn / pause_turn / stop_sequence ─▶ "stop"
      ├── max_tokens ─────────────────────────────▶ "length"
      ├── tool_use ───────────────────────────────▶ "toolUse"
      └── refusal / sensitive ────────────────────▶ "error" ─┐
   流结束时仍是 "pending"（896）──▶ 抛异常 ───────────────────┤
   signal 已中止（892）──▶ 抛异常 ─────────────────────────────┤
                                                              ▼
                                   catch（918-919）："aborted"（signal 已中止）或 "error"
```

---

## 6. 设计要点

### 6.1 失败是流里的最后一个事件

`streamSimple` 同步返回一个流，所有失败都以 `error` 事件结束这个流。实现这一点的有三层：`lazyStream` 接住准备阶段的异常（`packages/ai/src/api/lazy.ts:76`），`createProvider` 对没有实现的 API 也返回 `error` 流（`packages/ai/src/models.ts:1113`），每个 API 实现用 `try/catch` 包住整个请求（`packages/ai/src/api/anthropic-messages.ts:918`）。

好处是调用方只有一条处理路径。M2 不用 `try/catch`，只要看最终消息的 `stopReason`；错误消息和正常消息一样进入 transcript、写进会话文件，M8 的自动重试再根据 `errorMessage` 判断要不要重试。

### 6.2 系统提示词和工具清单写在 transcript 里

`TranscriptContext` 只有 `messages`。系统提示词和工具是 system 消息，可以出现在对话的任何位置。这让"对话中途改提示词、加工具"成为普通的追加操作，会话文件不需要单独的字段（M4），压缩和分支也不用特殊处理（M5）。代价由 provider 承担：每个 API 实现都要用 `resolveTranscript` 或 `collapseSystemMessages` 把它变回自己能接受的形状（§4.14.2）。`TranscriptContext` 的类型标记（`packages/ai/src/types.ts:800`）保证没有经过 `normalizeContext` 的输入到不了 provider。

### 6.3 按能力声明适配，而不是按 provider 名字

各 API 实现读 `model.compat` 里的开关：`supportsMidConvoSystemMessages`、`supportsMidConvoToolChanges`、`forceAdaptiveThinking`、`supportsMidConvoEffort`、`supportsLongCacheRetention` 等（Anthropic 的默认值见 `packages/ai/src/api/anthropic-messages.ts:225`）。同一个 API 下的不同模型、不同的兼容服务（OpenRouter、Vercel AI Gateway 也提供 Anthropic 格式的接口）通过开关区分。新模型上线时通常只需要改模型目录里的开关，不需要改代码。

### 6.4 尽量保持请求前缀不变

提示词缓存按前缀命中。pi-ai 的几处设计都是为了让前缀稳定：中途的 system 消息原位插入而不是改写开头（§4.14.2）；工具清单在只有新增时不改顶层字段（§4.14.3）；Anthropic 的占位工具让第一次工具变更也不打破缓存（`packages/ai/src/api/anthropic-messages.ts:214`）；可变的 effort 写在消息末尾，顶层固定为 `"low"`（`packages/ai/src/api/anthropic-messages.ts:1285`）。

### 6.5 流式解析不污染数据

流式过程中需要的临时状态都放在内容块之外：Anthropic 的 `openBlocks` 表（`packages/ai/src/api/anthropic-messages.ts:696`）、工具参数的原始文本（`ToolCallArgumentsStream`，`packages/ai/src/utils/streaming-json.ts:452`）。内容块上只有最终会保存的字段，所以 `partial` 可以直接作为界面数据，最终消息可以直接写进会话文件，不需要再清理。

---

## 7. 实验

### 7.1 partial 不是快照

**预测**：§2.2 的 `events` 场景给 faux 设了 `tokensPerSecond: 1000`，每片之间等 1ms，消费者每次都追得上，所以每个 `toolcall_delta` 读到的 `arguments` 都在增长。`live` 场景去掉这个设置，faux 只在片之间等一个微任务。消费者是 `for await`，每取一个事件至少要经过一次 Promise 回调和两层 `lazyStream` 转发，会落后于生产者。读到的 `partial` 会是什么？

```bash
PI_CODING_AGENT_DIR=/tmp/pi-lab/m1-agent node --conditions=source --experimental-strip-types m1-runtime.ts live | grep '\[事件\]'
```

```
[事件] start          partial.content=[] stopReason=pending
[事件] text_start     [0]
[事件] text_delta     [0] delta="先读文件"       partial 文本="先读文件。"
[事件] text_delta     [0] delta="。"          partial 文本="先读文件。"
[事件] text_end       [0] content="先读文件。"
[事件] toolcall_start [1]
[事件] toolcall_delta [1] delta="{\"pa"      arguments={"path":"README.md","limit":20}
[事件] toolcall_delta [1] delta="th\":"      arguments={"path":"README.md","limit":20}
[事件] toolcall_delta [1] delta="\"REA"      arguments={"path":"README.md","limit":20}
[事件] toolcall_delta [1] delta="DME."       arguments={"path":"README.md","limit":20}
[事件] toolcall_delta [1] delta="md\","      arguments={"path":"README.md","limit":20}
[事件] toolcall_delta [1] delta="\"lim"      arguments={"path":"README.md","limit":20}
[事件] toolcall_delta [1] delta="it\":"      arguments={"path":"README.md","limit":20}
[事件] toolcall_delta [1] delta="20}"        arguments={"path":"README.md","limit":20}
[事件] toolcall_end   [1] toolCall={"type":"toolCall","id":"call_1","name":"read","arguments":{"path":"README.md","limit":20}}
[事件] done           reason=toolUse
```

**结果**：第一个 `text_delta` 的 `delta` 是 `"先读文件"`，但 `partial` 里的文本已经是完整的 `"先读文件。"`；每个 `toolcall_delta` 读到的 `arguments` 都已经是完整对象。事件本身（`delta` 字段）没有丢，也没有乱序，只是 `partial` 指向的对象已经被后面的片段修改了。所以消费者应该用 `delta` 增量更新自己的状态，或者把 `partial` 只当作"当前最新值"，不要当作"这个事件发生时的值"。M2 每次收到事件就把 `partial` 当作最新消息（M2 §4.6），符合这个用法。

### 7.2 同一个思考级别，三类模型收到什么

**预测**：`claude-sonnet-4-5` 是预算思考，`claude-opus-4-8` 是自适应思考，`claude-opus-5` 是自适应思考并支持中途改 effort。调用方都传 `maxTokens: 32000`。根据 §4.14.4，`reasoning` 分别为空、`minimal`、`medium`、`high`、`xhigh` 时，三个模型的 `max_tokens`、`thinking`、`output_config` 各是什么？

```bash
node --conditions=source --experimental-strip-types m1-anthropic.ts thinking
```

```
=== claude-sonnet-4-5 maxTokens=64000 forceAdaptiveThinking=false supportsMidConvoEffort=false thinkingLevelMap={}
  reasoning=undefined max_tokens=32000  thinking={"type":"disabled"} output_config=undefined
  reasoning=minimal   max_tokens=33024  thinking={"type":"enabled","budget_tokens":1024,"display":"summarized"} output_config=undefined
  reasoning=medium    max_tokens=40192  thinking={"type":"enabled","budget_tokens":8192,"display":"summarized"} output_config=undefined
  reasoning=high      max_tokens=48384  thinking={"type":"enabled","budget_tokens":16384,"display":"summarized"} output_config=undefined
  reasoning=xhigh     max_tokens=48384  thinking={"type":"enabled","budget_tokens":16384,"display":"summarized"} output_config=undefined
=== claude-opus-4-8 maxTokens=128000 forceAdaptiveThinking=true supportsMidConvoEffort=false thinkingLevelMap={"xhigh":"xhigh","max":"max"}
  reasoning=undefined max_tokens=32000  thinking={"type":"disabled"} output_config=undefined
  reasoning=minimal   max_tokens=32000  thinking={"type":"adaptive","display":"summarized"} output_config={"effort":"low"}
  reasoning=medium    max_tokens=32000  thinking={"type":"adaptive","display":"summarized"} output_config={"effort":"medium"}
  reasoning=high      max_tokens=32000  thinking={"type":"adaptive","display":"summarized"} output_config={"effort":"high"}
  reasoning=xhigh     max_tokens=32000  thinking={"type":"adaptive","display":"summarized"} output_config={"effort":"xhigh"}
=== claude-opus-5 maxTokens=128000 forceAdaptiveThinking=true supportsMidConvoEffort=true thinkingLevelMap={"xhigh":"xhigh","max":"max"}
  reasoning=undefined max_tokens=32000  thinking={"type":"disabled"} output_config=undefined
  reasoning=minimal   max_tokens=32000  thinking={"type":"adaptive","display":"summarized","block_binding":{"prefix_mismatch_behavior":"drop_block"}} output_config={"effort":"low"} effort 标记=["low"] providerThinkingLevel=low
  reasoning=medium    max_tokens=32000  thinking={"type":"adaptive","display":"summarized","block_binding":{"prefix_mismatch_behavior":"drop_block"}} output_config={"effort":"low"} effort 标记=["medium"] providerThinkingLevel=medium
  reasoning=high      max_tokens=32000  thinking={"type":"adaptive","display":"summarized","block_binding":{"prefix_mismatch_behavior":"drop_block"}} output_config={"effort":"low"} effort 标记=["high"] providerThinkingLevel=high
  reasoning=xhigh     max_tokens=32000  thinking={"type":"adaptive","display":"summarized","block_binding":{"prefix_mismatch_behavior":"drop_block"}} output_config={"effort":"low"} effort 标记=["xhigh"] providerThinkingLevel=xhigh
```

**结果**：

- 三个模型在 `reasoning` 为空时都是 `thinking: {"type":"disabled"}`，`max_tokens` 保持 32000。
- `claude-sonnet-4-5`：`max_tokens` = 32000 + 预算，预算依次是 1024、8192、16384；`xhigh` 被当作 `high`，和 `high` 完全相同。
- `claude-opus-4-8`：`max_tokens` 不变，`minimal` 变成 `effort: "low"`；它的 `thinkingLevelMap` 里有 `xhigh`，所以 `xhigh` 原样发送。
- `claude-opus-5`：顶层 `output_config.effort` 始终是 `"low"`，实际级别在末尾的 effort 标记消息里，并作为 `providerThinkingLevel` 写进回复。

### 7.3 同一份历史，两个模型的请求体

**预测**：`lab/m1-anthropic.ts request` 构造了这样一段历史：开头 system 消息带 `rules` 段落"Be brief"和 `read` 工具；第 1 轮由 `openai/gpt-5` 回答（有签名的 thinking、工具调用 id `call_1|fc_0123456789abcdef`）；然后一条中途 system 消息把 `rules` 改成"Answer in English"并新增 `grep` 工具；最后用户说"总结一下"。分别发给 `claude-sonnet-4-5`（不支持中途 system 消息）和 `claude-opus-4-8`（支持中途 system 消息和工具变更）。两者的 `system`、`messages`、`tools` 有什么不同？

```bash
node --conditions=source --experimental-strip-types m1-anthropic.ts request
```

```
=== claude-sonnet-4-5（supportsMidConvoSystemMessages=false）
system: ["You are a test agent.\n\n<rules>Answer in English.</rules>"]
  user      text("读 README.md")
  assistant text("好的。") tool_use(id=call_1_fc_0123456789abcdef)
  user      tool_result(tool_use_id=call_1_fc_0123456789abcdef)
  user      text("总结一下")
tools: read, grep
anthropic-beta: null
=== claude-opus-4-8（supportsMidConvoSystemMessages=true）
system: ["You are a test agent.\n\n<rules>Be brief.</rules>"]
  user      text("读 README.md")
  assistant text("好的。") tool_use(id=call_1_fc_0123456789abcdef)
  user      tool_result(tool_use_id=call_1_fc_0123456789abcdef)
  user      text("总结一下")
  system    text("Updated system prompt section \"rules\":\n\n<rules>Answer in Eng") tool_addition(grep)
tools: read, __pi_deferred_placeholder__
anthropic-beta: inline-tools-2026-09-15
```

**结果**：

- 两个模型的 assistant 消息里都没有 thinking 块（来自别的模型，被 `transformMessages` 丢掉），工具调用 id 都被改写成 `call_1_fc_0123456789abcdef`。
- `claude-sonnet-4-5`：system 消息被合并，开头的 `system` 已经是新规则"Answer in English"，`tools` 是 `read, grep`，`messages` 里没有 system 消息。
- `claude-opus-4-8`：开头的 `system` 保持旧规则"Be brief"；新规则作为一条 `system` 消息出现在末尾（被推迟到最后一条 user 消息之后），`grep` 以 `tool_addition` 块出现在同一条消息里；顶层 `tools` 是 `read` 加占位工具；请求头带上 `inline-tools-2026-09-15` beta。

---

## 8. 自测与衔接

### 8.1 自测

1. 某个扩展注册的 provider 在 `streamSimple` 里同步抛了一个异常。M2 的循环会崩溃吗？
2. 用户先用 GPT 对话了几轮，再切到 Claude。GPT 回复里的 thinking 块还在会话文件里吗？发给 Claude 的请求里有吗？
3. Anthropic 的 SSE 连接在 `content_block_delta` 中途断开。M2 收到的最后一个事件是什么？最终消息里有已经收到的文本吗？

<details>
<summary>答案</summary>

1. 不会。扩展的 `streamSimple` 是在 `composeModelProvider` 的 `streamWith` 里调用的，外面包着 `lazyStream`（`packages/coding-agent/src/core/provider-composer.ts:594`）。同步异常发生在 `setup()` 里，被 `lazyStream` 的 `catch`（`packages/ai/src/api/lazy.ts:76`）变成一个 `error` 事件。M2 收到 `stopReason: "error"` 的消息，按错误退出循环。
2. 在会话文件里，因为 `transformMessages` 只在构造请求时运行，不修改 transcript。请求里没有：`isSameModel` 为假，thinking 块被丢掉（`packages/ai/src/api/transform-messages.ts:132`）。
3. 最后一个事件是 `error`，`errorMessage` 是 `Anthropic stream ended before message_stop`（`packages/ai/src/api/anthropic-messages.ts:573`）。`catch` 修改的是同一个 `output`，已经收到的文本块留在错误消息里（§4.11 实验的第 1 种失败）。下一次请求时这条消息会被 `transformMessages` 跳过。

</details>

### 8.2 衔接

M1 交给上层的是一个 `AssistantMessageEventStream`。M2 从 `streamAssistantResponse()`（`packages/agent/src/agent-loop.ts:390`）开始消费它：把 `start` 变成 `message_start`，把每个更新包成 `message_update`，把 `done`/`error` 变成 `message_end`。如果还没读 M2，下一步读 [M2 pi-agent-core：agent 循环](M2-agent-core.md)，入口是 `Agent.prompt()`。
