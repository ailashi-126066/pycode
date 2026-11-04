# 总结：设计取舍与综合练习

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

这是教程的最后一篇。它不引入新模块，做三件事：

1. 把主线场景 `pi -p "读 README.md，总结成三句话"` 的调用栈完整展开一遍，每一步标上所属模块和源码行号（§1）。README §2 的调用链是目录，这里是展开后的版本。
2. 把散在各模块"设计要点"里的几个大取舍放在一起讲：它们各自解决什么问题，代价是什么（§2）。
3. 一个综合练习：写一个扩展，拦下 bash 里的 `rm -rf`，并把每一轮的 token 用量写进会话文件。练习用到 M2、M3、M4、M7、M8 的知识，参考答案在 `lab/` 里，已经实际运行过（§3）。

---

## 1. 主线场景的完整调用栈

下面的调用栈对应 README §5 的那次运行：faux 模型第 1 轮返回文本和一个 `read` 调用，第 2 轮返回总结。每行右边的方括号是模块。行号格式是 `文件:行`，`→` 后面是被调用函数的定义位置。路径省略了 `packages/` 前缀和各包的 `src/`：`coding-agent/` 下的路径写成 `core/…`、`modes/…`、`main.ts`，`agent/` 写成 `agent.ts`、`agent-loop.ts`，`ai/` 写成 `ai/…`。

### 1.1 启动：从 cli.ts 到第一次 prompt 之前

```
cli.ts:6  main(process.argv.slice(2))                                     [M9]
main()                                         main.ts:574               [M9]
├─ extensionFactories = [...builtInExtensions, …]  main.ts:578           [M9 → M10]  内置扩展：llama.cpp、codemode、tool-search、mcp
├─ resolveAppMode()                            main.ts:665 → 112         [M9]  结果 "print"
├─ createSessionManager()                      main.ts:707 → 358         [M9 → M4]  新会话，文件还没建
├─ createAgentSessionRuntime(createRuntime)    main.ts:875 → core/agent-session-runtime.ts:420  [M9]
│   └─ createRuntime(options)  工厂闭包        core/agent-session-runtime.ts:430 → main.ts:744
│       ├─ createAgentSessionServices()        main.ts:761 → core/agent-session-services.ts:135  [M8]
│       │   ├─ ModelRuntime.create()           core/agent-session-services.ts:142 → core/model-runtime.ts:218  [M1]
│       │   ├─ SettingsManager.create()        core/agent-session-services.ts:147 → core/settings-manager.ts:310  [M6]
│       │   └─ resourceLoader.reload()         core/agent-session-services.ts:154 → core/resource-loader.ts:541  [M6]
│       │       └─ loadExtensionsCached()      core/resource-loader.ts:735  加载 faux-ext.ts（jiti）  [M7]
│       └─ createAgentSessionFromServices()    main.ts:851 → core/agent-session-services.ts:214  [M8]
│           └─ createAgentSession()            core/agent-session-services.ts:217 → core/sdk.ts:191  [M8]
│               ├─ sessionManager.buildSessionContext()  core/sdk.ts:210  新会话，没有消息  [M4]
│               ├─ new Agent({ systemPrompt: "", tools: [], streamFn, convertToLlm, transformContext… })
│               │                              core/sdk.ts:403            [M2]
│               └─ new AgentSession()          core/sdk.ts:455 → core/agent-session.ts:498  [M8]
│                   ├─ agent.subscribe(_handleAgentEvent)  core/agent-session.ts:526
│                   ├─ _installAgentToolHooks() … _installAgentForcedPromptProjection()
│                   │                          core/agent-session.ts:527-532  6 个 hook
│                   └─ _buildRuntime()         core/agent-session.ts:534 → 3687
│                       ├─ createAllToolDefinitions()  core/agent-session.ts:3702  read/bash/edit/write  [M3]
│                       └─ new ExtensionRunner()       core/agent-session.ts:3718  [M7]
└─ runPrintMode(runtime)                       main.ts:997 → modes/print-mode.ts:33  [M9]
    └─ rebindSession()                         modes/print-mode.ts:129
        └─ session.bindExtensions()            modes/print-mode.ts:76 → core/agent-session.ts:3339  [M8]
            └─ emit(session_start)             core/agent-session.ts:3360  mcp 扩展读配置（这里没有服务器）  [M7 / M10]
```

到这里，`Agent` 的 `systemPrompt` 还是空字符串，`tools` 还是空数组（M8 §4.1）。系统提示词和工具清单要等第一次 prompt 时才写进 transcript。

### 1.2 一次 prompt：从 session.prompt() 到 agent_settled

```
session.prompt(text)                           modes/print-mode.ts:132 → core/agent-session.ts:2113  [M8]
└─ _startRun()                                 core/agent-session.ts:1894
    ├─ _prepareRun()                           core/agent-session.ts:1899 → 1947  [M8]
    │   ├─ _checkCompaction(lastAssistant)     core/agent-session.ts:1952  新会话没有 assistant 消息，跳过  [M5]
    │   ├─ emitBeforeAgentStart()              core/agent-session.ts:1956 → core/extensions/runner.ts:1427  [M7]
    │   ├─ 构造 user 消息                       core/agent-session.ts:1974-1978
    │   └─ _preparePromptAndToolLoadout()      core/agent-session.ts:1994 → 1779  system 消息：sections + toolsAdded  [M6]
    ├─ agent.prompt(messages)                  core/agent-session.ts:1920 → agent.ts:382  [M2]
    │   └─ runPromptMessages()                 agent.ts:441
    │       └─ runWithLifecycle()              agent.ts:445 → 517
    │           └─ runAgentLoop()              agent.ts:446 → agent-loop.ts:111
    │               └─ runLoop()               agent-loop.ts:172   展开见 §1.3
    ├─ _handlePostAgentRun()                   core/agent-session.ts:1922 → 2000  [M8]
    │   └─ _checkCompaction(message)           core/agent-session.ts:2029 → 3032  没到阈值  [M5]
    └─ _emitAgentSettled()                     core/agent-session.ts:1937 → 1094  agent_settled  [M8]
取 state.messages 最后一条 assistant，打印文本   modes/print-mode.ts:141  [M9]
```

### 1.3 runLoop 内部：两轮

