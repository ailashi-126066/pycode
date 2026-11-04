# M11 durable · D5 内置任务

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。durable 自带的示例在 `$PI/packages/durable` 目录下运行；本文新增的实验脚本在 `docs/pi-tutorial/lab` 目录下运行，并且已经设置了 `export PI=/path/to/pi`。
>
> 本篇假定你读过 [D4](D4-tasks-scheduler.md)：任务、阶段、检查点、`runtime.commit()`、规则 1–6、`waiting`/`sleeping`、重启计数和中止标记。还要用到 [M2](../M2-agent-core.md) 的 `runLoop`，本篇会逐段对照。

---

## 1. 这个模块是什么

### 1.1 职责

D4 的调度器不认识任何任务种类。本篇读 Harness 自带的几个任务，它们合起来就是 durable 版本的 agent 循环：

- `pi.generation`（`GenerationTask`）：一次模型请求。准备系统提示词和工具清单、请求或轮询模型、按退避重试、分类回复。回复里有工具调用时，为每个调用创建一个子任务，等它们结束后把 run 交给下一个生成任务。
- `pi.tool`（`ToolTask`）：一次工具调用。解析工具、校验参数、运行 `beforeTool` hook、**先提交意图再执行**、运行 `afterTool` hook、追加结果条目。恢复时按工具的 `replay` 策略决定是否重跑。工具还可以通过 `api.executeTool()` 调用别的工具（嵌套调用），每个嵌套调用也是一个 `pi.tool` 任务。
- `pi.compaction`（`CompactionTask`）：选取旧前缀、调用模型生成摘要、放置摘要条目。

把这些任务串起来的是两样持久状态：

- **inbox**（`pi.inbox` 文档）和 **submission**（提交记录）：用户输入怎么进入会话，忙的时候怎么排队，`requestId` 怎么让重复提交变成同一个。
- **live**（`pi.live` 文档）：当前 run 由哪个任务控制、流式 partial、工具槽位。D6 会从观察者的角度读它，本篇只读和任务相关的部分。

### 1.2 在调用链中的位置

M2 的循环在一个函数里：

```
runLoop()                                    agent-loop.ts:172   [M2]
└─ while (true) / while (hasMoreToolCalls…)  188 / 192
    ├─ prepareNextTurn / declareToolChanges  194 / 220
    ├─ prepareRequest                        228
    ├─ streamAssistantResponse               251
    ├─ error/aborted 退出                     254
    ├─ length → failToolCallsFromTruncatedMessage  277
    ├─ executeToolCalls                      279 → 518
    ├─ finishTurn                            295
    ├─ followUp / explicitContinuation       311 / 320
    └─ agent_end                             329
```

durable 把它拆成任务和阶段，每个箭头都是一次持久提交：

```
conversation.submit({ type: "input", … })         [本篇 §4.1]
└─ admitSubmission()  空闲：追加 pi.user 条目，startRun() 创建 pi.generation
    pi.generation  prepare → request ─┬─ 无工具调用：answer()，结束 run
                    ▲      │          │
                    │      ├─ 可重试的错误：sleeping → retry → prepare
                    │      ├─ deferred：sleeping → poll
                    │      └─ 有工具调用：startToolRound()，waiting on [pi.tool…]
                    │                   │
                    │      pi.tool  call ─(意图)→ execute ─→ 结果条目
                    │                   │
                    │      tools 阶段：finishToolRound()
                    └──── 新的 pi.generation（run 交接）
```

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/durable/src/harness/generation.ts` | 1187 | `GenerationTask` 的 7 个阶段、abort、`classify`、`startToolRound`、`finishToolRound`、`startRun` | 精读阶段和分类，失败分支（`failNoModel` 等）略读 |
| `packages/durable/src/harness/tool.ts` | 1140 | `ToolTask`、`run()`、`settle()`、嵌套调用、结果规范化 | 精读 `call`/`execute`/`abort`、`run`、`settle`、`startNestedCall`；输出截断和 structured output 校验略读 |
| `packages/durable/src/harness/inbox.ts` | 178 | `pi.inbox` 文档、边界（boundary）上放置排队项 | 精读 |
| `packages/durable/src/harness/submissions.ts` | 317 | `Submissions` 类、`admitSubmission()` | 精读 `admitSubmission` |
| `packages/durable/src/harness/compaction.ts` | 500 | `CompactionTask`：`select` → `summarize` → `retry` | 读阶段结构，摘要提示词和切点选择略读（M5 讲过同样的思路） |
| `packages/durable/src/harness/live.ts` | 451 | `pi.live` 文档、`endRun`、重启上限辅助函数、`settleSchedulerOutcome` | 读本篇用到的部分，其余 → D6 |
| `packages/durable/docs/spec.md` §6、§8 | 第 2847–3066、4489–5378 行 | submission 和 inbox、内置任务 | 对照阅读 |

### 1.4 读完能回答的问题

1. M2 的 `runLoop` 里哪一步对应 durable 的哪个阶段？哪些 M2 的 hook 在 durable 里变成了阶段边界？
2. 进程在模型流式输出到一半时死了，重新打开后会重新发请求吗？已经收到的 partial 去哪了？
3. 进程在工具执行到一半时死了，`replay: "safe"` 和默认值分别会发生什么？
4. 一个工具通过 `executeTool()` 调用了三个工具后崩溃。重跑时这三个调用会再执行一次吗？
5. 宿主因为网络超时把同一条消息提交了两次，会出现两条用户消息吗？

---

## 2. 先跑一次

### 2.1 一次带工具调用的 run

[`lab/m11b-observe.ts`](../lab/m11b-observe.ts) 用假模型跑一次和 README §5 主线场景同样形状的 run：第 1 次回复是文字加一个 `slow` 工具调用，第 2 次回复是总结。`graph` 模式打印任务图的每次变化：

```bash
cd docs/pi-tutorial/lab
node --conditions=source --experimental-strip-types m11b-observe.ts graph
```

```
   6  (没有活任务)
   9  9:pi.generation pending phase=prepare
  10  9:pi.generation running phase=prepare
  12  9:pi.generation running phase=request
 245  9:pi.generation waiting phase=tools on=[12]  |  12:pi.tool pending phase=call owner=9
 245  9:pi.generation waiting phase=tools on=[12]  |  12:pi.tool running phase=call owner=9
 248  9:pi.generation waiting phase=tools on=[12]  |  12:pi.tool running phase=execute owner=9
 852  9:pi.generation waiting phase=tools on=[12]
 853  9:pi.generation running phase=tools
 854  14:pi.generation pending phase=prepare
 854  14:pi.generation running phase=prepare
 854  14:pi.generation running phase=request
 932  (没有活任务)
```

M2 的一次 `agent.prompt()` 在这里变成了三个任务：两个 `pi.generation`（对应 M2 的两轮）和一个 `pi.tool`。

### 2.2 同一次 run 的条目

`dup` 模式用同一个 `requestId` 提交两次（§7.3），最后打印会话条目：

```bash
node --conditions=source --experimental-strip-types m11b-observe.ts dup
```

```
第 1 次 submit：id=8
第 2 次 submit：id=8
settled: {"conversationId":1,"requestId":"req-1","type":"input","status":"done","entry":7,"id":8,"answer":15}
条目：7:pi.user, 10:pi.system, 11:pi.assistant, 13:pi.tool-result, 15:pi.assistant
```

条目和 README §5.3 主线场景的会话文件一一对应：`pi.user` 是用户消息，`pi.system` 是系统提示词和工具清单，`pi.assistant` 两条，`pi.tool-result` 一条。ID 不连续，因为文档、任务、提交记录和条目共用一个 ID 序列。

---

## 3. 数据结构

### 3.1 GenerationCheckpoint：生成任务的 7 个阶段

源码：[`packages/durable/src/harness/generation.ts:69-136`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L69-L136)

```ts
export type GenerationCheckpoint =
	| {
			phase: "prepare";
			attempt: number;
			/** The blocking compaction this generation waited for; it starts no other compaction (spec §8.3). */
			compacted?: TaskId<CompactionResult>;
			/** Error text of the overflow that started `compacted`; checked once when `prepare` resumes. */
			overflow?: string;
			/** Set when the blocking threshold started `compacted`; its outcome is checked once when `prepare` resumes. */
			threshold?: true;
	  }
	| {
			phase: "request";
			attempt: number;
			compacted?: TaskId<CompactionResult>;
			model: ModelRef;
			thinkingLevel: ModelThinkingLevel;
			/** The settings' request options when preparation committed; a resend after recovery uses them unchanged. */
			streamOptions: ConversationStreamOptions;
			/** Newest entry included in the request. */
			cutoff: EntryId;
	  }
	| {
			phase: "retry";
			attempt: number;
			compacted?: TaskId<CompactionResult>;
			until: number;
			/**
			 * The deferred response a poll was fetching when the host died: the next attempt polls it again instead of
			 * sending another request, so the provider's job is neither lost nor paid twice. Absent otherwise.
			 */
			poll?: { model: ModelRef; thinkingLevel?: ModelThinkingLevel; cutoff: EntryId; handle: DeferredHandle };
	  }
	| {
			phase: "poll";
			attempt: number;
			compacted?: TaskId<CompactionResult>;
			model: ModelRef;
			/** Absent in checkpoints of earlier versions. */
			thinkingLevel?: ModelThinkingLevel;
			cutoff: EntryId;
			handle: DeferredHandle;
			pollAt: number;
	  }
	| {
			/** Waiting on the round's tool tasks, which the generation owns (spec §8.5). */
			phase: "tools";
			/** The tool-calling answer. */
			assistant: EntryId;
			/** Tool tasks created so far, in call order; grows by one per started call of a sequential round. */
			tools: TaskId<ToolTaskResult>[];
			/** Calls of a sequential round not started yet, in call order. */
			pending: string[];
	  }
	| {
			/**
			 * The run answered: its answer is committed and its inputs settled, and it keeps `pi.live.run` while `afterRun`
			 * hooks are asked for tasks (spec §8.3). A crash here asks them again: nothing was created yet.
			 */
			phase: "finish";
			run: RunSummary;
	  }
	| {
			/** Waiting on the tasks `afterRun` hooks asked for, which the generation owns; then the run ends. */
			phase: "drain";
			answer: EntryId;
			tasks: TaskId[];
	  };
```

| 阶段 | 带什么 | 相当于 M2 的 |
|---|---|---|
| `prepare` | `attempt`；等待过的压缩任务 | `prepareNextTurn` + `declareToolChanges` + `prepareRequest` |
| `request` | 固定下来的模型、思考级别、请求选项、`cutoff`（请求包含的最后一个条目） | `streamAssistantResponse` |
| `retry` | `until`；被打断的 deferred 轮询 | pi-ai / M8 的重试，M2 没有 |
| `poll` | provider 的 deferred handle、`pollAt` | M2 没有 |
| `tools` | 这一轮的 assistant 条目、已创建的工具任务、串行轮次里还没开始的调用 | `executeToolCalls` |
| `finish` | run 的摘要 | `agent_end` 之后的 `afterRun` |
| `drain` | `afterRun` hook 要求创建的任务 | 同上 |

`request` 检查点存下了 `streamOptions` 和 `cutoff`。恢复后的重发（如果需要）用的是完全相同的输入，不会因为设置在中途改了而变成另一个请求。

**真实实例。** §7.1 的实验里，生成任务在等工具时的记录是：

```
task 9 pi.generation status=waiting restarts=0 {"phase":"tools","assistant":11,"tools":[12],"pending":[]}
```

### 3.2 ToolTaskInput、ToolTaskCheckpoint、ToolTaskResult

源码：[`packages/durable/src/harness/tool.ts:62-95`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/tool.ts#L62-L95)

```ts
/**
 * A model-issued call, read from its assistant entry, or a nested call a running tool made through `executeTool()`,
 * whose call the input carries because no entry holds it.
 */
