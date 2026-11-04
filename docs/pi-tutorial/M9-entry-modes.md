# M9 入口与运行模式：从 cli.ts 到四种前端

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

M9 是 pi 的最外层。它做三件事：

- 把命令行参数解析成 `Args`，决定运行模式（`AppMode`）：`interactive`、`print`、`json`、`rpc` 四选一。
- 按参数选出会话来源（新建、继续、打开、分叉、只在内存里），建好 `SessionManager`，再通过一个工厂函数建出 `AgentSessionRuntime`。runtime 持有当前的 `AgentSession` 和它依赖的服务。
- 把 runtime 交给一个前端。前端负责从外界拿到用户输入、调用 `session.prompt()`，再把 `AgentSessionEvent` 以自己的格式送出去：print 模式只输出最后的文本，json 模式输出每个事件，rpc 模式在 stdin/stdout 上跑一个 JSONL 协议，TUI 画界面。

本模块要说明的核心事实是：**所有前端共用同一个 `AgentSession`**。前端之间的差别只在三处：`bindExtensions()` 传入的 `mode` 和 UI 上下文、`subscribe()` 之后怎么输出事件、用户输入从哪里来。agent 循环、会话存储、压缩、扩展都在 `AgentSession` 里面（→ M8），前端不碰它们。SDK 用户直接调 `createAgentSession()` 拿到同一个类的实例，等于自己写一个前端。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M9 是最上面和最下面两段：

```
cli.ts → main()                                    [M9] ← 本模块入口
├─ parseArgs() → resolveAppMode()                  [M9]
├─ createSessionManager()                          [M9 → M4]
├─ createAgentSessionRuntime(createRuntime)        [M9]
│   └─ createRuntime 闭包
│       ├─ createAgentSessionServices()            → M8
│       └─ createAgentSessionFromServices()        → M8
└─ 按 appMode 分派
    ├─ runPrintMode()   text / json                [M9]
    │   └─ session.prompt(text)                    → M8 … M2 … M1
    │   └─ 输出最后一条 assistant 消息 / toJsonEvent()
    ├─ runRpcMode()     stdin 命令 → session 方法   [M9]
    └─ InteractiveMode  TUI                        （不在本教程范围）
SDK：createAgentSession() → 自己 subscribe + prompt [M8，示例在 M9]
```

`session.prompt()` 之下的一切（构造 user 消息、跑 agent 循环、写会话文件）都属于 M8 及更下层。M9 只在循环开始前把东西准备好，在事件出来后把它们送出去。

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/coding-agent/src/cli.ts` | 6 | 可执行入口：`setupCli()` + `main(argv)` | 精读，§4.1 |
| `packages/coding-agent/src/cli/setup.ts` | 13 | 设置进程标题和几个环境变量 | 精读，§4.1 |
| `packages/coding-agent/src/main.ts` | 1010 | `main()`、`resolveAppMode`、`createSessionManager`、`buildSessionOptions`、`createRuntime` 闭包 | 精读 574–1010 行和 112–127、358–449 行；auth 子命令（133–209）、会话路径解析的细节略读 |
| `packages/coding-agent/src/cli/args.ts` | 480 | `Args` 类型、`parseArgs`、`printHelp` | 只读类型和几个关键参数的解析 |
| `packages/coding-agent/src/cli/initial-message.ts` | 43 | 把 stdin、`@file` 和第一条消息拼成首条提示词 | 精读，§4.8 |
| `packages/coding-agent/src/core/agent-session-runtime.ts` | 447 | `AgentSessionRuntime`：持有当前 session，负责 new/resume/fork/import 时整体替换 | 精读 1–260、404–438 行；`fork`、`importFromJsonl` 略读 |
| `packages/coding-agent/src/core/output-guard.ts` | 108 | 接管 stdout：非交互模式下 `console.log` 改写到 stderr | 精读，§4.4 |
| `packages/coding-agent/src/core/timings.ts` | 50 | `PI_TIMING=1` 的计时 | 精读，§4.15 |
| `packages/coding-agent/src/modes/print-mode.ts` | 173 | print 前端（text 和 json 两种输出） | 精读，§4.10 |
| `packages/coding-agent/src/modes/json-event.ts` | 61 | `toJsonEvent`：去掉流式事件里的累积快照 | 精读，§4.11 |
| `packages/coding-agent/src/modes/rpc/rpc-mode.ts` | 819 | rpc 前端：命令分派、扩展 UI 请求、关闭流程 | 精读入口、`rebindSession`、`prompt` 命令和输入处理；其余命令按表格略读 |
| `packages/coding-agent/src/modes/rpc/rpc-types.ts` | 303 | `RpcCommand`、`RpcResponse`、扩展 UI 请求/响应类型 | 只读类型，§3.5 |
| `packages/coding-agent/src/modes/rpc/jsonl.ts` | 58 | 只按 `\n` 分行的 JSONL 读写 | 精读，§4.13 |
| `packages/coding-agent/src/modes/rpc/rpc-client.ts` | 617 | 一个启动子进程、包装 RPC 命令的客户端类 | 略，§4.12 末尾提到 |
| `packages/coding-agent/examples/sdk/*.ts` | 14 个文件 | SDK 示例 | 读 `01-minimal.ts` 和 `13-session-runtime.ts`，§4.14 |
| `packages/coding-agent/src/modes/interactive/` | — | TUI 前端，`interactive-mode.ts` 一个文件就有 7211 行 | 不在范围；只引用它调用 `bindExtensions`、`subscribe` 的几行 |

行数用 `wc -l` 统计。pi 仓库里另有用户文档 `packages/coding-agent/docs/rpc.md`、`json.md`、`sdk.md`，可以和本模块对照着读。

### 1.4 读完能回答的问题

1. `pi -p "…"`、`pi --mode json "…"`、`echo … | pi`、`pi --mode rpc` 分别走哪个前端？判断写在哪几行？
2. `--no-session`、`-c`、`--session`、`--fork` 怎么变成一个 `SessionManager`？
3. 为什么 json 模式的 `message_update` 里没有累积的 `message`？
4. RPC 模式下，`prompt` 命令的响应在什么时候发出？响应和输入的顺序一定一致吗？
5. 用 `/new` 换一个会话时，前端手里的 `session` 引用会失效吗？前端怎么知道要重新订阅？

---

## 2. 先跑一次

### 2.1 print 模式和 PI_TIMING

`PI_TIMING=1` 让 `main()` 把启动阶段的耗时打印到 stderr：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m9-work-1 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-1 PI_TIMING=1 ./run.sh
```

```
Running without API keys...
--- Startup Timings: main ---
  parseArgs: 1ms
  runMigrations: 1ms
  createSessionManager: 1ms
  createRuntime: 0ms
  createAgentSessionRuntime: 33ms
  readPipedStdin: 0ms
  prepareInitialMessage: 0ms
  initTheme: 1ms
  resolveModelScope: 0ms
  createAgentSession: 0ms
  TOTAL: 37ms
-----------------------------
--- Startup Timings: extensions ---
  <lab>/faux-ext.ts module import: 12ms
  <lab>/faux-ext.ts factory: 0ms
  builtin:llama.cpp factory: 1ms
  builtin:codemode factory: 0ms
  builtin:tool-search factory: 0ms
  builtin:mcp factory: 0ms
  TOTAL: 13ms
-----------------------------------
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

（输出里 `faux-ext.ts` 的绝对路径换成了 `<lab>`，其余原样。）

可以看到：

- `main` 组的每一行是"从上一个打点到这个打点"的耗时，标签就是 `main.ts` 里 `time("…")` 的参数（§4.15）。耗时几乎都在 `createAgentSessionRuntime`：这一步建 `ModelRuntime`、加载扩展、创建 `AgentSession`。
- `extensions` 组是加载扩展时的打点。除了 `-e` 传入的 `faux-ext.ts`，还有 4 个内置扩展：`llama.cpp`、`codemode`、`tool-search`、`mcp`（`packages/coding-agent/src/extensions/index.ts:7`）。
- 计时打印在模型回复之前，因为 `printTimings()` 在把 runtime 交给前端之前调用（§4.9）。
- `Running without API keys...` 是 `pi-test.sh` 处理 `--no-env` 时 echo 的，不是 pi 的输出。

### 2.2 json 模式

```bash
WORK=/tmp/pi-lab/m9-work-5 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-5 ./run.sh --mode json "读 README.md，总结成三句话" > /tmp/pi-lab/m9-events-5.jsonl
head -c 400 /tmp/pi-lab/m9-events-5.jsonl; echo
grep -m1 'toolcall_start' /tmp/pi-lab/m9-events-5.jsonl
grep -m1 'text_delta' /tmp/pi-lab/m9-events-5.jsonl
```

```
Running without API keys...
{"type":"session","version":3,"id":"01a195e2-c0df-77e1-8cfa-e059e26f5d6f","timestamp":"2026-11-01T06:53:48.128Z","cwd":"/private/tmp/pi-lab/m9-work-5"}
{"type":"agent_start"}
{"type":"turn_start"}
{"type":"message_start","message":{"role":"system","content":"","sections":{"preamble":"You are a coding assistant running inside pi, a coding agent harness. Help users inspec
{"type":"message_update","usage":{"input":0,"output":10,"cacheRead":0,"cacheWrite":1311,"totalTokens":1321,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"assistantMessageEvent":{"type":"toolcall_start","contentIndex":1,"id":"call_1","toolName":"read"}}
{"type":"message_update","usage":{"input":0,"output":10,"cacheRead":0,"cacheWrite":1311,"totalTokens":1321,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"我先读一下 README。"}}
```

文件的第一行 `Running without API keys...` 是 `pi-test.sh` echo 到 stdout 的，不属于 pi 的输出；`summarize-events.mjs` 只处理以 `{` 开头的行，会跳过它。接下来是会话文件头（`SessionHeader`，→ M4），之后每行一个 `AgentSessionEvent`。`message_update` 行没有 `message` 字段，`assistantMessageEvent` 里也没有 `partial`，只有 `usage` 和本次增量；`toolcall_start` 多了 `id` 和 `toolName`。§4.11 读 `toJsonEvent` 时解释原因。完整的事件序列见 README §5.2。

### 2.3 rpc 模式

[`lab/m9-rpc.mjs`](lab/m9-rpc.mjs) 用 `run.sh --mode rpc` 启动 pi，往 stdin 写命令，把 stdout 上的每一行压缩打印出来。`→` 是发给 pi 的行，`←` 是 pi 写出的行：

```bash
WORK=/tmp/pi-lab/m9-work-4 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-4 node m9-rpc.mjs
```

```
    0 → this is not json
    2 → {"id":"s1","type":"get_state"}
    2 → {"id":"p1","type":"prompt","message":"读 README.md，总结成三句话"}
      （非 JSON 行，来自 pi-test.sh）Running without API keys...
  438 ← response id=- command=parse success=false error="Failed to parse command: Unexpected token 'h', \"this is not json\" is not valid JSON"
  439 ← response id=s1 command=get_state success=true {"model":"faux/faux-1","isStreaming":false,"messageCount":0,"pendingMessageCount":0,"sessionFile":"…_01a195e1-0c39-7340-9692-8c75f35f29de.jsonl"}
  439 ← response id=p1 command=prompt success=true {"disposition":"started"}
      ← agent_start
      ← turn_start
      ← message_start (system)
      ← message_end (system)
      ← message_start (user)
      ← message_end (user)
      ← message_start (assistant)
      ← message_update [text_start]
      ← message_update [text_delta]
      ← message_update [text_end]
      ← message_update [toolcall_start]
      ← message_update [toolcall_delta]
      ← message_update [toolcall_end]
      ← message_end (assistant)
      ← tool_execution_start read
      ← tool_execution_end read
      ← message_start (toolResult)
      ← message_end (toolResult)
      ← turn_end (assistant)
      ← turn_start
      ← message_start (assistant)
      ← message_update [text_start]
      ← message_update [text_delta] x3
      ← message_update [text_end]
      ← message_end (assistant)
      ← turn_end (assistant)
      ← agent_end
  446 ← agent_settled
  446 → {"id":"s2","type":"get_state"}
  446 → {"id":"t1","type":"get_last_assistant_text"}
  446 → {"id":"x1","type":"no_such_command"}
  446 ← response id=s2 command=get_state success=true {"model":"faux/faux-1","isStreaming":false,"messageCount":5,"pendingMessageCount":0,"sessionFile":"…_01a195e1-0c39-7340-9692-8c75f35f29de.jsonl"}
  446 ← response id=t1 command=get_last_assistant_text success=true {"text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}
  446 ← response id=x1 command=no_such_command success=false error="Unknown command: no_such_command"
  658 pi 退出，exit code=0
```

左边是毫秒时间戳（只给响应和 `agent_settled` 标了时间）。可以看到：

- 事件序列和 json 模式完全一样，因为两者都订阅同一个 `AgentSession`，都经过 `toJsonEvent`。
- `prompt` 命令的响应 `{"disposition":"started"}` 在 `agent_start` 之前就发出了，不等运行结束。
- 命令发出时 pi 还在启动（前 438ms 没有任何输出）。命令留在 stdin 管道里，启动完成后才被读取。
- 脚本关闭 stdin 后约 200ms，pi 以退出码 0 结束。
- `delta` 的个数每次运行可能不同（README §5.2），下同。

### 2.4 SDK：不经过 CLI

[`lab/m9-sdk.ts`](lab/m9-sdk.ts) 用 SDK 的 `createAgentSessionServices` + `createAgentSessionFromServices` 建一个 `AgentSession`（内存会话，假模型），挂两个订阅者：A 像 `examples/sdk/01-minimal.ts` 一样把 `text_delta` 写到 stdout，B 统计每种事件的次数。

```bash
export PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-sdk-1 WORK=/tmp/pi-lab/m9-work-sdk-1
R="$(node -p 'require("node:url").pathToFileURL(process.argv[1]).href' "$PI/packages/coding-agent/src/experimental/source-resolver.ts")"
node --import "$R" m9-sdk.ts sdk
```

```
agentDir = /tmp/pi-lab/m9-agent-sdk-1
model = faux/faux-1，sessionFile = undefined
我先读一下 README。
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
[订阅者 B] 事件计数： {"agent_start":1,"turn_start":2,"message_start":5,"message_end":5,"message_update":13,"tool_execution_start":1,"tool_execution_end":1,"turn_end":2,"agent_end":1,"agent_settled":1}
[state] messages role： system, user, assistant, toolResult, assistant
```

`--import "$R"` 加载的是 `pi-test.sh` 用的同一个源码解析器（`pi-test.sh:84-85`），它让 `@earendil-works/pi-coding-agent` 解析到 `packages/coding-agent/src/index.ts`。SDK 拿到的 `session` 和 CLI 里的是同一个类；事件种类和次数与 §2.3 的事件流一致（`message_update` 的个数随片段数变化）。

---

## 3. 数据结构

M9 自己定义的类型不多，大部分是"把参数和 session 连起来"的胶水类型。

### 3.1 Args、Mode 和 AppMode：参数和运行模式

`parseArgs` 的输出是 `Args`。下面是完整定义，表格只解释和本模块有关的字段：

源码：[`packages/coding-agent/src/cli/args.ts:11-59`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/cli/args.ts#L11-L59)

```ts
export type Mode = "text" | "json" | "rpc";

export interface Args {
	provider?: string;
	model?: string;
	apiKey?: string;
	systemPrompt?: string;
	appendSystemPrompt?: string[];
	thinking?: ThinkingLevel;
	continue?: boolean;
	resume?: boolean;
	help?: boolean;
	version?: boolean;
	mode?: Mode;
	name?: string;
	noSession?: boolean;
	session?: string;
	sessionId?: string;
	fork?: string;
	sessionDir?: string;
	models?: string[];
	tools?: string[];
	excludeTools?: string[];
	noTools?: boolean;
	noBuiltinTools?: boolean;
	extensions?: string[];
	noExtensions?: boolean;
	noMcp?: boolean;
	print?: boolean;
	export?: string;
	noSkills?: boolean;
	skills?: string[];
	promptTemplates?: string[];
	noPromptTemplates?: boolean;
	themes?: string[];
	useTheme?: string;
	noThemes?: boolean;
	noContextFiles?: boolean;
	listModels?: string | true;
	offline?: boolean;
	tuiMode?: TuiMode;
	verbose?: boolean;
	projectTrustOverride?: boolean;
	messages: string[];
	fileArgs: string[];
	/** Unknown flags (potentially extension flags) - map of flag name to value */
	unknownFlags: Map<string, boolean | string>;
	diagnostics: Array<{ type: "warning" | "error"; message: string }>;
}
```

运行模式有两个类型。`Mode` 是 `--mode` 参数能取的值；`AppMode` 是 `main()` 最终决定的模式：

源码：[`packages/coding-agent/src/core/project-trust.ts:13`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/project-trust.ts#L13)

```ts
export type AppMode = "interactive" | "print" | "json" | "rpc";
```

`AppMode` 定义在 `project-trust.ts`，因为项目信任的提示方式也取决于模式（交互模式可以弹窗，其余模式不能）。

| 字段 | 说明 |
|---|---|
| `print` | `-p`/`--print`。只影响模式判断，不影响输出格式 |
| `mode` | `--mode text|json|rpc` |
| `continue`、`resume`、`session`、`sessionId`、`fork`、`noSession` | 会话来源，§4.5 按优先级读 |
| `provider`、`model`、`thinking`、`models`、`apiKey` | 模型选择，交给 `buildSessionOptions`（§4.6） |
| `extensions`、`noExtensions`、`noMcp`、`skills`、`noSkills` … | 资源加载选项，原样交给 resource loader（→ M6 / M7） |
| `messages` | 位置参数里的提示词，按顺序 |
| `fileArgs` | `@file` 参数 |
| `unknownFlags` | 不认识的 `--xxx`。它们可能是扩展注册的参数，要等扩展加载后才能判断（`packages/coding-agent/src/core/agent-session-services.ts:82`） |
| `diagnostics` | 解析错误和警告。有 error 时 `main()` 直接退出 |

`AppMode` 和前端的对应关系：

| AppMode | 前端函数 | `bindExtensions` 的 `mode` | stdout 上的内容 |
|---|---|---|---|
| `interactive` | `new InteractiveMode(runtime).run()` | `"tui"` | TUI 画面 |
| `print` | `runPrintMode(runtime, { mode: "text" })` | `"print"` | 最后一条 assistant 消息的文本 |
| `json` | `runPrintMode(runtime, { mode: "json" })` | `"json"` | 会话头 + 每个事件一行 JSON |
| `rpc` | `runRpcMode(runtime)` | `"rpc"` | 事件、命令响应、扩展 UI 请求，每行一个 JSON |

`bindExtensions` 的 `mode` 取值是 `ExtensionMode`（`packages/coding-agent/src/core/extensions/types.ts:343`），扩展可以据此判断有没有界面（→ M7）。

**真实实例。** [`lab/m9-dump.ts`](lab/m9-dump.ts) 的 `args` 场景对几组参数调用 `parseArgs`，只打印非空字段：

```bash
export PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-dump-1 WORK=/tmp/pi-lab/m9-work-dump-1
R="$(node -p 'require("node:url").pathToFileURL(process.argv[1]).href' "$PI/packages/coding-agent/src/experimental/source-resolver.ts")"
node --import "$R" m9-dump.ts args
```

```
parseArgs(["-e","<lab>/faux-ext.ts","--provider","faux","--model","faux-1","-p","读 README.md，总结成三句话"])
  → {"print":true,"provider":"faux","model":"faux-1","extensions":["<lab>/faux-ext.ts"],"messages":["读 README.md，总结成三句话"],"fileArgs":[],"unknownFlags":{},"diagnostics":[]}
