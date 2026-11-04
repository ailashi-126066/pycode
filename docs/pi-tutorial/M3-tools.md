# M3 工具：read、bash、edit、write

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

M2 的循环把模型给的工具调用交给 `tool.execute()`，自己不关心工具做了什么。M3 读的是这一调用的另一端：coding-agent 内置的四个工具 read、bash、edit、write 怎么实现，以及它们共用的几样基础设施。

- **工具的两种形状。** coding-agent 内部用 `ToolDefinition` 描述工具，它比 M2 的 `AgentTool` 多了提示词片段、渲染函数和一个上下文参数 `ctx`。`wrapToolDefinition` 把前者包装成后者，交给循环。
- **输出截断。** 工具结果要进 transcript，太长会挤占上下文。read 从头截、bash 从尾截，上限都是 2000 行或 50KB，先到哪个算哪个。截断后在文本末尾告诉模型怎么继续读。
- **进程管理。** bash 在独立的进程组里启动 shell，超时或中止时杀掉整个进程组，完整输出写到临时文件。
- **精确替换。** edit 按 `oldText` 在原文件里找唯一的一处并替换，支持一次多处替换、模糊匹配、保留 BOM 和 CRLF。
- **文件修改队列。** edit 和 write 对同一个文件的修改排队执行，不同文件之间仍然并行。
- **嵌套工具调用。** 工具在执行中可以通过 `ctx.executeTool()` 调用别的工具。这些调用走和模型发起的调用相同的流水线（参数校验、`tool_call` 扩展事件、权限检查）。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M3 是 `executeToolCalls()` 下面的 `tool.execute()` 这一格：

```
agent.prompt(messages)                                    [M2]
└─ runLoop() → executeToolCalls()
    ├─ prepareToolCall() → beforeToolCall()               [M2 → M7]
    ├─ executePreparedToolCall()                          [M2]
    │   └─ tool.execute()                                 [M3] ← 本模块入口
    │       └─ wrapToolDefinition 的 execute
    │           └─ ToolDefinition.execute(…, ctx)
    │               ├─ read  → truncateHead()
    │               ├─ bash  → BashOperations.exec() → spawn / killProcessTree
    │               │          OutputAccumulator → truncateTail() / 临时文件
    │               ├─ edit  → withFileMutationQueue() → applyEditsToNormalizedContent()
    │               ├─ write → withFileMutationQueue()
    │               └─ ctx.executeTool() → NestedToolCallRunner → runToolCall()  [M3 → M2]
    └─ finalizeExecutedToolCall() → afterToolCall()       [M2 → M7]
```

工具在什么时候被创建、怎么注册进 `Agent`，属于 M8 的 `_buildRuntime`。本模块只在 §4.2 看一眼注册点。

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/coding-agent/src/core/tools/tool-definition-wrapper.ts` | 58 | `ToolDefinition` 和 `AgentTool` 互相转换 | 精读，§4.1 |
| `packages/coding-agent/src/core/tools/read.ts` | 224 | read 工具 | 精读文本分支，图片分支略读，§4.3 |
| `packages/coding-agent/src/core/tools/truncate.ts` | 314 | `truncateHead`、`truncateTail` 等截断函数 | 精读前两个，§4.4 |
| `packages/coding-agent/src/core/tools/bash.ts` | 442 | bash 工具和本地 shell 执行 | 精读，§4.5–§4.7 |
| `packages/coding-agent/src/core/tools/output-accumulator.ts` | 257 | bash 流式输出的累加器：只保留尾部，超限时写临时文件 | 精读 `append`/`snapshot`/临时文件，§4.8 |
| `packages/coding-agent/src/core/exec.ts` | 158 | `execCommand`：扩展用的 `pi.exec()` 背后的实现 | 精读，§4.9 |
| `packages/coding-agent/src/core/tools/edit.ts` | 218 | edit 工具 | 精读，§4.10 |
| `packages/coding-agent/src/core/tools/edit-diff.ts` | 556 | 匹配、替换、生成 diff | 精读匹配和替换部分（1–362 行），diff 生成略读，§4.11–§4.12 |
| `packages/coding-agent/src/core/tools/write.ts` | 97 | write 工具 | 精读，§4.13 |
| `packages/coding-agent/src/core/tools/file-mutation-queue.ts` | 61 | 按文件排队的修改锁 | 精读，§4.14 |
| `packages/coding-agent/src/core/nested-tool-calls.ts` | 263 | 嵌套工具调用的执行器和记录器 | 精读，§4.15–§4.16 |
| `packages/coding-agent/src/core/tools/index.ts` | 224 | 工具工厂函数和导出 | 通读，§4.2 |
| `packages/coding-agent/src/core/tools/path-utils.ts` | 118 | 路径解析（`~`、`@` 前缀、macOS 文件名变体） | 略读 |
| `packages/coding-agent/src/utils/shell.ts` | 264 | shell 配置、环境变量、`killProcessTree` | 只读 `killProcessTree`，§4.7 |
| `packages/coding-agent/src/utils/child-process.ts` | 137 | `waitForChildProcess` | 只读注释，§4.6 |
| `packages/coding-agent/src/utils/output-files.ts` | 34 | 临时输出文件的创建 | 通读，§4.8 |
| `packages/coding-agent/src/core/tools/grep.ts`、`find.ts`、`ls.ts`、`powershell.ts` | 325、318、175、67 | 其余内置工具，默认不启用 | 略，结构和 read/bash 相同 |
| `packages/coding-agent/src/core/tools/renderers/`、`render-utils.ts` | 1007、180 | TUI 里怎么画工具调用和结果 | 略（TUI 不在本教程范围） |

行数用 `wc -l` 统计。默认启用的只有 read、bash、edit、write 四个工具（`packages/coding-agent/src/core/settings-manager.ts:85` 的 `DEFAULT_TOOL_NAMES`）。

### 1.4 读完能回答的问题

1. 扩展注册的工具和内置工具，在 `Agent` 眼里有区别吗？工具执行时拿到的 `ctx` 从哪里来？
2. read 一个 3000 行的文件，模型看到什么？bash 输出 3000 行呢？两者截掉的是哪一部分？bash 命令超时后，它启动的后台子进程还活着吗？
3. edit 的 `oldText` 在文件里出现了两次会怎样？文件里是弯引号、模型写的是直引号，能替换成功吗？
4. 模型一次并行发出两个 edit，改的是同一个文件，会互相覆盖吗？
5. 一个工具在执行中调用另一个工具，扩展的权限检查还管得到吗？这次调用会出现在 transcript 里吗？

---

## 2. 先跑一次

### 2.1 主线场景里的 read 调用

主线场景里模型只调用了一次 read。用 json 模式运行，取出工具执行的两个事件：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m3-work-main PI_CODING_AGENT_DIR=/tmp/pi-lab/m3-agent-main ./run.sh --mode json "读 README.md，总结成三句话" > /tmp/pi-lab/m3-events.jsonl
grep -E '^\{"type":"(tool_execution_start|tool_execution_end)"' /tmp/pi-lab/m3-events.jsonl
```

```
{"type":"tool_execution_start","toolCallId":"call_1","toolName":"read","args":{"path":"README.md"}}
{"type":"tool_execution_end","toolCallId":"call_1","toolName":"read","result":{"content":[{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}],"structuredContent":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"},"isError":false,"durationMs":1}
```

`args` 是模型给的参数，`result` 是 read 工具 `execute()` 的返回值，也就是 M2 里的 `AgentToolResult`：

- `content` 是给模型看的文本，文件内容原样返回，因为它没有超过截断上限。
- `structuredContent` 是给程序调用方看的结果。read 的 `outputSchema` 规定文本文件的结构化结果就是文本本身（§4.3）。
- 没有 `details`。read 只在发生截断时才设置 `details`，这里它是 `undefined`，JSON 序列化时被丢掉了。

### 2.2 模型看到的工具声明

工具声明写在 transcript 第一条 `system` 消息的 `toolsAdded` 里（M2 §4.4）。从会话文件里取出来：

```bash
node -e 'const fs=require("fs");for(const l of fs.readFileSync(process.argv[1],"utf8").trim().split("\n")){const e=JSON.parse(l);if(e.message?.role==="system"){const ts=e.message.toolsAdded;console.log(ts.map(t=>t.name).join(", "));console.log(JSON.stringify(ts[0],null,1))}}' "$(ls -t /tmp/pi-lab/m3-agent-main/sessions/*/*.jsonl | head -1)"
```

脚本逐行解析会话文件，找到 `system` 消息，先打印所有工具名，再格式化输出第一个工具的声明。

```
read, bash, edit, write
{
 "name": "read",
 "description": "Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete.",
 "parameters": {
  "type": "object",
  "required": [
   "path"
  ],
  "properties": {
   "path": {
    "type": "string",
    "description": "Path to the file to read (relative or absolute)"
   },
   "offset": {
    "type": "number",
    "description": "Line number to start reading from (1-indexed)"
   },
   "limit": {
    "type": "number",
    "description": "Maximum number of lines to read"
   }
  }
 },
 "constrainedSampling": {
  "type": "json_schema",
  "strict": "prefer"
 }
}
```

声明里只有四个字段：`name`、`description`、`parameters`、`constrainedSampling`。`label`、`execute`、渲染函数都不进 transcript。`description` 里的 "2000 lines or 50KB" 来自 `truncate.ts` 的两个常量，是代码里拼出来的（§4.3）。

### 2.3 不经过循环，直接调用工具

[`lab/m3-tools.ts`](lab/m3-tools.ts) 直接 import coding-agent 的工具工厂函数，在一个临时目录里调用工具，不需要模型、会话和 `Agent`。后面各节的输出大多来自它。`shapes` 场景创建 read 工具的两种形状，比较字段，然后执行一次：

```bash
WORK=/tmp/pi-lab/m3-work node --conditions=source --experimental-strip-types m3-tools.ts shapes
```

```
[ToolDefinition 字段] ["name","label","description","promptSnippet","promptGuidelines","parameters","outputSchema","constrainedSampling","execute","renderCall","renderResult"]
[AgentTool 字段] ["name","label","description","parameters","outputSchema","constrainedSampling","prepareArguments","executionMode","execute"]
[execute 参数个数 ToolDefinition / AgentTool] [5,5]
[AgentToolResult] {"content":[{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}],"structuredContent":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}
```

`ToolDefinition` 有 `promptSnippet`、`promptGuidelines`、`renderCall`、`renderResult`，`AgentTool` 没有。`AgentTool` 列出了 `prepareArguments` 和 `executionMode`，值是 `undefined`，因为包装函数逐个拷贝字段，不管有没有值（§4.1）。直接调用 `tool.execute("call_1", { path: "README.md" })` 得到的结果和 §2.1 主线场景里的 `result` 一样。

---

## 3. 数据结构

M2 已经讲过 `AgentTool` 和 `AgentToolResult`（M2 §3.2）。本模块自己的类型有五个：工具定义 `ToolDefinition`、工具上下文 `ExtensionToolContext`、截断结果 `TruncationResult`、替换描述 `Edit` 和匹配结果 `FuzzyMatchResult`、嵌套调用记录 `NestedToolCalls`。

### 3.1 ToolDefinition：coding-agent 的工具定义

内置工具和扩展用 `pi.registerTool()` 注册的工具，都先写成 `ToolDefinition`。定义在扩展的类型文件里，因为它同时是扩展 API 的一部分（→ M7）。

源码：[`packages/coding-agent/src/core/extensions/types.ts:599-625`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L599-L625)

```ts
export interface ToolDefinition<TParams extends TSchema = TSchema, TDetails = unknown, TState = any> {
	/** Tool name (used in LLM tool calls) */
	name: string;
	/** Human-readable label for UI */
	label: string;
	/** Description for LLM */
	description: string;
	/** Optional one-line snippet for the Available tools section in the default system prompt. Custom tools are omitted from that section when this is not provided. */
	promptSnippet?: string;
	/** Optional guideline bullets appended to the default system prompt Guidelines section when this tool is active. */
	promptGuidelines?: string[];
	/** Parameter schema (TypeBox) */
	parameters: TParams;
	/** Optional provider-side constrained sampling request for this tool. Set false to explicitly disable it, equivalent to leaving it undefined. */
	constrainedSampling?: false | ConstrainedSamplingConfig;
	/** Controls whether ToolExecutionComponent renders the standard colored shell or the tool renders its own framing. */
	renderShell?: "default" | "self";

	/** Optional compatibility shim to prepare raw tool call arguments before schema validation. Must return an object conforming to TParams. */
	prepareArguments?: (args: unknown) => Static<TParams>;

	/**
	 * JSON Schema of `structuredContent` in successful results. Tools that declare it should always
	 * set `structuredContent`; codemode scripts then receive it instead of the text content.
	 */
	outputSchema?: TSchema;

```

源码：[`packages/coding-agent/src/core/extensions/types.ts:660-676`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L660-L676)

```ts
	/**
	 * Per-tool execution mode override.
	 * - "sequential": this tool must execute one at a time with other tool calls.
	 * - "parallel": this tool can execute concurrently with other tool calls.
	 *
	 * If omitted, the default execution mode applies.
	 */
	executionMode?: ToolExecutionMode;

	/** Execute the tool. */
	execute(
		toolCallId: string,
		params: Static<TParams>,
		signal: AbortSignal | undefined,
		onUpdate: AgentToolUpdateCallback<TDetails> | undefined,
		ctx: ExtensionToolContext,
	): Promise<AgentToolResult<TDetails>>;
```

中间省略的 626–659 行是 `exposure`、`namespace`、`reachesTools`、`annotations`、`defaultActive`、`prepareLoadout`，它们决定工具怎么暴露给模型（direct / deferred / codemode），在 M6 和 M10 讲。后面 678–687 行是两个渲染函数 `renderCall`、`renderResult`，属于 TUI。

| 字段 | 说明 | `AgentTool` 有没有 |
|---|---|---|
| `name`、`label`、`description`、`parameters` | 和 `AgentTool` 相同 | 有 |
| `promptSnippet`、`promptGuidelines` | 写进系统提示词的工具简介和使用准则（→ M6） | 没有 |
| `constrainedSampling` | 请求 provider 按 schema 约束采样（→ M1） | 有（`AgentTool` 继承自 M1 的 `Tool`） |
| `prepareArguments`、`outputSchema`、`executionMode` | 和 `AgentTool` 相同 | 有 |
| `renderShell`、`renderCall`、`renderResult` | TUI 渲染 | 没有 |
| `execute` | 比 `AgentTool.execute` 多第 5 个参数 `ctx: ExtensionToolContext` | 有，但只有 4 个参数 |

两者最重要的区别在 `execute` 的签名。M2 的循环调用 `tool.execute(id, args, signal, onUpdate)`，只传 4 个参数（`packages/agent/src/agent-loop.ts:842`）。`ToolDefinition.execute` 要第 5 个参数 `ctx`。补上 `ctx` 是 §4.1 包装函数的工作。

**真实实例。** §2.3 的 `shapes` 场景打印了 read 工具两种形状的字段：

```
[ToolDefinition 字段] ["name","label","description","promptSnippet","promptGuidelines","parameters","outputSchema","constrainedSampling","execute","renderCall","renderResult"]
[AgentTool 字段] ["name","label","description","parameters","outputSchema","constrainedSampling","prepareArguments","executionMode","execute"]
…省略…
```

### 3.2 ExtensionToolContext：工具执行时的上下文

源码：[`packages/coding-agent/src/core/extensions/types.ts:395-415`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L395-L415)

```ts
/**
 * Context passed to tool `execute()` in a session: the extension context plus `executeTool()`
 * for running other tools through the same validation, hooks, and permission checks as
 * model-issued calls.
 *
 * A tool wrapped with `wrapToolDefinition()` without a context factory, such as a built-in tool
 * created with `createBashTool()` and run in a plain `Agent` or called directly, gets no context.
 */
export interface ExtensionToolContext extends ExtensionContext {
	/** Tools {@link executeTool} can call. */
	readonly tools: readonly AgentTool[];
	/**
	 * Run another tool. The call gets the id `<calling id>/<n>`, and the `tool_call`, `tool_result`,
	 * and `tool_execution_*` events carry `parentToolCallId`. It does not appear in the transcript;
	 * a bounded record of it is kept as `nestedCalls` on the calling tool's result message.
	 *
	 * Never rejects for tool failures: unknown tools, validation errors, blocked calls, and thrown
	 * errors come back as `isError: true`.
	 */
	executeTool(name: string, args: unknown, options?: ExecuteToolOptions): Promise<AgentToolCallOutcome>;
}
```

`ExtensionToolContext` 继承 `ExtensionContext`（`packages/coding-agent/src/core/extensions/types.ts:345`），也就是扩展事件处理器拿到的那个 `ctx`（→ M7），再加两样东西：

| 字段 | 说明 |
|---|---|
| `cwd` | 继承来的字段。工具解析相对路径时优先用它，没有才用创建工具时传入的 `cwd` |
| `model`、`thinkingLevel`、`sessionManager` | 继承来的字段。read 用 `model` 判断要不要附图片；bash 用它们设置 `PI_*` 环境变量 |
| `tools` | 本次调用可以通过 `executeTool` 调用的工具 |
| `executeTool(name, args, options)` | 发起嵌套工具调用，返回 M2 的 `AgentToolCallOutcome`（`packages/agent/src/types.ts:449`）。永远不因为工具失败而 reject |

注释说明了一种情况：用 `createBashTool()` 这类工厂函数直接创建的工具，没有上下文工厂，`ctx` 是 `undefined`。所以四个内置工具的 `execute` 里都写 `ctx?.cwd || cwd`，在没有 `ctx` 时也能工作。`lab/m3-tools.ts` 就是这样调用的。

