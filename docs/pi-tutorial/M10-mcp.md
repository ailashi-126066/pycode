# M10 MCP：接入外部工具服务器

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

MCP（Model Context Protocol）是一个让 agent 调用外部"工具服务器"的协议。服务器可以是本地子进程（通过 stdin/stdout 通信），也可以是远程 HTTP 服务。双方交换的是 JSON-RPC 2.0 消息：客户端先 `initialize`，再用 `tools/list` 取工具清单，用 `tools/call` 调用工具。

pi 的 MCP 支持分成两层，本模块两层都读：

- **(a) pi-mcp 包**（源码目录 `packages/mcp`）：一个独立的 MCP 客户端。它只认 JSON-RPC 消息和传输层（transport），不知道 agent、会话、扩展的存在。入口是 `McpClient.connect()`。
- **(b) coding-agent 的内置 MCP 扩展**（源码目录 `packages/coding-agent/src/extensions/mcp`）：一个随 CLI 一起加载的扩展。它读取 `mcp.json`，为每个服务器建一个 `McpClient`，把服务器的每个工具包装成 pi 的 `ToolDefinition`，名字是 `mcp__<server>__<tool>`，再用 `pi.registerTool()` 注册。

注册之后，MCP 工具和 read、bash 这些内置工具走同一条路：出现在 `system` 消息的 `toolsAdded` 里（M2 §4.4），经过扩展的 `tool_call` 事件（M7），由 agent 循环调用 `execute()`（M2 §4.12），结果写进会话文件（M4）。MCP 扩展本身只额外做三件事：决定每个工具怎么暴露给模型（exposure），在工具调用前等服务器连上，以及把 MCP 的结果格式转换成 pi 的工具结果。

### 1.2 在调用链中的位置

