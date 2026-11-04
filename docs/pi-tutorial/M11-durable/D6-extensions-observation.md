# M11 durable · D6 扩展与观察

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。durable 自带的示例在 `$PI/packages/durable` 目录下运行；本文新增的实验脚本在 `docs/pi-tutorial/lab` 目录下运行，并且已经设置了 `export PI=/path/to/pi`。
>
> 本篇是 M11 的最后一篇，假定你读过 [D4](D4-tasks-scheduler.md)（任务和调度器）和 [D5](D5-builtin-tasks.md)（生成、工具、inbox）。文档的 delta、base 和 `checkpointWhen` 在 [D2](D2-chord.md) 和 [D3](D3-storage-transactions.md) 里讲过。

---

## 1. 这个模块是什么

### 1.1 职责

D5 的内置任务把进度写进两个地方：transcript 条目，和 `pi.live` 等会话文档。本篇从两个方向读它们的外部接口：

- **观察**：界面怎么看到一个正在进行的 run。durable 没有 M2 那样的事件流（M2 §3.5 的 `AgentEvent`）。界面订阅**会话视图**（`ConversationView`）：一个条目窗口加上 5 个内置文档，每次提交带着 chord 的 delta 操作推送过来。流式 partial、工具输出、run 状态都是文档里的字段。
- **扩展**：扩展（`Extension`）提供工具、系统提示词段落、任务定义，以及挂在内置任务上的 **hook**。hook 在任务的阶段里被调用，相当于 M2 的 `AgentLoopConfig` 回调和 M7 的扩展事件。

最后读 code mode 的一小部分：一个 `replay: "safe"` 的工具怎么用确定性重放，在进程重启后继续一段模型写的脚本。这是 D5 嵌套调用 key 机制最完整的用例。

### 1.2 在调用链中的位置

```
 任务提交（D5）
   ├─ 条目追加、pi.live / pi.inbox / pi.usage … 文档变更
   └─ Session 发布提交 → followCommits 监听器
        ├─ ConversationViews.advance()   会话视图     ← 本篇 §4.2–4.3
        ├─ TaskGraphView.advance()       任务图（D4 §4.12）
        └─ Submissions / TaskScheduler   （D4、D5）
 阶段函数（D5）
   └─ runtime.hooks.each(name, invoke)    逐个调用扩展的 hook   ← 本篇 §4.5
        agentHooks(agent, taskName)       按会话选中的扩展过滤
```

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/durable/src/harness/live.ts` | 451 | `pi.live` 文档的类型、`checkpointWhen`、`PROGRESS_COMMIT`、`settleSchedulerOutcome` | 精读 |
| `packages/durable/src/harness/view.ts` | 399 | `ConversationViews`：每个会话一个挂载点，多个观察者各有窗口 | 精读 |
| `packages/durable/src/harness/types.ts` | 1070 | `Extension`、`ToolRegistration`、`GenerationHooks`、`ToolHooks`、`CompactionHooks` | 读本篇用到的类型 |
| `packages/durable/src/harness/agent.ts` | 361 | 会话 agent 的解析、`agentHooks` | 只读 `agentHooks` |
| `packages/durable/src/harness/define.ts` | 62 | `defineExtension`、`hook` 等（D4 §3.4） | 已读 |
| `packages/durable/src/codemode/codemode.ts` | 1042 | durable 的 code mode 工具 | 只读开头的设计注释、启动数据和 journal |
| `packages/durable/docs/spec.md` §7、§9 | 第 3067–4488、5379–5886 行 | 扩展、hook、工具、code mode；观察 | 对照阅读 |

不在本篇范围：系统提示词段落的渲染细节（`prompt.ts`）、扩展重载、`ConversationTranscript` 客户端合并、code mode 的 QuickJS 沙箱本身（`packages/codemode`）。

### 1.4 读完能回答的问题

1. durable 没有事件流，界面怎么显示流式输出？
2. 界面在 run 进行到一半时才连上来，它会错过什么？
3. 一个扩展怎么拦截某个工具调用？拦截发生在 D5 工具任务的哪一步？
4. `pi.live` 在流式输出时每 100ms 改一次，存储会不会无限增长？
5. code mode 的脚本跑到一半进程重启了，脚本里已经完成的工具调用会再执行一次吗？

---

## 2. 先跑一次

### 2.1 从会话视图看一次 run

[`lab/m11b-observe.ts`](../lab/m11b-observe.ts) 的 `live` 模式和 D5 §2.1 是同一个 run（第 1 次回复是文字加一个 `slow` 工具调用，`slow` 输出两段文本，每段之间等 300ms；第 2 次回复是总结）。这次用 `root.watch()` 订阅会话视图，每次回调打印新条目和 `pi.live` 的摘要：

```bash
cd docs/pi-tutorial/lab
node --conditions=source --experimental-strip-types m11b-observe.ts live
```

```
   8  [条目] 7 pi.user
  10  pi.live: run={taskId:9,inputs:[8],turns:0}
  13  [条目] 10 pi.system
  13  pi.live: run={taskId:9,inputs:[8],turns:0}  generation={attempt:1,partial:""}
 117  pi.live: run={taskId:9,inputs:[8],turns:0}  generation={attempt:1,partial:"我来读一下 README。"}
 246  [条目] 11 pi.assistant
 246  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 pending output=""
 249  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 running output=""
 250  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 running output="reading README.md\n"
 551  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 running output="reading README.md\nhalf done\n"
 854  [条目] 13 pi.tool-result
 854  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 done output="" entry=13
 855  pi.live: run={taskId:14,inputs:[8],turns:1}
 855  pi.live: run={taskId:14,inputs:[8],turns:1}  generation={attempt:1,partial:""}
 933  [条目] 15 pi.assistant
 933  pi.live = {}
```

第一列是毫秒，每次运行略有不同。把它和 M2 §2.1 的事件流对照：

| M2 的事件 | 这里看到的变化 |
|---|---|
| `agent_start` | `pi.live.run` 出现 |
| `message_start/end (user)` | 条目 7 `pi.user` |
| `message_update [text_delta]` | `pi.live.generation.message`（partial）变长 |
| `message_end (assistant)` | 条目 11 `pi.assistant`，`generation` 消失，`tools` 槽位出现 |
| `tool_execution_start` | 槽位 `pending` → `running` |
| `tool_execution_update` | 槽位的 `output` 变长 |
| `tool_execution_end` + `message_end (toolResult)` | 条目 13，槽位 `done` 且 `entry=13` |
| 第 2 个 `turn_start` | `run.taskId` 从 9 变成 14 |
| `agent_end` | `pi.live` 清空 |

第 2 次回复的 partial 一直是空的，因为假模型吐完这几个字只用了不到 100ms，节流器还没来得及提交 partial，回复就结束了。

---

## 3. 数据结构

### 3.1 SlotProgress、ToolSlot、NestedToolSlot：工具槽位

D5 §3.3 读了 `LiveState` 里的 `run`。这里读工具的展示部分：

源码：[`packages/durable/src/harness/live.ts:19-60`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/live.ts#L19-L60)

```ts
/** What a running tool call publishes, shared by model-issued and nested calls. */
type SlotProgress = {
	callId: string;
	name: string;
	status: "pending" | "running" | "done";
	/** Retained running output and what the bounds dropped. */
	output?: string;
	droppedBytes?: number;
	droppedLines?: number;
	/** Last `details()` value. */
	details?: JsonValue;
	/** Diagnostics recorded through `api.diagnostic()`. */
	diagnostics?: ToolDiagnostic[];
};

