# pi 源码教程

这份教程带你从源码读懂 [pi](https://github.com/earendil-works/pi)，一个用 TypeScript 写的终端 coding agent。教程按模块组织，每个模块从一个入口函数开始，顺着调用往下读，读完就能看清这个模块的全貌。

- 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)（2026-11-01），所有包 v1.4.0。文中的行号和链接都固定在这个提交。
- 运行环境：Node.js 24（本文用 v24.21.0 验证）。
- 文中所有命令和输出都实际运行过。

---

## 1. 包与模块对照

| npm 包 | 源码目录 | 教程模块 |
|---|---|---|
| `@earendil-works/pi-ai` | `packages/ai` | [M1 pi-ai：统一的 LLM 接口](M1-pi-ai.md) |
| `@earendil-works/pi-agent-core` | `packages/agent` | [M2 pi-agent-core：agent 循环](M2-agent-core.md) |
| `@earendil-works/pi-coding-agent` | `packages/coding-agent` | [M3 工具](M3-tools.md)、[M4 会话存储](M4-session.md)、[M5 压缩](M5-compaction.md)、[M6 提示词与资源](M6-prompt-resources.md)、[M7 扩展](M7-extensions.md)、[M8 AgentSession](M8-agent-session.md)、[M9 入口与运行模式](M9-entry-modes.md)、[M10 MCP](M10-mcp.md) 的第二部分（`src/extensions/mcp`，MCP 作为内置扩展） |
| `@earendil-works/pi-mcp` | `packages/mcp` | [M10 MCP](M10-mcp.md) 的第一部分：MCP 客户端 |
| `@earendil-works/pi-durable` | `packages/durable` | [M11 durable](M11-durable/D1-concepts.md)（D1–D6） |
| `@earendil-works/chord` | `packages/chord` | 只在 [M11 D2](M11-durable/D2-chord.md) 讲 durable 用到的部分 |
| `@earendil-works/pi-codemode` | `packages/codemode` | durable 依赖它，只在 [M11 D6](M11-durable/D6-extensions-observation.md) 讲和 durable 重放有关的部分 |

依赖方向是 `pi-ai` ← `pi-agent-core` ← `pi-coding-agent`。下层不知道上层的存在，所以从 M1 往后读，每读一个模块，它依赖的东西都已经读过了。

不在本教程范围内：TUI（`modes/interactive/` 和 `packages/tui`）、包管理器、主题、自更新、其余十几个 provider 的实现细节。它们不影响理解 agent 的工作方式。

---

## 2. 全局调用链

下面是主线场景 `pi -p "读 README.md，总结成三句话"` 的完整调用链。方括号里是这一步所属的模块。读任何模块之前，先在这张图上找到它的位置。