**真实实例。** [`lab/m3-nested-ext.ts`](lab/m3-nested-ext.ts) 的 `peek` 工具在设置 `M3_SHOW_CTX=1` 时，把收到的 `ctx` 的几个字段打印到 stderr（stdout 上仍是 json 事件流）：

```bash
M3_SHOW_CTX=1 EXT=$PWD/m3-nested-ext.ts WORK=/tmp/pi-lab/m3-ctx-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m3-ctx-agent ./run.sh --mode json "用 peek 看 README.md" > /tmp/pi-lab/m3-ctx.jsonl 2> /tmp/pi-lab/m3-ctx.txt
cat /tmp/pi-lab/m3-ctx.txt
```

```
[ctx] {"ownKeys":["ui","mode","hasUI","cwd","sessionManager","modelRegistry","model","scopedModels","thinkingLevel","isIdle","isProjectTrusted","signal","abort","hasPendingMessages","shutdown","getContextUsage","compact","getSystemPrompt"],"cwd":"/private/tmp/pi-lab/m3-ctx-work","model":"faux-1","tools":["read","bash","edit","write","peek"],"executeTool":"function"}
```

`ownKeys` 是 `Object.keys(ctx)`，列出的都是继承自 `ExtensionContext` 的字段。`tools` 和 `executeTool` 不在里面，因为 `createToolContext` 用 `Object.defineProperties` 加上它们，这样定义的属性默认不可枚举（§4.2）。但它们确实存在：`tools` 是当前可调用的五个工具，包括 `peek` 自己；`executeTool` 是函数。`cwd` 是 pi 进程的工作目录，macOS 上 `/tmp` 解析成了 `/private/tmp`。

### 3.3 TruncationResult：截断结果