MCP 不在 [README §2](README.md#2-全局调用链) 主线场景的调用链里，因为主线场景没有配置 MCP 服务器。配置了服务器之后，它在调用链上的位置如下：

```
cli.ts → main()                                                    [M9]
├─ extensionFactories = [...builtInExtensions, …]  内置的 mcp 扩展   [M9 → M7]
├─ createAgentSessionRuntime() … 加载扩展，发出 session_start         [M8 / M7]
│   └─ mcp 扩展的 session_start 处理函数                              [M10 b] ← 本模块
│       ├─ loadMcpConfig()  读 mcp.json
│       └─ startConnection()（后台）→ McpServerConnection.connectOnce()
│           ├─ McpClient.connect(StdioTransport)                     [M10 a] ← 本模块入口
│           ├─ McpClient.listTools()
│           └─ registerTools() → pi.registerTool(mcp__demo__echo)    [M10 b → M7 / M8]
└─ runPrintMode() → session.prompt(text)                           [M9 → M8]
    ├─ before_agent_start  ← mcp 扩展等 direct 服务器、写 mcp_servers 段 [M10 b / M6]
    └─ agent.prompt() → runLoop()                                  [M2]
        ├─ declareToolChanges()  toolsAdded 里有 mcp__demo__echo
        ├─ beforeToolCall() → 扩展 tool_call                         [M7]
        │   ├─ mcp 扩展：等这个工具的服务器就绪                        [M10 b]
        │   └─ 权限扩展：可以拦截
        └─ tool.execute()  MCP 工具定义里的 execute                  [M10 b]
            ├─ McpServerConnection.callTool() → McpClient.callTool() [M10 a]
            └─ convertMcpResult()  CallToolResult → AgentToolResult  [M10 b]
```

### 1.3 文件清单

**(a) `packages/mcp/src`**，共 3287 行：

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `client.ts` | 621 | `McpClient`：握手、请求/响应配对、超时、取消、分页列表、`callTool` | 精读，§4.1–§4.9 的主体 |
| `transports/transport.ts` | 64 | `McpTransport` 接口和共享的监听器管理 `TransportEvents` | 精读，§3.2 |
| `transports/stdio.ts` | 216 | `StdioTransport`：启动子进程，按行收发 JSON，关闭时杀进程组 | 精读，§4.2、§4.4、§4.9 |
| `transports/in-memory.ts` | 51 | `InMemoryTransport`：进程内一对传输，测试用 | 通读，§4.10 |
| `transports/streamable-http.ts` | 522 | `StreamableHttpTransport`：POST 请求、SSE 响应流、会话 id、断线重连、401 时交给 `AuthProvider` | 只读 `send` 和 `authorizedFetch`，§4.10 |
| `protocol/jsonrpc.ts` | 113 | JSON-RPC 消息类型、错误类型、类型守卫 | 精读，§3.1、§4.5 |
| `protocol/content.ts` | 118 | 工具结果的内容块类型和 `toLlmContent` | 精读，§3.4、§4.8 |
| `protocol/types.ts` | 131 | `initialize`、`Tool`、资源等协议类型，支持的协议版本 | 通读，§3.4 |
| `auth-provider.ts` | 20 | `AuthProvider` 接口：HTTP 传输向它要 token | 只看接口，§4.10 |
| `oauth/*.ts`（7 个文件） | 1358 | OAuth 发现、动态注册客户端、授权码流程、回调服务器 | 略，只在 §4.10 说明它在结构上接在哪里 |
| `index.ts`、`testing/index.ts` | 73 | 重新导出 | 略 |

**(b) `packages/coding-agent/src/extensions/mcp`**，共 4319 行，以及它依赖的几个文件：

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `extensions/mcp/index.ts` | 1347 | 扩展本体：事件处理函数、连接管理、工具注册、exposure、`/mcp` 命令 | 精读事件处理和工具注册，§4.11–§4.20；`/mcp` 命令和界面略 |
| `extensions/mcp/runtime.ts` | 454 | `McpServerConnection`：一个服务器的连接、懒重连、选择传输 | 精读，§4.14、§4.19 |
| `extensions/mcp/tools.ts` | 391 | 工具命名、`createMcpToolDefinition`、结果转换 | 精读，§4.15–§4.17；渲染器略 |
| `extensions/mcp/config.ts` | 243 | 读 `mcp.json`，项目覆盖规则 | 精读 `readConfigFile`、`loadMcpConfig`，§4.12 |
| `core/mcp-servers.ts` | 358 | 配置类型、校验、`McpExposure`、`getMcpToolExposure` | 通读，§3.5、§4.18 |
| `extensions/index.ts` | 14 | 内置扩展列表 | 通读，§4.11 |
| `extensions/tool-search/tool.ts` | 337 | `tool_search` 工具（BM25 排序） | 只读 `searchAndLoad`，§4.18 |
| `extensions/codemode/tool.ts` | 434 | `codemode` 工具 | 只读 `reachesTools`，§4.20 |
| `extensions/mcp/oauth.ts`、`cli.ts`、`ui.ts`、`resources.ts`、`log.ts`、`*.lazy.ts` | 1884 | OAuth 登录、`pi mcp` 子命令、`/mcp` 界面、资源工具、服务器日志 | 略 |

行数用 `wc -l` 统计。

### 1.4 读完能回答的问题

1. pi 和一个 stdio MCP 服务器之间，从启动到第一次工具调用，线上依次出现哪些 JSON-RPC 消息？
2. 一个请求发出去之后，`McpClient` 怎么把回来的响应对上号？请求超时或被用户取消时，服务器会收到什么？
3. 服务器的工具 `echo` 在 pi 里叫什么名字？它的结果在模型、界面、codemode 脚本三处分别是什么样子？
4. `exposure` 取 `direct`、`deferred`、`codemode` 时，模型第一次请求里能看到这个工具吗？模型要怎样才能调用它？
5. 权限扩展能拦截 MCP 工具调用吗？被拦截的调用会发到服务器吗？

---

## 2. 先跑一次

### 2.1 给 pi 接上一个 stdio MCP 服务器

pi-mcp 的测试里有一个最小的 stdio 服务器 `packages/mcp/test/fixtures/stdio-server.mjs`（27 行）：逐行读 stdin，回答 `initialize`、`tools/list`、`tools/call`、`ping`。它的 `echo` 工具没有 description，`inputSchema` 只有 `{"type":"object"}`。为了让假模型有一个带参数说明的工具可调，并且能看到服务器到底收到了哪些消息，本模块在 [`lab/m10-server.mjs`](lab/m10-server.mjs) 里按同样的写法写了一个服务器，多了三样东西：

- 工具带 description、`inputSchema.properties` 和 `annotations`。
- 两个工具：`echo` 返回 `echo: <text>`；`fail` 返回 `isError: true`。
- 设置 `M10_SERVER_LOG` 时，把收到的每条消息的 method 追加到这个文件。

[`lab/m10-run.sh`](lab/m10-run.sh) 在 `$PI_CODING_AGENT_DIR/mcp.json` 里写入这个服务器，然后用 [`lab/m10-faux-ext.ts`](lab/m10-faux-ext.ts) 作为假模型调用 `run.sh`。写入的 `mcp.json` 是：

```json
{
  "mcpServers": {
    "demo": {
      "command": "node",
      "args": ["<lab 目录>/m10-server.mjs"],
      "exposure": "direct"
    }
  }
}
```

假模型第 1 次请求调用 `mcp__demo__echo`，第 2 次请求把工具结果复述出来：

```bash
WORK=/tmp/pi-lab/m10-work-1 PI_CODING_AGENT_DIR=/tmp/pi-lab/m10-agent-1 PI_TRACE=1 ./m10-run.sh -p "用 echo 工具说你好"
cat /tmp/pi-lab/m10-agent-1/server.log
```

```
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write,mcp__demo__echo,mcp__demo__fail]
  user       [{"type":"text","text":"用 echo 工具说你好"}]
[faux] 第 2 次请求，收到 4 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write,mcp__demo__echo,mcp__demo__fail]
  user       [{"type":"text","text":"用 echo 工具说你好"}]
  assistant  [{"type":"toolCall","id":"call_1","name":"mcp__demo__echo","arguments":{"text":"你好 MCP"}}]
  toolResult mcp__demo__echo isError=false "echo: 你好 MCP"
工具返回：echo: 你好 MCP
initialize
notifications/initialized
tools/list
tools/call
```

可以看到：

- 服务器的两个工具 `echo` 和 `fail` 变成了 `mcp__demo__echo` 和 `mcp__demo__fail`，和内置的 read、bash、edit、write 一起出现在第一条 `system` 消息的 `toolsAdded` 里。和 README §5.1 对比，`sections` 没有变化。
- 服务器收到 4 条消息：握手的 `initialize` 和 `notifications/initialized`，取清单的 `tools/list`，以及模型触发的 `tools/call`。前三条在会话开始时发出，第四条在工具执行时发出。

### 2.2 事件流和会话文件

```bash
WORK=/tmp/pi-lab/m10-work-2 PI_CODING_AGENT_DIR=/tmp/pi-lab/m10-agent-2 ./m10-run.sh --mode json "用 echo 工具说你好" > /tmp/pi-lab/m10-events.jsonl
node summarize-events.mjs /tmp/pi-lab/m10-events.jsonl
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
message_update [toolcall_start]
message_update [toolcall_delta] x2
message_update [toolcall_end]
message_end (assistant)
tool_execution_start mcp__demo__echo
tool_execution_end mcp__demo__echo
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
共 28 行（含第一行 session 头）
```

事件的种类和顺序与 README §5.2 完全一样，只是工具名换成了 `mcp__demo__echo`。MCP 没有引入新的事件。工具执行的两个事件和工具结果消息：

```bash
grep -E '^\{"type":"(tool_execution_start|tool_execution_end)"' /tmp/pi-lab/m10-events.jsonl
grep -m1 '^{"type":"message_end","message":{"role":"toolResult"' /tmp/pi-lab/m10-events.jsonl
```

```
{"type":"tool_execution_start","toolCallId":"call_1","toolName":"mcp__demo__echo","args":{"text":"你好 MCP"}}
{"type":"tool_execution_end","toolCallId":"call_1","toolName":"mcp__demo__echo","result":{"content":[{"type":"text","text":"echo: 你好 MCP"}],"details":{"server":"demo","tool":"echo"},"structuredContent":{"content":[{"type":"text","text":"echo: 你好 MCP"}]}},"isError":false,"durationMs":1}
{"type":"message_end","message":{"role":"toolResult","toolCallId":"call_1","toolName":"mcp__demo__echo","content":[{"type":"text","text":"echo: 你好 MCP"}],"details":{"server":"demo","tool":"echo"},"isError":false,"durationMs":1,"timestamp":1793516052309}}
```

`tool_execution_end.result` 是 MCP 扩展返回的 `AgentToolResult`（§4.17）：

- `content` 是给模型看的文本，来自服务器的 `content`。
- `details` 记下工具属于哪个服务器，给界面用。
- `structuredContent` 是服务器返回的整个 `CallToolResult`，给 codemode 脚本用。按 M2 §2.2 的规则，它不进 `toolResult` 消息。

会话文件：

```bash
node show-session.mjs "$(ls -t /tmp/pi-lab/m10-agent-2/sessions/*/*.jsonl | head -1)"
```

```
session                id=01a195e3 parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/m10-work-2"}
model_change           id=a563ceb7 parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=44c7e44b parent=a563ceb7 {"thinkingLevel":"off"}
message                id=9aaf73d9 parent=44c7e44b system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write,mcp__demo__echo,mcp__demo__fail
message                id=db77bbbc parent=9aaf73d9 user       [{"type":"text","text":"用 echo 工具说你好"}]
message                id=65740463 parent=db77bbbc assistant  [{"type":"toolCall","id":"call_1","name":"mcp__demo__echo","arguments":{"text":"你好 MCP"}}] stop=toolUse
message                id=539d1c9c parent=65740463 toolResult [{"type":"text","text":"echo: 你好 MCP"}]
message                id=a3eeaa75 parent=539d1c9c assistant  [{"type":"text","text":"工具返回：echo: 你好 MCP"}] stop=stop
```

会话文件里没有任何 MCP 专用的条目。`system` 消息里两个 MCP 工具的声明是：

```bash
node -e 'const fs=require("fs");for(const l of fs.readFileSync(process.argv[1],"utf8").split("\n")){if(!l)continue;const e=JSON.parse(l);if(e.type==="message"&&e.message.role==="system"){for(const t of e.message.toolsAdded.filter(t=>t.name.startsWith("mcp__")))console.log(JSON.stringify(t))}}' "$(ls -t /tmp/pi-lab/m10-agent-2/sessions/*/*.jsonl | head -1)"
```

```
{"name":"mcp__demo__echo","description":"Echo the given text back","parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}
{"name":"mcp__demo__fail","description":"Always fails","parameters":{"type":"object","properties":{}}}
```

`description` 和 `parameters` 原样来自服务器的 `tools/list`（§3.4）。

### 2.3 去掉 coding-agent，只留 MCP 客户端

[`lab/m10-client.ts`](lab/m10-client.ts) 直接用 pi-mcp 的 `McpClient` 和 `StdioTransport` 连接 pi 仓库自带的测试服务器 `stdio-server.mjs`，并在传输层外面包一层记录器。`→` 是客户端发出的消息，`←` 是收到的消息：

```bash
node --conditions=source --experimental-strip-types m10-client.ts stdio
```

```
connectionState=idle
   1ms → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"m10-lab","version":"1.0.0"}}}
  26ms ← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"stdio-fixture","version":"1.0.0"}}}
  26ms → {"jsonrpc":"2.0","method":"notifications/initialized"}
connectionState=connected protocolVersion=2025-06-18
InitializeResult: {"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"stdio-fixture","version":"1.0.0"}}
  27ms → {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  27ms ← {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo","inputSchema":{"type":"object"}}]}}
listTools(): [{"name":"echo","inputSchema":{"type":"object"}}]
  27ms → {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}}}
  27ms ← {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"hello"}]}}
callTool(): {"content":[{"type":"text","text":"hello"}]}
toLlmContent(): [{"type":"text","text":"hello"}]
stderr of server: "stdio fixture ready\n"
connectionState=closed
```

可以看到：

- 每个请求有一个递增的 `id`（1、2、3），响应带着同一个 `id` 回来。`notifications/initialized` 没有 `id`，它是通知，不需要响应。
- 客户端请求的协议版本是 `2025-11-25`，服务器回答 `2025-06-18`。客户端接受了，因为这个版本在它的支持列表里（§4.1）。
- 从发出 `initialize` 到收到响应用了 25ms，主要是启动 Node 子进程的时间。后面的请求都在 1ms 内完成。
- 服务器写到 stderr 的 `stdio fixture ready` 没有混进 JSON-RPC 消息，而是被传输层单独收集起来（§4.2）。

时间（毫秒）每次运行会不同。

---

## 3. 数据结构

### 3.1 JsonRpcMessage：线上的消息

MCP 的所有通信都是 JSON-RPC 2.0 消息。pi-mcp 把它定义成三种形状的联合：

源码：[`packages/mcp/src/protocol/jsonrpc.ts:1-35`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/protocol/jsonrpc.ts#L1-L35)

```ts
export type JsonRpcId = string | number;

export interface JsonRpcRequest {
	jsonrpc: "2.0";
	id: JsonRpcId;
	method: string;
	params?: unknown;
}

export interface JsonRpcNotification {
	jsonrpc: "2.0";
	method: string;
	params?: unknown;
}

export interface JsonRpcErrorObject {
	code: number;
	message: string;
	data?: unknown;
}

export interface JsonRpcSuccessResponse {
	jsonrpc: "2.0";
	id: JsonRpcId;
	result: unknown;
}

export interface JsonRpcErrorResponse {
	jsonrpc: "2.0";
	id: JsonRpcId;
	error: JsonRpcErrorObject;
}

export type JsonRpcResponse = JsonRpcSuccessResponse | JsonRpcErrorResponse;
export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse;
```

| 形状 | 区分方法 | 说明 |
|---|---|---|
| 请求 `JsonRpcRequest` | 有 `id`，有 `method` | 需要对方回答。客户端和服务器都可以发请求，例如服务器可以向客户端请求 `roots/list` |
| 通知 `JsonRpcNotification` | 没有 `id`，有 `method` | 不需要回答，例如 `notifications/initialized`、`notifications/progress`、`notifications/cancelled` |
| 响应 `JsonRpcResponse` | 有 `id`，有 `result` 或 `error`（二选一） | `id` 和它回答的请求相同 |

`id` 可以是字符串或数字。pi-mcp 自己发出的请求用从 1 开始递增的数字（§4.3）。

**真实实例。** §2.3 的输出里，三种形状都出现了：

```
→ {"jsonrpc":"2.0","id":2,"method":"tools/list"}                                    请求
← {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo","inputSchema":{"type":"object"}}]}}  成功响应
→ {"jsonrpc":"2.0","method":"notifications/initialized"}                            通知
```

本模块的实验里没有出现错误响应。作为参照，`stdio-server.mjs` 对不认识的 method 回答 `{"jsonrpc":"2.0","id":…,"error":{"code":-32601,"message":"not found"}}`。`-32601` 是 `JSON_RPC_ERROR_CODES.methodNotFound`（`packages/mcp/src/protocol/jsonrpc.ts:40`）。

### 3.2 McpTransport：传输层接口

`McpClient` 不关心消息怎么到达对方。它只依赖下面这个接口：

源码：[`packages/mcp/src/transports/transport.ts:18-26`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/transport.ts#L18-L26)

```ts
export interface McpTransport {
	start(): Promise<void>;
	send(message: JsonRpcMessage, options?: McpTransportSendOptions): Promise<void>;
	close(): Promise<void>;
	onMessage(listener: McpTransportMessageListener): () => void;
	onError(listener: McpTransportErrorListener): () => void;
	onClose(listener: McpTransportCloseListener): () => void;
	setProtocolVersion?(version: string): void;
}
```

| 成员 | 说明 |
|---|---|
| `start()` | 建立连接。stdio 在这里启动子进程；HTTP 什么都不做，第一次 `send` 时才发请求 |
| `send(message, { signal })` | 发出一条消息。Promise 在消息写出后 resolve，不等对方回答。`signal` 在发送方不再等待回答时被中止，传输层看到它就不能再发这条消息（例如 HTTP 在刷新 token 之后重试） |
| `close()` | 关闭连接 |
| `onMessage` / `onError` / `onClose` | 注册监听器，返回取消注册的函数。收到的每条消息都通过 `onMessage` 交给客户端 |
| `setProtocolVersion` | 可选。握手完成后客户端告诉传输层协商好的版本。只有 HTTP 用它，放进 `MCP-Protocol-Version` 请求头 |

三个监听器的管理代码由抽象类 `TransportEvents`（`packages/mcp/src/transports/transport.ts:29`）提供，三种传输都继承它。它保证 `emitClose` 对一个传输只触发一次（`packages/mcp/src/transports/transport.ts:59-63`）。

接口的特点是"发"和"收"分开：`send` 只管写出去，回答从 `onMessage` 进来。把回答和请求配对是客户端的事（§4.6），传输层不知道哪条消息回答哪个请求。

**真实实例。** pi-mcp 提供三种实现：

| 实现 | `start()` 做什么 | `send()` 做什么 | 消息从哪里进来 |
|---|---|---|---|
| `StdioTransport` | 启动子进程，`detached: true` 放进独立进程组 | 把 JSON 加 `\n` 写进子进程 stdin | 子进程 stdout，按 `\n` 切行 |
| `StreamableHttpTransport` | 只设标志 | 一条消息一个 POST 请求 | POST 的响应体（JSON 或 SSE 流），以及一个可选的长连接 GET SSE 流 |
| `InMemoryTransport` | 只设标志 | `structuredClone` 后在微任务里交给对端 | 对端的 `send` |

§2.3 用的是 `StdioTransport`，§7.3 用的是 `InMemoryTransport`。

### 3.3 McpClient 的状态和 PendingRequest

`McpClient` 有四个连接状态：

源码：[`packages/mcp/src/client.ts:41`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L41)

```ts
type ClientState = "idle" | "connecting" | "connected" | "closed";
```

每个发出去、还没收到回答的请求，在 `pending` 表里有一条 `PendingRequest`：

源码：[`packages/mcp/src/client.ts:60-72`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L60-L72)

```ts
interface PendingRequest {
	resolve: (value: unknown) => void;
	reject: (reason: unknown) => void;
	timeoutMs: number;
	timer: ReturnType<typeof setTimeout> | undefined;
	signal: AbortSignal | undefined;
	onAbort: () => void;
	/** Aborted when the request settles, so the transport does not dispatch (or retry) it afterwards. */
	sendController: AbortController;
	cancellable: boolean;
	onProgress: ((progress: ProgressNotification) => void) | undefined;
	progressToken: JsonRpcId | undefined;
}
```

| 字段 | 说明 |
|---|---|
| `resolve`、`reject` | 调用方拿到的 Promise 的两个出口。收到响应、超时、取消、连接关闭，最终都调用其中一个 |
| `timeoutMs`、`timer` | 超时时长和计时器。默认 30 秒（`DEFAULT_REQUEST_TIMEOUT_MS`，`packages/mcp/src/client.ts:38`），coding-agent 设为服务器配置的 `timeout`，默认 60 秒 |
| `signal`、`onAbort` | 调用方传入的中止信号，以及注册在它上面的处理函数 |
| `sendController` | 请求结束时中止，告诉传输层不要再发这条消息 |
| `cancellable` | `initialize` 为 `false`，协议规定不能取消它；其余请求为 `true` |
| `onProgress`、`progressToken` | 调用方想要进度通知时，`progressToken` 等于请求 id，并写进请求的 `params._meta` |

`McpClient` 的私有字段里有四张表（`packages/mcp/src/client.ts:164-167`），前三张以请求 id 为键：

| 字段 | 内容 |
|---|---|
| `pending` | 本端发出的、等待回答的请求 |
| `progressRequests` | `progressToken` → 请求 id，收到进度通知时用它找到请求 |
| `incoming` | 对端发来的、本端正在处理的请求，值是 `AbortController`，对端发 `notifications/cancelled` 时中止它 |
| `requestHandlers` | method → 处理函数。构造时注册了 `ping`，配了 `roots` 选项时再注册 `roots/list` |

**真实实例。** §7.3 的 `timeout` 场景里，`callTool` 发出 id 为 2 的请求后，`pending` 里有一条 `timeoutMs: 200`、`cancellable: true` 的记录。200ms 后计时器触发，这条记录被删除，`reject` 收到 `McpTimeoutError`，同时客户端发出 `{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":2,"reason":"Request timed out"}}`。

### 3.4 Tool 和 CallToolResult：工具声明和工具结果

`tools/list` 返回的每个工具：

源码：[`packages/mcp/src/protocol/types.ts:76-85`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/protocol/types.ts#L76-L85)

```ts
export interface Tool {
	name: string;
	title?: string;
	description?: string;
	inputSchema: Record<string, unknown>;
	outputSchema?: Record<string, unknown>;
	annotations?: ToolAnnotations;
	execution?: ToolExecution;
	_meta?: Record<string, unknown>;
}
```

| 字段 | 说明 | coding-agent 怎么用（§4.16） |
|---|---|---|
| `name` | 服务器内的工具名 | 拼成 `mcp__<server>__<name>`；调用时原样发回服务器 |
| `title`、`description` | 显示名和说明 | `description` 优先，没有时用 `title`，再没有时生成一句 `MCP tool … from server …` |
| `inputSchema` | 参数的 JSON Schema | 变成 `ToolDefinition.parameters`，补齐 `type` 和 `properties` |
| `outputSchema` | `structuredContent` 的 JSON Schema | 放进 `ToolDefinition.outputSchema` 的 `structuredContent` 属性 |
| `annotations` | `readOnlyHint`、`destructiveHint` 等提示，由服务器作者声明，不保证真实 | 只保留四个布尔提示，变成 `ToolDefinition.annotations` |

`tools/call` 的结果：

源码：[`packages/mcp/src/protocol/content.ts:63-71`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/protocol/content.ts#L63-L71)

```ts
export type ContentBlock = TextContent | ImageContent | AudioContent | ResourceLinkContent | EmbeddedResourceContent;

export interface CallToolResult {
	content: ContentBlock[];
	/** Any JSON value, including arrays and `null` (MCP 2026-07-28; earlier revisions allowed only objects). */
	structuredContent?: unknown;
	isError?: boolean;
	_meta?: Record<string, unknown>;
}
```

| 字段 | 说明 |
|---|---|
| `content` | 内容块数组。五种块：`text`、`image`（base64）、`audio`（base64）、`resource_link`（只给 URI）、`resource`（内嵌的资源内容，文本或 base64） |
| `structuredContent` | 可选的结构化结果，任意 JSON 值 |
| `isError` | 工具执行失败。这是"工具层面的失败"，和 JSON-RPC 的 `error` 响应不同：后者表示请求本身出错（method 不存在、参数无效） |

LLM 消息（M1）只接受文本和图片两种内容，所以 MCP 的五种块需要转换（§4.8）。

**真实实例。** `m10-client.ts wrap` 连接 `m10-server.mjs`，`listTools()` 返回：

```
[{"name":"echo","description":"Echo the given text back","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]},"annotations":{"readOnlyHint":true}},{"name":"fail","description":"Always fails","inputSchema":{"type":"object","properties":{}}}]
```

`fail` 工具返回的 `CallToolResult` 是 `{"content":[{"type":"text","text":"something went wrong"}],"isError":true}`（完整输出见 §3.6）。

### 3.5 McpServerConfig 和 McpServerEntry：一个服务器的配置

`mcp.json` 里 `mcpServers` 下的每一项是一个 `McpServerConfig`。两种服务器共用一组基础字段：

源码：[`packages/coding-agent/src/core/mcp-servers.ts:59-88`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/mcp-servers.ts#L59-L88)

```ts
interface McpServerConfigBase {
	/** Default: `codemode`. */
	exposure?: McpExposure;
	/**
	 * What the server offers, in a sentence. The `mcp_servers` system prompt section lists the server
	 * with it, tool search ranks the server's tools by it, and codemode's `describeNamespace()` returns it.
	 */
	description?: string;
	/**
	 * Exposure of single tools, overriding `exposure`. Keys are tool names as the server offers them,
	 * or patterns where `*` matches any characters. An exact name wins over patterns; among patterns
	 * the first match in the object wins. `hidden` removes tools, so `"exposure": "hidden"` with
	 * overrides for a few tools exposes only those.
	 */
	toolExposure?: Record<string, McpExposure>;
	/** Set to false to keep the entry without connecting. Default: true. */
	enabled?: boolean;
	/** Per-request timeout in seconds. Progress notifications from the server reset it. Default: 60. */
	timeout?: number;
}

export interface McpStdioServerConfig extends McpServerConfigBase {
	type?: "stdio";
	command: string;
	args?: string[];
	/** Values may reference environment variables (`${NAME}`) or commands (`!cmd`). */
	env?: Record<string, string>;
	/** Relative paths resolve against the session working directory. */
	cwd?: string;
}
```

| 字段 | 说明 |
|---|---|
| `exposure` | 工具怎么暴露给模型，四个取值见 §4.18。**默认 `codemode`** |
| `description` | 一句话说明服务器提供什么 |
| `toolExposure` | 按工具名（或带 `*` 的模式）单独设置 exposure |
| `enabled` | `false` 时保留配置但不连接 |
| `timeout` | 每个请求的超时，单位秒，默认 60 |
| `command`、`args`、`env`、`cwd` | stdio 服务器：可执行文件、参数、额外的环境变量、工作目录。`command` 是一个可执行文件，不是 shell 命令 |

HTTP 服务器是 `McpHttpServerConfig`（`packages/coding-agent/src/core/mcp-servers.ts:149`），把 `command` 换成 `url`，另有 `headers`、`oauth`、`auth`。两者的联合是 `McpServerConfig`（`packages/coding-agent/src/core/mcp-servers.ts:162`）。有 `command` 的是 stdio，有 `url` 的是 HTTP，`type` 可以省略。

读进来的配置包一层，记下来源：

源码：[`packages/coding-agent/src/extensions/mcp/config.ts:52-64`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/config.ts#L52-L64)

```ts
export interface McpServerEntry {
	name: string;
	config: McpServerConfig;
	/** Config file that defined the entry, or the path of the extension that registered it. */
	source: string;
	/**
	 * The global or the project `mcp.json`, or `extension` for servers registered with
	 * `pi.registerMcpServer()`. Changes to extension servers are not saved.
	 */
	scope?: "global" | "project" | "extension";
	/** Project `mcp.json` with an override of this global server's `enabled`, `exposure`, or `toolExposure`. */
	override?: string;
}
```

| 字段 | 说明 |
|---|---|
| `name` | `mcpServers` 里的键，只能含字母、数字、`_`、`-` |
| `config` | 校验过的 `McpServerConfig` |
| `source` | 定义它的文件，或注册它的扩展的路径 |
| `scope` | `global`（`$PI_CODING_AGENT_DIR/mcp.json`）、`project`（`<项目>/.pi/mcp.json`，项目被信任时才读）、`extension`（扩展用 `pi.registerMcpServer()` 注册） |
| `override` | 项目文件只覆盖了全局服务器的 `enabled`/`exposure`/`toolExposure` 时，记下项目文件路径 |

**真实实例。** `m10-client.ts config` 调用 coding-agent 的 `loadMcpConfig` 读取 §2.2 用的配置目录：

```bash
node --conditions=source --experimental-strip-types m10-client.ts config /tmp/pi-lab/m10-agent-2
```

```
{
  "servers": [
    {
      "name": "demo",
      "config": {
        "command": "node",
        "args": [
          "/Users/cxg126/Desktop/golang/project/miniclaudecode/docs/pi-tutorial/lab/m10-server.mjs"
        ],
        "exposure": "direct"
      },
      "source": "/tmp/pi-lab/m10-agent-2/mcp.json",
      "scope": "global"
    }
  ],
  "errors": []
}
```

`args` 里的路径取决于你的 lab 目录位置。外层的 `LoadedMcpConfig`（`packages/coding-agent/src/extensions/mcp/config.ts:66`）还有 `errors`：配置有问题的服务器被跳过，错误收集在这里，不影响其他服务器。

### 3.6 MCP 工具的 ToolDefinition 和结果

MCP 扩展为每个工具生成一个 `ToolDefinition`（定义见 M7，`packages/coding-agent/src/core/extensions/types.ts:599`）。和内置工具相比，MCP 工具会设置下面几个字段：

| 字段 | 值 |
|---|---|
| `name` | `mcp__<server>__<tool>`，非 `[A-Za-z0-9_]` 字符换成 `_`，过长时截断加哈希（§4.15） |
| `label` | `<server>/<tool>`，界面显示用 |
| `parameters` | 服务器的 `inputSchema` |
| `outputSchema` | 一个描述 `CallToolResult` 的 schema，所有 MCP 工具都有 |
| `exposure` | 由 MCP 的 exposure 换算而来（§4.18） |
| `namespace` | `{ name: "mcp__<server>", aliases: [<server>], description?, instructions? }`，codemode 和 tool_search 按它分组 |
| `annotations` | 服务器声明的布尔提示 |
| `execute` | 调 `callTool`，再把结果转成 `AgentToolResult` |

`execute` 返回的 `AgentToolResult`（M2 §3.2）里，`details` 的类型是：

源码：[`packages/coding-agent/src/extensions/mcp/tools.ts:57-62`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/tools.ts#L57-L62)

```ts
export interface McpToolDetails {
	server: string;
	tool: string;
	/** Temp file with the full text output, when the model-facing text was truncated. */
	fullOutputPath?: string;
}
```

**真实实例。** `m10-client.ts wrap` 把 `m10-server.mjs` 的两个工具包装成 `ToolDefinition`，打印主要字段，再直接调用 `execute()`：

```bash
node --conditions=source --experimental-strip-types m10-client.ts wrap
```

```
listTools(): [{"name":"echo","description":"Echo the given text back","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]},"annotations":{"readOnlyHint":true}},{"name":"fail","description":"Always fails","inputSchema":{"type":"object","properties":{}}}]
ToolDefinition: {"name":"mcp__demo__echo","label":"demo/echo","description":"Echo the given text back","parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]},"exposure":"direct","namespace":{"name":"mcp__demo","aliases":["demo"]},"annotations":{"readOnlyHint":true}}
  outputSchema.properties 的键: ["content","structuredContent","isError"]
ToolDefinition: {"name":"mcp__demo__fail","label":"demo/fail","description":"Always fails","parameters":{"type":"object","properties":{}},"exposure":"direct","namespace":{"name":"mcp__demo","aliases":["demo"]}}
  outputSchema.properties 的键: ["content","structuredContent","isError"]
mcp__demo__echo.execute() → {"content":[{"type":"text","text":"echo: hi"}],"details":{"server":"demo","tool":"echo"},"structuredContent":{"content":[{"type":"text","text":"echo: hi"}]}}
mcp__demo__fail.execute() → {"content":[{"type":"text","text":"something went wrong"}],"details":{"server":"demo","tool":"fail"},"structuredContent":{"content":[{"type":"text","text":"something went wrong"}],"isError":true},"isError":true}
```

`fail` 的结果有两处 `isError`：`structuredContent.isError` 是服务器原样返回的；外层的 `isError: true` 是 MCP 扩展加的，告诉 agent 循环这是一个失败的工具结果（M2 §4.12）。

### 3.7 类型关系图

```
 mcp.json ──loadMcpConfig()──▶ McpServerEntry { name, config: McpServerConfig, source, scope }
                                    │ startConnection()
                                    ▼
                           McpServerConnection（runtime.ts，每个服务器一个）
                           ├─ state: connecting / connected / disconnected / needs-auth / failed / closed
                           ├─ tools: Tool[] ◀──────────── McpClient.listTools()
                           └─ client: McpClient ─────────────┐
                                                             │
 ┌──────────────── pi-mcp（packages/mcp）────────────────────▼──────────────────────┐
 │ McpClient                                                                        │
 │ ├─ state: idle / connecting / connected / closed                                 │
 │ ├─ pending: Map<id, PendingRequest>   ◀── 响应按 id 配对                           │
 │ └─ transport: McpTransport ── send(JsonRpcMessage) ──▶ 服务器                     │
 │        StdioTransport / StreamableHttpTransport / InMemoryTransport               │
 │                              ◀── onMessage(JsonRpcMessage) ──                     │
 │ callTool() ─▶ CallToolResult { content: ContentBlock[], structuredContent, isError }│
 └──────────────────────────────────────────────────────────────────────────────────┘
          │ Tool                                          │ CallToolResult
          ▼ createMcpToolDefinition()                     ▼ convertMcpResult()
   ToolDefinition { name: mcp__s__t, exposure, ... }   AgentToolResult { content, details: McpToolDetails,
          │ pi.registerTool()                                            structuredContent, isError }
          ▼                                                   │
   AgentSession 的工具表 → AgentTool → agent 循环（M2）────────┘
```

pi-mcp 里的类型（框内）只描述协议。框外的类型属于 coding-agent，负责把协议对象接到 pi 的工具系统上。两边的接口只有两处：`Tool` 变成 `ToolDefinition`，`CallToolResult` 变成 `AgentToolResult`。

---

## 4. 从入口读起

调用树如下。前半部分是 pi-mcp，从 `McpClient.connect()` 读起；后半部分是 coding-agent 的 MCP 扩展，从 `session_start` 读起，它在 §4.14 调用前半部分。

```
(a) pi-mcp：packages/mcp/src
McpClient.connect(transport)                              §4.1
├─ transport.start()  StdioTransport.start()              §4.2
├─ requestInternal("initialize")                          §4.3
│   └─ transport.send()  StdioTransport.send()            §4.4
│        …服务器回答…
│        StdioTransport.handleStdout()                    §4.4
│        ├─ parseJsonRpcMessage()                         §4.5
│        └─ emitMessage() → McpClient.handleMessage()     §4.6
│             └─ handleResponse() → resolve
├─ validateInitializeResult()、版本检查                   §4.1
└─ notifyInternal("notifications/initialized")            §4.3
超时和取消：armTimeout()、cancelPending()                 §4.7
McpClient.listTools() → listAll() → listPage()            §4.8
McpClient.callTool() → validateCallToolResult()           §4.8
toLlmContent()                                            §4.8
McpClient.close() → markClosed() → StdioTransport.close() §4.9
其他传输：InMemoryTransport、StreamableHttpTransport、OAuth §4.10

(b) coding-agent：packages/coding-agent/src/extensions/mcp
builtInExtensions 里的 mcp 扩展                           §4.11
session_start 处理函数                                    §4.12
├─ loadMcpConfig() → readConfigFile()                     §4.12
├─ ensureDiscoveryActive()                                §4.19
└─ startConnection() → createConnection()                 §4.13
    └─ McpServerConnection.getClient() → open() → connectOnce()  §4.14
        ├─ createDefaultTransport()                       §4.14
        ├─ McpClient.connect()、listTools()               → §4.1、§4.8
        └─ onTools → registerTools()                      §4.15
            ├─ createMcpToolName()                        §4.15
            ├─ createMcpToolDefinition()                  §4.16
            └─ pi.registerTool()                          → M7 / M8
exposure 的换算                                           §4.18
ensureDiscoveryActive() 和 tool_search                    §4.19
before_agent_start 处理函数                               §4.20
tool_call 处理函数                                        §4.21
ToolDefinition.execute()                                  §4.16
├─ McpServerConnection.callTool() → withClient()          §4.22
└─ convertMcpResult()                                     §4.17
session_shutdown 处理函数                                 §4.23
```

### 4.1 McpClient.connect()

**调用位置**：coding-agent 的 `McpServerConnection.connectOnce()` 调用 `client.connect(transport)`（§4.14）。在 `lab/m10-client.ts` 里是 `await client.connect(logged(transport))`。

源码：[`packages/mcp/src/client.ts:204-250`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L204-L250)

```ts
	async connect(transport: McpTransport): Promise<InitializeResult> {
		if (this.state !== "idle") throw new Error(`Cannot connect MCP client in ${this.state} state`);
		this.state = "connecting";
		this.transport = transport;
		this.disposers = [
			transport.onMessage((message) => this.handleMessage(message)),
			// Transport errors are reported only. Pending requests fail when the transport closes.
			transport.onError((error) => this.emitError(error)),
			transport.onClose(() => this.handleTransportClose()),
		];

		try {
			await transport.start();
			const capabilities: ClientCapabilities = { ...this.options.capabilities };
			if (this.options.roots && capabilities.roots === undefined) capabilities.roots = {};
			const result = validateInitializeResult(
				await this.requestInternal(
					"initialize",
					{
						protocolVersion: this.options.protocolVersion ?? LATEST_PROTOCOL_VERSION,
						capabilities,
						clientInfo: {
							name: this.options.name,
							version: this.options.version,
							...(this.options.title === undefined ? {} : { title: this.options.title }),
						},
					},
					{},
					true,
				),
			);
			if (!(SUPPORTED_PROTOCOL_VERSIONS as readonly string[]).includes(result.protocolVersion)) {
				throw new Error(`MCP server selected unsupported protocol version ${result.protocolVersion}`);
			}
			this.protocolVersionValue = result.protocolVersion;
			this.serverInfoValue = result.serverInfo;
			this.serverCapabilitiesValue = result.capabilities;
			this.instructionsValue = result.instructions;
			transport.setProtocolVersion?.(result.protocolVersion);
			await this.notifyInternal("notifications/initialized", undefined, true);
			this.state = "connected";
			return result;
		} catch (error) {
			await this.close().catch(() => {});
			throw error;
		}
	}
```

**讲解**

`connect` 完成 MCP 的握手，分四步：

1. **挂监听器。** 在传输层上注册三个监听器：收到消息交给 `handleMessage`（§4.6），传输错误只转发给客户端的错误监听器，传输关闭时调用 `handleTransportClose`（§4.9）。返回的取消函数存在 `disposers` 里，`close()` 时统一取消。注释写明了一条规则：传输错误不会让请求失败，请求只在传输关闭时失败。
2. **启动传输。** `transport.start()`。stdio 在这里启动子进程（§4.2）。
3. **发 `initialize` 请求。** 参数是三样东西：客户端想用的协议版本（默认 `LATEST_PROTOCOL_VERSION`，即 `2025-11-25`），客户端能力 `capabilities`，客户端信息 `clientInfo`。构造时传了 `roots`，就在能力里声明 `roots: {}`，表示服务器可以向客户端请求 `roots/list`。coding-agent 把会话工作目录作为唯一的 root（§4.14）。最后一个参数 `true` 是 `allowConnecting`：此时状态还是 `connecting`，普通请求不允许发出，握手请求要例外（§4.3）。
4. **检查回答，发 `notifications/initialized`。** `validateInitializeResult`（`packages/mcp/src/client.ts:74`）检查回答里有字符串 `protocolVersion`、对象 `capabilities`、带 `name` 和 `version` 的 `serverInfo`。服务器可以回答一个和请求不同的版本，只要它在 `SUPPORTED_PROTOCOL_VERSIONS` 里就接受。这个列表是：

源码：[`packages/mcp/src/protocol/types.ts:4-9`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/protocol/types.ts#L4-L9)

```ts
export const LATEST_PROTOCOL_VERSION = "2025-11-25";
/**
 * Versions the client accepts from a server. Servers that do not support the requested version answer
 * with their own latest one, so older versions stay accepted for servers built on older SDKs.
 */
export const SUPPORTED_PROTOCOL_VERSIONS = [LATEST_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"] as const;
```

§2.3 里测试服务器回答 `2025-06-18`，就是走的这条路。版本确定后，客户端保存服务器信息、能力和 `instructions`（服务器对自己工具的整体说明），把版本告诉传输层，然后发出 `notifications/initialized` 通知，状态变为 `connected`。

任何一步失败，`catch` 先 `close()` 再把异常抛给调用方。所以 `connect` 失败后客户端处于 `closed`，不能再用。一个 `McpClient` 只能连接一次（第一行检查 `idle`），coding-agent 重连时会新建客户端（§4.14）。

**往哪走**：§4.2 `StdioTransport.start()`，然后 §4.3 `requestInternal`。

### 4.2 StdioTransport.start()

**调用位置**：`McpClient.connect()` → `transport.start()`。

源码：[`packages/mcp/src/transports/stdio.ts:90-141`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/stdio.ts#L90-L141)

```ts
	async start(): Promise<void> {
		if (this.started) throw new Error("MCP stdio transport already started");
		if (this.closed) throw new McpConnectionClosedError();
		this.started = true;
		const env = this.options.inheritEnv === false ? { ...this.options.env } : { ...process.env, ...this.options.env };
		const child = crossSpawn(this.options.command, this.options.args ?? [], {
			cwd: this.options.cwd,
			env,
			stdio: ["pipe", "pipe", this.options.stderr === "inherit" ? "inherit" : "pipe"],
			windowsHide: true,
			// Own process group, so closing the transport can terminate the server's children too.
			detached: USE_PROCESS_GROUPS,
		});
		this.child = child;
		const pid = child.pid;
		if (USE_PROCESS_GROUPS && pid !== undefined) {
			installExitHook();
			liveProcessGroups.add(pid);
			child.once("exit", () => liveProcessGroups.delete(pid));
		}
		child.stdout?.on("data", (chunk: Buffer | string) => this.handleStdout(chunk));
		child.stdout?.on("error", (error) => this.emitError(error));
		child.stdin?.on("error", (error) => {
			if (!this.closed) this.emitError(error);
		});
		child.stderr?.on("data", (chunk: Buffer | string) => this.handleStderr(chunk));
		child.stderr?.on("error", (error) => this.emitError(error));
		child.on("close", () => {
			this.child = undefined;
			if (this.stdoutBuffer.toString("utf8").trim()) {
				this.emitError(new Error("MCP stdio server closed with an incomplete JSON-RPC message"));
			}
			this.stdoutBuffer = Buffer.alloc(0);
			this.emitClose();
		});

		await new Promise<void>((resolve, reject) => {
			const onSpawn = () => {
				child.off("error", onError);
				resolve();
			};
			const onError = (error: Error) => {
				child.off("spawn", onSpawn);
				reject(error);
			};
			child.once("spawn", onSpawn);
			child.once("error", onError);
		});
		child.on("error", (error) => {
			if (!this.closed) this.emitError(error);
		});
	}
```

**讲解**

`start` 启动服务器子进程，把三个标准流接到传输层上：

- **环境变量。** 默认继承 pi 进程的环境，再叠加配置里的 `env`。`inheritEnv: false` 时只用配置的 `env`。§7.2 里服务器能读到 `M10_SERVER_LOG`，就是因为继承了环境。
- **`crossSpawn`。** 用 `cross-spawn` 包而不是 Node 的 `spawn`，是为了在 Windows 上正确启动 `npx.cmd` 这类脚本。
- **进程组。** 非 Windows 平台上 `detached: true`，子进程成为一个新进程组的组长。关闭时向整个组发信号（`process.kill(-pid, signal)`，`packages/mcp/src/transports/stdio.ts:32`），这样经 `npx`、`uvx` 包装启动的服务器和它的子进程都会被结束。进程组 id 记在模块级的 `liveProcessGroups` 里；`installExitHook` 在 pi 进程退出时给所有还活着的组发 SIGTERM，防止 pi 异常退出后留下服务器进程。
- **stdout** 是 JSON-RPC 消息的通道，交给 `handleStdout` 切行解析（§4.4）。
- **stderr** 是服务器的日志通道，交给 `handleStderr`（`packages/mcp/src/transports/stdio.ts:209`），只保留最后 64KB，可以通过 `transport.stderr` 读取。§2.3 最后打印的 `stderr of server` 就来自这里。coding-agent 在服务器连接失败时把 stderr 的末尾附在错误信息后面（§4.14）。
- **子进程结束**时触发 `close` 事件：缓冲区里还有半条消息就报错，然后 `emitClose()`，客户端随之进入 `closed`（§4.9）。

最后等 `spawn` 或 `error` 事件之一。命令不存在时这里 reject，`connect` 失败。

**往哪走**：§4.3 `requestInternal`。

### 4.3 requestInternal()、notifyInternal()、requireTransport()

**调用位置**：`connect()` 直接调用 `requestInternal("initialize", …, true)`；其余请求经公开方法 `request()`（`packages/mcp/src/client.ts:252`）调用 `requestInternal(…, false)`。`listTools`、`callTool`、`ping` 都走 `request()`。

源码：[`packages/mcp/src/client.ts:396-444`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L396-L444)

```ts
	private async requestInternal(
		method: string,
		params: Record<string, unknown> | undefined,
		options: McpRequestOptions,
		allowConnecting: boolean,
	): Promise<unknown> {
		const transport = this.requireTransport(allowConnecting);
		if (options.signal?.aborted) throw new McpAbortError();
		const id = this.nextRequestId++;
		const progressToken = options.onProgress ? id : undefined;
		const requestParams =
			progressToken === undefined
				? params
				: { ...params, _meta: { ...(isObject(params?._meta) ? params._meta : {}), progressToken } };
		const message: JsonRpcRequest = {
			jsonrpc: "2.0",
			id,
			method,
			...(requestParams === undefined ? {} : { params: requestParams }),
		};
		return new Promise<unknown>((resolve, reject) => {
			const entry: PendingRequest = {
				resolve,
				reject,
				timeoutMs: options.timeoutMs ?? this.options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
				timer: undefined,
				signal: options.signal,
				// The spec forbids cancelling `initialize`.
				onAbort: () =>
					this.cancelPending(
						id,
						new McpAbortError(),
						method !== "initialize",
						String(options.signal?.reason ?? "Aborted"),
					),
				sendController: new AbortController(),
				cancellable: method !== "initialize",
				onProgress: options.onProgress,
				progressToken,
			};
			this.pending.set(id, entry);
			if (progressToken !== undefined) this.progressRequests.set(progressToken, id);
			options.signal?.addEventListener("abort", entry.onAbort, { once: true });
			this.armTimeout(id, entry);
			transport
				.send(message, { signal: entry.sendController.signal })
				.catch((error) => this.cancelPending(id, error, false));
		});
	}
```

**讲解**

这是所有请求的公共路径。输入是 method、params 和选项 `McpRequestOptions`（`signal`、`timeoutMs`、`onProgress`，`packages/mcp/src/client.ts:54`），输出是一个 Promise，在收到响应时 resolve 成响应的 `result`。

它做的事：

1. `requireTransport` 检查状态：`connected` 才能发；`allowConnecting` 为真时 `connecting` 也可以。否则抛 `McpConnectionClosedError`。
2. 分配 id。`nextRequestId` 从 1 开始，每个请求加 1。§2.3 里 `initialize`、`tools/list`、`tools/call` 的 id 依次是 1、2、3。
3. 调用方要进度通知时，用 id 作为 `progressToken` 写进 `params._meta`。服务器发 `notifications/progress` 时带上这个 token，客户端据此找到请求（§4.7）。
4. 创建 `PendingRequest`（§3.3），放进 `pending` 表，在调用方的 `signal` 上挂 `onAbort`，启动超时计时器。
5. 调 `transport.send()`。注意它没有被 `await`：Promise 的结果取决于响应何时到达，不取决于消息何时写出。写出失败时 `cancelPending(id, error, false)` 让请求以这个错误失败，第三个参数 `false` 表示不通知服务器（消息本来就没发出去）。

`onAbort` 里的第三个参数是 `method !== "initialize"`：中止 `initialize` 时只在本地让请求失败，不发 `notifications/cancelled`，因为协议不允许取消握手。

通知的路径简单得多，没有 id，也不进 `pending` 表：

源码：[`packages/mcp/src/client.ts:446-463`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L446-L463)

```ts
	private async notifyInternal(
		method: string,
		params: Record<string, unknown> | undefined,
		allowConnecting: boolean,
	): Promise<void> {
		await this.requireTransport(allowConnecting).send({
			jsonrpc: "2.0",
			method,
			...(params === undefined ? {} : { params }),
		});
	}

	private requireTransport(allowConnecting: boolean): McpTransport {
		if (this.transport && (this.state === "connected" || (allowConnecting && this.state === "connecting"))) {
			return this.transport;
		}
		throw new McpConnectionClosedError(`MCP client is ${this.state}`);
	}
```

**往哪走**：§4.4 消息怎么写出去、回答怎么读进来。

### 4.4 StdioTransport.send() 和 handleStdout()

**调用位置**：`requestInternal()` → `transport.send()`；子进程 stdout 的 `data` 事件 → `handleStdout()`（§4.2 注册）。

源码：[`packages/mcp/src/transports/stdio.ts:143-150`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/stdio.ts#L143-L150)

```ts
	async send(message: JsonRpcMessage): Promise<void> {
		const stdin = this.child?.stdin;
		if (!this.started || this.closed || !stdin?.writable) throw new McpConnectionClosedError();
		const payload = `${JSON.stringify(message)}\n`;
		await new Promise<void>((resolve, reject) => {
			stdin.write(payload, (error) => (error ? reject(error) : resolve()));
		});
	}
```

源码：[`packages/mcp/src/transports/stdio.ts:181-207`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/stdio.ts#L181-L207)

```ts
	private handleStdout(chunk: Buffer | string): void {
		this.stdoutBuffer = Buffer.concat([this.stdoutBuffer, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
		const maxMessageBytes = this.options.maxMessageBytes ?? DEFAULT_MAX_MESSAGE_BYTES;
		while (true) {
			const newline = this.stdoutBuffer.indexOf(0x0a);
			if (newline < 0) {
				if (this.stdoutBuffer.length > maxMessageBytes) {
					this.stdoutBuffer = Buffer.alloc(0);
					this.emitError(new Error(`MCP stdio message exceeds ${maxMessageBytes} bytes`));
				}
				return;
			}
			const line = this.stdoutBuffer.subarray(0, newline);
			this.stdoutBuffer = this.stdoutBuffer.subarray(newline + 1);
			if (line.length > maxMessageBytes) {
				this.emitError(new Error(`MCP stdio message exceeds ${maxMessageBytes} bytes`));
				continue;
			}
			const text = line.toString("utf8").replace(/\r$/, "");
			if (!text.trim()) continue;
			try {
				this.emitMessage(parseJsonRpcMessage(JSON.parse(text)));
			} catch (error) {
				this.emitError(error);
			}
		}
	}
```

**讲解**

stdio 传输的线上格式是"一行一条 JSON"。`send` 把消息序列化后加一个 `\n` 写进 stdin，写出完成时 resolve。`JSON.stringify` 不会输出未转义的换行，所以一条消息总是一行。

`handleStdout` 是反方向。stdout 的 `data` 事件可能只带半行，也可能一次带好几行，所以先拼进 `stdoutBuffer`，再循环取出完整的行：

- 按字节 `0x0a`（`\n`）切分，而不是先转字符串。这样一个多字节 UTF-8 字符被切在两个 chunk 之间时也不会出错。
- 去掉行尾的 `\r`，兼容用 `\r\n` 换行的服务器；跳过空行。
- 单条消息上限 16MB（`DEFAULT_MAX_MESSAGE_BYTES`，`packages/mcp/src/transports/transport.ts:3`）。超过时丢弃并报错，防止一个不换行的服务器让缓冲区无限增长。
- 每行先 `JSON.parse`，再用 `parseJsonRpcMessage`（§4.5）检查形状。任何一步失败都只 `emitError`，不中断循环，后面的行照常处理。

解析成功的消息经 `emitMessage` 交给所有 `onMessage` 监听器，也就是 `McpClient.handleMessage`（§4.6）。`m10-client.ts` 在客户端之前注册了一个记录用的监听器，所以 §2.3 里每条 `←` 都打印在客户端处理它之前。

**往哪走**：§4.5 `parseJsonRpcMessage`，然后 §4.6 `handleMessage`。

### 4.5 parseJsonRpcMessage() 和类型守卫

**调用位置**：`StdioTransport.handleStdout()`、`StreamableHttpTransport` 的 JSON 和 SSE 解析 → `parseJsonRpcMessage()`；`McpClient.handleMessage()` → 三个 `isJsonRpc*` 守卫。

源码：[`packages/mcp/src/protocol/jsonrpc.ts:89-113`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/protocol/jsonrpc.ts#L89-L113)

```ts
export function isJsonRpcId(value: unknown): value is JsonRpcId {
	return typeof value === "string" || (typeof value === "number" && Number.isFinite(value));
}

export function isJsonRpcRequest(message: unknown): message is JsonRpcRequest {
	return (
		isObject(message) && message.jsonrpc === "2.0" && isJsonRpcId(message.id) && typeof message.method === "string"
	);
}

export function isJsonRpcNotification(message: unknown): message is JsonRpcNotification {
	return isObject(message) && message.jsonrpc === "2.0" && !("id" in message) && typeof message.method === "string";
}

export function isJsonRpcResponse(message: unknown): message is JsonRpcResponse {
	if (!isObject(message) || message.jsonrpc !== "2.0" || !isJsonRpcId(message.id)) return false;
	if ("result" in message) return !("error" in message);
	if (!("error" in message) || !isObject(message.error)) return false;
	return typeof message.error.code === "number" && typeof message.error.message === "string";
}

export function parseJsonRpcMessage(value: unknown): JsonRpcMessage {
	if (isJsonRpcRequest(value) || isJsonRpcNotification(value) || isJsonRpcResponse(value)) return value;
	throw new McpError(JSON_RPC_ERROR_CODES.invalidRequest, "Invalid JSON-RPC message");
}
```

**讲解**

三个守卫按 §3.1 表里的规则区分消息：有 `id` 有 `method` 是请求，没有 `id` 有 `method` 是通知，有 `id` 有 `result` 或 `error`（不能两个都有）是响应。`parseJsonRpcMessage` 只是三者的"或"，不符合任何一种就抛 `McpError`。

这里只检查信封，不检查 `params` 或 `result` 的内容。内容的检查放在具体的方法里，例如 `validateInitializeResult`（§4.1）、`validateListPage` 和 `validateCallToolResult`（§4.8）。

同一个文件还定义了四种错误（`packages/mcp/src/protocol/jsonrpc.ts:45-79`）：`McpError`（带 JSON-RPC 错误码，服务器回答 `error` 时用）、`McpConnectionClosedError`、`McpTimeoutError`、`McpAbortError`（`name` 是 `"AbortError"`，和 DOM 的中止错误同名，方便上层统一判断）。

**往哪走**：§4.6 `handleMessage`。

### 4.6 handleMessage()、handleResponse()、handleRequest()、handleNotification()

**调用位置**：传输层的 `onMessage` 监听器 → `handleMessage()`（§4.1 注册）。

源码：[`packages/mcp/src/client.ts:465-490`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L465-L490)

```ts
	private handleMessage(message: JsonRpcMessage): void {
		if (isJsonRpcResponse(message)) {
			this.handleResponse(message);
			return;
		}
		if (isJsonRpcRequest(message)) {
			void this.handleRequest(message);
			return;
		}
		if (isJsonRpcNotification(message)) {
			this.handleNotification(message.method, message.params);
			return;
		}
		this.emitError(new McpError(JSON_RPC_ERROR_CODES.invalidRequest, "Received invalid JSON-RPC message"));
	}

	private handleResponse(message: JsonRpcResponse): void {
		const entry = this.pending.get(message.id);
		if (!entry) {
			this.emitError(new Error(`Received response for unknown MCP request ${String(message.id)}`));
			return;
		}
		this.removePending(message.id, entry);
		if ("error" in message) entry.reject(new McpError(message.error.code, message.error.message, message.error.data));
		else entry.resolve(message.result);
	}
```

**讲解**

`handleMessage` 按消息形状分三路。

**响应**：用 `id` 在 `pending` 表里找到请求。找不到（例如请求已经超时被删掉，服务器才回答）就报一个错误，丢掉这条响应。找到了就先 `removePending`（清计时器、解除 abort 监听，§4.7），再按 `result` 或 `error` 调 `resolve` 或 `reject`。`error` 响应变成 `McpError`，保留错误码和 `data`。

这就是"发"和"收"分开的传输接口（§3.2）在客户端这边的另一半：多个请求可以同时在途，响应可以按任意顺序回来，靠 `id` 配对。

**请求和通知**：MCP 是双向的，服务器也可以向客户端发请求和通知。

源码：[`packages/mcp/src/client.ts:492-534`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L492-L534)

```ts
	private async handleRequest(message: JsonRpcRequest): Promise<void> {
		const transport = this.transport;
		if (!transport) return;
		const handler = this.requestHandlers.get(message.method);
		if (!handler) {
			await transport
				.send({
					jsonrpc: "2.0",
					id: message.id,
					error: { code: JSON_RPC_ERROR_CODES.methodNotFound, message: `Method not found: ${message.method}` },
				})
				.catch((error) => this.emitError(error));
			return;
		}
		const controller = new AbortController();
		this.incoming.set(message.id, controller);
		try {
			const result = await handler(message.params, { signal: controller.signal });
			await transport.send({ jsonrpc: "2.0", id: message.id, result: result ?? {} });
		} catch (error) {
			const responseError =
				error instanceof McpError
					? { code: error.code, message: error.message, data: error.data }
					: { code: JSON_RPC_ERROR_CODES.internalError, message: toError(error).message };
			await transport
				.send({ jsonrpc: "2.0", id: message.id, error: responseError })
				.catch((sendError) => this.emitError(sendError));
		} finally {
			this.incoming.delete(message.id);
		}
	}

	private handleNotification(method: string, params: unknown): void {
		if (method === "notifications/progress") this.handleProgress(params);
		else if (method === "notifications/cancelled") this.handleCancelled(params);
		for (const listener of this.notificationListeners.get(method) ?? []) {
			try {
				listener(params);
			} catch (error) {
				this.emitError(error);
			}
		}
	}
```

服务器发来的请求，按 method 在 `requestHandlers` 里找处理函数。默认只有 `ping` 和（配了 roots 时的）`roots/list`（`packages/mcp/src/client.ts:173-182`）。找不到就回答 `methodNotFound`。处理中的请求记在 `incoming` 表里，服务器发 `notifications/cancelled` 取消它时，`handleCancelled`（`packages/mcp/src/client.ts:549`）中止对应的 `AbortController`。

通知有两个是客户端自己处理的：进度通知（§4.7）和取消通知。之后所有通知都分发给用 `onNotification(method, listener)` 注册的监听器。coding-agent 用它监听两个通知：`notifications/message`（服务器日志，写进 `mcp.log`）和 `notifications/tools/list_changed`（工具清单变了，重新 `listTools`），见 §4.14。

监听器抛的异常被捕获后转成 `emitError`，不会打断其他监听器，也不会打断传输层的读循环。

**往哪走**：§4.7 请求的其他结局：超时、取消、进度。

### 4.7 armTimeout()、cancelPending()、removePending()

**调用位置**：`requestInternal()` 发出请求时 → `armTimeout()`；超时、调用方中止、发送失败 → `cancelPending()`；收到响应 → `removePending()`。

源码：[`packages/mcp/src/client.ts:536-583`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L536-L583)

```ts
	private handleProgress(params: unknown): void {
		if (!isObject(params) || !isJsonRpcId(params.progressToken) || typeof params.progress !== "number") return;
		const requestId = this.progressRequests.get(params.progressToken);
		const entry = requestId === undefined ? undefined : this.pending.get(requestId);
		if (requestId === undefined || !entry) return;
		this.armTimeout(requestId, entry);
		try {
			entry.onProgress?.(params as unknown as ProgressNotification);
		} catch (error) {
			this.emitError(error);
		}
	}

	private handleCancelled(params: unknown): void {
		if (isObject(params) && isJsonRpcId(params.requestId)) this.incoming.get(params.requestId)?.abort(params.reason);
	}

	private armTimeout(id: JsonRpcId, entry: PendingRequest): void {
		if (entry.timer) clearTimeout(entry.timer);
		if (!Number.isFinite(entry.timeoutMs) || entry.timeoutMs <= 0) return;
		entry.timer = setTimeout(() => {
			this.cancelPending(id, new McpTimeoutError(entry.timeoutMs), entry.cancellable, "Request timed out");
		}, entry.timeoutMs);
	}

	private cancelPending(id: JsonRpcId, error: unknown, notifyServer: boolean, reason?: string): void {
		const entry = this.pending.get(id);
		if (!entry) return;
		this.removePending(id, entry);
		entry.reject(error);
		if (notifyServer && this.transport) {
			void this.transport
				.send({
					jsonrpc: "2.0",
					method: "notifications/cancelled",
					params: { requestId: id, ...(reason ? { reason } : {}) },
				})
				.catch((sendError) => this.emitError(sendError));
		}
	}

	private removePending(id: JsonRpcId, entry: PendingRequest): void {
		this.pending.delete(id);
		if (entry.timer) clearTimeout(entry.timer);
		if (entry.progressToken !== undefined) this.progressRequests.delete(entry.progressToken);
		entry.signal?.removeEventListener("abort", entry.onAbort);
		entry.sendController.abort();
	}
```

**讲解**

一个请求有四种结局，都经过 `removePending` 清理：

| 结局 | 入口 | reject 的错误 | 通知服务器 |
|---|---|---|---|
| 收到响应 | `handleResponse`（§4.6） | `error` 响应时为 `McpError` | 否 |
| 超时 | `armTimeout` 的计时器 | `McpTimeoutError` | 是，`reason: "Request timed out"`（`initialize` 除外） |
| 调用方中止 | `onAbort`（§4.3） | `McpAbortError` | 是，`reason` 取 `signal.reason`（`initialize` 除外） |
| 发送失败 | `transport.send().catch` | 发送的错误 | 否 |

另外，连接关闭时 `rejectPending` 让所有在途请求以 `McpConnectionClosedError` 失败（§4.9）。

`cancelPending` 先检查请求还在不在 `pending` 表里，所以四种结局只会发生一种。例如超时之后响应才到，`handleResponse` 找不到请求，只报一个错误。

`removePending` 最后一行中止 `sendController`。它通过 `send(message, { signal })` 传给了传输层（§4.3），告诉传输层这个请求已经有了结局，还没发出去就不要再发。HTTP 传输在等 OAuth 刷新 token 时会用到它（§4.10）。

**进度通知会重置超时。** `handleProgress` 收到带已知 `progressToken` 的进度通知时，重新调用 `armTimeout`。所以超时的含义是"多长时间没有任何消息"，而不是"总共多长时间"。一个慢但持续报告进度的工具不会超时。coding-agent 的 MCP 工具总是传 `onProgress`（§4.16），所以总能享受这个规则。

§7.3 的实验验证了超时和中止两种结局在线上发出的消息。

**往哪走**：§4.8 两个最常用的请求：`listTools` 和 `callTool`。

### 4.8 listTools()、callTool() 和 toLlmContent()

**调用位置**：`McpServerConnection.connectOnce()` → `client.listTools()`（§4.14）；MCP 工具的 `execute` → `McpServerConnection.callTool()` → `client.callTool()`（§4.16、§4.22）。

源码：[`packages/mcp/src/client.ts:356-386`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L356-L386)

```ts
	/** Every item of a paginated list method. */
	private async listAll(
		method: string,
		key: string,
		isItem: (item: Record<string, unknown>) => boolean,
		options: McpRequestOptions,
	): Promise<Record<string, unknown>[]> {
		const items: Record<string, unknown>[] = [];
		const cursors = new Set<string>();
		let cursor: string | undefined;
		for (let pageNumber = 0; pageNumber < MAX_LIST_PAGES; pageNumber++) {
			const page = await this.listPage(method, key, isItem, cursor, options);
			items.push(...page.items);
			if (page.nextCursor === undefined) return items;
			if (cursors.has(page.nextCursor))
				throw new Error(`MCP ${method} returned duplicate cursor: ${page.nextCursor}`);
			cursors.add(page.nextCursor);
			cursor = page.nextCursor;
		}
		throw new Error(`MCP ${method} exceeded ${MAX_LIST_PAGES} pages`);
	}

	async callTool(
		name: string,
		args?: Record<string, unknown>,
		options: McpRequestOptions = {},
	): Promise<CallToolResult> {
		return validateCallToolResult(
			await this.request("tools/call", { name, ...(args === undefined ? {} : { arguments: args }) }, options),
		);
	}
```

**讲解**

`listTools()`（`packages/mcp/src/client.ts:296-298`）就是 `listAll("tools/list", "tools", isTool, options)`。MCP 的列表方法是分页的：每页结果带一个可选的 `nextCursor`，下次请求把它放进 `params.cursor`。`listAll` 循环取页，直到没有 `nextCursor`。两条保护：同一个 cursor 出现两次说明服务器在原地打转，直接报错；最多 1000 页（`MAX_LIST_PAGES`）。资源列表 `listResources`、`listResourceTemplates` 共用这段代码。

每一页由 `validateListPage`（`packages/mcp/src/client.ts:94-109`）检查：`tools` 是数组，每一项通过 `isTool`，即有字符串 `name` 和对象 `inputSchema`（`packages/mcp/src/client.ts:111`）。它还把 `nextCursor` 为 `null` 或 `""` 当成没有，兼容不规范的服务器。

`callTool(name, args)` 发出 `tools/call`，参数是 `{ name, arguments }`。返回值经过：

源码：[`packages/mcp/src/client.ts:144-153`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L144-L153)

```ts
/**
 * `content` is required by the spec, but servers that only return `structuredContent` omit it (the SDK defaults it too).
 * `structuredContent` may be any JSON value, as MCP 2026-07-28 allows.
 */
function validateCallToolResult(value: unknown): CallToolResult {
	if (!isObject(value) || (value.content !== undefined && !Array.isArray(value.content))) {
		throw new McpError(JSON_RPC_ERROR_CODES.invalidRequest, "Invalid MCP tools/call result");
	}
	return (value.content === undefined ? { ...value, content: [] } : value) as unknown as CallToolResult;
}
```

缺少 `content` 时补一个空数组。之后所有使用者都可以假定 `content` 存在。

`callTool` 返回的是协议原样的 `CallToolResult`。要把它交给模型，需要变成 LLM 能接受的文本和图片：

源码：[`packages/mcp/src/protocol/content.ts:79-118`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/protocol/content.ts#L79-L118)

```ts
function blockToLlmContent(block: ContentBlock): LlmContent {
	switch (block.type) {
		case "text":
			return { type: "text", text: block.text };
		case "image":
			return { type: "image", data: block.data, mimeType: block.mimeType };
		case "audio":
			return { type: "text", text: `[audio ${block.mimeType} omitted]` };
		case "resource_link":
			return { type: "text", text: `${block.name}: ${block.uri}` };
		case "resource": {
			const resource = block.resource;
			if ("text" in resource) return { type: "text", text: resource.text };
			if (resource.mimeType?.startsWith("image/")) {
				return { type: "image", data: resource.blob, mimeType: resource.mimeType };
			}
			return {
				type: "text",
				text: `[binary resource ${resource.uri} (${resource.mimeType ?? "unknown type"}) omitted]`,
			};
		}
		default:
			return { type: "text", text: `[unsupported MCP content ${(block as { type: string }).type}]` };
	}
}

/**
 * Convert a tool result to text and image content for a model. Text and images pass through,
 * embedded text resources become text, embedded image resources become images, and other blocks
 * (audio, resource links, binary resources) become a short text placeholder. A result without
 * content blocks but with `structuredContent` becomes its JSON, since servers should, but do not
 * always, mirror structured results as text.
 */
export function toLlmContent(result: Pick<CallToolResult, "content" | "structuredContent">): LlmContent[] {
	const content = (result.content ?? []).map(blockToLlmContent);
	if (content.length === 0 && result.structuredContent !== undefined) {
		content.push({ type: "text", text: JSON.stringify(result.structuredContent, null, 2) });
	}
	return content;
}
```

`LlmContent`（`packages/mcp/src/protocol/content.ts:77`）和 pi-ai 的 `TextContent`/`ImageContent` 形状相同，但 pi-mcp 没有依赖 pi-ai，而是自己定义了一份。转换规则是：文本和图片原样通过；内嵌的文本资源变成文本，内嵌的图片资源变成图片；音频、资源链接、其他二进制资源变成一行文字占位。完全没有内容块、只有 `structuredContent` 时，把它格式化成 JSON 文本。`m10-client.ts content` 对每种块调用一次 `toLlmContent`：

```bash
node --conditions=source --experimental-strip-types m10-client.ts content
```

```
text          → [{"type":"text","text":"plain text"}]
image         → [{"type":"image","data":"iVBORw0KGgo=","mimeType":"image/png"}]
audio         → [{"type":"text","text":"[audio audio/wav omitted]"}]
resource_link → [{"type":"text","text":"notes.md: file:///notes.md"}]
resource      → [{"type":"text","text":"embedded text"}]
resource      → [{"type":"text","text":"[binary resource file:///b.bin (application/octet-stream) omitted]"}]
只有 structuredContent → [{"type":"text","text":"{\n  \"total\": 3\n}"}]
```

coding-agent 对资源链接和二进制资源有自己的处理（§4.17），其余块交给 `toLlmContent`。

**往哪走**：§4.9 连接怎么关闭。

### 4.9 close()、markClosed() 和 StdioTransport.close()

**调用位置**：`connect()` 失败时、coding-agent 的 `McpServerConnection.close()` 和 `dropClient()` → `client.close()`；传输层关闭（例如服务器进程退出）→ `handleTransportClose()`。

源码：[`packages/mcp/src/client.ts:388-394`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L388-L394)

```ts
	async close(): Promise<void> {
		const transport = this.transport;
		this.transport = undefined;
		this.disposeTransportListeners();
		this.markClosed(new McpConnectionClosedError());
		await transport?.close();
	}
```

源码：[`packages/mcp/src/client.ts:592-611`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/client.ts#L592-L611)

```ts
	private handleTransportClose(): void {
		this.markClosed(new McpConnectionClosedError());
	}

	/** Idempotent: rejects in-flight requests, aborts server requests we are serving, and flips the state. */
	private markClosed(error: Error): void {
		const wasClosed = this.state === "closed";
		this.state = "closed";
		this.rejectPending(error);
		for (const controller of this.incoming.values()) controller.abort(error);
		this.incoming.clear();
		if (wasClosed) return;
		for (const listener of [...this.closeListeners]) {
			try {
				listener();
			} catch (listenerError) {
				this.emitError(listenerError);
			}
		}
	}
```

**讲解**

关闭有两个来源：本端主动 `close()`，或者传输层报告关闭（stdio 子进程退出、HTTP 传输被关闭）。两条路都进 `markClosed`：

- 状态变为 `closed`。
- `rejectPending`（`packages/mcp/src/client.ts:585`）让所有在途请求以 `McpConnectionClosedError` 失败。这就是 §4.1 注释里说的"请求只在传输关闭时失败"。
- 中止所有正在处理的服务器请求。
- 第一次关闭时通知 `onClose` 监听器。coding-agent 用这个监听器把服务器状态改成 `disconnected`（§4.22）。

`markClosed` 可以重复调用，监听器只会被通知一次。主动 `close()` 先取消了传输层监听器，所以之后传输层的 `close` 事件不会再进来。

stdio 传输的关闭按 MCP 规范的顺序：

源码：[`packages/mcp/src/transports/stdio.ts:152-179`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/stdio.ts#L152-L179)

```ts
	async close(): Promise<void> {
		if (this.closed) return;
		this.closed = true;
		const child = this.child;
		if (!child) {
			this.emitClose();
			return;
		}
		if (child.exitCode !== null || child.signalCode !== null) {
			child.stdin?.end();
			return;
		}
		// Shutdown per the spec: close stdin and let the server exit, then SIGTERM, then SIGKILL.
		const closeTimeoutMs = this.options.closeTimeoutMs ?? DEFAULT_CLOSE_TIMEOUT_MS;
		await new Promise<void>((resolve) => {
			const timers: ReturnType<typeof setTimeout>[] = [];
			child.once("close", () => {
				for (const timer of timers) clearTimeout(timer);
				// Children of the server that ignored stdin closing would otherwise outlive it.
				killProcessTree(child, "SIGTERM");
				resolve();
			});
			const grace = Math.min(STDIN_CLOSE_GRACE_MS, closeTimeoutMs);
			timers.push(setTimeout(() => killProcessTree(child, "SIGTERM"), grace));
			timers.push(setTimeout(() => killProcessTree(child, "SIGKILL"), grace + closeTimeoutMs));
			child.stdin?.end();
		});
	}
```

先关闭 stdin，大多数服务器读到 EOF 就自己退出（`m10-server.mjs` 的 `for await` 循环会结束）。500ms 后还没退出就给整个进程组发 SIGTERM，再过 2 秒发 SIGKILL。服务器退出后还要再发一次 SIGTERM 给进程组，因为服务器自己退出了，它启动的子进程可能还在。测试夹具 `stubborn-server.mjs` 就是一个忽略 EOF 和 SIGTERM、还留下孙进程的服务器，用来测这条路径。

**往哪走**：§4.10 另外两种传输。之后进入 coding-agent 部分（§4.11）。

### 4.10 其他传输：InMemoryTransport、StreamableHttpTransport 和 OAuth 的位置

**调用位置**：`InMemoryTransport` 只在测试和 `lab/m10-client.ts` 的 `timeout`/`abort` 场景里使用；`StreamableHttpTransport` 由 coding-agent 的 `createDefaultTransport()` 为配置了 `url` 的服务器创建（§4.14）。

`InMemoryTransport` 是最简单的实现。`createInMemoryTransportPair()` 创建两个互为对端的传输，`send` 是这样的：

源码：[`packages/mcp/src/transports/in-memory.ts:19-25`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/in-memory.ts#L19-L25)

```ts
	async send(message: JsonRpcMessage): Promise<void> {
		if (!this.started || this.closed) throw new McpConnectionClosedError();
		const peer = this.peer;
		if (!peer?.started || peer.closed) throw new McpConnectionClosedError("In-memory MCP peer is not connected");
		const copy = structuredClone(message);
		queueMicrotask(() => peer.deliver(copy));
	}
```

`structuredClone` 保证两端不共享对象，和真实的序列化效果一样；`queueMicrotask` 保证消息异步到达，`send` 返回之前对端不会收到它，和真实传输的时序一样。

`StreamableHttpTransport` 实现 MCP 的 Streamable HTTP 传输。它的 `send`：

源码：[`packages/mcp/src/transports/streamable-http.ts:231-266`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/streamable-http.ts#L231-L266)

```ts
	async send(message: JsonRpcMessage, { signal }: McpTransportSendOptions = {}): Promise<void> {
		if (!this.started || this.closed) throw new McpConnectionClosedError();
		const response = await this.authorizedFetch(
			"POST",
			{
				headers: { accept: "application/json, text/event-stream", "content-type": "application/json" },
				body: JSON.stringify(message),
			},
			signal,
		);
		await this.checkResponse(response);
		this.captureSession(response);

		if (!isJsonRpcRequest(message)) {
			// Notifications and responses are acknowledged with 202 and carry no reply; ignore any body.
			await discard(response);
			// The server-to-client stream may only open once the session is initialized.
			if ("method" in message && message.method === "notifications/initialized") this.startGetStream();
			return;
		}
		if (response.status === 202 || response.status === 204) {
			throw new McpHttpError(response.status, `MCP server accepted request ${message.method} without a response`);
		}
		const type = contentType(response);
		if (type === "application/json") {
			const body: unknown = await response.json();
			for (const item of Array.isArray(body) ? body : [body]) this.emitMessage(parseJsonRpcMessage(item));
			return;
		}
		if (type === "text/event-stream" && response.body) {
			void this.consumeResponseStream(response.body, message.id);
			return;
		}
		await discard(response);
		throw new McpHttpError(response.status, `Unsupported MCP response content type: ${type ?? "missing"}`);
	}
```

每条消息一个 POST 请求。对通知和响应，服务器回 202，没有内容。对请求，服务器可以直接回 JSON，也可以回一个 SSE 流，流里先发若干通知（例如进度），最后发响应。无论哪种，回来的消息都经 `emitMessage` 交给客户端，和 stdio 一样，`McpClient` 看不出区别。

和 stdio 不同的地方：

- **会话 id。** 服务器在响应头 `Mcp-Session-Id` 里给出会话 id，`captureSession` 记下来，之后每个请求都带上（`buildHeaders`，`packages/mcp/src/transports/streamable-http.ts:325-332`）。服务器重启后不认识旧 id，会回 404，变成 `McpSessionExpiredError`，coding-agent 在一个新会话上重试一次（§4.22）。
- **服务器到客户端的流。** 发出 `notifications/initialized` 之后，传输层打开一个 GET SSE 长连接，服务器可以通过它主动发通知和请求，断开时按指数退避重连（`runGetStream`，`packages/mcp/src/transports/streamable-http.ts:430`）。
- **认证。** 所有请求都经过 `authorizedFetch`：

源码：[`packages/mcp/src/transports/streamable-http.ts:295-317`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/transports/streamable-http.ts#L295-L317)

```ts
	private async authorizedFetch(
		method: "GET" | "POST",
		init: { headers: Record<string, string>; body?: string },
		signal?: AbortSignal,
	): Promise<Response> {
		const onUnauthorized = this.options.authProvider?.onUnauthorized?.bind(this.options.authProvider);
		for (let attempt = 0; ; attempt++) {
			const { headers, token } = await this.headers(init.headers);
			if (signal?.aborted) throw new McpAbortError();
			const response = await this.fetch(this.url, {
				method,
				headers,
				body: init.body,
				signal: this.controller.signal,
			});
			if (attempt > 0 || !onUnauthorized || !needsAuthorization(response)) return response;
			try {
				await onUnauthorized({ response, serverUrl: this.url, fetch: this.fetch, token });
			} finally {
				await discard(response);
			}
		}
	}
```

每次请求前向 `authProvider.token()` 要 token，放进 `Authorization: Bearer` 头。收到 401（或要求更多权限的 403）时，调用一次 `authProvider.onUnauthorized()`，然后重试一次。第二次还失败就把响应原样返回，由 `checkResponse` 变成 `McpAuthRequiredError`。注意 `signal?.aborted` 的检查在拿到 token 之后、发出请求之前：刷新 token 可能要一会儿，这期间请求如果已经超时或被取消（§4.7 的 `sendController`），就不发了。

传输层对 OAuth 的了解就到这个接口为止：

源码：[`packages/mcp/src/auth-provider.ts:12-20`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/mcp/src/auth-provider.ts#L12-L20)

```ts
/** Supplies bearer tokens to an MCP HTTP transport and may refresh them after a 401 response. */
export interface AuthProvider {
	/**
	 * Token for the next request. May refresh it over the network first, so the transport calls it only
	 * before requests whose answer it waits for. Closing a session reuses the token of the last request.
	 */
	token(): Promise<string | undefined>;
	onUnauthorized?(context: UnauthorizedContext): Promise<void>;
}
```

OAuth 的实现在单独的入口 `@earendil-works/pi-mcp/oauth`（`packages/mcp/src/oauth/`，1358 行），本教程不展开。它在结构上分三块：发现（`discovery.ts`：从 401 的 `WWW-Authenticate` 头和 `.well-known` 元数据找到授权服务器）、流程（`flow.ts`：注册客户端、授权码交换、刷新 token，`adaptOAuthProvider` 把它适配成上面的 `AuthProvider`）、本地回调服务器（`callback.ts`：接收浏览器重定向回来的授权码）。coding-agent 在 `extensions/mcp/oauth.ts` 里把 token 存进 `mcp-auth.json`，并提供 `/mcp login` 和 `pi mcp login`。

**往哪走**：pi-mcp 读完了。§4.11 开始读 coding-agent 怎么使用它。

### 4.11 MCP 是一个内置扩展

**调用位置**：`main()`（→ M9）把内置扩展放在扩展工厂列表的最前面：`const extensionFactories = [...builtInExtensions, ...(options?.extensionFactories ?? [])];`（`packages/coding-agent/src/main.ts:578`）。

源码：[`packages/coding-agent/src/extensions/index.ts:7-14`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/index.ts#L7-L14)

```ts
export const builtInExtensions: InlineExtension[] = [
	{ name: "llama.cpp", factory: llamaExtension, builtin: true },
	// Replaceable: an extension that registers `codemode`, `tool_search`, or `/mcp` (such as a third-party
	// MCP extension) takes over instead of running alongside the built-in one.
	{ name: "codemode", factory: codemodeExtension, replaceable: true, builtin: true },
	{ name: "tool-search", factory: toolSearchExtension, replaceable: true, builtin: true },
	{ name: "mcp", factory: mcpExtension, replaceable: true, builtin: true },
];
```

**讲解**

coding-agent 的核心（`core/`）不连接 MCP 服务器。MCP 支持是一个普通扩展，和用户写的扩展用同一套 `ExtensionAPI`（→ M7）：`pi.on(...)` 订阅事件，`pi.registerTool(...)` 注册工具，`pi.registerCommand("mcp", ...)` 注册 `/mcp` 命令。核心只为它保留了两样东西：`core/mcp-servers.ts` 里的配置类型和校验（扩展也可以用 `pi.registerMcpServer()` 注册服务器，核心负责存储），以及 `--tools` 过滤时对 `mcp__` 名字的特殊处理（`isMcpToolName`，`packages/coding-agent/src/core/mcp-servers.ts:39`）。

两个标志的含义（定义在 `InlineExtension`，`packages/coding-agent/src/core/extensions/types.ts:2071`）：

- `builtin: true`：扩展以 `builtin:mcp` 的名字出现，可以用设置里的 `"extensions": ["-builtin:mcp"]` 或命令行 `--no-mcp` 关掉。`--no-mcp` 的实现就是 `disabledBuiltinExtensions: parsed.noMcp ? ["mcp"] : undefined`（`packages/coding-agent/src/main.ts:797`）。
- `replaceable: true`：另一个扩展注册了同名的工具或命令（例如第三方 MCP 扩展注册 `/mcp`）时，内置扩展让位，而不是报冲突。

`codemode` 和 `tool-search` 是另外两个内置扩展。它们各注册一个默认不激活的工具，MCP 扩展在需要时激活它们（§4.19）。

扩展工厂本身只是一个闭包：`createMcpExtension()`（`packages/coding-agent/src/extensions/mcp/index.ts:279`）在闭包里保存服务器列表等状态，注册事件处理函数。`index.ts` 最后一行 `export default createMcpExtension();` 导出默认实例。

**往哪走**：§4.12 第一个事件处理函数 `session_start`。

### 4.12 session_start：读配置，后台连接

**调用位置**：会话创建完、扩展绑定之后，`AgentSession` 发出 `session_start` 事件（→ M7 / M8），MCP 扩展的处理函数被调用。

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:1114-1151`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L1114-L1151)

```ts
		pi.on("session_start", (_event, ctx) => {
			const loaded = (options.loadConfig ?? defaultLoadConfig)(ctx);
			configErrors = loaded.errors;
			projectConfig = loaded.projectConfig;
			autoEnableCodemode = loaded.autoEnableCodemode ?? true;
			warnedUnreachable = false;
			waitedForStartup = false;
			sessionCwd = ctx.cwd;
			modelRegistry = ctx.modelRegistry;
			session = new AbortController();
			const { signal } = session;
			configuredEntries = loaded.servers;
			const registered = registeredServers();
			overridden = registered.overridden;
			servers = [...loaded.servers.map((entry) => ({ entry })), ...registered.servers];
			emitChange();
			// Codemode or tool_search is activated from the config: the first prompt does not wait for
			// servers whose tools are not declared to the model, and scripts or searches wait for them.
			ensureDiscoveryActive(ctx);
			const enabled = servers.filter(isEnabled);
			if (enabled.length === 0) {
				reportProblems(ctx);
				return;
			}
			// The MCP client loads only now, so sessions without servers never pay for it. Waiting one
			// event loop turn lets the first render happen before loading and connecting.
			const runtime = new Promise((resolve) => setImmediate(resolve)).then(() => loadMcpRuntime());
			pending = Promise.all(enabled.map((server) => startConnection(server, runtime)))
				.then(() => {
					if (!signal.aborted) reportProblems(ctx);
				})
				.catch((error: unknown) => {
					// The session may have been disposed meanwhile, which makes ctx stale.
					try {
						ctx.ui.notify(`MCP failed to load: ${errorMessage(error)}`, "error");
					} catch {}
				});
		});
```

**讲解**

处理函数是同步的，没有 `await` 任何连接。它做的事：

1. **读配置。** `defaultLoadConfig`（`packages/coding-agent/src/extensions/mcp/index.ts:1343-1345`）调用 `loadMcpConfig({ agentDir: getAgentDir(), cwd: ctx.cwd, projectTrusted: ctx.isProjectTrusted() })`。`getAgentDir()` 就是 `PI_CODING_AGENT_DIR`，所以实验里的 `mcp.json` 放在那个目录下。
2. **合并服务器列表。** `mcp.json` 里的服务器在前，扩展注册的服务器在后；同名时 `mcp.json` 优先，被覆盖的注册记在 `overridden` 里，`/mcp` 会显示。
3. **`session` 控制器。** 每个会话一个新的 `AbortController`。会话结束时中止它（§4.23），所有还在进行的连接工作看到它就放弃结果。闭包里的初值是一个已经中止的控制器（`packages/coding-agent/src/extensions/mcp/index.ts:317-318`），所以 `session_start` 之前的工作都无效。
4. **按配置激活 codemode 或 tool_search**（§4.19）。此时服务器还没连上，只看配置里的 exposure。
5. **后台连接。** MCP 运行时（`runtime.ts`，它导入 pi-mcp）通过 `loadMcpRuntime = () => import("./runtime.ts")`（`packages/coding-agent/src/extensions/mcp/runtime.lazy.ts:2`）懒加载：没有配置服务器的会话不加载 pi-mcp。`setImmediate` 让出一轮事件循环，让界面先画出来。每个启用的服务器调用 `startConnection`（§4.13），结果存在 `pending` 里，不等待。全部结束后，`reportProblems` 一次性报告失败或需要登录的服务器。

`loadMcpConfig` 先读全局文件，项目被信任时再读项目文件：

源码：[`packages/coding-agent/src/extensions/mcp/config.ts:146-157`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/config.ts#L146-L157)

```ts
export function loadMcpConfig(options: { agentDir: string; cwd: string; projectTrusted: boolean }): LoadedMcpConfig {
	const state: McpConfigState = { servers: new Map(), errors: [] };
	readConfigFile(join(options.agentDir, "mcp.json"), "global", state);
	const projectConfig = options.projectTrusted ? join(options.cwd, CONFIG_DIR_NAME, "mcp.json") : undefined;
	if (projectConfig) readConfigFile(projectConfig, "project", state);
	return {
		servers: [...state.servers.values()],
		...(state.autoEnableCodemode === undefined ? {} : { autoEnableCodemode: state.autoEnableCodemode }),
		errors: state.errors,
		...(projectConfig ? { projectConfig } : {}),
	};
}
```

两个文件写进同一个 `Map`，后读的项目文件覆盖同名的全局服务器。`readConfigFile`（`packages/coding-agent/src/extensions/mcp/config.ts:92-140`）对每个服务器做这几项检查，任何一项失败都只是往 `errors` 里加一条，跳过这个服务器：

- 项目文件里一个没有 `command`、`url`、`type` 的条目是"覆盖"：只能设置 `enabled`、`exposure`、`toolExposure` 三个键，合并到同名全局服务器上（`packages/coding-agent/src/extensions/mcp/config.ts:109-122`）。这样项目可以关掉一个全局服务器，但不能改它的命令或凭据。
- `validateMcpServerConfig`（`packages/coding-agent/src/core/mcp-servers.ts:260`）检查名字和字段类型，拒绝旧的 SSE 传输。
- 只差 `-` 和 `_` 的两个名字（`my-server` 和 `my_server`）会得到同一个工具名前缀，第二个被拒绝（`packages/coding-agent/src/extensions/mcp/config.ts:128-133`）。
- 项目文件不能使用 `auth`（把 pi 登录的 token 发给服务器），否则一个仓库就能决定把用户的凭据发到哪里（`packages/coding-agent/src/extensions/mcp/config.ts:134-137`）。

§3.5 的 `config` 实验是这个函数的真实输出。

**往哪走**：§4.13 `startConnection`。

### 4.13 startConnection() 和 createConnection()

**调用位置**：`session_start` → `startConnection(server, runtime)`；服务器在会话中途注册（`mcp_servers_change` 事件）或在 `/mcp` 里被启用时也调用它。

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:562-608`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L562-L608)

```ts
		const createConnection = async (
			server: McpServer,
			isCurrent: () => boolean,
		): Promise<McpServerConnection | undefined> => {
			const runtime = await loadMcpRuntime();
			if (!isCurrent()) return undefined;
			const connection = new runtime.McpServerConnection({
				entry: server.entry,
				cwd: sessionCwd,
				createTransport: options.createTransport ?? runtime.createDefaultTransport,
				credentials: getCredentials(runtime),
				providerToken: async (provider) => modelRegistry?.getApiKeyForProvider(provider),
				log: getServerLog(runtime),
				onTools: (connection) => {
					if (isCurrent()) registerTools(connection);
				},
				onChange: (connection) => {
					if (isCurrent()) onConnectionChange(connection);
					else tokensAtSignIn.delete(connection);
				},
			});
			server.connection = connection;
			emitChange();
			return connection;
		};

		/**
		 * Connect the server in the background. `server.ready` settles when it connected or failed;
		 * failures show in its state. It starts after `after`, unless disabled, replaced, or shut down
		 * meanwhile. The returned promise rejects when the MCP runtime cannot be loaded.
		 */
		const startConnection = (server: McpServer, after?: Promise<unknown>): Promise<void> => {
			const { signal } = session;
			const attempt = Symbol();
			server.attempt = attempt;
			const isCurrent = () =>
				!signal.aborted && server.attempt === attempt && isEnabled(server) && servers.includes(server);
			const ready = (async () => {
				await after;
				if (!isCurrent()) return;
				const connection = await createConnection(server, isCurrent);
				if (!connection || !isCurrent()) return;
				await connection.getClient().catch(() => undefined);
			})();
			server.ready = ready.catch(() => undefined);
			return ready;
		};
```

**讲解**

扩展闭包里，每个服务器是一个 `McpServer` 对象（`packages/coding-agent/src/extensions/mcp/index.ts:102-115`）：配置 `entry`、连接 `connection`、本次连接尝试的标识 `attempt`，以及 `ready`，一个在连接成功或失败时 settle 的 Promise。后面的 `before_agent_start` 和 `tool_call` 处理函数靠等 `ready` 来等服务器（§4.20、§4.21）。

`startConnection` 的关键是 `isCurrent`。连接是异步的，可能要几秒，这期间会话可能结束、服务器可能在 `/mcp` 里被关掉或重新启用（启用会再调一次 `startConnection`，生成新的 `attempt`）。每个异步步骤之后都检查 `isCurrent()`，过时的尝试直接放弃，它的回调（`onTools`、`onChange`）也不再生效。用 `Symbol()` 做标识，保证每次尝试都不相等。

`ready` 吞掉了 `getClient()` 的异常：连接失败不是 `ready` 的失败，失败原因记在 `connection.state` 和 `connection.error` 里（§4.14）。

`createConnection` 把扩展的状态和回调注入 `McpServerConnection`：

- `createTransport`：默认是 `createDefaultTransport`（§4.14）。测试可以通过 `createMcpExtension({ createTransport })` 换成内存传输。
- `onTools`：服务器连上或工具清单变化时调用，注册工具（§4.15）。
- `onChange`：状态变化时调用，用于 `/mcp` 界面和 OAuth 登录检测。

**往哪走**：§4.14 `McpServerConnection` 怎么建立连接。

### 4.14 McpServerConnection：getClient()、connectOnce()、createDefaultTransport()

**调用位置**：`startConnection` → `connection.getClient()`；之后每次工具调用 → `callTool()` → `withClient()` → `getClient()`（§4.22）。

源码：[`packages/coding-agent/src/extensions/mcp/runtime.ts:237-244`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/runtime.ts#L237-L244)

```ts
	getClient(): Promise<McpClient> {
		if (this.closed) return Promise.reject(new Error(`MCP server "${this.entry.name}" is shut down`));
		if (this.client?.connectionState === "connected") return Promise.resolve(this.client);
		this.opening ??= this.open().finally(() => {
			this.opening = undefined;
		});
		return this.opening;
	}
```

`getClient` 有三种结果：已关闭就失败；已连接就直接返回客户端；否则开始连接。`opening ??=` 保证同时有多个调用时只连接一次，大家等同一个 Promise。连接断开后 `this.client` 被清空（§4.22），下一次调用自动重连。这就是"懒重连"：连接断了不立即重连，等有人要用时再连。

`open()`（`packages/coding-agent/src/extensions/mcp/runtime.ts:340-357`）把状态设为 `connecting`，调用 `connectOnce()`。HTTP 服务器遇到网络错误或 408、429、5xx 时，分别在 250ms 和 1000ms 后再试两次（`CONNECT_RETRY_DELAYS_MS`，`packages/coding-agent/src/extensions/mcp/runtime.ts:51`）；stdio 服务器不重试。

源码：[`packages/coding-agent/src/extensions/mcp/runtime.ts:359-407`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/runtime.ts#L359-L407)

```ts
	private async connectOnce(): Promise<McpClient> {
		const client = new McpClient({
			name: "pi",
			version: VERSION,
			requestTimeoutMs: this.timeoutMs,
			roots: [{ uri: pathToFileURL(this.cwd).href, name: basename(this.cwd) }],
		});
		const log = this.log;
		if (log) client.onNotification("notifications/message", (params) => log.write(this.entry.name, params));
		// Closing the client fails its pending initialize or discovery requests and closes the transport.
		// Memoized: a second `client.close()` returns before the first finished closing the transport.
		let closing: Promise<void> | undefined;
		const closeClient = () => {
			closing ??= client.close().catch(() => undefined);
			return closing;
		};
		this.shutdown.signal.addEventListener("abort", closeClient, { once: true });
		let transport: McpTransport | undefined;
		try {
			transport = this.createTransport(this.entry, this.cwd, this.authProvider);
			await client.connect(transport);
			client.onNotification("notifications/tools/list_changed", () => {
				void this.refreshTools(client);
			});
			const stdio = transport instanceof StdioTransport ? transport : undefined;
			client.onClose(() => this.handleClientClose(client, stdio));
			// Servers without the tools capability (prompts or resources only) do not answer tools/list.
			const tools = client.serverCapabilities?.tools ? await client.listTools() : [];
			if (this.closed) throw new Error("shut down while connecting");
			if (client.connectionState !== "connected") throw new Error("connection closed during setup");
			this.client = client;
			this.tools = tools;
			this.hasResources = client.serverCapabilities?.resources !== undefined;
			this.instructions = client.instructions?.trim() || undefined;
			this.state = "connected";
			this.error = undefined;
			this.onTools(this);
			this.changed();
			return client;
		} catch (error) {
			await closeClient();
			if (transport instanceof StdioTransport) {
				this.stderrTail = transport.stderr.trim().slice(-STDERR_TAIL_CHARS) || undefined;
			}
			throw error;
		} finally {
			this.shutdown.signal.removeEventListener("abort", closeClient);
		}
	}
```

**讲解**

这是 coding-agent 和 pi-mcp 的交界处。每次连接都新建一个 `McpClient`（§4.1 说过，一个客户端只能连接一次）：

- `name: "pi"`、`version: VERSION`：服务器在 `initialize` 里看到的客户端信息。
- `requestTimeoutMs`：服务器配置的 `timeout` 乘以 1000，默认 60 秒（`timeoutMs` getter，`packages/coding-agent/src/extensions/mcp/runtime.ts:210-212`）。
- `roots`：会话的工作目录，作为 `file://` URI。服务器可以用 `roots/list` 问客户端"我该在哪个目录工作"。

连接成功后，按顺序：

1. 监听 `notifications/tools/list_changed`。服务器宣布工具清单变了，就重新 `listTools`，再调用 `onTools`（`refreshTools`，`packages/coding-agent/src/extensions/mcp/runtime.ts:430-440`）。
2. 监听关闭，连接断开时进入 `disconnected`（§4.22）。
3. 服务器在 `initialize` 里声明了 `tools` 能力才调用 `listTools()`。只提供资源的服务器不回答 `tools/list`。
4. 两次检查：连接期间会话结束了吗？`listTools` 期间连接断了吗？
5. 保存客户端、工具、`instructions`，状态变为 `connected`，调用 `onTools(this)`，触发工具注册（§4.15）。

失败时关闭客户端（也就关闭了传输、结束了子进程）。stdio 服务器还会把 stderr 的最后 2000 个字符保存下来，`connectFailed`（`packages/coding-agent/src/extensions/mcp/runtime.ts:409-418`）把它附在错误信息后面，用户在 `/mcp` 里能看到服务器崩溃前打印了什么。

`shutdown` 控制器在 `McpServerConnection.close()` 时中止，`closeClient` 挂在它上面：会话结束时正在进行的连接会被立即关闭，不用等 `initialize` 超时。

传输由 `createDefaultTransport` 创建：

源码：[`packages/coding-agent/src/extensions/mcp/runtime.ts:98-122`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/runtime.ts#L98-L122)

```ts
export function createDefaultTransport(
	entry: McpServerEntry,
	cwd: string,
	authProvider: AuthProvider | undefined,
): McpTransport {
	const { config, name } = entry;
	if ("url" in config) {
		return new StreamableHttpTransport({
			url: config.url,
			headers: resolveHeadersOrThrow(config.headers, `MCP server "${name}"`),
			authProvider,
		});
	}
	const env: Record<string, string> = {};
	for (const [key, value] of Object.entries(config.env ?? {})) {
		env[key] = resolveConfigValueOrThrow(value, `MCP server "${name}" env "${key}"`);
	}
	return new StdioTransport({
		command: expandHome(config.command),
		args: config.args?.map(expandHome),
		cwd: resolve(cwd, expandHome(config.cwd ?? ".")),
		env,
		stderr: "pipe",
	});
}
```

有 `url` 就用 HTTP 传输，否则用 stdio 传输。`env` 和 `headers` 的值在这里才解析：`${NAME}` 引用环境变量，`!cmd` 运行命令取输出。解析放在连接时而不是读配置时，所以 token 轮换后重连就能拿到新值。`cwd` 相对于会话工作目录，`~` 展开为家目录。`authProvider` 在 `McpServerConnection` 构造时创建（`packages/coding-agent/src/extensions/mcp/runtime.ts:185-199`）：没有 `Authorization` 头也没有 `auth` 的 HTTP 服务器用 OAuth，有 `auth.provider` 的用 pi 登录的 token，stdio 服务器没有。

**往哪走**：§4.15 `onTools` 触发的 `registerTools`。

### 4.15 registerTools() 和 createMcpToolName()

**调用位置**：`McpServerConnection.connectOnce()` 或 `refreshTools()` → `onTools(connection)` → `registerTools(connection)`（§4.13 注入）。

源码：[`packages/coding-agent/src/extensions/mcp/tools.ts:78-93`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/tools.ts#L78-L93)

```ts
/**
 * `mcp__<server>__<tool>`, sanitized and shortened with a hash suffix when too long. Like Codex,
 * everything but `[A-Za-z0-9_]` becomes `_`, so the name is also the identifier codemode scripts
 * call it by. `isTaken` reports names used by a different MCP tool: sanitizing can map two tools to
 * one name (`a-b` and `a_b`), which then get the hash suffix.
 */
export function createMcpToolName(
	server: string,
	tool: string,
	isTaken: (name: string) => boolean = () => false,
): string {
	const name = `mcp__${server}__${tool}`.replace(/[^A-Za-z0-9_]/g, "_");
	if (name.length <= MAX_TOOL_NAME_LENGTH && !isTaken(name)) return name;
	const hash = createHash("sha256").update(`${server}\0${tool}`).digest("hex").slice(0, 8);
	return `${name.slice(0, MAX_TOOL_NAME_LENGTH - hash.length - 1)}_${hash}`;
}
```

**讲解**

工具名的规则有三条：

- 格式是 `mcp__<server>__<tool>`。双下划线把服务器名和工具名隔开，`isMcpToolName` 靠 `mcp__` 前缀识别 MCP 工具。
- 除字母、数字、`_` 以外的字符都换成 `_`。各家 provider 只接受 `[A-Za-z0-9_-]`，而换掉 `-` 之后，名字还是一个合法的 JavaScript 标识符，codemode 脚本可以写 `tools.mcp__demo__echo(...)`。
- 超过 64 个字符（`MAX_TOOL_NAME_LENGTH`，`packages/coding-agent/src/extensions/mcp/tools.ts:49`），或者名字已被另一个工具占用时，截断并加 8 位哈希后缀。哈希来自原始的服务器名和工具名，所以同一个工具每次得到同一个名字。

`m10-client.ts names` 演示了四种情况：

```bash
node --conditions=source --experimental-strip-types m10-client.ts names
```

```
demo / echo → mcp__demo__echo
my-server / get.item → mcp__my_server__get_item
github / search_code_in_every_repository_of_the_whole_organization → mcp__github__search_code_in_every_repository_of_the_who_18cd40db
demo / a-b（mcp__demo__a_b 已被 a_b 占用）→ mcp__demo__a_b_108b2fb7
```

`registerTools` 为服务器的每个工具分配名字、创建定义、注册：

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:387-417`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L387-L417)

```ts
		const registerTools = (connection: McpServerConnection) => {
			const server = connection.entry.name;
			const entry = findServer(server)?.entry ?? connection.entry;
			const description = entry.config.description?.trim();
			const namespace = {
				name: mcpNamespace(server),
				// `describeNamespace("docs")` finds `mcp__docs`.
				aliases: [server],
				...(description ? { description } : {}),
				...(connection.instructions ? { instructions: connection.instructions } : {}),
			};
			const previous = serverTools.get(server) ?? new Set<string>();
			const current = new Set<string>();
			// Like Codex, all tools whose names sanitize to the same name get the hash suffix, so which
			// one would keep the plain name does not depend on the order of the list.
			const plain = [...new Set(connection.tools.map((tool) => tool.name))].map((tool) =>
				createMcpToolName(server, tool),
			);
			const assignName = (tool: string, owner: string) => {
				const name = createMcpToolName(server, tool, (candidate) => {
					const existing = toolOwners.get(candidate);
					return (
						(existing !== undefined && existing !== owner) ||
						current.has(candidate) ||
						plain.indexOf(candidate) !== plain.lastIndexOf(candidate)
					);
				});
				toolOwners.set(name, owner);
				current.add(name);
				return name;
			};
```

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:418-454`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L418-L454)

```ts
			for (const tool of connection.tools) {
				const definition = createMcpToolDefinition({
					server,
					tool,
					name: assignName(tool.name, `${server}\0${tool.name}`),
					exposure: getMcpToolExposure(entry.config, tool.name),
					namespace,
					timeoutMs: connection.timeoutMs,
					getClient: async () => {
						// A prepared call can outlive this definition's connection while the readiness
						// hook waits for a disable/re-enable. Resolve the replacement at execution time.
						const current = findServer(server);
						if (!current || !isEnabled(current)) throw new Error(`MCP server "${server}" is disabled.`);
						const client = current.connection;
						if (!client) throw new Error(`MCP server "${server}" is still starting.`);
						if (
							getMcpToolExposure(current.entry.config, tool.name) === "hidden" ||
							(client.state === "connected" && !client.tools.some((offered) => offered.name === tool.name))
						) {
							throw new Error(`MCP tool "${server}/${tool.name}" is no longer available.`);
						}
						return client;
					},
					readableResources: () => resourceServers().some((current) => current.name === server),
				});
				definitions.set(definition.name, definition);
				pi.registerTool(definition);
			}
			serverTools.set(server, current);
			// Tools cannot be unregistered, so tools the server dropped are re-registered as hidden. When
			// the server offers them again they are registered with their configured exposure above.
			for (const name of previous) {
				const definition = definitions.get(name);
				if (!current.has(name) && definition) pi.registerTool({ ...definition, exposure: "hidden" });
			}
			syncResourceTools();
		};
```

**讲解**

几个要点：

- **命名空间。** 同一个服务器的工具共用一个 `ToolNamespace`：名字 `mcp__demo`，别名 `demo`，说明取配置的 `description`，`instructions` 取服务器在 `initialize` 里给的说明。codemode 的 `describeNamespace()` 和 tool_search 的排序用它（§4.19）。
- **名字稳定。** `toolOwners` 记录每个 pi 工具名属于哪个 `<server>\0<tool>`。同一个工具重新注册（例如重连后）得到同一个名字；名字被别的工具占用时才加哈希。`plain` 处理 `a-b` 和 `a_b` 这种清洗后同名的情况：两个都加哈希，而不是先到的那个保留原名，这样结果不依赖服务器列表的顺序。
- **`getClient` 是延迟的。** 工具定义不直接持有 `McpServerConnection`，而是在执行时用 `findServer(server)` 现找。从 `tool_call` 处理函数等待服务器（§4.21）到真正执行之间，服务器可能被关掉又打开，换了一个新连接对象。执行时还检查工具是否已被隐藏、服务器是否还提供它。
- **工具不能注销。** `ExtensionAPI` 没有 `unregisterTool`。服务器不再提供某个工具时，把它重新注册成 `exposure: "hidden"`：名字还在，但模型看不到，也不能调用。

`pi.registerTool()`（`packages/coding-agent/src/core/extensions/loader.ts:292-304`）把定义存进扩展的工具表，然后调用 `runtime.refreshTools()`。扩展绑定到会话后，它就是 `AgentSession._refreshToolRegistry()`（`packages/coding-agent/src/core/agent-session.ts:3507`，→ M8）。会话重建工具表，决定哪些工具激活，激活集合的变化在下一次请求前由 `declareToolChanges` 写进 transcript（→ M2 §4.4）。

`registerTools` 最后调用 `syncResourceTools`（`packages/coding-agent/src/extensions/mcp/index.ts:480`），服务器有资源时注册 `list_mcp_resources` 等三个资源工具（`resources.ts`，本模块略）。

**往哪走**：§4.16 `createMcpToolDefinition`。

### 4.16 createMcpToolDefinition()

**调用位置**：`registerTools()` 对每个工具调用一次。

源码：[`packages/coding-agent/src/extensions/mcp/tools.ts:312-351`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/tools.ts#L312-L351)

```ts
export function createMcpToolDefinition(options: {
	server: string;
	tool: McpTool;
	name: string;
	exposure: McpExposure;
	namespace: ToolNamespace;
	timeoutMs: number;
	getClient: () => Promise<McpToolCaller>;
	/** Whether `read_mcp_resource` can read the server's resources. */
	readableResources?: () => boolean;
}): ToolDefinition<TSchema, McpToolDetails> {
	const { server, tool } = options;
	const title = tool.title ?? tool.annotations?.title;
	const annotations = toToolAnnotations(tool);
	const label = `${server}/${tool.name}`;
	return {
		name: options.name,
		label,
		description: tool.description?.trim() || title || `MCP tool ${tool.name} from server ${server}`,
		parameters: toParameters(tool.inputSchema),
		outputSchema: createMcpResultSchema(tool.outputSchema),
		exposure: toToolExposure(options.exposure),
		namespace: options.namespace,
		...(annotations ? { annotations } : {}),
		...createMcpToolRenderers(label),
		async execute(_toolCallId, params, signal, onUpdate) {
			const client = await options.getClient();
			const result = await client.callTool(tool.name, (params ?? {}) as Record<string, unknown>, {
				signal,
				timeoutMs: options.timeoutMs,
				onProgress: (progress) => {
					const total = progress.total === undefined ? "" : `/${progress.total}`;
					const text = progress.message ?? `Progress ${progress.progress}${total}`;
					onUpdate?.({ content: [{ type: "text", text }], details: { server, tool: tool.name } });
				},
			});
			return convertMcpResult(server, tool.name, result, { readableResources: options.readableResources?.() });
		},
	};
}
```

**讲解**

这个函数把 MCP 的 `Tool`（§3.4）变成 pi 的 `ToolDefinition`（§3.6）。§3.6 的 `wrap` 实验打印的就是它的返回值。字段的来源：

- `parameters`：`toParameters`（`packages/coding-agent/src/extensions/mcp/tools.ts:292-298`）补上缺失的 `type: "object"` 和 `properties: {}`。有些 provider 拒绝没有 `properties` 的对象 schema，而 MCP 服务器经常省略它，例如 `stdio-server.mjs` 的 `{"type":"object"}`。
- `outputSchema`：`createMcpResultSchema` 生成一个描述 `CallToolResult` 的 schema（`content`、`structuredContent`、`isError` 三个属性，§3.6 输出里能看到）。服务器的 `outputSchema` 放在 `structuredContent` 属性下面，里面的 `$ref` 被改写到新位置（`relocateSchema`，`packages/coding-agent/src/extensions/mcp/tools.ts:135`）。所有 MCP 工具都声明 `outputSchema`，所以 codemode 脚本调用它们时拿到的是 `structuredContent`，也就是整个 `CallToolResult`，而不是文本。
- `annotations`：`toToolAnnotations`（`packages/coding-agent/src/extensions/mcp/tools.ts:303-310`）只保留四个布尔提示。权限扩展可以通过 `pi.getAllTools()` 读到它们（§7.2）。

`execute` 的四个参数和 `AgentTool.execute` 一样（M2 §3.2）：

- `signal`：agent 循环的中止信号，原样传给 `callTool`。用户按 Esc 中止时，`McpClient` 会给服务器发 `notifications/cancelled`（§4.7）。
- `onUpdate`：进度回调。服务器的每个进度通知变成一次 `onUpdate`，agent 循环据此发出 `tool_execution_update` 事件（M2 §4.12）。传了 `onProgress`，所以进度通知还会重置超时。
- 工具名：发给服务器的是原始的 `tool.name`（`echo`），不是 pi 的名字（`mcp__demo__echo`）。

`execute` 不捕获异常。`callTool` 超时、连接断开、服务器回答 JSON-RPC `error`，都会抛出，由 agent 循环变成 `isError: true` 的工具结果（M2 §4.12）。

**往哪走**：§4.17 `convertMcpResult`。

### 4.17 convertMcpResult()

**调用位置**：MCP 工具的 `execute()` → `convertMcpResult()`。

源码：[`packages/coding-agent/src/extensions/mcp/tools.ts:265-286`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/tools.ts#L265-L286)

```ts
/** Convert an MCP result. `isError` results become error results that keep the structured result. */
export async function convertMcpResult(
	server: string,
	tool: string,
	result: CallToolResult,
	options: ConvertMcpResultOptions = {},
): Promise<AgentToolResult<McpToolDetails>> {
	// Without content blocks, toLlmContent falls back to the structured content as JSON.
	const converted: (TextContent | ImageContent)[] =
		result.content.length > 0 ? await toModelContent(server, result.content, options) : toLlmContent(result);
	if (result.isError && textOf(converted) === "") {
		converted.push({ type: "text", text: `MCP tool ${server}/${tool} returned an error` });
	}
	const { content, fullOutputPath } = await limitMcpContent(converted, options.saveOutput);
	const { _meta: _ignored, ...scriptResult } = result;
	return {
		content,
		details: { server, tool, ...(fullOutputPath ? { fullOutputPath } : {}) },
		structuredContent: scriptResult as unknown as JsonValue,
		...(result.isError ? { isError: true } : {}),
	};
}
```

**讲解**

输入是协议的 `CallToolResult`，输出是 M2 的 `AgentToolResult`。一份结果分给三类读者：

| 字段 | 读者 | 内容 |
|---|---|---|
| `content` | 模型 | 文本和图片，超过 20KB 时截断 |
| `details` | 界面 | `{ server, tool }`，截断时加 `fullOutputPath` |
| `structuredContent` | codemode 脚本 | 去掉 `_meta` 的完整 `CallToolResult`，不截断 |

步骤：

1. **转换内容块。** `toModelContent`（`packages/coding-agent/src/extensions/mcp/tools.ts:257-263`）对每个块调用 `blockToContent`（`packages/coding-agent/src/extensions/mcp/tools.ts:224-254`）。和 pi-mcp 的 `toLlmContent`（§4.8）相比，它改了两种块：资源链接变成一段提示，告诉模型用 `read_mcp_resource` 工具去读；非图片的二进制资源存成临时文件，告诉模型文件路径（文本类 MIME 直接解码成文本）。其余块交给 `toLlmContent`。
2. **错误结果不能没有文字。** `isError` 而没有任何文本时，补一句 `MCP tool <server>/<tool> returned an error`，否则模型只看到一个空的错误结果。
3. **截断。** `limitMcpContent`（`packages/coding-agent/src/extensions/mcp/tools.ts:180-201`）把所有文本拼起来，超过 20KB（`MCP_OUTPUT_MAX_BYTES`）时用 `truncateMiddle`（`packages/coding-agent/src/core/tools/truncate.ts:292`，→ M3）保留头尾、去掉中间，完整文本存进临时文件，路径写在结果末尾。
4. **`isError`。** 服务器的 `isError: true` 变成 `AgentToolResult.isError: true`。按 `AgentTool` 的约定（M2 §3.2），这是"报告失败但不抛异常"：模型看到错误结果，`structuredContent` 仍然保留，codemode 脚本里的调用照常 resolve。

§3.6 输出的两行 `execute()` 结果就是这个函数的输出。

**往哪走**：§4.18 定义里的 `exposure` 字段是怎么来的。

### 4.18 exposure 的换算：McpExposure → ToolExposure

**调用位置**：`registerTools()` → `getMcpToolExposure(entry.config, tool.name)` → `createMcpToolDefinition()` → `toToolExposure()`。

MCP 配置里的 exposure 有四个取值：

源码：[`packages/coding-agent/src/core/mcp-servers.ts:43-52`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/mcp-servers.ts#L43-L52)

```ts
/**
 * - `codemode`: tools are callable from codemode scripts but neither declared to the model nor
 *   listed in the codemode description, which lists only the server's namespace. Scripts find them
 *   with `searchTools()`. `codemode-deferred` is accepted as an alias.
 * - `deferred`: not declared to the model until the `tool_search` tool loads them; the model then
 *   calls them directly. Does not need codemode.
 * - `direct`: tools are declared to the model like any other tool (and callable from codemode).
 * - `hidden`: tools are registered but unreachable.
 */
export type McpExposure = "codemode" | "deferred" | "direct" | "hidden";
```

一个工具用哪个取值，由 `getMcpToolExposure` 决定：

源码：[`packages/coding-agent/src/core/mcp-servers.ts:245-254`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/mcp-servers.ts#L245-L254)

```ts
/** Exposure of one tool of a server: its `toolExposure` entry, else the server's `exposure`. */
export function getMcpToolExposure(config: McpServerConfig, toolName: string): McpExposure {
	const overrides = config.toolExposure ?? {};
	const exact = overrides[toolName];
	if (exact !== undefined) return exact;
	for (const [pattern, exposure] of Object.entries(overrides)) {
		if (pattern.includes("*") && toolPatternRegExp(pattern).test(toolName)) return exposure;
	}
	return config.exposure ?? "codemode";
}
```

先找 `toolExposure` 里的精确名字，再按顺序找第一个匹配的 `*` 模式，最后用服务器的 `exposure`，都没有时默认 `codemode`。

pi 的工具系统（M7 的 `ToolDefinition`）有自己的一套 exposure，比 MCP 的多一个 `model-only`：

源码：[`packages/coding-agent/src/core/extensions/types.ts:521-536`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/types.ts#L521-L536)

```ts
/**
 * How the model reaches a tool. "Callable" means callable from other tools through
 * `ctx.executeTool()`, as the `codemode` tool does.
 *
 * - `direct`: declared to the model while active, and callable while active.
 * - `model-only`: declared to the model while active, never callable. Use it for orchestrating or
 *   interactive tools.
 * - `codemode`: callable whenever registered. Not declared to the model unless explicitly
 *   activated. Codemode tools list it in their description.
 * - `deferred`: like `codemode`, but codemode tools do not list it; tool search can find it.
 * - `hidden`: registered but unreachable. Activating it has no effect.
 *
 * `direct` and `model-only` tools are activated when they are registered; the others are not.
 * The active tool set (`getActiveTools`/`setActiveTools`) is the set declared to the model.
 */
export type ToolExposure = "direct" | "model-only" | "codemode" | "deferred" | "hidden";
```

MCP 的 exposure 这样换算成 pi 的：

源码：[`packages/coding-agent/src/extensions/mcp/tools.ts:40-46`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/tools.ts#L40-L46)

```ts
/**
 * Tool exposure of an MCP exposure. `codemode` and `deferred` both leave tools out of the codemode
 * description; they differ only in which tool the MCP extension activates to reach them.
 */
export function toToolExposure(exposure: McpExposure): ToolExposure {
	return exposure === "codemode" ? "deferred" : exposure;
}
```

**讲解**

同一个词 `codemode` 在两套里意思不同。pi 的 `codemode` 表示"在 codemode 工具的说明里列出来"；MCP 的 `codemode` 表示"从 codemode 脚本调用，但不在说明里列出"，换算成 pi 的 `deferred`。所以 MCP 的 `codemode` 和 `deferred` 在 pi 工具层面都是 `deferred`，它们的区别只在 MCP 扩展激活哪个工具去够到它们：前者激活 `codemode`，后者激活 `tool_search`（§4.19）。

pi 的 exposure 决定工具在会话里的两个属性（→ M8）：

- **是否声明给模型。** "激活"的工具集合就是声明给模型的工具集合。`_isActivatedOnRegistration` 规定只有 `direct` 和 `model-only` 在注册时自动激活：

源码：[`packages/coding-agent/src/core/agent-session.ts:3676-3685`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3676-L3685)

```ts
	/** Whether activating the tool declares it to the model. */
	private _isDeclarable(name: string): boolean {
		const exposure = this._getToolExposure(name);
		return exposure === "direct" || exposure === "model-only";
	}

	/** Whether registering the tool activates it, which declares it to the model. */
	private _isActivatedOnRegistration(name: string): boolean {
		return this._isDeclarable(name) && this._toolDefinitions.get(name)?.definition.defaultActive !== false;
	}
```

- **是否可以被其他工具调用。** codemode 脚本通过 `ctx.executeTool()` 调用其他工具，能调用的范围由 `_getCallableTools` 规定：

源码：[`packages/coding-agent/src/core/agent-session.ts:1594-1603`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1594-L1603)

```ts
	/**
	 * Tools callable through `ctx.executeTool()`: the active `direct` tools and every registered
	 * `codemode` or `deferred` tool.
	 */
	private _getCallableTools(active: ReadonlySet<string> = new Set(this.getActiveToolNames())): AgentTool[] {
		return [...this._toolRegistry.values()].filter((tool) => {
			const exposure = this._getToolExposure(tool.name);
			return exposure === "codemode" || exposure === "deferred" || (exposure === "direct" && active.has(tool.name));
		});
	}
```

合起来，一个 MCP 工具的四种 exposure 是：

| MCP `exposure` | pi `ToolExposure` | 注册时激活（声明给模型） | codemode 脚本能调用 | 模型怎么够到它 |
|---|---|---|---|---|
| `direct` | `direct` | 是 | 是（激活时） | 直接调用 |
| `deferred` | `deferred` | 否 | 是 | 先调 `tool_search`，搜到后它被激活，下一次请求起直接调用 |
| `codemode`（默认） | `deferred` | 否 | 是 | 调 `codemode`，在脚本里调用 |
| `hidden` | `hidden` | 否 | 否 | 够不到 |

§7.1 用同一个服务器、三种 exposure 各跑一次，验证这张表。

**往哪走**：§4.19 MCP 扩展怎么激活 `tool_search` 和 `codemode`。

### 4.19 ensureDiscoveryActive() 和 tool_search

**调用位置**：`session_start`（§4.12）、`mcp_servers_change`、`/mcp` 改变 exposure 或登录成功之后 → `ensureDiscoveryActive(ctx)`。

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:495-536`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L495-L536)

```ts
		/**
		 * Tools that are not declared to the model are reached through the codemode tool (scripts call
		 * them) or the tool_search tool (it declares them). Either reaches every such tool. Activate the
		 * one the tools' exposure asks for: codemode for `codemode` unless `autoEnableCodemode` is false,
		 * tool_search for `deferred`.
		 */
		const ensureDiscoveryActive = (ctx: ExtensionContext) => {
			// From the config, so the tool is active before the servers connect. Resource tools share
			// their server's exposure.
			const exposures = new Set<McpExposure>();
			for (const server of servers) {
				if (isEnabled(server)) for (const exposure of configuredExposures(server.entry)) exposures.add(exposure);
			}
			const needsCodemode = exposures.has("codemode");
			const needsToolSearch = exposures.has("deferred");
			if (!needsCodemode && !needsToolSearch) return;
			// Only a tool of these names that declares `reachesTools` can reach MCP tools; another
			// extension's tool of the same name without it is never activated.
			const tools = pi.getAllTools();
			const hasReachingTool = (name: string) => tools.some((tool) => tool.name === name && tool.reachesTools);
			const hasCodemode = hasReachingTool(CODEMODE_TOOL_NAME);
			const hasToolSearch = hasReachingTool(TOOL_SEARCH_TOOL_NAME);
			const active = pi.getActiveTools();
			const activate: string[] = [];
			if (needsCodemode && hasCodemode && autoEnableCodemode && !active.includes(CODEMODE_TOOL_NAME)) {
				activate.push(CODEMODE_TOOL_NAME);
			}
			if (needsToolSearch && hasToolSearch && !active.includes(TOOL_SEARCH_TOOL_NAME)) {
				activate.push(TOOL_SEARCH_TOOL_NAME);
			}
			if (activate.length > 0) pi.setActiveTools([...active, ...activate]);
			const reachable = [...active, ...activate];
			if (hasCodemode && reachable.includes(CODEMODE_TOOL_NAME)) return;
			if (hasToolSearch && reachable.includes(TOOL_SEARCH_TOOL_NAME)) return;
			if (warnedUnreachable) return;
			warnedUnreachable = true;
			const reason = needsCodemode && hasCodemode && !autoEnableCodemode ? " (autoEnableCodemode is false)" : "";
			ctx.ui.notify(
				`MCP tools are only reachable from the codemode or tool_search tool, but neither is active${reason}; they cannot be called.`,
				"warning",
			);
		};
```

**讲解**

`codemode` 和 `tool_search` 两个工具都由内置扩展注册，并且都是 `defaultActive: false`（`packages/coding-agent/src/extensions/codemode/index.ts:44`、`packages/coding-agent/src/extensions/tool-search/index.ts:14`），所以默认模型看不到它们。`ensureDiscoveryActive` 按配置决定激活哪一个：

- 有 MCP `codemode` 的服务器 → 激活 `codemode`，除非 `mcp.json` 里设了 `"autoEnableCodemode": false`。
- 有 MCP `deferred` 的服务器 → 激活 `tool_search`。
- 两者都不需要（只有 `direct` 或 `hidden`）→ 什么都不做。

判断只看配置（`configuredExposures`，`packages/coding-agent/src/extensions/mcp/index.ts:140-142`），不看服务器是否连上。这样第一次请求就能声明 `codemode` 或 `tool_search`，不必等服务器。§7.1 里 `deferred` 场景第一次请求的 `toolsAdded` 里有 `tool_search`，`codemode` 场景里有 `codemode`。

激活走的是 `pi.setActiveTools()`，和用户用 `--tools` 激活工具是同一个入口。激活集合变化后，下一次请求前 M2 的 `declareToolChanges` 把它写进 transcript。

`hasReachingTool` 要求工具声明了 `reachesTools`。如果另一个扩展替换了内置的 `codemode`，但没有声明 `reachesTools`，MCP 扩展就认为它够不到 MCP 工具，不去激活它。

`tool_search` 被调用时，按查询给"还没激活的 `codemode`/`deferred` 工具"排序，激活排名靠前的：

源码：[`packages/coding-agent/src/extensions/tool-search/tool.ts:284-302`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/tool-search/tool.ts#L284-L302)

```ts
/**
 * Rank the searchable tools that are not active yet and activate the matches, so the next model
 * call declares them. Activation is recorded in the transcript like any tool change.
 */
function searchAndLoad(
	tools: NonNullable<ToolSearchToolOptions["tools"]>,
	query: string,
	limit: number,
): ToolSearchResultTool[] {
	const active = tools.getActiveTools();
	const candidates = tools.getAllTools().filter((tool) => isSearchable(tool.exposure) && !active.includes(tool.name));
	const documents = candidates.map((tool) => createToolSearchDocument(tool, tool.namespace));
	const matches = new Bm25Ranker().rank(query, documents, limit);
	if (matches.length > 0) tools.setActiveTools([...active, ...matches.map((match) => match.name)]);
	return matches.map((match) => ({
		name: match.name,
		description: candidates.find((tool) => tool.name === match.name)?.description ?? "",
	}));
}
```

排序器是 BM25（`Bm25Ranker`，`packages/coding-agent/src/extensions/tool-search/tool.ts:201`），文档由工具名、说明、参数和命名空间的说明组成。搜到的工具同样通过 `setActiveTools` 激活。它们从"下一次请求"起才出现：激活集合的变化在下一次请求前写成一条新的 `system` 消息。§7.1 里能看到这条消息：`system sections=[] toolsAdded=[mcp__demo__echo]`。激活记录在 transcript 里，所以 `/tree` 切换分支、恢复会话之后，这个分支上加载过的工具仍然是声明状态（→ M4）。

**往哪走**：§4.20 每次 prompt 开始时的 `before_agent_start`。

### 4.20 before_agent_start：等 direct 服务器，写 mcp_servers 段

**调用位置**：`AgentSession.prompt()` → `_prepareRun()` → `this._extensionRunner.emitBeforeAgentStart(...)`（`packages/coding-agent/src/core/agent-session.ts:1956`，→ M8 / M6），在构造系统提示词之前。

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:1153-1184`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L1153-L1184)

```ts
		// The first prompt waits for servers whose tools are declared to the model, so they are declared
		// in its first request, but not indefinitely: a slow or hanging server must not hold up the
		// prompt. Other servers are waited for when a script or search needs them (below).
		const waitForDirectServers = async (ctx: ExtensionContext) => {
			if (waitedForStartup) return;
			waitedForStartup = true;
			const ready = servers.flatMap((server) =>
				isEnabled(server) && hasDirectTools(server.entry) && server.ready ? [server.ready] : [],
			);
			if (ready.length === 0) return;
			let timer: NodeJS.Timeout | undefined;
			const finished = await Promise.race([
				Promise.all(ready).then(() => true),
				new Promise<boolean>((resolve) => {
					timer = setTimeout(() => resolve(false), startupWaitMs);
				}),
			]);
			clearTimeout(timer);
			if (!finished) {
				ctx.ui.notify("MCP servers are still connecting; their tools become available once connected.", "info");
			}
		};

		// Every prompt lists the servers in the `mcp_servers` section as they are when it starts. Pi
		// appends the section to the conversation when it changed, for example after a server connected.
		pi.on("before_agent_start", async (event, ctx) => {
			await waitForDirectServers(ctx);
			const { sections } = event.systemPromptOptions;
			const section = renderServersSection(servers);
			if (section) sections[MCP_SERVERS_SECTION] = section;
			else delete sections[MCP_SERVERS_SECTION];
		});
```

**讲解**

这个处理函数做两件事。

**等 direct 服务器。** `direct` 工具要在第一次请求里声明，所以第一次 prompt 要等这些服务器连上，最多 10 秒（`DEFAULT_STARTUP_WAIT_MS`，`packages/coding-agent/src/extensions/mcp/index.ts:93`）。只等一次（`waitedForStartup`）；超时就提示一下，不再等，服务器之后连上时工具会出现在后面的请求里。其他 exposure 的服务器不等，因为它们的工具不在声明里，等到真正要调用时再等（§4.21）。§2.1 的第一次请求就已经有 `mcp__demo__echo`，靠的就是这里的等待。

**写 `mcp_servers` 段。** `codemode` 和 `tool_search` 的工具说明都不列出 MCP 服务器，模型需要从别处知道有哪些服务器。`renderServersSection`（`packages/coding-agent/src/extensions/mcp/index.ts:195-221`）为每个启用的、有非 direct 工具的服务器生成一行：命名空间、怎么够到（`codemode` 或 `tool_search`）、一句说明（配置的 `description`，没有时取服务器 `instructions` 的第一行），整段不超过 4096 个字符。结果放进 `event.systemPromptOptions.sections`，成为系统提示词的一段（→ M6）。

§7.1 的 `deferred` 场景里，这一段是：

```
<mcp_servers>
MCP servers whose tools are not declared to you. Load the tools of `tool_search` servers with `tool_search`.
- mcp__demo (tool_search)
</mcp_servers>
```

`renderServersSection` 返回的文本不带标签，外面的 `<mcp_servers>` 标签是 M6 构造段落时加的（`packages/coding-agent/src/core/system-prompt.ts:189`）。这里没有说明文字，因为服务器还没连上，`instructions` 还没拿到，配置里也没有 `description`。`direct` 场景（§2.1）没有这一段，因为 direct 工具已经声明给模型了。

每次 prompt 都重新生成这一段。内容变了（例如服务器连上后有了 `instructions`），pi 追加一条只含这一段的 `system` 消息，而不是改写第一条 `system` 消息，这样之前的消息仍然能命中 prompt cache（→ M6 `diffSystemPromptSections`）。

**往哪走**：§4.21 工具调用前的 `tool_call`。

### 4.21 tool_call：调用前等服务器就绪

**调用位置**：agent 循环 `prepareToolCall()` → `beforeToolCall` hook（M2 §4.11）→ `AgentSession` 发出扩展的 `tool_call` 事件（→ M7），在参数校验之后、`execute` 之前。所有扩展的 `tool_call` 处理函数都会被调用，内置的 MCP 扩展和权限扩展都在其中。

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:1186-1209`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L1186-L1209)

```ts
		// A tool that reaches other tools (`reachesTools`) waits for the servers it may reach, so their
		// tools are registered before it runs: a codemode script for the servers it names, or for every
		// server when it searches or enumerates tools, and tool_search for every server. The resource
		// tools reach every server, so they wait for all of them. Direct MCP calls wait only for their
		// own server, including a reconnect still closing its old transport.
		pi.on("tool_call", async (event, ctx) => {
			const tool = pi.getAllTools().find((candidate) => candidate.name === event.toolName);
			if (!tool) return;
			// Readiness can be pending while the display state is still connected: reconnect first
			// closes the old transport, then opens a new one. Settled promises return immediately.
			const readyServers = servers.filter((server) => isEnabled(server) && server.ready);
			if (readyServers.length === 0) return;
			let waiting: McpServer[] = [];
			const reach = tool.reachesTools?.(event.input);
			if (reach !== undefined) {
				waiting = readyServers.filter((server) => reachesServer(reach, server.entry.name));
			} else if (RESOURCE_TOOL_NAMES.has(tool.name)) {
				waiting = readyServers;
			} else {
				const owner = toolOwners.get(event.toolName)?.split("\0", 1)[0];
				waiting = readyServers.filter((server) => server.entry.name === owner);
			}
			await waitForServers(waiting, ctx.signal);
		});
```

**讲解**

连接在后台进行（§4.12），所以模型调用一个工具时，相关的服务器可能还没连上。这个处理函数在调用之前等待，等哪些服务器取决于被调用的工具：

| 被调用的工具 | 等待的服务器 |
|---|---|
| 声明了 `reachesTools` 的工具（`codemode`、`tool_search`） | `reachesTools(input)` 说它可能够到的服务器 |
| 三个资源工具 | 所有服务器 |
| MCP 工具本身 | 它所属的服务器，从 `toolOwners` 查到 |
| 其他工具（read、bash…） | 不在 `toolOwners` 里，`waiting` 为空，不等 |

`reachesTools` 是 `ToolDefinition` 上的可选函数（`packages/coding-agent/src/core/extensions/types.ts:641`）。`tool_search` 的实现是 `() => "all"`（`packages/coding-agent/src/extensions/tool-search/tool.ts:322`），因为任何查询都可能命中任何工具。`codemode` 的实现是：

源码：[`packages/coding-agent/src/extensions/codemode/tool.ts:384-392`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/codemode/tool.ts#L384-L392)

```ts
/**
 * The tools a script may call (`ToolDefinition.reachesTools`): every tool when it uses a discovery
 * global, else the words of its source, so `tools.mcp__docs__search` and
 * `tools["mcp__docs__" + name]` both name the `mcp__docs` namespace.
 */
function getScriptReach(code: unknown): "all" | readonly string[] {
	if (typeof code !== "string") return [];
	return DISCOVERY_GLOBALS.test(code) ? "all" : (code.match(/\w+/g) ?? []);
}
```

脚本用了 `searchTools`、`ALL_TOOLS` 这类发现函数就等所有服务器；否则把脚本源码切成单词，`reachesServer`（`packages/coding-agent/src/extensions/mcp/index.ts:227-231`）检查有没有单词包含 `mcp__<server>`。§7.1 的脚本里有 `tools.mcp__demo__echo`，切出的单词 `mcp__demo__echo` 包含 `mcp__demo`，所以只等 `demo`。

`waitForServers`（`packages/coding-agent/src/extensions/mcp/index.ts:611-637`）等这些服务器的 `ready`，也会在 `ctx.signal`（用户中止）时提前返回。等待期间如果又开始了新的连接尝试（`server.ready` 换了），它会接着等新的。

这个处理函数不返回任何东西，所以从不拦截调用。拦截是权限扩展的事：它们订阅同一个 `tool_call` 事件，可以返回 `{ block: true, reason }`（→ M7）。MCP 工具在 pi 里就是普通工具，权限扩展不需要知道 MCP 的存在，只看工具名或 `annotations`。§7.2 验证了这一点，也验证了 codemode 脚本里的嵌套调用同样经过 `tool_call`，事件带 `parentToolCallId`。

**往哪走**：通过 `tool_call` 之后，agent 循环调用 `execute`（§4.16），`execute` 调用 `McpServerConnection.callTool()`（§4.22）。

### 4.22 callTool()、withClient() 和 handleClientClose()

**调用位置**：MCP 工具 `execute()` → `options.getClient()` 返回 `McpServerConnection` → `connection.callTool()`。

源码：[`packages/coding-agent/src/extensions/mcp/runtime.ts:246-248`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/runtime.ts#L246-L248)

```ts
	callTool(name: string, args: Record<string, unknown>, options: McpRequestOptions): Promise<CallToolResult> {
		return this.withClient((client) => client.callTool(name, args, options));
	}
```

源码：[`packages/coding-agent/src/extensions/mcp/runtime.ts:274-301`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/runtime.ts#L274-L301)

```ts
	/**
	 * Run a request, reconnecting when needed. `readOnly` requests are retried once after a transient
	 * HTTP error; tool calls are not, since they may have run.
	 */
	private async withClient<T>(run: (client: McpClient) => Promise<T>, readOnly = false): Promise<T> {
		for (let attempt = 1; ; attempt++) {
			const client = await this.getClient();
			try {
				return await run(client);
			} catch (error) {
				if (readOnly && attempt === 1 && error instanceof McpHttpError && isTransientError(error)) {
					await new Promise((resolve) => setTimeout(resolve, CONNECT_RETRY_DELAYS_MS[0]));
					continue;
				}
				if (error instanceof McpSessionExpiredError && attempt === 1) {
					// The server no longer knows the session (restart, deploy), so it did not run the request.
					// Retry once on a new session. The old client is detached but not closed: closing would
					// fail its other in-flight calls, which instead get the same 404 and retry the same way.
					if (this.client === client) this.client = undefined;
					continue;
				}
				if (!this.needsSignIn(error)) throw error;
				await this.dropClient(client);
				this.markNeedsAuth();
				throw new Error(signInRequiredMessage(this.entry));
			}
		}
	}
```

**讲解**

`withClient` 是所有请求的包装：先 `getClient()`（断开了就重连，§4.14），再执行请求，失败时按错误类型决定是否重试：

| 错误 | 读操作（列资源、读资源） | `callTool` |
|---|---|---|
| 暂时的 HTTP 错误（408、429、5xx，501 除外） | 等 250ms 重试一次 | 不重试，直接抛出 |
| `McpSessionExpiredError`（HTTP 404） | 丢掉旧客户端，在新会话上重试一次 | 同左 |
| 需要登录（OAuth 刷新后仍然 401） | 关闭客户端，状态改为 `needs-auth`，抛出"请运行 /mcp 登录" | 同左 |
| 其他 | 抛出 | 抛出 |

工具调用不在暂时错误后重试，注释给了理由：请求可能已经在服务器上执行了，重试可能让一个有副作用的操作执行两次。会话过期是例外：404 说明服务器根本不认识这个会话，请求没有执行。

连接断开的处理：

源码：[`packages/coding-agent/src/extensions/mcp/runtime.ts:420-428`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/runtime.ts#L420-L428)

```ts
	/** The transport dropped. The next call reconnects; until then the status shows why. */
	private handleClientClose(client: McpClient, stdio: StdioTransport | undefined): void {
		if (this.client !== client || this.closed) return;
		this.client = undefined;
		this.state = "disconnected";
		const stderr = stdio?.stderr.trim().slice(-STDERR_TAIL_CHARS);
		this.error = stderr ? `Connection closed\n${stderr}` : "Connection closed";
		this.changed();
	}
```

stdio 服务器进程退出时，传输层关闭，`McpClient` 通知 `onClose`（§4.9），这里把状态改成 `disconnected`，清掉 `this.client`。当时在途的工具调用以 `McpConnectionClosedError` 失败，变成错误的工具结果。下一次调用时 `getClient()` 发现没有客户端，重新启动服务器。

**往哪走**：§4.23 会话结束。

### 4.23 session_shutdown：关闭所有连接

**调用位置**：会话结束时（pi 退出、`/new`、切换会话）`AgentSession` 发出 `session_shutdown`（→ M7 / M8）。

源码：[`packages/coding-agent/src/extensions/mcp/index.ts:1252-1258`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/index.ts#L1252-L1258)

```ts
		pi.on("session_shutdown", async () => {
			session.abort();
			const closing = connections();
			servers = [];
			emitChange();
			await Promise.all([...backgroundActions, ...closing.map((connection) => connection.close())]);
		});
```

源码：[`packages/coding-agent/src/extensions/mcp/runtime.ts:442-453`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/extensions/mcp/runtime.ts#L442-L453)

```ts
	/** Resolves once no transport of this server is open, including one that was still connecting. */
	async close(): Promise<void> {
		this.shutdown.abort();
		this.state = "closed";
		this.changed();
		const client = this.client;
		this.client = undefined;
		await Promise.all([client?.close().catch(() => undefined), this.opening?.catch(() => undefined)]);
		// A refresh the server already answered may have rotated the refresh token; exiting before the
		// new tokens are saved would lose the grant.
		await this.authProvider?.settled();
	}
```

**讲解**

`session.abort()` 让所有还在进行的连接尝试失效（`isCurrent()` 返回 `false`，§4.13）。然后关闭每个连接：`shutdown.abort()` 关闭正在连接的客户端（§4.14 挂的 `closeClient`），`client.close()` 关闭已连接的客户端，最终走到 `StdioTransport.close()` 结束子进程（§4.9）。最后等 OAuth 的 token 保存完，避免刷新过的 refresh token 丢失。

处理函数是 `async` 的。退出时 `AgentSessionRuntime.dispose()`（`packages/coding-agent/src/core/agent-session-runtime.ts:404`）会 `await emitSessionShutdownEvent(...)`，扩展运行器又会 await 每个处理函数（→ M7），所以连接关闭完成之后会话才被释放。

---

## 5. 状态机

### 5.1 McpClient 的连接状态

```
  ┌──────┐ connect()                  ┌────────────┐ initialize 响应通过检查，    ┌───────────┐
  │ idle │ ─────────────────────────▶ │ connecting │ ──────────────────────────▶ │ connected │
  └──────┘ client.ts:206              └─────┬──────┘ 发出 initialized           └─────┬─────┘
     │                                      │        client.ts:243-244               │
     │ connect() 只允许在 idle 调用           │ 任何一步失败 → close()                 │ close()        client.ts:388
     │ client.ts:205                        │ client.ts:246-248                    │ 传输关闭        client.ts:592
     │                                      ▼                                      ▼
     │                                ┌─────────────────────────────────────────────────┐
     └─────────────────────────────── │ closed  markClosed() client.ts:597              │
          （从 idle 直接 close()）      │ 在途请求 reject McpConnectionClosedError (600)  │
                                      │ onClose 监听器只通知一次 (603-610)               │
                                      └─────────────────────────────────────────────────┘
  requireTransport()：connected 可以发请求；connecting 只允许握手的两条消息 (client.ts:458-463)
```

### 5.2 一个请求的结局

```
               requestInternal()  pending.set(id) (client.ts:436)，armTimeout (439)，send (440-442)
                        │
     ┌──────────────────┼──────────────────────┬──────────────────────┬─────────────────────┐
     │ 收到同 id 的响应    │ 计时器到期              │ 调用方 signal 中止      │ send 失败             │ 连接关闭
     │ handleResponse     │ armTimeout 的回调       │ onAbort               │ .catch               │ rejectPending
     │ (481-490)          │ (556-558)              │ (424-430)             │ (442)                │ (585-590)
     ▼                    ▼                        ▼                       ▼                      ▼
  resolve(result)    reject McpTimeoutError   reject McpAbortError   reject 发送错误      reject ConnectionClosed
  或 reject McpError       │                        │
                           └── 发 notifications/cancelled（initialize 除外）(566-574)
  每条路都先 removePending (577-583)：删表项、清计时器、中止 sendController
  进度通知：handleProgress → armTimeout 重新计时 (541)，请求仍在 pending
```

### 5.3 McpServerConnection 的状态

```
                 构造（state 初值 connecting，runtime.ts:141）
                        │ getClient() → open() (runtime.ts:240, 341)
                        ▼
               ┌──────────────┐ connectOnce 成功 (393)  ┌───────────┐
       ┌─────▶ │ connecting   │ ──────────────────────▶ │ connected │
       │       └──────┬───────┘                         └─────┬─────┘
       │              │ 失败，需要登录 (410-412)                 │ 传输关闭 handleClientClose (424)
       │              │ 失败，其他原因 (414)                     ▼
       │              ▼                                 ┌──────────────┐
       │       ┌────────────┐ ┌──────────┐              │ disconnected │
       │       │ needs-auth │ │ failed   │              └──────┬───────┘
       │       └─────┬──────┘ └────┬─────┘                     │ 下一次 callTool → getClient()
       │             │ 登录后 reconnect()   │ 下一次调用 getClient() │ (runtime.ts:239-243)
       └─────────────┴──────────────┴─────────────────────────┘
  任何状态 close() → closed (runtime.ts:443-446)，之后 getClient() 直接失败 (238)
  调用中 OAuth 仍被拒绝 → needs-auth (withClient，runtime.ts:295-298)
```

### 5.4 一个 MCP 工具从注册到被模型调用

```
 服务器 tools/list 里的 Tool
   │ registerTools → createMcpToolDefinition (index.ts:419) → pi.registerTool (444)
   ▼
 已注册，pi exposure = toToolExposure(getMcpToolExposure(...)) (tools.ts:333, index.ts:423)
   ├─ direct ──────── 注册即激活 (agent-session.ts:3683) ──────────────────────▶ 已声明给模型
   ├─ deferred（MCP deferred）── tool_search 搜到 → setActiveTools (tool-search/tool.ts:297) ─▶ 已声明（下一次请求起）
   ├─ deferred（MCP codemode）── 不声明；codemode 脚本经 ctx.executeTool 调用 (agent-session.ts:1601)
   └─ hidden ──────── 不能声明也不能调用
 已声明或被脚本调用
   │ tool_call：MCP 扩展等服务器 (index.ts:1191-1209)，权限扩展可拦截 → 被拦截的调用不发给服务器
   ▼
 execute → callTool → convertMcpResult (tools.ts:337-349)
 服务器不再提供该工具 → 重新注册为 hidden (index.ts:449-452)
```

---

## 6. 设计要点

### 6.1 协议客户端和产品集成分成两个包

**做法。** pi-mcp 只依赖 Node 和 `cross-spawn`，不依赖 pi-ai 或 pi-agent-core。它连内容类型都自己定义了一份 `LlmContent`（§4.8），而不是导入 pi-ai 的类型。agent 相关的部分（命名、exposure、结果转换、权限）全在 coding-agent 的扩展里。

**理由。** 协议部分可以被其他程序单独使用（`package.json` 的描述是 "Standalone Model Context Protocol client for pi and other applications"），也可以单独测试：§2.3 的脚本不需要会话、扩展、模型就能跑。产品部分换实现时，协议部分不受影响，第三方 MCP 扩展可以替换内置扩展（`replaceable: true`，§4.11）。

**代码位置。** `packages/mcp/src/index.ts`（公开接口），`packages/coding-agent/src/extensions/mcp/runtime.ts:139`（两者的唯一交界）。

### 6.2 MCP 工具就是普通工具

**做法。** 每个 MCP 工具注册成一个 `ToolDefinition`，走 pi 的工具流水线：`toolsAdded` 声明、参数校验、`tool_call`/`tool_result` 事件、`isError` 结果、写进会话文件。MCP 扩展没有在 agent 循环里加任何专门的分支。

**理由。** 所有对工具生效的机制自动对 MCP 工具生效：权限扩展不需要知道 MCP（§7.2），会话恢复、分支、压缩不需要处理新的条目类型（§2.2 的会话文件里没有 MCP 专用条目），codemode 脚本里的嵌套调用也经过同样的检查。代价是工具一旦注册就不能注销，服务器撤回的工具只能改成 `hidden`（§4.15）。

**代码位置。** `packages/coding-agent/src/extensions/mcp/index.ts:20-21`（文件头注释），`packages/coding-agent/src/extensions/mcp/index.ts:444`。

### 6.3 连接在后台，按需等待

**做法。** `session_start` 不等任何连接（§4.12）。第一次 prompt 只等有 `direct` 工具的服务器，最多 10 秒（§4.20）。其他服务器在工具被调用前，由 `tool_call` 处理函数按 `reachesTools` 只等相关的那几个（§4.21）。连接断开不立即重连，下一次调用时再连（§4.14）。

**理由。** 启动时间不受慢服务器拖累，没有配置服务器的会话连 pi-mcp 都不加载（`runtime.lazy.ts`）。直接声明的工具必须在第一次请求前到位，否则模型第一次看不到它们，所以只有这一类需要提前等。

**代码位置。** `packages/coding-agent/src/extensions/mcp/index.ts:1140`、`packages/coding-agent/src/extensions/mcp/index.ts:1156`、`packages/coding-agent/src/extensions/mcp/index.ts:1191`。

### 6.4 默认不把 MCP 工具声明给模型

**做法。** 服务器的默认 exposure 是 `codemode`（`packages/coding-agent/src/core/mcp-servers.ts:253`）：工具不进声明，模型通过 `codemode` 脚本调用它们；`deferred` 通过 `tool_search` 先搜后用。模型从 `mcp_servers` 段知道有哪些服务器（§4.20）。

**理由。** MCP 服务器常常有几十个工具，全部声明会占用大量上下文，每加一个服务器所有请求都变长。`codemode` 和 `tool_search` 的说明都不列出 MCP 工具，所以服务器连上、工具变化时，这两个工具的声明不变，已缓存的 prompt 前缀不失效。`tool_search` 激活的工具写进 transcript，和 M2 §6.2"工具清单写进 transcript"是同一个思路。

**代码位置。** `packages/coding-agent/src/extensions/mcp/tools.ts:44`，`packages/coding-agent/src/extensions/tool-search/tool.ts:288`。

### 6.5 请求的结局只有一个，并且总会到来

**做法。** 每个请求在 `pending` 表里有一项，响应、超时、中止、发送失败、连接关闭五条路都先 `removePending` 再 resolve/reject，第二条到达的路找不到表项就什么也不做（§4.7）。超时和中止时通知服务器 `notifications/cancelled`。工具调用不在暂时错误后重试（§4.22）。

**理由。** 调用方（agent 循环）总能等到一个结果，不会因为服务器不回答而永远挂起；服务器知道请求被放弃，可以停止工作。不重试工具调用，是因为客户端无法知道服务器是否已经执行了它。

**代码位置。** `packages/mcp/src/client.ts:561`，`packages/coding-agent/src/extensions/mcp/runtime.ts:278`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行。每次运行用新的 `WORK` 和 `PI_CODING_AGENT_DIR` 目录，这样 `server.log` 只包含本次运行的记录。

### 7.1 三种 exposure 下模型看到什么

**问题。** 同一个 `demo` 服务器，`exposure` 分别设为 `direct`、`deferred`、`codemode`。模型第一次请求的 `toolsAdded` 里有什么？要几次请求才能拿到 `echo` 的结果？服务器分别收到哪些消息？

**预测。**

- `direct`：第一次请求就声明了 `mcp__demo__echo` 和 `mcp__demo__fail`，没有 `mcp_servers` 段。2 次请求。
- `deferred`：第一次请求声明 `tool_search` 而没有 MCP 工具（§4.19），有 `mcp_servers` 段，写着 `tool_search`。模型调用 `tool_search` 后，第二次请求的 transcript 末尾多一条只声明 `mcp__demo__echo` 的 `system` 消息。第三次请求才拿到结果，共 3 次。
- `codemode`：第一次请求声明 `codemode`，`mcp_servers` 段写着 `codemode`。脚本里调用 `tools.mcp__demo__echo`，transcript 里只有一个 `codemode` 工具结果，没有 `mcp__demo__echo` 的结果。2 次请求。
- 三种情况下服务器收到的消息相同：`initialize`、`notifications/initialized`、`tools/list`、`tools/call`。exposure 只影响 pi 怎么对模型展示工具，不影响 MCP 协议。

**运行。**

```bash
for e in direct deferred codemode; do
  echo "=== $e"
  WORK=/tmp/pi-lab/m10-work-7$e PI_CODING_AGENT_DIR=/tmp/pi-lab/m10-agent-7$e EXPOSURE=$e M10_SCENARIO=$e PI_TRACE=1 ./m10-run.sh -p "用 echo 工具说你好"
  echo "--- server.log"; cat /tmp/pi-lab/m10-agent-7$e/server.log
done
```

```
=== direct
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write,mcp__demo__echo,mcp__demo__fail]
  user       [{"type":"text","text":"用 echo 工具说你好"}]
[faux] 第 2 次请求，收到 4 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write,mcp__demo__echo,mcp__demo__fail]
  user       [{"type":"text","text":"用 echo 工具说你好"}]
  assistant  [{"type":"toolCall","id":"call_1","name":"mcp__demo__echo","arguments":{"text":"你好 MCP"}}]
  toolResult mcp__demo__echo isError=false "echo: 你好 MCP"
工具返回：echo: 你好 MCP
--- server.log
initialize
notifications/initialized
tools/list
tools/call
=== deferred
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd,mcp_servers] toolsAdded=[read,bash,edit,write,tool_search]
             mcp_servers: "<mcp_servers>\nMCP servers whose tools are not declared to you. Load the tools of `tool_search` servers with `tool_search`.\n- mcp__demo (tool_search)\n</mcp_servers>"
  user       [{"type":"text","text":"用 echo 工具说你好"}]
[faux] 第 2 次请求，收到 5 条消息:
  system     sections=[preamble,tools,rules,docs,cwd,mcp_servers] toolsAdded=[read,bash,edit,write,tool_search]
             mcp_servers: "<mcp_servers>\nMCP servers whose tools are not declared to you. Load the tools of `tool_search` servers with `tool_search`.\n- mcp__demo (tool_search)\n</mcp_servers>"
  user       [{"type":"text","text":"用 echo 工具说你好"}]
  assistant  [{"type":"toolCall","id":"call_1","name":"tool_search","arguments":{"query":"echo text"}}]
  toolResult tool_search isError=false "Loaded 1 tool. They are available from your next call:\n- mcp__demo__echo: Echo the given
  system     sections=[] toolsAdded=[mcp__demo__echo]
[faux] 第 3 次请求，收到 7 条消息:
  system     sections=[preamble,tools,rules,docs,cwd,mcp_servers] toolsAdded=[read,bash,edit,write,tool_search]
             mcp_servers: "<mcp_servers>\nMCP servers whose tools are not declared to you. Load the tools of `tool_search` servers with `tool_search`.\n- mcp__demo (tool_search)\n</mcp_servers>"
  user       [{"type":"text","text":"用 echo 工具说你好"}]
  assistant  [{"type":"toolCall","id":"call_1","name":"tool_search","arguments":{"query":"echo text"}}]
  toolResult tool_search isError=false "Loaded 1 tool. They are available from your next call:\n- mcp__demo__echo: Echo the given
  system     sections=[] toolsAdded=[mcp__demo__echo]
  assistant  [{"type":"toolCall","id":"call_2","name":"mcp__demo__echo","arguments":{"text":"你好 MCP"}}]
  toolResult mcp__demo__echo isError=false "echo: 你好 MCP"
工具返回：echo: 你好 MCP
--- server.log
initialize
notifications/initialized
tools/list
tools/call
=== codemode
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd,mcp_servers] toolsAdded=[read,bash,edit,write,codemode]
             mcp_servers: "<mcp_servers>\nMCP servers whose tools are not declared to you. Call the tools of `codemode` servers from codemode scripts.\n- mcp__demo (codemode)\n</mcp_servers>"
  user       [{"type":"text","text":"用 echo 工具说你好"}]
[faux] 第 2 次请求，收到 4 条消息:
  system     sections=[preamble,tools,rules,docs,cwd,mcp_servers] toolsAdded=[read,bash,edit,write,codemode]
             mcp_servers: "<mcp_servers>\nMCP servers whose tools are not declared to you. Call the tools of `codemode` servers from codemode scripts.\n- mcp__demo (codemode)\n</mcp_servers>"
  user       [{"type":"text","text":"用 echo 工具说你好"}]
  assistant  [{"type":"toolCall","id":"call_1","name":"codemode","arguments":{"code":"const r = await t
  toolResult codemode isError=false "Script completed\nWall time 0.1 seconds\nOutput:\necho: 来自脚本"
工具返回：Script completed
Wall time 0.1 seconds
Output:
echo: 来自脚本
--- server.log
initialize
notifications/initialized
tools/list
tools/call
```

**结论。** 四个预测都成立。

- `deferred` 的 `tool_search` 只加载了 `echo`，没有加载 `fail`：查询 `echo text` 在 BM25 排序里只命中了 `echo`。加载记录是第 2 次请求末尾那条 `system sections=[] toolsAdded=[mcp__demo__echo]`，它在工具结果之后、下一次请求之前由 `declareToolChanges` 写入（M2 §4.4）。
- `codemode` 的脚本拿到的 `r` 是整个 `CallToolResult`（§4.16 说过所有 MCP 工具都声明 `outputSchema`），所以脚本写的是 `r.content[0].text`。模型只看到脚本的输出，`Wall time` 每次运行可能不同。
- 三种情况下 `server.log` 完全相同。

### 7.2 权限扩展拦截 MCP 工具调用

**问题。** [`lab/m10-gate.ts`](lab/m10-gate.ts) 是一个 17 行的扩展：工具名以 `mcp__` 开头、并且 `annotations.readOnlyHint` 不是 `true` 的调用，返回 `{ block: true, reason }`。它不导入任何 MCP 相关的东西，只用 `pi.getAllTools()` 读工具的 `annotations`。

1. 模型调用 `mcp__demo__fail`（没有 `readOnlyHint`）。不加载 gate 和加载 gate 时，工具结果分别是什么？服务器会收到 `tools/call` 吗？
2. `codemode` exposure 下，脚本里调用 `mcp__demo__echo`（`readOnlyHint: true`）。gate 会看到几次 `tool_call`？

**预测。**

1. 不加载 gate：服务器执行 `fail`，返回 `isError: true`，模型看到 `something went wrong`，`isError=true`（§4.17）。加载 gate：`tool_call` 返回 `block`，agent 循环不调用 `execute`（M2 §4.11），模型看到的错误文本是 gate 的 `reason`。`server.log` 里没有 `tools/call`。
2. 两次：一次是 `codemode` 本身（`id=call_1`），一次是脚本里的嵌套调用，带 `parentToolCallId`。嵌套调用的 id 格式是 `<父 id>/<n>`（`packages/coding-agent/src/core/extensions/types.ts:1216-1218`），所以是 `call_1/1`。`echo` 是只读的，两次都放行。

**运行。**

```bash
export PI_TRACE=1
echo "=== fail 无 gate"
WORK=/tmp/pi-lab/m10-work-8a PI_CODING_AGENT_DIR=/tmp/pi-lab/m10-agent-8a M10_SCENARIO=fail ./m10-run.sh -p "调用 fail" 2>&1 | grep -v '^  system\|^  user'
cat /tmp/pi-lab/m10-agent-8a/server.log
echo "=== fail GATE=1"
WORK=/tmp/pi-lab/m10-work-8b PI_CODING_AGENT_DIR=/tmp/pi-lab/m10-agent-8b M10_SCENARIO=fail GATE=1 ./m10-run.sh -p "调用 fail" 2>&1 | grep -v '^  system\|^  user'
cat /tmp/pi-lab/m10-agent-8b/server.log
echo "=== codemode GATE=1"
WORK=/tmp/pi-lab/m10-work-8c PI_CODING_AGENT_DIR=/tmp/pi-lab/m10-agent-8c EXPOSURE=codemode M10_SCENARIO=codemode GATE=1 ./m10-run.sh -p "用 echo 工具说你好" 2>&1 | grep -v '^  system\|^  user\|^             mcp_servers'
```

```
=== fail 无 gate
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
[faux] 第 2 次请求，收到 4 条消息:
  assistant  [{"type":"toolCall","id":"call_1","name":"mcp__demo__fail","arguments":{}}]
  toolResult mcp__demo__fail isError=true "something went wrong"
工具返回：something went wrong
initialize
notifications/initialized
tools/list
tools/call
=== fail GATE=1
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
[gate] tool_call mcp__demo__fail id=call_1 readOnlyHint=false → block
[faux] 第 2 次请求，收到 4 条消息:
  assistant  [{"type":"toolCall","id":"call_1","name":"mcp__demo__fail","arguments":{}}]
  toolResult mcp__demo__fail isError=true "MCP tool mcp__demo__fail is not read-only"
工具返回：MCP tool mcp__demo__fail is not read-only
initialize
notifications/initialized
tools/list
=== codemode GATE=1
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
[gate] tool_call codemode id=call_1 readOnlyHint=false → allow
[gate] tool_call mcp__demo__echo id=call_1/1 parent=call_1 readOnlyHint=true → allow
[faux] 第 2 次请求，收到 4 条消息:
  assistant  [{"type":"toolCall","id":"call_1","name":"codemode","arguments":{"code":"const r = await t
  toolResult codemode isError=false "Script completed\nWall time 0.1 seconds\nOutput:\necho: 来自脚本"
工具返回：Script completed
Wall time 0.1 seconds
Output:
echo: 来自脚本
```

**结论。** 预测都成立。

- 两种失败对模型来说形状一样（`isError=true` 加一段文字），但来源不同：前者是服务器执行了工具并报告失败，后者是请求在 pi 内部被拦下，`server.log` 少了 `tools/call`。
- gate 的规则只用到工具名和 `annotations`，它对 `codemode` 工具本身放行（`codemode` 不以 `mcp__` 开头），对脚本里的嵌套调用单独做了判断。如果把脚本里的 `echo` 换成 `fail`，嵌套调用会被拦截，脚本里的 `await` 以错误 reject（`codemode/tool.ts` 文件头注释："A failed, blocked, or invalid call rejects with an Error"）。

### 7.3 超时和取消在线上发出什么

**问题。** `m10-client.ts timeout` 用 `InMemoryTransport` 在进程内放一个只回答 `initialize`、从不回答 `tools/call` 的服务器，客户端的 `callTool` 设 `timeoutMs: 200`。`abort` 场景相同，但在 50ms 时用 `AbortController.abort("user pressed Esc")` 取消请求。两个场景里 `callTool` 何时失败、抛什么错误？客户端还会发出什么消息？

**预测。**

- `timeout`：约 200ms 时 `armTimeout` 的计时器触发（§4.7），`callTool` reject `McpTimeoutError: MCP request timed out after 200ms`。客户端发出 `notifications/cancelled`，`requestId` 是 2（1 是 `initialize`），`reason` 是 `"Request timed out"`。
- `abort`：约 50ms 时 `onAbort` 触发（§4.3），reject `McpAbortError`，它的 `name` 是 `AbortError`（§4.5），消息是 `MCP request aborted`。`notifications/cancelled` 的 `reason` 取 `signal.reason`，即 `"user pressed Esc"`。
- 两个场景里服务器都不回答，所以输出里只有一条 `←`（`initialize` 的响应）。

**运行。**

```bash
node --conditions=source --experimental-strip-types m10-client.ts timeout
node --conditions=source --experimental-strip-types m10-client.ts abort
```

```
   0ms → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"m10-lab","version":"1.0.0"}}}
   1ms ← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"silent","version":"0"}}}
   1ms → {"jsonrpc":"2.0","method":"notifications/initialized"}
   1ms → {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}}}
 202ms → {"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":2,"reason":"Request timed out"}}
 202ms callTool rejected: McpTimeoutError: MCP request timed out after 200ms
```

```
   0ms → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"m10-lab","version":"1.0.0"}}}
   1ms ← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"silent","version":"0"}}}
   1ms → {"jsonrpc":"2.0","method":"notifications/initialized"}
   1ms → {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}}}
  51ms → {"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":2,"reason":"user pressed Esc"}}
  51ms callTool rejected: AbortError: MCP request aborted
```

**结论。** 预测都成立。`notifications/cancelled` 在 `reject` 之前打印，因为 `cancelPending` 里 `entry.reject(error)` 只是安排了 Promise 回调，`transport.send()` 是同步调用的，先打印了记录（`packages/mcp/src/client.ts:565-567`）。在 pi 里，用户按 Esc 时 agent 循环中止 `signal`，MCP 工具把它传给 `callTool`（§4.16），线上发生的就是 `abort` 场景这一串。

---

## 8. 自测与衔接

### 8.1 自测

1. 一个 HTTP MCP 服务器重启了，pi 下一次调用它的工具时会发生什么？如果是在调用过程中服务器返回了 503 呢？
2. 服务器配置里 `"exposure": "hidden", "toolExposure": { "search_*": "direct" }`，服务器有 `search_code`、`search_issue`、`delete_repo` 三个工具。模型第一次请求能看到哪些 MCP 工具？会出现 `mcp_servers` 段吗？
3. 服务器把一个工具的结果分 30 次进度通知慢慢发，总共 3 分钟，服务器配置 `timeout: 60`。这个调用会超时吗？

<details>
<summary>答案</summary>

1. 重启后服务器不认识旧的 `Mcp-Session-Id`，返回 404，`checkResponse` 抛 `McpSessionExpiredError`（`packages/mcp/src/transports/streamable-http.ts:344`）。`withClient` 在第一次尝试遇到它时丢掉旧客户端，`getClient()` 新建连接重新握手，再执行一次（`packages/coding-agent/src/extensions/mcp/runtime.ts:288-294`）。503 是暂时错误，但 `callTool` 不是只读操作，不重试（`packages/coding-agent/src/extensions/mcp/runtime.ts:284`），错误抛给 agent 循环，变成 `isError: true` 的工具结果。
2. `getMcpToolExposure` 对 `search_code` 和 `search_issue` 用模式 `search_*` 得到 `direct`，对 `delete_repo` 用服务器的 `hidden`（`packages/coding-agent/src/core/mcp-servers.ts:246-254`）。第一次请求声明 `mcp__<server>__search_code` 和 `mcp__<server>__search_issue`。`configuredExposures` 是 `{hidden, direct}`，没有 `codemode` 或 `deferred`，`renderServersSection` 不列出这个服务器（`hasIndirectTools` 为假，`packages/coding-agent/src/extensions/mcp/index.ts:150-153`），所以没有 `mcp_servers` 段；`ensureDiscoveryActive` 也不激活 `codemode` 或 `tool_search`。
3. 不会，只要相邻两次进度通知的间隔小于 60 秒。MCP 工具总是传 `onProgress`（`packages/coding-agent/src/extensions/mcp/tools.ts:342`），所以请求带 `progressToken`；每收到一个进度通知，`handleProgress` 调用 `armTimeout` 重新计时（`packages/mcp/src/client.ts:541`）。3 分钟 30 次，平均 6 秒一次。

</details>

### 8.2 衔接

本模块是一个"用扩展 API 搭起来的功能"的完整例子：它用到了 M7 的 `session_start`、`before_agent_start`、`tool_call`、`session_shutdown` 四个事件和 `registerTool`、`setActiveTools` 两个 API，用到了 M6 的系统提示词段落，用到了 M2 的工具声明进 transcript 和 `isError` 结果。读到 `pi.registerTool()` 之后，工具怎么进入 `AgentSession` 的工具表、怎么变成 `system` 消息里的声明，在 M8 从 `_refreshToolRegistry()`（`packages/coding-agent/src/core/agent-session.ts:3576`）读起；`tool_call` 事件怎么分发、`block` 怎么变成错误结果，在 M7 从 `ExtensionRunner.emitToolCall()` 读起。

下一个模块 M11 durable 从 `Harness.open()` 开始，讲另一种组织 agent 循环的方式：把每一步都持久化，进程中断后可以恢复。它的工具定义里也有本模块 §3.6 没讲的 `replay` 字段（M2 §3.2）。