export type ToolTaskInput =
	| {
			kind: "model";
			assistant: EntryId;
			callId: string;
			/** A call to a tool its request did not offer: settles `tool_unavailable` without resolving the name. */
			unoffered?: true;
	  }
	| {
			kind: "nested";
			parent: TaskId;
			parentCallId: string;
			key: string;
			call: ToolCall;
			/** `false`: commit no running output, details, or diagnostics to the slot; the result still gets them. */
			progress?: false;
	  };

export type ToolTaskCheckpoint =
	| { phase: "call" }
	/** Durable intent: the final arguments and the replay policy recorded before execution. */
	| { phase: "execute"; arguments: JsonObject; replay: "safe" | "unsafe" };

/**
 * A model-issued call's result is its transcript entry; a nested call's result is in its caller's
 * `NestedCallDoc`, so the receipt stays small and the result retires with the caller. A version 1 task that was
 * already holding its outcome as `completing` when the Harness upgraded finishes with a version 1 result,
 * `{ entryId, control? }` without `kind`; treat a result without `kind` as a model-issued call's.
 */
export type ToolTaskResult = { kind: "model"; entryId: EntryId; control?: ToolControl } | { kind: "nested" };
```

| 类型 | 说明 |
|---|---|
| `ToolTaskInput` | 模型发起的调用只存 assistant 条目 ID 和调用 ID，调用本身从条目里读；嵌套调用没有条目，输入里带着整个 `ToolCall`、父任务和 `key` |
| `ToolTaskCheckpoint` | 只有两个阶段。`call` 是初始阶段；`execute` 是**意图**：最终参数和当时的 replay 策略，在工具执行**之前**提交 |
| `ToolTaskResult` | 模型发起的调用，结果是 transcript 里的 `pi.tool-result` 条目 ID；嵌套调用的结果在父任务的 `NestedCallDoc` 里 |

**真实实例。** §7.1 里进程在工具执行中被杀，重新打开后工具任务的记录：

```
task 12 pi.tool status=pending restarts=1 {"phase":"execute","arguments":{},"replay":"unsafe"}
```

`phase: "execute"` 说明意图已经提交，工具可能已经运行过。

### 3.3 LiveRun 和 LiveState：run 控制

源码：[`packages/durable/src/harness/live.ts:137-192`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/live.ts#L137-L192)

```ts
/** Run control (spec §8.2). */
export type LiveRun = {
	/** The task that settles the run's inputs: its generation, handed from one to the next. */
	taskId: TaskId;
	/** The run's placed inputs, settled when it ends; `[]` once it answered and runs only its `afterRun` tasks. */
	inputs: SubmissionId[];
	/** Tool rounds so far, which `AgentState.maxTurns` bounds; an `onYield` continuation starts the count over. */
	turns: number;
	/**
	 * The run's first entry: the first one the boundary that started it placed, a continue's the failed run's it
	 * continues; otherwise its first tool-calling answer. A run started before it was recorded may lack it; `afterRun`
	 * hooks then get the answer as the first entry.
	 */
	first?: EntryId;
	/**
	 * Present once the run answered and its generation waits for the tasks `afterRun` hooks asked for, listed once
	 * created: queued items stay queued until they are terminal, and the run then ends without a failure.
	 */
	afterRun?: { tasks?: TaskId[] };
};

/** Built-in live conversation state: run control and presentation of the current generation and tool round. */
export type LiveState = {
	/** Run control; present exactly while busy, also while a run that answered runs its `afterRun` tasks. */
	run?: LiveRun;
	/** Presentation of the current generation attempt. */
	generation?: {
		attempt: number;
		/** Committed throttled partial of the in-flight response. */
		message?: JsonRepresentation<AssistantMessage>;
		/** Durable backoff before the next attempt. */
		retry?: { at: number; error: string };
		/** Provider-side deferred response being polled. */
		deferred?: { pollAt: number };
	};
	/** The current tool round in call order, from the tool-calling answer until the generation's `tools` phase ends it. */
	tools?: ToolSlot[];
	/**
	 * Nested calls of running tool calls, in creation order, so a parent precedes its children and task IDs ascend. A
	 * call's nested slots are removed when the call settles.
	 */
	nestedTools?: NestedToolSlot[];
	/** Live compaction tasks in task ID order; absent when none. */
	compactions?: CompactionStatus[];
	/**
	 * The newest compaction failure, so a client can show it after the task ended. A compaction that produces a summary
	 * removes it; one that is aborted or finds nothing to compact leaves it.
	 */
	compactionFailure?: CompactionFailure;
	/**
	 * Why the newest run ended without an answer, so a client can show it after the run ended. Set in the commit that
	 * ends the run; removed when a later commit starts a run. A run started in the same commit, by
	 * `afterFailedRun: "continue"` or after a reset, keeps it.
	 */
	runFailure?: RunFailure;
};
```

本篇只看 `run`：

| 字段 | 说明 |
|---|---|
| `taskId` | 当前控制 run 的任务。每一轮的生成任务把它交给下一个（`handOver`） |
| `inputs` | 这个 run 放置的用户输入 submission。run 结束时一起 settle |
| `turns` | 工具轮数，`maxTurns` 用它限制 |
| `first` | run 的第一个条目 |

`run` 存在就表示会话**忙**。`generation`、`tools`、`nestedTools` 是给界面看的进度，D6 详细读。

**真实实例。** `m11b-observe.ts live` 的输出（D6 §2.1）里，run 开始时是 `run={taskId:9,inputs:[8],turns:0}`，工具轮之后 `turns:1`，交接后 `taskId:14`，run 结束后 `pi.live = {}`。

### 3.4 InboxItem 和 SubmissionRecord

源码：[`packages/durable/src/harness/inbox.ts:7-19`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/inbox.ts#L7-L19)

```ts
/** A queued submission: user input for a run, with the entry it is placed as when not `pi.user`, or an entry write. */
export type InboxItem =
	| {
			id: SubmissionId;
			mode: "steer" | "followUp";
			content: JsonRepresentation<UserInput>;
			entry?: JsonRepresentation<InputEntry>;
	  }
	/** `entry` is an `EntryDraft`, stored as plain JSON. */
	| { id: SubmissionId; mode: "write"; entry: JsonObject };

/** Built-in queue of one conversation's submissions waiting for a boundary, in ID order. */
export type InboxState = { items: InboxItem[] };
```

源码：[`packages/durable/src/types.ts:495-542`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L495-L542)

```ts
/** Identity fields shared by every durable submission state. */
type SubmissionRecordBase = {
	/** Adapter-owned context of the admission span; absent without propagation. */
	readonly trace?: TelemetryCarrier;
	readonly id: SubmissionId;
	readonly conversationId: ConversationId;
	/** Host-provided deduplication key, scoped to the conversation. */
	readonly requestId?: string;
};

/** Durable lifecycle of one admitted user input, passive entry write, or continue. */
export type SubmissionRecord =
	| (SubmissionRecordBase & {
			readonly type: "input";
	  } & (
				| {
						/** Admitted but not yet represented in the transcript. */
						readonly status: "queued";
						readonly entry?: never;
						readonly answer?: never;
						readonly reason?: never;
						readonly detail?: never;
				  }
				| {
						/** Added to the transcript and owned by an active run. */
						readonly status: "placed";
						readonly entry: EntryId;
						readonly answer?: never;
						readonly reason?: never;
						readonly detail?: never;
				  }
				| {
						/** Successfully answered user input. */
						readonly status: "done";
						readonly entry: EntryId;
						readonly answer: EntryId;
						readonly reason?: never;
						readonly detail?: never;
				  }
				| {
						/** Terminal input that can no longer receive an answer. */
						readonly status: "unanswered";
						readonly entry?: EntryId;
						readonly answer?: never;
						readonly reason: string;
						readonly detail?: JsonValue;
				  }
			))
```

| 字段 | 说明 |
|---|---|
| `status` | `queued`（在 inbox 里排队）→ `placed`（已追加成条目，属于一个活的 run）→ `done`（有了回答）或 `unanswered`（带 `reason`） |
| `entry` | 放置成的条目 |
| `answer` | 回答它的 assistant 条目 |
| `requestId` | 宿主给的去重键，在会话内唯一 |

**真实实例。** §2.2 里 `settled` 那一行：`{"conversationId":1,"requestId":"req-1","type":"input","status":"done","entry":7,"id":8,"answer":15}`。用户消息是条目 7，回答是条目 15。

### 3.5 NestedCallDoc：嵌套调用的索引

源码：[`packages/durable/src/harness/tool.ts:103-125`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/tool.ts#L103-L125)

```ts
/** One nested call of a running tool: its task, and once it ended, its result. */
export type NestedCallState = {
	/** Absent only inside the commit that creates the call; a creation that fails leaves no member. */
	taskId?: TaskId<ToolTaskResult>;
	/** Exactly what `executeTool()` returns; absent while running, and for a call the scheduler faulted or orphaned. */
	result?: JsonRepresentation<NestedToolExecutionResult>;
};

/**
 * The nested calls of a tool call, a task-scoped family of the calling task keyed by the nested call's key: so a rerun
 * finds the calls it already made, and the caller reads each result. Created with the nested task; the nested call's
 * terminal commit adds its result. One document per call, so no commit rewrites the others. Retires with the caller.
 */
export const NestedCallDoc = defineDocFamily<NestedCallState, null>({
	kind: "pi.tool.nested-call",
	version: 1,
	scope: "task",
	family: true,
	initial: () => ({}),
	// The document retires with its task.
	checkpointWhen: () => false,
});

```

每个嵌套调用一个文档，按 `key` 区分，作用域是**调用方**的任务。重跑的调用方用同一个 key 能找回它之前创建的嵌套任务，嵌套调用结束时把结果写进这个文档。调用方结束时文档随任务一起退役。

### 3.6 类型关系图

```
 Submission（提交记录）── requestId 去重
   queued ─ InboxItem 在 pi.inbox 里 ─┐ 边界 applyBoundary()
   placed ─ entry: pi.user 条目 ◀──────┘
   done ─ answer: pi.assistant 条目
        │ startRun()
        ▼
 pi.live.run { taskId, inputs, turns }  ← 每轮 handOver 给新的 pi.generation
        │
 pi.generation  GenerationCheckpoint
   prepare → request → (retry|poll) → tools{assistant, tools[], pending[]}
                                           │ createTask(owner = generation)
                                           ▼
                                  pi.tool  ToolTaskInput{assistant, callId}
                                    call → execute{arguments, replay}
                                           │ api.executeTool(key)
                                           ▼
                                  pi.tool  ToolTaskInput{kind:"nested", parent, key, call}
                                    结果 → 父任务的 NestedCallDoc[key]
                                  模型发起的调用结果 → pi.tool-result 条目
```

---

## 4. 从入口读起

```
Conversation.submit() → Submissions.submit() → admitSubmission()   §4.1
    └─ startRun()                                                  §4.1
pi.generation
    prepare                                                        §4.2
    request → streamResponse() → classify()                        §4.3
        retry / poll                                               §4.4
        startToolRound()                                           §4.5
    tools → finishToolRound()                                      §4.6
    answer() / finish / drain                                      §4.7
    abort                                                          §4.7
pi.tool
    call → run() → settle()                                        §4.8
    execute（恢复）/ abort                                          §4.9
    api.executeTool() → startNestedCall()                          §4.10