源码：[`packages/coding-agent/src/core/tools/truncate.ts:11-38`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/truncate.ts#L11-L38)

```ts
export const DEFAULT_MAX_LINES = 2000;
export const DEFAULT_MAX_BYTES = 50 * 1024; // 50KB
export const GREP_MAX_LINE_LENGTH = 500; // Max chars per grep match line

export interface TruncationResult {
	/** The truncated content */
	content: string;
	/** Whether truncation occurred */
	truncated: boolean;
	/** Which limit was hit: "lines", "bytes", or null if not truncated */
	truncatedBy: "lines" | "bytes" | null;
	/** Total number of lines in the original content */
	totalLines: number;
	/** Total number of bytes in the original content */
	totalBytes: number;
	/** Number of complete lines in the truncated output */
	outputLines: number;
	/** Number of bytes in the truncated output */
	outputBytes: number;
	/** Whether the last line was partially truncated (only for tail truncation edge case) */
	lastLinePartial: boolean;
	/** Whether the first line exceeded the byte limit (for head truncation) */
	firstLineExceedsLimit: boolean;
	/** The max lines limit that was applied */
	maxLines: number;
	/** The max bytes limit that was applied */
	maxBytes: number;
}
```

| 字段 | 说明 |
|---|---|
| `content` | 截断后的文本 |
| `truncated`、`truncatedBy` | 是否截断；被行数还是字节数限制住 |
| `totalLines`、`totalBytes` | 原文的行数和 UTF-8 字节数 |
| `outputLines`、`outputBytes` | 输出的行数和字节数 |
| `lastLinePartial` | 只在从尾部截断时可能为 `true`：最后一行本身超过字节上限，只保留了它的结尾 |
| `firstLineExceedsLimit` | 只在从头部截断时可能为 `true`：第一行本身超过字节上限，输出为空 |
| `maxLines`、`maxBytes` | 实际使用的上限，默认 2000 和 51200 |

工具发生截断时，把整个 `TruncationResult` 放进结果的 `details.truncation`。`details` 不发给模型，只给界面和日志用；模型看到的是 `content` 末尾的提示文字（§4.3）。

**真实实例。** `lab/m3-tools.ts read` 让 read 读一个 3000 行的文件，`details.truncation` 如下（`content` 字段太长，脚本把它换成了 `…`）：

```
…省略…
[3000 行：details.truncation（content 字段略）] {"content":"…","truncated":true,"truncatedBy":"lines","totalLines":3000,"totalBytes":28893,"outputLines":2000,"outputBytes":18892,"lastLinePartial":false,"firstLineExceedsLimit":false,"maxLines":2000,"maxBytes":51200}
…省略…
```

### 3.4 Edit 和 FuzzyMatchResult：替换和匹配

源码：[`packages/coding-agent/src/core/tools/edit-diff.ts:175-199`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit-diff.ts#L175-L199)

```ts
export interface FuzzyMatchResult {
	/** Whether a match was found */
	found: boolean;
	/** The index where the match starts (in the content that should be used for replacement) */
	index: number;
	/** Length of the matched text */
	matchLength: number;
	/** Whether fuzzy matching was used (false = exact match) */
	usedFuzzyMatch: boolean;
	/**
	 * The content to use for replacement operations.
	 * When exact match: original content. When fuzzy match: normalized content.
	 */
	contentForReplacement: string;
}

export interface Edit {
	oldText: string;
	newText: string;
}

export interface AppliedEditsResult {
	baseContent: string;
	newContent: string;
}
```

| 类型 | 字段 | 说明 |
|---|---|---|
| `Edit` | `oldText`、`newText` | 模型给的一处替换。edit 工具的参数 `edits` 是 `Edit[]` |
| `FuzzyMatchResult` | `found`、`index`、`matchLength` | 找到没有，在哪，多长 |
| | `usedFuzzyMatch` | 精确匹配失败，用规范化后的文本匹配上了 |
| | `contentForReplacement` | `index` 是相对哪段文本的位置。精确匹配时是原文，模糊匹配时是规范化后的原文 |
| `AppliedEditsResult` | `baseContent`、`newContent` | 替换前后的全文（换行已统一成 LF），用来生成 diff |

**真实实例。** edit 工具的参数，以及它成功后返回的 `details`（一份给界面看的 diff、一份标准 unified patch、第一处修改所在行）：

```json
{ "path": "a.ts", "edits": [
  { "oldText": "const a = 1;", "newText": "const a = 10;" },
  { "oldText": "const c = 3;", "newText": "const c = 30;" } ] }
```

```
[两处替换] 成功 "Successfully replaced 2 block(s) in a.ts."
[details.diff] -1 const a = 1;
+1 const a = 10;
 2 const b = 2;
-3 const c = 3;
+3 const c = 30;
 4 console.log(a, b, c);
[details.firstChangedLine] 1
[details.patch] --- a.ts
+++ a.ts
@@ -1,4 +1,4 @@
-const a = 1;
+const a = 10;
 const b = 2;
-const c = 3;
+const c = 30;
 console.log(a, b, c);

…省略…
```

`FuzzyMatchResult` 是 `fuzzyFindText` 的返回值，edit 工具不把它放进结果。`lab/m3-tools.ts fuzzy` 直接调用这个导出函数，内容和 §7.3 的 `quote.ts` 相同（弯引号，第一行末尾 3 个空格）：

```bash
node --conditions=source --experimental-strip-types m3-tools.ts fuzzy
```

```
[精确匹配 const other] {"found":true,"index":26,"matchLength":11,"usedFuzzyMatch":false,"contentForReplacement":"const greet = “hello”;   \nconst other = “world”;\n"}
[模糊匹配 直引号] {"found":true,"index":0,"matchLength":22,"usedFuzzyMatch":true,"contentForReplacement":"const greet = \"hello\";\nconst other = \"world\";\n"}
[找不到] {"found":false,"index":-1,"matchLength":0,"usedFuzzyMatch":false,"contentForReplacement":"…"}
```

精确匹配时 `contentForReplacement` 是原文，`index` 26 是原文里的位置（第一行 25 个字符加换行）。模糊匹配时 `contentForReplacement` 是整份规范化后的文本：两行的弯引号都变成直引号，行尾空格也去掉了，`index` 和 `matchLength` 都相对这份文本。所以 edit 不能直接把它写回文件（§4.12 第 6 步）。找不到时 `index` 是 -1（脚本把 `contentForReplacement` 换成了 `…`）。

### 3.5 NestedToolCalls：嵌套调用的记录

嵌套调用不进 transcript，只在发起它的工具结果消息上留一份有界的记录。类型定义在 pi-ai，因为它是 `ToolResultMessage` 的字段：

源码：[`packages/ai/src/types.ts:618-638`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L618-L638)

```ts
/** A tool call that another tool made while it ran, for example from a codemode script. */
export interface NestedToolCallRecord {
	id: string;
	name: string;
	/** Omitted when over the size limits; `argumentsBytes` then gives their size. */
	arguments?: JsonObject;
	/** UTF-8 size of the arguments as JSON, set when `arguments` is omitted. */
	argumentsBytes?: number;
	/** `unfinished`: the call was still running when the calling tool finished. */
	status: "ok" | "error" | "unfinished";
	durationMs?: number;
	/** Error text, truncated. */
	error?: string;
}

/** Bounded record of the nested calls a tool made. Results are not recorded. */
export interface NestedToolCalls {
	calls: NestedToolCallRecord[];
	/** False when calls were dropped, arguments omitted, or calls had not finished. */
	complete: boolean;
}
```

| 字段 | 说明 |
|---|---|
| `calls[].id` | `<父调用 id>/<序号>`，例如 `call_1/1` |
| `calls[].arguments` / `argumentsBytes` | 参数；超过大小上限时省略参数，只记字节数 |
| `calls[].status` | `ok`、`error`，或 `unfinished`（父工具结束时它还没结束） |
| `calls[].error` | 错误文本，截到 500 字符 |
| `complete` | 有调用被丢弃、参数被省略、或调用没结束时为 `false` |

`ToolResultMessage` 的 `nestedCalls` 字段（`packages/ai/src/types.ts:650`）的注释说明它只用于会话记录，不发给模型。结果本身（`content`）不记录，父工具自己决定把哪些内容写进它的返回值。

**真实实例。** [`lab/m3-nested-ext.ts`](lab/m3-nested-ext.ts) 注册了一个 `peek` 工具，它在 `execute` 里调用 read 和一个不存在的工具 `no_such_tool`。运行后 `peek` 的 toolResult 消息带着这份记录（§4.15 有完整的运行过程）：

```
{
 "calls": [
  {
   "id": "call_1/1",
   "name": "read",
   "status": "ok",
   "arguments": {
    "path": "README.md"
   },
   "durationMs": 1
  },
  {
   "id": "call_1/2",
   "name": "no_such_tool",
   "status": "error",
   "arguments": {},
   "durationMs": 0,
   "error": "Tool no_such_tool not found"
  }
 ],
 "complete": true
}
```

### 3.6 类型关系图

```
 ToolDefinition<TParams, TDetails>           内置工具工厂 / pi.registerTool()
   name, description, parameters, promptSnippet, renderCall …
   execute(id, params, signal, onUpdate, ctx)
        │ wrapToolDefinition(def, ctxFactory)                       §4.1
        ▼
 AgentTool                                   交给 M2 的循环
   execute(id, params, signal, onUpdate)  ── 调用时用 ctxFactory(id, signal) 补上 ctx
        │                                          │
        │                                          ▼
        │                            ExtensionToolContext
        │                              cwd, model, sessionManager, tools
        │                              executeTool() ──▶ NestedToolCallRunner ──▶ runToolCall [M2]
        │                                                      │
        ▼ 返回                                                 ▼ 记录
 AgentToolResult                                      NestedToolCalls
   content    ← 截断后的文本 + 继续读的提示              挂在 ToolResultMessage.nestedCalls
   details    ← { truncation: TruncationResult, fullOutputPath }   (read / bash)
                { diff, patch, firstChangedLine }                  (edit)
   structuredContent ← outputSchema 规定的结构                     (read / bash)

 edit 的参数 { path, edits: Edit[] }
   每个 Edit ── fuzzyFindText() ──▶ FuzzyMatchResult ──▶ AppliedEditsResult
```

---

## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。

```
tool.execute(id, args, signal, onUpdate)          ← M2 executePreparedToolCall 调用
└─ wrapToolDefinition 的 execute                   §4.1
    │   ctx = ctxFactory(id, signal)               §4.2 注册时传入 runner.createToolContext
    └─ ToolDefinition.execute(…, ctx)
        ├─ read                                    §4.3
        │   └─ truncateHead()                      §4.4
        ├─ bash  createShellToolDefinition         §4.5 ①②③
        │   ├─ BashOperations.exec()               §4.6
        │   │   └─ killProcessTree()               §4.7
        │   └─ OutputAccumulator → truncateTail()  §4.8 / §4.4
        │   （扩展的 pi.exec() → execCommand()      §4.9）
        ├─ edit                                    §4.10
        │   ├─ withFileMutationQueue()             §4.14
        │   ├─ normalizeToLF / fuzzyFindText()     §4.11
        │   └─ applyEditsToNormalizedContent()     §4.12
        ├─ write                                   §4.13
        │   └─ withFileMutationQueue()             §4.14
        └─ ctx.executeTool()                       §4.15
            └─ NestedToolCallRunner.execute()      §4.15
                └─ runToolCall() → M2              §4.16
            记录 → toolResult.nestedCalls           §4.17
```

### 4.1 wrapToolDefinition()：ToolDefinition 变成 AgentTool

**调用位置**：每个内置工具的 `createXxxTool()` 工厂函数（例如 `packages/coding-agent/src/core/tools/read.ts:223`），以及 AgentSession 注册工具时经过的 `wrapRegisteredTool()`（§4.2）。

源码：[`packages/coding-agent/src/core/tools/tool-definition-wrapper.ts:1-38`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/tool-definition-wrapper.ts#L1-L38)

```ts
import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { ExtensionToolContext, ToolDefinition } from "../extensions/types.ts";

/** Creates the context for one tool call. */
export type ToolContextFactory = (toolCallId: string, signal: AbortSignal | undefined) => ExtensionToolContext;

/** Wrap a ToolDefinition into an AgentTool for the core runtime. */
export function wrapToolDefinition<TDetails = unknown>(
	definition: ToolDefinition<any, TDetails>,
	ctxFactory?: ToolContextFactory,
): AgentTool<any, TDetails> {
	return {
		name: definition.name,
		label: definition.label,
		description: definition.description,
		parameters: definition.parameters,
		outputSchema: definition.outputSchema,
		constrainedSampling: definition.constrainedSampling,
		prepareArguments: definition.prepareArguments,
		executionMode: definition.executionMode,
		execute: (toolCallId, params, signal, onUpdate, ctx?: ExtensionToolContext) =>
			definition.execute(
				toolCallId,
				params,
				signal,
				onUpdate,
				ctx ?? (ctxFactory?.(toolCallId, signal) as ExtensionToolContext),
			),
	};
}

/** Wrap multiple ToolDefinitions into AgentTools for the core runtime. */
export function wrapToolDefinitions(
	definitions: ToolDefinition<any, any>[],
	ctxFactory?: ToolContextFactory,
): AgentTool<any>[] {
	return definitions.map((definition) => wrapToolDefinition(definition, ctxFactory));
}
```

**讲解**

包装函数做两件事：

- 把 `AgentTool` 需要的字段逐个拷贝过来。`promptSnippet`、`promptGuidelines`、渲染函数不拷贝，它们只在 coding-agent 内部使用，循环用不到。所以 §2.3 里 `AgentTool` 的字段列表比 `ToolDefinition` 少。
- 把 4 参数的 `execute` 适配成 5 参数的调用。第 5 个参数 `ctx` 有两个来源：调用方显式传了就用传入的值；否则用 `ctxFactory(toolCallId, signal)` 现造一个。M2 的循环只传 4 个参数，所以在 AgentSession 里 `ctx` 总是由 `ctxFactory` 创建，每次工具调用一个新的。

`ctxFactory` 是可选的。工厂函数 `createReadTool()` 调用 `wrapToolDefinition(createReadToolDefinition(cwd, options))` 时没有传它，`ctx` 就是 `undefined`。

反方向的转换是 `createToolDefinitionFromAgentTool`：

源码：[`packages/coding-agent/src/core/tools/tool-definition-wrapper.ts:40-58`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/tool-definition-wrapper.ts#L40-L58)

```ts
/**
 * Synthesize a minimal ToolDefinition from an AgentTool.
 *
 * This keeps AgentSession's internal registry definition-first even when a caller
 * provides plain AgentTool overrides that do not include prompt metadata or renderers.
 */
export function createToolDefinitionFromAgentTool(tool: AgentTool<any>): ToolDefinition<any, unknown> {
	return {
		name: tool.name,
		label: tool.label,
		description: tool.description,
		parameters: tool.parameters as any,
		outputSchema: tool.outputSchema,
		constrainedSampling: tool.constrainedSampling,
		prepareArguments: tool.prepareArguments,
		executionMode: tool.executionMode,
		execute: async (toolCallId, params, signal, onUpdate) => tool.execute(toolCallId, params, signal, onUpdate),
	};
}
```

SDK 调用方可以直接传 `AgentTool` 覆盖内置工具（M8 的 `_baseToolsOverride`）。AgentSession 内部的工具注册表以 `ToolDefinition` 为准，所以先把 `AgentTool` 转回 `ToolDefinition`。转回来的 `execute` 丢掉了 `ctx`，因为原来的 `AgentTool` 不认识它。

**往哪走**：§4.2 看 AgentSession 在哪里传入 `ctxFactory`。

### 4.2 工具的注册点：createAllToolDefinitions 和 wrapRegisteredTool

**调用位置**：`AgentSession._buildRuntime()`（→ M8）。

源码：[`packages/coding-agent/src/core/agent-session.ts:3692-3709`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3692-L3709)

```ts
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
```

AgentSession 用 `createAllToolDefinitions` 创建全部 8 个内置工具的 `ToolDefinition`（`packages/coding-agent/src/core/tools/index.ts:182`），把设置里的 `autoResizeImages`、`shellCommandPrefix`、`shellPath` 传进去。之后把内置工具和扩展注册的工具一起交给 `wrapRegisteredTools`（`packages/coding-agent/src/core/agent-session.ts:3628`）：

源码：[`packages/coding-agent/src/core/extensions/wrapper.ts:13-21`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/wrapper.ts#L13-L21)

```ts
/**
 * Wrap a RegisteredTool into an AgentTool.
 * Uses the runner's createToolContext() for consistent context across tools and event handlers.
 */
export function wrapRegisteredTool(registeredTool: RegisteredTool, runner: ExtensionRunner): AgentTool {
	return wrapToolDefinition(registeredTool.definition, (toolCallId, signal) =>
		runner.createToolContext(toolCallId, signal),
	);
}
```

内置工具和扩展工具走同一个包装函数，`ctxFactory` 都是 `runner.createToolContext`。在 `Agent` 看来，它们没有区别。

源码：[`packages/coding-agent/src/core/extensions/runner.ts:964-995`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L964-L995)

```ts
	/**
	 * Create the context for executing the tool call `toolCallId`: the extension context plus
	 * `tools` and `executeTool()`. `signal` is the default signal of nested calls.
	 */
	createToolContext(toolCallId: string, signal: AbortSignal | undefined): ExtensionToolContext {
		const runner = this;
		// createContext() returns a fresh object, so adding properties does not affect other contexts.
		return Object.defineProperties(this.createContext() as ExtensionToolContext, {
			tools: {
				get() {
					runner.assertActive();
					return runner.getCallableToolsFn();
				},
			},
			executeTool: {
				value: async (name: string, args: unknown, options: ExecuteToolOptions = {}) => {
					runner.assertActive();
					if (!runner.executeToolFn) {
						return {
							toolCall: { type: "toolCall", id: `${toolCallId}/0`, name, arguments: {} },
							result: {
								content: [{ type: "text", text: "Nested tool calls are not available in this context" }],
								details: {},
							},
							isError: true,
						};
					}
					return runner.executeToolFn(toolCallId, name, args, { ...options, signal: options.signal ?? signal });
				},
			},
		});
	}
```

**讲解**

`createToolContext` 先调用 `createContext()` 得到扩展事件处理器用的那种 `ctx`，再用 `Object.defineProperties` 加上两个属性：

- `tools` 是 getter，每次读取都调用 `getCallableToolsFn()`，拿到的是当前时刻可调用的工具。
- `executeTool` 把调用方的 `toolCallId` 闭包进去，所以工具里只写 `ctx.executeTool("read", args)`，执行器就知道父调用是谁。`signal` 默认用父调用的 `signal`，父调用被中止时嵌套调用也会收到中止信号。`executeToolFn` 来自 AgentSession 绑定的 `_executeNestedToolCall`（`packages/coding-agent/src/core/agent-session.ts:3548`），§4.15 读它。

`assertActive()` 检查 runner 是否还有效。会话被替换（例如 `/new`）后旧的 `ctx` 失效，旧工具再调用会抛错。

**往哪走**：§4.3 read 工具。

### 4.3 read：读文本文件

**调用位置**：`wrapToolDefinition` 的 `execute` → `createReadToolDefinition` 返回对象的 `execute`。主线场景里参数是 `{ path: "README.md" }`。

源码：[`packages/coding-agent/src/core/tools/read.ts:86-132`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/read.ts#L86-L132)

```ts
export function createReadToolDefinition(
	cwd: string,
	options?: ReadToolOptions,
): ToolDefinition<typeof readSchema, ReadToolDetails | undefined> {
	const autoResizeImages = options?.autoResizeImages ?? true;
	const fallbackResizeOptions = options?.resizeOptions;
	const ops = options?.operations ?? defaultReadOperations;
	return {
		name: "read",
		label: "read",
		description: `Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as attachments. For text files, output is truncated to ${DEFAULT_MAX_LINES} lines or ${DEFAULT_MAX_BYTES / 1024}KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete.`,
		promptSnippet: readToolSystemPromptContribution.snippet,
		promptGuidelines: [...readToolSystemPromptContribution.guidelines],
		parameters: readSchema,
		outputSchema: readOutputSchema,
		constrainedSampling: { type: "json_schema", strict: "prefer" },
		async execute(
			_toolCallId,
			{ path, offset, limit }: { path: string; offset?: number; limit?: number },
			signal?: AbortSignal,
			_onUpdate?,
			ctx?: ExtensionContext,
		) {
			return new Promise<{ content: (TextContent | ImageContent)[]; details: ReadToolDetails | undefined }>(
				(resolve, reject) => {
					if (signal?.aborted) {
						reject(new Error("Operation aborted"));
						return;
					}
					let aborted = false;
					const onAbort = () => {
						aborted = true;
						reject(new Error("Operation aborted"));
					};
					signal?.addEventListener("abort", onAbort, { once: true });

					(async () => {
						try {
							const absolutePath = await resolveReadPathAsync(path, ctx?.cwd || cwd);
							if (aborted) return;
							// Check if file exists and is readable.
							await ops.access(absolutePath);
							if (aborted) return;
							const mimeType = ops.detectImageMimeType ? await ops.detectImageMimeType(absolutePath) : undefined;
							let content: (TextContent | ImageContent)[];
							let details: ReadToolDetails | undefined;
							const nonVisionImageNote = getNonVisionImageNote(ctx?.model);
```

**讲解（框架部分）**

`description` 用模板字符串拼进了 `DEFAULT_MAX_LINES` 和 `DEFAULT_MAX_BYTES / 1024`，所以 §2.2 的工具声明里写着 "2000 lines or 50KB"。模型从描述里就知道上限和 `offset`/`limit` 的用法。

`execute` 用一个手写的 `new Promise` 包住异步过程，目的是让中止立即生效：`signal` 一触发，`onAbort` 马上 reject，不等正在进行的文件读取。异步过程在每个 `await` 之后检查 `aborted`，已中止就直接返回，不再 resolve。

路径解析用 `resolveReadPathAsync(path, ctx?.cwd || cwd)`（`packages/coding-agent/src/core/tools/path-utils.ts:86`）。它处理 `~`、`@` 前缀，并在文件不存在时尝试 macOS 截图文件名的几种变体（窄空格、NFD、弯引号）。`detectImageMimeType` 按文件内容判断是不是图片。图片分支（133–152 行）读二进制、按模型的限制缩放，返回一个文本块加一个图片块；模型不支持图片时加一句提示。本节只读文本分支。

源码：[`packages/coding-agent/src/core/tools/read.ts:153-205`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/read.ts#L153-L205)

```ts
							} else {
								// Read text content.
								const buffer = await ops.readFile(absolutePath);
								const textContent = buffer.toString("utf-8");
								const allLines = textContent.split("\n");
								const totalFileLines = allLines.length;
								// Apply offset if specified. Convert from 1-indexed input to 0-indexed array access.
								const startLine = offset ? Math.max(0, offset - 1) : 0;
								const startLineDisplay = startLine + 1;
								// Check if offset is out of bounds.
								if (startLine >= allLines.length) {
									throw new Error(`Offset ${offset} is beyond end of file (${allLines.length} lines total)`);
								}
								let selectedContent: string;
								let userLimitedLines: number | undefined;
								// If limit is specified by the user, honor it first. Otherwise truncateHead decides.
								if (limit !== undefined) {
									const endLine = Math.min(startLine + limit, allLines.length);
									selectedContent = allLines.slice(startLine, endLine).join("\n");
									userLimitedLines = endLine - startLine;
								} else {
									selectedContent = allLines.slice(startLine).join("\n");
								}
								// Apply truncation, respecting both line and byte limits.
								const truncation = truncateHead(selectedContent);
								let outputText: string;
								if (truncation.firstLineExceedsLimit) {
									// First line alone exceeds the byte limit. Point the model at a bash fallback.
									const firstLineSize = formatSize(Buffer.byteLength(allLines[startLine], "utf-8"));
									outputText = `[Line ${startLineDisplay} is ${firstLineSize}, exceeds ${formatSize(DEFAULT_MAX_BYTES)} limit. Use bash: sed -n '${startLineDisplay}p' ${path} | head -c ${DEFAULT_MAX_BYTES}]`;
									details = { truncation };
								} else if (truncation.truncated) {
									// Truncation occurred. Build an actionable continuation notice.
									const endLineDisplay = startLineDisplay + truncation.outputLines - 1;
									const nextOffset = endLineDisplay + 1;
									outputText = truncation.content;
									if (truncation.truncatedBy === "lines") {
										outputText += `\n\n[Showing lines ${startLineDisplay}-${endLineDisplay} of ${totalFileLines}. Use offset=${nextOffset} to continue.]`;
									} else {
										outputText += `\n\n[Showing lines ${startLineDisplay}-${endLineDisplay} of ${totalFileLines} (${formatSize(DEFAULT_MAX_BYTES)} limit). Use offset=${nextOffset} to continue.]`;
									}
									details = { truncation };
								} else if (userLimitedLines !== undefined && startLine + userLimitedLines < allLines.length) {
									// User-specified limit stopped early, but the file still has more content.
									const remaining = allLines.length - (startLine + userLimitedLines);
									const nextOffset = startLine + userLimitedLines + 1;
									outputText = `${truncation.content}\n\n[${remaining} more lines in file. Use offset=${nextOffset} to continue.]`;
								} else {
									// No truncation and no remaining user-limited content.
									outputText = truncation.content;
								}
								content = [{ type: "text", text: outputText }];
							}
```

**讲解（文本分支）**

1. 把整个文件读进内存，按 `\n` 切成行。`offset` 是从 1 开始的行号，转成数组下标；越界直接抛错。
2. 有 `limit` 时先按 `limit` 取行，否则取到文件末尾。
3. 无论有没有 `limit`，都再交给 `truncateHead`（§4.4）检查 2000 行 / 50KB 的上限。
4. 根据截断结果，在文本后面追加一句提示。四种情况：

| 情况 | 追加的提示 |
|---|---|
| 第一行就超过 50KB | 整段输出替换为提示，建议用 `sed -n 'Np' 文件 \| head -c 51200` |
| 被行数截断 | `[Showing lines 1-2000 of 3001. Use offset=2001 to continue.]` |
| 被字节数截断 | 同上，多一个 `(50.0KB limit)` |
| `limit` 提前停下，文件还有剩余 | `[N more lines in file. Use offset=M to continue.]` |

提示里直接给出下一次调用该用的 `offset`，模型照着调用就能接着读。截断时 `details = { truncation }`，否则 `details` 为 `undefined`。

`totalFileLines` 是 `split("\n")` 的长度。文件以换行结尾时，最后会多出一个空字符串，所以 3000 行的文件报告 "of 3001"。§7.1 的实验能看到这个差一。

源码：[`packages/coding-agent/src/core/tools/read.ts:206-217`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/read.ts#L206-L217)

```ts

							if (aborted) return;
							signal?.removeEventListener("abort", onAbort);
							resolve({ content, details });
						} catch (error: any) {
							signal?.removeEventListener("abort", onAbort);
							if (!aborted) reject(error);
						}
					})();
				},
			).then((result) => ({ ...result, structuredContent: toReadOutput(result.content) }));
		},
```

最后一步 `.then` 给结果加上 `structuredContent`。`toReadOutput`（`packages/coding-agent/src/core/tools/read.ts:73`）对文本文件返回文本本身，对图片返回 `{ type: "image", data, mimeType, note }`。这就是 §2.1 里 `structuredContent` 和 `content[0].text` 相同的原因。

**往哪走**：§4.4 `truncateHead`。

### 4.4 truncateHead() 和 truncateTail()

**调用位置**：read 调用 `truncateHead(selectedContent)`；bash 经 `OutputAccumulator.snapshot()` 调用 `truncateTail`（§4.8）。grep、find、ls 也用这两个函数。

源码：[`packages/coding-agent/src/core/tools/truncate.ts:47-56`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/truncate.ts#L47-L56)

```ts
function splitLinesForCounting(content: string): string[] {
	if (content.length === 0) {
		return [];
	}
	const lines = content.split("\n");
	if (content.endsWith("\n")) {
		lines.pop();
	}
	return lines;
}
```

源码：[`packages/coding-agent/src/core/tools/truncate.ts:78-119`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/truncate.ts#L78-L119)

```ts
export function truncateHead(content: string, options: TruncationOptions = {}): TruncationResult {
	const maxLines = options.maxLines ?? DEFAULT_MAX_LINES;
	const maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;

	const totalBytes = Buffer.byteLength(content, "utf-8");
	const lines = splitLinesForCounting(content);
	const totalLines = lines.length;

	// Check if no truncation needed
	if (totalLines <= maxLines && totalBytes <= maxBytes) {
		return {
			content,
			truncated: false,
			truncatedBy: null,
			totalLines,
			totalBytes,
			outputLines: totalLines,
			outputBytes: totalBytes,
			lastLinePartial: false,
			firstLineExceedsLimit: false,
			maxLines,
			maxBytes,
		};
	}

	// Check if first line alone exceeds byte limit
	const firstLineBytes = Buffer.byteLength(lines[0], "utf-8");
	if (firstLineBytes > maxBytes) {
		return {
			content: "",
			truncated: true,
			truncatedBy: "bytes",
			totalLines,
			totalBytes,
			outputLines: 0,
			outputBytes: 0,
			lastLinePartial: false,
			firstLineExceedsLimit: true,
			maxLines,
			maxBytes,
		};
	}
```

源码：[`packages/coding-agent/src/core/tools/truncate.ts:121-160`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/truncate.ts#L121-L160)

```ts
	// Collect complete lines that fit
	const outputLinesArr: string[] = [];
	let outputBytesCount = 0;
	let truncatedBy: "lines" | "bytes" = "lines";

	for (let i = 0; i < lines.length && i < maxLines; i++) {
		const line = lines[i];
		const lineBytes = Buffer.byteLength(line, "utf-8") + (i > 0 ? 1 : 0); // +1 for newline

		if (outputBytesCount + lineBytes > maxBytes) {
			truncatedBy = "bytes";
			break;
		}

		outputLinesArr.push(line);
		outputBytesCount += lineBytes;
	}

	// If we exited due to line limit
	if (outputLinesArr.length >= maxLines && outputBytesCount <= maxBytes) {
		truncatedBy = "lines";
	}

	const outputContent = outputLinesArr.join("\n");
	const finalOutputBytes = Buffer.byteLength(outputContent, "utf-8");

	return {
		content: outputContent,
		truncated: true,
		truncatedBy,
		totalLines,
		totalBytes,
		outputLines: outputLinesArr.length,
		outputBytes: finalOutputBytes,
		lastLinePartial: false,
		firstLineExceedsLimit: false,
		maxLines,
		maxBytes,
	};
}
```

**讲解**

`splitLinesForCounting` 数行时去掉末尾换行产生的空串，所以 `TruncationResult.totalLines` 是 3000，而 read 自己算的 `totalFileLines` 是 3001。两个数字来自两种切法。

`truncateHead` 的规则：

- 行数和字节数都没超，原样返回。
- 第一行就超过字节上限，返回空内容，`firstLineExceedsLimit: true`。它不输出半行，read 据此给出 `sed` 的建议。
- 否则从头逐行累加，直到行数到 2000 或者再加一行就超过 51200 字节。每行的字节数加 1 算上换行符（第一行除外）。输出总是完整的行。

`truncateTail` 是镜像：从最后一行往前收集，用 `unshift` 插到前面。唯一的区别是边界情况：

源码：[`packages/coding-agent/src/core/tools/truncate.ts:199-218`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/truncate.ts#L199-L218)

```ts
	for (let i = lines.length - 1; i >= 0 && outputLinesArr.length < maxLines; i--) {
		const line = lines[i];
		const lineBytes = Buffer.byteLength(line, "utf-8") + (outputLinesArr.length > 0 ? 1 : 0); // +1 for newline

		if (outputBytesCount + lineBytes > maxBytes) {
			truncatedBy = "bytes";
			// Edge case: if we haven't added ANY lines yet and this line exceeds maxBytes,
			// take the end of the line (partial)
			if (outputLinesArr.length === 0) {
				const truncatedLine = truncateStringToBytesFromEnd(line, maxBytes);
				outputLinesArr.unshift(truncatedLine);
				outputBytesCount = Buffer.byteLength(truncatedLine, "utf-8");
				lastLinePartial = true;
			}
			break;
		}

		outputLinesArr.unshift(line);
		outputBytesCount += lineBytes;
	}
```

如果最后一行本身就超过字节上限（例如命令输出一行 1MB 的 JSON），`truncateTail` 保留这一行的结尾，`lastLinePartial: true`。`truncateStringToBytesFromEnd`（`packages/coding-agent/src/core/tools/truncate.ts:247`）从末尾往前数 `maxBytes` 个字节，再向后跳过 UTF-8 的续字节（`10xxxxxx`），保证不切开一个多字节字符。

为什么 read 从头截、bash 从尾截：读文件时开头最有用，模型可以用 `offset` 继续；命令输出的结尾通常是错误信息和最终结果，而完整输出已经存进临时文件，需要时可以再读。

**往哪走**：§4.5 bash 工具。

### 4.5 bash：createShellToolDefinition() 的 execute

**调用位置**：`createBashToolDefinition(cwd, options)` 直接返回 `createShellToolDefinition(cwd, bashToolConfig, options)`（`packages/coding-agent/src/core/tools/bash.ts:427`）。powershell 工具用同一个函数，只是配置不同。

参数 schema 只有两个字段：`command` 和可选的 `timeout`（秒，没有默认超时，`packages/coding-agent/src/core/tools/bash.ts:40`）。

#### ① 准备：命令前缀、环境变量、输出累加器、进度节流

源码：[`packages/coding-agent/src/core/tools/bash.ts:261-294`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/bash.ts#L261-L294)

```ts
		async execute(
			_toolCallId,
			{ command, timeout }: { command: string; timeout?: number },
			signal?: AbortSignal,
			onUpdate?,
			ctx?: ExtensionContext,
		) {
			const resolvedCommand = commandPrefix ? `${commandPrefix}\n${command}` : command;
			const spawnContext = resolveSpawnContext(
				resolvedCommand,
				ctx?.cwd || cwd,
				spawnHook,
				exposeSessionEnvironment,
				ctx,
			);
			const output = new OutputAccumulator({ tempFilePrefix: config.tempFilePrefix });
			let acceptingOutput = true;
			let updateTimer: NodeJS.Timeout | undefined;
			let updateDirty = false;
			let lastUpdateAt = 0;

			const emitOutputUpdate = () => {
				if (!onUpdate || !updateDirty) return;
				updateDirty = false;
				lastUpdateAt = Date.now();
				const snapshot = output.snapshot({ persistIfTruncated: true });
				onUpdate({
					content: [{ type: "text", text: snapshot.content || "" }],
					details: {
						truncation: snapshot.truncation.truncated ? snapshot.truncation : undefined,
						fullOutputPath: snapshot.fullOutputPath,
					},
				});
			};
```

**讲解**

- `commandPrefix` 来自设置 `shellCommandPrefix`，拼在命令前面，用换行分隔。常用来 `source` 一个环境脚本。
- `resolveSpawnContext`（`packages/coding-agent/src/core/tools/bash.ts:186`）复制当前进程的环境变量，先删掉继承来的 `PI_SESSION_ID`、`PI_SESSION_FILE`、`PI_PROVIDER`、`PI_MODEL`、`PI_REASONING_LEVEL`，再根据 `ctx` 重新设置。所以命令里可以用 `$PI_SESSION_FILE` 找到当前会话文件；工具描述里的准则 "You can inspect PI_* environment variables" 说的就是它（`packages/coding-agent/src/core/tools/bash.ts:47`）。`spawnHook` 是给扩展的最后一次改写机会。
- `OutputAccumulator`（§4.8）负责收集输出。
- `emitOutputUpdate` 把当前输出的快照通过 `onUpdate` 发出去，M2 把它变成 `tool_execution_update` 事件。快照用 `persistIfTruncated: true`，一旦超限就开临时文件，界面上能看到完整输出的路径。

#### ② 收集输出和收尾

源码：[`packages/coding-agent/src/core/tools/bash.ts:303-356`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/bash.ts#L303-L356)

```ts
			const scheduleOutputUpdate = () => {
				if (!onUpdate) return;
				updateDirty = true;
				const delay = BASH_UPDATE_THROTTLE_MS - (Date.now() - lastUpdateAt);
				if (delay <= 0) {
					clearUpdateTimer();
					emitOutputUpdate();
					return;
				}
				updateTimer ??= setTimeout(() => {
					updateTimer = undefined;
					emitOutputUpdate();
				}, delay);
			};

			if (onUpdate) {
				onUpdate({ content: [], details: undefined });
			}

			const handleData = (data: Buffer) => {
				if (!acceptingOutput) return;
				output.append(data);
				scheduleOutputUpdate();
			};

			const finishOutput = async () => {
				acceptingOutput = false;
				output.finish();
				clearUpdateTimer();
				emitOutputUpdate();
				const snapshot = output.snapshot({ persistIfTruncated: true });
				await output.closeTempFile();
				return snapshot;
			};

			const formatOutput = (snapshot: Awaited<ReturnType<typeof finishOutput>>, emptyText = "(no output)") => {
				const truncation = snapshot.truncation;
				let text = snapshot.content || emptyText;
				let details: BashToolDetails | undefined;
				if (truncation.truncated) {
					details = { truncation, fullOutputPath: snapshot.fullOutputPath };
					const startLine = truncation.totalLines - truncation.outputLines + 1;
					const endLine = truncation.totalLines;
					if (truncation.lastLinePartial) {
						const lastLineSize = formatSize(output.getLastLineBytes());
						text += `\n\n[Showing last ${formatSize(truncation.outputBytes)} of line ${endLine} (line is ${lastLineSize}). Full output: ${snapshot.fullOutputPath}]`;
					} else if (truncation.truncatedBy === "lines") {
						text += `\n\n[Showing lines ${startLine}-${endLine} of ${truncation.totalLines}. Full output: ${snapshot.fullOutputPath}]`;
					} else {
						text += `\n\n[Showing lines ${startLine}-${endLine} of ${truncation.totalLines} (${formatSize(DEFAULT_MAX_BYTES)} limit). Full output: ${snapshot.fullOutputPath}]`;
					}
				}
				return { text, details };
			};
```

**讲解**

`scheduleOutputUpdate` 是节流：距上次发出不到 `BASH_UPDATE_THROTTLE_MS`（100ms，`packages/coding-agent/src/core/tools/renderers/bash.ts:19`）就设一个定时器，到点再发；定时器已经存在时（`??=`）不重复设置。输出很密集时，`onUpdate` 每 100ms 最多一次，不会每个 chunk 一次。开始执行前先发一次空的更新（319 行），让界面立即显示"正在运行"。

`finishOutput` 停止接收输出，发出最后一次更新，取最终快照，关闭临时文件。

`formatOutput` 在被截断时追加一句提示。和 read 不同，提示里给的是临时文件路径 `Full output: …`，因为命令不能像文件那样用 `offset` 重读。三种提示分别对应 `lastLinePartial`、按行截断、按字节截断。

#### ③ 执行和结果

源码：[`packages/coding-agent/src/core/tools/bash.ts:358-411`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/bash.ts#L358-L411)

```ts
			const appendStatus = (text: string, status: string) => `${text ? `${text}\n\n` : ""}${status}`;
			const startedAt = performance.now();

			try {
				let exitCode: number | null;
				try {
					const result = await ops.exec(spawnContext.command, spawnContext.cwd, {
						onData: handleData,
						signal,
						timeout,
						env: spawnContext.env,
					});
					exitCode = result.exitCode;
				} catch (err) {
					const snapshot = await finishOutput();
					const { text } = formatOutput(snapshot, "");
					if (err instanceof Error && err.message === "aborted") {
						throw new Error(appendStatus(text, "Command aborted"));
					}
					if (err instanceof Error && err.message.startsWith("timeout:")) {
						const timeoutSecs = err.message.split(":")[1];
						throw new Error(appendStatus(text, `Command timed out after ${timeoutSecs} seconds`));
					}
					throw err;
				}

				const snapshot = await finishOutput();
				const { text: outputText, details } = formatOutput(snapshot);
				if (exitCode === null) {
					throw new Error(appendStatus(outputText, "Command terminated without an exit code"));
				}
				const wallTimeSeconds = Math.round((performance.now() - startedAt) / 100) / 10;
				const fullOutput = await output.readFullOutput(STRUCTURED_OUTPUT_MAX_BYTES);
				const structuredContent: BashToolOutput = {
					output: fullOutput.content,
					truncated: fullOutput.truncated,
					...(fullOutput.truncated && snapshot.fullOutputPath
						? { full_output_path: snapshot.fullOutputPath }
						: {}),
					exit_code: exitCode,
					wall_time_seconds: wallTimeSeconds,
				};
				if (exitCode !== 0) {
					return {
						content: [{ type: "text", text: appendStatus(outputText, `Command exited with code ${exitCode}`) }],
						details,
						structuredContent,
						isError: true,
					};
				}
				return { content: [{ type: "text", text: outputText }], details, structuredContent };
			} finally {
				clearUpdateTimer();
			}
```

**讲解**

`ops.exec` 返回退出码，或者抛出两种约定好的错误：消息为 `"aborted"` 的中止，消息以 `"timeout:"` 开头的超时（§4.6）。bash 工具把它们翻译成模型能读懂的文本：已有的输出加上 `Command aborted` 或 `Command timed out after N seconds`，然后抛出 `Error`。M2 的 `executePreparedToolCall` 捕获异常，变成 `isError: true` 的结果（M2 §4.12），所以模型仍然能看到超时前的输出。

正常结束时：

- 退出码非 0 不抛异常，而是返回 `isError: true` 的结果，文本末尾追加 `Command exited with code N`。这样 `details` 和 `structuredContent` 都还在。`AgentToolResult` 的注释要求"失败时抛异常或返回 `isError: true`，不要只在 `content` 里描述"，这里用的是后一种。
- `structuredContent` 里的 `output` 不受 50KB 限制，上限是 1MB（`STRUCTURED_OUTPUT_MAX_BYTES`，`packages/coding-agent/src/core/tools/bash.ts:24`），超过时保留头尾各一半（§4.8 `readFullOutput`）。程序调用方（例如 codemode 脚本）拿到的是它，模型拿到的是截断过的 `content`。
- `wall_time_seconds` 精确到 0.1 秒。

**往哪走**：§4.6 `ops.exec` 的默认实现。

### 4.6 createLocalShellOperations()：启动进程

**调用位置**：`createShellToolDefinition` 第一行，`options?.operations` 没有提供时，用 `createLocalBashOperations()` → `createLocalShellOperations("bash", …)`（`packages/coding-agent/src/core/tools/bash.ts:174`）。

`BashOperations` 只有一个方法 `exec(command, cwd, { onData, signal, timeout, env })`（`packages/coding-agent/src/core/tools/bash.ts:75`）。read、edit、write 也各有一个 `XxxOperations` 接口，注释都写着"Override these to delegate … to remote systems (for example SSH)"：替换掉它，同一个工具就能在远程机器上执行，截断、提示文本、超时处理都不用改。

源码：[`packages/coding-agent/src/core/tools/bash.ts:97-129`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/bash.ts#L97-L129)

```ts
export function createLocalShellOperations(shellName: string, resolveShellConfig: () => ShellConfig): BashOperations {
	return {
		exec: async (command, cwd, { onData, signal, timeout, env }) => {
			const timeoutMs = resolveTimeoutMs(timeout);
			if (signal?.aborted) {
				throw new Error("aborted");
			}
			const shellConfig = resolveShellConfig();
			try {
				await fsAccess(cwd, constants.F_OK);
			} catch {
				throw new Error(`Working directory does not exist: ${cwd}\nCannot execute ${shellName} commands.`);
			}

			const commandFromStdin = shellConfig.commandTransport === "stdin";
			const child = spawn(shellConfig.shell, commandFromStdin ? shellConfig.args : [...shellConfig.args, command], {
				cwd,
				detached: process.platform !== "win32",
				env: env ?? getShellEnv(),
				stdio: [commandFromStdin ? "pipe" : "ignore", "pipe", "pipe"],
				windowsHide: true,
			});
			if (commandFromStdin) {
				child.stdin?.on("error", () => {});
				child.stdin?.end(command);
			}
			if (child.pid) trackDetachedChildPid(child.pid);
			let timedOut = false;
			let timeoutHandle: NodeJS.Timeout | undefined;
			const onAbort = () => {
				if (child.pid) killProcessTree(child.pid);
			};

```

**讲解（启动）**

- `resolveTimeoutMs`（`packages/coding-agent/src/core/tools/bash.ts:27`）把秒转成毫秒，拒绝 0、负数、非有限数和超过 `setTimeout` 上限（2147483647ms）的值。
- 先检查 `cwd` 存在，否则 spawn 的错误信息不好懂。
- `spawn(shell, [...args, command])`：命令作为 `bash -c` 的参数传入（`commandTransport` 为 `"stdin"` 的 shell 改从标准输入传）。
- **`detached: true`**（非 Windows）。子进程调用 `setsid`，成为新进程组的组长，进程组 id 等于它的 pid。命令再启动的子进程默认留在这个组里。后面的 `killProcessTree(pid)` 就是对整个组发信号。
- stdin 是 `"ignore"`，命令读不到输入，不会卡在等待输入上。
- `trackDetachedChildPid` 把 pid 记进一个全局集合。detached 的进程组不会随 pi 退出而收到 SIGHUP，所以 print、rpc、交互模式在收到退出信号时调用 `killTrackedDetachedChildren()` 逐个杀掉（例如 `packages/coding-agent/src/modes/print-mode.ts:58`）。

源码：[`packages/coding-agent/src/core/tools/bash.ts:130-166`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/bash.ts#L130-L166)

```ts
			try {
				// Set timeout if provided.
				if (timeoutMs !== undefined) {
					timeoutHandle = setTimeout(() => {
						timedOut = true;
						if (child.pid) killProcessTree(child.pid);
					}, timeoutMs);
				}
				// Stream stdout and stderr.
				child.stdout?.on("data", onData);
				child.stderr?.on("data", onData);
				// Handle abort signal by killing the entire process tree.
				if (signal) {
					if (signal.aborted) onAbort();
					else signal.addEventListener("abort", onAbort, { once: true });
				}
				// Handle shell spawn errors and wait for the process to terminate without hanging
				// on inherited stdio handles held by detached descendants.
				const exitCode = await waitForChildProcess(child);
				if (signal?.aborted) {
					throw new Error("aborted");
				}
				if (timedOut) {
					throw new Error(`timeout:${timeout}`);
				}
				// A signal-killed shell has no exit code. Use the standard shell convention so
				// callers do not mistake the termination for a successful command.
				const signalCode = child.signalCode;
				return { exitCode: exitCode ?? (signalCode ? 128 + (osConstants.signals[signalCode] ?? 0) : 1) };
			} finally {
				if (child.pid) untrackDetachedChildPid(child.pid);
				if (timeoutHandle) clearTimeout(timeoutHandle);
				if (signal) signal.removeEventListener("abort", onAbort);
			}
		},
	};
}
```

**讲解（等待和结束）**

- 超时：定时器到点设置 `timedOut = true` 并杀掉进程树。进程退出后 `waitForChildProcess` 返回，然后抛出 `timeout:<秒数>`。
- 中止：`signal` 的 abort 事件同样杀掉进程树，退出后抛出 `aborted`。
- `waitForChildProcess`（`packages/coding-agent/src/utils/child-process.ts:49`）不等 `close` 事件。后台子进程可能继承了 stdout，一直不关闭管道，等 `close` 就会永远挂住。它在 `exit` 之后等管道空闲 100ms 再结束，期间每来一个 chunk 就重新计时，不会丢掉正在写的输出。
- 被信号杀死的 shell 没有退出码，按 shell 的惯例换成 `128 + 信号编号`，例如 SIGKILL 是 137。这样调用方不会把它当成成功。
- `finally` 里取消跟踪、清掉定时器、移除监听器。

**往哪走**：§4.7 `killProcessTree`。

### 4.7 killProcessTree()：杀掉整个进程组

**调用位置**：§4.6 的超时定时器和 `onAbort`；退出时的 `killTrackedDetachedChildren()`。

源码：[`packages/coding-agent/src/utils/shell.ts:217-264`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/utils/shell.ts#L217-L264)

```ts
/**
 * Kill a process and all its children (cross-platform)
 */
export function killProcessTree(pid: number): void {
	if (process.platform === "win32") {
		// Use the trusted System32 executable so cleanup does not depend on PATH.
		try {
			const child = spawn(
				join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe"),
				["/F", "/T", "/PID", String(pid)],
				{
					stdio: "ignore",
					detached: true,
					windowsHide: true,
				},
			);
			// A failed spawn emits "error" asynchronously; consume it to avoid crashing Node.
			child.once("error", () => {});
		} catch {
			// Ignore errors if taskkill fails.
		}
	} else {
		if (process.platform === "darwin") {
			// macOS signals group members individually after releasing the group lock. A killed
			// child can wake its waiting shell before that shell is signalled, letting `a; b`
			// start b during cancellation (#11252). Stop the group first to reduce this race;
			// this is best-effort, not an atomic freeze. SIGKILL also kills stopped processes.
			// Linux holds tasklist_lock across group signalling; child exit notification needs
			// its write lock, so the shell is signalled before observing the killed child's exit.
			try {
				process.kill(-pid, "SIGSTOP");
			} catch {
				// A failed stop must not prevent the kill attempt or its single-process fallback.
			}
		}
		// Use SIGKILL on Unix/Linux/Mac
		try {
			process.kill(-pid, "SIGKILL");
		} catch {
			// Fallback to killing just the child if process group kill fails
			try {
				process.kill(pid, "SIGKILL");
			} catch {
				// Process already dead
			}
		}
	}
}
```

**讲解**

在 Unix 上，`process.kill(-pid, "SIGKILL")` 的负号表示"发给进程组 `pid` 里的所有进程"。因为 §4.6 用 `detached: true` 让 shell 成了组长，`sleep 30 &` 这类后台子进程也在组里，一起被杀掉。如果只杀 shell，后台进程会成为孤儿继续运行。§7.2 的实验验证了这一点。

macOS 上先发 `SIGSTOP` 冻住整个组。注释解释了原因：macOS 逐个给组员发信号，一个子进程被杀后可能先唤醒正在等待它的 shell，shell 在自己被杀之前执行了 `a; b` 里的 `b`。先冻住可以缩小这个窗口。SIGKILL 对已停止的进程同样有效。

杀进程组失败（例如组已经不存在）时，退回只杀这一个 pid。Windows 用 `taskkill /F /T` 杀进程树。

用的是 SIGKILL，不给进程清理的机会。对 agent 来说，命令超时或被中止后不再需要它的任何输出，立即释放比体面退出更重要。

**往哪走**：§4.8 输出累加器。

### 4.8 OutputAccumulator：只保留尾部，超限写临时文件

**调用位置**：bash 的 `handleData` 调用 `output.append(data)`；`emitOutputUpdate`、`finishOutput` 调用 `output.snapshot()`。

源码：[`packages/coding-agent/src/core/tools/output-accumulator.ts:57-89`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/output-accumulator.ts#L57-L89)

```ts
	constructor(options: OutputAccumulatorOptions = {}) {
		this.maxLines = options.maxLines ?? DEFAULT_MAX_LINES;
		this.maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;
		this.maxRollingBytes = Math.max(this.maxBytes * 2, 1);
		this.tempFilePrefix = options.tempFilePrefix ?? "pi-output";
	}

	append(data: Buffer): void {
		if (this.finished) {
			throw new Error("Cannot append to a finished output accumulator");
		}

		this.totalRawBytes += data.length;
		this.appendDecodedText(this.decoder.decode(data, { stream: true }));

		if (this.tempFileStream || this.shouldUseTempFile()) {
			this.ensureTempFile();
			this.tempFileStream?.write(data);
		} else if (data.length > 0) {
			this.rawChunks.push(data);
		}
	}

	finish(): void {
		if (this.finished) {
			return;
		}
		this.finished = true;
		this.appendDecodedText(this.decoder.decode());
		if (this.shouldUseTempFile()) {
			this.ensureTempFile();
		}
	}
```

**讲解（追加）**

累加器同时维护两份数据：

- **解码后的尾部** `tailText`。`TextDecoder` 用 `stream: true` 解码，一个多字节字符被切在两个 chunk 之间也能正确拼起来。`appendDecodedText`（`packages/coding-agent/src/core/tools/output-accumulator.ts:182`）同时数行数；`tailText` 超过 `maxRollingBytes * 2`（默认 200KB）时裁掉前面的部分，只留最后 100KB。内存占用有上限，命令输出 1GB 也不会撑爆。
- **原始字节**。没超限时存在 `rawChunks` 数组里；一旦总字节数或行数超过上限（`shouldUseTempFile`，`packages/coding-agent/src/core/tools/output-accumulator.ts:239`），就打开临时文件，把已有的 chunk 写进去，之后的 chunk 直接写文件，不再留在内存里。

源码：[`packages/coding-agent/src/core/tools/output-accumulator.ts:91-119`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/output-accumulator.ts#L91-L119)

```ts
	snapshot(options: { persistIfTruncated?: boolean } = {}): OutputSnapshot {
		const tailTruncation = truncateTail(this.getSnapshotText(), {
			maxLines: this.maxLines,
			maxBytes: this.maxBytes,
		});
		const truncated = this.totalLines > this.maxLines || this.totalDecodedBytes > this.maxBytes;
		const truncatedBy = truncated
			? (tailTruncation.truncatedBy ?? (this.totalDecodedBytes > this.maxBytes ? "bytes" : "lines"))
			: null;
		const truncation: TruncationResult = {
			...tailTruncation,
			truncated,
			truncatedBy,
			totalLines: this.totalLines,
			totalBytes: this.totalDecodedBytes,
			maxLines: this.maxLines,
			maxBytes: this.maxBytes,
		};

		if (options.persistIfTruncated && truncation.truncated) {
			this.ensureTempFile();
		}

		return {
			content: truncation.content,
			truncation,
			fullOutputPath: this.tempFilePath,
		};
	}
```

**讲解（快照）**

`snapshot` 对尾部调用 `truncateTail`，再用累加器自己数的总行数和总字节数覆盖 `totalLines`、`totalBytes`、`truncated`。原因是 `tailText` 已经被裁过，`truncateTail` 看到的只是尾部，算出的总数不对。

临时文件由 `createOutputFileStream`（`packages/coding-agent/src/utils/output-files.ts:31`）创建：路径是系统临时目录下的 `pi-bash-<16 位随机十六进制>.log`，权限 `0o600`，打开方式 `wx`（文件已存在就失败，不会跟随别人预先放好的符号链接）。

`readFullOutput(maxBytes)`（`packages/coding-agent/src/core/tools/output-accumulator.ts:149`）给 `structuredContent` 用：没开临时文件就拼接 `rawChunks`；文件不超过 `maxBytes` 就整个读出；否则读头尾各一半，中间放 `[... N bytes omitted ...]`。

**往哪走**：§4.9 是一个旁支：扩展执行命令用的 `execCommand`。主流程继续看 §4.10 edit。

### 4.9 旁支：execCommand()（core/exec.ts）

**调用位置**：扩展 API `pi.exec(command, args, options)`（`packages/coding-agent/src/core/extensions/loader.ts:414`）。bash 工具不用它。

源码：[`packages/coding-agent/src/core/exec.ts:47-98`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/exec.ts#L47-L98)

```ts
export async function execCommand(
	command: string,
	args: string[],
	cwd: string,
	options?: ExecOptions,
): Promise<ExecResult> {
	const maxBuffer = options?.maxBuffer ?? DEFAULT_EXEC_MAX_BUFFER;
	if (!Number.isInteger(maxBuffer) || maxBuffer < 0 || maxBuffer > bufferConstants.MAX_STRING_LENGTH) {
		throw new RangeError(`maxBuffer must be an integer between 0 and ${bufferConstants.MAX_STRING_LENGTH} bytes`);
	}

	const proc = spawn(command, args, {
		cwd,
		shell: false,
		stdio: ["ignore", "pipe", "pipe"],
	});

	let stdout = "";
	let stderr = "";
	// Decode across chunks: a multi-byte UTF-8 character can be split between two chunks,
	// and decoding each chunk separately would corrupt it (earendil-works/pi#11312).
	const stdoutDecoder = new StringDecoder("utf8");
	const stderrDecoder = new StringDecoder("utf8");
	let outputBytes = 0;
	let truncated = false;
	let killed = false;
	let timeoutId: NodeJS.Timeout | undefined;
	let forceKillId: NodeJS.Timeout | undefined;

	const killProcess = () => {
		if (killed) return;
		killed = true;
		proc.kill("SIGTERM");
		forceKillId = setTimeout(() => {
			// `proc.killed` only says a signal was sent, so check whether the child actually exited.
			if (proc.exitCode === null && proc.signalCode === null) {
				proc.kill("SIGKILL");
			}
		}, FORCE_KILL_DELAY_MS);
	};

	if (options?.signal) {
		if (options.signal.aborted) {
			killProcess();
		} else {
			options.signal.addEventListener("abort", killProcess, { once: true });
		}
	}

	if (options?.timeout && options.timeout > 0) {
		timeoutId = setTimeout(killProcess, options.timeout);
	}
```

源码：[`packages/coding-agent/src/core/exec.ts:100-158`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/exec.ts#L100-L158)

```ts
	// Keep only bytes within the combined cap (#11360). Close both pipes on overflow so writing
	// descendants cannot keep waitForChildProcess busy after the child exits. Pausing is not
	// enough: Node resumes the child's stdio on exit.
	const takeWithinCap = (data: Buffer): Buffer | undefined => {
		if (truncated) return undefined;
		const remaining = maxBuffer - outputBytes;
		if (data.length <= remaining) {
			outputBytes += data.length;
			return data;
		}
		truncated = true;
		proc.stdout?.destroy();
		proc.stderr?.destroy();
		killProcess();
		return remaining > 0 ? data.subarray(0, remaining) : undefined;
	};

	proc.stdout?.on("data", (data: Buffer) => {
		const chunk = takeWithinCap(data);
		if (chunk) stdout += stdoutDecoder.write(chunk);
	});

	proc.stderr?.on("data", (data: Buffer) => {
		const chunk = takeWithinCap(data);
		if (chunk) stderr += stderrDecoder.write(chunk);
	});

	let code: number | null;
	try {
		// Do not hang on inherited stdio handles held open by detached descendants.
		code = await waitForChildProcess(proc);
	} catch (error) {
		if (!(error instanceof Error)) throw error;
		// Child-process errors (e.g. ENOENT) are failed executions, not host failures.
		code = 1;
		if (!stderr) stderr = error.message;
	} finally {
		clearTimeout(timeoutId);
		clearTimeout(forceKillId);
		options?.signal?.removeEventListener("abort", killProcess);
	}

	// On truncation, discard any incomplete UTF-8 suffix buffered by either decoder.
	// Flushing it would append a replacement character that was not in the accepted output.
	if (!truncated) {
		stdout += stdoutDecoder.end();
		stderr += stderrDecoder.end();
	}

	const exitCode = code ?? 0;
	return {
		stdout,
		stderr,
		// A truncated result must never read as success.
		code: truncated && exitCode === 0 ? 1 : exitCode,
		killed,
		truncated,
	};
}
```

**讲解**

`execCommand` 和 bash 工具的进程管理有三处不同：

| | bash 工具（§4.6） | `execCommand` |
|---|---|---|
| 启动方式 | 通过 shell，`detached: true` 新建进程组 | `shell: false` 直接执行程序，不新建进程组 |
| 结束方式 | `killProcessTree` 对整个组发 SIGKILL | 先对子进程发 SIGTERM，5 秒后还没退出再发 SIGKILL |
| 输出 | 流式回调，尾部截断，超限写临时文件 | 全部收集成字符串，超过 `maxBuffer`（默认 256MB）就关管道、杀进程，`truncated: true` |

`execCommand` 面向的是扩展调用 `git`、`gh` 这类确定的程序，参数作为数组传入，没有 shell 注入的问题。它给子进程体面退出的机会。超过 `maxBuffer` 时退出码强制非 0（154 行），被截断的结果不会被当成成功。spawn 本身失败（例如程序不存在，ENOENT）时返回 `code: 1` 和错误信息，不抛异常。

两者共用 `waitForChildProcess`，都不会被继承了管道的后台进程挂住。

**往哪走**：§4.10 edit 工具。

### 4.10 edit：读、匹配、替换、写回

**调用位置**：M2 的 `prepareToolCall` 先调用 `tool.prepareArguments`，再校验参数（M2 §4.11），然后才到 `execute`。

参数 schema 是 `{ path, edits: [{ oldText, newText }] }`（`packages/coding-agent/src/core/tools/edit.ts:21`）。schema 的描述和工具描述反复强调三条规则：每个 `oldText` 在原文件里唯一；各处替换不重叠；每处都针对原文件匹配，不是依次在上一处替换的结果上匹配。

#### ① prepareArguments：兼容不规范的参数

源码：[`packages/coding-agent/src/core/tools/edit.ts:101-132`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit.ts#L101-L132)

```ts
function prepareEditArguments(input: unknown): EditToolInput {
	if (!input || typeof input !== "object") {
		return input as EditToolInput;
	}

	const args = input as Record<string, unknown>;

	// Some models (Opus 4.6, GLM-5.1) send edits as a JSON string instead of an array.
	// Others send a single edit object instead of a one-element edits array.
	if (typeof args.edits === "string") {
		try {
			const parsed = JSON.parse(args.edits);
			if (Array.isArray(parsed)) {
				args.edits = parsed;
			} else if (isSingleEditInput(parsed)) {
				args.edits = [parsed];
			}
		} catch {}
	} else if (isSingleEditInput(args.edits)) {
		args.edits = [args.edits];
	}

	const legacy = args as LegacyEditToolInput;
	if (typeof legacy.oldText !== "string" || typeof legacy.newText !== "string") {
		return args as EditToolInput;
	}

	const edits = Array.isArray(legacy.edits) ? [...legacy.edits] : [];
	edits.push({ oldText: legacy.oldText, newText: legacy.newText });
	const { oldText: _oldText, newText: _newText, ...rest } = legacy;
	return { ...rest, edits } as EditToolInput;
}
```

**讲解**

注释点名了几种模型的习惯：把 `edits` 写成 JSON 字符串、写成单个对象而不是数组。还有旧版本的参数格式：顶层直接是 `oldText`、`newText`。`prepareEditArguments` 把这些都改写成标准的 `edits` 数组。它在 schema 校验之前运行，所以这些写法不会因为校验失败而报错。改写失败（例如 JSON 解析不了）就原样返回，交给校验去报错。

`lab/m3-tools.ts edit` 的最后两行输出展示了两种改写：

```
…省略…
[prepareArguments 旧格式] {"path":"a.ts","edits":[{"oldText":"x","newText":"y"}]}
[prepareArguments edits 是 JSON 字符串] {"path":"a.ts","edits":[{"oldText":"x","newText":"y"}]}
```

#### ② execute

源码：[`packages/coding-agent/src/core/tools/edit.ts:157-211`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit.ts#L157-L211)

```ts
		async execute(_toolCallId, input: EditToolInput, signal?: AbortSignal, _onUpdate?, ctx?: ExtensionContext) {
			const { path, edits } = validateEditInput(input);
			const absolutePath = resolveToCwd(path, ctx?.cwd || cwd);

			return withFileMutationQueue(absolutePath, async () => {
				// Do not reject from an abort event listener here: that would release the
				// mutation queue while an in-flight filesystem operation may still finish.
				// Checking signal.aborted after each await observes the same aborts while
				// keeping the queue locked until the current operation has settled.
				const throwIfAborted = (): void => {
					if (signal?.aborted) throw new Error("Operation aborted");
				};

				throwIfAborted();

				// Check if file exists.
				try {
					await ops.access(absolutePath);
				} catch (error: unknown) {
					throwIfAborted();
					const errorMessage =
						error instanceof Error && "code" in error ? `Error code: ${error.code}` : String(error);
					throw new Error(`Could not edit file: ${path}. ${errorMessage}.`);
				}
				throwIfAborted();

				// Read the file.
				const buffer = await ops.readFile(absolutePath);
				const rawContent = buffer.toString("utf-8");
				throwIfAborted();

				// Strip BOM before matching. The model will not include an invisible BOM in oldText.
				const { bom, text: content } = splitBom(rawContent);
				const originalEnding = detectLineEnding(content);
				const normalizedContent = normalizeToLF(content);
				const { baseContent, newContent } = applyEditsToNormalizedContent(normalizedContent, edits, path);
				throwIfAborted();

				const finalContent = bom + restoreLineEndings(newContent, originalEnding);
				await ops.writeFile(absolutePath, finalContent);
				throwIfAborted();

				const diffResult = generateDiffString(baseContent, newContent);
				const patch = generateUnifiedPatch(path, baseContent, newContent);
				return {
					content: [
						{
							type: "text",
							text: `Successfully replaced ${edits.length} block(s) in ${path}.`,
						},
					],
					details: { diff: diffResult.diff, patch, firstChangedLine: diffResult.firstChangedLine },
				};
			});
		},
```

**讲解**

整个读改写过程包在 `withFileMutationQueue(absolutePath, …)` 里（§4.14），同一个文件的修改一个接一个执行。

注释解释了为什么这里不像 read 那样用 abort 监听器立即 reject：那会提前释放队列，而正在进行的写操作可能稍后才完成，下一个排队的修改就会读到旧内容。所以改为在每个 `await` 之后检查 `signal.aborted`，让队列一直锁到当前操作真正结束。

文本处理分四步：

1. `splitBom` 去掉开头的 BOM（`\uFEFF`，`packages/coding-agent/src/utils/text.ts:2`）。模型看不到 BOM，`oldText` 里不会有它。
2. `detectLineEnding` 看第一个换行是 `\r\n` 还是 `\n`，记下原来的换行风格；`normalizeToLF` 把全文统一成 `\n`。
3. `applyEditsToNormalizedContent`（§4.12）在 LF 文本上匹配和替换。
4. 写回时 `restoreLineEndings` 把 `\n` 换回原来的换行，再在前面加回 BOM。

所以一个 BOM + CRLF 的 Windows 文件，经过 edit 后仍然是 BOM + CRLF。§7.3 的实验输出里有字节级的验证（`ef bb bf` 开头，`0d 0a` 换行）。

返回给模型的只有一句 `Successfully replaced N block(s) in path.`，diff 放在 `details` 里给界面看。edit 没有 `structuredContent`。

**往哪走**：§4.11 匹配函数。

### 4.11 normalizeForFuzzyMatch() 和 fuzzyFindText()

**调用位置**：`applyEditsToNormalizedContent` 对每个 `Edit` 调用 `fuzzyFindText`（§4.12）。

源码：[`packages/coding-agent/src/core/tools/edit-diff.ts:11-55`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit-diff.ts#L11-L55)

```ts
export function detectLineEnding(content: string): "\r\n" | "\n" {
	const crlfIdx = content.indexOf("\r\n");
	const lfIdx = content.indexOf("\n");
	if (lfIdx === -1) return "\n";
	if (crlfIdx === -1) return "\n";
	return crlfIdx < lfIdx ? "\r\n" : "\n";
}

export function normalizeToLF(text: string): string {
	return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

export function restoreLineEndings(text: string, ending: "\r\n" | "\n"): string {
	return ending === "\r\n" ? text.replace(/\n/g, "\r\n") : text;
}

/**
 * Normalize text for fuzzy matching. Applies progressive transformations:
 * - Strip trailing whitespace from each line
 * - Normalize smart quotes to ASCII equivalents
 * - Normalize Unicode dashes/hyphens to ASCII hyphen
 * - Normalize special Unicode spaces to regular space
 */
export function normalizeForFuzzyMatch(text: string): string {
	return (
		text
			.normalize("NFKC")
			// Strip trailing whitespace per line
			.split("\n")
			.map((line) => line.trimEnd())
			.join("\n")
			// Smart single quotes → '
			.replace(/[\u2018\u2019\u201A\u201B]/g, "'")
			// Smart double quotes → "
			.replace(/[\u201C\u201D\u201E\u201F]/g, '"')
			// Various dashes/hyphens → -
			// U+2010 hyphen, U+2011 non-breaking hyphen, U+2012 figure dash,
			// U+2013 en-dash, U+2014 em-dash, U+2015 horizontal bar, U+2212 minus
			.replace(/[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]/g, "-")
			// Special spaces → regular space
			// U+00A0 NBSP, U+2002-U+200A various spaces, U+202F narrow NBSP,
			// U+205F medium math space, U+3000 ideographic space
			.replace(/[\u00A0\u2002-\u200A\u202F\u205F\u3000]/g, " ")
	);
}
```

**讲解（规范化）**

三个换行函数很直接。`normalizeToLF` 先换 `\r\n` 再换单独的 `\r`，老式 Mac 的换行也统一了。

`normalizeForFuzzyMatch` 处理的是模型和文件"看起来一样、字节不同"的几种情况：

- `NFKC` 规范化：全角字母数字变半角、兼容字符合并等。
- 去掉每行末尾的空白。模型复述代码时经常丢掉行尾空格。
- 弯引号 `‘’‚‛` `“”„‟` 换成直引号。
- 各种连字符、破折号、减号换成 `-`。
- 不换行空格、各种宽度的空格、全角空格换成普通空格。

源码：[`packages/coding-agent/src/core/tools/edit-diff.ts:201-251`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit-diff.ts#L201-L251)

```ts
/**
 * Find oldText in content, trying exact match first, then fuzzy match.
 * When fuzzy matching is used, the returned contentForReplacement is the
 * fuzzy-normalized version of the content (trailing whitespace stripped,
 * Unicode quotes/dashes normalized to ASCII).
 */
export function fuzzyFindText(content: string, oldText: string): FuzzyMatchResult {
	// Try exact match first
	const exactIndex = content.indexOf(oldText);
	if (exactIndex !== -1) {
		return {
			found: true,
			index: exactIndex,
			matchLength: oldText.length,
			usedFuzzyMatch: false,
			contentForReplacement: content,
		};
	}

	// Try fuzzy match - work entirely in normalized space
	const fuzzyContent = normalizeForFuzzyMatch(content);
	const fuzzyOldText = normalizeForFuzzyMatch(oldText);
	const fuzzyIndex = fuzzyContent.indexOf(fuzzyOldText);

	if (fuzzyIndex === -1) {
		return {
			found: false,
			index: -1,
			matchLength: 0,
			usedFuzzyMatch: false,
			contentForReplacement: content,
		};
	}

	// When fuzzy matching, return offsets in normalized space. Callers can use
	// the normalized content to compute replacements, then decide how much of
	// that normalized output should be written back.
	return {
		found: true,
		index: fuzzyIndex,
		matchLength: fuzzyOldText.length,
		usedFuzzyMatch: true,
		contentForReplacement: fuzzyContent,
	};
}

function countOccurrences(content: string, oldText: string): number {
	const fuzzyContent = normalizeForFuzzyMatch(content);
	const fuzzyOldText = normalizeForFuzzyMatch(oldText);
	return fuzzyContent.split(fuzzyOldText).length - 1;
}
```

**讲解（查找）**

`fuzzyFindText` 先精确查找，找到就返回原文中的位置。找不到时，把原文和 `oldText` 都规范化，在规范化后的文本里查找，返回的 `index` 是规范化文本里的位置，`contentForReplacement` 也是规范化文本。调用方必须在同一份文本上做替换，否则位置会对不上。

`countOccurrences` 统计出现次数时总是在规范化文本上数。所以即使精确匹配只有一处，只要规范化后有两处相同，也算不唯一。

**往哪走**：§4.12 多处替换。

### 4.12 applyEditsToNormalizedContent()：多处替换

**调用位置**：edit 的 `execute`；TUI 预览 diff 的 `computeEditsDiff`（`packages/coding-agent/src/core/tools/edit-diff.ts:514`，略）。

源码：[`packages/coding-agent/src/core/tools/edit-diff.ts:291-339`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit-diff.ts#L291-L339)

```ts
/**
 * Apply one or more exact-text replacements to LF-normalized content.
 *
 * All edits are matched against the same original content. Replacements are
 * then applied in reverse order so offsets remain stable. If any edit needs
 * fuzzy matching, the operation runs in fuzzy-normalized content space and then
 * overlays those line-level changes onto the original content so unchanged line
 * blocks keep their original bytes.
 */
export function applyEditsToNormalizedContent(
	normalizedContent: string,
	edits: Edit[],
	path: string,
): AppliedEditsResult {
	const normalizedEdits = edits.map((edit) => ({
		oldText: normalizeToLF(edit.oldText),
		newText: normalizeToLF(edit.newText),
	}));

	for (let i = 0; i < normalizedEdits.length; i++) {
		if (normalizedEdits[i].oldText.length === 0) {
			throw getEmptyOldTextError(path, i, normalizedEdits.length);
		}
	}

	const initialMatches = normalizedEdits.map((edit) => fuzzyFindText(normalizedContent, edit.oldText));
	const usedFuzzyMatch = initialMatches.some((match) => match.usedFuzzyMatch);
	const replacementBaseContent = usedFuzzyMatch ? normalizeForFuzzyMatch(normalizedContent) : normalizedContent;

	const matchedEdits: MatchedEdit[] = [];
	for (let i = 0; i < normalizedEdits.length; i++) {
		const edit = normalizedEdits[i];
		const matchResult = fuzzyFindText(replacementBaseContent, edit.oldText);
		if (!matchResult.found) {
			throw getNotFoundError(path, i, normalizedEdits.length);
		}

		const occurrences = countOccurrences(replacementBaseContent, edit.oldText);
		if (occurrences > 1) {
			throw getDuplicateError(path, i, normalizedEdits.length, occurrences);
		}

		matchedEdits.push({
			editIndex: i,
			matchIndex: matchResult.index,
			matchLength: matchResult.matchLength,
			newText: edit.newText,
		});
	}
```

**讲解（匹配和检查）**

1. 每个 `Edit` 的 `oldText`、`newText` 也统一成 LF。模型可能在参数里写 `\r\n`。
2. 空的 `oldText` 直接报错。
3. 先对每个 `Edit` 试一次匹配。**只要有一个需要模糊匹配，整批都在规范化文本上做**（`replacementBaseContent`）。全部的位置都在同一份文本里，才能比较是否重叠。
4. 在这份文本上逐个查找：找不到报错；出现超过一次报错。错误信息在只有一处替换时和多处替换时措辞不同，多处时带上 `edits[i]` 的下标，模型能知道是哪一处出错。

源码：[`packages/coding-agent/src/core/tools/edit-diff.ts:341-362`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit-diff.ts#L341-L362)

```ts
	matchedEdits.sort((a, b) => a.matchIndex - b.matchIndex);
	for (let i = 1; i < matchedEdits.length; i++) {
		const previous = matchedEdits[i - 1];
		const current = matchedEdits[i];
		if (previous.matchIndex + previous.matchLength > current.matchIndex) {
			throw new Error(
				`edits[${previous.editIndex}] and edits[${current.editIndex}] overlap in ${path}. Merge them into one edit or target disjoint regions.`,
			);
		}
	}

	const baseContent = normalizedContent;
	const newContent = usedFuzzyMatch
		? applyReplacementsPreservingUnchangedLines(normalizedContent, replacementBaseContent, matchedEdits)
		: applyReplacements(replacementBaseContent, matchedEdits);

	if (baseContent === newContent) {
		throw getNoChangeError(path, normalizedEdits.length);
	}

	return { baseContent, newContent };
}
```

**讲解（重叠检查和替换）**

5. 按位置排序，相邻两处如果前一处的结尾超过后一处的开头，报重叠错误，建议合并。
6. 替换：
   - 全部精确匹配时，`applyReplacements`（`packages/coding-agent/src/core/tools/edit-diff.ts:111`）**从后往前**逐个替换。后面的替换不影响前面的位置，所以所有位置都可以针对原文计算一次。
   - 用了模糊匹配时，不能把规范化后的全文写回去，那会把文件里其他地方的弯引号、行尾空格也改掉。`applyReplacementsPreservingUnchangedLines`（`packages/coding-agent/src/core/tools/edit-diff.ts:132`）把每处替换扩展到它涉及的整行，只有这些行用规范化文本替换后的结果，其余行从原文逐字节拷回。§7.3 的模糊匹配实验里，第 1 行被替换，第 2 行的弯引号 `“world”` 保留了下来。
7. 替换前后完全相同（例如 `newText` 等于 `oldText`）也报错。注释的推测是：这多半说明特殊字符有问题，模型以为改了其实没改。

所有错误都是抛出的 `Error`，M2 把它变成 `isError: true` 的工具结果。模型读到错误信息后通常会换一个更长、更唯一的 `oldText` 重试。edit 的规则是"全部成功或全部不做"：任何一处出错，文件都不会被写入，因为写回发生在这个函数返回之后。

**往哪走**：§4.13 write。

### 4.13 write：整个覆盖

源码：[`packages/coding-agent/src/core/tools/write.ts:58-90`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/write.ts#L58-L90)

```ts
		async execute(
			_toolCallId,
			{ path, content }: { path: string; content: string },
			signal?: AbortSignal,
			_onUpdate?,
			ctx?: ExtensionContext,
		) {
			const absolutePath = resolveToCwd(path, ctx?.cwd || cwd);
			const dir = dirname(absolutePath);
			return withFileMutationQueue(absolutePath, async () => {
				// Do not reject from an abort event listener here: that would release the
				// mutation queue while an in-flight filesystem operation may still finish.
				// Checking signal.aborted after each await observes the same aborts while
				// keeping the queue locked until the current operation has settled.
				const throwIfAborted = (): void => {
					if (signal?.aborted) throw new Error("Operation aborted");
				};

				throwIfAborted();
				// Create parent directories if needed.
				await ops.mkdir(dir);
				throwIfAborted();

				// Write the file contents.
				await ops.writeFile(absolutePath, content);
				throwIfAborted();

				return {
					content: [{ type: "text", text: `Successfully wrote to ${path}` }],
					details: undefined,
				};
			});
		},
```

**讲解**

write 最简单：解析路径，在队列里 `mkdir -p` 父目录，写入内容。中止的处理方式和 edit 相同。它不保留 BOM 和换行风格，模型给什么就写什么；系统提示词的准则要求"只在新文件或完全重写时用 write"（`packages/coding-agent/src/core/tools/write.ts:18`）。

edit 和 write 用同一个 `withFileMutationQueue`，键是同一个绝对路径（真实路径），所以模型在同一批里对一个文件先 write 再 edit，两次修改会排队执行。

**往哪走**：§4.14 队列本身。

### 4.14 withFileMutationQueue()：按文件排队

**调用位置**：edit 和 write 的 `execute`。扩展也可以 import 它（`packages/coding-agent/src/core/tools/index.ts:20`），让自己的写文件工具加入同一个队列。

源码：[`packages/coding-agent/src/core/tools/file-mutation-queue.ts:4-61`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/file-mutation-queue.ts#L4-L61)

```ts
const fileMutationQueues = new Map<string, Promise<void>>();
let registrationQueue = Promise.resolve();

function isMissingPathError(error: unknown): boolean {
	return (
		typeof error === "object" &&
		error !== null &&
		"code" in error &&
		(error.code === "ENOENT" || error.code === "ENOTDIR")
	);
}

async function getMutationQueueKey(filePath: string): Promise<string> {
	const resolvedPath = resolve(filePath);
	try {
		return await realpath(resolvedPath);
	} catch (error) {
		if (isMissingPathError(error)) {
			return resolvedPath;
		}
		throw error;
	}
}

/**
 * Serialize file mutation operations targeting the same file.
 * Operations for different files still run in parallel.
 */
export async function withFileMutationQueue<T>(filePath: string, fn: () => Promise<T>): Promise<T> {
	const registration = registrationQueue.then(async () => {
		const key = await getMutationQueueKey(filePath);
		const currentQueue = fileMutationQueues.get(key) ?? Promise.resolve();

		let releaseNext!: () => void;
		const nextQueue = new Promise<void>((resolveQueue) => {
			releaseNext = resolveQueue;
		});
		const chainedQueue = currentQueue.then(() => nextQueue);
		fileMutationQueues.set(key, chainedQueue);

		return { key, currentQueue, chainedQueue, releaseNext };
	});
	registrationQueue = registration.then(
		() => undefined,
		() => undefined,
	);

	const { key, currentQueue, chainedQueue, releaseNext } = await registration;
	await currentQueue;
	try {
		return await fn();
	} finally {
		releaseNext();
		if (fileMutationQueues.get(key) === chainedQueue) {
			fileMutationQueues.delete(key);
		}
	}
}
```

**讲解**

为什么需要它：M2 的循环默认并行执行工具（M2 §4.10）。模型在一次回复里发出两个 edit，改同一个文件的两处，两个 `execute` 会同时读到旧内容，各自替换一处后写回，后写的覆盖先写的，丢掉一处修改。§7.3 的实验复现了这种丢失。

实现是每个文件一条 Promise 链：

- `fileMutationQueues` 是"文件 → 最后一个排队者完成时 resolve 的 Promise"。
- 新来的调用创建 `nextQueue`，把链接成 `currentQueue.then(() => nextQueue)` 存回去，然后 `await currentQueue` 等前一个完成，执行 `fn`，最后 `releaseNext()` 放行下一个。
- 队列的键用 `realpath` 求出真实路径：`a.txt` 和指向它的符号链接共用一个队列。文件还不存在（write 新建文件）时用 `resolve` 后的路径。
- 求真实路径是异步的。如果两个调用各自 `await realpath` 再登记，登记顺序可能和调用顺序不同。`registrationQueue` 把"求键 + 登记"这一步本身串行化，保证先调用的先排队。
- 最后一个排队者完成时，如果表里存的还是自己的链，就删掉这个键，表不会无限增长。

不同文件的键不同，互不等待。§7.3 里对 `q1.txt` 的两次 edit 依次执行，对 `q2.txt` 的 edit 和第一次同时执行。

**往哪走**：§4.15 嵌套工具调用。

### 4.15 嵌套工具调用：_executeNestedToolCall() 和 NestedToolCallRunner.execute()

**调用位置**：工具调用 `ctx.executeTool(name, args)` → `createToolContext` 里的闭包（§4.2）→ `runner.executeToolFn(toolCallId, …)` → `AgentSession._executeNestedToolCall`。内置的四个工具不用它；用它的是 codemode 这类"在脚本里调用工具"的工具（→ M10）。本节用 [`lab/m3-nested-ext.ts`](lab/m3-nested-ext.ts) 的 `peek` 工具观察：

```bash
EXT=$PWD/m3-nested-ext.ts WORK=/tmp/pi-lab/m3-nested-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m3-nested-agent ./run.sh --mode json "用 peek 看 README.md" > /tmp/pi-lab/m3-nested.jsonl
node summarize-events.mjs /tmp/pi-lab/m3-nested.jsonl
```

```
…省略…
message_end (assistant)
tool_execution_start peek
tool_execution_start read
tool_execution_end read
tool_execution_start no_such_tool
tool_execution_end no_such_tool
tool_execution_end peek
message_start (toolResult)
message_end (toolResult)
…省略…
```

在 `peek` 的 `tool_execution_start` 和 `tool_execution_end` 之间，多了两对嵌套调用的事件。但 `toolResult` 消息只有一条，是 `peek` 的。

源码：[`packages/coding-agent/src/core/agent-session.ts:742-780`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L742-L780)

```ts
	/**
	 * Run a call that the tool call `parentToolCallId` made through `ctx.executeTool()`. It goes
	 * through the agent's tool pipeline with the session's hooks, against the callable tools.
	 */
	private async _executeNestedToolCall(
		parentToolCallId: string,
		name: string,
		args: unknown,
		options: ExecuteToolOptions,
	): Promise<AgentToolCallOutcome> {
		this._nestedToolCalls ??= new NestedToolCallRunner({
			getTools: () => this._getCallableTools(),
			isSequential: () => this.agent.toolExecution === "sequential",
			runToolCall: (toolCall, parentId, signal, onUpdate) => {
				const assistantMessage = this._findLastAssistantMessage();
				if (!assistantMessage) {
					return Promise.resolve({
						toolCall,
						result: { content: [{ type: "text", text: "No assistant message issued this call" }], details: {} },
						isError: true,
					});
				}
				return runToolCall(toolCall, {
					tools: this._getCallableTools(),
					assistantMessage,
					context: { messages: this.agent.state.messages, tools: this.agent.state.tools },
					beforeToolCall: (context) => this._beforeToolCall(context, parentId),
					afterToolCall: (context) => this._afterToolCall(context, parentId),
					signal,
					onUpdate,
				});
			},
			emit: async (event) => {
				await this._extensionRunner.emit(event);
				this._emit(event);
			},
		});
		return this._nestedToolCalls.execute(parentToolCallId, name, args, options);
	}
```

**讲解（AgentSession 一侧）**

`NestedToolCallRunner` 在第一次嵌套调用时才创建（`??=`）。AgentSession 给它提供四个回调（`NestedToolCallHost`，`packages/coding-agent/src/core/nested-tool-calls.ts:131`）：

- `getTools`：可调用的工具，用 `_getCallableTools()`（`packages/coding-agent/src/core/agent-session.ts:1598`）。它包括当前激活的 direct 工具，以及 deferred、codemode 暴露方式的工具。也就是说，嵌套调用可以用到模型不能直接调用的工具。
- `isSequential`：`Agent` 的 `toolExecution` 是不是 `"sequential"`。
- `runToolCall`：调用 M2 导出的 `runToolCall`（`packages/agent/src/agent-loop.ts:821`）。`assistantMessage` 取 transcript 里最后一条 assistant 消息，就是发起父调用的那条。`beforeToolCall`、`afterToolCall` 用的是和模型发起的调用相同的 `_beforeToolCall`、`_afterToolCall`，只是多传了 `parentId`。
- `emit`：先发给扩展，再发给 AgentSession 的订阅者。

源码：[`packages/coding-agent/src/core/nested-tool-calls.ts:172-218`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/nested-tool-calls.ts#L172-L218)

```ts
	/**
	 * Run `name` on behalf of the call `callerId`. The nested call gets the id `<callerId>/<n>`.
	 * Never rejects for tool failures: they come back as `isError: true`.
	 */
	async execute(
		callerId: string,
		name: string,
		args: unknown,
		options: NestedToolCallOptions = {},
	): Promise<AgentToolCallOutcome> {
		let scope = this.scopes.get(callerId);
		if (!scope) {
			scope = { recorder: new NestedCallRecorder(), nextId: 1, holdsQueue: false };
			this.scopes.set(callerId, scope);
		}
		const toolCall: AgentToolCall = {
			type: "toolCall",
			id: `${callerId}/${scope.nextId++}`,
			name,
			arguments: (args ?? {}) as AgentToolCall["arguments"],
		};
		const record = scope.recorder.start(toolCall);
		await this.host.emit({
			type: "tool_execution_start",
			toolCallId: toolCall.id,
			toolName: name,
			args: toolCall.arguments,
			parentToolCallId: callerId,
		});

		const exclusive =
			!scope.holdsQueue &&
			(this.host.isSequential() ||
				this.host.getTools().find((tool) => tool.name === name)?.executionMode === "sequential");
		let release: (() => void) | undefined;
		if (exclusive) {
			const previous = this.queueTail;
			this.queueTail = new Promise((resolve) => {
				release = resolve;
			});
			await previous;
		}
		this.scopes.set(toolCall.id, {
			recorder: scope.recorder,
			nextId: 1,
			holdsQueue: scope.holdsQueue || exclusive,
		});
```

**讲解（执行器 ①：编号、事件、互斥）**

- **作用域。** 每个"调用方 id"一个 `CallScope`，里面有记录器 `recorder` 和下一个序号 `nextId`。嵌套调用的 id 是 `<调用方 id>/<序号>`，所以 `peek` 的两次调用是 `call_1/1` 和 `call_1/2`。
- **事件。** 先发出带 `parentToolCallId` 的 `tool_execution_start`。这些事件不是 M2 的循环发的，循环不知道嵌套调用的存在（文件开头的注释，`packages/coding-agent/src/core/nested-tool-calls.ts:1`）。
- **互斥。** 当 `Agent` 设为串行执行，或者被调用的工具声明了 `executionMode: "sequential"` 时，这次调用要独占：在 `queueTail` 链上排队，等前一个独占调用结束。`holdsQueue` 防止死锁：已经持有队列的调用再发起嵌套调用时，不再排队等自己。
- **子作用域。** 用嵌套调用自己的 id 登记一个新作用域，`recorder` 共用父作用域的。嵌套工具再发起嵌套调用，id 就是 `call_1/1/1`，记录仍然汇总到最外层模型发起的那次调用上。

源码：[`packages/coding-agent/src/core/nested-tool-calls.ts:219-250`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/nested-tool-calls.ts#L219-L250)

```ts
		let outcome: AgentToolCallOutcome;
		try {
			outcome = await this.host.runToolCall(toolCall, callerId, options.signal, async (partialResult) => {
				options.onUpdate?.(partialResult);
				await this.host.emit({
					type: "tool_execution_update",
					toolCallId: toolCall.id,
					toolName: name,
					args: toolCall.arguments,
					partialResult,
					parentToolCallId: callerId,
				});
			});
		} finally {
			this.scopes.delete(toolCall.id);
			release?.();
		}

		scope.recorder.finish(record, outcome.isError, textOf(outcome.result));
		// Nested results are not persisted, so their usage is only counted through the recorder.
		if (outcome.result.usage) scope.recorder.addUsage(outcome.result.usage);
		await this.host.emit({
			type: "tool_execution_end",
			toolCallId: toolCall.id,
			toolName: name,
			result: outcome.result,
			isError: outcome.isError,
			...(outcome.durationMs === undefined ? {} : { durationMs: outcome.durationMs }),
			parentToolCallId: callerId,
		});
		return outcome;
	}
```

**讲解（执行器 ②：执行和记录）**

- `host.runToolCall` 执行。进度更新既转给调用方传入的 `onUpdate`，也发成带 `parentToolCallId` 的 `tool_execution_update` 事件。
- `finally` 删除子作用域，释放互斥队列。
- 记录器记下结果的状态和错误文本；结果里有 `usage`（例如嵌套工具自己调用了模型）就累加。注释说明：嵌套调用的结果不持久化，它们的用量只能通过记录器计入。
- 发出 `tool_execution_end`，把 `AgentToolCallOutcome` 原样返回给调用方。

真实的事件如下。注意 `no_such_tool` 的 `tool_execution_end` 没有 `durationMs`，因为工具不存在，根本没有执行（`AgentToolCallOutcome.durationMs` 的注释：工具没运行时缺省）：

```bash
grep -E '^\{"type":"tool_execution_(start|end)"' /tmp/pi-lab/m3-nested.jsonl
```

```
{"type":"tool_execution_start","toolCallId":"call_1","toolName":"peek","args":{"path":"README.md"}}
{"type":"tool_execution_start","toolCallId":"call_1/1","toolName":"read","args":{"path":"README.md"},"parentToolCallId":"call_1"}
{"type":"tool_execution_end","toolCallId":"call_1/1","toolName":"read","result":{"content":[{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"}],"structuredContent":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n"},"isError":false,"durationMs":1,"parentToolCallId":"call_1"}
{"type":"tool_execution_start","toolCallId":"call_1/2","toolName":"no_such_tool","args":{},"parentToolCallId":"call_1"}
{"type":"tool_execution_end","toolCallId":"call_1/2","toolName":"no_such_tool","result":{"content":[{"type":"text","text":"Tool no_such_tool not found"}],"details":{}},"isError":true,"parentToolCallId":"call_1"}
{"type":"tool_execution_end","toolCallId":"call_1","toolName":"peek","result":{"content":[{"type":"text","text":"read: id=call_1/1 isError=false # Demo\nno_such_tool: id=call_1/2 isError=true Tool no_such_tool not found"}],"details":{}},"isError":false,"durationMs":1}
```

**往哪走**：§4.16 `runToolCall`。

### 4.16 runToolCall()：同一条流水线

**调用位置**：`NestedToolCallHost.runToolCall` → `runToolCall(toolCall, { tools, assistantMessage, context, beforeToolCall, afterToolCall, signal, onUpdate })`。

`runToolCall` 本身在 M2 §4.19 读过（`packages/agent/src/agent-loop.ts:821-829`）：依次调用 `prepareToolCall`、`executePreparedToolCall`、`finalizeExecutedToolCall`，不发事件，不加消息。这里关心的是它带来的效果：

1. **工具查找。** `prepareToolCall` 在 `options.tools` 里找工具，找不到返回 `Tool no_such_tool not found`（`packages/agent/src/agent-loop.ts:730`）。上面的事件里能看到这句话。
2. **参数处理。** 同样经过 `prepareArguments` 和 schema 校验。
3. **扩展的 `tool_call` 事件。** `beforeToolCall` 是 `_beforeToolCall(context, parentId)`：

源码：[`packages/coding-agent/src/core/agent-session.ts:673-697`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L673-L697)

```ts
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

扩展收到的 `tool_call` 事件多了 `parentToolCallId` 字段（`packages/coding-agent/src/core/extensions/types.ts:1222`），可以据此区分"模型直接发起的调用"和"别的工具发起的调用"。权限扩展（例如 M7 的 permission-gate）对嵌套调用同样生效。

`lab/m3-nested-ext.ts` 在设置 `M3_BLOCK_NESTED=1` 时注册一个 `tool_call` 处理器，拦截 `parentToolCallId` 不为空的 read 调用：

```bash
M3_BLOCK_NESTED=1 EXT=$PWD/m3-nested-ext.ts WORK=/tmp/pi-lab/m3-nested-work-2 PI_CODING_AGENT_DIR=/tmp/pi-lab/m3-nested-agent-2 ./run.sh --mode json "用 peek 看 README.md" > /tmp/pi-lab/m3-nested-2.jsonl
grep -E '^\{"type":"tool_execution_end"' /tmp/pi-lab/m3-nested-2.jsonl
```

```
{"type":"tool_execution_end","toolCallId":"call_1/1","toolName":"read","result":{"content":[{"type":"text","text":"nested read blocked (parent call_1)"}],"details":{}},"isError":true,"parentToolCallId":"call_1"}
{"type":"tool_execution_end","toolCallId":"call_1/2","toolName":"no_such_tool","result":{"content":[{"type":"text","text":"Tool no_such_tool not found"}],"details":{}},"isError":true,"parentToolCallId":"call_1"}
{"type":"tool_execution_end","toolCallId":"call_1","toolName":"peek","result":{"content":[{"type":"text","text":"read: id=call_1/1 isError=true nested read blocked (parent call_1)\nno_such_tool: id=call_1/2 isError=true Tool no_such_tool not found"}],"details":{}},"isError":false,"durationMs":0}
```

嵌套的 read 被拦截，得到 `isError: true` 和拦截理由，read 没有执行（没有 `durationMs`）。`peek` 自己没有失败：`executeTool` 不 reject，把错误结果交给 `peek`，由它决定怎么处理。

4. **`afterToolCall`。** 扩展的 `tool_result` 事件和图片规范化同样作用于嵌套调用的结果。

**往哪走**：§4.17 记录怎么挂到 toolResult 消息上。

### 4.17 记录挂到父调用的 toolResult 消息上

**调用位置**：M2 的循环为 `peek` 创建 toolResult 消息并发出 `message_start`（M2 §4.14）→ `Agent.processEvents` → `AgentSession._handleAgentEvent`。

源码：[`packages/coding-agent/src/core/agent-session.ts:1119-1135`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1119-L1135)

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
```

源码：[`packages/coding-agent/src/core/nested-tool-calls.ts:252-258`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/nested-tool-calls.ts#L252-L258)

```ts
	/** Remove and return the record of the nested calls a model-issued call made. */
	takeRecord(toolCallId: string): NestedCallSummary | undefined {
		const scope = this.scopes.get(toolCallId);
		this.scopes.delete(toolCallId);
		if (!scope) return undefined;
		return { calls: scope.recorder.snapshot(), usage: scope.recorder.totalUsage };
	}
```

**讲解**

AgentSession 在 `message_start` 时拦下 toolResult 消息，用它的 `toolCallId` 取出这次调用的记录，直接改写消息对象：加上 `nestedCalls`，把嵌套调用的用量累加进 `usage`。`message_start` 先于 `message_end`，而会话文件在 `message_end` 时写入（→ M8、M4），所以写进会话文件的消息已经带着记录。`agent_end` 时清空所有作用域。

记录器的上限在 `NESTED_CALL_LIMITS`（`packages/coding-agent/src/core/nested-tool-calls.ts:26`）：最多 256 次调用，每次参数最多 8KB，参数总共最多 32KB，错误文本截到 500 字符。超出时 `complete` 为 `false`。codemode 脚本可能在一次工具调用里循环调用几百次别的工具，有了上限，toolResult 消息不会因此变得很大。

会话文件里确实保存了 `nestedCalls`：

```bash
grep -c '"nestedCalls"' "$(ls -t /tmp/pi-lab/m3-nested-agent/sessions/*/*.jsonl | head -1)"
```

```
1
```

只有一行匹配，就是 `peek` 的 toolResult 条目。两次嵌套调用本身在会话文件里没有自己的条目。

**往哪走**：读完了。M3 里没有更下层的调用，工具的结果回到 M2 的 `finalizeExecutedToolCall`。

---

## 5. 状态机

### 5.1 bash 命令的进程

```
                    ops.exec()  bash.ts:99
                        │ timeout 非法 → 抛错 (100 → resolveTimeoutMs 29-36)
                        │ 已中止 → 抛 "aborted" (101-103)
                        │ cwd 不存在 → 抛错 (105-109)
                        ▼
              spawn(shell, [...args, command], detached)  (112-118)
              trackDetachedChildPid (123)
                        │
                        ▼
              ┌──────────────────────┐
              │ running              │ stdout/stderr → onData (139-140)
              │ 新进程组，组长=shell  │
              └──┬────────┬────────┬─┘
     定时器到点  │        │ abort   │ shell 自己退出
     timedOut=true (134)  │ (126-128, 143-144)
     killProcessTree (135)│ killProcessTree (127)
                 │        │         │
                 ▼        ▼         ▼
              waitForChildProcess 返回 (148)：exit 后管道空闲 100ms
                 │        │         │
                 │        │         ├─ exitCode 有值 → { exitCode } (158)
                 │        │         └─ 被信号杀死 → 128 + 信号编号 (157-158)
                 │        └─ signal.aborted → 抛 "aborted" (149-151)
                 └─ timedOut → 抛 "timeout:N" (152-154)
                        │
                 finally：untrack，清定时器，移除监听 (159-163)

 bash 工具对结果的处理（bash.ts:361-408）：
   "aborted"   → 抛 Error(输出 + "Command aborted")             (374-376)
   "timeout:N" → 抛 Error(输出 + "Command timed out after N seconds") (377-380)
   exitCode≠0  → 返回 isError:true，输出 + "Command exited with code N" (400-407)
   exitCode=0  → 返回 { content, details, structuredContent }  (408)
```

### 5.2 OutputAccumulator 的输出存放位置

```
 ┌─────────────────────┐  总字节 > 50KB 或 总行数 > 2000
 │ memory              │  shouldUseTempFile (output-accumulator.ts:239-243)
 │ rawChunks 保存原始字节│  append (72-73) / finish (86-88)
 │ tailText 保存尾部     │  或 snapshot({persistIfTruncated}) 发现已截断 (110-112)
 └──────────┬──────────┘
            │ ensureTempFile (245-256)：创建 pi-bash-<hex>.log，写入已有 rawChunks，清空数组
            ▼
 ┌─────────────────────┐
 │ temp-file           │  之后每个 chunk 直接写文件 (74)
 │ tailText 仍只保存尾部 │
 └──────────┬──────────┘
            │ finish() (80-89) → finished = true，之后 append 抛错 (65-67)
            ▼
 ┌─────────────────────┐
 │ finished            │  closeTempFile() (121-142) 关闭写流
 └─────────────────────┘  readFullOutput() (149) 从 rawChunks 或文件读出
```

### 5.3 一次 edit 调用

```
 prepareArguments (edit.ts:101) → schema 校验 (M2)
        │
 validateEditInput：edits 为空 → 抛错 (edit.ts:135-137)
        │
 withFileMutationQueue 等到轮到自己 (file-mutation-queue.ts:52)
        │
 access 失败 → "Could not edit file" (edit.ts:173-180)
        │
 splitBom / detectLineEnding / normalizeToLF (189-191)
        │
 applyEditsToNormalizedContent (edit-diff.ts:300)
   ├─ oldText 为空           → 抛错 (311-313)
   ├─ 有 fuzzy 匹配 → 整批在规范化文本上做 (316-318)
   ├─ 找不到                 → 抛错 (324-326)
   ├─ 出现多次               → 抛错 (328-331)
   ├─ 重叠                   → 抛错 (342-350)
   ├─ 替换后无变化            → 抛错 (357-359)
   └─ 成功 → { baseContent, newContent } (361)
        │
 写回：BOM + restoreLineEndings (edit.ts:195-196)
        │
 返回 "Successfully replaced N block(s)"，details = { diff, patch, firstChangedLine } (199-209)
        │
 finally：releaseNext()，轮到下一个排队者 (file-mutation-queue.ts:55-59)

 任一步抛错：文件没有被写入，M2 把错误变成 isError:true 的结果
```

### 5.4 一个文件的修改队列

```
               withFileMutationQueue(file) 登记 (file-mutation-queue.ts:33-45)
 ┌────────┐    表里没有这个键：currentQueue = resolved  ┌────────────┐
 │ 空闲    │ ─────────────────────────────────────────▶ │ 执行中      │ fn() (54)
 │ 无键    │                                            │            │
 └────────┘ ◀───────────────────────────────────────── └─────┬──────┘
    ▲       releaseNext()，表里还是自己的链 → delete (56-59)    │ 又一个调用登记：
    │                                                          │ chainedQueue = currentQueue.then(nextQueue) (41-42)
    │                                                          ▼
    │                                                   ┌────────────┐
    └─────────────── 最后一个执行者 release ──────────── │ 有人排队    │ await currentQueue (52)
                                                        └────────────┘
```

---

## 6. 设计要点

### 6.1 一种定义，一个包装：内置工具和扩展工具走同一条路

**做法。** 内置工具和扩展工具都写成 `ToolDefinition`，都经过 `wrapRegisteredTool` → `wrapToolDefinition` 变成 `AgentTool`，`ctxFactory` 都是 `runner.createToolContext`。工具的 IO 抽成 `ReadOperations`、`BashOperations`、`EditOperations`、`WriteOperations` 接口，默认实现是本地文件系统和本地 shell。

**理由。** M2 的循环只认 `AgentTool`，coding-agent 需要的提示词片段、渲染、`ctx` 都留在 coding-agent 里，pi-agent-core 不用知道。内置工具没有特权：扩展可以注册一个同名工具替换它，也可以用 `createBashToolDefinition(cwd, { operations })` 复用全部逻辑，只把执行换到 SSH 或容器里。

**代码位置。** `packages/coding-agent/src/core/tools/tool-definition-wrapper.ts:8`，`packages/coding-agent/src/core/extensions/wrapper.ts:17`，`packages/coding-agent/src/core/tools/bash.ts:75`。

### 6.2 截断时告诉模型下一步怎么做

**做法。** 所有工具输出都受 2000 行 / 50KB 限制。截断后在文本末尾追加一句具体的提示：read 给出下一个 `offset`；bash 给出完整输出的临时文件路径；第一行太长时给出一条 `sed | head -c` 命令。`TruncationResult` 放在 `details` 里，不发给模型。不受限的完整结果放在 `structuredContent` 里给程序调用方。

**理由。** 截断保护的是上下文窗口，但截断本身不能让模型失去信息。提示是可以照抄的下一步调用，模型不需要猜。三个字段各有读者：`content` 给模型，`details` 给界面，`structuredContent` 给脚本。

**代码位置。** `packages/coding-agent/src/core/tools/read.ts:179-203`，`packages/coding-agent/src/core/tools/bash.ts:338-356`，`packages/coding-agent/src/core/tools/truncate.ts:11`。

### 6.3 用进程组管理命令的生死

**做法。** bash 用 `detached: true` 让 shell 成为新进程组的组长，超时和中止时对整个组发 SIGKILL；macOS 上先 SIGSTOP。detached 的 pid 记在全局集合里，pi 退出时统一清理。等待进程结束时不等 `close`，而是在 `exit` 后等管道空闲。

**理由。** 模型写的命令经常启动后台进程（`npm run dev &`、`sleep 100 &`）。只杀 shell 会留下孤儿进程；等 `close` 会被继承了管道的后台进程永远挂住。进程组让"这条命令启动的所有东西"有了一个可以一次杀掉的边界。

**代码位置。** `packages/coding-agent/src/core/tools/bash.ts:112-118`，`packages/coding-agent/src/utils/shell.ts:220`，`packages/coding-agent/src/utils/child-process.ts:49`。

### 6.4 edit 针对原文件匹配，全部成功才写入

**做法。** 每个 `oldText` 都在同一份原文里查找，必须唯一、互不重叠；有任何一处失败就抛错，文件不动。匹配先精确后模糊，模糊匹配只改涉及的行，其余行逐字节保留。BOM 和 CRLF 在匹配前去掉，写回时恢复。

**理由。** 依次在上一次替换的结果上匹配，结果会依赖顺序，模型很难预测；针对原文匹配，各处替换互不影响，可以一次提交。唯一性要求让模型给足上下文，避免改错地方。全部成功或全部不做，失败时文件不会处于改了一半的状态，模型可以放心重试。模糊匹配接住了模型最常见的复述误差（行尾空格、弯引号），又不会顺手改掉文件其他地方。

**代码位置。** `packages/coding-agent/src/core/tools/edit-diff.ts:300`，`packages/coding-agent/src/core/tools/edit.ts:189-196`。

### 6.5 嵌套调用走同一条流水线，但不进 transcript

**做法。** `ctx.executeTool()` 通过 M2 导出的 `runToolCall` 执行，经过相同的参数校验、`tool_call`/`tool_result` 扩展事件和图片规范化。事件带 `parentToolCallId`。结果只返回给调用方工具，在父调用的 toolResult 消息上留一份有上限的 `nestedCalls` 记录。

**理由。** 权限检查只写一次，就同时管住模型的直接调用和脚本里的间接调用，扩展不会被绕过。嵌套调用不进 transcript，transcript 仍然严格是"模型的调用 + 对应的结果"，provider 不需要处理模型没发起过的工具调用 id。记录有上限，脚本循环调用几百次也不会撑大会话文件。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:746`，`packages/coding-agent/src/core/nested-tool-calls.ts:176`，`packages/agent/src/agent-loop.ts:821`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行，`WORK` 可以换成任意新目录。临时文件名、pid 和毫秒数每次运行不同。

### 7.1 read 和 bash 的截断

**问题。** 对下面的输入，模型分别看到什么？

- read 一个 3000 行的文件（每行 `line N`，约 29KB）；再带 `offset=2995, limit=3`；再带 `offset=2999`；再带 `offset=5000`。
- read 一个 1000 行、每行 100 字节的文件（约 100KB）。
- read 一个只有一行、60KB 的文件。
- bash 执行 `seq 1 3000`；执行 `echo out; echo err >&2; exit 3`；执行一个每 0.3 秒输出一行、共 3 行的命令，数 `onUpdate` 被调用几次。

**预测。**

- 3000 行文件：行数先到上限，保留第 1–2000 行，提示 `Use offset=2001`。read 自己数行时末尾换行多算一行，提示里写 "of 3001"，而 `details.truncation.totalLines` 是 3000（§4.4）。`offset=2995, limit=3` 返回 2995–2997 行，并提示还剩几行；`offset=2999` 返回最后两行，没有提示；`offset=5000` 抛错。
- 100KB 文件：字节数先到上限。每行 100 字节，加上换行共 101 字节，51200 字节能放下约 506 行（第一行不算换行，只差 1 字节）。提示带 `(50.0KB limit)`。
- 单行 60KB：`firstLineExceedsLimit`，输出只有一句 `sed` 建议。
- `seq 1 3000`：保留最后 2000 行，即 1001–3000；临时文件里有完整的 3000 行。`structuredContent.output` 只有约 14KB，不超过 1MB，`truncated: false`。
- `exit 3`：不抛异常，`isError: true`，stdout 和 stderr 都在 `content` 里，末尾 `Command exited with code 3`。
- 节流 100ms：开始前 1 次空更新，3 行输出相隔 300ms，每行 1 次，结束时如果还有未发出的更新再发 1 次。预计 4 次左右。

**运行。**

```bash
WORK=/tmp/pi-lab/m3-work node --conditions=source --experimental-strip-types m3-tools.ts read
```

```
[3000 行：最后三行] ["line 2000","","[Showing lines 1-2000 of 3001. Use offset=2001 to continue.]"]
[3000 行：details.truncation（content 字段略）] {"content":"…","truncated":true,"truncatedBy":"lines","totalLines":3000,"totalBytes":28893,"outputLines":2000,"outputBytes":18892,"lastLinePartial":false,"firstLineExceedsLimit":false,"maxLines":2000,"maxBytes":51200}
[offset=2995 limit=3] line 2995
line 2996
line 2997

[4 more lines in file. Use offset=2998 to continue.]
[offset=2999] line 2999
line 3000

[offset=5000] 抛出 Error: Offset 5000 is beyond end of file (3001 lines total)
[1000 行 x 100 字节：最后一行] [Showing lines 1-506 of 1001 (50.0KB limit). Use offset=507 to continue.]
[1000 行 x 100 字节：truncatedBy/outputLines/outputBytes] ["bytes",506,51105]
[单行 60KB] [Line 1 is 60.0KB, exceeds 50.0KB limit. Use bash: sed -n '1p' one-line.txt | head -c 51200]
```

```bash
WORK=/tmp/pi-lab/m3-work node --conditions=source --experimental-strip-types m3-tools.ts bash
```

```
[seq 1 3000：content 前两行] ["1001","1002"]
[seq 1 3000：content 最后三行] ["3000","","[Showing lines 1001-3000 of 3000. Full output: /var/folders/6_/q1zysyrx13lc214ph_lvrkj00000gn/T/pi-bash-c3bd975af4c7f04e.log]"]
[seq 1 3000：details.truncation 部分字段] {"truncated":true,"truncatedBy":"lines","totalLines":3000,"outputLines":2000}
[临时文件行数 / 首行 / 末行] [3000,"1","3000"]
[structuredContent] {"output":"<13893 字符>","truncated":false,"exit_code":0,"wall_time_seconds":0}
[exit 3：isError] true
[exit 3：content] out
err


Command exited with code 3
[每 0.3 秒输出一行：onUpdate 次数 / 最终 content] [4,"1\n2\n3\n"]
```

**结论。** 预测都成立。100KB 文件保留了 506 行、51105 字节：前 4 位行号加空格加 95 个 `x` 是 100 字节，再加换行 101 字节，`51200 / 101` 取整是 506（第一行不算换行，多出的 1 字节不够再放一行）。`exit 3` 的 stderr 和 stdout 交错写在同一段输出里，因为两个管道的数据进的是同一个 `onData`。

### 7.2 超时和中止杀掉整个进程组

**问题。** bash 执行 `sleep 30 & echo "bg=$!"; wait`，`timeout: 1`。命令启动了一个后台 `sleep 30` 并打印它的 pid。超时后：

1. 工具多久返回？返回什么？
2. 后台的 `sleep 30` 还活着吗？

再执行 `echo start; sleep 30`，300ms 后中止。最后传 `timeout: 0`。

**预测。**

1. 约 1000ms 后抛出 Error，消息是已有输出 `bg=<pid>` 加 `Command timed out after 1 seconds`（§4.5 ③）。
2. 不活着。`sleep 30` 和 shell 在同一个进程组里，`killProcessTree` 对组发 SIGKILL（§4.7）。脚本用 `process.kill(pid, 0)` 探测：进程不存在时它抛错。
3. 中止：约 300ms 后抛出 Error，消息是 `start` 加 `Command aborted`。
4. `timeout: 0`：`resolveTimeoutMs` 拒绝，抛出 `Invalid timeout`，命令不执行。

**运行。**

```bash
WORK=/tmp/pi-lab/m3-work node --conditions=source --experimental-strip-types m3-tools.ts timeout
```

```
[timeout=1：耗时 ms] 1003
[timeout=1：Error.message] bg=97898


Command timed out after 1 seconds
[后台 sleep（pid 97898）还活着吗] false
[300ms 后 abort：耗时 ms] 301
[300ms 后 abort：Error.message] start


Command aborted
[timeout=0] 抛出 Error: Invalid timeout: must be a finite number of seconds
```

**结论。** 四个预测都成立。可以对照试一下：在 `bash.ts` 里把 `detached: process.platform !== "win32"` 改成 `detached: false`，`killProcessTree` 里的 `process.kill(-pid, …)` 会因为 shell 不是组长而失败，退回只杀 shell，后台的 `sleep 30` 就会活下来。这个修改要改 pi 的源码，本教程没有运行它。

### 7.3 edit 的匹配规则，以及同一文件的并发修改

**问题 1。** 文件 `a.ts` 在两处替换后内容是：

```
const a = 10;
const b = 2;
const c = 30;
console.log(a, b, c);
```

下面几次 edit 分别得到什么结果？

- `oldText: "const"`。
- `oldText: "const d = 4;"`。
- 两处替换，一处是 `"const a = 10;\nconst b"`，一处是 `"const b = 2;"`。
- `oldText` 和 `newText` 都是 `"const b = 2;"`。
- 文件 `quote.ts` 是 `const greet = “hello”;   `（弯引号、行尾 3 个空格）和 `const other = “world”;` 两行，`oldText: 'const greet = "hello";'`（直引号）。
- 文件 `win.txt` 是 BOM + `first\r\nsecond\r\n`，把 `second` 换成 `SECOND`。

**预测。**

- `"const"` 出现 3 次，报 `Found 3 occurrences`。
- 找不到，报 `Could not find the exact text`。
- 两个匹配区间重叠，报 `edits[0] and edits[1] overlap`。
- 替换后内容不变，报 `No changes made`。
- 精确匹配失败，规范化后匹配成功。第 1 行被替换成 `newText`（行尾空格随之消失），第 2 行的弯引号保留（§4.12 第 6 步）。
- 成功，写回的字节以 `ef bb bf` 开头，换行仍是 `0d 0a`。

**运行。**

```bash
WORK=/tmp/pi-lab/m3-work node --conditions=source --experimental-strip-types m3-tools.ts edit
```

```
…省略…
[出现多次] 抛出 Error: Found 3 occurrences of the text in a.ts. The text must be unique. Please provide more context to make it unique.
[找不到] 抛出 Error: Could not find the exact text in a.ts. The old text must match exactly including all whitespace and newlines.
[两处重叠] 抛出 Error: edits[0] and edits[1] overlap in a.ts. Merge them into one edit or target disjoint regions.
[替换成相同内容] 抛出 Error: No changes made to a.ts. The replacement produced identical content. This might indicate an issue with special characters or the text not existing as expected.
[模糊匹配] 成功 "Successfully replaced 1 block(s) in quote.ts."
[模糊匹配后的文件] const greet = "hi";
const other = “world”;

[BOM+CRLF 文件] 成功 "Successfully replaced 1 block(s) in win.txt."
[写回后的字节] ef bb bf 66 69 72 73 74 0d 0a 53 45 43 4f 4e 44 0d 0a
…省略…
```

**问题 2。** `queue` 场景有三步：

1. 两个并发的"读文件 → 等 50ms → 追加一行 → 写回"，分别追加 `A` 和 `B`，不排队。
2. 同样两个操作，各自包在 `withFileMutationQueue(file, …)` 里。
3. 用带延迟的 `EditOperations`（读完等 100ms）创建 edit 工具，同时发起三个 edit：两个改 `q1.txt`，一个改 `q2.txt`。打印每次读写的时刻。

**预测。**

1. 两个操作都读到空文件，后写的覆盖先写的，文件只剩一行，丢了一次更新。
2. 第二个操作等第一个写完才读，文件是 `A\nB\n`。
3. `q1.txt` 的第一个 edit 和 `q2.txt` 的 edit 同时读；`q1.txt` 的第二个 edit 等第一个写完后才读，比前两个晚约 100ms。两处修改都保留。

**运行。**

```bash
WORK=/tmp/pi-lab/m3-work node --conditions=source --experimental-strip-types m3-tools.ts queue
```

```
[不排队，并发追加 A、B 后的文件] "B\n"
[排队，并发追加 A、B 后的文件] "A\nB\n"
 155ms  read  q1.txt
 155ms  read  q2.txt
 258ms  write q1.txt
 259ms  write q2.txt
 259ms  read  q1.txt
 361ms  write q1.txt
[q1.txt] "ONE\nTWO\n"
[q2.txt] "THREE\n"
```

**结论。** 预测都成立。不排队时只剩 `B`（谁后写由调度决定）。edit 工具对 `q1.txt` 的第二次读发生在第一次写之后，两个文件之间没有等待。第一次读在约 150ms 处，是脚本前两步花掉的时间。

---

## 8. 自测与衔接

### 8.1 自测

1. 一个 bash 命令输出了 10 行，每行 10KB。模型看到几行？`details.truncation.truncatedBy` 是什么？临时文件会创建吗？
2. 模型在一次回复里发出两个工具调用：`write` 新建 `b.txt`，`edit` 修改 `b.txt`。默认并行执行模式下，edit 一定能看到 write 写入的内容吗？
3. 扩展 A 注册了一个工具，它在 `execute` 里调用 `ctx.executeTool("bash", { command: "rm -rf build" })`。扩展 B 在 `tool_call` 事件里拦截所有包含 `rm -rf` 的 bash 命令。这次调用会被拦截吗？transcript 里会出现一条 bash 的 toolResult 消息吗？

<details>
<summary>答案</summary>

1. 总共约 100KB，字节先到上限。`truncateTail` 从最后一行往前收集，每行 10241 字节（含换行），51200 字节放得下 4 行（第 5 行加上就超过），模型看到最后 4 行加一句 `[Showing lines 7-10 of 10 (50.0KB limit). Full output: …]`。`truncatedBy` 是 `"bytes"`。总字节数超过 50KB，`shouldUseTempFile` 为真，临时文件会创建（`packages/coding-agent/src/core/tools/output-accumulator.ts:239`）。
2. 一定能。两个调用按 assistant 消息里的顺序依次进入 `withFileMutationQueue`，登记顺序由 `registrationQueue` 保证（`packages/coding-agent/src/core/tools/file-mutation-queue.ts:33`），队列键都是 `b.txt` 的绝对路径。write 先执行，edit 等它完成后才读文件。这依赖 M2 的并行模式按 assistant 消息里的顺序同步启动各个执行闭包（`packages/agent/src/agent-loop.ts:657`），而 edit 和 write 在第一个 `await` 之前就调用了 `withFileMutationQueue`，登记顺序就是调用顺序。
3. 会被拦截。嵌套调用经过 `runToolCall` → `prepareToolCall` → `beforeToolCall`，即 `_beforeToolCall(context, parentId)`，扩展 B 收到带 `parentToolCallId` 的 `tool_call` 事件（`packages/coding-agent/src/core/agent-session.ts:684`）。transcript 里不会有 bash 的 toolResult 消息，只有扩展 A 那个工具的 toolResult，上面的 `nestedCalls` 记录了这次被拦截的 bash 调用（`status: "error"`）。

</details>

### 8.2 衔接

M3 读完了工具结果是怎么产生的。结果回到 M2，被包成 toolResult 消息，在 `message_end` 事件里由 AgentSession 写进会话文件。M4 从这一步开始：`SessionManager.appendMessage()`（`packages/coding-agent/src/core/session-manager.ts:1214`），读会话条目怎么组成一棵树、怎么写进 JSONL，以及反方向怎么从会话文件重建 Agent 消息。

本模块里标了"→ M7"的 `tool_call`/`tool_result` 扩展事件和 `ExtensionContext`，在 M7 从 `ExtensionRunner.emitToolCall()` 读起；标了"→ M8"的工具注册（`_buildRuntime`、`_getCallableTools`），在 M8 读；codemode 和 MCP 工具怎么用嵌套调用，在 M10 读。