```
cli.ts → main()                                                    [M9]
├─ createSessionManager()                                          [M9 → M4]
├─ createAgentSessionRuntime(createRuntime)                        [M9]
│   ├─ createAgentSessionServices()                                [M8]
│   │   ├─ ModelRuntime.create()                                   [M1]
│   │   ├─ SettingsManager.create()  CLI 下由 main() 预先创建后传入   [M6]
│   │   └─ DefaultResourceLoader.reload()  加载扩展、AGENTS.md、skills [M6 / M7]
│   └─ createAgentSessionFromServices() → createAgentSession()     [M8]
│       ├─ new Agent({ streamFn, convertToLlm, transformContext… }) [M2]
│       └─ new AgentSession()  安装 hook、创建工具和扩展运行器         [M8 → M3 / M7]
└─ runPrintMode()                                                  [M9]
    └─ session.prompt(text)                                        [M8]
        └─ _startRun() → _prepareRun()  构造 user 消息和系统提示词补丁 [M8 → M6]
            └─ agent.prompt(messages)                              [M2]
                └─ runWithLifecycle() → runAgentLoop() → runLoop()
                    ├─ declareToolChanges()  工具清单写进 system 消息
                    ├─ prepareRequest()  ← AgentSession 用会话文件重建上下文 [M8 → M4]
                    ├─ streamAssistantResponse()
                    │   ├─ transformContext()  ← 扩展的 context 事件       [M7]
                    │   ├─ convertToLlm()                                  [M8]
                    │   └─ streamFn() → ModelRuntime.streamSimple() → provider [M1]
                    ├─ executeToolCalls()
                    │   ├─ prepareToolCall() → beforeToolCall()  ← 扩展 tool_call [M7]
                    │   ├─ tool.execute()  read 工具                        [M3]
                    │   └─ finalizeExecutedToolCall() → afterToolCall()     [M7]
                    ├─ 每个事件 → Agent.processEvents()
                    │            → AgentSession._handleAgentEvent()
                    │            → SessionManager.appendMessage()           [M8 → M4]
                    ├─ finishTurn()  ← turn_end 边界                        [M8]
                    ├─ prepareNextTurn()  ← 轮间压缩检查                     [M8 → M5]
                    └─ 没有工具调用、没有排队消息 → agent_end
            └─ _handlePostAgentRun()  重试 / 压缩判断 → agent_settled     [M8]
        └─ 从 state.messages 取最后一条 assistant 消息，输出文本            [M9]
```

---

## 3. 全局数据结构

pi 的代码大部分在做转换：把一种消息变成另一种消息，把一种事件包装成另一种事件。先认识这几层，读代码时只需要关注"它把什么变成了什么"。

### 3.1 消息：三层

```
 会话条目 SessionEntry                     [M4]  写在 JSONL 里，组成一棵树
   message / model_change / compaction / context_edit / …
        │ buildSessionProjection()  沿当前分支取出条目，应用压缩和编辑
        ▼
 Agent 消息 AgentMessage                   [M2]  transcript 内部使用
   system / user / assistant / toolResult
   + bashExecution / custom / branchSummary / compactionSummary
        │ convertToLlm()  自定义消息转成 user 消息或丢弃
        ▼
 LLM 消息 Message                          [M1]  发给模型的形状
   system / user / assistant / toolResult
        │ transformMessages()  跨 provider 修正 thinking、工具调用 id
        │ resolveTranscript()  不支持中途 system 消息的 provider 会合并
        ▼
 各家 API 的请求体                         [M1 provider]
```

模型返回的 `AssistantMessage` 走反方向：在 `message_end` 事件里被写回会话文件，成为一个 `message` 条目。

### 3.2 事件：四层

```
 provider 原始流（如 Anthropic SSE 的 content_block_delta）
        │ provider 实现                               [M1]
        ▼
 AssistantMessageEvent（12 种：start、text_delta、toolcall_end、done、error …）
        │ streamAssistantResponse() 包成 message_update  [M2]
        ▼
 AgentEvent（10 种：agent_start、turn_*、message_*、tool_execution_*）
        │ AgentSession._handleAgentEvent()             [M8]
        ├──▶ 扩展事件（32 种事件类型、41 个事件名，部分可以拦截或改写）[M7]
        └──▶ AgentSessionEvent（AgentEvent + compaction_*、auto_retry_*、
             queue_update、agent_settled …）
                │ subscribe()
                ▼
             前端：print / json / rpc / TUI             [M9]
```

---

## 4. 环境准备

### 4.1 获取源码并安装

```bash
git clone https://github.com/earendil-works/pi.git
cd pi
git checkout 2c8d1feb147f6b3d97e855da803bfe7b19f3c403
npm ci --ignore-scripts
npm run build
```

在这个提交上，`npm run build` 会在 durable 包自身的类型检查处报错退出。这不影响本教程：在报错之前，pi-ai 等依赖已经构建完成，而 pi 和各模块的实验都直接从 TypeScript 源码运行。

后文用环境变量 `PI` 表示 pi 仓库的路径：

```bash
export PI=/path/to/pi
```