```
runLoop()                                      agent-loop.ts:172                           [M2]
├─ getSteeringMessages()                       agent-loop.ts:185  空
│
├─ 第 1 轮 ───────────────────────────────────────────────────────────────────────────
│  ├─ declareToolChanges()                     agent-loop.ts:220 → 342  _prepareRun 给的 system 消息在队首，工具字段换成实际差异
│  │   └─ 每条消息 emit message_start/end      agent-loop.ts:221-222
│  │       └─ Agent.processEvents()            agent.ts:450 → 575
│  │           └─ await listener(event)        agent.ts:620
│  │               └─ AgentSession._handleAgentEvent()  core/agent-session.ts:1119  [M8]
│  │                   ├─ _emitExtensionEvent()           core/agent-session.ts:1159 → 1319  [M7]
│  │                   ├─ _emit() 给订阅者                 core/agent-session.ts:1160  [M9 print/json 前端]
│  │                   └─ sessionManager.appendMessage()  core/agent-session.ts:1181 → core/session-manager.ts:1214  [M4]
│  │                       └─ _appendEntry() → _persist()  core/session-manager.ts:1191 → 1172  user 消息出现，建文件
│  ├─ prepareRequest()                         agent-loop.ts:228 → core/agent-session.ts:805  [M8]
│  │   └─ sessionManager.buildSessionProjection()  core/agent-session.ts:809 → core/session-manager.ts:1503 → 543  [M4]
│  │        用会话文件重建 context.messages（2 条：system、user）
│  ├─ streamAssistantResponse()                agent-loop.ts:251 → 390  [M2]
│  │   ├─ transformContext()                   agent-loop.ts:400 → core/sdk.ts:428 → core/extensions/runner.ts:1305  context 事件  [M7]
│  │   ├─ convertToLlm()                       agent-loop.ts:404 → core/sdk.ts:297 → core/messages.ts:148  Agent 消息 → LLM 消息  [M8]
│  │   ├─ normalizeContext()                   agent-loop.ts:406  [M1]
│  │   └─ streamFunction()                     agent-loop.ts:412 → core/sdk.ts:412
│  │       └─ ModelRuntime.streamSimple()      core/sdk.ts:422 → core/model-runtime.ts:731  [M1]
│  │           └─ provider.streamSimple()      core/model-runtime.ts:755 → ai/providers/faux.ts:588  [M1]
│  │      事件：message_start / message_update×n / message_end(assistant)，每个都走上面的 processEvents 路径
│  ├─ executeToolCalls()                       agent-loop.ts:279 → 518 → 并行版 596  [M2]
│  │   ├─ emit tool_execution_start            agent-loop.ts:608
│  │   ├─ prepareToolCall()                    agent-loop.ts:614 → 718
│  │   │   └─ config.beforeToolCall()          agent-loop.ts:739 → core/agent-session.ts:669 → 674  [M8]
│  │   │       └─ runner.emitToolCall()        core/agent-session.ts:684 → core/extensions/runner.ts:1249  tool_call 事件  [M7]
│  │   ├─ executePreparedToolCall()            agent-loop.ts:639 → 831
│  │   │   └─ prepared.tool.execute()          agent-loop.ts:842 → core/tools/read.ts:102  [M3]
│  │   ├─ finalizeExecutedToolCall()           agent-loop.ts:640 → 869
│  │   │   └─ config.afterToolCall()           agent-loop.ts:882 → core/agent-session.ts:670 → 700  [M8]
│  │   │       └─ runner.emitToolResult()      core/extensions/runner.ts:1190  tool_result 事件  [M7]
│  │   └─ createToolResultMessage()            agent-loop.ts:661 → 940，emit message_start/end(toolResult)
│  ├─ config.finishTurn()                      agent-loop.ts:295 → core/agent-session.ts:904  [M8]
│  │   └─ _dispatchTurnEndBoundary()           core/agent-session.ts:906 → 862
│  │       └─ runner.emitBoundary()            core/agent-session.ts:882 → core/extensions/runner.ts:1036  扩展 turn_end  [M7]
│  └─ emit turn_end                            agent-loop.ts:296
│
├─ 第 2 轮 ───────────────────────────────────────────────────────────────────────────
│  ├─ config.prepareNextTurn()                 agent-loop.ts:195 → core/agent-session.ts:920  [M8]
│  │   ├─ _compactBeforeNextAssistantResponse()  core/agent-session.ts:921 → 792  轮间压缩检查  [M5]
│  │   └─ _preparePromptAndToolLoadout()       core/agent-session.ts:931  提示词没变，不出补丁  [M6]
│  ├─ emit turn_start                          agent-loop.ts:216
│  ├─ prepareRequest() → 投影出 4 条消息        agent-loop.ts:228  [M8 → M4]
│  ├─ streamAssistantResponse()                agent-loop.ts:251  总结文本，没有工具调用  [M2 → M1]
│  ├─ config.finishTurn() / emit turn_end      agent-loop.ts:295-296
│  └─ hasMoreToolCalls=false，steering 为空，内层 while 结束
│
├─ getFollowUpMessages()                       agent-loop.ts:311  空
└─ emit agent_end                              agent-loop.ts:329
```

### 1.4 主线没有走到的部分

- **M5 压缩**：主线只经过三个检查点（§1.2 的两处 `_checkCompaction`，§1.3 的 `_compactBeforeNextAssistantResponse`），都没到阈值。真正压缩时，`_runAutoCompaction` 往会话文件追加一个 `compaction` 条目，下一次投影用摘要替换旧消息（M5 §4.11–4.13）。
- **M3 的 bash/edit/write 和嵌套调用**：主线只调用了 `read`。工具通过 `ctx.executeTool()` 调用别的工具时，走 `runToolCall`（`packages/agent/src/agent-loop.ts:821`），和模型发起的调用经过同一组 `beforeToolCall`/`afterToolCall`（M3 §4.15–4.16）。
- **M9 的 json / rpc 模式**：只是换一个前端订阅同一个 `AgentSession`（M9 §6.1）。json 模式还在 `session.agent` 上挂了一个 await 背压的监听器（`packages/coding-agent/src/modes/print-mode.ts:113-118`）。
- **M10 MCP**：内置的 `mcp` 扩展在 `session_start` 读配置。没有启用的服务器时，它在 `packages/coding-agent/src/extensions/mcp/index.ts:1134-1137` 直接返回，连 MCP 客户端模块都不加载（1138-1140 行）。配置了服务器时，MCP 工具就是普通的扩展工具，同样经过 `tool_call`（M10 §6.2）。
- **M11 durable**：不在这条调用栈上。它是另一套实现，把 `runLoop` 的每一段换成持久化的任务（D5 §4.13 的对照表），§2.6 会再对照一次。

---

## 2. 设计取舍

每个模块的"设计要点"讲的是局部的选择。这一节挑出贯穿多个模块的五个选择，再和 durable 的做法对照一次。每一项都按"做法、理由、代价、代码位置"来讲。

### 2.1 循环和产品分离：产品逻辑全部通过 hook 接入