/** Presentation of one tool call of the current round. */
export type ToolSlot = SlotProgress & {
	/** Absent for a call not started yet (sequential round). */
	taskId?: TaskId;
	/** A call to a tool its request did not offer: its task settles `tool_unavailable` without running anything. */
	unoffered?: true;
	/** Result entry once done; absent when the tool task faulted or was orphaned. */
	entry?: EntryId;
};

/**
 * Presentation of one nested call: a call a running tool made through `executeTool()`. `callId` is
 * `<parentCallId>/<key>`, the call's ID, unique because keys contain no `/`. The Harness relates
 * slots by task ID: `taskId` is the call's tool task, `parentTaskId` the calling one, model-issued or nested.
 */
export type NestedToolSlot = SlotProgress & {
	parentCallId: string;
	parentTaskId: TaskId;
	taskId: TaskId;
	/**
	 * What the call was made with, replaced by the arguments it runs with, after repair, `beforeTool`, and coercion,
	 * once it starts executing. A model-issued call's arguments are in its assistant entry instead.
	 */
	arguments: JsonObject;
	/** Once done: how the call ended, for a status line. The result itself is in the caller's `NestedCallDoc`. */
	summary?: NestedToolSummary;
};
```

| 字段 | 说明 |
|---|---|
| `status` | `pending`（任务已创建）→ `running`（`call` 阶段校验通过）→ `done` |
| `output`、`droppedBytes`、`droppedLines` | 工具通过 `api.output()` 报告的输出，按工具的 `outputLimits` 截断后保留的部分 |
| `details`、`diagnostics` | `api.details()`、`api.diagnostic()` 的最新值 |
| `entry` | 结果条目，`done` 时设置。任务被调度器 faulted 或 orphaned 时没有 |
| `parentTaskId`、`arguments`、`summary` | 只有嵌套槽位有。嵌套调用没有 transcript 条目，结束时只留一个摘要 |

**真实实例。** §2.1 里槽位依次是 `tool=slow#12 pending output=""`、`running output="reading README.md\n"`、`done output="" entry=13`。结束时 `output` 被清空（`finishSlot`），结果在条目 13 里。

### 3.2 ConversationView：会话视图