### 4.2 从源码运行 pi

仓库根目录的 `pi-test.sh` 用 Node 的 `--import` 加载一个源码解析器，直接运行 `packages/coding-agent/src/cli.ts`，不需要构建。加上 `--no-env` 参数会清空所有 API key 环境变量。

### 4.3 用假模型代替真实 LLM

pi-ai 自带一个 faux provider（`packages/ai/src/providers/faux.ts`），可以按顺序返回预先写好的回复。本教程的实验都用它，不需要 API key，每次运行的结果也是确定的。

实验文件在 [`lab/`](lab/) 目录：

| 文件 | 作用 |
|---|---|
| `faux-ext.ts` | 一个 pi 扩展，注册名为 `faux` 的假模型。第 1 次请求返回"调用 read 读 README.md"，第 2 次请求返回总结。设置 `PI_TRACE=1` 时，打印模型每次收到的消息 |
| `run.sh` | 用假模型运行 pi。默认工作目录是 `/tmp/pi-lab/work`，会在里面生成一个演示用的 README.md；配置目录隔离在 `/tmp/pi-lab/agent`，不影响你的 `~/.pi` |
| `summarize-events.mjs` | 把 `--mode json` 输出的事件流压缩成一行一个事件 |
| `show-session.mjs` | 把会话 JSONL 文件打印成一行一个条目 |
| `m2-loop.ts` | M2 的实验：绕开 coding-agent，直接驱动 agent 循环。场景 basic / parallel / sequential / mixed / length / steer |
| `m2-agent.ts` | M2 的实验：通过 `Agent` 类运行循环，打印核心数据结构的实例；`block` 场景演示 `beforeToolCall` 拦截 |
| `m1-runtime.ts` | M1 的实验：从 `ModelRuntime.streamSimple()` 进入 pi-ai，打印事件流 |
| `m1-anthropic.ts` | M1 的实验：用假 fetch 不联网运行 Anthropic 适配器，看 SSE、请求体、思考级别、缓存、错误 |
| `m1-transcript.ts` | M1 的实验：`resolveTranscript`、`resolveTranscriptTools`、`transformMessages` 三种转换 |
| `m3-tools.ts` | M3 的实验：直接调用内置工具，看截断、超时、edit 匹配规则 |
| `m3-nested-ext.ts` | M3 的实验：扩展工具通过 `ctx.executeTool()` 发起嵌套工具调用 |
| `m4-session.ts` | M4 的实验：直接调用 `SessionManager`，看会话文件的写入、投影、压缩条目和分支 |
| `m5-run.sh` | M5 的实验：用小上下文窗口的假模型（`m5-faux-ext.ts`）运行 pi，强制触发压缩 |
| `m5-faux-ext.ts` | M5 的假模型扩展：上下文窗口很小，按请求内容决定回复 |
| `m5-inspect.mjs` | M5 的查看工具：事件流里的 token 数据、会话文件里的 compaction 条目 |
| `m5-direct.ts` | M5 的实验：直接调用压缩的纯函数，打印中间数据结构；以及分支摘要 |
| `m6-dump-ext.ts` | M6 的扩展：在 `before_agent_start` 里打印或修改系统提示词选项 |
| `m6-resources.ts` | M6 的实验：单独运行 `DefaultResourceLoader`，打印发现的资源 |
| `m6-settings.ts` | M6 的实验：看全局设置和项目设置怎么合并 |
| `m6-show-system.ts` | M6 的实验：打印会话文件里的 system 消息和重放后的系统提示词 |
| `m7-trace-ext.ts` | M7 的扩展：订阅全部 41 个扩展事件名，每个事件打印一行 |
| `m7-runner.ts` | M7 的实验：不启动 pi，直接用 loader 加载扩展、用 `ExtensionRunner` 派发事件 |
| `m7-faux-bash.ts` | M7 的假模型扩展：调用 bash，命令取自 `M7_CMD` |
| `m7-handlers.ts` | M7 的实验：几种 handler 写法（改写参数、抛异常、裁剪上下文） |
| `m8-session.ts` | M8 的实验：用 `createAgentSession()` 创建 AgentSession，打印 hook、事件序列和状态标志 |
| `m9-dump.ts` | M9 的实验：打印入口层数据结构（命令行参数、运行模式等）的实例 |
| `m9-rpc.mjs` | M9 的实验：以 RPC 模式启动 pi，通过 stdin 发命令 |
| `m9-sdk.ts` | M9 的实验：用 SDK 创建 AgentSession，接上不同的前端 |
| `m10-run.sh` | M10 的实验：用假模型运行 pi，并接上 `m10-server.mjs` |
| `m10-server.mjs` | M10 的最小 stdio MCP 服务器，由 `m10-run.sh` 写进 mcp.json 启动 |
| `m10-faux-ext.ts` | M10 的假模型扩展：调用 MCP 工具，`M10_SCENARIO` 选择脚本 |
| `m10-gate.ts` | M10 的权限扩展：拦截 `mcp__` 开头的工具调用 |
| `m10-client.ts` | M10 的实验：直接用 `McpClient` 连接 MCP 服务器，打印每条 JSON-RPC 消息 |
| `m11a-durable.ts` | M11 D1/D3 的实验：用 durable 跑主线场景，打印每次 `Storage.commit()` 的写入 |
| `m11a-chord.ts` | M11 D2 的实验：单独运行 chord/context、chord/delta、chord/sqlite |
| `m11b-crashloop.ts` | M11 D4 的实验：`restartLimit`、没有持久进度的阶段、任务记录 |
| `m11b-kill.ts` | M11 D5 的实验：工具执行到一半杀掉进程，重新打开后对比 `replay: "safe"` 和 `"unsafe"` |
| `m11b-observe.ts` | M11 D5/D6 的实验：观察任务图和会话视图的变化、`requestId` 去重、hook |
| `summary-faux.ts` | 总结篇练习的假模型扩展：先调用 `rm -rf build`，再调用 `ls` |
| `summary-guard.ts` | 总结篇练习的参考答案：拦截 `rm -rf`，把每轮 usage 写进会话文件 |
| `summary-check.mjs` | 总结篇练习的检查脚本：全部通过时退出码为 0 |
| `summary-message-end.ts` | 总结篇实验：练习第 2 问的一个错误写法（在 `message_end` 里写条目） |