**做法。** M2 的 `agent-loop.ts` 只规定一次运行的规则：什么时候请求模型、工具怎么执行、什么时候停。它不读文件，不认识会话、扩展、压缩。M8 的 `AgentSession` 在构造函数里给 `Agent` 的公开字段赋值，把产品逻辑装进这些 hook（§1.1 的 `core/agent-session.ts:527-532`）。工具权限就是一个例子：循环只知道"调用 `beforeToolCall`，返回 `block` 就不执行"。

源码：[`packages/agent/src/agent-loop.ts:738-766`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L738-L766)

```ts
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
```

M8 把这个 hook 接到扩展的 `tool_call` 事件上：

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

**理由。** 循环的规则可以脱离产品单独测试。M2 的 `lab/m2-loop.ts` 不需要会话文件和扩展，几十行就能驱动整个循环。规则写在一处，也方便对照：durable（M11）没有复用这份代码，而是把同样的轮次规则重写成持久化的任务，它的代码注释里直接以 agent 循环的行为为准（`packages/durable/src/harness/generation.ts:768`）。

**代价。** 一个功能的代码分散在几层。想知道"工具被拦下时发生了什么"，要先读 M2 的 `prepareToolCall`，再读 M8 的 `_beforeToolCall`，再读 M7 的 `emitToolCall`。hook 串联的顺序取决于 `_install*` 的调用顺序（M8 §6.2），要读构造函数才知道。

**代码位置。** `packages/agent/src/types.ts:193`（`AgentLoopConfig`），`packages/coding-agent/src/core/agent-session.ts:527-532`。

### 2.2 会话文件是上下文的唯一来源

**做法。** 每次请求模型之前，`prepareRequest` 用会话文件的投影替换循环传入的 `context.messages`：

源码：[`packages/coding-agent/src/core/agent-session.ts:803-815`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L803-L815)

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
```

压缩、重试、扩展的 `context_edit` 都只往会话文件追加条目，然后从投影刷新 `agent.state.messages`（`_refreshFinalizedContext`，`packages/coding-agent/src/core/agent-session.ts:954`）。

**理由。** 会改上下文的地方很多：压缩、重试时去掉失败回复、扩展编辑、分支切换。只要它们都"追加条目，再投影"，模型看到的上下文就总能从会话文件复现。`pi -c` 恢复后的第一次请求和进程没退出时一样（M4 §6.2、M8 §6.1）。

**代价。** 每次请求都要沿当前分支遍历一次条目。直接塞进 `agent.state.messages` 的消息会被投影覆盖，不会发给模型（M8 §4.5 的 `projection` 场景），扩展必须走 `sendMessage`、`appendEntry` 或边界草稿这些写会话文件的入口。§3 的练习就利用了这一点：`custom` 条目写进了文件，但投影规则让它不进上下文。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:803`，`packages/coding-agent/src/core/session-manager.ts:543`。

### 2.3 工具清单写进 transcript

**做法。** 系统提示词和工具声明都不是请求的单独字段，而是 transcript 里的 `system` 消息。`declareToolChanges` 每次请求前比较"transcript 声明过的工具"和"现在能执行的工具"（`context.tools`），把差异写成 `system` 消息上的 `toolsAdded`/`toolsRemoved`。待发送的消息里已经有 `system` 消息时，改写它的工具字段；没有时，插入一条新的：