parseArgs(["--mode","json","读 README.md，总结成三句话"])
  → {"mode":"json","messages":["读 README.md，总结成三句话"],"fileArgs":[],"unknownFlags":{},"diagnostics":[]}
parseArgs(["--mode","rpc"])
  → {"mode":"rpc","messages":[],"fileArgs":[],"unknownFlags":{},"diagnostics":[]}
parseArgs(["-c","--no-session","第一条","第二条"])
  → {"continue":true,"noSession":true,"messages":["第一条","第二条"],"fileArgs":[],"unknownFlags":{},"diagnostics":[]}
buildInitialMessage → {"initialMessage":"读 README.md，总结成三句话"}，之后 parsed.messages = []
```

第一组是 `run.sh` 传给 `cli.ts` 的参数（`--no-env` 已经被 `pi-test.sh` 拿掉）。`-p` 后面紧跟的文本被当作第一条消息收进 `messages`。第四组同时给了 `-c` 和 `--no-session`，`parseArgs` 不报错；§4.5 会看到 `createSessionManager` 先检查 `noSession`，所以 `-c` 被忽略。

### 3.2 AgentSessionRuntime 和它的工厂

`AgentSessionRuntime` 持有"当前"的 `AgentSession` 和它依赖的服务。它由一个工厂函数创建，并且保存这个工厂，以后 `/new`、`/resume`、`/fork` 时用同一个工厂再建一套：

源码：[`packages/coding-agent/src/core/agent-session-runtime.ts:17-41`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-runtime.ts#L17-L41)

```ts
/**
 * Result returned by runtime creation.
 *
 * The caller gets the created session, its cwd-bound services, and all
 * diagnostics collected during setup.
 */
export interface CreateAgentSessionRuntimeResult extends CreateAgentSessionResult {
	services: AgentSessionServices;
	diagnostics: AgentSessionRuntimeDiagnostic[];
}

/**
 * Creates a full runtime for a target cwd and session manager.
 *
 * The factory closes over process-global fixed inputs, recreates cwd-bound
 * services for the effective cwd, resolves session options against those
 * services, and finally creates the AgentSession.
 */
export type CreateAgentSessionRuntimeFactory = (options: {
	cwd: string;
	agentDir: string;
	sessionManager: SessionManager;
	sessionStartEvent?: SessionStartEvent;
	projectTrustContext?: ProjectTrustContext;
}) => Promise<CreateAgentSessionRuntimeResult>;
```

`CreateAgentSessionResult`（`packages/coding-agent/src/core/sdk.ts:109`）有 `session`、`extensionsResult`、`modelFallbackMessage` 三个字段。工厂的返回值在它上面加了 `services` 和 `diagnostics`。`services` 是和 cwd 绑定的一组服务：

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

| 类型 / 字段 | 说明 |
|---|---|
| `CreateAgentSessionRuntimeFactory` 的参数 | `cwd`、`agentDir`、`sessionManager` 必填；`sessionStartEvent` 在替换会话时给出（`reason` 为 `new`/`resume`/`fork`），首次创建时为 `undefined`；`projectTrustContext` 只在切换到别的项目时用 |
| `AgentSessionServices.modelRuntime` | 模型和认证（→ M1） |
| `AgentSessionServices.settingsManager` | 全局 + 项目设置（→ M6） |
| `AgentSessionServices.resourceLoader` | 扩展、skills、提示词模板、AGENTS.md（→ M6 / M7） |
| `diagnostics` | 收集的警告和错误，由调用方决定是否打印、是否退出 |
| `AgentSessionRuntime.session` | getter，返回当前 session。会话被替换后返回新对象 |

**真实实例。** `m9-dump.ts` 的 `runtime` 场景照 `main()` 的写法建一个 runtime，再调用一次 `newSession()`：

```bash
node --import "$R" m9-dump.ts runtime
```

```
[factory] 第 1 次调用，sessionStartEvent=undefined
AgentSessionRuntime：
  cwd = /tmp/pi-lab/m9-work-dump-1
  diagnostics = []
  modelFallbackMessage = undefined
  session.sessionId = 01a195e4-847b-71cf-ab95-a2a3de8aeb59
  session.model = faux/faux-1
AgentSessionServices：
  keys = cwd, agentDir, modelRuntime, settingsManager, resourceLoader, diagnostics
  agentDir = /tmp/pi-lab/m9-agent-dump-1
  resourceLoader 加载的扩展 = <lab>/faux-ext.ts
[factory] 第 2 次调用，sessionStartEvent={"type":"session_start","reason":"new"}
调用 runtime.newSession() 之后：
  session 是同一个对象吗？ false
  session.sessionId = 01a195e4-849a-71cf-ab95-a2a4f8c42839
```

`newSession()` 又调用了一次工厂，`runtime.session` 换成了一个新对象。所以前端不能长期持有 `session` 引用，§4.7 会看到 runtime 怎么通知前端重新绑定。这里没有加载内置扩展，因为 `main()` 把 `builtInExtensions` 放在 `extensionFactories` 里传给 resource loader，而这个脚本没有传。

### 3.3 PrintModeOptions：print 前端的输入

源码：[`packages/coding-agent/src/modes/print-mode.ts:15-27`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/print-mode.ts#L15-L27)

```ts
/**
 * Options for print mode.
 */
export interface PrintModeOptions {
	/** Output mode: "text" for final response only, "json" for all events */
	mode: "text" | "json";
	/** Array of additional prompts to send after initialMessage */
	messages?: string[];
	/** First message to send (may contain @file content) */
	initialMessage?: string;
	/** Images to attach to the initial message */
	initialImages?: ImageContent[];
}
```

| 字段 | 说明 |
|---|---|
| `mode` | `"text"` 只输出最后的文本；`"json"` 输出全部事件。由 `toPrintOutputMode(appMode)` 得到（§4.3） |
| `initialMessage` | 第一条提示词。stdin 内容、`@file` 文本和第一条位置参数拼在一起（§4.8） |
| `initialImages` | `@file` 里的图片 |
| `messages` | 剩下的位置参数。每条依次单独 `prompt` 一次 |

**真实实例。** 主线场景里，`main()` 传给 `runPrintMode` 的是 `{ mode: "text", messages: [], initialMessage: "读 README.md，总结成三句话", initialImages: undefined }`。`initialMessage` 和 `messages` 的值取自 §3.1 最后一行 `buildInitialMessage` 的输出：它从 `parsed.messages` 里 `shift()` 掉了第一条。

### 3.4 JsonAgentSessionEvent：json 和 rpc 的事件格式

源码：[`packages/coding-agent/src/modes/json-event.ts:4-18`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/json-event.ts#L4-L18)

```ts
type WithoutPartial<T> = T extends { partial: unknown } ? Omit<T, "partial"> : T;

type ToJsonAssistantMessageEvent<T> = T extends { type: "toolcall_start"; partial: unknown }
	? WithoutPartial<T> & { id: string; toolName: string }
	: WithoutPartial<T>;

type MessageUpdateEvent = Extract<AgentSessionEvent, { type: "message_update" }>;
type JsonMessageUpdateEvent = {
	type: "message_update";
	usage: Usage;
	assistantMessageEvent: ToJsonAssistantMessageEvent<MessageUpdateEvent["assistantMessageEvent"]>;
};

/** Session event shape emitted by the JSON and RPC stdout protocols. */
export type JsonAgentSessionEvent = Exclude<AgentSessionEvent, { type: "message_update" }> | JsonMessageUpdateEvent;
```

`AgentSessionEvent`（`packages/coding-agent/src/core/agent-session.ts:209`，→ M8）是 README §3.2 事件层次里的最上层。`JsonAgentSessionEvent` 只改其中一种：`message_update`。

| 字段 | `AgentSessionEvent` 里的 `message_update` | `JsonAgentSessionEvent` 里的 `message_update` |
|---|---|---|
| `message` | 到目前为止累积的 assistant 消息 | 没有 |
| `usage` | 没有（在 `message.usage` 里） | 有，取自 `message.usage` |
| `assistantMessageEvent.partial` | 有，又一份累积消息 | 没有 |
| `assistantMessageEvent.id`、`toolName` | 没有 | 只在 `toolcall_start` 上有 |

其余事件原样输出。

**真实实例。** §2.2 的 `toolcall_start` 和 `text_delta` 两行。

### 3.5 RpcCommand 和 RpcResponse：rpc 协议

stdin 上的命令：

源码：[`packages/coding-agent/src/modes/rpc/rpc-types.ts:20-74`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-types.ts#L20-L74)

```ts
export type RpcCommand =
	// Prompting
	| { id?: string; type: "prompt"; message: string; images?: ImageContent[]; streamingBehavior?: "steer" | "followUp" }
	| { id?: string; type: "steer"; message: string; images?: ImageContent[] }
	| { id?: string; type: "follow_up"; message: string; images?: ImageContent[] }
	| { id?: string; type: "abort" }
	| { id?: string; type: "clear_queue" }
	| { id?: string; type: "new_session"; parentSession?: string }

	// State
	| { id?: string; type: "get_state" }

	// Model
	| { id?: string; type: "set_model"; provider: string; modelId: string }
	| { id?: string; type: "cycle_model" }
	| { id?: string; type: "get_available_models" }

	// Thinking
	| { id?: string; type: "set_thinking_level"; level: ThinkingLevel }
	| { id?: string; type: "cycle_thinking_level" }
	| { id?: string; type: "get_available_thinking_levels" }

	// Queue modes
	| { id?: string; type: "set_steering_mode"; mode: "all" | "one-at-a-time" }
	| { id?: string; type: "set_follow_up_mode"; mode: "all" | "one-at-a-time" }

	// Compaction
	| { id?: string; type: "compact"; customInstructions?: string }
	| { id?: string; type: "set_auto_compaction"; enabled: boolean }

	// Retry
	| { id?: string; type: "set_auto_retry"; enabled: boolean }
	| { id?: string; type: "abort_retry" }

	// Bash
	| { id?: string; type: "bash"; command: string; excludeFromContext?: boolean }
	| { id?: string; type: "abort_bash" }

	// Session
	| { id?: string; type: "get_session_stats" }
	| { id?: string; type: "export_html"; outputPath?: string }
	| { id?: string; type: "switch_session"; sessionPath: string }
	| { id?: string; type: "fork"; entryId: string }
	| { id?: string; type: "clone" }
	| { id?: string; type: "get_fork_messages" }
	| { id?: string; type: "get_entries"; since?: string }
	| { id?: string; type: "get_tree" }
	| { id?: string; type: "get_last_assistant_text" }
	| { id?: string; type: "set_session_name"; name: string }

	// Messages
	| { id?: string; type: "get_messages" }

	// Commands (available for invocation via prompt)
	| { id?: string; type: "get_commands" };
```

stdout 上的响应是 `RpcResponse`。成功响应每种命令一个变体，失败响应只有一种：

源码：[`packages/coding-agent/src/modes/rpc/rpc-types.ts:115-135`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-types.ts#L115-L135)

```ts
// Success responses with data
export type RpcResponse =
	// Prompting
	| { id?: string; type: "response"; command: "prompt"; success: true; data: { disposition: PromptDisposition } }
	| { id?: string; type: "response"; command: "steer"; success: true; data: { disposition: QueuedInputDisposition } }
	| {
			id?: string;
			type: "response";
			command: "follow_up";
			success: true;
			data: { disposition: QueuedInputDisposition };
	  }
	| { id?: string; type: "response"; command: "abort"; success: true }
	| {
			id?: string;
			type: "response";
			command: "clear_queue";
			success: true;
			data: { steering: string[]; followUp: string[] };
	  }
	| { id?: string; type: "response"; command: "new_session"; success: true; data: { cancelled: boolean } }
```

…省略 `get_state` 到 `get_commands` 的成功变体（`rpc-types.ts:137-242`）…

源码：[`packages/coding-agent/src/modes/rpc/rpc-types.ts:244-245`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-types.ts#L244-L245)

```ts
	// Error response (any command can fail)
	| { id?: string; type: "response"; command: string; success: false; error: string };