---

## 5. 主线场景的真实输出

这一节的输出是后面所有模块的共同参照。

### 5.1 print 模式

```bash
cd docs/pi-tutorial/lab
PI_TRACE=1 ./run.sh
```

```
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
[faux] 第 2 次请求，收到 4 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

可以看到：
- 系统提示词和工具清单不是请求的单独字段，而是 transcript 里的第一条 `system` 消息。
- 第 2 次请求带上了第 1 次的 assistant 回复和 read 工具的结果。

### 5.2 事件流

```bash
mkdir -p /tmp/pi-lab
./run.sh --mode json "读 README.md，总结成三句话" > /tmp/pi-lab/events.jsonl
node summarize-events.mjs /tmp/pi-lab/events.jsonl
```

```
session                          ← 会话文件头，不是事件
agent_start
turn_start                       ← 第 1 轮
message_start (system)
message_end (system)
message_start (user)
message_end (user)
message_start (assistant)        ← 开始流式接收第 1 次回复
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
turn_start                       ← 第 2 轮
message_start (assistant)
message_update [text_start]
message_update [text_delta] x2
message_update [text_end]
message_end (assistant)
turn_end (assistant)
agent_end                        ← agent 循环结束
agent_settled                    ← AgentSession 层的事件：会话已空闲
共 31 行（含第一行 session 头）
```

faux provider 会把回复切成随机长度的片段来模拟流式输出，所以 `text_delta` 和 `toolcall_delta` 的个数、以及最后一行的总行数，每次运行可能不同。其余事件的顺序是固定的。

### 5.3 会话文件

```bash
node show-session.mjs "$(ls -t /tmp/pi-lab/agent/sessions/*/*.jsonl | head -1)"
```

```
session                id=01a195bd parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/work"}
model_change           id=5ebeb42b parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=36830ce6 parent=5ebeb42b {"thinkingLevel":"off"}
message                id=46f5c33e parent=36830ce6 system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write
message                id=aef02905 parent=46f5c33e user       [{"type":"text","text":"读 README.md，总结成三句话"}]
message                id=c6b087b5 parent=aef02905 assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_1","name":"read","ar stop=toolUse
message                id=3eda990d parent=c6b087b5 toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used 
message                id=5f5c9e0d parent=3eda990d assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}] stop=stop
```

可以看到：
- 每个条目的 `parent` 指向上一个条目。会话文件是一棵树，这里只有一条分支，所以看起来是一条链。
- 事件流里每个 `message_end` 都对应这里的一个 `message` 条目。
- id 和时间戳每次运行都不同。

---

## 6. 目录

| 模块 | 入口 | 状态 |
|---|---|---|
| [M1 pi-ai：统一的 LLM 接口](M1-pi-ai.md) | `ModelRuntime.streamSimple()` | 已写 |
| [M2 pi-agent-core：agent 循环](M2-agent-core.md) | `Agent.prompt()` | 已写 |
| [M3 工具](M3-tools.md) | `tool.execute()` | 已写 |
| [M4 会话存储](M4-session.md) | `SessionManager.appendMessage()` | 已写 |
| [M5 压缩](M5-compaction.md) | `shouldCompact()` | 已写 |
| [M6 提示词与资源](M6-prompt-resources.md) | `buildSystemPromptSections()` | 已写 |
| [M7 扩展](M7-extensions.md) | `ExtensionRunner.emitToolCall()` | 已写 |
| [M8 AgentSession](M8-agent-session.md) | `createAgentSession()` | 已写 |
| [M9 入口与运行模式](M9-entry-modes.md) | `main()` | 已写 |
| [M10 MCP](M10-mcp.md) | `McpClient.connect()` | 已写 |
| M11 durable：[D1 概念](M11-durable/D1-concepts.md)、[D2 chord](M11-durable/D2-chord.md)、[D3 存储与事务](M11-durable/D3-storage-transactions.md)、[D4 任务与调度器](M11-durable/D4-tasks-scheduler.md)、[D5 内置任务](M11-durable/D5-builtin-tasks.md)、[D6 扩展与观察](M11-durable/D6-extensions-observation.md) | `Harness.open()` | 已写 |
| [总结：设计取舍与综合练习](summary.md) | — | 已写 |

建议的阅读顺序是 M1 → M11。如果想先看最核心的部分，可以从 M2 开始，M2 只在一处（`streamFn`）依赖 M1，读到那里时先把它当成"返回事件流的函数"即可。

---

## 7. 每个模块的结构

每个模块都按同样的 8 节组织：

1. **这个模块是什么**：职责、在调用链中的位置、文件清单、读完能回答的问题。
2. **先跑一次**：主线场景中和本模块相关的输入输出。
3. **数据结构**：本模块拥有的核心类型。每个类型配源码定义、字段说明和主线场景里的真实实例。
4. **从入口读起**：按调用树逐个函数读源码。每个小节包含调用位置、源码、讲解、往哪走。读到其他模块的函数时标注"→ M几"并跳过。
5. **状态机**：读完流程后，画出本模块的隐式状态机，每条边标注代码行号。
6. **设计要点**：值得学的设计和它们的理由。
7. **实验**：改代码或改输入，预测结果，再运行验证。
8. **自测与衔接**：自测问题和下一个模块的入口。

源码引用格式为 `packages/agent/src/agent-loop.ts:172`，链接指向固定提交上的对应行。