源码：[`packages/agent/src/agent-loop.ts:356-371`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent-loop.ts#L356-L371)

```ts
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
```

**理由。** 工具清单的变化和消息一样有了时间顺序。会话文件能重放出任意时刻模型看到的工具；中途加减工具只追加一条消息，不改写前面的 transcript。请求前缀因此保持稳定，提示词缓存可以命中（M1 §6.4）。

**代价。** 复杂度转给了 provider 层。不支持中途 `system` 消息或中途改工具的 API，要由 `resolveTranscript`、`collapseSystemMessages`、`resolveTranscriptTools` 把 transcript 变回自己能接受的形状（M1 §4.14）。`context.tools` 和 transcript 声明是两份数据，`prepareRequest` 要同时维护它们（§2.2 源码 813-814 行的注释）。

**代码位置。** `packages/agent/src/agent-loop.ts:342`，`packages/ai/src/types.ts:800`（`TranscriptContext`）。

### 2.4 事件监听器被 await

**做法。** 循环里每次 `emit` 都被 await，`Agent.processEvents` 再依次 await 每个监听器：

源码：[`packages/agent/src/agent.ts:615-621`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/agent/src/agent.ts#L615-L621)

```ts
		const signal = this.activeRun?.abortController.signal;
		if (!signal) {
			throw new Error("Agent listener invoked outside active run");
		}
		for (const listener of this.listeners) {
			await listener(event, signal);
		}
```

M8 的 `_handleAgentEvent` 就是其中一个监听器。它内部先 await 扩展，再同步通知订阅者，最后写会话文件：

源码：[`packages/coding-agent/src/core/agent-session.ts:1158-1163`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1158-L1163)

```ts
		// Emit to extensions first, then notify public listeners.
		await this._emitExtensionEvent(event);
		this._emit(event.type === "agent_end" ? { ...event, willRetry: this._willRetryAfterAgentEnd(event) } : event);

		// Handle session persistence
		if (event.type === "message_end") {
```

**理由。** 上层在事件里做的事有先后要求。`message_end` 的消息要先写进会话文件，下一次 `prepareRequest` 才能从会话文件投影出它（§2.2）。扩展可以改写 `message_end` 的消息，所以扩展要在持久化之前运行。await 让循环等上层做完，这些顺序不需要额外的锁或队列。json 模式的 stdout 背压也借用了这一点（M9 §6.4）。

**代价。** 慢的监听器会拖慢循环。M8 因此只 await 扩展和自己的持久化，不 await 公开订阅者（`session.subscribe()` 的监听器返回 `void`，M8 §6.3）。需要完全不阻塞的场合，M2 另外提供了 `agentLoop` 的事件流版本，代价是消费者会落后于循环（M2 §4.19）。

**代码位置。** `packages/agent/src/agent.ts:619-621`，`packages/coding-agent/src/core/agent-session.ts:1158-1163`。

### 2.5 不内置子 agent 和 plan 模式

**做法。** pi 的 README 直接写明了这一点：

源码：[`packages/coding-agent/README.md:19`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/README.md#L19)

```text
Pi ships with powerful defaults but skips features like sub-agents and plan mode. Ask Pi to build what you want, or install a package that does it your way.
```

这两个功能以示例扩展的形式提供，只用公开的扩展 API。`plan-mode` 用 `tool_call` 拦下非只读的 bash 命令，用 `pi.setActiveTools()` 去掉 edit/write：

源码：[`packages/coding-agent/examples/extensions/plan-mode/index.ts:164-174`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/examples/extensions/plan-mode/index.ts#L164-L174)

```ts
	pi.on("tool_call", async (event) => {
		if (!planModeEnabled || event.toolName !== "bash") return;

		const command = event.input.command as string;
		if (!isSafeCommand(command)) {
			return {
				block: true,
				reason: `Plan mode: command blocked (not allowlisted). Use /plan to disable plan mode first.\nCommand: ${command}`,
			};
		}
	});
```

`subagent` 注册一个工具，工具里启动一个新的 pi 进程，用 json 模式读它的事件流：

源码：[`packages/coding-agent/examples/extensions/subagent/index.ts:298`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/examples/extensions/subagent/index.ts#L298)

```ts
	const args: string[] = ["--mode", "json", "-p", "--no-session"];
```

**理由。** 子 agent 和 plan 模式各有很多种做法：子 agent 用独立进程还是同进程、共享多少上下文；plan 模式怎么定义"只读"、计划怎么跟踪。核心只提供足够的接入点（`tool_call`、`setActiveTools`、`registerTool`、`appendEntry`、`before_agent_start`），具体策略留给扩展。核心保持小，前面 §2.1–2.4 的规则不需要为这些功能开特例。

**代价。** 开箱即用的体验要靠安装扩展。扩展实现的功能和核心之间没有类型层面的约定，例如 `plan-mode` 的状态保存在它自己定义的 `custom` 条目里（`packages/coding-agent/examples/extensions/plan-mode/index.ts:116-122`），别的扩展读不懂。durable 包在存储层支持"由任务拥有的对话"（D1 §3），这是另一种思路：把子 agent 做成持久的子任务（D5 §4.10 提到的 `21-subagent-foreground.ts`）。

**代码位置。** `packages/coding-agent/README.md:19`，`packages/coding-agent/examples/extensions/plan-mode/index.ts:108`，`packages/coding-agent/examples/extensions/subagent/index.ts:344`。

### 2.6 对照：durable 怎样回答同样的问题

M11 的 durable 包实现了同样的轮次规则（D5 §4.13），但对上面几个问题给了不同的答案。

| 问题 | M2 + M8（主线） | durable（M11） |
|---|---|---|
| 状态在哪里 | 循环状态在调用栈和 `Agent` 的内存里；会话文件记录消息和设置变化，不记录循环走到了哪一步 | 循环状态在任务记录和 `pi.live` 等文档里，每个阶段一次提交（D4 §4.7） |
| 上下文来源 | 会话文件的投影（§2.2） | 已提交的条目（D1 §6.1、D5 §4.13） |
| 先显示还是先存储 | 先给扩展和订阅者看，再写会话文件（§2.4） | 先存储，提交确认后才对外可见（D1 §6.1） |
| 进程崩溃 | 没写完的轮次丢失；`pi -c` 从最后写入的条目继续 | 从检查点恢复；工具按 `replay` 策略重跑或得到 `interrupted` 结果（D5 §6.1） |
| 扩展接入 | `ExtensionRunner` 派发事件（M7） | 任务上的 hook，按会话解析（D6 §6.3） |
| 观察 | 事件流（AgentEvent → AgentSessionEvent） | 没有事件流，观察提交本身（D6 §6.1） |

主线的做法更简单，每一步都是内存里的函数调用，代价是崩溃时会丢掉正在进行的那一轮。durable 每一步都要等存储写入（D1 §6.1 的代价），换来任意时刻崩溃都能继续。

---

## 3. 综合练习：拦下 rm -rf，记录每一轮的用量

### 3.1 题目

写一个 pi 扩展，满足两条要求：

1. **拦截**：模型调用 `bash`，命令里有带递归和强制选项的 `rm`（`rm -rf`、`rm -fr`、`rm -r -f`、`rm --recursive --force`）时，命令不执行，模型收到一条 `isError: true` 的工具结果，说明被拦下的原因。
2. **记账**：每一轮结束时，把这一轮 assistant 消息的 `usage`（`input`、`output`、`cacheRead`、`cacheWrite`、`totalTokens`）写进会话文件，作为一个 `customType` 为 `"turn-usage"` 的 `custom` 条目，并记下它对应的 assistant 条目 id。这些记录不能发给模型。

练习配套的假模型是 `lab/summary-faux.ts`，一共 3 轮：

| 轮 | 模型的回复 |
|---|---|
| 1 | 调用 `bash`，命令是环境变量 `SUMMARY_CMD`，默认 `rm -rf build` |
| 2 | 复述第一个命令的工具结果，再调用 `bash` 执行 `ls` |
| 3 | 复述 `ls` 的结果，结束 |

它每次被请求时把收到的消息角色打印到 stderr，用来确认 `custom` 条目没有发给模型。

检查脚本是 `lab/summary-check.mjs`。它读一个会话文件，检查 `rm` 调用的工具结果是不是 `isError: true`，`turn-usage` 条目数是不是等于 assistant 条目数，每个 `turn-usage` 的 usage 是不是和它指向的 assistant 条目一致。

### 3.2 提示

动手前，先在 §1.3 的调用栈上找到两个要求各自的接入点。

- 拦截：`prepareToolCall` 里的 `beforeToolCall`（`packages/agent/src/agent-loop.ts:739`）。M8 把它接到扩展的 `tool_call` 事件，handler 返回 `{ block: true, reason }`，循环就用 `reason` 生成错误结果，不调用 `tool.execute()`（§2.1 的两段源码）。`event.input` 是校验过的参数。`isToolCallEventType("bash", event)` 可以把 `event.input` 收窄成 bash 的参数类型（`packages/coding-agent/src/core/extensions/types.ts:1408`）。
- 记账：扩展的 `turn_end` 事件由 `finishTurn` 派发，在下一轮请求之前（`packages/agent/src/agent-loop.ts:295`）。`turn_end` 是边界事件，handler 可以返回 `entries` 草稿，AgentSession 会把它们写进会话文件（M7 §4.9）。注意 `emitBoundary` 用返回值**替换**草稿列表：

源码：[`packages/coding-agent/src/core/extensions/runner.ts:1046-1057`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L1046-L1057)

```ts
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
```

- 不进上下文：`custom` 条目在投影时不产生任何消息：

源码：[`packages/coding-agent/src/core/session-manager.ts:435-439`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L435-L439)

```ts
/**
 * Project one selected session entry into LLM/runtime messages.
 * Plain custom entries are display/state entries and do not participate in context.
 */
export function sessionEntryToContextMessages(entry: SessionEntry): AgentMessage[] {
```

（函数末尾 `packages/coding-agent/src/core/session-manager.ts:465` 对其他条目类型返回 `[]`。）

### 3.3 先预测

用参考答案运行默认场景（`rm -rf build`），在看结果之前回答：

1. `build` 目录还在吗？第 2 轮模型收到的工具结果是什么？
2. 会话文件里有几个 `turn-usage` 条目？它们分别排在哪两个条目之间？
3. 第 2、3 次请求，模型分别收到几条消息？
4. json 模式的事件流里，`entry_appended` 出现在 `turn_end` 之前还是之后？

### 3.4 参考答案

`lab/summary-guard.ts`：

```ts
// 总结篇综合练习的参考答案：一个 pi 扩展，做两件事。
//   1. tool_call：bash 命令里出现 rm 同时带递归和强制选项（rm -rf、rm -fr、rm -r -f、
//      rm --recursive --force 等）时拦下，工具不执行，模型收到一条 isError 的工具结果。
//   2. turn_end：把这一轮 assistant 消息的 usage 作为 custom 条目（customType "turn-usage"）
//      写进会话文件。custom 条目只用于保存状态，不会发给模型。
// 用法（配合 summary-faux.ts 假模型）：
//   EXT=$PWD/summary-faux.ts PI=/path/to/pi ./run.sh -e "$PWD/summary-guard.ts" -p "清理构建目录"
// 检查结果：
//   node summary-check.mjs "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)"
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { isToolCallEventType } from "@earendil-works/pi-coding-agent";

// 把命令按 shell 分隔符切成片段，逐段检查 rm 的选项。
// 这是字符串检查，不是沙箱：bash -c "$(...)"、变量展开、别名都能绕过它。
function isRecursiveForceRm(command: string): boolean {
	for (const segment of command.split(/[;&|\n]+/)) {
		const words = segment.trim().split(/\s+/);
		const rmIndex = words.findIndex((w) => w === "rm" || w.endsWith("/rm"));
		if (rmIndex === -1) continue;
		let recursive = false;
		let force = false;
		for (const word of words.slice(rmIndex + 1)) {
			if (word === "--") break;
			if (word === "--recursive") recursive = true;
			else if (word === "--force") force = true;
			else if (/^-[a-zA-Z]+$/.test(word)) {
				if (/[rR]/.test(word)) recursive = true;
				if (word.includes("f")) force = true;
			}
		}
		if (recursive && force) return true;
	}
	return false;
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event) => {
		if (!isToolCallEventType("bash", event)) return undefined;
		if (!isRecursiveForceRm(event.input.command)) return undefined;
		return { block: true, reason: `Blocked by summary-guard: recursive force rm is not allowed (${event.input.command})` };
	});

	pi.on("turn_end", async (event) => {
		const message = event.message;
		if (message.role !== "assistant") return undefined;
		const { input, output, cacheRead, cacheWrite, totalTokens } = message.usage;
		// event.entries 是前面的 handler 已经加的草稿。返回值会替换它，所以要在后面追加。
		return {
			entries: [
				...event.entries,
				{
					type: "custom",
					customType: "turn-usage",
					data: {
						turnIndex: event.turnIndex,
						messageEntryId: event.messageEntryId,
						model: `${message.provider}/${message.model}`,
						stopReason: message.stopReason,
						usage: { input, output, cacheRead, cacheWrite, totalTokens },
					},
				},
			],
		};
	});
}
```

拦截部分没有用正则直接匹配整条命令，而是按 `;`、`&`、`|`、换行切成片段，在每一段里找 `rm`，再看它后面的选项。这样 `rm -r -f`、`rm -Rf` 和 `/bin/rm` 都能识别。§3.7 会看到它的局限。

记账部分在 `turn_end` 里返回 `[...event.entries, 新草稿]`，保留前面的扩展已经加的草稿。

### 3.5 运行和检查

在 `WORK` 里先建一个 `build` 目录，再运行：

```bash
mkdir -p /tmp/pi-lab/summary-work-2/build && touch /tmp/pi-lab/summary-work-2/build/out.o
WORK=/tmp/pi-lab/summary-work-2 PI_CODING_AGENT_DIR=/tmp/pi-lab/summary-agent-2 EXT=$PWD/summary-faux.ts \
  ./run.sh -e "$PWD/summary-guard.ts" -p "清理构建目录"
ls /tmp/pi-lab/summary-work-2/build
```

```
Running without API keys...
[faux] 第 1 次请求，2 条消息：system,user
[faux] 第 2 次请求，4 条消息：system,user,assistant,toolResult
[faux] 第 3 次请求，6 条消息：system,user,assistant,toolResult,assistant,toolResult
目录内容（isError=false：README.md build）。
out.o
```

`build/out.o` 还在。第 2、3 次请求分别收到 4 条和 6 条消息，没有 `custom`。

看会话文件，再跑检查脚本：

```bash
F="$(ls -t /tmp/pi-lab/summary-agent-2/sessions/*/*.jsonl | head -1)"
node show-session.mjs "$F"
node summary-check.mjs "$F"; echo "exit=$?"
```

```
session                id=01a196d8 parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/summary-work-2"}
model_change           id=29a8ce44 parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=8495d23d parent=29a8ce44 {"thinkingLevel":"off"}
message                id=9345a503 parent=8495d23d system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write
message                id=f12e9581 parent=9345a503 user       [{"type":"text","text":"清理构建目录"}]
message                id=f98f3053 parent=f12e9581 assistant  [{"type":"toolCall","id":"call_1","name":"bash","arguments":{"command":"rm -rf build"}}] stop=toolUse
message                id=de75bbe0 parent=f98f3053 toolResult [{"type":"text","text":"Blocked by summary-guard: recursive force rm is not allowed (rm -r
custom                 id=3531295d parent=de75bbe0 {"customType":"turn-usage","data":{"turnIndex":0,"messageEntryId":"f98f3053","model":"faux/faux-1","
message                id=001b022d parent=3531295d assistant  [{"type":"text","text":"第一个命令的结果（isError=true：Blocked by summary-guard: recursive force rm stop=toolUse
message                id=378f8534 parent=001b022d toolResult [{"type":"text","text":"README.md\nbuild\n"}]
custom                 id=78fc341c parent=378f8534 {"customType":"turn-usage","data":{"turnIndex":1,"messageEntryId":"001b022d","model":"faux/faux-1","
message                id=0580f9c9 parent=78fc341c assistant  [{"type":"text","text":"目录内容（isError=false：README.md build）。"}] stop=stop
custom                 id=278e758a parent=0580f9c9 {"customType":"turn-usage","data":{"turnIndex":2,"messageEntryId":"0580f9c9","model":"faux/faux-1","
PASS 有 rm 调用：rm -rf build
PASS call_1 的工具结果 isError=true：Blocked by summary-guard: recursive force rm is not allowed (rm -rf build)
PASS assistant 条目 3 个，turn-usage 条目 3 个
PASS turnIndex=0 → f98f3053 {"input":0,"output":8,"cacheRead":0,"cacheWrite":1309,"totalTokens":1317}
PASS turnIndex=1 → 001b022d {"input":0,"output":32,"cacheRead":1309,"cacheWrite":34,"totalTokens":1375}
PASS turnIndex=2 → 0580f9c9 {"input":0,"output":9,"cacheRead":1343,"cacheWrite":43,"totalTokens":1395}
totalTokens 合计 4087
exit=0
```

id 每次运行都不同。`output` 和 `cacheWrite` 也可能差几个 token：第 2、3 轮的回复里复述了工具结果，`cacheWrite` 还随 `WORK` 路径的长度变化，因为系统提示词的 `cwd` 段里有这个路径。`input` 都是 0，是因为 faux 用 `sessionId` 模拟了提示词缓存，把每个提示词 token 都算进 `cacheRead` 或 `cacheWrite`（`packages/ai/src/providers/faux.ts:262-272`）。

对照 §3.3 的预测：

1. `build` 还在。第 2 轮模型收到的工具结果是 `isError=true` 和 `reason` 的文本。
2. 3 个 `turn-usage` 条目，各自排在一轮的最后一条消息（toolResult 或最终的 assistant）之后。前两个后面紧跟下一轮的 assistant，最后一个在文件末尾。每个 `turn-usage` 的 `parent` 是它前面那条消息，下一条 assistant 的 `parent` 是它。
3. 4 条和 6 条。`custom` 条目在路径上，但投影时不产生消息。
4. 在 `turn_end` 之前，见 §3.7 的实验 3。

### 3.6 讲解：两个 handler 在调用栈上的位置

**拦截。** 第 1 轮的 `bash` 调用沿 §1.3 的工具分支走：

```
executeToolCallsParallel()                    agent-loop.ts:596
├─ emit tool_execution_start                  agent-loop.ts:608
├─ prepareToolCall()                          agent-loop.ts:614 → 718
│   └─ beforeToolCall → _beforeToolCall → emitToolCall → summary-guard 的 tool_call handler
│       返回 { block: true, reason }         emitToolCall 立刻返回（runner.ts:1259-1260）
│   → { kind: "immediate", result: createErrorToolResult(reason), isError: true }  agent-loop.ts:755-764
├─ immediate：直接 emitToolExecutionEnd，不调用 execute 和 afterToolCall  agent-loop.ts:615-626
└─ 批次结束后 createToolResultMessage，emit message_start/end(toolResult)  agent-loop.ts:660-663
```

所以被拦下的调用在事件流里仍然有 `tool_execution_start` 和 `tool_execution_end`，只是中间没有 `tool_execution_update`（§3.7 实验 3 的输出）。工具结果是一条普通的 `toolResult` 消息，写进会话文件，模型下一轮能看到它并换一种做法。这是 M2 §6.5"失败是数据"的一个实例。

`tool_call` 是失败时关闭的：handler 抛异常时，`_beforeToolCall` 把异常继续抛出（`packages/coding-agent/src/core/agent-session.ts:691-695`），`prepareToolCall` 的 `catch` 把它变成错误结果（`packages/agent/src/agent-loop.ts:780-785`），工具同样不执行（M7 §6.4）。

**记账。** 每一轮结束时：

```
runLoop                                        agent-loop.ts:295  config.finishTurn()
└─ _installAgentBoundaryHooks 装的 finishTurn  core/agent-session.ts:904
    └─ _dispatchTurnEndBoundary()              core/agent-session.ts:862
        ├─ _findPersistedMessageEntryId()      这一轮 assistant 的条目 id → event.messageEntryId
        ├─ runner.emitBoundary()               core/extensions/runner.ts:1036
        │   └─ summary-guard 的 turn_end handler 返回 entries
        │       每个 handler 之后用 _buildBoundaryContext(entries) 在内存副本上预演草稿
        └─ _commitBoundaryDrafts(entries)      core/agent-session.ts:894 → 1040
            ├─ _applyBoundaryDrafts()          core/agent-session.ts:962：custom → appendCustomEntry
            ├─ _refreshFinalizedContext()
            └─ _emit(entry_appended)
runLoop                                        agent-loop.ts:296  emit turn_end
```

`_applyBoundaryDrafts` 把 `custom` 草稿交给 `SessionManager.appendCustomEntry`：

源码：[`packages/coding-agent/src/core/agent-session.ts:962-969`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L962-L969)

```ts
	private _applyBoundaryDrafts(manager: SessionManager, drafts: SessionBoundaryDraft[]): SessionEntry[] {
		const appended: SessionEntry[] = [];
		for (const draft of drafts) {
			let entryId: string;
			switch (draft.type) {
				case "custom":
					entryId = manager.appendCustomEntry(draft.customType, draft.data);
					break;
```

`appendCustomEntry` 和 `appendMessage` 一样走 `_appendEntry`，`parentId` 取当前的 leaf（`packages/coding-agent/src/core/session-manager.ts:1300-1311`）。这就是 `turn-usage` 条目接在这一轮最后一条消息后面的原因。

`finishTurn` 被 await，草稿提交完之后循环才进入下一轮的 `prepareRequest`（§2.4）。下一次投影沿 leaf 往回走，会经过 `custom` 条目，但 `sessionEntryToContextMessages` 对它返回 `[]`（§3.2），所以模型收到的消息数不变（§2.2）。

**为什么用草稿而不是 `pi.appendEntry()`。** 在 `turn_end` 里直接调用 `pi.appendEntry("turn-usage", …)`，条目也会落在同样的位置，因为它同样调用 `appendCustomEntry`（`packages/coding-agent/src/core/agent-session.ts:3487-3488`）。草稿的好处在于它是边界协议的一部分：同一个边界上的多个扩展能看到彼此的草稿（`event.entries`），每个 handler 之后都会在内存副本上预演一次（`packages/coding-agent/src/core/agent-session.ts:1003-1009`），有一个草稿不合法时整批丢弃（`packages/coding-agent/src/core/extensions/runner.ts:1082-1084`）。对 `custom` 条目这些好处不明显，对 `context_edit` 和 `compaction` 草稿就很重要。

**为什么用 turn_end 而不是 message_end。** 见 §3.7 的实验 4。

### 3.7 实验

每个实验先写预测，再运行验证。每次运行都用新的 `WORK` 和 `PI_CODING_AGENT_DIR`，先建好 `build` 目录。id、token 数、`message_update` 和 `tool_execution_update` 的个数每次运行可能不同。

```bash
for n in 4 5 6 7 8 9 10; do mkdir -p /tmp/pi-lab/summary-work-$n/build; touch /tmp/pi-lab/summary-work-$n/build/out.o; done
export EXT=$PWD/summary-faux.ts
```

#### 实验 1：不加载扩展

**预测。** 没有 `tool_call` handler，`_beforeToolCall` 直接返回 `undefined`（`packages/coding-agent/src/core/agent-session.ts:679-681`），`rm -rf build` 会执行。也没有 `turn-usage` 条目，检查脚本应该失败。

```bash
WORK=/tmp/pi-lab/summary-work-4 PI_CODING_AGENT_DIR=/tmp/pi-lab/summary-agent-4 ./run.sh -p "清理构建目录"
ls /tmp/pi-lab/summary-work-4
F="$(ls -t /tmp/pi-lab/summary-agent-4/sessions/*/*.jsonl | head -1)"; node summary-check.mjs "$F"; echo "exit=$?"
```

```
Running without API keys...
[faux] 第 1 次请求，2 条消息：system,user
[faux] 第 2 次请求，4 条消息：system,user,assistant,toolResult
[faux] 第 3 次请求，6 条消息：system,user,assistant,toolResult,assistant,toolResult
目录内容（isError=false：README.md）。
README.md
PASS 有 rm 调用：rm -rf build
FAIL call_1 的工具结果 isError=false：(no output)
FAIL assistant 条目 3 个，turn-usage 条目 0 个
totalTokens 合计 0
exit=1
```

**结论。** 预测成立。`build` 被删掉了，`ls` 只列出 `README.md`。

#### 实验 2：换几种命令

**预测。** 用 `SUMMARY_CMD` 换掉第 1 轮的命令：

| 命令 | 预测 |
|---|---|
| `rm -r -f build` | 拦下：分开写的 `-r` 和 `-f` 都会被识别 |
| `cd . && /bin/rm --recursive --force build` | 拦下：`&&` 切出第二段，`/bin/rm` 以 `/rm` 结尾 |
| `rm -r build` | 放行：只有递归，没有强制 |
| `echo rm -rf build` | 拦下：这是误判，`echo` 的参数里也有 `rm` 这个词 |
| `sh -c 'rm -rf build'` | 放行：这是漏判，按空白切开后，`rm` 前面带着引号，不等于 `rm` |

```bash
n=5; for c in "rm -r -f build" "cd . && /bin/rm --recursive --force build" "rm -r build" "echo rm -rf build"; do
  echo "== $n: $c"
  WORK=/tmp/pi-lab/summary-work-$n PI_CODING_AGENT_DIR=/tmp/pi-lab/summary-agent-$n SUMMARY_CMD="$c" \
    ./run.sh -e "$PWD/summary-guard.ts" -p "清理构建目录" 2>/dev/null
  echo "build: $(ls /tmp/pi-lab/summary-work-$n/build 2>&1)"
  n=$((n+1))
done
n=9; c="sh -c 'rm -rf build'"; echo "== $n: $c"
WORK=/tmp/pi-lab/summary-work-$n PI_CODING_AGENT_DIR=/tmp/pi-lab/summary-agent-$n SUMMARY_CMD="$c" \
  ./run.sh -e "$PWD/summary-guard.ts" -p "清理构建目录" 2>/dev/null
echo "build: $(ls /tmp/pi-lab/summary-work-$n/build 2>&1)"
```

```
== 5: rm -r -f build
Running without API keys...
目录内容（isError=false：README.md build）。
build: out.o
== 6: cd . && /bin/rm --recursive --force build
Running without API keys...
目录内容（isError=false：README.md build）。
build: out.o
== 7: rm -r build
Running without API keys...
目录内容（isError=false：README.md）。
build: ls: /tmp/pi-lab/summary-work-7/build: No such file or directory
== 8: echo rm -rf build
Running without API keys...
目录内容（isError=false：README.md build）。
build: out.o
== 9: sh -c 'rm -rf build'
Running without API keys...
目录内容（isError=false：README.md）。
build: ls: /tmp/pi-lab/summary-work-9/build: No such file or directory
```

第 1 个命令的工具结果在第 2 轮 assistant 消息的文本里，从会话文件取出来：

```bash
for n in 5 6 7 8; do F="$(ls -t /tmp/pi-lab/summary-agent-$n/sessions/*/*.jsonl | head -1)"; node -e 'const fs=require("fs");for(const l of fs.readFileSync(process.argv[1],"utf8").trim().split("\n")){const e=JSON.parse(l);if(e.type==="message"&&e.message.role==="assistant"){const t=e.message.content.find(c=>c.type==="text");if(t&&t.text.startsWith("第一个"))console.log(process.argv[2]+": "+t.text)}}' "$F" $n; done
```

```
5: 第一个命令的结果（isError=true：Blocked by summary-guard: recursive force rm is not allowed (rm -r -f build)）。再看看目录。
6: 第一个命令的结果（isError=true：Blocked by summary-guard: recursive force rm is not allowed (cd . && /bin/rm --recursive --force build)）。再看看目录。
7: 第一个命令的结果（isError=false：(no output)）。再看看目录。
8: 第一个命令的结果（isError=true：Blocked by summary-guard: recursive force rm is not allowed (echo rm -rf build)）。再看看目录。
```

**结论。** 预测成立。`tool_call` 看到的只是模型给出的命令字符串，字符串检查总会有误判和漏判。官方的 `permission-gate.ts` 用一组正则匹配整条命令（`packages/coding-agent/examples/extensions/permission-gate.ts:11`）。它能拦下 `sh -c 'rm -rf build'`，但同样会误判 `echo rm -rf build`，也挡不住用变量拼出来的命令。要真正防止删除，需要在执行层隔离，例如通过 bash 工具的 `operations` 选项换成在沙箱里执行的 `BashOperations`（M3 §4.6）。

#### 实验 3：事件顺序

**预测。** `finishTurn`（295 行）在 `emit turn_end`（296 行）之前，草稿在 `finishTurn` 里提交并发出 `entry_appended`，所以每轮的 `entry_appended` 在 `turn_end` 之前。被拦下的 `bash` 调用有 `tool_execution_start/end`，没有 `tool_execution_update`；放行的 `ls` 有。

```bash
WORK=/tmp/pi-lab/summary-work-3 PI_CODING_AGENT_DIR=/tmp/pi-lab/summary-agent-3 \
  ./run.sh -e "$PWD/summary-guard.ts" --mode json "清理构建目录" > /tmp/pi-lab/summary-events-3.jsonl 2>/dev/null
node summarize-events.mjs /tmp/pi-lab/summary-events-3.jsonl
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
tool_execution_start bash
tool_execution_end bash
message_start (toolResult)
message_end (toolResult)
entry_appended
turn_end (assistant)
turn_start
message_start (assistant)
message_update [text_start]
message_update [text_delta] x7
message_update [text_end]
message_update [toolcall_start]
message_update [toolcall_delta]
message_update [toolcall_end]
message_end (assistant)
tool_execution_start bash
tool_execution_update bash x2
tool_execution_end bash
message_start (toolResult)
message_end (toolResult)
entry_appended
turn_end (assistant)
turn_start
message_start (assistant)
message_update [text_start]
message_update [text_delta] x3
message_update [text_end]
message_end (assistant)
entry_appended
turn_end (assistant)
agent_end
agent_settled
共 54 行（含第一行 session 头）
```

**结论。** 预测成立。`entry_appended` 是 `AgentSessionEvent`，不是 `AgentEvent`（README §3.2 的事件层次）。`message_update` 和 `tool_execution_update` 的个数每次运行可能不同。

#### 实验 4：改在 message_end 里记账

`lab/summary-message-end.ts` 是一个错误写法：在 `message_end` 里用 `pi.appendEntry()` 记录 assistant 消息的用量。

**预测。** §2.4 的第二段源码说明，`_handleAgentEvent` 先 await 扩展（1159 行），后写 `message` 条目（1163 行起）。扩展在 `message_end` 里追加的条目会排在它记录的那条 assistant 消息**之前**，而且那时 assistant 消息还没有条目 id。这个实验不加载 `summary-guard.ts`，所以 `rm -rf build` 会执行。

```bash
WORK=/tmp/pi-lab/summary-work-10 PI_CODING_AGENT_DIR=/tmp/pi-lab/summary-agent-10 \
  ./run.sh -e "$PWD/summary-message-end.ts" -p "清理构建目录"
F="$(ls -t /tmp/pi-lab/summary-agent-10/sessions/*/*.jsonl | head -1)"; node show-session.mjs "$F"
```

```
Running without API keys...
[faux] 第 1 次请求，2 条消息：system,user
[faux] 第 2 次请求，4 条消息：system,user,assistant,toolResult
[faux] 第 3 次请求，6 条消息：system,user,assistant,toolResult,assistant,toolResult
目录内容（isError=false：README.md）。
session                id=01a196dc parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/summary-work-10"}
model_change           id=bc415fe6 parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=732e52f9 parent=bc415fe6 {"thinkingLevel":"off"}
message                id=720bbfe9 parent=732e52f9 system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write
message                id=353e9125 parent=720bbfe9 user       [{"type":"text","text":"清理构建目录"}]
custom                 id=42576ae6 parent=353e9125 {"customType":"turn-usage","data":{"totalTokens":1317}}
message                id=9246f4de parent=42576ae6 assistant  [{"type":"toolCall","id":"call_1","name":"bash","arguments":{"command":"rm -rf build"}}] stop=toolUse
message                id=ec6d192f parent=9246f4de toolResult [{"type":"text","text":"(no output)"}]
custom                 id=a360ce49 parent=ec6d192f {"customType":"turn-usage","data":{"totalTokens":1343}}
message                id=b8c80436 parent=a360ce49 assistant  [{"type":"text","text":"第一个命令的结果（isError=false：(no output)）。再看看目录。"},{"type":"toolCall","i stop=toolUse
message                id=ebe0e82a parent=b8c80436 toolResult [{"type":"text","text":"README.md\n"}]
custom                 id=bc62ca02 parent=ebe0e82a {"customType":"turn-usage","data":{"totalTokens":1361}}
message                id=cb0bbe74 parent=bc62ca02 assistant  [{"type":"text","text":"目录内容（isError=false：README.md）。"}] stop=stop
```

**结论。** 预测成立。每个 `custom` 条目都是它所记录的 assistant 条目的父条目。读会话文件的程序如果按"usage 记录跟在消息后面"来配对，会配错。`turn_end` 的 `messageEntryId` 是 AgentSession 在 assistant 条目写入之后才查出来的（`_findPersistedMessageEntryId`，`packages/coding-agent/src/core/agent-session.ts:868`），所以记账放在 `turn_end`。

---

## 4. 自测

1. 把 `summary-guard.ts` 的 `tool_call` handler 改成"检测到 `rm -rf` 就 `throw new Error("no")`"。工具会执行吗？模型收到什么？
2. 另一个扩展也在 `turn_end` 追加草稿，它排在 `summary-guard.ts` 前面加载，返回 `{ entries: [draftA] }`。如果 `summary-guard.ts` 返回 `{ entries: [draftB] }` 而不是 `[...event.entries, draftB]`，会话文件里会有哪些条目？
3. 用户用 `pi -c` 继续这个会话。扩展想在启动时知道之前累计用了多少 token，应该怎么拿？这些记录会占模型的上下文吗？

<details>
<summary>答案</summary>

1. 不会执行。`emitToolCall` 不捕获异常（`packages/coding-agent/src/core/extensions/runner.ts:1255`），`_beforeToolCall` 把 `Error` 继续抛出（`packages/coding-agent/src/core/agent-session.ts:691-695`），`prepareToolCall` 的 `catch` 返回 `{ kind: "immediate", result: createErrorToolResult(error.message), isError: true }`（`packages/agent/src/agent-loop.ts:780-785`）。模型收到一条 `isError: true`、文本为 `no` 的工具结果。`tool_call` 失败时关闭（M7 §6.4）。
2. 只有 `draftB`。`emitBoundary` 在每个 handler 之后执行 `if (handlerResult?.entries !== undefined) entries = handlerResult.entries;`（`packages/coding-agent/src/core/extensions/runner.ts:1056`），后一个 handler 的返回值替换整个列表，`draftA` 丢失。
3. 在 `session_start` 里用 `ctx.sessionManager.getEntries()` 过滤 `type === "custom" && customType === "turn-usage"` 的条目，把 `data.usage.totalTokens` 加起来。`plan-mode` 示例就是这样恢复状态的（`packages/coding-agent/examples/extensions/plan-mode/index.ts:349-353`）。这些条目不占上下文：投影时 `sessionEntryToContextMessages` 对 `custom` 条目返回 `[]`（§3.2）。

</details>

---

## 5. 读完之后

到这里，主线场景的每一步都能在某个模块里找到对应的源码。继续往下读，可以从这几个方向入手：

- 改用真实 provider 运行同样的练习，看 `usage` 里的 `input` 和 `cacheRead` 怎样变化（M1 §4.10 讲 Anthropic 的 usage 从哪里来）。
- 统计每一轮被拦下的次数，放进同一个 `turn-usage` 条目。注意被拦下的调用不经过 `afterToolCall`，扩展的 `tool_result` 事件看不到它们（§3.6）。可以在 `turn_end` 里数 `event.toolResults` 中 `isError` 的结果，或者在 `tool_call` 里计数。
- 读 `examples/extensions/` 里的其他示例，例如 `protected-paths.ts` 和 `custom-compaction.ts`，判断它们分别接在 §1.3 调用栈的哪一行。
- 用 durable 重写拦截部分：工具任务的 `beforeTool` hook 对应这里的 `tool_call`（D6 §3、§7.2）。