```

| 字段 | 说明 |
|---|---|
| `id` | 可选。客户端自己生成，pi 原样带回响应，用来把响应对上请求 |
| `type` | 命令名，33 种，按注释分为 prompting、state、model、thinking、queue modes、compaction、retry、bash、session、messages、commands |
| `command`（响应） | 对应的命令名。JSON 解析失败时为 `"parse"` |
| `success` / `data` / `error` | 成功时可能带 `data`，失败时带 `error` 字符串 |

stdout 上还有两类行：`AgentSessionEvent`（经过 `toJsonEvent`），以及扩展需要用户交互时发出的 `extension_ui_request`（`rpc-types.ts:252`）。客户端用 `extension_ui_response`（`rpc-types.ts:294`）回答。扩展出错时还有 `extension_error` 行（`rpc-mode.ts:349`）。这几类行靠 `type` 字段区分：`"response"`、`"extension_ui_request"`、`"extension_error"`，其余都是事件。

**真实实例。** 把三行直接管道给 rpc 模式，看原始输出：

```bash
printf '{"id":"a1","type":"get_last_assistant_text"}\n{"id":"a2","type":"clear_queue"}\nnot json\n' | WORK=/tmp/pi-lab/m9-work-8 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-8 ./run.sh --mode rpc
```

```
Running without API keys...
{"type":"queue_update","steering":[],"followUp":[]}
{"type":"response","command":"parse","success":false,"error":"Failed to parse command: Unexpected token 'o', \"not json\" is not valid JSON"}
{"id":"a1","type":"response","command":"get_last_assistant_text","success":true,"data":{}}
{"id":"a2","type":"response","command":"clear_queue","success":true,"data":{"steering":[],"followUp":[]}}
```

- `parse` 错误响应没有 `id`，因为这一行根本没解析出来。
- 还没有 assistant 消息时，`get_last_assistant_text` 的 `data` 是 `{}`。类型写的是 `{ text: string | null }`（`rpc-types.ts:228`），但 `session.getLastAssistantText()` 返回的是 `undefined`（`packages/coding-agent/src/core/agent-session.ts:4428`），`JSON.stringify` 会丢掉值为 `undefined` 的字段。客户端要把"没有 `text` 字段"也当成 null。
- 输出顺序和输入顺序不一样。§7.3 解释原因。

### 3.6 类型关系图

```
 argv ──parseArgs()──▶ Args ──resolveAppMode()──▶ AppMode
                        │                           │
                        │ createSessionManager()    │ toPrintOutputMode()
                        ▼                           ▼
                  SessionManager (M4)         PrintModeOptions.mode
                        │
                        │  createAgentSessionRuntime(createRuntime, {cwd, agentDir, sessionManager})
                        ▼
  CreateAgentSessionRuntimeFactory ──调用──▶ CreateAgentSessionRuntimeResult
   （main() 里的 createRuntime 闭包，         ├─ session: AgentSession (M8)
     被 runtime 保存，换会话时再调用）          ├─ services: AgentSessionServices
                        │                    └─ diagnostics
                        ▼
                AgentSessionRuntime ──.session──▶ AgentSession
                        │                         │ subscribe()
          ┌─────────────┼──────────────┐          ▼ AgentSessionEvent
          ▼             ▼              ▼          │
   runPrintMode   runRpcMode    InteractiveMode   ├─ text：只读 state.messages 最后一条
   (PrintMode-    (RpcCommand →                   ├─ json / rpc：toJsonEvent() → JsonAgentSessionEvent
    Options)       session 方法 → RpcResponse)     └─ TUI：渲染
```

---

## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。

```
cli.ts                                           §4.1
└─ main()                                        §4.2
    ├─ parseArgs() → resolveAppMode()            §4.3
    ├─ takeOverStdout()                          §4.4
    ├─ createSessionManager()                    §4.5
    ├─ createRuntime 闭包                         §4.6
    │   ├─ createAgentSessionServices()          → M8
    │   ├─ buildSessionOptions()                 §4.6
    │   └─ createAgentSessionFromServices()      → M8
    ├─ createAgentSessionRuntime()               §4.7
    │   └─ AgentSessionRuntime（替换会话、rebind）  §4.7
    ├─ readPipedStdin() / buildInitialMessage()  §4.8
    └─ 按 appMode 分派                            §4.9
        ├─ runPrintMode()                        §4.10
        │   └─ toJsonEvent()                     §4.11
        ├─ runRpcMode()                          §4.12
        │   └─ attachJsonlLineReader()           §4.13
        └─ InteractiveMode                       不在范围
SDK：createAgentSession() 和 examples/sdk        §4.14
PI_TIMING：timings.ts                            §4.15
```

### 4.1 cli.ts 和 setupCli()

**调用位置**：`pi` 命令（`package.json` 的 `bin` 指向构建后的 `dist/bundle/cli.js`）；从源码运行时是 `pi-test.sh` 的最后一行：

源码：[`pi-test.sh:83-85`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/pi-test.sh#L83-L85)

```bash
# --import takes a module specifier, so pass the resolver as a file URL (raw paths break on #, ?, %).
RESOLVER_URL="$(node -p 'require("node:url").pathToFileURL(process.argv[1]).href' "$SCRIPT_DIR/packages/coding-agent/src/experimental/source-resolver.ts")"
node --import "$RESOLVER_URL" "$SCRIPT_DIR/packages/coding-agent/src/cli.ts" ${ARGS[@]+"${ARGS[@]}"}
```

（`pi-test.sh` 在仓库根目录，不在 `packages/` 下。）

源码：[`packages/coding-agent/src/cli.ts:1-6`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/cli.ts#L1-L6)

```ts
#!/usr/bin/env node
import { setupCli } from "./cli/setup.ts";
import { main } from "./main.ts";

setupCli();
main(process.argv.slice(2));
```

源码：[`packages/coding-agent/src/cli/setup.ts:1-13`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/cli/setup.ts#L1-L13)

```ts
import { APP_NAME } from "../config.ts";
import { configureHttpDispatcher } from "../core/http-dispatcher.ts";

export function setupCli(): void {
	process.title = APP_NAME;
	process.env.PI_CODING_AGENT = "true";
	process.env.AI_AGENT = "pi";
	process.emitWarning = (() => {}) as typeof process.emitWarning;

	// Configure undici before provider SDKs issue requests. Settings are applied
	// once SettingsManager has loaded global/project configuration.
	configureHttpDispatcher();
}
```

**讲解**

`cli.ts` 只有两步：`setupCli()` 设置进程级的东西，`main()` 做其余所有事。`setupCli` 设了两个环境变量 `PI_CODING_AGENT=true` 和 `AI_AGENT=pi`，pi 启动的子进程（例如 bash 工具里跑的命令）能据此知道自己运行在 agent 里。`process.emitWarning` 被换成空函数，Node 的实验特性警告不会混进输出。

`main()` 是导出函数，第二个参数 `MainOptions`（`packages/coding-agent/src/main.ts:570`）可以传入额外的内联扩展。仓库里还有一个 RPC 专用入口 `packages/coding-agent/src/rpc-entry.ts`，它做同样的设置后调用 `main(["--mode", "rpc", ...argv])`。

**往哪走**：§4.2 `main()`。

### 4.2 main() ①：启动和子命令

**调用位置**：`cli.ts:6`。

源码：[`packages/coding-agent/src/main.ts:574-633`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L574-L633)

```ts
export async function main(args: string[], options?: MainOptions) {
	resetTimings();
	// Commands and startup selectors can load user-authored themes before runtime creation.
	setThemeJsonValidator(validateThemeJson);
	const extensionFactories = [...builtInExtensions, ...(options?.extensionFactories ?? [])];
	const offlineMode = args.includes("--offline") || isTruthyEnvFlag(process.env.PI_OFFLINE);
	if (offlineMode) {
		process.env.PI_OFFLINE = "1";
		process.env.PI_SKIP_VERSION_CHECK = "1";
	}

	if (await runAuthCommand(args)) {
		return;
	}

	if (process.platform === "win32") {
		cleanupWindowsSelfUpdateQuarantine(getPackageDir());
	}
	cleanupManagedInstall();

	let cwd: string;
	try {
		cwd = process.cwd();
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
		console.error(
			chalk.red(
				"Error: The current working directory no longer exists. Change to an existing directory and try again.",
			),
		);
		process.exit(1);
	}
	const agentDir = getAgentDir();
	const bootstrapSettingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
	applyHttpProxySettings(bootstrapSettingsManager.getGlobalSettings().httpProxy);
	configureHttpDispatcher();

	if (await handlePackageCommand(args, { extensionFactories })) {
		const exitCode = process.exitCode ?? 0;
		if (process.platform === "win32" && exitCode === 0 && args[0] === "update") {
			// We normally prefer process.exit(0) for package commands so bad extensions cannot keep
			// one-shot commands alive. On Windows, Node can assert after fetch() if process.exit(0)
			// runs during teardown; let successful `pi update` drain naturally instead.
			// https://github.com/nodejs/node/issues/56645
			return;
		}
		process.exit(exitCode);
		return;
	}

	if (await handleConfigCommand(args, { extensionFactories })) {
		return;
	}

	if (args[0] === "mcp") {
		const { runMcpCommand } = await loadMcpCommand();
		process.exitCode = await runMcpCommand(args.slice(1), { cwd, agentDir });
		return;
	}

```

**讲解**

`main()` 开头处理不需要 AgentSession 的子命令，处理完就 `return` 或退出：

- `resetTimings()`（575 行）开始 `PI_TIMING` 的计时（§4.15）。
- 578 行把内置扩展放在 `extensionFactories` 最前面，后面传给 resource loader（§4.6）。
- `pi auth …`（585 行）、`pi install/update/…` 等包管理命令（611 行）、`pi config`（624 行）、`pi mcp …`（628 行）各自处理。这些都不在本教程范围。
- 606 行 `getAgentDir()` 读环境变量 `PI_CODING_AGENT_DIR`（`packages/coding-agent/src/config.ts:630`），没设置时用 `~/.pi/agent`。本教程的实验都设了它。

607 行建了一个 `projectTrusted: false` 的 `SettingsManager`，只用来读全局设置里的 HTTP 代理。它不读项目设置，因为这时还不知道项目是否可信（→ M6）。

**往哪走**：§4.3 参数解析和模式判断。

### 4.3 main() ②：parseArgs() 和 resolveAppMode()

**调用位置**：`main()` 634 行起。

源码：[`packages/coding-agent/src/main.ts:634-681`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L634-L681)

```ts
	const parsed = parseArgs(args);
	if (parsed.diagnostics.length > 0) {
		for (const d of parsed.diagnostics) {
			const color = d.type === "error" ? chalk.red : chalk.yellow;
			console.error(color(`${d.type === "error" ? "Error" : "Warning"}: ${d.message}`));
		}
		if (parsed.diagnostics.some((d) => d.type === "error")) {
			process.exit(1);
		}
	}
	time("parseArgs");

	if (parsed.version) {
		console.log(VERSION);
		process.exit(0);
	}

	if (parsed.export) {
		let result: string;
		try {
			const outputPath = parsed.messages.length > 0 ? parsed.messages[0] : undefined;
			result = await exportFromFile(parsed.export, outputPath);
		} catch (error: unknown) {
			const message = error instanceof Error ? error.message : "Failed to export session";
			console.error(chalk.red(`Error: ${message}`));
			process.exit(1);
		}
		console.log(`Exported to: ${result}`);
		process.exit(0);
	}

	let appMode = resolveAppMode(parsed, process.stdin.isTTY, process.stdout.isTTY);
	const shouldTakeOverStdout = appMode !== "interactive" && !isPlainRuntimeMetadataCommand(parsed);
	if (shouldTakeOverStdout) {
		takeOverStdout();
	}

	if (parsed.mode === "rpc" && parsed.fileArgs.length > 0) {
		console.error(chalk.red("Error: @file arguments are not supported in RPC mode"));
		process.exit(1);
	}

	validateForkFlags(parsed);
	validateSessionIdFlags(parsed);

	// Run migrations (pass cwd for project-local migrations)
	const { migratedAuthProviders: migratedProviders, deprecationWarnings } = runMigrations(cwd);
	time("runMigrations");
```

`resolveAppMode` 和 `toPrintOutputMode`：

源码：[`packages/coding-agent/src/main.ts:112-127`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L112-L127)

```ts
function resolveAppMode(parsed: Args, stdinIsTTY: boolean, stdoutIsTTY: boolean): AppMode {
	if (parsed.mode === "rpc") {
		return "rpc";
	}
	if (parsed.mode === "json") {
		return "json";
	}
	if (parsed.print || !stdinIsTTY || !stdoutIsTTY) {
		return "print";
	}
	return "interactive";
}

function toPrintOutputMode(appMode: AppMode): Exclude<Mode, "rpc"> {
	return appMode === "json" ? "json" : "text";
}
```

**讲解**

`parseArgs` 的结果见 §3.1。有 error 级别的诊断就退出（640 行）；`--version`、`--export` 也在这里处理掉。

`resolveAppMode` 的优先级是：

1. `--mode rpc` → `rpc`。
2. `--mode json` → `json`。
3. 有 `-p`，或者 stdin 不是终端，或者 stdout 不是终端 → `print`。
4. 否则 → `interactive`。

所以 `pi "问题" > out.txt` 和 `echo 问题 | pi` 都会走 print 模式，不会启动 TUI。`--mode text` 不改变判断：它在第 3 步之前没有被检查，只在终端里运行时仍然是 `interactive`。`toPrintOutputMode` 把 `json` 映射为 json 输出，其余（也就是 `print`）映射为 text 输出。

还有一处会改模式：§4.8 里如果从 stdin 读到了内容而模式是 `interactive`，模式改成 `print`（`packages/coding-agent/src/main.ts:910-911`）。

666–669 行：除了交互模式和 `--help`/`--list-models` 这种纯查询命令，都要接管 stdout（§4.4）。671 行：rpc 模式下 stdin 是命令通道，不能再用 `@file` 参数。

**往哪走**：§4.4 `takeOverStdout()`。

### 4.4 takeOverStdout()：stdout 只留给协议输出

**调用位置**：`main()` 668 行；`runRpcMode()` 开头也调用一次（重复调用直接返回）。

源码：[`packages/coding-agent/src/core/output-guard.ts:45-70`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/output-guard.ts#L45-L70)

```ts
export function takeOverStdout(): void {
	if (stdoutTakeoverState) {
		return;
	}

	const rawStdoutWrite = process.stdout.write.bind(process.stdout) as StdoutTakeoverState["rawStdoutWrite"];
	const rawStderrWrite = process.stderr.write.bind(process.stderr) as StdoutTakeoverState["rawStderrWrite"];
	const originalStdoutWrite = process.stdout.write;

	process.stdout.write = ((
		chunk: string | Uint8Array,
		encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
		callback?: (error?: Error | null) => void,
	): boolean => {
		if (typeof encodingOrCallback === "function") {
			return rawStderrWrite(String(chunk), encodingOrCallback);
		}
		return rawStderrWrite(String(chunk), callback);
	}) as typeof process.stdout.write;

	stdoutTakeoverState = {
		rawStdoutWrite,
		rawStderrWrite,
		originalStdoutWrite,
	};
}
```

真正写 stdout 要用 `writeRawStdout`：

源码：[`packages/coding-agent/src/core/output-guard.ts:85-108`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/output-guard.ts#L85-L108)

```ts
export function writeRawStdout(text: string): void {
	if (text.length === 0) {
		return;
	}
	rawStdoutWriteTail = rawStdoutWriteTail.then(() => writeRawStdoutChunk(text));
	void rawStdoutWriteTail.catch(() => {
		process.exit(1);
	});
}

export async function waitForRawStdoutBackpressure(): Promise<void> {
	while (true) {
		const tail = rawStdoutWriteTail;
		await tail;
		if (tail === rawStdoutWriteTail) {
			return;
		}
	}
}

export async function flushRawStdout(): Promise<void> {
	await waitForRawStdoutBackpressure();
	await writeRawStdoutChunk("");
}
```

**讲解**

接管之后，`process.stdout.write` 被换成一个写 stderr 的函数。任何代码（pi 自己的、扩展的、第三方库的）调用 `console.log`，内容都去了 stderr。只有调用 `writeRawStdout` 的地方能写到真正的 stdout：print 模式的最终文本、json 模式的事件行、rpc 模式的所有输出。这样 json 和 rpc 的 stdout 上每一行都是合法 JSON，text 模式的 stdout 上只有答案。

`writeRawStdout` 不直接写，而是把写操作挂到 `rawStdoutWriteTail` 这条 Promise 链上，按顺序一个个写；遇到 `EAGAIN` 之类的错误时等 10ms 重试（`output-guard.ts:37-40`）。`waitForRawStdoutBackpressure` 等到链上的写操作全部完成，§4.10 会看到它被用来给 agent 循环施加背压。

**往哪走**：§4.5 `createSessionManager()`。

### 4.5 createSessionManager()：选会话来源

**调用位置**：`main()` 707 行。调用之前先算出 `sessionDir`：

源码：[`packages/coding-agent/src/main.ts:683-729`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L683-L729)

```ts
	const startupSettingsManager = SettingsManager.create(cwd, agentDir);
	const startupSettingsDiagnostics = collectSettingsDiagnostics(startupSettingsManager);

	// Experimental first-time setup: theme choice and analytics opt-in.
	// Runs before any runtime services are created so the chosen settings apply everywhere.
	if (appMode === "interactive" && !parsed.help && parsed.listModels === undefined && shouldRunFirstTimeSetup()) {
		await showFirstTimeSetup(startupSettingsManager);
		time("firstTimeSetup");
	}

	if (appMode === "interactive" && parsed.useTheme !== undefined) {
		startupSettingsManager.applyOverrides({ theme: parsed.useTheme });
	}

	// Decide the final runtime cwd before creating cwd-bound runtime services.
	// --session and --resume may select a session from another project, so project-local
	// settings, resources, provider registrations, and models must be resolved only after
	// the target session cwd is known. The startup-cwd settings manager is used only for
	// sessionDir lookup during session selection.
	const envSessionDir = process.env[ENV_SESSION_DIR];
	const sessionDir =
		(parsed.sessionDir ? normalizePath(parsed.sessionDir) : undefined) ??
		(envSessionDir ? expandTildePath(envSessionDir) : undefined) ??
		startupSettingsManager.getSessionDir();
	let sessionManager = await createSessionManager(parsed, cwd, sessionDir, startupSettingsManager);
	const missingSessionCwdIssue = getMissingSessionCwdIssue(sessionManager, cwd);
	if (missingSessionCwdIssue) {
		if (appMode === "interactive") {
			const selectedCwd = await promptForMissingSessionCwd(missingSessionCwdIssue, startupSettingsManager);
			if (!selectedCwd) {
				process.exit(0);
			}
			sessionManager = SessionManager.open(missingSessionCwdIssue.sessionFile!, sessionDir, selectedCwd);
		} else {
			console.error(chalk.red(new MissingSessionCwdError(missingSessionCwdIssue).message));
			process.exit(1);
		}
	}
	if (parsed.name !== undefined) {
		const name = normalizeSessionName(parsed.name);
		if (name === undefined) {
			console.error(chalk.red("Error: --name requires a non-empty value"));
			process.exit(1);
		}
		sessionManager.appendSessionInfo(name);
	}
	time("createSessionManager");
```

`createSessionManager` 前半段：

源码：[`packages/coding-agent/src/main.ts:358-413`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L358-L413)

```ts
export async function createSessionManager(
	parsed: Args,
	cwd: string,
	sessionDir: string | undefined,
	settingsManager: SettingsManager,
): Promise<SessionManager> {
	if (parsed.noSession || parsed.help || parsed.listModels !== undefined) {
		return SessionManager.inMemory(cwd, parsed.sessionId !== undefined ? { id: parsed.sessionId } : undefined);
	}

	if (parsed.fork) {
		if (parsed.sessionId) {
			const existingTarget = findLocalSessionByExactId(parsed.sessionId, cwd, sessionDir);
			if (existingTarget) {
				console.error(chalk.red(`Session already exists with id '${parsed.sessionId}'`));
				process.exit(1);
			}
		}

		const resolved = await resolveSessionPath(parsed.fork, cwd, sessionDir);

		switch (resolved.type) {
			case "path":
			case "local":
			case "global":
				return forkSessionOrExit(resolved.path, cwd, sessionDir, parsed.sessionId);

			case "not_found":
				console.error(chalk.red(`No session found matching '${resolved.arg}'`));
				process.exit(1);
		}
	}

	if (parsed.session) {
		const resolved = await resolveSessionPath(parsed.session, cwd, sessionDir);

		switch (resolved.type) {
			case "path":
			case "local":
				return openSessionOrExit(resolved.path, sessionDir);

			case "global": {
				console.log(chalk.yellow(`Session found in different project: ${resolved.cwd}`));
				const shouldFork = await promptConfirm("Fork this session into current directory?");
				if (!shouldFork) {
					console.log(chalk.dim("Aborted."));
					process.exit(0);
				}
				return forkSessionOrExit(resolved.path, cwd, sessionDir);
			}

			case "not_found":
				console.error(chalk.red(`No session found matching '${resolved.arg}'`));
				process.exit(1);
		}
	}
```

后半段：

源码：[`packages/coding-agent/src/main.ts:415-449`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L415-L449)

```ts
	if (parsed.resume) {
		try {
			const selectedPath = await selectSession(
				(onProgress, signal) => SessionManager.list(cwd, sessionDir, onProgress, signal),
				(onProgress, signal) => SessionManager.listAll(sessionDir, onProgress, signal),
				settingsManager,
			);
			if (!selectedPath) {
				console.log(chalk.dim("No session selected"));
				process.exit(0);
			}
			return SessionManager.open(selectedPath, sessionDir);
		} finally {
			stopThemeWatcher();
		}
	}

	if (parsed.continue) {
		return SessionManager.continueRecent(cwd, sessionDir);
	}

	if (parsed.sessionId) {
		const existingSession = findLocalSessionByExactId(parsed.sessionId, cwd, sessionDir);
		if (existingSession) {
			return SessionManager.open(existingSession.path, sessionDir);
		}
		console.error(
			chalk.yellow(
				`Warning: No project session found with id '${parsed.sessionId}'; creating a new session with that id.`,
			),
		);
	}

	return SessionManager.create(cwd, sessionDir, { id: parsed.sessionId });
}
```

**讲解**

会话目录的优先级是 `--session-dir` > 环境变量 `PI_CODING_AGENT_SESSION_DIR`（`config.ts:611`）> 设置里的 `sessionDir`。都没有时传 `undefined`，由 `SessionManager` 用默认目录 `<agentDir>/sessions/<编码后的 cwd>/`。

`createSessionManager` 按固定顺序检查参数，第一个命中的分支决定结果：

| 顺序 | 条件 | 结果 | 行 |
|---|---|---|---|
| 1 | `--no-session`、`--help`、`--list-models` | `SessionManager.inMemory()`，不写文件 | 364–366 |
| 2 | `--fork <id或路径>` | 找到源会话，`SessionManager.forkFrom()` 复制到当前项目 | 368–389 |
| 3 | `--session <id或路径>` | `SessionManager.open()`；会话属于别的项目时先问是否分叉 | 391–413 |
| 4 | `--resume` / `-r` | 弹出会话选择器，`open()` 选中的文件 | 415–430 |
| 5 | `--continue` / `-c` | `SessionManager.continueRecent()`：最近的会话，没有就新建 | 432–434 |
| 6 | `--session-id <id>` | 已有同 id 会话就打开，否则用这个 id 新建 | 436–446 |
| 7 | 都没有 | `SessionManager.create()` 新建 | 448 |

`resolveSessionPath`（`main.ts:252`）负责把 `--session`/`--fork` 的参数解析成文件：带 `/` 或以 `.jsonl` 结尾就当路径，否则当 id 前缀，先在当前项目找，再全局找。

这些 `SessionManager` 的静态方法都在 M4 读。这里要注意一点：`create()` 和 `continueRecent()` 返回的 `SessionManager` 不会立即建文件，第一条 user 或 assistant 消息写入时才建（→ M4 `_persist`）。

`createSessionManager` 之后，`main()` 检查会话记录的 cwd 是否还存在（708 行）：交互模式下弹窗选，其他模式直接报错退出。`--name` 写一条 `session_info` 条目（727 行）。

**往哪走**：§4.6 `createRuntime` 闭包。

### 4.6 createRuntime 闭包：建服务、解析模型、建 AgentSession

**调用位置**：`main()` 744 行定义，`createAgentSessionRuntime()` 第一次调用它（§4.7）；之后每次替换会话，runtime 再调用它。

第一段：决定项目是否可信，建服务：

源码：[`packages/coding-agent/src/main.ts:744-807`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L744-L807)

```ts
	const createRuntime: CreateAgentSessionRuntimeFactory = async ({
		cwd,
		agentDir,
		sessionManager,
		sessionStartEvent,
		projectTrustContext,
	}) => {
		const isInitialRuntime = sessionStartEvent === undefined;
		const projectTrustDiagnostics: AgentSessionRuntimeDiagnostic[] = [];
		const cachedProjectTrust = projectTrustByCwd.get(cwd);
		const hasTrustRequiringResources = hasTrustRequiringProjectResources(cwd);
		const shouldResolveProjectTrust =
			parsed.projectTrustOverride === undefined && cachedProjectTrust === undefined && hasTrustRequiringResources;
		const projectTrusted = shouldResolveProjectTrust
			? false
			: (cachedProjectTrust ?? parsed.projectTrustOverride ?? trustStore.get(cwd) ?? !hasTrustRequiringResources);
		const runtimeSettingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted });
		const services = await createAgentSessionServices({
			cwd,
			agentDir,
			settingsManager: runtimeSettingsManager,
			modelRuntimeSignal: AbortSignal.timeout(15_000),
			extensionFlagValues: parsed.unknownFlags,
			resourceLoaderReloadOptions: shouldResolveProjectTrust
				? {
						resolveProjectTrust: async ({ extensionsResult }) => {
							const trusted = await resolveProjectTrusted({
								cwd,
								trustStore,
								trustOverride: parsed.projectTrustOverride,
								defaultProjectTrust: startupSettingsManager.getDefaultProjectTrust(),
								extensionsResult,
								projectTrustContext:
									projectTrustContext ??
									createProjectTrustContext({
										cwd,
										mode: isInitialRuntime ? trustPromptMode : appMode,
										settingsManager: startupSettingsManager,
										hasUI: isInitialRuntime && trustPromptMode === "interactive",
									}),
								onExtensionError: (message) => projectTrustDiagnostics.push({ type: "warning", message }),
							});
							projectTrustByCwd.set(cwd, trusted);
							return trusted;
						},
					}
				: undefined,
			resourceLoaderOptions: {
				additionalExtensionPaths: resolvedExtensionPaths,
				additionalSkillPaths: resolvedSkillPaths,
				additionalPromptTemplatePaths: resolvedPromptTemplatePaths,
				additionalThemePaths: resolvedThemePaths,
				noExtensions: parsed.noExtensions,
				disabledBuiltinExtensions: parsed.noMcp ? ["mcp"] : undefined,
				noSkills: parsed.noSkills,
				noPromptTemplates: parsed.noPromptTemplates,
				noThemes: parsed.noThemes,
				noContextFiles: parsed.noContextFiles,
				systemPrompt: parsed.systemPrompt,
				appendSystemPrompt: parsed.appendSystemPrompt,
				extensionFactories,
			},
		});
		const { settingsManager, modelRuntime, resourceLoader } = services;