源码：[`packages/durable/src/harness/view.ts:42-54`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/view.ts#L42-L54)

```ts
/** Structural view of one conversation: a window over its transcript and its built-in documents (spec §9.3). */
export type ConversationView = {
	readonly conversation: ConversationRecord;
	/**
	 * A window over the transcript, its fork-aware history (`Conversation.entries()`), in entry ID order. Head markers
	 * such as compaction summaries sit where they were written; their `head` tells where the entries they summarize end.
	 */
	readonly entries: readonly EntryRecord[];
	/** ID of the entry just before the window; absent when the window starts the transcript. */
	readonly before?: EntryId;
	/** Built-in conversation documents keyed by kind; absent documents are absent. */
	readonly docs: Readonly<Record<string, JsonObject>>;
};
```

| 字段 | 说明 |
|---|---|
| `conversation` | 会话记录 |
| `entries` | 条目窗口，默认最新 50 条，超过 100 条时一次删掉最旧的，剩 50 条 |
| `before` | 窗口之前那个条目的 ID。客户端用它判断是否还有更早的条目 |
| `docs` | 5 个内置文档：`pi.agent`、`pi.live`、`pi.inbox`、`pi.provider`、`pi.usage`（`MOUNTED`，`packages/durable/src/harness/view.ts:84`） |

**真实实例。** §2.1 的脚本只打印了 `entries` 的新增部分和 `docs["pi.live"]`。§7.3 的 `20-late-join.ts` 打印了 `entries` 的 kind 列表。

### 3.3 Extension 和 hook

源码：[`packages/durable/src/harness/types.ts:412-421`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/types.ts#L412-L421)

```ts
export interface Extension<Tool extends ToolRegistration = ToolRegistration> {
	readonly name: string;
	readonly tools?: readonly Tool[];
	readonly sections?: readonly PromptSection<Tool>[];
	readonly hooks?: readonly HookRegistration[];
	/** Apply where this extension is selected, in order. */
	readonly wraps?: readonly Wrap<Tool>[];
	/** Resolved by name for every task, whichever conversations select this extension. */
	readonly tasks?: readonly AnyTask[];
}
```

扩展按名字注册在 registry 里。每个会话的 `pi.agent` 文档选择用哪些扩展（`extensions` 字段），工具、段落和 hook 只在选中它的会话里生效；`tasks` 不同，它们按 kind 全局解析，不管哪个会话选了这个扩展（D4 §4.3）。

hook 由 `hook(task, handlers)` 创建（D4 §3.4），`task` 是内置任务对象，`handlers` 是这个任务支持的 hook 的子集。生成任务的 hook：

源码：[`packages/durable/src/harness/types.ts:967-999`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/types.ts#L967-L999)

```ts
/** Hooks of the built-in generation task. */
export interface GenerationHooks {
	/** Before every request attempt, including recovery; the result is used for that request only. */
	beforeRequest(
		request: { readonly messages: readonly Message[] },
		api: HookApi,
		context: Context,
	): HookResult<{ readonly messages: readonly Message[] }>;
	/**
	 * After pi-ai builds the provider request body, before it is sent; the result replaces the body for that request only.
	 * Runs again whenever pi-ai rebuilds the body, such as a retry within one attempt. The body is in the provider API's
	 * own format (`request.model.api`), so a hook checks the API before changing it.
	 */
	beforeProviderRequest(
		request: { readonly model: Model<Api>; readonly payload: unknown },
		api: HookApi,
		context: Context,
	): HookResult<{ readonly payload: unknown }>;
	/** Every terminal provider message, before classification. */
	afterResponse(message: AssistantMessage, api: HookApi, context: Context): void | Promise<void>;
	/** A final answer; the first `continue` appends a user message and continues the run. */
	onYield(answer: AssistantMessage, api: HookApi, context: Context): HookResult<{ readonly continue: UserInput }>;
	/** After every tool of the round is terminal; `results` are the round's result entries in call order. */
	afterTools(assistant: EntryId, results: readonly EntryId[], api: HookApi, context: Context): void | Promise<void>;
	/**
	 * After a run answered, its answer committed and its inputs settled `done`: a final answer, or a `terminate` round;
	 * not when an `onYield` continuation is taken, and never after a failure or an abort. The requested tasks of every
	 * hook are created together as children of the generation, which keeps the run, and the conversation busy, until
	 * they are terminal; then queued items are placed and the run ends (spec §8.3). A crash before they are created asks
	 * again; once created, never again for this run.
	 */
	afterRun(run: RunSummary, api: HookApi, context: Context): HookResult<{ readonly tasks: readonly RunEndTask[] }>;
}
```

工具任务的 hook（`ToolHooks`，`packages/durable/src/harness/types.ts:1025`）有两个：`beforeTool`（意图之前，可以 block 或改参数）和 `afterTool`（结果提交之前，可以替换结果）。压缩任务有 `beforeCompact`（D5 §4.12）。

| hook | 调用位置（D5） | 相当于 M2 / M7 的 |
|---|---|---|
| `beforeRequest` | `request` 阶段，构造消息后 | `transformContext`，扩展的 `context` 事件 |
| `beforeProviderRequest` | 发请求前，替换 provider 请求体 | `onPayload` |
| `afterResponse` | `classify` 开头 | `message_end (assistant)` |
| `onYield` | `answer()` | `finishTurn` 返回 `continue` |
| `afterTools` | `finishToolRound()` | `turn_end` |
| `afterRun` | `finish` 阶段 | `agent_end` 之后 |
| `beforeTool` | 工具任务 `call` 阶段，意图提交前 | `beforeToolCall`，扩展的 `tool_call` 事件 |
| `afterTool` | `settle()` 前 | `afterToolCall` |

**真实实例。** §7.2 的实验注册了 `beforeTool`、`afterTool` 和 `beforeRequest`，打印出它们被调用的时刻和参数。

### 3.4 code mode 的 journal

源码：[`packages/durable/src/codemode/codemode.ts:134-158`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/codemode/codemode.ts#L134-L158)

```ts
type JournalEntry = {
	/** The VM's call ID. Its result is the entry's position + 1 (`seq`). */
	id: number;
	/** The VM clock while it processes this result. */
	at: number;
	/** Calls the VM had issued after processing this result; absent until acknowledged. */
	issuedAfter?: number;
	/** A global's call and outcome; a tool's result is its nested call's. */
	global?: { name: string; args?: JsonValue; outcome: JsonObject };
};

type JournalState = {
	entries: JournalEntry[];
	/** Calls the VM had issued after the script's first run, before any result; absent until acknowledged. */
	firstIssued?: number;
};

export const CodemodeJournalDoc = defineDoc<JournalState>({
	kind: "pi.codemode.journal",
	version: 1,
	scope: "task",
	initial: () => ({ entries: [] }),
	// The document retires with its task.
	checkpointWhen: () => false,
});
```

journal 是 code mode 工具调用任务作用域下的文档，每交付一个结果给脚本，就先追加一条：VM 的调用 ID、VM 时钟、处理完这个结果后脚本发出了多少个调用。任务结束时文档随之退役。§4.6 读它怎么用。

### 3.5 类型关系图

```
 Registry ── Extension{ name, tools, sections, hooks[], wraps, tasks }
                                     │ hook(ToolTask | GenerationTask | CompactionTask, handlers)
 pi.agent.extensions 选中 ──▶ Agent（每个阶段解析一次）
                                     │ agentHooks(agent, taskName)
                                     ▼
                        runtime.hooks.each(name, invoke)    （D5 的阶段里）

 任务提交 ──▶ 条目 + 文档 delta ──▶ ConversationViews（每个会话一个 Mount）
                                     Mount{ conversation, docs{pi.agent, pi.live, pi.inbox,
                                                                pi.provider, pi.usage},
                                            observers: Windowed[] }
                                     │ advance(publication)
                                     ▼
                        每个观察者：ConversationView{ entries 窗口, before, docs } + ops
 pi.live = LiveState{ run, generation{message…}, tools: ToolSlot[], nestedTools, compactions, … }
```

---

## 4. 从入口读起

```
pi.live 文档：checkpointWhen 和 PROGRESS_COMMIT        §4.1
Conversation.watch() → ConversationViews.watch() → attach()   §4.2
followCommits → advance()                              §4.3
settleSchedulerOutcome()：调度器写结果时的 live 清理     §4.4
runtime.hooks.each() / agentHooks()                    §4.5
code mode：确定性重放                                    §4.6
```

### 4.1 pi.live：给界面看、又要有界的文档

源码：[`packages/durable/src/harness/live.ts:194-223`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/live.ts#L194-L223)

```ts
export const LiveDoc = defineDoc<LiveState>({
	kind: "pi.live",
	version: 3,
	scope: "conversation",
	history: "latest",
	fork: "initial",
	initial: () => ({}),
	// Version 1 named a compaction's trigger `reason`, in `compactions` and `compactionFailure`. Versions 1 and 2 (as
	// builds before `run.turns` stored it) may lack `run.turns`: a run in flight across the upgrade counts its turns
	// from 0. Version 2 already names the trigger `trigger`, so renaming leaves it alone.
	migrate: (value) => {
		const live = value as { run?: JsonObject; compactions?: JsonObject[]; compactionFailure?: JsonObject };
		const { run, compactions, compactionFailure } = live;
		return {
			...value,
			...(run === undefined ? {} : { run: { ...run, turns: typeof run.turns === "number" ? run.turns : 0 } }),
			...(compactions === undefined ? {} : { compactions: compactions.map(renameReason) }),
			...(compactionFailure === undefined ? {} : { compactionFailure: renameReason(compactionFailure) }),
		} as LiveState;
	},
	// REMINDER: a complete base whenever nothing runs (spec §8.2): no generation and no running tool slot. That holds
	// while idle, in the commit handing a generation over to its tool round, and between tools, so the delta chain
	// spans at most one generation or the overlapping execution of one round's tools. A slot holds output only while
	// running, so every base is small; a nested call's result lives in its caller's task documents, never here. Do not
	// add a delta-count bound; the tool output benchmark checks this rule.
	checkpointWhen: (value) =>
		value.generation === undefined &&
		!(value.tools ?? []).some((slot) => slot.status === "running") &&
		!(value.nestedTools ?? []).some((slot) => slot.status === "running"),
});
```

源码：[`packages/durable/src/harness/live.ts:230-235`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/live.ts#L230-L235)

```ts
/**
 * How the progress throttles commit a partial answer and running tool output into `pi.live`: deferred, so storage gets
 * them together, at the next commit that reads or writes `pi.live` or after `settings.progress.persistWithinMs`. Every
 * commit that consumes or clears streamed fields reads `pi.live`, so it stores them with its own changes (spec §8.2).
 */
export const PROGRESS_COMMIT: CommitOptions = { persistence: { kind: "deferred" } };
```

**讲解**

`pi.live` 每 100ms 被流式 partial 或工具输出改一次。D2 讲过，文档以"一个完整的 base 加后续 delta"存储，`checkpointWhen` 决定什么时候写一个新 base。这里的规则是：没有生成在进行、没有工具在运行时写 base。也就是说，delta 链最长覆盖一次生成或一个工具轮，跨 run 不会增长。而且这时 `pi.live` 里没有 partial 和工具输出，base 本身也很小。

`PROGRESS_COMMIT` 是延迟持久化（D3 的 deferred commit）：进度提交先进内存、立刻发布给观察者，存储在下一次读写 `pi.live` 的提交时一起写入，最迟在 `persistWithinMs`（默认 1000ms）后写入。所以界面看到的流式输出是实时的，存储的写入次数少得多；代价是崩溃时最多丢掉大约 1 秒的 partial 或工具输出（README 的 Watching a Conversation 一节）。D5 §7.1 等 1.5 秒再杀进程，就是为了让 `step 1 done` 落盘。

每个"消费或清除"流式字段的提交（例如 `classify` 删除 `generation`、`settle` 清空槽位）都会读 `pi.live`，所以它会把之前延迟的进度一起写下，再在上面做修改。

**往哪走**：§4.2。

### 4.2 ConversationViews.attach()：挂载和窗口

**调用位置**：`conversation.watch(context, window?)`（`packages/durable/src/harness/harness.ts:184`）→ `ConversationViews.watch()` → `attach()`。`harness.observeConversation()` 也走 `attach()`，区别是同步观察者，不经过 chord 的 `Watch` 排队。

源码：[`packages/durable/src/harness/view.ts:226-255`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/view.ts#L226-L255)

```ts
	/**
	 * Register an observer created from its window's first revision, atomically on the Session line: it sees every later
	 * publication and nothing earlier. `release` drops it, and the mount with its last observer.
	 */
	async attach<O extends ViewObserver>(
		id: ConversationId,
		window: ConversationWindow | undefined,
		create: (value: ConversationView, release: () => void) => O,
		context: Context,
	): Promise<{ observer: O; detach: () => void }> {
		const bounds = resolveWindow(window);
		return this.#session.readOnLine(async (storage) => {
			const mount = this.#mounts.get(id) ?? (await this.#build(storage, id, context));
			const { entries, before } = await loadWindow(storage, id, bounds, context);
			const value = view(mount.conversation, entries, before, mount.docs);
			let windowed: Windowed | undefined;
			const detach = (): void => {
				if (windowed !== undefined) mount.observers.delete(windowed);
				if (mount.observers.size === 0 && this.#mounts.get(id) === mount) this.#mounts.delete(id);
			};
			const observer = create(value, detach);
			// Close or cancellation may begin while the mount hydrates; register nothing then.
			if (this.#closed) throw closedError(this.#session);
			context.abortSignal?.throwIfAborted();
			windowed = { min: bounds.min, max: bounds.max, value, observer };
			this.#mounts.set(id, mount);
			mount.observers.add(windowed);
			return { observer, detach };
		});
	}
```

**讲解**

`attach` 在 Session line 上执行（`readOnLine`），所以"读当前状态"和"注册观察者"之间不会有提交插进来：观察者拿到的第一个值是挂载那一刻的状态，之后收到的是那以后的每一次提交，不多也不少。

- 一个会话只有一个 Mount，5 个内置文档在第一次挂载时加载（`#build`，`packages/durable/src/harness/view.ts:258`），所有观察者共享。
- 每个观察者有自己的条目窗口（`loadWindow`），用 `min`/`max`/`from` 控制。`from` 用于断线重连：从客户端已有的最新条目之后开始。
- 最后一个观察者离开时 Mount 被删除。

这就是"晚加入"（§7.3）能工作的原因：客户端什么时候连上来都一样，先拿当前视图，再拿增量，不需要重放历史事件。

**往哪走**：§4.3。

### 4.3 advance()：把一次提交变成视图的 delta

源码：[`packages/durable/src/harness/view.ts:324-378`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/view.ts#L324-L378)

```ts
function advance(
	id: ConversationId,
	mount: Mount,
	publication: ObservedCommit,
	context: Context,
	report: (error: unknown) => void,
): void {
	const docOps: Op[] = [];
	// Entry writes are published in ID order.
	const appended: EntryRecord[] = [];
	for (const change of publication.changes) {
		if (change.type === "entry" && change.value.conversationId === id) {
			appended.push(change.value);
			continue;
		}
		if (change.type !== "document" || change.conversationId !== id) continue;
		const kind = change.record.kind;
		if (!MOUNTED_KINDS.has(kind) || change.record.key !== undefined) continue;
		const path: NonEmptyPath = ["docs", kind];
		const mounted = mount.incarnations.get(kind);
		if (change.kind === "retire") {
			if (mounted?.id !== change.record.id) continue;
			mount.incarnations.delete(kind);
			docOps.push(["d", path]);
		} else if (mounted?.id === change.record.id && mounted.version === change.version) {
			for (const op of change.ops) docOps.push(prefixed(op, path));
		} else {
			mount.incarnations.set(kind, { id: change.record.id, version: change.version });
			docOps.push(["s", path, change.value]);
		}
	}
	if (docOps.length === 0 && appended.length === 0) return;
	if (docOps.length > 0) mount.docs = applyImmutable({ docs: mount.docs }, docOps).docs;
	const frameContext = withoutAbortSignal(context);
	const inserted = appended as unknown as JsonValue[];
	for (const windowed of [...mount.observers]) {
		const previous = windowed.value;
		let entries = previous.entries;
		let before = previous.before;
		let ops: Op[] = docOps;
		if (appended.length > 0) {
			ops = [...docOps, ["p", ["entries"], entries.length, 0, inserted]];
			entries = entries.concat(appended);
			if (entries.length > windowed.max) {
				const drop = entries.length - windowed.min;
				before = entries[drop - 1]!.id;
				entries = entries.slice(drop);
				ops.push(["p", ["entries"], 0, drop, []], ["s", ["before"], before]);
			}
		}
		windowed.value = view(mount.conversation, entries, before, mount.docs);
		const call: WatchCall<ConversationView> = { kind: "value", value: windowed.value, ops, retired: false };
		contained(() => windowed.observer.advance(call, frameContext), report);
	}
}
```

**讲解**

每次提交发布时，对每个 Mount：

1. 属于这个会话的新条目收集到 `appended`。
2. 属于这个会话的内置文档变更：同一个文档实例、同一个版本时，把文档自己的 delta 操作加上 `["docs", kind]` 前缀（`prefixed`）；换了实例或版本时整体替换；退役时删除。
3. 文档操作只应用一次，所有观察者共享结果。
4. 每个观察者：新条目用一个 `p`（splice）操作追加到窗口末尾；超过 `max` 时再用一个 splice 从头删到 `min`，并设置 `before`。然后把新值和这次的操作列表交给观察者。

所以客户端可以不重新拉取整个视图，只把 `ops` 应用到自己的副本上（例如通过网络转发给浏览器）。流式 partial 的 `text_delta` 在这里表现为对 `docs["pi.live"].generation.message.content[…].text` 的字符串追加操作：chord 对字符串字段做的是追加 diff（D2），所以每次只传新增的文字。

这个函数对每次已发布的提交都运行，包括延迟持久化、还没写进存储的提交。D4 §4.12 的任务图只跟随已存储的提交，因为任务记录的变更从不延迟。

**往哪走**：§4.4。

### 4.4 settleSchedulerOutcome()：没有任务代码时的清理

源码：[`packages/durable/src/harness/live.ts:410-451`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/live.ts#L410-L451)

```ts
export async function settleSchedulerOutcome(
	tx: Transaction,
	record: TaskRecord<JsonValue, JsonValue, JsonValue>,
	outcome: SchedulerOutcome,
): Promise<void> {
	if (record.kind === TOOL_TASK_KIND) {
		const live = await tx.doc(LiveDoc, record.conversationId);
		const slot = toolSlot(live, record.id);
		if (slot !== undefined) finishSlot(slot);
		removeNestedSlots(live, record.id);
		return;
	}
	// Only an abort handler is faulted for the limit here (the built-in phases end themselves): the task was aborted,
	// and keeps that meaning. Its run ends `aborted`, with the limit as detail, and an aborted compaction is no failure.
	const detail = outcome.status === "faulted" ? outcome.error.detail : undefined;
	const cutOff =
		typeof detail === "object" && detail !== null && "reason" in detail && detail.reason === "restart_limit";
	if (record.kind === COMPACTION_TASK_KIND) {
		const live = await tx.doc(LiveDoc, record.conversationId);
		if (cutOff) removeCompactionStatus(live, record.id);
		else failCompactionStatus(live, record.id, outcome.status === "faulted" ? outcome.error.message : outcome.reason);
		return;
	}
	if (!RUN_TASK_KINDS.has(record.kind)) return;
	const live = await tx.doc(LiveDoc, record.conversationId);
	if (live.run?.taskId !== record.id) return;
	// Cut off, the abort handler may have died converting the partial: the run ends without it.
	if (!cutOff) await convertPartial(tx, live, record.conversationId);
	endRun(
		tx,
		live,
		record.id,
		outcome.status === "faulted"
			? { status: "unanswered", reason: cutOff ? "aborted" : "faulted", detail: outcome.error.message }
			: {
					status: "unanswered",
					reason: "orphaned",
					detail: outcome.reason,
					settlement: { status: "unanswered", reason: outcome.reason },
				},
	);
}
```

**讲解**

D4 §6.4 说调度器不认识任务种类。它自己写 `faulted` 或 `orphaned` 结果时（任务代码没机会清理），调用 Harness 传进来的这个函数：

- 工具任务：槽位标为 `done`，但没有 `entry`。上下文推导会为这个调用合成一个缺失结果（spec §2.1），模型仍然能看到一个结果。
- 压缩任务：删掉状态，记录 `compactionFailure`。
- 生成任务：已提交的 partial 转成 aborted 条目，`endRun` 把 run 以 `faulted` 或 `orphaned` 结束，输入 settle 成 `unanswered`。

这样无论任务以什么方式结束，`pi.live` 都不会留下一个永远 `running` 的槽位或永远存在的 `run`，界面也不会卡在"忙"的状态。

**往哪走**：§4.5。

### 4.5 runtime.hooks.each() 和 agentHooks()：hook 的分发

`TaskRuntime.hooks` 在 `#runtime()` 里构造（D4 §4.7）：

源码：[`packages/durable/src/harness/scheduler.ts:2627-2644`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2627-L2644)

```ts
		const hooks: HookRunner<Record<string, unknown>> = {
			each: async (name, invoke) => {
				for (const { extension, handlers } of agentHooks(await agent(), phase.task().definition.name)) {
					const handler = (handlers as Record<string, unknown>)[name];
					if (typeof handler !== "function") continue;
					try {
						await invoke(handler.bind(handlers));
					} catch (error) {
						if (invocation.controller.signal.aborted) throw error;
						this.#report(
							error,
							{ kind: "extension_error", extension, task: reportTask(invocation) },
							invocation.context,
						);
					}
				}
			},
		};
```

按任务名过滤会话 agent 选中的扩展：

源码：[`packages/durable/src/harness/agent.ts:190-201`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/agent.ts#L190-L201)

```ts
export function agentHooks(
	agent: Agent,
	taskName: string,
): { readonly extension: string; readonly handlers: object }[] {
	const handlers: { readonly extension: string; readonly handlers: object }[] = [];
	for (const extension of agent.extensions) {
		for (const hook of extension.hooks ?? []) {
			if (hook.task === taskName) handlers.push({ extension: extension.name, handlers: hook.handlers });
		}
	}
	return handlers;
}
```

**讲解**

- 每个阶段解析一次 agent（`runtime.agent()`），所以一个阶段里调用的所有 hook 来自同一个注册表快照。扩展重新安装后，下一个阶段才用新的 hook（spec §7.5）。
- 按扩展顺序逐个调用。组合方式由调用方的 `invoke` 决定：D5 里 `beforeTool` 第一个 `block` 生效后，后面的 handler 直接返回；`beforeRequest` 把上一个的返回值作为下一个的输入；`afterTool` 每个都可以替换结果。
- hook 抛异常时报告为 `extension_error`，**继续调用下一个**，不影响任务。只有调用已经被中止时才把异常抛出去。这和 M2 的"hook 不许抛异常"约定（M2 §4.18）目标一样：扩展的 bug 不会弄坏循环。工具任务的 `beforeTool` 例外：它自己捕获异常并当作 block（D5 §4.8），因为放行一个检查失败的调用更危险。

hook 运行在任务的阶段里，所以它们也受恢复规则约束。`beforeTool` 在意图之前运行，崩溃后会在 `call` 阶段重跑；但一旦意图提交，恢复走 `execute` 阶段，不会再运行 `beforeTool`（D5 §4.9）。所以一个 `beforeTool` 不能假设"它允许的调用一定会执行"，也不能假设"它只被调用一次"。

§7.2 用实验验证分发和 block。

**往哪走**：§4.6。

### 4.6 code mode：确定性重放

`createCodemodeTool()` 定义了一个 `replay: "safe"` 的工具，参数是一段 JavaScript，在 QuickJS 沙箱里执行，脚本通过 `tools.<name>()` 调用其他工具：

源码：[`packages/durable/src/codemode/codemode.ts:238-250`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/codemode/codemode.ts#L238-L250)

```ts
export function createCodemodeTool(options: CodemodeToolOptions): ToolRegistration<typeof codemodeSchema> {
	return defineTool({
		name: CODEMODE_TOOL_NAME,
		description: codemodeDescription(options.models !== false),
		parameters: codemodeSchema,
		outputLimits: OUTPUT_LIMITS,
		// Capable models write the script as raw text instead of a JSON-escaped string; pi-ai applies it only for them.
		constrainedSampling: { type: "grammar", variants: { openai_lark: CODEMODE_SOURCE_GRAMMAR } },
		callers: ["model"],
		replay: "safe",
		execute: (args, api, context) => new CodemodeCall(options, api, context).execute(args.code),
	});
}
```

文件开头的注释说明了它怎么在重启后继续一个脚本：

源码：[`packages/durable/src/codemode/codemode.ts:1-35`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/codemode/codemode.ts#L1-L35)

```ts
/**
 * Durable code mode: a tool that runs model-written JavaScript in a QuickJS sandbox, whose script survives a restart.
 *
 * Problem: a script is in-memory VM state. A restart while
 *
 *     const results = await Promise.all(paths.map((path) => tools.read({ path })));
 *
 * waits for the last of 200 reads would lose the script and every read it made.
 *
 * Solution: deterministic replay (`CodemodeSandboxBase.start()` in pi-codemode). The VM only runs when the host hands
 * it a result; given the same start data, clock, seed, and results in the same order, it makes the same calls with
 * the same arguments. The tool is replay-safe: after a restart, `execute()` runs again, starts the script again from
 * the stored start data, and hands it the results it got before, in the order it got them, from a journal written
 * before each delivery. Nested tool calls reattach to their tasks by key (`c<VM call id>`), so they never run twice
 * unless their own replay policy reruns them; their results come from `NestedCallDoc`. Then the script continues live.
 *
 * Documents, scoped to the code mode call's task and gone when it settles:
 * - `pi.codemode.run`: the start data, written once before the VM starts, and the outcome, written once when the call
 *   decides it (with the store writes, in one transaction), so a rerun after that returns it without a VM.
 * - `pi.codemode.journal`: one entry per delivered result, in delivery order: the VM call ID, the clock value, the
 *   calls the VM had issued after processing it (once acknowledged), and a global's outcome, which nothing else stores.
 *
 * Each result's acknowledged call count is recorded with the next journal commit, or at once when the VM has processed
 * everything delivered: a script waiting on a long call is where a restart usually finds it. A sequential script
 * therefore costs one journal commit and one count commit per result, on top of its nested calls' own; that is the
 * price of checking the most common crash point.
 *
 * A call is launched only once the count of calls issued in the drain that made it is committed. So every key an earlier
 * attempt admitted is at most its last recorded count; a replay checks every recorded count, so when it releases its
 * calls it has made every call an earlier attempt could have admitted, and later calls are new.
 *
 * A rerun refuses to replay (no new nested calls; nested calls admitted earlier may still resume on their own and are
 * aborted by the call's cleanup) when this module's version or the VM's fingerprint changed, when the deadline passed,
 * or when the script diverges: it makes another call, or calls in another order, than the journal says. Each ends the
 * call with an error result.
```

**讲解**

脚本是 VM 内存里的状态，进程一死就没了。code mode 的做法是让脚本可以被**确定性地重放**：

1. **启动数据只写一次。** 第一次执行时，在一次提交里把随机种子、开始时间、工具声明、`store` 的值写进 `pi.codemode.run` 文档；重跑时读回同样的启动数据：

源码：[`packages/durable/src/codemode/codemode.ts:405-461`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/codemode/codemode.ts#L405-L461)

```ts
		const now = api.now();
		const seed = crypto.getRandomValues(new Uint32Array(1))[0]!;
		type Found =
			| { done: ToolExecutionResult }
			| { start: Start; journal: JournalEntry[]; firstIssued: number | undefined; rerun: boolean };
		const found = await api.commit(async (tx): Promise<Found> => {
			const run = await tx.doc(CodemodeRunDoc, api.taskId);
			if (run.result !== undefined) return { done: copyJson(run.result as JsonValue) as ToolExecutionResult };
			const rerun = run.start !== undefined;
			if (run.start === undefined) {
				const store = await tx.doc(CodemodeStoreDoc, api.conversationId);
				const timeout = sourceOptions.timeoutMs;
				run.start = {
					version: CODEMODE_MODULE_VERSION,
					startedAt: now,
					seed,
					...(timeout === undefined ? {} : { deadline: now + timeout }),
					tools,
					namespaces,
					globals,
					maxOutputTokens: sourceOptions.maxOutputTokens ?? DEFAULT_MAX_OUTPUT_TOKENS,
					store: copyJson(store.values as JsonValue) as Record<string, JsonValue>,
				};
			}
			const journal = await tx.doc(CodemodeJournalDoc, api.taskId);
			return {
				start: copyJson(run.start as unknown as JsonValue) as unknown as Start,
				journal: copyJson(journal.entries as unknown as JsonValue) as unknown as JournalEntry[],
				firstIssued: journal.firstIssued,
				rerun,
			};
		}, context);
		if ("done" in found) return found.done;
		this.start = found.start;
		this.replayed = found.journal;
		this.replayIndex = new Map(found.journal.map((entry, index) => [entry.id, index]));
		this.journalLength = found.journal.length;
		this.replaying = found.journal.length > 0;
		this.rerun = found.rerun;
		this.firstIssued = found.firstIssued;
		// A loop, not spread arguments: a journal can hold more entries than a call takes arguments.
		this.durableIssued = found.journal.reduce(
			(max, entry) => Math.max(max, entry.issuedAfter ?? 0),
			found.firstIssued ?? 0,
		);

		if (this.start.version !== CODEMODE_MODULE_VERSION) {
			return this.decide(
				errorResult(
					`The script cannot resume: it started under code mode version ${this.start.version}, this is ${CODEMODE_MODULE_VERSION}.`,
				),
			);
		}
		const remaining = this.start.deadline === undefined ? undefined : this.start.deadline - this.api.now();
		if (remaining !== undefined && remaining <= 0) {
			return this.decide(errorResult(`Script timed out: its deadline passed while it was interrupted.`));
		}
```

2. **VM 只在宿主交付结果时运行。** 时钟是虚拟的，随机数用存下的种子，时区固定为 UTC。给定同样的启动数据和同样顺序的结果，脚本就会发出同样的调用、带同样的参数。
3. **交付前先写 journal。** 每个结果交付给 VM 之前，先在 journal 里追加一条（§3.4）。
4. **嵌套调用按 key 挂接。** 脚本的每个工具调用都通过 D5 §4.10 的 `executeTool()` 发起，key 是 `c<VM 调用 ID>`：

源码：[`packages/durable/src/codemode/codemode.ts:838-838`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/codemode/codemode.ts#L838-L838)

```ts
			result = await this.api.executeTool(call.name, args, this.context, { key: `c${call.id}` });
```

重跑时 `execute()` 从头运行脚本，按 journal 的顺序交付已有的结果，VM 发出的调用按同样的 key 挂接回原来的嵌套任务（已完成的直接拿 `NestedCallDoc` 里的结果，在跑的继续等），不会执行第二次。journal 交付完后脚本继续"live"运行。

5. **拒绝重放的情况。** 模块版本变了、沙箱指纹变了、`timeout_ms` 的截止时间已过、或者脚本走了和 journal 不同的路径（发出了不同的调用或不同的顺序，`diverge`），调用以一个错误结果结束。这些检查保证重放不会悄悄产生和第一次不同的副作用。

这个设计依赖 durable 提供的三样东西：工具任务的 `replay: "safe"` 让它在 `execute` 阶段重跑（D5 §4.9）；任务作用域的文档（`pi.codemode.run`、`pi.codemode.journal`）随任务一起退役；嵌套调用的 key 让重跑找回已有的调用。

**往哪走**：§5。

---

## 5. 状态机

### 5.1 一个工具槽位

```
 startToolRound：push 槽位 { status: "pending", taskId } (generation.ts:1038、1043)
        │ 串行轮次的后续调用：pending 且没有 taskId，tools 阶段创建任务时补上 (generation.ts:349-350)
        ▼
 ┌─────────┐ call 阶段校验通过，PROGRESS_COMMIT (tool.ts:182-192)
 │ pending │──────────────────────────────▶ ┌─────────┐ api.output()/details() 节流提交
 └────┬────┘                                │ running │ output/details/diagnostics 增长
      │ reject（不存在/无效/block）           └────┬────┘ (tool.ts:635-649)
      │                                          │ settle()：finishSlot + entry (tool.ts:842-845)
      ▼                                          ▼
 ┌──────────────────────────────────────────────────────┐
 │ done（entry = 结果条目；调度器 faulted/orphaned 时没有 entry，live.ts:415-419）│
 └──────────────────────────────────────────────────────┘
        │ finishToolRound 交接或 endRun：delete live.tools (generation.ts:1134、live.ts:315)
        ▼
     （消失）
```

### 5.2 一个会话视图观察者

```
 attach()：readOnLine 里读 Mount 和窗口，注册观察者 (view.ts:237-254)
      │ 第一个值 = 挂载时刻的状态
      ▼
 ┌──────────┐ 每次发布：advance() 计算 ops (view.ts:324-378)
 │ attached │──▶ observer.advance({ kind: "value", value, ops })
 └────┬─────┘     窗口超过 max：splice 到 min，设置 before (view.ts:367-372)
      │ detach() / watch.stop()           │ Session 关闭
      ▼                                   ▼
   移出 Mount；最后一个离开时删 Mount     observer.end(ended | failed) (view.ts:158-170)
   (view.ts:242-245)
```

---

## 6. 设计要点

### 6.1 状态即事件

**做法。** durable 没有事件总线。界面需要的一切都在已提交的状态里：条目和 5 个内置文档。观察就是订阅这些状态的 delta。

**理由。** 事件流有两个问题：晚加入的客户端要么错过事件，要么需要重放；进程重启后事件流断开。状态加 delta 两个问题都没有：任何时候连上来都先拿当前值，增量只是优化。和 M2 相比，代价是没有"一个工具开始了"这样的离散事件，界面要从槽位状态的变化里推断。

**代码位置。** `packages/durable/src/harness/view.ts:230`、`packages/durable/src/harness/view.ts:324`。

### 6.2 进度提交延迟持久化，结果提交立即持久化

**做法。** partial 和工具输出用 `PROGRESS_COMMIT`，先进内存、立刻发布；意图、条目、任务状态用普通提交。

**理由。** 进度数据量大、更新频繁、丢了也能接受（最多约 1 秒）；结果和意图丢了会破坏恢复语义。把两类写入分开，界面实时性和存储写入量都得到满足。`checkpointWhen` 让延迟进度的 delta 链在每个工具轮或生成结束时截断。

**代码位置。** `packages/durable/src/harness/live.ts:219-222`、`packages/durable/src/harness/live.ts:235`。

### 6.3 hook 挂在任务上，按会话选择

**做法。** hook 注册时指定任务名，运行时只取当前会话选中的扩展的 hook；每个阶段解析一次。

**理由。** 同一个 Harness 里的不同会话可以有不同的策略（例如子代理会话去掉子代理扩展，见 `21-subagent-foreground.ts` 的 `configure`）。按阶段解析，扩展热重载不会让一个阶段的前后两半用不同版本的 hook。

**代码位置。** `packages/durable/src/harness/agent.ts:190`、`packages/durable/src/harness/scheduler.ts:2627`。

### 6.4 hook 的异常被报告而不是传播

**做法。** `hooks.each` 捕获 handler 的异常，报告为 `extension_error`，继续下一个。

**理由。** 一个扩展的 bug 不应该让任务 faulted（D4 规则 4），否则一个写错的日志 hook 会让每个 run 都失败。`beforeTool` 自己把异常当 block，是因为在那个位置"失败即拒绝"更安全。

**代码位置。** `packages/durable/src/harness/scheduler.ts:2632-2641`、`packages/durable/src/harness/tool.ts:161-171`。

### 6.5 可重放的工具 = 确定性 + journal + key

**做法。** code mode 不保存 VM 的内存，而是保存足够重新算出它的输入：启动数据和按顺序交付的结果。

**理由。** VM 的内存不可序列化，也可能很大；输入很小，而且大部分（嵌套调用的结果）已经作为任务和文档存在了。重放时校验调用数和调用顺序，保证"重算"和"第一次"走的是同一条路。

**代码位置。** `packages/durable/src/codemode/codemode.ts:410-436`、`packages/durable/src/codemode/codemode.ts:838`。

---

## 7. 实验

### 7.1 进度节流：去掉两段输出之间的等待

**问题。** `m11b-observe.ts` 的 `slow` 工具先输出 `reading README.md\n`，等 `GAP_MS` 毫秒（默认 300），再输出 `half done\n`。设置 `GAP_MS=0` 后，§2.1 的 `live` 输出里还会出现只有第一段的 `running` 槽位吗？第二段在第一段之后多久出现？

**预测。** 工具输出由 chord 的 `Progress` 节流器提交（`run()` 里的 `publishProgress`）。它的规则写在 `packages/chord/src/output.ts:322-326`：空闲之后的第一次变化**立即**提交；之后每次提交至少间隔 `minIntervalMs`（这里是 `outputIntervalMs`，默认 100ms），期间的变化合并进下一次。所以：

- 第一段仍然单独出现：它是空闲后的第一次变化，立即提交。
- 第二段在第一次提交约 100ms 后出现，而不是紧跟着出现。
- 工具在第二段之后还要等 300ms 才返回，所以结果条目比默认情况早约 300ms 出现。

**运行。**

```bash
GAP_MS=0 node --conditions=source --experimental-strip-types m11b-observe.ts live
```

```
   8  [条目] 7 pi.user
  10  pi.live: run={taskId:9,inputs:[8],turns:0}
  13  [条目] 10 pi.system
  14  pi.live: run={taskId:9,inputs:[8],turns:0}  generation={attempt:1,partial:""}
 117  pi.live: run={taskId:9,inputs:[8],turns:0}  generation={attempt:1,partial:"我来读一下 README。"}
 245  [条目] 11 pi.assistant
 245  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 pending output=""
 249  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 running output=""
 250  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 running output="reading README.md\n"
 351  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 running output="reading README.md\nhalf done\n"
 554  [条目] 13 pi.tool-result
 554  pi.live: run={taskId:9,inputs:[8],turns:1}  tool=slow#12 done output="" entry=13
 556  pi.live: run={taskId:14,inputs:[8],turns:1}
 557  pi.live: run={taskId:14,inputs:[8],turns:1}  generation={attempt:1,partial:""}
 634  [条目] 15 pi.assistant
 634  pi.live = {}
```

**结论。** 三个预测都成立。和 §2.1 对比：第二段从 551ms 提前到 351ms，间隔正好是节流的 100ms；结果条目从 854ms 提前到 554ms。生成阶段的 partial 用的是另一种节流：`streamResponse` 收到第一个片段时才启动一个 `partialIntervalMs` 的定时器，到时再提交（`packages/durable/src/harness/generation.ts:747`），第一次也要等满 100ms。所以第 1 次回复的文字在 117ms 才出现，比 `generation={attempt:1,partial:""}` 晚了约 100ms。

### 7.2 用 hook 拦截工具调用

**问题。** `m11b-observe.ts hook` 另装一个名为 `policy` 的扩展，用 `hook(ToolTask, …)` 注册：`beforeTool` 对所有调用返回 `{ block: "lab policy forbids slow" }`，`afterTool` 打印结果的诊断码；用 `hook(GenerationTask, …)` 注册 `beforeRequest` 打印消息的 role。会打印什么？transcript 里的工具结果是什么？

**预测。**

- 第 1 次请求前 `beforeRequest`：`system, user`。
- 工具任务 `call` 阶段：`beforeTool` 被调用，返回 block。工具不执行，`reject()` 构造一个 `blocked` 诊断的错误结果；`afterTool` 也作用于这个 Harness 写的结果（D5 §4.8），所以会打印 `isError=true diagnostics=blocked`。
- 第 2 次请求前 `beforeRequest`：`system, user, assistant, toolResult`。
- 结果条目内容只有 `<harness>` 块：`[error] Tool call blocked: lab policy forbids slow`。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11b-observe.ts hook
```

```
  13  [beforeRequest] system, user
 248  [beforeTool] slow {"path":"README.md"} → block
 248  [afterTool] slow isError=true diagnostics=blocked
 250  [beforeRequest] system, user, assistant, toolResult
[transcript] 13 pi.tool-result isError=true: "<harness>\n[error] Tool call blocked: lab policy forbids slow\n</harness>"
```

**结论。** 预测成立。被拦截的调用和被执行的调用一样有一个 `pi.tool-result` 条目，模型在第 2 次请求里看到它。错误文本由 `harnessError("blocked", …)`（`packages/durable/src/harness/tool.ts:173`）和 `renderDiagnostics` 生成。

### 7.3 晚加入的客户端

**问题。** `20-late-join.ts` 里，一个 `count` 工具每隔一段时间输出一个数字，从 1 数到 10，之后模型慢慢吐出回答。客户端在工具已经输出到 5 时才 `watch()`。它第一次看到的视图里有什么？之后呢？

**预测。** `attach()` 在 line 上读当前状态（§4.2），所以第一个值里条目是 `pi.user, pi.system, pi.assistant`，槽位 `running` 且 `output` 已经是 `1` 到 `5`。之后每次回调只多出新的数字，然后是回答的 partial 变长，最后条目里多出 `pi.tool-result` 和第二个 `pi.assistant`。客户端看不到 1 到 5 是怎么一个个出现的：没有历史可重放。

**运行。**

```bash
cd $PI/packages/durable
node --conditions=source --experimental-strip-types test/examples/20-late-join.ts
```

```
view entries: [ 'pi.user', 'pi.system', 'pi.assistant' ]
view tool slot: running "1\n2\n3\n4\n5\n"
view output now: "1\n2\n3\n4\n5\n6\n"
view output now: "1\n2\n3\n4\n5\n6\n7\n"
view output now: "1\n2\n3\n4\n5\n6\n7\n8\n"
view output now: "1\n2\n3\n4\n5\n6\n7\n8\n9\n"
view output now: "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"
view answer now: "Counted to t"
view answer now: "Counted to ten, and this ans"
view answer now: "Counted to ten, and this answer streams slow"
view entries: [
  'pi.user',
  'pi.system',
  'pi.assistant',
  'pi.tool-result',
  'pi.assistant'
]
```

`view answer now` 的片段长短每次运行可能不同（假模型的片段是随机长度）。

**结论。** 预测成立。第一行和第二行是挂载时刻的状态，之后的每一行是一次提交的增量。

---

## 8. 自测与衔接

### 8.1 自测

1. 进程在工具输出到一半时崩溃。重新打开后，一个新连上来的界面在槽位里能看到崩溃前的输出吗？
2. 两个扩展都给 `ToolTask` 注册了 `beforeTool`，第一个扩展的 handler 抛了异常。第二个还会被调用吗？工具会执行吗？
3. code mode 脚本 `await tools.a(); await tools.b();` 在 `b` 执行中进程崩溃。重启后 `a` 会再执行吗？`b` 呢？

<details>
<summary>答案</summary>

1. 能看到已经持久化的那部分：延迟提交最迟在 `persistWithinMs` 后写入存储（§4.1）。重新打开后挂载从存储加载 `pi.live`。如果工具不是 replay-safe，`execute` 阶段用这部分输出构造 `interrupted` 结果，槽位随后变成 `done`（D5 §7.1）。
2. 工具任务的 `beforeTool` 由 `call` 阶段自己捕获异常并设置 `block`（`packages/durable/src/harness/tool.ts:161-171`），之后的 handler 因为 `block !== undefined` 直接返回，所以第二个 handler 实际不做任何事，工具不执行。其他 hook（例如 `afterTool`）的异常由 `hooks.each` 报告后继续下一个（§4.5）。
3. `a` 不会：它的结果在 journal 和 `NestedCallDoc` 里，重放时脚本以同样的 key `c1` 发起调用，挂接到已结束的嵌套任务，直接拿到结果。`b` 的嵌套任务被崩溃打断时，按 `b` 自己的 replay 策略恢复：safe 就重跑，否则得到 `interrupted` 结果，交给脚本（§4.6，spec §7.6）。

</details>

### 8.2 衔接

M11 到这里结束。D1–D6 合起来回答了一个问题：怎么把 M2 那个在内存里跑的 agent 循环，变成一组可以在任何时刻崩溃、重新打开后从存储继续的任务。总结篇（summary.md）把 M2 的循环和 M8 的 AgentSession 放在一起，比较两种做法的取舍。