inbox 边界：applyBoundary()                                         §4.11
pi.compaction                                                      §4.12
和 M2 runLoop 的逐段对照                                             §4.13
```

### 4.1 admitSubmission() 和 startRun()：输入进入会话

**调用位置**：`conversation.submit({ type: "input", content }, context)` → `Submissions.submit()`（`packages/durable/src/harness/submissions.ts:65`）→ 一次 `commitWith`，里面调用 `admitSubmission()`。

源码：[`packages/durable/src/harness/submissions.ts:223-240`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/submissions.ts#L223-L240)

```ts
export async function admitSubmission(
	tx: Tx,
	conversationId: ConversationId,
	draft: SubmissionDraft,
	now: number,
	queueModes: QueueModes,
	span?: TelemetrySpan,
): Promise<Admission> {
	if (draft.type === "input" && draft.entry !== undefined) checkInputEntry(draft.entry);
	if (draft.requestId !== undefined) {
		const existing = await tx.submissionByRequest(conversationId, draft.requestId);
		if (existing !== undefined) {
			if (existing.type !== draft.type) {
				throw new Error(`Request ${draft.requestId} already identifies a submission of type ${existing.type}`);
			}
			return { id: existing.id, status: existing.status === "unanswered" ? "done" : existing.status };
		}
	}
```

**讲解**

`requestId` 的去重在最前面：同一会话里已经有这个 `requestId` 的提交，就直接返回它的 ID，**什么也不写**。类型不同时抛错，防止一个键被两种提交复用。因为这是在同一次提交里先查后写，两次并发的提交在 Session line 上也是串行的，只有一个会真正写入。

后面的逻辑（`packages/durable/src/harness/submissions.ts:241-316`）按会话忙不忙分情况：

- 忙（`pi.live.run` 存在）：输入以 `queued` 状态排进 `pi.inbox`，`whenBusy: "steer"` 时 mode 是 `steer`，否则是 `followUp`。
- 空闲但 inbox 里还有排队项：也排进去，然后立即跑一次 `final` 边界（§4.11）。
- 空闲且 inbox 为空：追加 `pi.user` 条目，创建 `placed` 状态的提交，调用 `startRun()`。

源码：[`packages/durable/src/harness/generation.ts:1162-1183`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L1162-L1183)

```ts
export async function startRun(
	tx: Tx,
	conversationId: ConversationId,
	live: Draft<LiveState>,
	run: { readonly inputs: SubmissionId[]; readonly first: EntryId | undefined },
	options?: { readonly keepFailure?: boolean },
): Promise<void> {
	const { inputs, first } = run;
	const taskId = await createGeneration(tx, conversationId, { submissionIds: inputs });
	live.run = { taskId, inputs, turns: 0, ...(first === undefined ? {} : { first }) };
	if (options?.keepFailure !== true) delete live.runFailure;
}

/** A generation owned by its conversation. */
function createGeneration(
	tx: Tx,
	conversationId: ConversationId,
	telemetry: Pick<TaskCreationOptions, "submissionIds"> = {},
): Promise<TaskId<GenerationResult>> {
	return tx.createTask(GenerationTask, {}, { ownership: { kind: "conversation" }, conversationId, ...telemetry });
}

```

`startRun` 在同一次提交里创建一个会话拥有的 `pi.generation` 任务，并写入 `pi.live.run`。所以"用户消息落盘"和"开始生成"是原子的：不会出现条目已写、却没有任务来回答它的情况。

**往哪走**：§4.2。

### 4.2 prepare 阶段

源码：[`packages/durable/src/harness/generation.ts:162-228`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L162-L228)

```ts
export const GenerationTask = defineTask<GenerationInput, GenerationCheckpoint, GenerationResult, GenerationHooks>({
	name: "pi.generation",
	version: 2,
	initial: () => ({ phase: "prepare", attempt: 1 }),
	// Version 2 leaves calls to tools not offered in a sequential round's `pending`, flagged `unoffered` in their slots.
	// A version 1 Harness would start their tool tasks unflagged, resolving the name, so it leaves version 2 blocked
	// instead. A version 1 checkpoint has no such calls and is kept as it is.
	migrate: (input, checkpoint) => ({
		input: input as GenerationInput,
		checkpoint: checkpoint as GenerationCheckpoint,
	}),
	phases: {
		/**
		 * Render the system prompt and tool loadout and append the positional `pi.system` entries they need, then move to
		 * `request`. The agent and settings resolved here are fixed for this request. Only the Harness writes model
		 * context to a busy conversation, so the context read here is still current at the commit; display-only entries
		 * may land meanwhile.
		 */
		prepare: async (task, runtime, context) => {
			if (cutOffTooOften(task, runtime)) return endCutOff(runtime, task, context);
			const { conversationId } = runtime;
			const agent = await runtime.agent(context);
			const settings = runtime.settings;
			const { model, thinkingLevel } = agent;
			const resolved = model === undefined ? undefined : runtime.models.getModel(model.provider, model.modelId);
			if (model === undefined || resolved === undefined) return failNoModel(runtime, model, context);
			const { attempt, compacted, overflow, threshold: blocked } = task.state.checkpoint;
			if (compacted !== undefined && overflow !== undefined) {
				const [outcome] = await runtime.outcomes([compacted], context);
				if (outcome?.status !== "completed" || outcome.result.entryId === undefined) {
					return failModelError(runtime, overflow, context);
				}
			}
			// A threshold compaction that found nothing to compact or was declined leaves a request that may still fit. One
			// that failed left a context it judged too large: sending it would at best leave no room for the answer.
			if (compacted !== undefined && blocked === true) {
				const [outcome] = await runtime.outcomes([compacted], context);
				if (outcome !== undefined && outcome.status !== "completed") {
					return failModelError(runtime, compactionFailure(outcome), context);
				}
			}
			const view = await runtime.context(conversationId, context);
			// The model owes an answer only to a context ending in a user message or tool results; system messages say
			// nothing it answers. Otherwise, as for a continue after an answer or in an empty conversation, end the run
			// before any request.
			const last = view.messages.findLast((message) => message.role !== "system");
			if (last?.role !== "user" && last?.role !== "toolResult") return failNothingToAnswer(runtime, context);
			const shown = replaySections(view.messages);
			let env: ExecutionEnv | undefined;
			try {
				env = await runtime.env(context);
			} catch (error) {
				if (context.abortSignal?.aborted) throw error;
				runtime.report(error, { kind: "host", callback: "env" });
			}
			const input: PromptInput = { conversationId, agent, env, shown: Object.fromEntries(shown), read: runtime };
			const desired = await renderSections(
				agent.sections,
				input,
				shown,
				(error, section) =>
					runtime.report(error, {
						kind: "extension",
						...optionalExtension(sectionExtension(agent, section.key)),
					}),
				context,
			);
```

源码：[`packages/durable/src/harness/generation.ts:229-261`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L229-L261)

```ts
			const entries = planSystemEntries(view, desired, agent.tools, runtime.now());
			const threshold =
				compacted === undefined
					? thresholdCompaction(view, entries, resolved.contextWindow, compactionPolicy(settings, model))
					: undefined;
			if (threshold === "blocking") {
				// Compact first and prepare again; the transcript is unchanged until the compaction appends.
				await runtime.commit(async (tx): Promise<Next> => {
					const child = await createCompaction(tx, conversationId, { reason: "threshold" }, runtime.taskId);
					const checkpoint = { phase: "prepare", attempt, compacted: child, threshold: true } as const;
					return { status: "waiting", checkpoint, on: [child], policy: "allSettled" };
				}, context);
				return;
			}
			await runtime.commit(async (tx) => {
				// The context ends in a message the model answers, so the conversation has an entry.
				let cutoff = (await tx.latestEntryId(conversationId))!;
				for (const entry of entries) cutoff = (await tx.appendEntry(SystemEntry, conversationId, entry)).id;
				// Checked in this commit, so a compaction admitted during preparation counts.
				if (threshold === "background" && (await tx.doc(LiveDoc, conversationId)).compactions === undefined) {
					await createCompaction(tx, conversationId, { reason: "threshold" });
				}
				const request = {
					attempt,
					...(compacted === undefined ? {} : { compacted }),
					model,
					thinkingLevel,
					streamOptions: settings.stream,
					cutoff,
				};
				return { status: "running", checkpoint: { phase: "request", ...request } };
			}, context);
		},
```

**讲解**

`prepare` 做的事相当于 M2 一轮开始时的 `prepareNextTurn`、`declareToolChanges`、`prepareRequest`：

1. `cutOffTooOften`：重启次数到上限就结束 run（§4.4 末尾）。内置任务都在阶段开头自己检查（D4 §4.3）。
2. `runtime.agent()` 解析会话的 agent（模型、工具、系统提示词段落），每个阶段解析一次。
3. `runtime.context()` 取已提交的模型上下文。最后一条非 system 消息不是 user 或 toolResult 时，没有东西要回答，run 以 `nothing_to_answer` 结束，不发请求。
4. 渲染系统提示词段落，和 transcript 里已经生效的段落比较（`replaySections`），`planSystemEntries` 算出需要追加的 `pi.system` 条目。这和 M2 的"工具清单写进 transcript"（M2 §6.2）是同一个设计。
5. 上下文超过阻塞阈值时，先创建一个由本任务拥有的压缩子任务，自己 `waiting` 它，压缩完重新 `prepare`。
6. 否则一次提交：追加 `pi.system` 条目，`cutoff` 设为最新条目，进入 `request`，检查点里固定模型、思考级别和请求选项。

所有输入都来自已提交的状态，所以 `prepare` 在崩溃后重跑是安全的：它只是再算一遍同样的东西。

**往哪走**：§4.3。

### 4.3 request 阶段、streamResponse() 和 classify()

源码：[`packages/durable/src/harness/generation.ts:262-301`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L262-L301)

```ts
		request: async (task, runtime, context) => {
			// Charging the attempt may itself be what keeps failing, such as converting a huge partial: the limit ends it.
			if (cutOffTooOften(task, runtime)) return endCutOff(runtime, task, context);
			// The host stopped while this request was in flight: a failed attempt, not a resend (spec §8.3).
			if (task.restarts !== undefined) return interrupted(runtime, task.state.checkpoint, context);
			const { attempt, compacted, model: ref, thinkingLevel, streamOptions, cutoff } = task.state.checkpoint;
			const conversationId = runtime.conversationId;
			// The attempt shown is presentation, stored like progress (`PROGRESS_COMMIT`); converting a partial left by an
			// interrupted attempt appends an entry, which makes the commit immediate.
			await runtime.commit(
				async (tx) => {
					const live = await tx.doc(LiveDoc, conversationId);
					await convertPartial(tx, live, conversationId);
					live.generation = { attempt };
					return undefined;
				},
				context,
				PROGRESS_COMMIT,
			);
			const model = runtime.models.getModel(ref.provider, ref.modelId);
			if (model === undefined) return failNoModel(runtime, ref, context);
			const view = await runtime.context(conversationId, context, { at: cutoff });
			let messages = view.messages;
			await runtime.hooks.each("beforeRequest", async (hook) => {
				const replaced = await hook({ messages }, runtime, context);
				if (replaced !== undefined) messages = replaced.messages;
			});
			const options: SimpleStreamOptions = {
				...streamOptions,
				signal: runtime.signal,
				telemetry: context.telemetry,
				sessionId: await ensureProviderSessionId(runtime, context),
				...(thinkingLevel === "off" ? {} : { reasoning: thinkingLevel }),
				onPayload: (payload) => providerPayload(runtime, model, payload, context),
			};
			const message = await streamResponse(runtime, model, messages, options, attempt, context);
			const request = { attempt, compacted, model: ref, thinkingLevel, cutoff, messages: view.messages };
			await classify(runtime, request, message, context);
		},
		/** After the backoff's sleep; a run before `until` (an early wake, or a record of an earlier version) sleeps again. */
```

**讲解**

第二个 `if` 是理解 durable 生成的关键：

```ts
if (task.restarts !== undefined) return interrupted(runtime, task.state.checkpoint, context);
```

`request` 阶段是"已经发出请求、还没提交回复"的状态。进程死在这里，重新打开时 `restarts` 不为空（D4 §4.2），`request` **不会重发请求**，而是调用 `interrupted()`（`packages/durable/src/harness/generation.ts:501`）：把这次尝试算作一次失败，已提交的 partial 转成一条 `stopReason: "aborted"` 的 assistant 条目，然后按重试策略进入 `retry`（睡到退避时间）或以 `interrupted` 结束 run。请求可能已经在 provider 那边产生了费用，所以它被当作一次失败的尝试计入重试次数，而不是悄悄再发一次。

没有被打断时：

1. 一次 `PROGRESS_COMMIT`（延迟持久化的提交，D3）把 `pi.live.generation.attempt` 写好，顺带转换上一次尝试残留的 partial。
2. 用 `cutoff` 处的上下文（`{ at: cutoff }`）构造消息，`beforeRequest` hook 可以替换它们（相当于 M2 的 `transformContext`）。
3. `streamResponse()` 调用 `runtime.models.streamSimple()`（→ M1），消费事件流：

源码：[`packages/durable/src/harness/generation.ts:739-755`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L739-L755)

```ts
	try {
		const events = runtime.models.streamSimple(model, { messages: [...messages] }, options);
		for await (const event of events) {
			// A partial without content, such as pi-ai's opening `start` event, shows nothing; a deferred response
			// never gets past it, so it never leaves a partial.
			if (event.type === "done" || event.type === "error" || event.partial.content.length === 0) continue;
			changes.observe(event);
			pending = event.partial;
			if (timer === undefined && inFlight === undefined) timer = runtime.setTimer(flush, interval);
		}
		return await events.result();
	} finally {
		stopped = true;
		timer?.();
		await inFlight;
	}
}
```

收到的 partial 由节流器每 `partialIntervalMs`（默认 100ms）提交一次到 `pi.live.generation.message`，也是延迟持久化的。`finally` 停掉节流器并等待在途的提交，保证不会有过期的 partial 在结果之后落地。

4. `classify()` 对终止消息分类：

源码：[`packages/durable/src/harness/generation.ts:758-799`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L758-L799)

```ts
async function classify(
	runtime: Runtime,
	request: Request,
	message: AssistantMessage,
	context: Context,
): Promise<void> {
	// An abort mark or close: the abort invocation or the reopened run handles the committed state.
	runtime.signal.throwIfAborted();
	const conversationId = runtime.conversationId;
	const { attempt, compacted, model: ref, thinkingLevel, cutoff } = request;
	// Record the requested level, as pi-agent-core's agent loop does.
	if (thinkingLevel !== undefined) message.thinkingLevel = thinkingLevel;
	if (message.stopReason === "deferred" && message.deferred !== undefined) {
		const handle = message.deferred;
		const pollAt = Math.max(
			runtime.now() + (handle.pollAfterMs ?? DEFAULT_POLL_AFTER_MS),
			request.pollAt === undefined ? Number.NEGATIVE_INFINITY : request.pollAt + 1,
		);
		await runtime.commit(async (tx): Promise<Next> => {
			(await tx.doc(LiveDoc, conversationId)).generation = { attempt, deferred: { pollAt } };
			const checkpoint = {
				phase: "poll",
				attempt,
				...(compacted === undefined ? {} : { compacted }),
				model: ref,
				...(thinkingLevel === undefined ? {} : { thinkingLevel }),
				cutoff,
				handle,
				pollAt,
			} as const;
			return { status: "sleeping", checkpoint, until: pollAt };
		}, context);
		return;
	}
	await runtime.hooks.each("afterResponse", (hook) => hook(message, runtime, context));
	const calls = message.content.filter((content): content is ToolCall => content.type === "toolCall");
	if (message.stopReason === "toolUse" && calls.length > 0) {
		return startToolRound(runtime, request, message, calls, context);
	}
	if (message.stopReason === "stop" || message.stopReason === "length" || message.stopReason === "toolUse") {
		return answer(runtime, message, context);
	}
```

| 回复 | 去向 |
|---|---|
| `deferred` | `sleeping` 到 `pollAt`，进入 `poll` |
| `toolUse` 且有工具调用 | `startToolRound()`（§4.5） |
| `stop`、`length`、没有调用的 `toolUse` | `answer()`（§4.7） |
| 上下文溢出，且能找到切点 | 追加错误回复，创建压缩子任务，`waiting` 它，回到 `prepare` |
| 可重试的错误 | 追加错误回复，`sleeping` 到退避时间，进入 `retry` |
| 其他错误 | 追加错误回复，run 以 `model_error` 结束 |

和 M2 的一个区别：M2 遇到 `stopReason: "length"` 且有工具调用时，`failToolCallsFromTruncatedMessage` 为每个调用造一个错误结果（M2 §4.7）。durable 把 `length` 当作回答，直接走 `answer()`，工具调用不执行；上下文推导时为这些调用合成 `missing_result` 错误结果（spec §2.1），宿主之后可以发一个 `continue` 让模型回应它们。

**往哪走**：§4.4。

### 4.4 retry 和 poll 阶段

源码：[`packages/durable/src/harness/generation.ts:302-338`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L302-L338)

```ts
		retry: async (task, runtime, context) => {
			const { attempt, compacted, until, poll } = task.state.checkpoint;
			if (cutOffTooOften(task, runtime)) return endCutOff(runtime, task, context);
			if (until > runtime.now()) return runtime.sleepUntil(until, task.state.checkpoint, context);
			await runtime.commit(async (tx) => {
				const live = await tx.doc(LiveDoc, runtime.conversationId);
				const kept = compacted === undefined ? {} : { compacted };
				// A poll the host died during polls its response again, as the next attempt.
				if (poll !== undefined) {
					const pollAt = runtime.now();
					live.generation = { attempt: attempt + 1, deferred: { pollAt } };
					return {
						status: "running",
						checkpoint: { phase: "poll", attempt: attempt + 1, ...kept, ...poll, pollAt },
					};
				}
				live.generation = { attempt: attempt + 1 };
				return { status: "running", checkpoint: { phase: "prepare", attempt: attempt + 1, ...kept } };
			}, context);
		},
		/** After the sleep until `pollAt`; a run before it (an early wake, or a record of an earlier version) sleeps again. */
		poll: async (task, runtime, context) => {
			const { attempt, compacted, model: ref, thinkingLevel, cutoff, handle, pollAt } = task.state.checkpoint;
			if (cutOffTooOften(task, runtime)) return endCutOff(runtime, task, context);
			// The host stopped while the poll's fetch was in flight: a failed attempt, as for a request. Checked before the
			// time, which may have moved backwards on a replacement host; a sleeping poll is never cut off.
			if (task.restarts !== undefined) return interrupted(runtime, task.state.checkpoint, context);
			if (pollAt > runtime.now()) return runtime.sleepUntil(pollAt, task.state.checkpoint, context);
			const model = runtime.models.getModel(ref.provider, ref.modelId);
			if (model === undefined) return failNoModel(runtime, ref, context);
			const message = await runtime.models.fetchDeferred(model, handle, {
				signal: runtime.signal,
				telemetry: context.telemetry,
			});
			const request = { attempt, compacted, model: ref, thinkingLevel, cutoff, pollAt };
			await classify(runtime, request, message, context);
		},
```

**讲解**

两个阶段都是"睡醒之后"的阶段。都先检查 `until`/`pollAt`：提前醒来（例如宿主调用了 `resume()`）就用同一个检查点再睡一次。

- `retry` 把 `attempt` 加 1 回到 `prepare`。注意回到的是 `prepare` 而不是 `request`：重试会重新准备，读取最新的设置和上下文。
- `poll` 用 `fetchDeferred` 取 provider 的异步回复，然后同样交给 `classify`。`poll` 里也有 `task.restarts` 检查：轮询的 fetch 在途时进程死掉，也算一次失败的尝试，下一次尝试轮询同一个 handle，不会重新提交请求。

重启上限：每个阶段开头的 `cutOffTooOften` 在 `restarts >= restartLimit` 时调用 `endCutOff()`，run 以 `restart_limit` 结束（spec §5.4 的表格）。生成任务的 `request` 先按重试策略处理中断，只有中断本身反复失败（例如转换一个巨大的 partial 就把进程搞崩）才会撞到上限。

**往哪走**：§4.5。

### 4.5 startToolRound()：一次提交开始一个工具轮

源码：[`packages/durable/src/harness/generation.ts:1009-1056`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L1009-L1056)

```ts
async function startToolRound(
	runtime: Runtime,
	request: Request,
	message: AssistantMessage,
	calls: readonly ToolCall[],
	context: Context,
): Promise<void> {
	const conversationId = runtime.conversationId;
	const messages =
		request.messages ?? (await runtime.context(conversationId, context, { at: request.cutoff })).messages;
	const offered = new Set(getCurrentTools(messages).map((tool) => tool.name));
	// Read as the round starts; a tool is resolved as its tool task resolves it.
	const tools = (await runtime.agent(context)).tools;
	const sequential =
		runtime.settings.toolExecution === "sequential" ||
		calls.some(
			(call) =>
				offered.has(call.name) && tools.find((tool) => tool.name === call.name)?.executionMode === "sequential",
		);
	await runtime.commit(async (tx): Promise<Next> => {
		const live = await tx.doc(LiveDoc, conversationId);
		const entry = await appendAssistant(tx, conversationId, message);
		const slots: ToolSlot[] = [];
		const tools: TaskId<ToolTaskResult>[] = [];
		const pending: string[] = [];
		for (const call of calls) {
			const unoffered = offered.has(call.name) ? {} : ({ unoffered: true } as const);
			if (sequential && tools.length > 0) {
				pending.push(call.id);
				slots.push({ callId: call.id, name: call.name, status: "pending", ...unoffered });
				continue;
			}
			const taskId = await createToolTask(tx, runtime, entry.id, call.id, unoffered.unoffered);
			tools.push(taskId);
			slots.push({ callId: call.id, name: call.name, taskId, status: "pending", ...unoffered });
		}
		delete live.generation;
		live.tools = slots;
		if (live.run?.taskId === runtime.taskId) {
			// A tool round is one turn of the run (`AgentState.maxTurns`).
			live.run.turns += 1;
			// A run its boundary started without placing anything, as a continue after an answer, begins here.
			live.run.first ??= entry.id;
		}
		const checkpoint = { phase: "tools", assistant: entry.id, tools, pending } as const;
		return { status: "waiting", checkpoint, on: tools, policy: "allSettled" };
	}, context);
}
```

**讲解**

一次提交里：

1. 追加 assistant 条目（`appendAssistant` 同时记账 `pi.usage`）。
2. 每个工具调用创建一个由生成任务拥有的 `pi.tool` 任务。不在本次请求工具清单里的调用标记 `unoffered`，它的任务直接以 `tool_unavailable` 结束。
3. 串行轮次（设置是 `sequential`，或者任一被调用的工具声明 `executionMode: "sequential"`）只创建第一个任务，其余调用 ID 放进 `pending`。这和 M2 §4.8 的"一票否决"规则一样。
4. 写 `pi.live.tools` 槽位，`turns` 加 1。
5. 提交 `waiting on=tools policy=allSettled`。

按 D4 规则 1，生成任务的调用到此结束，不占用调用。工具任务并行运行。M2 里 `executeToolCalls` 在同一个 `await` 里等全部工具；这里等待是持久的：进程重启后生成任务还在 `waiting`，工具任务各自恢复。

**往哪走**：§4.6。

### 4.6 tools 阶段和 finishToolRound()

源码：[`packages/durable/src/harness/generation.ts:339-362`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L339-L362)

```ts
		tools: async (task, runtime, context) => {
			if (cutOffTooOften(task, runtime)) return endCutOff(runtime, task, context);
			const { assistant, tools, pending } = task.state.checkpoint;
			const [next, ...rest] = pending;
			if (next === undefined) return finishToolRound(runtime, assistant, tools, context);
			// Sequential round: start the next call and wait for it.
			await runtime.commit(async (tx): Promise<Next> => {
				const live = await tx.doc(LiveDoc, runtime.conversationId);
				const slot = live.tools?.find((slot) => slot.callId === next && slot.taskId === undefined);
				const taskId = await createToolTask(tx, runtime, assistant, next, slot?.unoffered);
				if (slot !== undefined) slot.taskId = taskId;
				const checkpoint: GenerationCheckpoint = {
					phase: "tools",
					assistant,
					tools: [...tools, taskId],
					pending: rest,
				};
				return { status: "waiting", checkpoint, on: [taskId], policy: "allSettled" };
			}, context);
		},
		/**
		 * Ask the `afterRun` hooks for tasks and create them, as the generation's children, in the commit that moves to
		 * `drain`; without any, end the run now.
		 */
```

`pending` 不为空时（串行轮次），每次启动下一个调用并再等它。全部结束后调用 `finishToolRound()`：

源码：[`packages/durable/src/harness/generation.ts:1094-1142`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L1094-L1142)

```ts
	await runtime.commit(async (tx): Promise<Next> => {
		const boundary = await prepareBoundary(tx, conversationId, runtime.settings);
		if (added.length > 0) await addTools(tx, conversationId, added);
		const live = await tx.doc(LiveDoc, conversationId);
		const now = runtime.now();
		// The run's last entry so far: the newest result, or the assistant when no result has an entry.
		let last = results.reduce((newest, id) => (id > newest ? id : newest), assistant);
		if (handoff !== undefined) {
			// The handoff's reset precedes every queued item, so queued writes and selected steers land in its context.
			const message = { role: "user", content: handoff, timestamp: now } as const;
			const entry = await tx.appendEntry(ResetEntry, conversationId, { head: "self", model: [message] });
			boundary.head = entry.id;
			last = entry.id;
		}
		const turns = live.run?.taskId === runtime.taskId ? live.run.turns : 0;
		const held = afterRun ? holdRun(tx, live, runtime, { reason: "terminate", answer: assistant, last }) : undefined;
		if (held !== undefined) return held;
		if (terminate) {
			const { users, first } = await applyBoundary(tx, boundary, "final", now);
			endRun(tx, live, runtime.taskId, { status: "done", answer: assistant });
			if (users.length > 0) await startRun(tx, conversationId, live, { inputs: users, first });
		} else if (maxTurns !== undefined && turns >= maxTurns) {
			// The turn limit ends the run like a failure: `afterFailedRun` decides the queued items, and a continue resumes
			// it on the round's results. The table was read for the boundary already, before any write.
			const policy = runtime.settings.afterFailedRun;
			const failure = { policy, ...(policy === "continue" ? { boundary } : {}) };
			await endFailedRun(tx, runtime, live, failure, {
				status: "unanswered",
				reason: "turn_limit",
				detail: turns === 1 ? "1 turn" : `${turns} turns`,
			});
		} else {
			const { users, first, reset } = await applyBoundary(tx, boundary, "postTools", now);
			if (reset) {
				// The queued reset cut the run's context before an answer.
				endRun(tx, live, runtime.taskId, { status: "unanswered", reason: "reset" });
				if (users.length > 0) {
					await startRun(tx, conversationId, live, { inputs: users, first }, { keepFailure: true });
				}
			} else {
				delete live.tools;
				delete live.nestedTools;
				if (live.run?.taskId === runtime.taskId) live.run.inputs.push(...users);
				handOver(live, runtime.taskId, await createGeneration(tx, conversationId));
			}
		}
		return { status: "terminal", outcome: { status: "completed", result: { entryId: assistant } } };
	}, context);
}
```

**讲解**

`finishToolRound` 先在提交外读出每个工具任务的结果（`runtime.outcomes`）、运行 `afterTools` hook，再在一次提交里决定 run 怎么继续：

| 条件 | 结果 | 相当于 M2 的 |
|---|---|---|
| 每个结果的 `control.terminate` 都为 true | 跑 `final` 边界，结束 run | `shouldTerminateToolBatch`（M2 §4.8） |
| `turns >= maxTurns` | run 以 `turn_limit` 结束 | — |
| `postTools` 边界放置了一个 reset | run 以 `reset` 结束 | — |
| 其他 | `postTools` 边界放置 steer 消息，创建新生成任务，`handOver` 交出 run | 内层 `while` 的下一轮，`getSteeringMessages`（M2 §4.15） |

最后本任务以 `completed` 结束。下一轮由一个**新的**生成任务负责，所以每个任务的检查点只描述一轮，不会无限增长。§2.1 里任务 9 结束、任务 14 开始，就是这里的 `handOver`。

**往哪走**：§4.7。

### 4.7 answer()、finish/drain 和 abort

`classify` 收到最终回答时调用 `answer()`（`packages/durable/src/harness/generation.ts:858`）。它先运行 `onYield` hook（可以要求继续，相当于 M2 的 `finishTurn` 返回 `continue`），然后在一次提交里：

- 追加 assistant 条目；
- 有 `onYield` 继续：追加一条 `pi.user`，交给新生成任务；
- 有 `afterRun` hook：把输入 settle 成 `done`，进入 `finish` 阶段（`holdRun`）；
- 否则 `endRun()` 把输入 settle 成 `done` 并删掉 `run`，再跑 `final` 边界，排队的 follow-up 会启动下一个 run。

`finish` 阶段询问 `afterRun` hook 要创建哪些任务，作为子任务创建后进入 `drain` 等它们；`drain` 结束 run：

源码：[`packages/durable/src/harness/generation.ts:363-389`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L363-L389)

```ts
		finish: async (task, runtime, context) => {
			const { run } = task.state.checkpoint;
			if (cutOffTooOften(task, runtime)) return endAnsweredCutOff(runtime, task, run.answer, context);
			const prepared: PreparedTask[] = [];
			await runtime.hooks.each("afterRun", async (hook) => {
				const asked = await hook(run, runtime, context);
				if (asked !== undefined) prepareRunEndTasks(runtime, asked.tasks, prepared);
			});
			if (prepared.length === 0) return endAnsweredRun(runtime, run.answer, context);
			await runtime.commit(async (tx): Promise<Next> => {
				const tasks: TaskId[] = [];
				for (const { task, input } of prepared) {
					tasks.push(await tx.createTask(task, input, { ownership: { kind: "task", taskId: runtime.taskId } }));
				}
				const live = await tx.doc(LiveDoc, runtime.conversationId);
				if (live.run?.taskId === runtime.taskId && live.run.afterRun !== undefined) live.run.afterRun.tasks = tasks;
				const checkpoint = { phase: "drain", answer: run.answer, tasks } as const;
				return { status: "waiting", checkpoint, on: tasks, policy: "allSettled" };
			}, context);
		},
		/** The after-run tasks are terminal, whatever their outcomes: end the run. */
		drain: async (task, runtime, context) => {
			const { answer } = task.state.checkpoint;
			if (cutOffTooOften(task, runtime)) return endAnsweredCutOff(runtime, task, answer, context);
			return endAnsweredRun(runtime, answer, context);
		},
	},
```

abort 处理函数：

源码：[`packages/durable/src/harness/generation.ts:390-416`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/generation.ts#L390-L416)

```ts
	abort: async (task, runtime, context) => {
		const checkpoint = task.state.checkpoint;
		const polled = deferredOf(checkpoint);
		if (polled !== undefined) await cancelDeferred(runtime, polled, context);
		const conversationId = runtime.conversationId;
		// Runs after the round's tool tasks are terminal; calls never started get `aborted` results (spec §8.5), unoffered
		// ones `tool_unavailable`.
		const unstarted =
			checkpoint.phase === "tools"
				? await readCalls(runtime, checkpoint.assistant, checkpoint.pending, context)
				: [];
		await runtime.commit(async (tx) => {
			const live = await tx.doc(LiveDoc, conversationId);
			await convertPartial(tx, live, conversationId);
			for (const call of unstarted) {
				// A call its request did not offer could not have run either way.
				const unoffered = live.tools?.find((slot) => slot.callId === call.id)?.unoffered === true;
				const result = unoffered
					? harnessError("tool_unavailable", `Tool ${call.name} is not available`)
					: harnessError("aborted", `Tool ${call.name} was aborted`);
				await appendToolResult(tx, conversationId, call, result, { timestamp: runtime.now() });
			}
			// In `finish` and `drain` the run answered already: it ends without a failure, its inputs stay `done`.
			endRun(tx, live, runtime.taskId, { status: "unanswered", reason: "aborted" });
			return { status: "terminal", outcome: { status: "aborted" } };
		}, context);
	},
```

**讲解**

abort 处理函数在工具任务都终止之后才运行（D4 §4.10 自底向上）。它取消 deferred 回复、把 partial 转成 aborted 条目、为串行轮次里没开始的调用追加 `aborted` 结果，然后以 `unanswered`/`aborted` 结束 run。transcript 因此总是完整的：每个工具调用都有一个结果条目，或者由上下文推导合成。

**往哪走**：§4.8。

### 4.8 ToolTask 的 call 阶段：先写意图，再执行

源码：[`packages/durable/src/harness/tool.ts:135-199`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/tool.ts#L135-L199)

```ts
export const ToolTask = defineTask<ToolTaskInput, ToolTaskCheckpoint, ToolTaskResult, ToolHooks>({
	name: "pi.tool",
	version: 3,
	initial: () => ({ phase: "call" }),
	// Version 1 had only model-issued calls, without `kind`; version 3 added `unoffered`, absent in version 2 inputs.
	migrate: (input, checkpoint, fromVersion) => ({
		input:
			fromVersion === 1
				? { kind: "model", ...(input as { assistant: EntryId; callId: string }) }
				: (input as ToolTaskInput),
		checkpoint: checkpoint as ToolTaskCheckpoint,
	}),
	phases: {
		call: async (task, runtime, context) => {
			const call = await readCall(runtime, task.input, context);
			if (cutOffTooOften(task, runtime)) return endCutOff(runtime, task, call, context);
			const tool = isUnoffered(task.input) ? undefined : await resolveTool(runtime, task.input, call.name, context);
			if (tool === undefined) {
				const error = harnessError("tool_unavailable", `Tool ${call.name} is not available`);
				return reject(runtime, task.input, call, undefined, error, context);
			}
			const prepared = prepare(tool, call.arguments as JsonObject);
			const checked = "error" in prepared ? prepared : validate(tool, call, prepared.args);
			if ("error" in checked) return reject(runtime, task.input, call, tool, invalid(checked.error), context);
			let args = checked.args;
			let block: string | undefined;
			await runtime.hooks.each("beforeTool", async (hook) => {
				if (block !== undefined) return;
				try {
					const decision = await hook({ ...call, arguments: args }, runtime, context);
					if (decision?.block !== undefined) block = decision.block;
					else if (decision?.arguments !== undefined) args = decision.arguments;
				} catch (error) {
					if (runtime.signal.aborted) throw error;
					block = errorMessage(error);
				}
			});
			if (block !== undefined) {
				const blocked = harnessError("blocked", `Tool call blocked: ${block}`);
				return reject(runtime, task.input, call, tool, blocked, context);
			}
			const validated = validate(tool, call, args);
			if ("error" in validated) return reject(runtime, task.input, call, tool, invalid(validated.error), context);
			// Strict JSON: repair may set optional properties to undefined, which the slot and checkpoint cannot hold.
			const final = copyJson(validated.args, { omitUndefinedProperties: true }) as JsonObject;
			// The slot's status and arguments are presentation, stored like progress (`PROGRESS_COMMIT`); the intent is
			// durable before the tool runs. Parallel calls of one round then share no immediate write to `pi.live`.
			await runtime.commit(
				async (tx) => {
					const slot = toolSlot(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId);
					if (slot !== undefined) slot.status = "running";
					// A nested slot shows the arguments the call runs with, which repair, hooks, and coercion may have changed.
					// Assigned whole: an equal value records no change, and a rerun corrects what an earlier attempt showed.
					if (slot !== undefined && "parentTaskId" in slot) slot.arguments = final;
					return undefined;
				},
				context,
				PROGRESS_COMMIT,
			);
			await runtime.commit(() => {
				const intent = { phase: "execute", arguments: final, replay: tool.replay ?? "unsafe" } as const;
				return { status: "running", checkpoint: intent };
			}, context);
			await run(runtime, task.input, call, tool, final, context);
		},
```

**讲解**

整个调用都在 `call` 一个阶段里完成，按顺序：

1. `readCall` 从 assistant 条目读出调用（嵌套调用从输入里读）。
2. `resolveTool` 在本阶段 agent 的工具里找工具，找不到就以 `tool_unavailable` 结束。
3. `prepareArguments` 修复参数，`validate` 校验（→ M1 的 `validateToolArguments`）。
4. `beforeTool` hook：第一个 `block` 生效，否则可以替换参数；hook 抛异常也算 block。改完参数再校验一次。
5. 一次 `PROGRESS_COMMIT` 把槽位改成 `running`。
6. **意图提交**：`{ phase: "execute", arguments: final, replay }`，`replay` 默认 `"unsafe"`。这是普通（立即持久化的）提交。
7. `run()` 执行工具，然后 `settle()`。

第 6 步是 D4 §6.1 的 effect sandwich。它之前的所有步骤都没有外部副作用，崩溃后整段重跑没问题。它之后，工具可能已经执行了任意副作用，所以恢复时不能简单地重跑 `call`。注意第 6 步后同一个调用没有新的阶段边界：`run()` 在 `call` 阶段函数里直接执行，执行完由 `settle()` 提交终止结果。`execute` 阶段只在恢复时才会被调度器调用。

`run()`（`packages/durable/src/harness/tool.ts:414`）构造工具看到的 `ToolExecutionApi`：`output()` 和 `details()` 通过节流器提交进度到槽位，`commit()` 让工具在任务的事务里写文档，`createTask()` 创建子任务，`executeTool()` 发起嵌套调用（§4.10）。工具抛异常时结果是 `isError: true` 加一个 `tool_error` 诊断，任务以 `failed` 结束。`failed` 是取消意图（D4 §4.11），所以这个调用拥有的会话和任务会被级联中止。

`settle()` 的核心：

源码：[`packages/durable/src/harness/tool.ts:804-830`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/tool.ts#L804-L830)

```ts
async function settle(
	runtime: Runtime,
	input: ToolTaskInput,
	call: ToolHookCall,
	ending: Ending,
	result: ToolExecutionResult,
	context: Context,
	durationMs?: number,
): Promise<void> {
	// A crash in between leaves the call in `execute`: a safe rerun reattaches to the calls, an interruption ends here.
	// Every live nested call the task owns, also those an earlier attempt made and this one never called again.
	const nestedIds = (await runtime.ownedTasks(context))
		.filter((record) => record.kind === ToolTask.definition.name && (record.input as ToolTaskInput).kind === "nested")
		.map((record) => record.id);
	// Read before the settling commit: no admission starts after `ended`, and those underway were awaited.
	const madeNested = (await runtime.memo(MADE_NESTED_CALLS, context)) === true;
	// Mark them all before waiting for any: one may run until a sibling is cancelled.
	await Promise.all(nestedIds.map((id) => runtime.abortOwned(id, context)));
	await runtime.commit(async (tx) => {
		const conversationId = runtime.conversationId;
		const live = await tx.doc(LiveDoc, conversationId);
		const slot = toolSlot(live, runtime.taskId);
		let settled: ToolTaskResult;
		if (input.kind === "nested") {
			const nested = nestedResult(runtime.taskId, result, durationMs);
			if (nested.usage !== undefined) await recordUsage(tx, conversationId, "tools", call.name, nested.usage);
			// The caller is never terminal before its owned nested calls, so its document is open; another task in it is a
```

**讲解**

结束前先中止这个调用留下的所有嵌套调用，然后一次提交：模型发起的调用追加 `pi.tool-result` 条目（`appendToolResult`，诊断渲染成 `<harness>` 块附在内容末尾，所以存下的消息就是模型看到的）、槽位标为 `done`、任务以带 `entryId` 的结果终止。`control`（`terminate`、`addTools`、`handoff`）只在 `completed` 时保留，供 `finishToolRound` 读。

**往哪走**：§4.9。

### 4.9 execute 阶段（恢复）和 abort

源码：[`packages/durable/src/harness/tool.ts:200-247`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/tool.ts#L200-L247)

```ts
		/** Recovery after intent: rerun only when the stored and the current policy both say `safe`. */
		execute: async (task, runtime, context) => {
			const { arguments: args, replay } = task.state.checkpoint;
			const call = await readCall(runtime, task.input, context);
			// Also before an interruption result: its `afterTool` hooks may be what cut the task off.
			if (cutOffTooOften(task, runtime)) return endCutOff(runtime, task, call, context);
			const tool = await resolveTool(runtime, task.input, call.name, context);
			if (replay === "safe" && tool?.replay === "safe") {
				// The rerun reports from scratch: clear what the interrupted attempt published, and show the slot running with
				// the arguments it runs with again, as the deferred commit that showed them may have been lost with the crash.
				await runtime.commit(async (tx) => {
					const slot = toolSlot(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId);
					if (slot !== undefined) {
						clearProgress(slot);
						slot.status = "running";
						if ("parentTaskId" in slot) slot.arguments = args;
					}
					return undefined;
				}, context);
				return run(runtime, task.input, call, tool, args, context);
			}
			const message = `Tool ${call.name} was interrupted and may have partially run`;
			// `failed` records cancellation intent, so the call's owned conversations, left unsupervised, are aborted.
			const ending = { status: "failed", message } as const;
			// Not cut off too often (checked above), so the hooks run.
			await settleFromSlot(runtime, task.input, call, { tool }, ending, "interrupted", message, context);
		},
	},
	abort: async (task, runtime, context) => {
		const call = await readCall(runtime, task.input, context);
		// Abandoned after a restart (`TaskOptions.abandonOnRestart`): say whether the call may have run. An unoffered call
		// could not run in any case, and its result says so.
		const [code, message] = isUnoffered(task.input)
			? ["tool_unavailable", `Tool ${call.name} is not available`]
			: task.abortReason !== "restart"
				? ["aborted", `Tool ${call.name} was aborted`]
				: task.state.checkpoint.phase === "execute"
					? ["interrupted", `Tool ${call.name} was interrupted by a restart and may have partially run`]
					: ["abandoned", `Tool ${call.name} was not started: its caller ended with a restart`];
		// The scheduler runs an abort handler only while its restarts are below the limit, and faults the task without a
		// result when the last attempt is cut off. That attempt skips the hooks, which may be what cuts it off.
		const hooks =
			(task.restarts ?? 0) < runtime.settings.restartLimit - 1
				? { tool: await toolForLimits(runtime, task.input, call.name, context) }
				: undefined;
		await settleFromSlot(runtime, task.input, call, hooks, { status: "aborted" }, code, message, context);
	},
});
```

**讲解**

调度器运行 `execute` 阶段，只可能是因为意图提交之后、结果提交之前进程死了。

- 存下的 `replay` 和当前工具定义的 `replay` **都是** `"safe"`：清掉上次尝试发布的进度，用存下的参数重新 `run()`。不重跑 `beforeTool`：参数已经是 hook 处理过的。
- 否则：不执行工具，用槽位里已经提交的部分输出、details 和诊断拼一个错误结果（`fromSlot`，`packages/durable/src/harness/tool.ts:999`），诊断码 `interrupted`，文本 "Tool X was interrupted and may have partially run"，任务以 `failed` 结束。

两个策略必须都是 safe：当前定义可以否决存下的 safe（新版本的工具认为自己不能重跑了），但不能把存下的 unsafe 升级成 safe（spec §7.3）。

abort 处理函数区分四种情况：没提供的工具（`tool_unavailable`）、被请求中止（`aborted`）、调用方因重启被放弃且已经开始执行（`interrupted`）、调用方因重启被放弃且还没开始（`abandoned`）。后两种来自 `abandonOnRestart`（D4 §4.2）：不能重跑的调用方的嵌套调用，在重启后没人等它们了。

§7.1 用实验对比两种策略。

**往哪走**：§4.10。

### 4.10 嵌套调用：executeTool() 和 key

源码：[`packages/durable/src/harness/tool.ts:519-547`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/tool.ts#L519-L547)

```ts
		executeTool: async (name, toolArgs, callContext, options = {}) => {
			assertLive();
			const key = options.key ?? String(++sequence);
			if (options.key !== undefined) checkKey(options.key);
			// The memo first, once per attempt; a failed one is tried again by the next call.
			madeNestedMemo ??= runtime.memo(MADE_NESTED_CALLS, true, callContext).catch((error: unknown) => {
				madeNestedMemo = undefined;
				throw error;
			});
			// Registered now, so cleanup waits for it.
			const admission = madeNestedMemo.then(() =>
				startNestedCall(
					runtime,
					call.id,
					name,
					toolArgs,
					{ key, progress: options.progress, abandonOnRestart: !resumes },
					callContext,
				),
			);
			admissions.push(admission);
			const id = await admission;
			const settled = await runtime.waitForTask(id, callContext);
			const stored = (await runtime.snapshot(NestedCallDoc, runtime.taskId, key, callContext))?.result;
			// A copy the tool may change; a nested call the scheduler faulted or orphaned stored none.
			return stored === undefined
				? fallbackResult(id, name, settled.state.outcome)
				: (copyJson(stored as unknown as JsonValue) as unknown as NestedToolExecutionResult);
		},
```

源码：[`packages/durable/src/harness/tool.ts:896-940`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/tool.ts#L896-L940)

```ts
async function startNestedCall(
	runtime: Runtime,
	parentCallId: string,
	name: string,
	args: JsonObject,
	options: { readonly key: string; readonly progress: boolean | undefined; readonly abandonOnRestart: boolean },
	context: Context,
): Promise<TaskId<ToolTaskResult>> {
	const key = options.key;
	const call: ToolCall = {
		type: "toolCall",
		id: `${parentCallId}/${key}`,
		name,
		arguments: copyJson(args, { omitUndefinedProperties: true }) as JsonObject,
	};
	let id: TaskId<ToolTaskResult> | undefined;
	let replayed = false;
	await runtime.commit(async (tx) => {
		// Opened, or created empty for a key not made yet.
		const state = await tx.doc(NestedCallDoc, runtime.taskId, key, null);
		const existing = state.taskId;
		if (existing !== undefined) {
			const input = (await tx.task(existing))?.input as ToolTaskInput | undefined;
			const same =
				input?.kind === "nested" &&
				input.call.name === name &&
				jsonEqual(input.call.arguments as JsonValue, call.arguments as JsonValue);
			if (!same) throw new Error(`Nested call ${call.id} was already made with another tool or other arguments`);
			id = existing as TaskId<ToolTaskResult>;
			replayed = state.result !== undefined;
			return undefined;
		}
		const input: ToolTaskInput = {
			kind: "nested",
			parent: runtime.taskId,
			parentCallId,
			key,
			call,
			...(options.progress === false ? { progress: false } : {}),
		};
		const created = await tx.createTask(ToolTask, input, {
			ownership: { kind: "task", taskId: runtime.taskId },
			abandonOnRestart: options.abandonOnRestart,
		});
		state.taskId = created;
```

**讲解**

一个工具（例如 code mode 的脚本，D6）调用另一个工具时：

1. `key` 默认是调用顺序 `"1"`、`"2"`、…；也可以显式给一个 key。显式 key 不能是正整数，不能含 `/`（`checkKey`，`packages/durable/src/harness/tool.ts:1119`），所以两种 key 不会撞。嵌套调用的 ID 是 `<父调用 ID>/<key>`。
2. 第一次嵌套调用前写一个 memo `pi.tool.madeNestedCalls`，结束时据此决定要不要清理嵌套槽位。
3. `startNestedCall` 在一次提交里打开调用方任务作用域下的 `NestedCallDoc[key]`。已经有任务时，检查工具名和参数是否相同：相同就返回已有的任务（`replayed` 表示它已经有结果，发出 `pi.durable.replay` 遥测事件）；不同就抛错。没有任务时创建一个由调用方拥有的 `pi.tool` 任务和嵌套槽位。
4. 等嵌套任务结束，从 `NestedCallDoc` 读出结果返回给调用方。

所以一个 `replay: "safe"` 的工具重跑时，只要按同样的顺序（或同样的 key）发起嵌套调用，就能拿回之前的调用：已完成的直接拿结果，还在跑的继续等，不会执行第二次。之前被崩溃打断、还没有结果的嵌套调用，按它自己的 replay 策略恢复。调用方不是 replay-safe 时，它的嵌套任务带 `abandonOnRestart`，重启后被中止（spec §7.3）。

`21-subagent-foreground.ts` 是这个机制的另一种用法：一个 `replay: "safe"` 的工具创建一个由自己拥有的子会话，用 `requestId: "subagent:<taskId>"` 向它提交输入（§4.11 的去重），重跑时通过 `scanConversations({ ownerTaskId })` 找回子会话，通过同一个 `requestId` 找回同一个提交：

```bash
cd $PI/packages/durable
node --conditions=source --experimental-strip-types test/examples/21-subagent-foreground.ts
```

```
tool subagent({"task":"Name three prime numbers."})
  assistant: 2, 3, and 5.
assistant: The subagent says: 2, 3, and 5.
```

缩进的那一行来自子会话。子会话由工具调用的任务拥有，中止这个工具调用会级联中止子会话里的工作（D4 §4.11）。

**往哪走**：§4.11。

### 4.11 inbox 边界：applyBoundary()

源码：[`packages/durable/src/harness/inbox.ts:77-120`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/inbox.ts#L77-L120)

```ts
export async function applyBoundary(
	tx: Tx,
	boundary: Boundary,
	at: "postTools" | "final",
	now: number,
): Promise<BoundaryResult> {
	const { conversationId, inbox } = boundary;
	const items = inbox.items;
	const reset = hasQueuedReset(items);
	const final = at === "final" || reset;
	const pick = (mode: "steer" | "followUp", queueMode: QueueMode): number[] => {
		const indexes = items.flatMap((item, index) => (item.mode === mode ? [index] : []));
		return queueMode === "all" ? indexes : indexes.slice(0, 1);
	};
	const writes = items.flatMap((item, index) => (item.mode === "write" ? [index] : []));
	const users = [...pick("steer", boundary.steeringMode), ...(final ? pick("followUp", boundary.followUpMode) : [])];
	users.sort((a, b) => a - b);

	// `appendEntry()` copies the drafts' values; the items are removed only afterwards.
	let first: EntryId | undefined;
	for (const index of writes) {
		const item = items[index] as Draft<Extract<InboxItem, { mode: "write" }>>;
		const draft = item.entry as unknown as EntryDraft;
		if (isStale(boundary, draft)) {
			tx.settleSubmission(item.id, { status: "unanswered", reason: "stale" });
			continue;
		}
		const entry = await tx.appendEntry(conversationId, draft);
		if (draft.head !== undefined) boundary.head = draft.head === "self" ? entry.id : draft.head;
		tx.placeSubmission(item.id, entry.id);
		first ??= entry.id;
	}
	const placed: SubmissionId[] = [];
	for (const index of users) {
		const item = items[index] as Draft<Extract<InboxItem, { mode: "steer" | "followUp" }>>;
		const entry = await appendInput(tx, conversationId, item.content as UserInput, item.entry as InputEntry, now);
		tx.placeSubmission(item.id, entry.id);
		placed.push(item.id);
		first ??= entry.id;
	}
	const removed = [...writes, ...users].sort((a, b) => b - a);
	for (const index of removed) items.splice(index, 1);
	return { users: placed, first, reset };
}
```

**讲解**

边界是 durable 版本的"轮询 steering / follow-up 队列"（M2 §4.15、§4.16）。有两种：

- `postTools`：工具轮结束、下一次请求之前（`finishToolRound`）。放置所有 write、第一个（或全部，取决于 `steeringMode`）steer。
- `final`：run 结束时（`answer`、`endAnsweredRun`、`endFailedRun`），以及空闲会话收到提交时。额外放置第一个（或全部）follow-up。

放置的意思是：追加条目、把提交改成 `placed`、从 inbox 删掉。被放置的用户输入加入当前 run 的 `inputs`，或启动下一个 run。和 M2 一样，steer 不会打断正在执行的工具，在下一次请求之前进入 transcript。区别是排队本身是持久的：进程重启后排队的消息还在 `pi.inbox` 里。

`Conversation.abort()` 和级联中止通过 `withdrawQueuedInputs()`（`packages/durable/src/harness/inbox.ts:170`）撤回排队的输入，它们以 `unanswered`/`aborted` 结束；排队的 write 保留。

**往哪走**：§4.12。

### 4.12 CompactionTask

源码：[`packages/durable/src/harness/compaction.ts:114-158`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/compaction.ts#L114-L158)

```ts
export const CompactionTask = defineTask<CompactionInput, CompactionCheckpoint, CompactionResult, CompactionHooks>({
	name: "pi.compaction",
	version: 1,
	initial: () => ({ phase: "select" }),
	phases: {
		select: async (task, runtime, context) => {
			if (cutOffTooOften(task, runtime)) return failCutOff(runtime, task, context);
			const { conversationId } = runtime;
			const agent = await runtime.agent(context);
			const settings = runtime.settings;
			const ref = agent.model;
			const model = ref === undefined ? undefined : runtime.models.getModel(ref.provider, ref.modelId);
			if (ref === undefined || model === undefined) return failNoModel(runtime, ref, context);
			const policy = compactionPolicy(settings, ref);
			const view = await runtime.context(conversationId, context);
			const cut = selectCut(view, policy.keepRecentTokens);
			if (cut === undefined) return complete(runtime, context);
			const firstKept = view.entries[cut]!.id;
			const { reason, instructions } = task.input;
			const compaction = {
				reason,
				entries: view.entries.slice(0, cut),
				messages: summarizedMessages(view, cut),
				firstKept,
				...(instructions === undefined ? {} : { instructions }),
			};
			let decision: { readonly decline: true } | { readonly summary: string } | undefined;
			await runtime.hooks.each("beforeCompact", async (hook) => {
				if (decision === undefined) decision = await hook(compaction, runtime, context);
			});
			if (decision !== undefined && "decline" in decision) return complete(runtime, context);
			if (decision !== undefined) return place(runtime, firstKept, decision.summary, context);
			const request: SummaryRequest = {
				attempt: 1,
				model: ref,
				thinkingLevel: agent.thinkingLevel,
				streamOptions: settings.stream,
				maxTokens: Math.min(
					Math.floor(0.8 * policy.reserveTokens),
					model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY,
				),
				tail: view.entries.reduce((tail, entry) => (entry.id > tail ? entry.id : tail), firstKept),
				firstKept,
			};
			await runtime.commit(() => ({ status: "running", checkpoint: { phase: "summarize", ...request } }), context);
```

**讲解**

压缩任务有三个阶段：`select` 选切点（`selectCut`，和 M5 一样不在工具结果处切）、调用 `beforeCompact` hook（可以拒绝或直接给出摘要），提交 `summarize` 检查点，检查点里存下 `tail` 和 `firstKept`；`summarize` 用 `{ at: tail }` 读同一段上下文调用模型，成功时放置摘要条目，可重试的失败进入 `retry` 睡眠；`retry` 醒来后回到 `summarize`。

它有两种所有者（spec §8.7）：

- 生成任务拥有的（`prepare` 发现超过阻塞阈值，或 `classify` 发现上下文溢出）：生成任务 `waiting` 它，摘要直接追加。
- 会话拥有的（`conversation.compact()`、后台阈值）：摘要通过一个 write 提交放置，所以如果会话忙，它在下一个边界才进入 transcript，不会插到正在进行的工具轮中间。

`summarize` 存下 `tail`，所以崩溃后重跑读到的是同一段不可变的上下文，切点不会因为新条目而移动。

**往哪走**：§4.13。

### 4.13 和 M2 runLoop 的逐段对照

| M2（`packages/agent/src/agent-loop.ts`） | durable | 区别 |
|---|---|---|
| `runLoop` 172，外层 `while` 188 | 一个 run：`pi.live.run`，由一串生成任务接力 | 循环状态在 `pi.live.run` 和任务记录里，不在调用栈上 |
| 内层 `while` 192 | 每轮一个新的 `pi.generation`，`handOver` | 每个检查点只描述一轮 |
| `prepareNextTurn` 194、`declareToolChanges` 220 | `prepare`：`planSystemEntries`、阈值压缩 | 压缩是一个子任务，生成任务持久地等它 |
| `prepareRequest` 228 | `prepare` 从 `runtime.context()` 读已提交的上下文 | 已提交的条目就是唯一的上下文来源，和 M8 用会话文件重建上下文的思路相同 |
| `transformContext` / `convertToLlm` | `beforeRequest` hook；条目自带 `model` 消息 | — |
| `streamAssistantResponse` 251 | `request`：`streamResponse` + 节流提交 partial | 崩溃后不重发，算作失败尝试（`interrupted`） |
| error/aborted 退出 254 | `classify`：重试（`retry` 睡眠）或 `model_error` 结束 | 重试和退避在任务里，跨重启有效 |
| `length` → `failToolCallsFromTruncatedMessage` 277 | `length` 走 `answer()`，调用的结果由上下文推导合成 | — |
| `executeToolCalls` 279/518 | `startToolRound`：每个调用一个 `pi.tool` 子任务，`waiting` | 工具并行执行是因为它们是独立的任务 |
| `beforeToolCall` / `afterToolCall` | `beforeTool` / `afterTool` hook（D6） | `beforeTool` 在意图之前，恢复时不重跑 |
| `finishTurn` 295 | `onYield` hook、`control.terminate` | — |
| 轮询 steering（`getSteeringMessages`） | `postTools` 边界 | 队列是持久的 `pi.inbox` |
| `getFollowUpMessages` 311 | `final` 边界 | — |
| `agent_end` 329 | `endRun()`，提交 settle 成 `done`；`afterRun` → `finish`/`drain` | 结束也是一次提交 |
| 发出 `AgentEvent` | 提交本身；观察者看 `pi.live` 和条目（D6） | 没有事件流，状态即事件 |

---

## 5. 状态机

### 5.1 一个 run 里的生成任务

```
 admitSubmission → startRun (generation.ts:1162)
        ▼
   ┌─────────┐ 超阈值：waiting on [compaction] (236-240)
   │ prepare │◀──────────────────────────────────────┐ 压缩结束
   └────┬────┘ nothing_to_answer / no_model → 结束 run │
        │ (243-260)                                   │
        ▼                                             │
   ┌─────────┐ restarts 存在 → interrupted() (266) ──▶ retry 或结束 run
   │ request │                                        │
   └────┬────┘                                        │
        │ classify (758)                              │
        ├─ deferred → sleeping → poll (770-789) ──────┤
        ├─ 可重试 → sleeping → retry (841) → prepare (318-319)
        ├─ 溢出 → waiting on [compaction] (812-815) ──┘
        ├─ 其他错误 → terminal failed，run 结束 (843-848)
        ├─ 回答 → answer() (798) → terminal completed，endRun
        │         或 holdRun → finish → drain → endAnsweredRun
        └─ 工具调用 → startToolRound (795) → waiting on [pi.tool…] (1054)
                                 │ 全部终止
                                 ▼
                           tools → finishToolRound (343)
                                 └─ handOver 给新的 pi.generation (1137)，本任务 completed
 任何阶段遇到中止标记：等工具任务终止后 abort (390-416)，run 以 aborted 结束
```

### 5.2 一个工具任务

```
   ┌──────┐ 工具不存在 / 参数无效 / beforeTool block ──▶ reject() → terminal completed，isError 结果
   │ call │                                                (tool.ts:150-177)
   └──┬───┘
      │ 意图提交 { phase: "execute", arguments, replay } (194-197)
      ▼
   run() 执行工具 (198) ──正常返回──▶ settle() → terminal completed (865)
      │                └─抛异常───▶ settle() → terminal failed (863)
      │ 进程死掉
      ▼
   ┌─────────┐  open：pending，restarts+1
   │ execute │
   └──┬──────┘
      ├─ 存下的和当前的 replay 都是 safe → 再次 run() (207-220)
      └─ 否则 → settleFromSlot(interrupted) → terminal failed (221-225)
 restarts ≥ restartLimit → endCutOff → terminal failed，restart_limit (150、205)
 中止标记 → abort (228-247) → terminal aborted
```

---

## 6. 设计要点

### 6.1 意图在副作用之前，replay 策略在意图里

**做法。** 工具任务在执行前提交 `{ phase: "execute", arguments, replay }`。恢复只看这个检查点：存下的和当前的策略都是 safe 才重跑，否则返回 `interrupted` 结果。

**理由。** 存储不能知道工具执行到了哪一步。把"可能已经执行"变成一个持久的状态，恢复逻辑就只需要回答"重跑安全吗"。默认是不安全的，因为大多数工具（例如写文件、执行命令）重跑可能产生不同的结果；模型看到 `interrupted` 结果和已经输出的部分内容，可以自己决定下一步。只读或幂等的工具可以声明 `safe`。

**代码位置。** `packages/durable/src/harness/tool.ts:194-197`、`packages/durable/src/harness/tool.ts:207`。

### 6.2 被打断的请求是一次失败的尝试，不是重发

**做法。** `request` 和 `poll` 阶段在 `restarts` 存在时调用 `interrupted()`，把 partial 转成 aborted 条目，按重试策略走 `retry`。

**理由。** 模型请求可能已经计费，partial 里可能有用户已经看到的内容。直接重发会让同一个回答出现两次、费用翻倍。算作失败尝试，重试次数和退避就自然限制了"请求一发就崩"的情况；partial 留在 transcript 里，`pi.usage` 也记下了它的花费。

**代码位置。** `packages/durable/src/harness/generation.ts:266`、`packages/durable/src/harness/generation.ts:501`。

### 6.3 每轮一个任务，run 控制权在文档里交接

**做法。** 生成任务只负责一次请求和它引出的工具轮，结束时创建下一个生成任务并把 `pi.live.run.taskId` 交给它。

**理由。** 检查点保持小而且有界。每个任务的生命周期短，崩溃时受影响的只有一轮。run 的连续性由 `pi.live.run` 表达，它和 transcript 在同一次提交里更新，所以任何时刻 `run` 指向的任务都是真正负责 run 的那个。

**代码位置。** `packages/durable/src/harness/generation.ts:1185`。

### 6.4 用户输入的每一步都是一次提交

**做法。** 提交记录有 `queued` → `placed` → `done`/`unanswered` 四个状态，每次转换都和对应的条目、`pi.inbox`、`pi.live.run` 变化在同一次提交里。`requestId` 在同一次提交里先查后写。

**理由。** 宿主拿到的 `Submission` 句柄可以在任何进程里 `wait()`，结果总是确定的。网络重试、页面刷新、进程重启后用同一个 `requestId` 再提交，拿到的是同一个提交，transcript 里只有一条用户消息。`21-subagent-foreground.ts` 正是用这一点让子代理工具可以安全重跑。

**代码位置。** `packages/durable/src/harness/submissions.ts:232-240`、`packages/durable/src/harness/live.ts:290`。

### 6.5 嵌套调用按 key 重新挂接

**做法。** 嵌套调用的身份是 `(调用方任务, key)`，存在调用方任务作用域的 `NestedCallDoc` 里；同一 key 必须是同一个工具和同样的参数。

**理由。** replay-safe 的调用方重跑时，只要确定性地发起同样的调用，就能拿回之前的结果，不会重复执行。这是 D6 的 code mode 能在重启后继续一个脚本的基础：脚本被确定性地重放，嵌套调用按 key `c<VM call id>` 挂接回原来的任务。

**代码位置。** `packages/durable/src/harness/tool.ts:896`、`packages/durable/src/harness/tool.ts:1119`。

---

## 7. 实验

### 7.1 工具执行中杀掉进程：replay never 和 safe

**问题。** [`lab/m11b-kill.ts`](../lab/m11b-kill.ts) 注册一个 `work` 工具，它每次执行时往一个文件里追加一行（模拟外部副作用），输出 `step 1 done`，等 1.5 秒，然后 `SIGKILL` 自己。`run` 模式提交一条消息，假模型返回一个 `work` 调用；`reopen` 模式用同一个 SQLite 文件重新打开，假模型返回 `ok`。分别用默认策略（`unsafe`）和 `replay: "safe"` 运行，重新打开后会看到什么？

**预测。**

- 两种策略下，重新打开时工具任务都停在 `execute`（意图已提交），`restarts=1`；生成任务停在 `waiting`，`restarts=0`（它在等，没有调用在跑，不算被打断）。
- `unsafe`：工具不再执行，副作用文件只有 1 行。结果条目 `isError=true`，内容是已经提交的部分输出 `step 1 done` 加上 `interrupted` 诊断。等 1.5 秒是为了让节流的进度提交落盘（默认 `persistWithinMs` 1 秒）。模型拿到这个结果后回答 `ok`。
- `safe`：工具在新进程里再执行一次，副作用文件有 2 行。结果条目是正常的 `all steps done`，`isError=false`。

**运行。**

```bash
mkdir -p /tmp/pi-lab/m11b-kill-2
for r in unsafe safe; do
  echo "=== $r run"; node --conditions=source --experimental-strip-types m11b-kill.ts run /tmp/pi-lab/m11b-kill-2/$r.sqlite $r; echo "exit=$?"
  echo "=== $r reopen"; node --conditions=source --experimental-strip-types m11b-kill.ts reopen /tmp/pi-lab/m11b-kill-2/$r.sqlite $r; echo "exit=$?"
done
```

```
=== unsafe run
[work] 第 1 次执行（run 进程）
[work] SIGKILL 自己
exit=137
=== unsafe reopen
[open 之后] task 12 pi.tool status=pending restarts=1 {"phase":"execute","arguments":{},"replay":"unsafe"} (pending)
[open 之后] task 9 pi.generation status=waiting restarts=0 {"phase":"tools","assistant":11,"tools":[12],"pending":[]} (waiting)
[transcript] 7 pi.user user: "do the work"
[transcript] 10 pi.system system: ""
[transcript] 11 pi.assistant assistant: "toolCall(work)"
[transcript] 13 pi.tool-result toolResult isError=true: "step 1 done\n | <harness>\n[error] Tool work was interrupted and may have partially run\n</harness>"
[transcript] 15 pi.assistant assistant: "ok"
[外部副作用] work 一共执行了 1 次：run
exit=0
=== safe run
[work] 第 1 次执行（run 进程）
[work] SIGKILL 自己
exit=137
=== safe reopen
[open 之后] task 12 pi.tool status=pending restarts=1 {"phase":"execute","arguments":{},"replay":"safe"} (pending)
[open 之后] task 9 pi.generation status=waiting restarts=0 {"phase":"tools","assistant":11,"tools":[12],"pending":[]} (waiting)
[work] 第 2 次执行（reopen 进程）
[transcript] 7 pi.user user: "do the work"
[transcript] 10 pi.system system: ""
[transcript] 11 pi.assistant assistant: "toolCall(work)"
[transcript] 13 pi.tool-result toolResult isError=false: "all steps done"
[transcript] 15 pi.assistant assistant: "ok"
[外部副作用] work 一共执行了 2 次：run, reopen
exit=0
```

`[transcript]` 行里用 ` | ` 连接一条消息的多个内容块。`pi.system` 的 `content` 为空，因为假模型的会话没有系统提示词段落，它只带工具声明。

**结论。** 三个预测都成立。`unsafe` 时模型看到的错误结果里保留了崩溃前已经提交的输出 `step 1 done`，这来自槽位的进度（`fromSlot`）。`safe` 时副作用执行了两次：`replay: "safe"` 承诺的是"重跑是安全的"，不是"只执行一次"，所以有外部副作用的工具要自己做到幂等（spec §7.3）。

### 7.2 关闭和恢复一个计数任务

**问题。** D4 §2.2 的 `12-recovery.ts` 里，tick 2 的阶段在重新打开后会再跑一次。把它和 §7.1 对比：为什么那里的任务记录是 `pending` 而没有 `restarts`？

**预测。** 那个示例用的是 `close()`，不是崩溃。`join()` 把停下来的任务写回 `pending` 且不计数（D4 §4.10）；§7.1 用 SIGKILL，存储里留下 `running`，open 时才计数。工具任务的 `execute` 阶段看的就是这个计数：如果 §7.1 改成 `close()`，工具任务会以 `restarts` 为空进入 `execute`，规则仍然一样（`execute` 只看 replay），但生成任务的 `request` 阶段只在 `restarts` 存在时才走 `interrupted()`。

**运行。**

```bash
cd $PI/packages/durable
node --conditions=source --experimental-strip-types test/examples/12-recovery.ts
```

```
tick 1
tick 2
closed; saved checkpoint: { status: 'pending', checkpoint: { phase: 'tick', n: 2 } } memos: { 'printed-1': true, 'printed-2': true }
tick 3
tick 4
tick 5
after reopen: { status: 'completed', result: 'counted to 5' }
```

**结论。** 记录里只有 `status` 和 `checkpoint`，没有 `restarts` 字段（脚本只打印了 `state` 和 `memos`，`restarts` 不存在时任务记录里就没有这个键，见 D4 §2.3）。

### 7.3 同一个 requestId 提交两次

**问题。** `m11b-observe.ts dup` 用 `requestId: "req-1"` 连续提交两次同样的输入。两次拿到的提交 ID 相同吗？transcript 里有几条 `pi.user`？

**预测。** 第一次提交时会话空闲，追加 `pi.user` 并启动 run。第二次提交在 `admitSubmission` 开头查到同一个 `requestId`，直接返回第一次的 ID，不写任何东西。所以 ID 相同，只有一条 `pi.user`。

**运行。** 见 §2.2，输出是：

```
第 1 次 submit：id=8
第 2 次 submit：id=8
settled: {"conversationId":1,"requestId":"req-1","type":"input","status":"done","entry":7,"id":8,"answer":15}
条目：7:pi.user, 10:pi.system, 11:pi.assistant, 13:pi.tool-result, 15:pi.assistant
```

**结论。** 预测成立。如果去掉 `requestId`，第二次提交时会话忙（第一个 run 还在跑），它会以 `followUp` 排进 inbox，第一个 run 结束时在 `final` 边界被放置，启动第二个 run。

---

## 8. 自测与衔接

### 8.1 自测

1. 生成任务在 `waiting` 工具任务时进程崩溃，重新打开后生成任务的 `restarts` 是多少？为什么？
2. 一个工具声明了 `replay: "safe"`，执行到一半进程死了。重新部署时新版本把它改成了默认值。恢复时会重跑吗？
3. 会话忙时提交一条 `whenBusy: "steer"` 的输入，它什么时候进入 transcript？

<details>
<summary>答案</summary>

1. 0。`waiting` 的任务没有调用在跑，open 只给 `running` 的任务计数（D4 §4.2）。§7.1 的输出里就是 `restarts=0`。
2. 不会。`execute` 阶段要求存下的和当前的策略都是 `safe`（`packages/durable/src/harness/tool.ts:207`）。当前定义否决了，结果是 `interrupted` 错误。
3. 在当前工具轮结束、下一次请求之前，`finishToolRound` 的 `postTools` 边界放置它（§4.11）。如果当前请求最终没有工具调用，就在 `answer()` 的 `final` 边界放置，`steer` 和 `followUp` 一样启动下一个 run。

</details>

### 8.2 衔接

本篇读的任务都把进度写进 `pi.live` 和条目，但没有讲谁在看它们。D6 从观察者那一侧读：`ConversationViews`（`packages/durable/src/harness/view.ts:146`）怎么把条目和 `pi.live` 等文档变成一个可订阅的视图，扩展的 hook 和工具在哪里接入这些任务，以及 code mode 怎么用本篇 §4.10 的 key 机制在重启后确定性地重放脚本。