```

第二段：收集诊断、解析模型、建 AgentSession：

源码：[`packages/coding-agent/src/main.ts:822-873`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L822-L873)

```ts
		const modelPatterns = parsed.models ?? settingsManager.getEnabledModels();
		const scopedModels =
			modelPatterns && modelPatterns.length > 0
				? await resolveModelScope(modelPatterns, modelRuntime, { signal: AbortSignal.timeout(15_000) })
				: [];
		const {
			options: sessionOptions,
			cliThinkingFromModel,
			diagnostics: sessionOptionDiagnostics,
		} = buildSessionOptions(
			parsed,
			scopedModels,
			sessionManager.buildSessionContext().messages.length > 0,
			modelRuntime,
			settingsManager,
		);
		diagnostics.push(...sessionOptionDiagnostics);

		if (parsed.apiKey) {
			if (!sessionOptions.model) {
				diagnostics.push({
					type: "error",
					message: "--api-key requires a model to be specified via --model, --provider/--model, or --models",
				});
			} else {
				await modelRuntime.setRuntimeApiKey(sessionOptions.model.provider, parsed.apiKey);
			}
		}

		const created = await createAgentSessionFromServices({
			services,
			sessionManager,
			sessionStartEvent,
			model: sessionOptions.model,
			thinkingLevel: sessionOptions.thinkingLevel,
			scopedModels: sessionOptions.scopedModels,
			tools: sessionOptions.tools,
			excludeTools: sessionOptions.excludeTools,
			noTools: sessionOptions.noTools,
			customTools: sessionOptions.customTools,
		});
		const cliThinkingOverride = parsed.thinking !== undefined || cliThinkingFromModel;
		if (created.session.model && cliThinkingOverride) {
			created.session.setThinkingLevel(created.session.thinkingLevel);
		}

		return {
			...created,
			services,
			diagnostics,
		};
	};
```

**讲解**

`createRuntime` 是 `main()` 里的一个闭包。它捕获了 `parsed`、`trustStore`、`projectTrustByCwd`、CLI 传入的资源路径等"进程级不变的输入"，参数里只接收随会话变化的东西：`cwd`、`sessionManager`、`sessionStartEvent`。换会话时 cwd 可能变（`/resume` 到别的项目），所以服务必须按新 cwd 重建，而 CLI 参数不变。

闭包做三件事：

1. **项目信任**（751–760 行）。项目目录里有需要信任的资源（例如项目级扩展）、又没有缓存的决定时，先按不信任建 `SettingsManager`，在 resource loader 加载时通过 `resolveProjectTrust` 回调询问（→ M6）。
2. **建服务**（761 行）。`createAgentSessionServices`（→ M8）创建 `ModelRuntime`、加载扩展和资源，并把扩展注册的 provider 加进 `ModelRuntime`。主线场景的 `faux` provider 就是在这里注册的。`resourceLoaderOptions` 里是 CLI 的资源参数：`-e` 进 `additionalExtensionPaths`，`--no-mcp` 禁用内置 mcp 扩展，`--system-prompt` 原样传下去。
3. **解析模型、建 session**（822–862 行）。`buildSessionOptions`（`main.ts:451`）把 `--provider`/`--model`/`--thinking`/`--models`/`--tools` 转成 `createAgentSession` 的选项。它要在服务建好之后调用，因为扩展注册的模型（例如 `faux/faux-1`）只有那时才查得到。最后 `createAgentSessionFromServices`（→ M8）调用 `createAgentSession()`。

808–820 行（没有摘录）把服务、设置、扩展加载的错误和警告汇总成 `diagnostics`。闭包本身不打印、不退出，交给 `main()` 决定（§4.8）。

**往哪走**：§4.7 `createAgentSessionRuntime()`。M8 从 `createAgentSession()` 读起。

### 4.7 createAgentSessionRuntime() 和 AgentSessionRuntime

**调用位置**：`main()` 875 行：

源码：[`packages/coding-agent/src/main.ts:874-880`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L874-L880)

```ts
	time("createRuntime");
	const runtime = await createAgentSessionRuntime(createRuntime, {
		cwd: sessionManager.getCwd(),
		agentDir,
		sessionManager,
	});
	time("createAgentSessionRuntime");
```

源码：[`packages/coding-agent/src/core/agent-session-runtime.ts:414-438`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-runtime.ts#L414-L438)

```ts
/**
 * Create the initial runtime from a runtime factory and initial session target.
 *
 * The same factory is stored on the returned AgentSessionRuntime and reused for
 * later /new, /resume, /fork, and import flows.
 */
export async function createAgentSessionRuntime(
	createRuntime: CreateAgentSessionRuntimeFactory,
	options: {
		cwd: string;
		agentDir: string;
		sessionManager: SessionManager;
		sessionStartEvent?: SessionStartEvent;
	},
): Promise<AgentSessionRuntime> {
	assertSessionCwdExists(options.sessionManager, options.cwd);
	const result = await createRuntime(options);
	return new AgentSessionRuntime(
		result.session,
		result.services,
		createRuntime,
		result.diagnostics,
		result.modelFallbackMessage,
	);
}
```

`AgentSessionRuntime` 的字段和对前端开放的两个回调：

源码：[`packages/coding-agent/src/core/agent-session-runtime.ts:67-131`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-runtime.ts#L67-L131)

```ts
/**
 * Owns the current AgentSession plus its cwd-bound services.
 *
 * Session replacement methods tear down the current runtime first, then create
 * and apply the next runtime. If creation fails, the error is propagated to the
 * caller. The caller is responsible for user-facing error handling.
 */
export class AgentSessionRuntime {
	private rebindSession?: (session: AgentSession) => Promise<void>;
	private beforeSessionInvalidate?: () => void;
	private _session: AgentSession;
	private _services: AgentSessionServices;
	private readonly createRuntime: CreateAgentSessionRuntimeFactory;
	private _diagnostics: AgentSessionRuntimeDiagnostic[];
	private _modelFallbackMessage?: string;

	constructor(
		_session: AgentSession,
		_services: AgentSessionServices,
		createRuntime: CreateAgentSessionRuntimeFactory,
		_diagnostics: AgentSessionRuntimeDiagnostic[] = [],
		_modelFallbackMessage?: string,
	) {
		this._session = _session;
		this._services = _services;
		this.createRuntime = createRuntime;
		this._diagnostics = _diagnostics;
		this._modelFallbackMessage = _modelFallbackMessage;
	}

	get services(): AgentSessionServices {
		return this._services;
	}

	get session(): AgentSession {
		return this._session;
	}

	get cwd(): string {
		return this._services.cwd;
	}

	get diagnostics(): readonly AgentSessionRuntimeDiagnostic[] {
		return this._diagnostics;
	}

	get modelFallbackMessage(): string | undefined {
		return this._modelFallbackMessage;
	}

	setRebindSession(rebindSession?: (session: AgentSession) => Promise<void>): void {
		this.rebindSession = rebindSession;
	}

	/**
	 * Set a synchronous callback that runs after `session_shutdown` handlers finish
	 * but before the current session is invalidated.
	 *
	 * This is for host-owned UI teardown that must not yield to the event loop,
	 * such as detaching extension-provided TUI components before the old extension
	 * context becomes stale.
	 */
	setBeforeSessionInvalidate(beforeSessionInvalidate?: () => void): void {
		this.beforeSessionInvalidate = beforeSessionInvalidate;
	}
```

替换会话的公共步骤，以 `newSession` 为例：

源码：[`packages/coding-agent/src/core/agent-session-runtime.ts:167-194`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-runtime.ts#L167-L194)

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

	private apply(result: CreateAgentSessionRuntimeResult): void {
		this._session = result.session;
		this._services = result.services;
		this._diagnostics = result.diagnostics;
		this._modelFallbackMessage = result.modelFallbackMessage;
	}

	private async finishSessionReplacement(withSession?: (ctx: ReplacedSessionContext) => Promise<void>): Promise<void> {
		if (this.rebindSession) {
			await this.rebindSession(this.session);
		}
		if (withSession) {
			await withSession(this.session.createReplacedSessionContext());
		}
	}
```

源码：[`packages/coding-agent/src/core/agent-session-runtime.ts:226-260`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session-runtime.ts#L226-L260)

```ts
	async newSession(options?: {
		parentSession?: string;
		setup?: (sessionManager: SessionManager) => Promise<void>;
		withSession?: (ctx: ReplacedSessionContext) => Promise<void>;
	}): Promise<{ cancelled: boolean }> {
		const beforeResult = await this.emitBeforeSwitch("new");
		if (beforeResult.cancelled) {
			return beforeResult;
		}

		const previousSessionFile = this.session.sessionFile;
		const sessionDir = this.session.sessionManager.getSessionDir();
		const sessionManager = this.session.sessionManager.isPersisted()
			? SessionManager.create(this.cwd, sessionDir)
			: SessionManager.inMemory(this.cwd);
		if (options?.parentSession) {
			sessionManager.newSession({ parentSession: options.parentSession });
		}

		await this.teardownCurrent("new", sessionManager.getSessionFile());
		this.apply(
			await this.createRuntime({
				cwd: this.cwd,
				agentDir: this.services.agentDir,
				sessionManager,
				sessionStartEvent: { type: "session_start", reason: "new", previousSessionFile },
			}),
		);
		if (options?.setup) {
			await options.setup(this.session.sessionManager);
			this.session.refreshContext();
		}
		await this.finishSessionReplacement(options?.withSession);
		return { cancelled: false };
	}
```

**讲解**

`createAgentSessionRuntime` 检查 cwd 存在，调用一次工厂，把结果和工厂本身一起存进 `AgentSessionRuntime`。

runtime 把"换一个会话"做成整体替换，而不是在同一个 `AgentSession` 上改：

1. `emitBeforeSwitch`：扩展可以通过 `session_before_switch` 事件取消（→ M7）。
2. 新建 `SessionManager`。
3. `teardownCurrent`：先 `abort()` 当前运行，让中止的那一轮写进旧会话文件；发 `session_shutdown` 给扩展；`dispose()` 旧 session。
4. 用保存的 `createRuntime` 建一套新的 session 和服务，`apply()` 替换字段。
5. `finishSessionReplacement`：调用前端注册的 `rebindSession` 回调。

第 5 步是前端和 runtime 之间的约定。前端在启动时调用 `runtime.setRebindSession(fn)`，在 `fn` 里重新执行 `session.bindExtensions(...)` 和 `session.subscribe(...)`。旧 session 被 `dispose()` 时清空了所有监听器（`packages/coding-agent/src/core/agent-session.ts:1443`），不重新订阅就收不到新 session 的事件。§3.2 的实例里，`newSession()` 之后 `runtime.session === firstSession` 是 `false`，就是这个原因。

`switchSession`、`fork`、`importFromJsonl`（196–402 行）走同样的五步，区别只在第 2 步怎么得到 `SessionManager`。`dispose()`（404–411 行）用于进程退出：发 `session_shutdown`（`reason: "quit"`）后销毁 session。

**往哪走**：§4.8 读 stdin 和组装首条提示词。

### 4.8 main() ③：读 stdin、组装首条提示词、处理诊断

**调用位置**：`main()` 881 行起。

源码：[`packages/coding-agent/src/main.ts:881-941`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L881-L941)

```ts
	const { services, session, modelFallbackMessage } = runtime;
	const { settingsManager, modelRuntime, resourceLoader } = services;
	setCapabilityOverrides(settingsManager.getTerminalCapabilityOverrides());
	applyHttpProxySettings(settingsManager.getGlobalSettings().httpProxy);
	configureHttpDispatcher(settingsManager.getHttpIdleTimeoutMs());

	const startupDiagnostics = deduplicateDiagnostics([...startupSettingsDiagnostics, ...runtime.diagnostics]);
	const hasRuntimeErrors = runtime.diagnostics.some((diagnostic) => diagnostic.type === "error");

	if (parsed.help) {
		reportDiagnostics(startupDiagnostics);
		const extensionFlags = resourceLoader
			.getExtensions()
			.extensions.flatMap((extension) => Array.from(extension.flags.values()));
		printHelp(extensionFlags);
		process.exit(0);
	}

	if (parsed.listModels !== undefined) {
		reportDiagnostics(startupDiagnostics);
		const searchPattern = typeof parsed.listModels === "string" ? parsed.listModels : undefined;
		await listModels(modelRuntime, searchPattern, AbortSignal.timeout(15_000));
		process.exit(0);
	}

	// Read piped stdin content (if any) - skip for RPC mode which uses stdin for JSON-RPC
	let stdinContent: string | undefined;
	if (appMode !== "rpc") {
		stdinContent = await readPipedStdin();
		if (stdinContent !== undefined && appMode === "interactive") {
			appMode = "print";
		}
	}
	time("readPipedStdin");

	const { initialMessage, initialImages } = await prepareInitialMessage(parsed, stdinContent);
	time("prepareInitialMessage");
	initTheme(settingsManager.getTheme(), appMode === "interactive");
	time("initTheme");

	// Show deprecation warnings in interactive mode
	if (appMode === "interactive" && deprecationWarnings.length > 0) {
		await showDeprecationWarnings(deprecationWarnings);
	}

	time("resolveModelScope");
	if (appMode !== "interactive" || hasRuntimeErrors) {
		reportDiagnostics(startupDiagnostics);
	}
	if (hasRuntimeErrors) {
		if (runtime.diagnostics.some((diagnostic) => diagnostic.message.includes("Failed to load extension"))) {
			console.error(chalk.yellow(EXTENSION_LOAD_FAILURE_HINT));
		}
		process.exit(1);
	}
	time("createAgentSession");

	if (appMode !== "interactive" && !session.model) {
		console.error(chalk.red(formatNoModelsAvailableMessage()));
		process.exit(1);
	}
```

`readPipedStdin`：

源码：[`packages/coding-agent/src/main.ts:76-97`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L76-L97)

```ts
/**
 * Read all content from piped stdin.
 * Returns undefined if stdin is a TTY (interactive terminal).
 */
async function readPipedStdin(): Promise<string | undefined> {
	// If stdin is a TTY, we're running interactively - don't read stdin
	if (process.stdin.isTTY) {
		return undefined;
	}

	return new Promise((resolve) => {
		let data = "";
		process.stdin.setEncoding("utf8");
		process.stdin.on("data", (chunk) => {
			data += chunk;
		});
		process.stdin.on("end", () => {
			resolve(data.trim() || undefined);
		});
		process.stdin.resume();
	});
}
```

`buildInitialMessage`：

源码：[`packages/coding-agent/src/cli/initial-message.ts:16-43`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/cli/initial-message.ts#L16-L43)

```ts
/**
 * Combine stdin content, @file text, and the first CLI message into a single
 * initial prompt for non-interactive mode.
 */
export function buildInitialMessage({
	parsed,
	fileText,
	fileImages,
	stdinContent,
}: InitialMessageInput): InitialMessageResult {
	const parts: string[] = [];
	if (stdinContent !== undefined) {
		parts.push(stdinContent);
	}
	if (fileText) {
		parts.push(fileText);
	}

	if (parsed.messages.length > 0) {
		parts.push(parsed.messages[0]);
		parsed.messages.shift();
	}

	return {
		initialMessage: parts.length > 0 ? parts.join("") : undefined,
		initialImages: fileImages && fileImages.length > 0 ? fileImages : undefined,
	};
}
```

**讲解**

runtime 建好之后，`main()` 用 runtime 里的设置重新配置终端能力和 HTTP 代理（883–885 行）。这时用的是项目可信与否已经确定的 `settingsManager`。

`--help` 和 `--list-models` 也要等 runtime 建好才处理（890–904 行），因为扩展可以注册命令行参数和模型，帮助和模型列表要包含它们。

stdin 的处理：

- rpc 模式不读 stdin，stdin 留给命令。
- 其他模式下，stdin 是终端时 `readPipedStdin` 直接返回 `undefined`；否则读到 EOF，去掉首尾空白。
- 读到了内容而模式是 `interactive`，改成 `print`（910–911 行）。

`buildInitialMessage` 把 stdin 内容、`@file` 文本、第一条位置参数按这个顺序直接拼接（`join("")`，中间不加分隔符），并从 `parsed.messages` 里移除第一条。剩下的 `parsed.messages` 由 print 前端逐条发送（§4.10）。

诊断（927–935 行）：非交互模式总是打印；有 error 就退出，退出码 1。扩展加载失败时多打一行提示 `Start without extensions using "pi -ne"`。938 行：非交互模式下没有可用模型就退出。

**往哪走**：§4.9 分派到前端。

### 4.9 main() ④：分派到前端

**调用位置**：`main()` 结尾。

源码：[`packages/coding-agent/src/main.ts:949-1010`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L949-L1010)

```ts
	// RPC refreshes catalogs here in the background; interactive mode starts its refresh after TUI initialization.
	if (!offlineMode && appMode === "rpc") {
		const controller = new AbortController();
		const timeout = setTimeout(() => controller.abort(), 15_000);
		void modelRuntime
			.refresh({ signal: controller.signal })
			.catch(() => {})
			.finally(() => clearTimeout(timeout));
	}

	if (appMode === "rpc") {
		printTimings();
		await runRpcMode(runtime);
	} else if (appMode === "interactive") {
		const interactiveMode = new InteractiveMode(runtime, {
			migratedProviders,
			startupDiagnostics,
			modelFallbackMessage,
			autoTrustOnReloadCwd,
			initialMessage,
			initialImages,
			initialMessages: parsed.messages,
			verbose: parsed.verbose,
			tuiMode: parsed.tuiMode,
			initialThemeSetting: parsed.useTheme,
		});
		if (startupBenchmark) {
			await interactiveMode.init();
			time("interactiveMode.init");
			// Give the TUI's stdin handler a brief chance to consume terminal query replies
			// (Kitty keyboard protocol, device attributes, cell size) before restoring the terminal.
			await new Promise((resolve) => setTimeout(resolve, 150));
			interactiveMode.stop();
			stopThemeWatcher();
			printTimings();
			if (process.stdout.writableLength > 0) {
				await new Promise<void>((resolve) => process.stdout.once("drain", resolve));
			}
			if (process.stderr.writableLength > 0) {
				await new Promise<void>((resolve) => process.stderr.once("drain", resolve));
			}
			return;
		}

		printTimings();
		await interactiveMode.run();
	} else {
		printTimings();
		const exitCode = await runPrintMode(runtime, {
			mode: toPrintOutputMode(appMode),
			messages: parsed.messages,
			initialMessage,
			initialImages,
		});
		stopThemeWatcher();
		restoreStdout();
		if (exitCode !== 0) {
			process.exitCode = exitCode;
		}
		return;
	}
}
```

**讲解**

三个分支都把同一个 `runtime` 交出去，前端各自从 `runtime.session` 拿 session。分派前都调用 `printTimings()`，所以 `PI_TIMING` 的输出只覆盖启动阶段。

- rpc 模式先在后台刷新一次模型目录（950–957 行），再进入 `runRpcMode`。`runRpcMode` 返回 `Promise<never>`，进程由它自己退出（§4.12）。
- interactive 模式交给 `InteractiveMode`（不在范围）。`PI_STARTUP_BENCHMARK` 只在这里有效：初始化 TUI 后立即退出，用来测启动时间。
- 其余（print 和 json）交给 `runPrintMode`，拿到退出码后恢复 stdout，设置 `process.exitCode`，然后 `return`。`main()` 返回后进程没有别的活跃句柄，自然退出。

**往哪走**：§4.10 `runPrintMode()`。

### 4.10 runPrintMode()：text 和 json 输出

**调用位置**：`main()` 997 行。

第一段：清理函数和信号处理：

源码：[`packages/coding-agent/src/modes/print-mode.ts:29-68`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/print-mode.ts#L29-L68)

```ts
/**
 * Run in print (single-shot) mode.
 * Sends prompts to the agent and outputs the result.
 */
export async function runPrintMode(runtimeHost: AgentSessionRuntime, options: PrintModeOptions): Promise<number> {
	const { mode, messages = [], initialMessage, initialImages } = options;
	let exitCode = 0;
	let session = runtimeHost.session;
	let unsubscribe: (() => void) | undefined;
	let unsubscribeBackpressure: (() => void) | undefined;
	let disposed = false;
	const signalCleanupHandlers: Array<() => void> = [];

	const disposeRuntime = async (): Promise<void> => {
		if (disposed) return;
		disposed = true;
		unsubscribe?.();
		unsubscribeBackpressure?.();
		await runtimeHost.dispose();
	};

	const registerSignalHandlers = (): void => {
		const signals: NodeJS.Signals[] = ["SIGTERM"];
		if (process.platform !== "win32") {
			signals.push("SIGHUP");
		}

		for (const signal of signals) {
			const handler = () => {
				killTrackedDetachedChildren();
				void disposeRuntime().finally(() => {
					process.exit(signal === "SIGHUP" ? 129 : 143);
				});
			};
			process.on(signal, handler);
			signalCleanupHandlers.push(() => process.off(signal, handler));
		}
	};

	registerSignalHandlers();
```

第二段：绑定 session：

源码：[`packages/coding-agent/src/modes/print-mode.ts:70-119`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/print-mode.ts#L70-L119)

```ts
	runtimeHost.setRebindSession(async () => {
		await rebindSession();
	});

	const rebindSession = async (): Promise<void> => {
		session = runtimeHost.session;
		await session.bindExtensions({
			mode: mode === "json" ? "json" : "print",
			commandContextActions: {
				waitForIdle: () => session.waitForIdle(),
				newSession: async (newSessionOptions) => runtimeHost.newSession(newSessionOptions),
				fork: async (entryId, forkOptions) => {
					const result = await runtimeHost.fork(entryId, forkOptions);
					return { cancelled: result.cancelled };
				},
				navigateTree: async (targetId, navigateOptions) => {
					const result = await session.navigateTree(targetId, {
						summarize: navigateOptions?.summarize,
						customInstructions: navigateOptions?.customInstructions,
						replaceInstructions: navigateOptions?.replaceInstructions,
						label: navigateOptions?.label,
					});
					return { cancelled: result.cancelled };
				},
				switchSession: async (sessionPath, switchOptions) => {
					return runtimeHost.switchSession(sessionPath, switchOptions);
				},
				reload: async () => {
					await session.reload();
				},
			},
			onError: (err) => {
				console.error(`Extension error (${err.extensionPath}): ${err.error}`);
			},
		});

		unsubscribe?.();
		unsubscribeBackpressure?.();
		unsubscribe = session.subscribe((event) => {
			if (mode === "json") {
				writeRawStdout(`${JSON.stringify(toJsonEvent(event))}\n`);
			}
		});
		unsubscribeBackpressure =
			mode === "json"
				? session.agent.subscribe(async () => {
						await waitForRawStdoutBackpressure();
					})
				: undefined;
	};
```

第三段：发送提示词、输出结果、清理：

源码：[`packages/coding-agent/src/modes/print-mode.ts:121-173`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/print-mode.ts#L121-L173)

```ts
	try {
		if (mode === "json") {
			const header = session.sessionManager.getHeader();
			if (header) {
				writeRawStdout(`${JSON.stringify(header)}\n`);
			}
		}

		await rebindSession();

		if (initialMessage) {
			await session.prompt(initialMessage, { images: initialImages });
		}

		for (const message of messages) {
			await session.prompt(message);
		}

		if (mode === "text") {
			const state = session.state;
			const lastMessage = state.messages[state.messages.length - 1];

			if (lastMessage?.role === "assistant") {
				const assistantMsg = lastMessage as AssistantMessage;
				if (assistantMsg.stopReason === "error" || assistantMsg.stopReason === "aborted") {
					console.error(assistantMsg.errorMessage || `Request ${assistantMsg.stopReason}`);
					exitCode = 1;
				} else {
					for (const content of assistantMsg.content) {
						if (content.type === "text") {
							writeRawStdout(`${content.text}\n`);
						}
					}
					if (assistantMsg.stopReason === "length") {
						console.error("Response was truncated: the model reached its output token limit.");
						exitCode = 1;
					}
				}
			}
		}

		return exitCode;
	} catch (error: unknown) {
		console.error(error instanceof Error ? error.message : String(error));
		return 1;
	} finally {
		for (const cleanup of signalCleanupHandlers) {
			cleanup();
		}
		await disposeRuntime();
		await flushRawStdout();
	}
}
```

**讲解**

`runPrintMode` 是一个最小的前端，结构就是每个前端都要做的三件事：

1. **绑定扩展**（76 行）。`bindExtensions` 告诉扩展当前的 `mode`（`"print"` 或 `"json"`），并提供 `commandContextActions`：扩展命令可以调用的 `newSession`、`fork`、`switchSession` 等操作，它们都转发给 runtime。print 模式没有传 `uiContext`，所以扩展拿不到可交互的 UI（→ M7）。`bindExtensions` 内部会发出 `session_start` 扩展事件（`packages/coding-agent/src/core/agent-session.ts:3360`）。
2. **订阅事件**（108 行）。json 模式下每个 `AgentSessionEvent` 经过 `toJsonEvent` 写成一行。text 模式下订阅者什么都不做。
3. **发送输入**（131–137 行）。先 `prompt(initialMessage)`，再对剩下的 `messages` 逐条 `prompt`。每个 `await session.prompt()` 在 `agent_settled` 发出之后才返回（`_startRun` 的 `finally` 里 `await this._emitAgentSettled()`，`packages/coding-agent/src/core/agent-session.ts:1937`，→ M8），所以多条消息是串行的。

text 模式的输出只看 `session.state.messages` 的最后一条（139–160 行）：

- 是 assistant 消息且 `stopReason` 为 `error` 或 `aborted`：错误信息写 stderr，退出码 1。
- 否则输出它的每个文本块；`stopReason` 为 `length` 时再在 stderr 提示被截断，退出码 1。
- 中间轮的文本（例如主线场景第 1 轮的"我先读一下 README。"）不输出。

json 模式不检查最后一条消息，退出码总是 0（除非抛异常）。调用方要从事件流里自己判断成败。§7.1 用实验对比这两种行为。

两处细节：

- 第 113–118 行，json 模式在 `session.agent`（M2 的 `Agent`）上额外挂了一个 async 监听器，内容只有 `await waitForRawStdoutBackpressure()`。`AgentSession.subscribe` 的监听器是同步调用的，返回值不被等待（`packages/coding-agent/src/core/agent-session.ts:1055-1059`）；而 `Agent` 会 await 每个监听器（M2 §6.3）。这个监听器让 agent 循环在 stdout 写不完时停下来等，读取方处理得慢，pi 就跟着慢，内存里不会堆积事件。
- `finally` 里先 `disposeRuntime()` 再 `flushRawStdout()`。`dispose` 会给扩展发 `session_shutdown`，扩展在里面写的输出也能在退出前刷出去。

`rebindSession` 同时注册给了 runtime（70 行）。扩展命令在 print 模式里调用 `newSession()` 时，runtime 替换 session 后会回调它，订阅随之转到新 session。

**往哪走**：§4.11 `toJsonEvent()`。

### 4.11 toJsonEvent()：去掉累积快照

**调用位置**：`runPrintMode` 的订阅者（`print-mode.ts:110`）和 `runRpcMode` 的订阅者（`rpc-mode.ts:356`）。

源码：[`packages/coding-agent/src/modes/json-event.ts:20-61`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/json-event.ts#L20-L61)

```ts
function toJsonAssistantMessageEvent(
	event: MessageUpdateEvent["assistantMessageEvent"],
): JsonMessageUpdateEvent["assistantMessageEvent"] {
	if (event.type === "toolcall_start") {
		const toolCall = event.partial.content[event.contentIndex];
		if (toolCall?.type !== "toolCall") {
			throw new Error(`toolcall_start content at index ${event.contentIndex} is not a tool call`);
		}
		const { partial: _partial, ...deltaEvent } = event;
		return { ...deltaEvent, id: toolCall.id, toolName: toolCall.name };
	}

	if (!("partial" in event)) {
		return event;
	}

	const { partial: _partial, ...deltaEvent } = event;
	return deltaEvent;
}

/**
 * Remove cumulative assistant snapshots from streaming wire events.
 * `message_start` provides the initial message, deltas build it, and
 * `message_end` provides the final authoritative message. Cumulative usage,
 * tool-call ids, and tool names remain available because their size is constant.
 */
export function toJsonEvent(event: MessageUpdateEvent): JsonMessageUpdateEvent;
export function toJsonEvent(event: AgentSessionEvent): JsonAgentSessionEvent;
export function toJsonEvent(event: AgentSessionEvent): JsonAgentSessionEvent {
	if (event.type !== "message_update") {
		return event;
	}
	if (event.message.role !== "assistant") {
		throw new Error("message_update message is not an assistant message");
	}

	return {
		type: "message_update",
		usage: event.message.usage,
		assistantMessageEvent: toJsonAssistantMessageEvent(event.assistantMessageEvent),
	};
}
```

**讲解**

M2 的 `message_update` 事件带两份累积状态：`event.message` 是到目前为止的整条 assistant 消息，`assistantMessageEvent.partial`（M1 的流式事件）也是。对进程内的订阅者，这只是同一个对象的引用，没有代价。序列化成 JSON 时，每个增量事件都要把整条消息再写一遍，输出量随回复长度平方增长。

`toJsonEvent` 只对 `message_update` 做转换：

- 去掉 `message`，换成 `message.usage`（大小固定）。
- 去掉 `assistantMessageEvent.partial`。
- `toolcall_start` 时，工具调用的 `id` 和 `name` 只在 partial 里有，所以先从 `partial.content[contentIndex]` 取出来，作为 `id`、`toolName` 字段补上。

客户端用 `message_start` 拿到初始消息，用增量拼出过程，用 `message_end` 拿到最终的完整消息（注释 41–44 行）。§2.2 的输出就是转换后的样子。

**往哪走**：§4.12 `runRpcMode()`。

### 4.12 runRpcMode()：stdin 上的命令，stdout 上的事件和响应

**调用位置**：`main()` 961 行。

入口和三个输出辅助函数：

源码：[`packages/coding-agent/src/modes/rpc/rpc-mode.ts:54-88`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L54-L88)

```ts
export async function runRpcMode(runtimeHost: AgentSessionRuntime): Promise<never> {
	takeOverStdout();
	let session = runtimeHost.session;
	let unsubscribe: (() => void) | undefined;
	let unsubscribeBackpressure: (() => void) | undefined;

	const output = (obj: RpcResponse | RpcExtensionUIRequest | object) => {
		writeRawStdout(serializeJsonLine(obj));
	};

	const success = <T extends RpcCommand["type"]>(
		id: string | undefined,
		command: T,
		data?: object | null,
	): RpcResponse => {
		if (data === undefined) {
			return { id, type: "response", command, success: true } as RpcResponse;
		}
		return { id, type: "response", command, success: true, data } as RpcResponse;
	};

	const error = (id: string | undefined, command: string, message: string): RpcResponse => {
		return { id, type: "response", command, success: false, error: message };
	};

	// Pending extension UI requests waiting for response
	const pendingExtensionRequests = new Map<
		string,
		{ resolve: (value: any) => void; reject: (error: Error) => void }
	>();

	// Shutdown request flag
	let shutdownRequested = false;
	let shuttingDown = false;
	const signalCleanupHandlers: Array<() => void> = [];
```

绑定 session（和 print 模式的 `rebindSession` 结构相同）：

源码：[`packages/coding-agent/src/modes/rpc/rpc-mode.ts:313-364`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L313-L364)

```ts
	runtimeHost.setRebindSession(async () => {
		await rebindSession();
	});

	const rebindSession = async (): Promise<void> => {
		session = runtimeHost.session;
		await session.bindExtensions({
			uiContext: createExtensionUIContext(),
			mode: "rpc",
			commandContextActions: {
				waitForIdle: () => session.waitForIdle(),
				newSession: async (options) => runtimeHost.newSession(options),
				fork: async (entryId, forkOptions) => {
					const result = await runtimeHost.fork(entryId, forkOptions);
					return { cancelled: result.cancelled };
				},
				navigateTree: async (targetId, options) => {
					const result = await session.navigateTree(targetId, {
						summarize: options?.summarize,
						customInstructions: options?.customInstructions,
						replaceInstructions: options?.replaceInstructions,
						label: options?.label,
					});
					return { cancelled: result.cancelled };
				},
				switchSession: async (sessionPath, options) => {
					return runtimeHost.switchSession(sessionPath, options);
				},
				reload: async () => {
					await session.reload();
				},
			},
			shutdownHandler: () => {
				shutdownRequested = true;
			},
			onError: (err) => {
				output({ type: "extension_error", extensionPath: err.extensionPath, event: err.event, error: err.error });
			},
		});

		unsubscribe?.();
		unsubscribeBackpressure?.();
		unsubscribe = session.subscribe((event) => {
			output(toJsonEvent(event));
			if (event.type === "agent_settled") {
				void checkShutdownRequested();
			}
		});
		unsubscribeBackpressure = session.agent.subscribe(async () => {
			await waitForRawStdoutBackpressure();
		});
	};
```

命令分派，前几个命令：

源码：[`packages/coding-agent/src/modes/rpc/rpc-mode.ts:385-442`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L385-L442)

```ts
	// Handle a single command
	const handleCommand = async (command: RpcCommand): Promise<RpcResponse | undefined> => {
		const id = command.id;

		switch (command.type) {
			// =================================================================
			// Prompting
			// =================================================================

			case "prompt": {
				// Start prompt handling immediately, but emit the authoritative response only after
				// prompt preflight succeeds. Queued and immediately handled prompts also count as success.
				let preflightSucceeded = false;
				void session
					.prompt(command.message, {
						images: command.images,
						streamingBehavior: command.streamingBehavior,
						source: "rpc",
						preflightResult: (disposition) => {
							preflightSucceeded = true;
							output(success(id, "prompt", { disposition }));
						},
					})
					.catch((e) => {
						if (!preflightSucceeded) {
							output(error(id, "prompt", e.message));
						}
					});
				return undefined;
			}

			case "steer": {
				const disposition = await session.steer(command.message, command.images, { source: "rpc" });
				return success(id, "steer", { disposition });
			}

			case "follow_up": {
				const disposition = await session.followUp(command.message, command.images, { source: "rpc" });
				return success(id, "follow_up", { disposition });
			}

			case "abort": {
				await session.abort();
				return success(id, "abort");
			}

			case "clear_queue": {
				return success(id, "clear_queue", session.clearQueue());
			}

			case "new_session": {
				const options = command.parentSession ? { parentSession: command.parentSession } : undefined;
				const result = await runtimeHost.newSession(options);
				if (!result.cancelled) {
					await rebindSession();
				}
				return success(id, "new_session", result);
			}
```

关闭：

源码：[`packages/coding-agent/src/modes/rpc/rpc-mode.ts:720-748`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L720-L748)

```ts
	/**
	 * Check if shutdown was requested and perform shutdown if so.
	 * Called after handling each command when waiting for the next command.
	 */
	let detachInput = () => {};

	async function shutdown(exitCode = 0, signal?: NodeJS.Signals): Promise<never> {
		if (shuttingDown) {
			process.exit(exitCode);
		}
		shuttingDown = true;
		for (const cleanup of signalCleanupHandlers) {
			cleanup();
		}
		unsubscribe?.();
		unsubscribeBackpressure?.();
		await runtimeHost.dispose();
		detachInput();
		process.stdin.pause();
		if (signal !== "SIGTERM") {
			await flushRawStdout();
		}
		process.exit(exitCode);
	}

	async function checkShutdownRequested(): Promise<void> {
		if (!shutdownRequested) return;
		await shutdown();
	}
```

输入处理：

源码：[`packages/coding-agent/src/modes/rpc/rpc-mode.ts:750-800`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L750-L800)

```ts
	const handleInputLine = async (line: string) => {
		let parsed: unknown;
		try {
			parsed = JSON.parse(line);
		} catch (parseError: unknown) {
			output(
				error(
					undefined,
					"parse",
					`Failed to parse command: ${parseError instanceof Error ? parseError.message : String(parseError)}`,
				),
			);
			await waitForRawStdoutBackpressure();
			return;
		}

		// Handle extension UI responses
		if (
			typeof parsed === "object" &&
			parsed !== null &&
			"type" in parsed &&
			parsed.type === "extension_ui_response"
		) {
			const response = parsed as RpcExtensionUIResponse;
			const pending = pendingExtensionRequests.get(response.id);
			if (pending) {
				pendingExtensionRequests.delete(response.id);
				pending.resolve(response);
			}
			return;
		}

		const command = parsed as RpcCommand;
		try {
			const response = await handleCommand(command);
			if (response) {
				output(response);
				await waitForRawStdoutBackpressure();
			}
			await checkShutdownRequested();
		} catch (commandError: unknown) {
			output(
				error(
					command.id,
					command.type,
					commandError instanceof Error ? commandError.message : String(commandError),
				),
			);
			await waitForRawStdoutBackpressure();
		}
	};
```

源码：[`packages/coding-agent/src/modes/rpc/rpc-mode.ts:802-819`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L802-L819)

```ts
	const onInputEnd = () => {
		void shutdown();
	};
	process.stdin.on("end", onInputEnd);

	detachInput = (() => {
		const detachJsonl = attachJsonlLineReader(process.stdin, (line) => {
			void handleInputLine(line);
		});
		return () => {
			detachJsonl();
			process.stdin.off("end", onInputEnd);
		};
	})();

	// Keep process alive forever
	return new Promise(() => {});
}
```

**讲解**

rpc 前端和 print 前端做的是同样三件事，只是输入来自 stdin 上的命令，并且进程常驻：

1. **绑定扩展**（319 行）。和 print 模式相比多了两项：`uiContext` 和 `shutdownHandler`。`uiContext` 由 `createExtensionUIContext()`（136–311 行）构造：`select`、`confirm`、`input`、`editor` 输出一行 `extension_ui_request`，把 Promise 存进 `pendingExtensionRequests`，等客户端发回同 `id` 的 `extension_ui_response`（766–780 行）；`notify`、`setStatus`、`setTitle` 等只输出不等待；需要 TUI 组件的方法（`setFooter`、`custom` 等）是空实现。这样同一个扩展在 TUI 里弹窗，在 rpc 里变成协议消息，扩展代码不用改（→ M7）。
2. **订阅事件**（355 行）。和 json 模式一样经过 `toJsonEvent`，也同样在 `session.agent` 上挂了背压监听器（361 行）。多了一步：收到 `agent_settled` 时检查有没有挂起的关闭请求。
3. **处理命令**（386 行 `handleCommand`）。大部分命令是一行转发：`steer` → `session.steer()`，`abort` → `session.abort()`，`get_state` → 读 session 的若干属性，`new_session`/`switch_session`/`fork` → runtime 的同名方法，成功后 `rebindSession()`。

`prompt` 命令和别的命令不同（394–414 行）：

- `session.prompt()` 不被 await（`void session.prompt(...)`），`handleCommand` 返回 `undefined`，所以 `handleInputLine` 不输出响应，马上可以处理下一行。否则一次运行期间 rpc 就收不了 `steer`、`abort` 这些命令了。
- 响应由 `preflightResult` 回调发出。M8 的 `AgentSession.prompt()` 检查完模型和 API key 后调用 `_startRun`，并把 `preflightResult("started")` 作为 `onStarted` 传进去（`packages/coding-agent/src/core/agent-session.ts:2192-2194`）；`_startRun` 在 `_prepareRun` 完成之后、`agent.prompt()` 之前调用它（`packages/coding-agent/src/core/agent-session.ts:1911`）。这时 `disposition` 为 `"started"`；消息被排队或被扩展命令处理时分别为 `"queued"`、`"handled"`。§2.3 里 `prompt` 的响应排在 `agent_start` 之前，就是这个原因。
- preflight 之前就失败（例如没有 API key），才输出错误响应。preflight 之后的失败会以事件形式出现（错误的 assistant 消息），不再有第二个响应。

`handleInputLine`（750 行）：JSON 解析失败输出 `command: "parse"` 的错误响应；`extension_ui_response` 交给挂起的 Promise；其余当作命令。每行的处理是 async 的，`attachJsonlLineReader` 的回调里用 `void handleInputLine(line)` 调用，不等上一行处理完。所以响应顺序可能和输入顺序不同，客户端要用 `id` 配对（§7.3）。

关闭有三条路径，都走 `shutdown()`（726 行）：

| 触发 | 位置 | 退出码 |
|---|---|---|
| stdin 结束（客户端关闭管道） | 802–805 行 | 0 |
| 扩展调用 `ctx.shutdown()` | `shutdownHandler` 只设标志（345 行），在下一个 `agent_settled`（357 行）或下一条命令处理完（789 行）时执行 | 0 |
| SIGTERM / SIGHUP | 366–380 行 | 143 / 129 |

`shutdown` 退订、`runtimeHost.dispose()`、停止读 stdin、刷出 stdout，然后 `process.exit`。函数最后返回一个永不 resolve 的 Promise（818 行），所以 `main()` 里的 `await runRpcMode(runtime)` 永远不会返回。

仓库里的 `RpcClient`（`packages/coding-agent/src/modes/rpc/rpc-client.ts`）是这个协议的 TypeScript 客户端：用 `spawn` 启动 `node dist/cli.js --mode rpc`，自动生成 `id`，把每个命令包成一个返回 Promise 的方法（`prompt`、`steer`、`getState` …），另有 `promptAndWait` 收集事件直到运行结束。本文的 `m9-rpc.mjs` 没有用它，直接写原始 JSON 行，以便看清协议本身。

**往哪走**：§4.13 JSONL 的分行规则。

### 4.13 attachJsonlLineReader()：只按 \n 分行

**调用位置**：`runRpcMode()` 808 行；`RpcClient` 读子进程 stdout 时也用它。

源码：[`packages/coding-agent/src/modes/rpc/jsonl.ts:1-58`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/modes/rpc/jsonl.ts#L1-L58)

```ts
import type { Readable } from "node:stream";
import { StringDecoder } from "node:string_decoder";

/**
 * Serialize a single strict JSONL record.
 *
 * Framing is LF-only. Payload strings may contain other Unicode separators such as
 * U+2028 and U+2029. Clients must split records on `\n` only.
 */
export function serializeJsonLine(value: unknown): string {
	return `${JSON.stringify(value)}\n`;
}

/**
 * Attach an LF-only JSONL reader to a stream.
 *
 * This intentionally does not use Node readline. Readline splits on additional
 * Unicode separators that are valid inside JSON strings and therefore does not
 * implement strict JSONL framing.
 */
export function attachJsonlLineReader(stream: Readable, onLine: (line: string) => void): () => void {
	const decoder = new StringDecoder("utf8");
	let buffer = "";

	const emitLine = (line: string) => {
		onLine(line.endsWith("\r") ? line.slice(0, -1) : line);
	};

	const onData = (chunk: string | Buffer) => {
		buffer += typeof chunk === "string" ? chunk : decoder.write(chunk);

		while (true) {
			const newlineIndex = buffer.indexOf("\n");
			if (newlineIndex === -1) {
				return;
			}

			emitLine(buffer.slice(0, newlineIndex));
			buffer = buffer.slice(newlineIndex + 1);
		}
	};

	const onEnd = () => {
		buffer += decoder.end();
		if (buffer.length > 0) {
			emitLine(buffer);
			buffer = "";
		}
	};

	stream.on("data", onData);
	stream.on("end", onEnd);

	return () => {
		stream.off("data", onData);
		stream.off("end", onEnd);
	};
}
```

**讲解**

注释说明了不用 Node `readline` 的原因：`readline` 除了 `\n` 还会在 U+2028、U+2029 等字符处分行。`JSON.stringify` 不会转义这两个字符，它们可以原样出现在 JSON 字符串里，用 `readline` 读就会把一条记录切成两半。这里只认 `\n`，行尾的 `\r` 去掉，以兼容 CRLF。`StringDecoder` 保证一个多字节 UTF-8 字符被拆在两个 chunk 里时也能正确拼回。

**往哪走**：§4.14 SDK。

### 4.14 SDK：createAgentSession() 和 examples/sdk

**调用位置**：用户自己的程序。

最小示例：

源码：[`packages/coding-agent/examples/sdk/01-minimal.ts:1-26`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/examples/sdk/01-minimal.ts#L1-L26)

```ts
/**
 * Minimal SDK Usage
 *
 * Uses all defaults: discovers skills, extensions, tools, context files
 * from cwd and ~/.pi/agent. Model chosen from settings or first available.
 */

import { createAgentSession } from "@earendil-works/pi-coding-agent";

const { session } = await createAgentSession();

try {
	session.subscribe((event) => {
		if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
			process.stdout.write(event.assistantMessageEvent.delta);
		}
	});

	await session.prompt("What files are in the current directory?");
	session.state.messages.forEach((msg) => {
		console.log(msg);
	});
	console.log();
} finally {
	session.dispose();
}
```

需要替换会话时用 runtime，模式和 CLI 一样：

源码：[`packages/coding-agent/examples/sdk/13-session-runtime.ts:20-67`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/examples/sdk/13-session-runtime.ts#L20-L67)

```ts
const createRuntime: CreateAgentSessionRuntimeFactory = async ({ cwd, sessionManager, sessionStartEvent }) => {
	const services = await createAgentSessionServices({ cwd });
	return {
		...(await createAgentSessionFromServices({
			services,
			sessionManager,
			sessionStartEvent,
		})),
		services,
		diagnostics: services.diagnostics,
	};
};
const runtime = await createAgentSessionRuntime(createRuntime, {
	cwd: process.cwd(),
	agentDir: getAgentDir(),
	sessionManager: SessionManager.create(process.cwd()),
});

let unsubscribe: (() => void) | undefined;

async function bindSession() {
	unsubscribe?.();
	const session = runtime.session;
	await session.bindExtensions({});
	unsubscribe = session.subscribe((event) => {
		if (event.type === "queue_update") {
			console.log("Queued:", event.steering.length + event.followUp.length);
		}
	});
	return session;
}

let session = await bindSession();
const originalSessionFile = session.sessionFile;
console.log("Initial session:", originalSessionFile);

await runtime.newSession();
session = await bindSession();
console.log("After newSession():", session.sessionFile);

if (originalSessionFile) {
	await runtime.switchSession(originalSessionFile);
	session = await bindSession();
	console.log("After switchSession():", session.sessionFile);
}

unsubscribe?.();
await runtime.dispose();
```

**讲解**

SDK 有两个层次：

- `createAgentSession()`（`packages/coding-agent/src/core/sdk.ts:191`，→ M8）：一步建好 `AgentSession`。不传参数时，它自己建 `ModelRuntime`、`SettingsManager`、`SessionManager`、`DefaultResourceLoader`，用 `~/.pi/agent`（或 `PI_CODING_AGENT_DIR`）下的配置。01-minimal 就是这样用的：订阅、`prompt`、读 `state.messages`、`dispose`。
- `createAgentSessionServices` + `createAgentSessionFromServices` + `createAgentSessionRuntime`：和 `main()` 完全相同的三步。13-session-runtime 演示了前端要做的 rebind：每次 `newSession()`/`switchSession()` 之后重新 `bindExtensions` 和 `subscribe`。

和 CLI 相比，SDK 少了参数解析、模式判断、stdout 接管，其余一样。`examples/sdk/` 的另外 12 个示例分别演示自定义模型、系统提示词、skills、工具、扩展、设置、会话等选项，它们都是 `createAgentSession()` 的参数（→ M6 / M7 / M8）。

§2.4 的 `m9-sdk.ts sdk` 场景是 01-minimal 加上假模型和内存会话。`m9-sdk.ts print` 场景直接调用导出的 `runPrintMode`，§7.2 用它验证"前端只是订阅者"。

**往哪走**：§4.15 计时。

### 4.15 timings.ts：PI_TIMING=1

**调用位置**：`main()` 里的 `resetTimings()`（575 行）、多处 `time("…")`、分派前的 `printTimings()`；扩展加载器里的 `time(…, "extensions")`（`packages/coding-agent/src/core/extensions/loader.ts:633`、`648`）；`DefaultResourceLoader.reload()` 里的 `resetTimings("extensions")`（`packages/coding-agent/src/core/resource-loader.ts:542`）。

源码：[`packages/coding-agent/src/core/timings.ts:1-50`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/timings.ts#L1-L50)

```ts
/**
 * Central timing instrumentation for startup profiling.
 * Enable with PI_TIMING=1 environment variable.
 */

const ENABLED = process.env.PI_TIMING === "1";
interface TimingNamespace {
	timings: Array<{ label: string; ms: number }>;
	lastTime: number;
}

type TimingLabel = "main" | "extensions";

const timingNamespaces = new Map<TimingLabel, TimingNamespace>();

export function resetTimings(namespace: TimingLabel = "main"): void {
	if (!ENABLED) return;
	timingNamespaces.set(namespace, { timings: [], lastTime: Date.now() });
}

export function time(label: string, namespace: TimingLabel = "main"): void {
	if (!ENABLED) return;
	const now = Date.now();

	if (!timingNamespaces.has(namespace)) {
		resetTimings(namespace);
	}

	const timingNamespace = timingNamespaces.get(namespace)!;
	timingNamespace.timings.push({ label, ms: now - timingNamespace.lastTime });
	timingNamespace.lastTime = now;
}

function printTimingGroup(title: string, timings: TimingNamespace["timings"]): void {
	const printableTimings = timings.filter((timing) => timing.ms >= 0);
	if (printableTimings.length === 0) return;
	console.error(`\n--- ${title} ---`);
	for (const t of printableTimings) {
		console.error(`  ${t.label}: ${t.ms}ms`);
	}
	console.error(`  TOTAL: ${printableTimings.reduce((a, b) => a + b.ms, 0)}ms`);
	console.error(`${"-".repeat(title.length + 8)}\n`);
}

export function printTimings(): void {
	if (!ENABLED) return;
	for (const [namespace, timingNamespace] of timingNamespaces) {
		printTimingGroup(`Startup Timings: ${namespace}`, timingNamespace.timings);
	}
}
```

**讲解**

`ENABLED` 在模块加载时读一次环境变量，之后 `time()` 和 `printTimings()` 在未启用时直接返回，没有开销。计时分两个命名空间：`main` 记录 `main()` 的启动阶段，`extensions` 记录每个扩展的模块导入和工厂函数执行。每次 `time(label)` 记的是距上一次打点的毫秒数，不是累计值。

对照 §2.1 的输出读 `main()` 的打点位置，可以看到标签的含义：

| 标签 | 打点行 | 这一段测的是 |
|---|---|---|
| `parseArgs` | 644 | `main()` 开头到参数解析完，包括子命令检查 |
| `runMigrations` | 681 | 配置迁移 |
| `createSessionManager` | 729 | 读设置、算会话目录、建 `SessionManager` |
| `createRuntime` | 874 | 只是定义闭包，接近 0ms |
| `createAgentSessionRuntime` | 880 | 调用工厂：建服务、加载扩展、建 session。启动耗时的主体 |
| `readPipedStdin` | 914 | 读 stdin |
| `prepareInitialMessage` | 917 | 组装首条提示词，含读取 `@file` |
| `initTheme` | 919 | 加载主题 |
| `resolveModelScope` | 926 | 只覆盖弃用警告（921–924 行，仅交互模式） |
| `createAgentSession` | 936 | 打印诊断 |

最后两个标签和它们实际测量的代码对不上。模型范围解析和 session 创建都已经移进了 `createRuntime` 闭包（822–862 行），算在 `createAgentSessionRuntime` 里。读 `PI_TIMING` 输出时，按打点行理解，不要按标签名理解。

**往哪走**：§4 读完。状态机见 §5。

---

## 5. 状态机

### 5.1 运行模式的判定

```
                        parseArgs (main.ts:634)
                               │
          ┌────────────────────┼─────────────────────────┐
  mode=rpc│ (113)      mode=json│ (116)                    │ 其余
          ▼                    ▼                          ▼
      ┌──────┐            ┌──────┐     -p 或 stdin/stdout 不是终端 (119)
      │ rpc  │            │ json │          ┌──────────┴──────────┐
      └──────┘            └──────┘          ▼ 是                   ▼ 否
     不读 stdin (908)                   ┌───────┐           ┌─────────────┐
     不许 @file (671)                   │ print │◀──────────│ interactive │
                                       └───────┘  stdin 有  └─────────────┘
                                                  内容 (910-911)
   接管 stdout：除 interactive 和 --help/--list-models 外都接管 (666-669)
   前端：rpc → runRpcMode (961)；interactive → InteractiveMode (963)；
         print/json → runPrintMode (997)，toPrintOutputMode (125-127) 决定 text/json
```

### 5.2 会话来源

```
 createSessionManager (main.ts:358)，按顺序检查，第一个命中即返回
   noSession / help / listModels ──▶ inMemory()            (364-366)
   fork ──resolveSessionPath──┬─ path/local/global ──▶ forkFrom()      (377-383)
                              └─ not_found ──▶ exit 1      (385-387)
   session ──resolveSessionPath──┬─ path/local ──▶ open()  (394-397)
                                 ├─ global ──确认──▶ forkFrom() / exit 0 (399-406)
                                 └─ not_found ──▶ exit 1   (409-411)
   resume ──选择器──▶ open() / 未选择 exit 0                 (415-430)
   continue ──▶ continueRecent()（最近的会话，没有则新建）     (432-434)
   sessionId ──已存在──▶ open()；不存在 ──警告──▶ 落到下一行   (436-446)
   默认 ──▶ create()                                       (448)
```

### 5.3 前端的生命周期

print 前端：

```
 runPrintMode (print-mode.ts:33)
   注册 SIGTERM/SIGHUP (68) ── setRebindSession (70)
   json 模式：写会话头 (122-127)
   rebindSession：bindExtensions (76) → subscribe (108) [→ agent 背压监听 (113-118)]
        │
        ▼
   prompt(initialMessage) (132) → prompt(messages[i]) … (135-137)   每次 await 到 agent_settled
        │
        ▼
   text 模式：看最后一条 assistant 消息 (139-160) → 输出 / exitCode=1
        │
        ▼ finally
   移除信号处理 (167-169) → runtime.dispose() (170) → flushRawStdout (171) → 返回 exitCode
   收到信号：kill 子进程 → dispose → exit 143/129 (57-61)
```

rpc 前端：

```
 runRpcMode (rpc-mode.ts:54)
   takeOverStdout (55) → setRebindSession (313) → rebindSession (382) → 注册信号 (383)
   attachJsonlLineReader (808) ── 返回永不 resolve 的 Promise (818)
        │
        ▼
 ┌─────────────────────── serving ───────────────────────┐
 │ 每行 → handleInputLine (750)，不等上一行                 │
 │   解析失败 → parse 错误响应 (755-763)                    │
 │   extension_ui_response → resolve 挂起的请求 (767-780)   │
 │   命令 → handleCommand (784)                           │
 │     prompt：void session.prompt，preflight 时发响应 (398-413) │
 │     new_session/switch_session/fork/clone 成功 → rebindSession │
 │ 事件 → toJsonEvent → stdout (355-356)                   │
 └──┬──────────────────────┬──────────────────────┬──────┘
    │ stdin end (802-805)  │ shutdownRequested 且   │ SIGTERM/SIGHUP (373-375)
    │                      │ agent_settled (357) 或  │
    │                      │ 命令处理完 (789)        │
    ▼                      ▼                        ▼
 shutdown (726)：退订 (734-735) → dispose (736) → 停读 stdin (737-738)
                 → 非 SIGTERM 时 flush (739-741) → process.exit (742)
```

---

## 6. 设计要点

### 6.1 所有前端共用一个 AgentSession

**做法。** 四种前端拿到的都是 `AgentSessionRuntime`，通过 `runtime.session` 使用同一个 `AgentSession` 类。每个前端只做三件事：

| | print / json | rpc | interactive | SDK 用户代码 |
|---|---|---|---|---|
| 绑定扩展 | `print-mode.ts:76`，`mode: "print"`/`"json"`，无 UI | `rpc-mode.ts:319`，`mode: "rpc"`，UI 走协议 | `interactive-mode.ts:2074`，`mode: "tui"`，UI 是 TUI 组件 | 可选，`13-session-runtime.ts:43` |
| 订阅事件 | `print-mode.ts:108` | `rpc-mode.ts:355` | `interactive-mode.ts:3462` | `01-minimal.ts:13` |
| 送入输入 | `print-mode.ts:132`、`136` | `rpc-mode.ts:398-399` 等 | `interactive-mode.ts:1258`、`3430` 等 | `01-minimal.ts:19` |

（`interactive-mode.ts` 指 `packages/coding-agent/src/modes/interactive/interactive-mode.ts`，只列行号，不展开。）

**理由。** agent 的行为只有一份实现。同一个提示词在 TUI、`-p`、rpc、SDK 里会产生同样的 Agent 消息、同样的会话条目、同样的事件序列：§2.2、§2.3、§2.4 的事件就是例子。扩展也只写一次，靠 `mode` 和 `ctx.hasUI` 适配有无界面。新增一种前端（例如 IDE 插件）只需要实现这三件事，或者直接用 rpc 协议。

**代码位置。** `packages/coding-agent/src/main.ts:959-1009`（分派），`packages/coding-agent/src/core/agent-session.ts:1404`（`subscribe`）、`3339`（`bindExtensions`）。

### 6.2 工厂闭包：进程级输入和 cwd 级服务分开

**做法。** `main()` 不直接建 session，而是定义 `createRuntime` 闭包交给 runtime 保存。闭包捕获 CLI 参数，按传入的 cwd 重建服务。runtime 替换会话时总是"销毁旧的 → 调用工厂建新的 → 通知前端 rebind"。

**理由。** 会话可以属于别的项目目录（`/resume` 一个其他项目的会话）。项目设置、项目扩展、AGENTS.md、项目信任都和 cwd 绑定，换 cwd 就必须重建，在旧 session 上修补很容易漏掉某个缓存。整体替换让每个 session 都是用同一套代码从头建出来的，首次启动和中途切换走同一条路径。代价是前端必须处理 rebind，`setRebindSession` 把这个要求变成了显式接口。

**代码位置。** `packages/coding-agent/src/main.ts:744`，`packages/coding-agent/src/core/agent-session-runtime.ts:167-194`、`420`。

### 6.3 stdout 只留给协议

**做法。** 非交互模式启动时就接管 `process.stdout.write`，改写到 stderr。协议输出走 `writeRawStdout`，按顺序排队写。

**理由。** json 和 rpc 的消费方逐行 `JSON.parse`，混进一行非 JSON 就会出错。pi 不能控制扩展和第三方库会不会 `console.log`，所以不靠约定，而是在进程层面把 stdout 收回来。text 模式同样受益：`pi -p "…" > answer.txt` 得到的文件里只有答案。§2.3 里 `m9-rpc.mjs` 要特殊处理的那一行非 JSON 输出来自 `pi-test.sh` 的 `echo`，它在 Node 进程启动之前，不受这个机制管。

**代码位置。** `packages/coding-agent/src/main.ts:666-669`，`packages/coding-agent/src/core/output-guard.ts:45`、`85`。

### 6.4 背压：借用 Agent 的 await 监听器

**做法。** json 和 rpc 前端除了在 `AgentSession` 上订阅（同步，用于输出），还在 `session.agent` 上挂一个只做 `await waitForRawStdoutBackpressure()` 的 async 监听器。

**理由。** `AgentSession` 的监听器不被 await，单靠它，agent 循环会一直往前跑，事件在 `writeRawStdout` 的 Promise 链里越积越多。M2 的 `Agent` 会 await 每个监听器（M2 §6.3），在那一层挂一个等待写完的监听器，循环每发一个事件就等 stdout 跟上。这样不需要在 `AgentSession` 里加任何流控代码。

**代码位置。** `packages/coding-agent/src/modes/print-mode.ts:113-118`，`packages/coding-agent/src/modes/rpc/rpc-mode.ts:361-363`，`packages/coding-agent/src/core/output-guard.ts:95`。

### 6.5 rpc 的 prompt：先确认受理，再异步运行

**做法。** `prompt` 命令不 await 运行结束，在 preflight 通过时输出响应，之后的进展全部以事件表达。命令行之间也不串行，靠 `id` 配对。

**理由。** 一次运行可能持续几分钟，期间客户端需要发 `steer`、`abort`、`get_state`。如果 `prompt` 要等运行结束才响应，命令通道就被占住了。preflight 响应告诉客户端"已受理、是立即开始还是排队"，失败（没有模型、没有 key）能同步报告。之后的成败已经在事件流里（错误的 assistant 消息、`agent_settled`），不需要第二个响应。

**代码位置。** `packages/coding-agent/src/modes/rpc/rpc-mode.ts:394-414`、`807-810`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行。

### 7.1 两条提示词：text 和 json 的退出码

**问题。** `faux-ext.ts` 只准备了 2 次回复，主线场景正好用完。如果在命令行上给两条提示词 `"读 README.md，总结成三句话" "再说一遍"`，第二条提示词的请求拿不到回复。text 模式和 json 模式分别输出什么？退出码是多少？

**预测。**

- `buildInitialMessage` 把第一条作为 `initialMessage`，第二条留在 `messages` 里，`runPrintMode` 依次 `prompt` 两次（§4.10）。
- 第二次运行时 faux provider 没有回复可给，返回 `stopReason: "error"` 的 assistant 消息（失败是数据，M2 §6.5）。
- text 模式：最后一条消息是错误，第一条提示词的总结不会输出；错误信息写 stderr；退出码 1（`print-mode.ts:145-147`）。
- json 模式：事件流里有两组 `agent_start … agent_settled`；退出码 0，因为 json 模式不检查最后一条消息。

**运行。**

```bash
WORK=/tmp/pi-lab/m9-work-7 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-7 ./run.sh -p "读 README.md，总结成三句话" "再说一遍"; echo "exit=$?"
```

```
Running without API keys...
No more faux responses queued
exit=1
```

```bash
WORK=/tmp/pi-lab/m9-work-7 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-7b ./run.sh --mode json "读 README.md，总结成三句话" "再说一遍" > /tmp/pi-lab/m9-events-7.jsonl; echo "exit=$?"
node summarize-events.mjs /tmp/pi-lab/m9-events-7.jsonl | grep -v message_update
grep -o '"stopReason":"error"[^}]*' /tmp/pi-lab/m9-events-7.jsonl | head -2
```

```
exit=0
session
agent_start
turn_start
message_start (system)
message_end (system)
message_start (user)
message_end (user)
message_start (assistant)
message_end (assistant)
tool_execution_start read
tool_execution_end read
message_start (toolResult)
message_end (toolResult)
turn_end (assistant)
turn_start
message_start (assistant)
message_end (assistant)
turn_end (assistant)
agent_end
agent_settled
agent_start
turn_start
message_start (user)
message_end (user)
message_start (assistant)
message_end (assistant)
turn_end (assistant)
agent_end
agent_settled
共 41 行（含第一行 session 头）
```

```
"stopReason":"error","errorMessage":"No more faux responses queued","timestamp":1793516072041,"durationMs":0,"thinkingLevel":"off"
"stopReason":"error","errorMessage":"No more faux responses queued","timestamp":1793516072041,"durationMs":0,"thinkingLevel":"off"
```

（`grep -v message_update` 去掉了流式增量行，便于看结构；`grep -o` 的两行分别来自 `message_end` 和 `turn_end` 里的同一条消息。）

**结论。** 预测都成立。text 模式的 stdout 上什么都没有（`Running without API keys...` 来自 `pi-test.sh`），只有 stderr 的错误和退出码 1。json 模式退出码 0，错误只能从事件里的 `stopReason` 看出来。另外，第二次运行没有 `message_start (system)`：系统提示词没有变化，M8 不再追加 system 消息（→ M8 `_prepareRun`）。

### 7.2 管道输入和 --no-session

**问题。** 不加 `-p`，把提示词从管道送进去，并加上 `--no-session`。会走哪个模式？会话文件会写到哪里？

**预测。**

- 管道让 stdin 不是终端，`resolveAppMode` 返回 `print`（119 行）。即使在真实终端里运行（stdout 是终端），结果也一样。
- `readPipedStdin` 读到提示词，`buildInitialMessage` 把它作为 `initialMessage`（`parsed.messages` 为空）。输出和主线场景相同。
- `--no-session` 命中 `createSessionManager` 的第一个分支，得到内存会话。`PI_CODING_AGENT_DIR` 下不会出现 `sessions` 目录。
- `PI_TIMING` 里 `readPipedStdin` 一项仍然很小，因为 stdin 很快就结束了。

**运行。**

```bash
printf '读 README.md，总结成三句话' | WORK=/tmp/pi-lab/m9-work-6 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-6 PI_TIMING=1 ./run.sh --no-session; echo "exit=$?"
ls /tmp/pi-lab/m9-agent-6
ls /tmp/pi-lab/m9-agent-6/sessions
```

```
Running without API keys...
--- Startup Timings: main ---
  parseArgs: 1ms
  runMigrations: 0ms
  createSessionManager: 1ms
  createRuntime: 0ms
  createAgentSessionRuntime: 35ms
  readPipedStdin: 1ms
  prepareInitialMessage: 0ms
  initTheme: 1ms
  resolveModelScope: 0ms
  createAgentSession: 0ms
  TOTAL: 39ms
-----------------------------
…省略 extensions 组…
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
exit=0
auth.json
models-store.json
ls: /tmp/pi-lab/m9-agent-6/sessions: No such file or directory
```

**结论。** 预测成立。对比 §2.1：同一个工作流程，有会话时 `PI_CODING_AGENT_DIR/sessions/<cwd>/` 下会出现一个 JSONL 文件（README §5.3），这里没有。`auth.json` 和 `models-store.json` 是 `ModelRuntime` 建的（→ M1），和会话无关。

注意：本文的命令都是在非终端环境里运行的，所以 §2.1 不加管道也走了 print 模式（`run.sh` 默认带 `-p`，结果相同）。在真实终端里去掉 `-p` 和管道，就会进入 TUI。

### 7.3 rpc 响应的顺序

**问题。** 把三行一次性写进 rpc 模式的 stdin：`get_last_assistant_text`（id `a1`）、`clear_queue`（id `a2`）、一行非 JSON。stdout 上的输出按什么顺序出现？（命令和输出见 §3.5 的真实实例。）

**预测。** 三行在同一个 chunk 里到达，`attachJsonlLineReader` 在一次 `onData` 里依次同步调用 `void handleInputLine(line)`（§4.13）：

1. 第 1 行解析成功，`await handleCommand(...)`。`get_last_assistant_text` 分支里没有 await，但 `handleCommand` 是 async 函数，返回值要等一个微任务，于是 `handleInputLine` 在这里让出。
2. 第 2 行同样进入 `handleCommand`。`clear_queue` 分支同步调用 `session.clearQueue()`，它同步发出 `queue_update` 事件（`packages/coding-agent/src/core/agent-session.ts:2493`），订阅者立即写出这一行。然后同样在 await 处让出。
3. 第 3 行 `JSON.parse` 失败，同步输出 `parse` 错误响应。
4. 微任务依次完成：输出 `a1` 的响应，再输出 `a2` 的响应。

所以顺序是：`queue_update` 事件、`parse` 错误、`a1`、`a2`。和输入顺序不同。

**运行。**

```bash
printf '{"id":"a1","type":"get_last_assistant_text"}\n{"id":"a2","type":"clear_queue"}\nnot json\n' | WORK=/tmp/pi-lab/m9-work-8 PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-8 ./run.sh --mode rpc; echo "exit=$?"
```

```
Running without API keys...
{"type":"queue_update","steering":[],"followUp":[]}
{"type":"response","command":"parse","success":false,"error":"Failed to parse command: Unexpected token 'o', \"not json\" is not valid JSON"}
{"id":"a1","type":"response","command":"get_last_assistant_text","success":true,"data":{}}
{"id":"a2","type":"response","command":"clear_queue","success":true,"data":{"steering":[],"followUp":[]}}
exit=0
```

**结论。** 顺序和预测一致。rpc 协议不保证响应顺序，客户端必须用 `id` 配对，并且要准备好事件夹在响应之间。stdin 在三行之后就结束了，pi 仍然输出了全部响应才退出：`shutdown()` 在 `process.exit` 之前调用了 `flushRawStdout()`（`rpc-mode.ts:739-741`）。

**附：前端只是订阅者。** `m9-sdk.ts print` 场景先用 `createAgentSessionRuntime` 建 runtime，在 `runtime.session` 上挂一个自己的订阅者，再把 runtime 交给 `runPrintMode`。预测：`runPrintMode` 的输出和 CLI 的 `-p` 一样；自己的订阅者和 `runPrintMode` 挂的订阅者看到同一组事件；`runPrintMode` 结束时 `dispose()` 了 runtime，返回 0。

```bash
export PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-sdk-1 WORK=/tmp/pi-lab/m9-work-sdk-1
R="$(node -p 'require("node:url").pathToFileURL(process.argv[1]).href' "$PI/packages/coding-agent/src/experimental/source-resolver.ts")"
node --import "$R" m9-sdk.ts print
```

```
agentDir = /tmp/pi-lab/m9-agent-sdk-1
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
[自己的订阅者] 看到：agent_start → agent_end → agent_settled
runPrintMode 返回 0
```

`runPrintMode` 不知道还有别的订阅者，两者互不影响。CLI 的 `main()` 在 print 模式下做的就是这件事，只是多了参数解析和 stdout 接管。

---

## 8. 自测与衔接

### 8.1 自测

1. `pi --mode text "你好" < /dev/null` 在终端里运行，走哪个前端？如果去掉 `< /dev/null` 呢？
2. 一个扩展在 `session_start` 事件里调用了 `console.log("loaded")`。在 `pi --mode json "…"` 的 stdout 上能看到这一行吗？在 TUI 里呢？
3. 一个 rpc 客户端发了 `prompt`，收到 `{"disposition":"started"}` 后立刻发 `{"type":"new_session"}`。正在进行的运行会怎样？之后的事件来自哪个 session？

<details>
<summary>答案</summary>

1. `< /dev/null` 让 stdin 不是终端，`resolveAppMode` 返回 `print`（`packages/coding-agent/src/main.ts:119`），`toPrintOutputMode` 得到 `"text"`，走 `runPrintMode`。`readPipedStdin` 读到空内容，`trim()` 后返回 `undefined`（93 行），首条提示词就是 `"你好"`。去掉重定向后 stdin 和 stdout 都是终端，`--mode text` 不参与判断，结果是 `interactive`，启动 TUI，`"你好"` 作为初始消息交给 `InteractiveMode`（968 行）。
2. json 模式下看不到：`main()` 在 668 行接管了 stdout，`console.log` 写到 stderr。`session_start` 在 `bindExtensions` 里发出（`packages/coding-agent/src/core/agent-session.ts:3360`），这时早已接管。TUI 模式不调用 `takeOverStdout`（666 行的条件排除了 `interactive`），`console.log` 照常写 stdout，也就是 TUI 正在绘制的终端，会和画面混在一起。扩展在 TUI 里应该用 `ctx.ui.notify`。
3. `new_session` 调用 `runtimeHost.newSession()`（`packages/coding-agent/src/modes/rpc/rpc-mode.ts:437`）。runtime 在 `teardownCurrent` 里先 `await this.session.abort()`（`packages/coding-agent/src/core/agent-session-runtime.ts:170`），正在进行的运行被中止，中止的那一轮写进旧会话文件；然后 dispose 旧 session，建新 session，调用 rpc 注册的 `rebindSession`（`finishSessionReplacement`，188–190 行）。`handleCommand` 在 439 行又调用一次 `rebindSession()`。之后的事件都来自新 session；旧 session 的监听器已经在 dispose 时清空。

</details>

### 8.2 衔接

到这里，coding-agent 包从入口到循环的整条链已经读完：M9 准备好 runtime 并把 `session` 交给前端，前端调用 `session.prompt()`，M8 把它变成 `agent.prompt()`，M2 跑循环，M1 请求模型，M3 执行工具，M4 写会话文件。

下一个模块 M10 读 MCP。它从 `McpClient.connect()`（`packages/mcp/src`）开始，然后回到 coding-agent，看内置的 mcp 扩展（就是 §2.1 计时里的 `builtin:mcp`）怎么把 MCP 服务器的工具包装成 pi 的工具。
