# M11 durable · D4 任务与调度器

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。durable 自带的示例在 `$PI/packages/durable` 目录下运行；本文新增的实验脚本在 `docs/pi-tutorial/lab` 目录下运行，并且已经设置了 `export PI=/path/to/pi`（见 [README §4.1](../README.md#41-获取源码并安装)）。
>
> 本篇是 M11 的第 4 篇。[D1](D1-concepts.md) 讲了 Harness、Conversation、Entry、Document、Task、Submission 这些概念，[D2](D2-chord.md) 讲了 durable 用到的 chord 部分，[D3](D3-storage-transactions.md) 讲了存储和事务。本篇假定你已经知道"一次提交（commit）在 Session line 上原子地写入条目、文档和任务记录"。

---

## 1. 这个模块是什么

### 1.1 职责

M2 的 agent 循环是一个在内存里跑的 `while` 循环。进程一死，循环走到哪一步就丢了。durable 把"循环走到哪一步"变成存储里的一条**任务记录**（`TaskRecord`）：

- 一个任务是一个小状态机。它的定义（`TaskDefinition`）给出若干个**阶段**（phase），每个阶段是一个异步函数。
- 任务当前在哪个阶段、带着什么数据，叫**检查点**（checkpoint），存在任务记录的 `state.checkpoint` 里。
- 阶段函数要么提交一个新检查点（进入下一阶段，或者同一阶段的新进度），要么提交最终结果（outcome）。提交通过 `runtime.commit()`，和这次提交写的条目、文档在同一个事务里。
- **调度器**（`TaskScheduler`）负责挑出可以运行的任务，在内存里为它创建一次**调用**（invocation），依次执行阶段函数，并在每两个阶段之间检查任务有没有取得持久进度。

进程重启后，调度器从存储里读出所有活任务，把上次停在 `running` 的改回 `pending`，再从检查点继续。D5 要讲的内置任务（生成、工具调用、压缩）全部建立在这套机制上。

### 1.2 在调用链中的位置

```
Harness.open(storage, options, context)            harness.ts:586   [D1]
├─ new HarnessImpl()  → new TaskScheduler({...})   harness.ts:237   ← 本篇
└─ harness.openTasks() → TaskScheduler.open()       harness.ts:602   ← 本篇入口
       把 running 改回 pending（restarts + 1），安排一次 reconcile
之后：resume() / submit() / waitForTask() 打开调度
    #kick() → #drain() → #reserve()  一次提交里把可运行的任务改成 running
        → #start() → #run()  依次执行阶段函数
             每个阶段前后 #step()：按规则 1–6 决定继续、交接、结束还是 faulted
             阶段函数里 runtime.commit() → #gated() → #commitState()
        或 #runAbort()  有中止标记时只执行一次 abort 处理函数
    #reconcile()  派生级联中止标记、写出 completing 任务的最终记录
```

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/durable/src/harness/scheduler.ts` | 3158 | `TaskScheduler`：恢复、预留、调用、步骤规则、中止、所有权树、睡眠和唤醒 | 精读调度主线（约 900 行），所有权索引的维护（`#loadChain`、`#relist`、`#sweep` 等）略读 |
| `packages/durable/src/types.ts` | 1445 | `TaskRecord`、`TaskState`、`TaskOutcome`、`TaskDefinition`、`TaskRuntime` 等类型 | 精读任务相关的约 250 行 |
| `packages/durable/src/tasks.ts` | 8 | `defineTask()`：恒等函数，只做类型推导 | 通读 |
| `packages/durable/src/harness/define.ts` | 62 | `defineExtension`、`defineTool`、`hook`、`runEndTask`、`wrapTool` 等定义辅助函数，全部是恒等函数 | 通读 |
| `packages/durable/src/harness/task-graph.ts` | 312 | 任务图视图：把活任务投影成一棵所有权树，供界面观察 | 通读，§4.12 |
| `packages/durable/docs/spec.md` §5 | 第 2067–2846 行 | 任务的规范：定义、effect sandwich、终止、调度与中止、结构化并发、睡眠 | 对照阅读 |

行数用 `wc -l` 统计。

### 1.4 读完能回答的问题

1. 一个阶段函数返回了，但没有提交新检查点，会发生什么？
2. 进程在一个阶段执行到一半时被杀掉，重新打开后这个阶段会再执行一次吗？怎么避免外部副作用重复？
3. `sleepUntil()` 为什么不能在阶段内部 `await setTimeout`？睡眠中的任务重启后还会睡吗？
4. 一个任务每次运行都把进程搞崩，调度器会无限重试吗？
5. 中止一个拥有子任务的任务，子任务和父任务的 abort 处理函数谁先运行？

---

## 2. 先跑一次

### 2.1 最小的任务

durable 自带的示例 `11-tasks.ts` 定义了一个两阶段的支付任务：`prepare` 阶段生成幂等键并写进检查点，`charge` 阶段用这个键扣款并提交结果。

```bash
cd $PI/packages/durable
node --conditions=source --experimental-strip-types test/examples/11-tasks.ts
```

```
payment outcome: { status: 'completed', result: { receipt: 500 } }
```

### 2.2 关闭、重新打开、继续

`12-recovery.ts` 定义了一个从 1 数到 5 的任务。每数一次，用 `sleepUntil()` 睡 50ms 再进入下一次。第一次运行在打印 `tick 2` 之后立即 `close()`，然后用同一个 SQLite 文件重新打开：

```bash
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

看第三行：

- 关闭时任务停在 `{ phase: "tick", n: 2 }`。`tick 2` 已经打印了，但"数到 3"这个检查点还没提交，所以重新打开后 `n: 2` 这个阶段会再跑一次。
- `memos` 里有 `printed-2: true`。重跑的阶段先查这个 memo，发现已经打印过，就不再打印。所以输出里 `tick 2` 只出现一次。
- 状态是 `pending` 而不是 `running`：`close()` 会把它停下的任务写回 `pending`（§4.10）。

### 2.3 一个任务的记录随时间变化

[`lab/m11b-crashloop.ts`](../lab/m11b-crashloop.ts) 的 `record` 模式创建一个任务：`work` 阶段写一个 memo，然后睡 100ms 进入 `finish` 阶段，`finish` 提交结果。脚本在三个时刻打印任务记录：

```bash
cd docs/pi-tutorial/lab
node --conditions=source --experimental-strip-types m11b-crashloop.ts record
```

```
[pending]  {"id":7,"conversationId":1,"kind":"lab.sleeper","version":1,"input":{"label":"demo"},"background":false,"abortRequested":false,"state":{"status":"pending","checkpoint":{"phase":"work"}}}
[sleeping] {"id":7,"conversationId":1,"kind":"lab.sleeper","version":1,"input":{"label":"demo"},"background":false,"abortRequested":false,"state":{"status":"sleeping","checkpoint":{"phase":"finish","note":"after sleep"},"until":1793516371931},"startedAt":1793516371829,"memos":{"effect-done":true}}
[terminal] {"id":7,"conversationId":1,"kind":"lab.sleeper","version":1,"input":{"label":"demo"},"background":false,"abortRequested":false,"state":{"status":"terminal","outcome":{"status":"completed","result":"demo: after sleep"}},"startedAt":1793516371829,"endedAt":1793516371931}
```

时间戳每次运行不同。§3 用这三行解释任务记录的每个字段。

---

## 3. 数据结构

### 3.1 TaskState：任务的 6 种状态

源码：[`packages/durable/src/types.ts:657-702`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L657-L702)

```ts
/** Complete durable execution state of a task. */
export type TaskState<S, R> =
	| {
			/** Eligible for scheduling. */
			readonly status: "pending";
			/** Complete durable state from which execution resumes. */
			readonly checkpoint: S;
			readonly outcome?: never;
	  }
	| {
			/** Reserved by one in-memory task invocation. */
			readonly status: "running";
			/** Complete durable state from which execution resumes. */
			readonly checkpoint: S;
			readonly outcome?: never;
	  }
	| {
			/** Parked without an invocation until every task in `on` is terminal; then resumes at `checkpoint`. */
			readonly status: "waiting";
			readonly checkpoint: S;
			readonly on: readonly TaskId[];
			readonly policy: JoinPolicy;
			readonly outcome?: never;
	  }
	| {
			/**
			 * Parked without an invocation until the Harness clock reaches `until`, in milliseconds on the
			 * `HarnessOptions.now` clock; then resumes at `checkpoint`. An abort mark does not wait for it.
			 */
			readonly status: "sleeping";
			readonly checkpoint: S;
			readonly until: number;
			readonly outcome?: never;
	  }
	| {
			/** Outcome decided; becomes terminal once no ordinary owned work below is live. Runs no more code. */
			readonly status: "completing";
			readonly checkpoint?: never;
			readonly outcome: TaskOutcome<R>;
	  }
	| {
			/** Permanently settled durable result receipt. */
			readonly status: "terminal";
			readonly checkpoint?: never;
			readonly outcome: TaskOutcome<R>;
	  };
```

| 状态 | 有没有调用在跑 | 带什么 | 谁写入 |
|---|---|---|---|
| `pending` | 没有 | `checkpoint` | 创建任务时；open 恢复、close、交接（§4.6）时写回 |
| `running` | 有一个 | `checkpoint` | 预留（§4.3）时写入；阶段函数提交新检查点时保持 |
| `waiting` | 没有 | `checkpoint`、`on`（等哪些任务）、`policy` | 阶段函数提交 |
| `sleeping` | 没有 | `checkpoint`、`until`（Harness 时钟的毫秒数） | 阶段函数提交，或 `runtime.sleepUntil()` |
| `completing` | 没有 | `outcome` | 任务已经有结果，但它拥有的工作还没结束（§4.11） |
| `terminal` | 没有 | `outcome` | 最终状态，记录就是结果的收据 |

注意 `pending` 只能由调度器写入。阶段函数能提交的只有 `NextTaskState`：

源码：[`packages/durable/src/types.ts:224-246`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L224-L246)

```ts
/** Live task record reserved by one invocation. */
export type RunningTask<I, S, R> = TaskRecord<I, S, R> & {
	readonly state: Extract<TaskState<S, R>, { readonly status: "running" }>;
};

/**
 * Next state a task commits for itself: a replacement checkpoint, a wait on tasks, a sleep, or its outcome. A returned `terminal` state is
 * stored as `completing` while ordinary owned work below the task is live (spec §5.5).
 */
export type NextTaskState<S, R> = Extract<
	TaskState<S, R>,
	{ readonly status: "running" | "waiting" | "sleeping" | "terminal" }
>;

/**
 * Runs one checkpoint phase. It must commit a changed checkpoint or a terminal outcome through `runtime.commit()`;
 * returning without durable progress faults the task.
 */
export type PhaseHandler<I, P, S, R, H extends object> = (
	task: RunningTask<I, P, R>,
	runtime: TaskRuntime<I, S, R, H>,
	context: Context,
) => Promise<void>;
```

`PhaseHandler` 的注释写明了合约：每个阶段必须通过 `runtime.commit()` 提交一个**变化了的**检查点或终止结果，否则任务 faulted（§4.5 规则 6）。

### 3.2 TaskOutcome：5 种结果

源码：[`packages/durable/src/types.ts:620-655`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L620-L655)

```ts
/** Durable reason and optional result recorded when a task becomes terminal. */
export type TaskOutcome<R> =
	| {
			readonly status: "completed";
			readonly result: R;
			readonly error?: never;
			readonly reason?: never;
	  }
	/** Expected task or domain failure explicitly committed by its implementation. */
	| {
			readonly status: "failed";
			readonly error: TaskOutcomeError;
			readonly result?: R;
			readonly reason?: never;
	  }
	/** Explicit cancellation handled by the task's abort protocol. */
	| {
			readonly status: "aborted";
			readonly reason?: string;
			readonly result?: R;
			readonly error?: never;
	  }
	/** Task that cannot resume because its definition or migration is unavailable. */
	| {
			readonly status: "orphaned";
			readonly reason: string;
			readonly result?: never;
			readonly error?: never;
	  }
	/** Runtime-detected contract failure, such as an uncaught throw or no durable progress. */
	| {
			readonly status: "faulted";
			readonly error: TaskOutcomeError;
			readonly result?: never;
			readonly reason?: never;
	  };
```

| 结果 | 谁决定 | 含义 |
|---|---|---|
| `completed` | 任务自己 | 成功，`result` 是结果 |
| `failed` | 任务自己 | 预期内的失败，例如支付被拒 |
| `aborted` | 任务的 abort 处理函数 | 任务的中止协议运行过，由它决定结果 |
| `orphaned` | 调度器 | 被中止时没有任何定义能接手它，任务代码一行都没运行 |
| `faulted` | 调度器 | 违反合约：阶段抛了未捕获的异常、没有持久进度、或被崩溃打断次数超过上限 |

### 3.3 TaskRecord：存储里的一行

源码：[`packages/durable/src/types.ts:704-764`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L704-L764)

```ts
/** Identity, definition, and scheduling fields shared by every task state. */
type TaskRecordBase<I, R> = {
	/** Adapter-owned context that created this task; phase invocations extract it as their parent. */
	readonly trace?: TelemetryCarrier;
	/** Immutable creation-time links for the task's first invocation. */
	readonly links?: readonly TelemetryCarrier[];
	readonly id: TaskId<R>;
	readonly conversationId: ConversationId;
	/** Registered task definition name. */
	readonly kind: string;
	/** Definition version used to migrate live input and checkpoints. */
	readonly version: number;
	/** Original task input retained while the task is live or terminal. */
	readonly input: I;
	/** Owning task of a child task; absent for a task its conversation owns. Immutable. */
	readonly owner?: TaskId;
	/** Whether this conversation-owned task is excluded from ordinary idle waits, conversation aborts, and cascades. */
	readonly background: boolean;
	/** Durable abort mark checked before run-mode progress is committed. */
	readonly abortRequested: boolean;
	/**
	 * Why the mark was set when not by an abort request: `restart` for a task abandoned after a restart
	 * (`TaskOptions.abandonOnRestart`) or below one. Such a task waits for its definition instead of becoming `orphaned`.
	 * A later abort request clears it.
	 */
	readonly abortReason?: "restart";
	/** Set from `TaskOptions.abandonOnRestart`. Immutable. */
	readonly abandonOnRestart?: true;
	/**
	 * Wall-clock milliseconds of the first change to `running`, stamped by the Session. Kept through waits and recovery,
	 * so the span to `endedAt` includes them. Absent before the task first runs, and on records written by earlier
	 * versions.
	 */
	readonly startedAt?: number;
	/** Wall-clock milliseconds of the change to `terminal`, stamped by the Session; absent while live. */
	readonly endedAt?: number;
	/**
	 * How many times in a row a crash, eviction, or reset cut the task off while it ran, without the task changing its
	 * state since; open counts them, and a close does not. At `HarnessSettings.restartLimit` the task ends (the built-in
	 * tasks end themselves, visibly; the scheduler faults any other kind), and a generation reads it to charge an
	 * interrupted model request as a failed attempt.
	 */
	readonly restarts?: number;
};

/** Complete replacement record for one durable task state machine. */
export type TaskRecord<I, S, R> = TaskRecordBase<I, R> &
	(
		| {
				readonly state: Extract<
					TaskState<S, R>,
					{ readonly status: "pending" | "running" | "waiting" | "sleeping" }
				>;
				/** Small first-writer-wins values retained while the task can run. */
				readonly memos?: Readonly<Record<string, JsonValue>>;
		  }
		| {
				readonly state: Extract<TaskState<S, R>, { readonly status: "completing" | "terminal" }>;
				readonly memos?: never;
		  }
	);
```

用 §2.3 的输出对照：

| 字段 | `[pending]` | `[sleeping]` | `[terminal]` | 说明 |
|---|---|---|---|---|
| `id` | 7 | 7 | 7 | 任务 ID。`TaskId<R>` 是数字加上结果类型的品牌 |
| `conversationId` | 1 | 1 | 1 | 所属会话。创建后不变 |
| `kind`、`version` | `lab.sleeper`、1 | 同 | 同 | 调度器按 `kind` 在注册表里找定义，按 `version` 判断要不要迁移 |
| `input` | `{"label":"demo"}` | 同 | 同 | 创建时的输入，一直保留 |
| `owner` | 无 | 无 | 无 | 拥有它的任务。没有时由会话拥有（顶层任务） |
| `background` | false | false | false | 后台任务不参与会话的空闲判断和级联中止 |
| `abortRequested` | false | false | false | 持久的中止标记 |
| `state` | `pending` + `{phase:"work"}` | `sleeping` + `{phase:"finish",…}` + `until` | `terminal` + `outcome` | §3.1 |
| `memos` | 无 | `{"effect-done":true}` | 无 | 先写者胜的小值。进入终止状态时被删掉 |
| `startedAt`、`endedAt` | 无 | 有 `startedAt` | 两个都有 | Session 在第一次变成 `running` 和变成 `terminal` 时盖章 |
| `restarts` | 无 | 无 | 无 | 被崩溃打断的连续次数，§4.2、§7.1 |

`[pending]` 到 `[sleeping]` 之间，检查点从 `work` 换成了 `finish`，同时多了 memo。`[terminal]` 里 `checkpoint` 和 `memos` 都没有了：终止记录只留结果。

### 3.4 TaskDefinition 和 TaskOptions

源码：[`packages/durable/src/types.ts:363-392`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L363-L392)

```ts
/** Executable durable state machine definition, registered in the registry by `name`. */
export type TaskDefinition<I, S extends { phase: string }, R, H extends object> = {
	/** Registered task kind persisted in `TaskRecord.kind`. */
	readonly name: string;
	/** Definition version persisted with live input and checkpoints. */
	readonly version: number;
	/** First durable checkpoint for a newly created task. */
	initial(input: I): S;
	/** Exhaustive phase map; each handler receives the task narrowed to its phase. */
	readonly phases: {
		readonly [P in S["phase"]]: PhaseHandler<I, Extract<S, { phase: P }>, S, R, H>;
	};
	/** Runs in a fresh invocation after an abort mark and must commit a terminal outcome. */
	abort(task: RunningTask<I, S, R>, runtime: TaskRuntime<I, S, R, H>, context: Context): Promise<void>;
	/** Convert a record stored by any older supported version; runs at reservation. */
	migrate?(
		input: JsonValue,
		checkpoint: JsonValue,
		fromVersion: number,
	): {
		input: I;
		checkpoint: S;
	};
	readonly hooks?: H;
};

/** Typed executable task definition. */
export interface Task<I, S extends { phase: string }, R, H extends object> {
	readonly definition: TaskDefinition<I, S, R, H>;
}
```

| 字段 | 说明 |
|---|---|
| `name` | 任务的 kind，存进 `TaskRecord.kind` |
| `version` | 定义的版本。存储里的版本比它旧时调用 `migrate`，比它新时任务被阻塞（`task_too_old`） |
| `initial(input)` | 新任务的第一个检查点 |
| `phases` | 阶段表。键必须覆盖检查点里 `phase` 的所有取值，类型上逐个收窄 |
| `abort` | 有中止标记后，在一次新的调用里只运行一次，必须提交终止结果 |
| `migrate` | 可选，在预留时把旧版本的输入和检查点转成新版本 |
| `hooks` | 这个任务支持的 hook 名字（类型参数 `H`），扩展用 `hook(task, handlers)` 注册（D6） |

`defineTask()` 本身什么也不做：

源码：[`packages/durable/src/tasks.ts:1-8`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/tasks.ts#L1-L8)

```ts
import type { Task, TaskDefinition } from "./types.ts";

/** Define an executable task. Register it in the registry so a Harness can run tasks of its kind. */
export function defineTask<I, S extends { phase: string }, R, H extends object = object>(
	definition: TaskDefinition<I, S, R, H>,
): Task<I, S, R, H> {
	return { definition };
}
```

`define.ts` 里的 `defineExtension`、`defineTool`、`hook`、`runEndTask` 等也都是这样的恒等函数，只为了让 TypeScript 推导出类型。例如 `hook()` 把任务对象换成它的名字：

源码：[`packages/durable/src/harness/define.ts:38-49`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/define.ts#L38-L49)

```ts
/** Hook handlers for tasks with `task`'s name. */
export function hook<K extends AnyTask>(task: K, handlers: Partial<HooksOf<K>>): HookRegistration {
	return { task: task.definition.name, handlers };
}

/** A task an `afterRun` hook asks for, its input typed by the task (spec §8.3). */
export function runEndTask<I extends JsonValue, S extends { phase: string }, R, H extends object>(
	task: Task<I, S, R, H>,
	input: I,
): RunEndTask {
	return { task, input };
}
```

创建任务时必须给出所有者：

源码：[`packages/durable/src/types.ts:394-421`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L394-L421)

```ts
/** Who owns a task: its conversation (a top-level task) or another task of the same conversation (a child task). */
export type TaskOwnership = { readonly kind: "conversation" } | { readonly kind: "task"; readonly taskId: TaskId };

/** How a waiting task treats the tasks it waits on (spec §5.5). */
export type JoinPolicy = "failFast" | "allSettled";

/** Creation options for a durable task. */
export type TaskOptions = {
	/** Omitted: inherit the creator's carrier. "root": start at the adapter root without a stored carrier. */
	readonly trace?: TelemetryCarrier | "root";
	/** Immutable creation-time links, attached to the spans of this task's first invocation. */
	readonly links?: readonly TelemetryCarrier[];
	/** Required: a task always names its owner (spec §5.5). */
	readonly ownership: TaskOwnership;
	/**
	 * Default: the owner task's conversation, or the transaction's bound conversation; required for conversation-owned
	 * tasks created by Session commits that are not bound to a conversation.
	 */
	readonly conversationId?: ConversationId;
	/** Conversation-owned tasks only: excluded from ordinary idle waits, conversation aborts, and cascades. */
	readonly background?: boolean;
	/**
	 * Its creator awaits it only in memory and does not resume after a restart, so it is pointless once the Harness that
	 * created it is gone. When scheduling starts in a later Harness, the task, if still live, is abort-marked with reason
	 * `restart`, and the mark cascades to its ordinary owned work, before any of it runs again.
	 */
	readonly abandonOnRestart?: boolean;
};
```

### 3.5 Invocation：内存里的一次调用

任务记录是持久的；调用只存在于内存。

源码：[`packages/durable/src/harness/scheduler.ts:103-125`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L103-L125)

```ts
/** One in-memory execution of a task in run or abort mode. */
type Invocation = {
	readonly taskId: TaskId;
	readonly kind: string;
	/** The extension that installed the definition this invocation runs, as of its reservation; none for a built-in. */
	readonly extension: string | undefined;
	readonly conversationId: ConversationId;
	readonly mode: "run" | "abort";
	readonly controller: AbortController;
	links?: readonly TelemetryCarrier[];
	/** Context passed to handlers; cancelled by `controller`. */
	context: Context;
	/** Keeps the terminal state available after it leaves the live index. Released when the invocation finishes. */
	record?: TaskRecord<JsonValue, Checkpoint, JsonValue>;
	fault?: Extract<TaskOutcome<JsonValue>, { readonly status: "faulted" }>;
	/** Watches acquired through the runtime; stopped at invocation end. */
	readonly watches: Set<DocumentWatch<JsonObject>>;
	/** Timers armed through the runtime; disarmed at invocation end. */
	readonly timers: Set<() => void>;
	ended: boolean;
	readonly done: Promise<void>;
	readonly finish: () => void;
};
```

| 字段 | 说明 |
|---|---|
| `mode` | `"run"` 运行阶段函数，`"abort"` 运行 abort 处理函数。由持久的 `abortRequested` 推出，重启后重新推导 |
| `controller` | 调用结束、被中止、Harness 关闭时 abort。阶段函数收到的 `context` 和 `runtime.signal` 都由它取消 |
| `record` | 调用看到的最新任务记录 |
| `watches`、`timers` | 通过 runtime 获得的 watch 和定时器，调用结束时全部停掉 |
| `ended` | 结束后 runtime 的所有操作都抛错（§4.9） |

### 3.6 类型关系图

```
 TaskDefinition（代码，注册在 registry 里，按 name 查找）
   name / version / initial / phases{…} / abort / migrate
        │ tx.createTask(task, input, { ownership })
        ▼
 TaskRecord（存储里的一行，完整替换式更新）
   id, conversationId, kind, version, input, owner?, background,
   abortRequested, abortReason?, restarts?, memos?, startedAt?, endedAt?
   state: TaskState ──────────────┐
        │ #reserve() 改成 running   │ pending | running | waiting | sleeping
        ▼                          │ | completing{outcome} | terminal{outcome}
 Invocation（内存，一次调用）       │                       └─ TaskOutcome
   mode run|abort, controller,     │                          completed|failed|aborted
   record, watches, timers         │                          |orphaned|faulted
        │ #runtime()               │
        ▼                          │
 TaskRuntime（交给阶段函数）        │
   commit(change) ─ NextTaskState ─┘  running|waiting|sleeping|terminal
   memo / sleepUntil / waitForTask / abortOwned / outcomes / context …
```

---

## 4. 从入口读起

```
Harness.open()                                    §4.1
└─ TaskScheduler.open()                           §4.2
resume() → #kick() → #drain()                     §4.3
    └─ #reserve() → #planReservation() / #resolve() / #fit()   §4.3
        └─ #start()                               §4.4
            ├─ #run()                              §4.4
            │   ├─ #step() + #rules() + #progress()  §4.5–4.6
            │   └─ 阶段函数 → runtime.commit() → #gated() → #commitState()  §4.7
            │       runtime.memo() / sleepUntil()  §4.8
            └─ #runAbort()                         §4.10
            #end()                                 §4.9
abort() / join()                                   §4.10
#reconcile() / #terminate()                        §4.11
TaskGraphView                                      §4.12
```

### 4.1 Harness.open()

**调用位置**：宿主代码。例如 `12-recovery.ts` 的 `Harness.open(await openNodeSqliteStorage(databasePath), { models: createModels(), registry }, context)`。

源码：[`packages/durable/src/harness/harness.ts:586-611`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/harness.ts#L586-L611)

```ts
export const Harness = {
	/** Open a Harness over storage. The registry may keep changing while the Harness runs. */
	async open<Tool extends ToolRegistration>(
		storage: Storage,
		options: HarnessOptions<Tool>,
		context: Context,
	): Promise<Harness> {
		context.abortSignal?.throwIfAborted();
		const snapshot = options.registry.snapshot();
		const missing = BUILTIN_TASKS.filter((task) => snapshot.task(task.definition.name) === undefined);
		if (missing.length > 0) {
			const names = missing.map((task) => task.definition.name).join(", ");
			throw new Error(`Registry lacks built-in tasks ${names}; create it with createRegistry()`);
		}
		const harness = new HarnessImpl(storage, options, context);
		try {
			await harness.openTasks(context);
		} catch (error) {
			// The caller's context may be what failed open: close without it, and rethrow the open error.
			// A failing Storage close fails the Session, which reports it.
			await harness.close(withoutAbortSignal(context)).catch(() => {});
			throw error;
		}
		return harness;
	},
};
```

**讲解**

`open` 先检查注册表里有没有内置任务（`pi.generation`、`pi.tool`、`pi.compaction` 等，D5），然后构造 `HarnessImpl`，再调用 `openTasks()`，也就是 `TaskScheduler.open()`。open 本身不运行任何任务：调度要等 `resume()`、`submit()` 或 `waitForTask()` 打开。

构造函数里创建调度器时，Harness 传进去几个回调，调度器靠它们知道内置任务的存在，自己并不认识任何任务种类：

源码：[`packages/durable/src/harness/harness.ts:237-265`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/harness.ts#L237-L265)

```ts
		this.#tasks = new TaskScheduler({
			telemetry: options.telemetry,
			session: this,
			storage,
			registry: options.registry,
			models: currentModels(options.models, options.identity),
			agent: (id, snapshot, callContext) => this.resolveAgent(id, snapshot as RegistrySnapshot<Tool>, callContext),
			settings,
			env: (id, callContext) => this.buildEnv(id, callContext),
			now,
			onWake: options.onWake,
			timers: hostTimers(options.setTimer, (error) => report(error, { kind: "host_error", callback: "setTimer" })),
			writeAtClose: (writes) => this.writeAtClose(writes),
			report: this.#report,
			limitsRestarts: (kind) => LIMITS_RESTARTS.has(kind),
			settleOutcome: settleSchedulerOutcome,
			withdrawInputs: withdrawQueuedInputs,
			conversation: async (id, binding, callContext) => {
				const record = await this.readOnLine((line) => line.conversation(id, callContext));
				return record === undefined ? undefined : boundConversation(id, binding, this.#submissions, this.#tasks);
			},
			context: withoutAbortSignal(context),
			readOnly: options.readOnly === true,
		});
		this.#submissions = new Submissions(this, now, settings, () => this.#tasks.resume(), options.telemetry);
		this.#taskGraph = new TaskGraphView(this, {
			blocked: (record) => this.#tasks.blockedReason(record),
			subscribeBlocked: (listener) => this.#tasks.subscribeBlocked(listener),
		});
```

`limitsRestarts` 告诉调度器哪些 kind 会在重启次数到上限时自己结束（§4.3）；`settleOutcome` 是调度器自己写 `faulted`/`orphaned` 结果时，Harness 要顺带做的清理（例如结束 run、标记工具槽位，见 D5）。

**往哪走**：§4.2。

### 4.2 TaskScheduler.open()：恢复

**调用位置**：`Harness.open()` → `HarnessImpl.openTasks()` → `TaskScheduler.open()`。

源码：[`packages/durable/src/harness/scheduler.ts:428-469`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L428-L469)

```ts
	/**
	 * Load live tasks and change surviving `running` tasks back to `pending`, counting the restart. Dispatches nothing;
	 * a task cut off `settings.restartLimit` times ends when it is next reserved. Read-only, it only loads them: a
	 * `running` task stays as committed, running in another process.
	 */
	async open(context: Context): Promise<void> {
		this.#session.followCommits((publication) => this.#observe(publication));
		this.#session.subscribeClose(() => this.#seal());
		this.#unsubscribeRegistry = this.#registry.subscribe(() => {
			this.#notifyBlocked();
			this.#kick();
		});
		await this.#session.commitWith(async (tx) => {
			await this.#published();
			// Every table read before the first write.
			const scans = [];
			for (const status of LIVE_STATUSES) {
				scans.push(await scanAll((cursor) => tx.scanTasks({ status }, SCAN_PAGE_SIZE, cursor)));
			}
			for (const records of scans) {
				for (const record of records) {
					this.#track(undefined, record);
					this.#resumed.add(record.id);
					if (this.#readOnly) continue;
					if (record.abandonOnRestart === true) this.#abandoned.add(record.id);
					if (record.state.status === "running") {
						const restarts = (record.restarts ?? 0) + 1;
						const pending = withState(record, { status: "pending", checkpoint: record.state.checkpoint });
						tx.setTask({ ...pending, restarts } as AnyTaskRecord);
					}
					if (record.state.status === "waiting" && record.state.policy === "failFast") {
						this.#failFastChecks.add(record.id);
					}
				}
			}
		}, context);
		if (this.#readOnly) return;
		// Derive abort marks a crash left unapplied below cancelled owners, and finalize held outcomes.
		this.#cascadePending = true;
		this.#scheduleReconcile();
		this.#scheduleWake();
	}
```

**讲解**

open 在一次提交里扫描 5 种活状态的全部任务，加载进内存索引（`#track`），然后：

- 还处在 `running` 的任务，说明上一个进程在它运行时死了（正常关闭会写回 `pending`，§4.10）。open 把它改回 `pending`，`restarts` 加 1。这就是"重启计数"。
- `waiting`、`sleeping` 的任务保持原样，睡眠的截止时间跨重启有效。
- `abandonOnRestart` 的任务记进 `#abandoned`，第一次预留时给它打上 `abortReason: "restart"` 的中止标记（§4.3）。

最后安排一次 reconcile（§4.11），把崩溃前没来得及派生的级联中止标记补上，并结束可以结束的 `completing` 任务。

open 不分派任何调用。只读模式（`readOnly`）只加载，不改记录。

**往哪走**：§4.3。

### 4.3 #drain()、#reserve()：挑出可运行的任务

**调用位置**：`resume()` 设置 `#enabled = true` 后调用 `#kick()`。每次提交发布（`#observe`）、注册表变化、调用结束（`#start` 的 `finally`）也会 `#kick()`。`#kick()` 在微任务里调用 `#drain()`，不会在提交监听器里同步提交。

源码：[`packages/durable/src/harness/scheduler.ts:1874-1888`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L1874-L1888)

```ts
	async #drain(): Promise<void> {
		try {
			while (this.#dirty && this.#enabled && !this.#closing) {
				this.#dirty = false;
				for (const reservation of await this.#reserve()) this.#start(reservation);
			}
		} catch (error) {
			// As in `#reconcile()`: the reservation commit runs no extension code.
			this.#failSession(error);
		} finally {
			this.#draining = false;
			// A wakeup that arrived after the loop's last check still needs its pass.
			if (this.#dirty) this.#kick();
		}
	}
```

`#reserve()` 在一次提交里预留所有可运行的任务。核心是这个循环：

源码：[`packages/durable/src/harness/scheduler.ts:1926-1967`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L1926-L1967)

```ts
				for (const record of plan.ready) {
					const id = record.id;
					const mode = record.abortRequested ? "abort" : "run";
					// Work cut off `restartLimit` times in a row would run forever: an abort handler, or a phase of a kind that
					// does not end itself at the limit, ends the task `faulted` instead.
					// The settings are read only for a task that was cut off at all.
					const cutOff = record.restarts !== undefined && record.restarts >= this.#restartLimit();
					if (cutOff && (mode === "abort" || !this.#limitsRestarts(record.kind))) {
						const message =
							mode === "abort"
								? `Abort handler of task ${id} (${record.kind}) was cut off ${record.restarts} times`
								: `Task ${id} (${record.kind}) was cut off ${record.restarts} times without progress`;
						await this.#terminate(tx, record, {
							status: "faulted",
							error: { message, detail: { reason: "restart_limit" } },
						});
						faulted.push({ record, message });
						continue;
					}
					snapshot ??= this.#registry.snapshot();
					const resolution = this.#resolve(record, snapshot);
					if (resolution.kind === "blocked") {
						// An abandoned task waits for its definition, so its abort handler can clean up.
						if (mode === "abort" && record.abortReason === undefined) {
							await this.#terminate(tx, record, { status: "orphaned", reason: resolution.reason });
						}
						continue;
					}
					if (resolution.record !== record || record.state.status !== "running") {
						tx.setTask(
							withState(resolution.record, {
								status: "running",
								checkpoint: resolution.record.state.checkpoint,
							}),
						);
					}
					// Registered on the line, so marks and later reservations see it and close joins it; `#end` makes it a
					// candidate again.
					this.#runnable.delete(id);
					const invocation = this.#createInvocation(record, mode, installerOf(snapshot, resolution.task));
					reservations.push({ invocation, task: resolution.task, snapshot });
				}
```

**讲解**

候选来自 `#planReservation()`（`packages/durable/src/harness/scheduler.ts:1996`）：在 `#runnable` 集合里，去掉已经有调用的、不存在的、`completing` 的、`waiting` 且 `on` 里还有活任务的、`sleeping` 且没到时间的。所有者有取消意图的任务也不运行，等级联标记。

对每个候选：

1. `mode` 由 `abortRequested` 决定。
2. **重启上限。** `restarts` 达到 `settings.restartLimit`（默认 5）时：abort 调用，或者不会自己处理上限的 kind（扩展定义的任务），直接写成 `faulted`，detail 是 `{ reason: "restart_limit" }`。内置任务（`LIMITS_RESTARTS`）照常运行，由它的阶段函数自己结束（D5）。
3. **解析定义。** `#resolve` 按 `kind` 查注册表：

源码：[`packages/durable/src/harness/scheduler.ts:2065-2073`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2065-L2073)

```ts
	#fit(record: Pick<AnyTaskRecord, "id" | "version">, task: AnyTask | undefined): Fit {
		if (task === undefined) return { reason: "missing_task" };
		const version = task.definition.version;
		if (version === record.version) return { task, migrates: false };
		if (version < record.version) return { reason: "task_too_old" };
		const failed = this.#failedMigrations.get(record.id);
		if (failed?.task === task) return { reason: "migration_failed", error: failed.error };
		return { task, migrates: true };
	}
```

定义缺失（`missing_task`）、比存储里的版本旧（`task_too_old`）、迁移失败（`migration_failed`）时任务**阻塞**：记录不变，继续保持活状态，等注册表变化后重新考虑。阻塞原因只在内存里，不持久化。只有在被中止时，没有定义可用的任务才变成 `orphaned`（spec §5.4）。

4. 记录不是 `running` 时写成 `running`，迁移后的记录也在这里写入。然后创建 `Invocation`，放进返回列表。

这一切都在同一次提交里，所以"预留"和"写 `running`"是原子的。`#drain` 拿到列表后逐个 `#start()`。

**往哪走**：§4.4。

### 4.4 #start() 和 #run()：一次调用

源码：[`packages/durable/src/harness/scheduler.ts:2172-2216`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2172-L2216)

```ts
	#start(reservation: Reservation): void {
		const invocation = reservation.invocation;
		void (async () => {
			try {
				if (invocation.mode === "run") await this.#run(reservation);
				else await this.#runAbort(reservation);
			} catch (error) {
				this.#report(error, { kind: "task_error", ...this.#aboutTask(invocation) }, invocation.context);
			} finally {
				this.#end(invocation);
				invocation.record = undefined;
				invocation.links = undefined;
				invocation.fault = undefined;
				invocation.finish();
				this.#kick();
			}
		})();
	}

	/** Run phase handlers, each preceded by a step that decides on the line whether the invocation continues. */
	async #run(reservation: Reservation): Promise<void> {
		const invocation = reservation.invocation;
		const state = { task: reservation.task, snapshot: reservation.snapshot, reported: undefined as ReportedTask };
		const phase: Phase = { snapshot: () => state.snapshot, task: () => state.task, agent: undefined };
		const runtime = this.#runtime(invocation, phase);
		const progress = (tx: Transaction, current: ErasedRunningTask) =>
			this.#progress(tx, current, state, invocation.extension, invocation.context);
		let current = await this.#step(invocation, (task) => this.#rules(task, undefined), progress);
		while (current !== undefined && !this.#closing) {
			const task = current;
			current = await this.#phase(invocation, task, async () => {
				const checkpoint = task.state.checkpoint;
				// Each phase handler resolves its agent afresh, at first use.
				phase.agent = undefined;
				let previous: PhaseResult;
				try {
					await erased(state.task).phases[checkpoint.phase]!(task, runtime, invocation.context);
					previous = { checkpoint };
				} catch (error) {
					previous = { checkpoint, failure: { error } };
				}
				return this.#step(invocation, (next) => this.#rules(next, previous), progress);
			});
		}
	}
```

**讲解**

`#start` 在后台启动调用，无论成功失败，`finally` 都会 `#end()` 并再 `#kick()` 一次，让调度器重新看看有没有别的任务能跑。

`#run` 的结构是"步骤、阶段、步骤、阶段……"：

- 第一个 `#step` 在任何阶段之前运行，`previous` 是 `undefined`。
- 每个阶段函数按检查点的 `phase` 从 `state.task.definition.phases` 里取出来执行。它的异常被捕获成 `failure`，不会冒出去。
- 阶段函数返回后再走一个 `#step`，用阶段开始时的检查点和现在的记录比较。`#step` 返回下一次要用的记录，返回 `undefined` 时调用结束。

所以一个调用可以连续执行多个阶段，只要每个阶段都提交了进度。`11-tasks.ts` 的 `prepare` → `charge` 就在同一个调用里。阶段之间不经过 `pending`。

`#phase`（`packages/durable/src/harness/scheduler.ts:2219`）只是在外面包一层遥测 span，记录这个阶段的结果（`done`、`next=<phase>`、`waiting` 等）。

**往哪走**：§4.5。

### 4.5 #step() 和 #rules()：每两个阶段之间的 6 条规则

源码：[`packages/durable/src/harness/scheduler.ts:2362-2416`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2362-L2416)

```ts
	/**
	 * One decision on the Session line, before a phase: a decision region (`#region()`) that reads the task's record. A
	 * task that is no longer running (rule 1: terminal, `completing`, `waiting`, or `sleeping`) or a closing Harness (rule
	 * 2) ends the invocation without a write; otherwise `plan` decides, and a decision that stands is applied: the
	 * invocation continues, with `progress` for rule 5, or ends, with a fault written. Ending happens inside the callback,
	 * before a fault's Harness cleanup. A rejected step, such as admission after close, also ends the invocation. Its
	 * outcome goes to the invocation only, so it is a scheduler commit (`commitInternal()`).
	 */
	async #step(
		invocation: Invocation,
		plan: (current: ErasedRunningTask) => StepPlan,
		progress?: (tx: Transaction, current: ErasedRunningTask) => boolean,
	): Promise<ErasedRunningTask | undefined> {
		let fault: { readonly error: unknown } | undefined;
		try {
			const continued = await this.#session.commitInternal(async (tx) => {
				const decided = await this.#region(() => {
					const found = this.#liveRecord(invocation.taskId);
					if (found?.state.status !== "running" || this.#closing) return { plan: END };
					const current = found as ErasedRunningTask;
					return { current, plan: plan(current) };
				});
				const { current } = decided;
				const step = decided.plan;
				if (step.kind === "continue") return current;
				if (step.kind === "progress" && progress!(tx, current!)) return current;
				this.#end(invocation);
				if (step.kind === "cascade") {
					this.#cascadePending = true;
					this.#scheduleReconcile();
				}
				if (step.kind === "fault") {
					fault = { error: step.error };
					const message = errorMessage(step.error);
					invocation.fault = { status: "faulted", error: { message } };
					await this.#terminate(tx, current!, invocation.fault);
				}
				return undefined;
			}, withoutAbortSignal(invocation.context));
			// Reported once the fault is durable: a host may count it, for example toward a breaker.
			if (fault !== undefined)
				this.#report(
					fault.error,
					{ kind: "task_faulted", ...this.#aboutTask(invocation), reason: "threw" },
					invocation.context,
				);
			return continued;
		} catch (error) {
			// The step writes the scheduler's own decision, a fault or a terminal record; a throw there would otherwise
			// leave the task running, to be reserved and run again.
			this.#end(invocation);
			this.#failSession(error, invocation.context);
			return undefined;
		}
	}
```

源码：[`packages/durable/src/harness/scheduler.ts:2284-2303`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2284-L2303)

```ts
	/**
	 * Precedence rules for a run invocation, a decision region of its step. Rules 1 (terminal, `completing`, `waiting`, or
	 * `sleeping`) and 2 (closing) are applied by `#step`. Progress (rule 5) is decided by `#progress()` once the decision
	 * stands, as it takes a registry snapshot.
	 */
	#rules(current: ErasedRunningTask, previous: PhaseResult | undefined): StepPlan {
		// 3. abort mark: end; a fresh abort invocation starts once the task's ordinary owned work is gone.
		if (current.abortRequested) return END;
		// An owner's cancellation intent ends it too, before its cascade marks it; reservation then holds it back.
		if (!current.background && this.#belowCancelled(parentOf(current))) return CASCADE;
		if (previous === undefined) return CONTINUE;
		// 4. uncaught error.
		if (previous.failure !== undefined) return { kind: "fault", error: previous.failure.error };
		// 6. no durable progress.
		if (jsonEqual(current.state.checkpoint, previous.checkpoint)) {
			const message = `Task ${current.kind} phase ${previous.checkpoint.phase} returned without durable progress`;
			return { kind: "fault", error: new Error(message) };
		}
		return PROGRESS;
	}
```

**讲解**

`#step` 是一次调度器自己的提交（`commitInternal`），在 Session line 上读最新的任务记录。spec §5.1 列出了 6 条规则，代码按这个顺序判断：

| 规则 | 条件 | 结果 | 代码 |
|---|---|---|---|
| 1 | 任务不再是 `running`（终止、`completing`、`waiting`、`sleeping`） | 结束调用，不写 | `#step` 里 `found?.state.status !== "running"` |
| 2 | Harness 正在关闭 | 结束调用，不写；检查点留给重新打开 | 同一行 `this.#closing` |
| 3 | 有持久中止标记 | 结束 run 调用，之后启动 abort 调用 | `#rules` 的 `return END` |
| — | 所有者有取消意图，但级联标记还没写 | 结束调用，安排级联 | `return CASCADE` |
| 4 | 阶段抛了未捕获的异常 | 写 `faulted` | `previous.failure !== undefined` |
| 6 | 检查点和阶段开始时相同（`jsonEqual`） | 写 `faulted`："returned without durable progress" | `jsonEqual(...)` |
| 5 | 检查点变了 | 刷新注册表快照，继续下一个阶段或交接 | `return PROGRESS` → `#progress()` |

规则 1 说明一个阶段提交 `waiting` 或 `sleeping` 后，调用就结束了，任务不再占用调用。规则 6 是整个机制的保险：一个阶段如果既没提交新检查点，也没提交结果，重跑它只会得到同样的结果，所以调度器直接判它 faulted，而不是无限循环。§7.2 会触发这条规则。

"检查点变了"是逐字段比较 JSON。同一阶段内的进度也算，例如 `{ phase: "tick", n: 2 }` 变成 `{ phase: "tick", n: 3 }`。

**往哪走**：§4.6 交接，§4.7 提交。

### 4.6 #progress()：阶段边界上的定义交接

源码：[`packages/durable/src/harness/scheduler.ts:2304-2338`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2304-L2338)

```ts

	/**
	 * Rule 5, progress: refresh the snapshot; hand over to a replacement definition that can take the task, writing it
	 * back as `pending`, or keep running under the old one, reported once per resolved definition. Returns whether the
	 * invocation continues.
	 */
	#progress(
		tx: Transaction,
		current: ErasedRunningTask,
		state: { task: AnyTask; snapshot: RegistrySnapshot; reported: ReportedTask },
		extension: string | undefined,
		context: Context,
	): boolean {
		state.snapshot = this.#registry.snapshot();
		const next = state.snapshot.task(current.kind);
		if (next === state.task) return true;
		if (next !== undefined && canReserve(next, current)) {
			tx.setTask(withState(current, { status: "pending", checkpoint: current.state.checkpoint }));
			return false;
		}
		if (state.reported === undefined || state.reported.task !== next) {
			state.reported = { task: next };
			this.#report(
				new Error(`Task ${current.id} keeps running under its old ${current.kind} definition`),
				{
					kind: "task_outdated",
					...this.#aboutTask({ ...recordTask(current), extension }),
					reason: outdatedReason(next, current),
				},
				context,
			);
		}
		return true;
	}

```

**讲解**

扩展可以在运行中重新安装（spec §7.5）。每到阶段边界，调度器重新取注册表快照：

- 定义对象没变：继续。
- 换成了一个能接手的新定义（同版本，或更高版本且有 `migrate`）：把任务写回 `pending`，结束本次调用。下一次预留会用新定义创建新调用。这叫**交接**（handover）。
- 新定义接不了：继续用旧定义跑，报告一次 `task_outdated`。

所以一个正在跑的阶段永远用它开始时的代码跑完，换代码只发生在阶段之间。

**往哪走**：§4.7。

### 4.7 runtime.commit()、#gated()、#commitState()：阶段的提交

**调用位置**：阶段函数里的 `await runtime.commit((tx, current) => next, context)`。

`TaskRuntime` 由 `#runtime()`（`packages/durable/src/harness/scheduler.ts:2616`）构造，每次调用一个。它的 `commit`：

源码：[`packages/durable/src/harness/scheduler.ts:2664-2715`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2664-L2715)

```ts
			commit: async (change, context, options) => {
				let checkpoint: string | undefined;
				let interrupted:
					| {
							readonly message: string;
							readonly reason: "restart_limit" | "interrupted";
							readonly restarts: number;
					  }
					| undefined;
				await this.#gated(
					invocation,
					async (tx, current) => {
						const next = await change(tx, current);
						if (next !== undefined) await this.#commitState(tx, invocation, current, next);
						if (next?.status === "running" && current.state.checkpoint.phase !== next.checkpoint.phase) {
							checkpoint = next.checkpoint.phase;
						}
						// A task that ends for crashes says so (`task_interrupted`), so a host keeps a crash-loop signal.
						if (next?.status === "terminal") {
							const restarts = current.restarts ?? 0;
							const reason = next.outcome.status === "failed" ? interruptionOf(next.outcome.error) : undefined;
							if (reason !== undefined && next.outcome.status === "failed") {
								interrupted = { message: next.outcome.error.message, reason, restarts };
							} else {
								// A task that ended for crashes with another outcome says so itself, as a generation whose run
								// answered ends `completed` without its after-run tasks.
								const message = endedByCrashesStates.get(next);
								if (message !== undefined) interrupted = { message, reason: "restart_limit", restarts };
							}
						}
					},
					context,
					options,
				);
				if (interrupted !== undefined) {
					this.#report(
						new Error(interrupted.message),
						{
							kind: "task_interrupted",
							...this.#aboutTask(invocation),
							reason: interrupted.reason,
							restarts: interrupted.restarts,
						},
						invocation.context,
					);
				}
				if (checkpoint !== undefined) {
					invocation.context.telemetry.addEvent("pi.durable.checkpoint", {
						phase: checkpoint,
					});
				}
			},
```

`#gated` 在提交前把门：

源码：[`packages/durable/src/harness/scheduler.ts:2927-2957`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2927-L2957)

```ts
	/** Commit after rereading the task on the line and gating the invocation. */
	#gated<T>(
		invocation: Invocation,
		change: (tx: Transaction, current: ErasedRunningTask) => T | Promise<T>,
		context: Context,
		options?: CommitOptions,
	): Promise<T> {
		if (invocation.ended) return Promise.reject(endedError(invocation));
		return this.#session.commitWith(
			async (tx) => {
				if (invocation.ended) throw endedError(invocation);
				if (this.#closing) throw closedError(this.#session);
				const current = await this.#region(() => {
					const found = this.#liveRecord(invocation.taskId);
					if (found === undefined) throw new Error(`Task ${invocation.taskId} is terminal`);
					if (found.state.status !== "running")
						throw new Error(`Task ${invocation.taskId} is ${found.state.status}`);
					if (invocation.mode === "run" && found.abortRequested) {
						throw new Error(`Task ${invocation.taskId} has a durable abort mark`);
					}
					return found as ErasedRunningTask;
				});
				// The step that ends it may have run while the region waited.
				if (invocation.ended) throw endedError(invocation);
				return change(tx, current);
			},
			context,
			{ conversationId: invocation.conversationId, taskId: invocation.taskId },
			options,
		);
	}
```

`#commitState` 写入新状态：

源码：[`packages/durable/src/harness/scheduler.ts:2432-2458`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2432-L2458)

```ts
	/**
	 * Replace a running task's state with what it committed. A terminal state holds as `completing` while ordinary owned
	 * work is live, judged on the commit's candidates, so work the same commit creates below the task counts. A wait is
	 * validated first.
	 */
	async #commitState(
		tx: Transaction,
		invocation: Invocation,
		current: ErasedRunningTask,
		next: NextTaskState<Checkpoint, JsonValue>,
	): Promise<void> {
		if (next.status === "waiting") await this.#validateWait(tx, invocation, current, next.on, next.policy);
		if (next.status === "sleeping") validateSleep(invocation, current.id, next.until);
		if (next.status === "terminal") {
			const overlay = overlayOf(tx);
			await this.#loadScopes(false);
			for (const record of overlay.tasks.values()) await this.#loadChain(parentOf(record), overlay);
			if (await this.#region(() => this.#hasOwnedLive(current.id, overlayOf(tx)), this.#reloadScopes)) {
				tx.setTask(withState(progressed(current), { status: "completing", outcome: next.outcome }));
				return;
			}
		}
		// A state the task committed that differs from its current one is progress: its restart count starts over. The
		// same checkpoint committed again, for example as intent before an effect, is not.
		const same = jsonEqual(current.state as unknown as JsonValue, next as unknown as JsonValue);
		tx.setTask(withState(same ? current : progressed(current), next));
	}
```

**讲解**

一次 `runtime.commit()` 的路径：

1. `#gated` 在 Session line 上重新读任务记录。调用已结束、Harness 正在关闭、任务不是 `running`、run 调用遇到中止标记，这几种情况都直接抛错，回调根本不会执行。所以一个已经被中止的任务，它残留的异步代码再也写不进任何东西。
2. 回调 `change(tx, current)` 在事务里执行，可以追加条目、改文档、创建子任务。提交选项里带着 `{ conversationId, taskId }`，所以这里追加的每个条目都记录 `byTaskId`，`tx.createTask()` 默认用任务所在的会话。
3. 回调返回下一个状态时，`#commitState` 在**同一个事务**里替换任务状态。返回 `undefined` 时状态不变，这次提交只写了条目或文档。
4. `#commitState` 里两个细节：
   - 返回 `terminal` 但它拥有的工作还活着时，写成 `completing`（§4.11）。
   - 新状态和当前状态不同才算进度，`progressed()` 删掉 `restarts`。同一个检查点再提交一次不算，例如 D5 里"先写意图再执行"的意图提交。

源码：[`packages/durable/src/harness/scheduler.ts:3109-3120`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L3109-L3120)

```ts
function progressed(record: AnyTaskRecord): AnyTaskRecord {
	if (record.restarts === undefined) return record;
	const { restarts: _restarts, ...rest } = record;
	return rest as AnyTaskRecord;
}

/** Replace a live record's state; memos and the restart count disappear once an outcome is decided. */
function withState(record: AnyTaskRecord, state: TaskState<JsonValue, JsonValue>): AnyTaskRecord {
	if (state.status !== "terminal" && state.status !== "completing") return { ...record, state } as AnyTaskRecord;
	const { memos: _memos, restarts: _restarts, ...rest } = record;
	return { ...rest, state };
}
```

这就是"检查点、结果和这次提交写的条目原子落盘"的实现：spec §5.1 说，返回的状态和回调写的条目、文档、子任务在同一次提交里。

**往哪走**：§4.8。

### 4.8 runtime.memo() 和 runtime.sleepUntil()

源码：[`packages/durable/src/harness/scheduler.ts:2716-2737`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2716-L2737)

```ts
			memo: ((name: string, ...rest: readonly unknown[]) => {
				if (rest.length === 1) {
					return this.#read(invocation, async () => memoOf(this.#live.get(invocation.taskId), name));
				}
				const candidate = rest[0] as JsonValue;
				return this.#gated(
					invocation,
					(tx, current) => {
						const winner = memoOf(current, name);
						if (winner !== undefined) return winner;
						tx.setTask({ ...current, memos: { ...current.memos, [name]: candidate } } as AnyTaskRecord);
						return candidate;
					},
					rest[1] as Context,
				);
			}) as ErasedRuntime["memo"],
			sleepUntil: (until, checkpoint, context) =>
				this.#gated(
					invocation,
					(tx, current) => this.#commitState(tx, invocation, current, { status: "sleeping", checkpoint, until }),
					context,
				),
```

**讲解**

`memo(name, candidate)` 也是一次门控提交：已有同名 memo 时返回已有的值，否则写入 `candidate` 并返回它。并发的两个候选只会有一个赢，它们拿到同一个值。`memo(name)` 只读。memo 存在任务记录里，跨检查点保留，进入终止状态时消失（`withState`，`packages/durable/src/harness/scheduler.ts:3116`）。`12-recovery.ts` 就是用 memo 防止重跑的阶段重复打印。

`sleepUntil(until, checkpoint)` 等于提交 `{ status: "sleeping", checkpoint, until }`，经过 `validateSleep` 检查：

源码：[`packages/durable/src/harness/scheduler.ts:3140-3145`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L3140-L3145)

```ts
function validateSleep(invocation: Invocation, task: TaskId, until: number): void {
	if (invocation.mode === "abort") throw new Error(`Abort handler of task ${task} cannot sleep`);
	if (typeof until !== "number" || !Number.isFinite(until) || until < 0) {
		throw new Error(`Task ${task} cannot sleep until ${until}: not a time in milliseconds`);
	}
}
```

睡眠在存储里而不在内存里。按规则 1，提交 `sleeping` 后调用结束，任务不占用调用。到时间后，`#planReservation` 里的 `#notDue` 不再拦住它，它被重新预留，从新检查点继续。spec §5.6 解释了为什么阶段内部不能睡：阶段在崩溃或关闭后会从头重跑，如果在阶段里算一个截止时间再 `await`，每次重跑截止时间都会往后推。

`wake()`（`packages/durable/src/harness/scheduler.ts:514`）根据内存回答"Session 下一次什么时候需要进程"：`active` 表示现在就有任务能跑，`at` 是最早的未到期睡眠。Cloudflare Durable Object 这类会被驱逐的宿主用它设置闹钟。

**往哪走**：§4.9。

### 4.9 #end()：调用结束

源码：[`packages/durable/src/harness/scheduler.ts:2578-2591`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2578-L2591)

```ts
	/** End an invocation: its runtime operations reject from now on, its signal aborts, its watches stop, and its task is free. */
	#end(invocation: Invocation): void {
		if (invocation.ended) return;
		invocation.ended = true;
		if (this.#invocations.get(invocation.taskId) === invocation) {
			this.#invocations.delete(invocation.taskId);
			if (this.#live.has(invocation.taskId)) this.#runnable.add(invocation.taskId);
		}
		for (const watch of invocation.watches) void watch.stop();
		for (const disarm of invocation.timers) disarm();
		invocation.timers.clear();
		// Pending waits bound to the invocation, such as a tool's waitForTask(), reject with it.
		invocation.controller.abort(endedError(invocation));
	}
```

**讲解**

调用结束时，它从 `#invocations` 移除，任务回到 `#runnable` 候选集，watch 停止，定时器解除，`controller.abort()` 让所有还挂在这个调用上的等待（例如一个没 await 的 `waitForTask()`）以 "invocation has ended" 拒绝。之后 runtime 的每个方法都先检查 `invocation.ended`，直接抛错。

**往哪走**：§4.10。

### 4.10 abort()、#runAbort()、join()：中止和关闭

`Harness.abortTask(id)` 调用调度器的 `abort()`：

源码：[`packages/durable/src/harness/scheduler.ts:586-622`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L586-L622)

```ts
	/**
	 * Commit the abort mark, or settle a task that no registered definition can take as `orphaned` when nothing it owns
	 * is live, then join the run invocation seen on the line; the commit listener signalled it. The abort invocation
	 * starts once the task's ordinary owned work is gone. A `completing` task is only marked. A request replaces a
	 * `restart` mark, so a task abandoned after a restart that waits for its definition is orphaned; with `keepRestart`,
	 * as for an owner's own cleanup, such a task keeps its mark and waits on.
	 */
	async abort(id: TaskId, context: Context, keepRestart = false): Promise<"marked" | "terminal"> {
		const marked = await this.#session.commitWith(async (tx) => {
			const current = await tx.task(id);
			if (current === undefined) throw new Error(`Task ${id} does not exist`);
			if (current.state.status === "terminal") return { result: "terminal" as const };
			const invocation = this.#invocations.get(id);
			// A restart-marked task has no run invocation; it only waits for its abort.
			if (keepRestart && current.abortReason === "restart") return { result: "marked" as const };
			if (invocation === undefined && current.state.status !== "completing") {
				await this.#loadScopes(false);
				const owns = await this.#region(() => this.#hasOwnedLive(id), this.#reloadScopes);
				// Resolved once that decision stands, as it may run `migrate`.
				const resolution = owns
					? undefined
					: this.#resolve(current as RunnableTaskRecord, this.#registry.snapshot());
				if (resolution?.kind === "blocked") {
					await this.#terminate(tx, current, { status: "orphaned", reason: resolution.reason });
					return { result: "marked" as const };
				}
			}
			if (!current.abortRequested || current.abortReason !== undefined) tx.setTask(withAbortMark(current));
			return { result: "marked" as const, run: invocation?.mode === "run" ? invocation : undefined };
		}, context);
		// The commit listener signalled the run; join it. A run that ignores its signal can outlive a failed Session,
		// which ends the wait instead.
		if (marked.run !== undefined) {
			await awaitWithContext(Promise.race([marked.run.done, this.#session.failed]), context);
		}
		return marked.result;
	}
```

运行 abort 处理函数：

源码：[`packages/durable/src/harness/scheduler.ts:2339-2360`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2339-L2360)

```ts
	/** Run the abort handler once; rules 1, 2, and 4 apply, and returning without an outcome faults. */
	async #runAbort(reservation: Reservation): Promise<void> {
		const invocation = reservation.invocation;
		const current = this.#live.get(invocation.taskId) as ErasedRunningTask | undefined;
		if (current === undefined || this.#closing) return;
		await this.#phase(invocation, current, async () => {
			let failure: { readonly error: unknown } | undefined;
			try {
				const phase: Phase = {
					snapshot: () => reservation.snapshot,
					task: () => reservation.task,
					agent: undefined,
				};
				const runtime = this.#runtime(invocation, phase);
				await erased(reservation.task).abort(current, runtime, invocation.context);
			} catch (error) {
				failure = { error };
			}
			const message = `Abort handler of task ${invocation.taskId} returned without a terminal outcome`;
			await this.#step(invocation, () => ({ kind: "fault", error: failure?.error ?? new Error(message) }));
		});
	}
```

关闭时的 `join()`：

源码：[`packages/durable/src/harness/scheduler.ts:563-591`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L563-L591)

```ts
	/**
	 * Signal every invocation and wait for them, then write the tasks they ran back as `pending` (`writeAtClose`), as a
	 * close is not a crash. Close calls this after every close listener has run, so waits, watches, and streams a task
	 * holds end with the Session's reason, not as cancelled by its signal.
	 */
	async join(): Promise<void> {
		const stopped = [...this.#invocations.values()];
		for (const invocation of stopped) invocation.controller.abort();
		await Promise.allSettled(stopped.map((invocation) => invocation.done));
		if (stopped.length === 0) return;
		// A close is not a crash: a task it stopped is `pending` again, so the next open counts no restart for it. Read
		// on the line after commits admitted before the seal, such as an abort mark, so the write keeps them. A failure
		// leaves them `running`, counted as if cut off; the Storage guard fails the Session and reports it.
		await this.#writeAtClose(() =>
			stopped.flatMap((invocation) => {
				const record = this.#live.get(invocation.taskId);
				if (record?.state.status !== "running") return [];
				const value = withState(record, { status: "pending", checkpoint: record.state.checkpoint });
				return [{ type: "task", value } as const];
			}),
		).catch(() => {});
	}

	/**
	 * Commit the abort mark, or settle a task that no registered definition can take as `orphaned` when nothing it owns
	 * is live, then join the run invocation seen on the line; the commit listener signalled it. The abort invocation
	 * starts once the task's ordinary owned work is gone. A `completing` task is only marked. A request replaces a
	 * `restart` mark, so a task abandoned after a restart that waits for its definition is orphaned; with `keepRestart`,
	 * as for an owner's own cleanup, such a task keeps its mark and waits on.
```

**讲解**

中止协议（spec §5.4）是：

```text
commit abortRequested
signal and join active run invocation
wait until ordinary owned work is no longer live
start a fresh abort invocation
abort handler commits terminal outcome
```

对应到代码：

1. `abort()` 在一次提交里写 `abortRequested: true`（`withAbortMark`）。提交发布后 `#observe` 看到新标记，在微任务里 abort 正在跑的 run 调用（`packages/durable/src/harness/scheduler.ts:803`）。`abort()` 等这个 run 调用结束才返回。
2. run 调用里任何后续的 `runtime.commit()` 都被 `#gated` 拒绝（"has a durable abort mark"），下一个 `#step` 按规则 3 结束它。
3. 任务拥有的工作还活着时，`#waitingOn` 返回它们，预留跳过这个任务。所以 abort 处理函数**自底向上**运行：子任务的 abort 先跑完，父任务的 abort 才开始，并且能看到子任务的最终结果。
4. `#reserve` 以 `mode: "abort"` 预留它，`#runAbort` 只运行一次 `definition.abort`。之后的 `#step` 直接给一个 fault 计划：如果 abort 处理函数已经提交了终止结果，规则 1 先命中，什么也不写；如果它没提交，就写 `faulted`。
5. 没有调用、没有活的子工作、并且没有定义能接手时，`abort()` 在标记的同一次提交里直接写 `orphaned`。

`join()` 是 `close()` 的一部分。它 abort 所有调用（前台和后台），等它们结束，然后把停下来的任务写回 `pending`，**不加** `restarts`。关闭不是崩溃：只有进程死掉，存储里才会留下 `running` 记录，下次 open 才会计数。这就是 §2.2 里关闭后状态是 `pending` 的原因。

**往哪走**：§4.11。

### 4.11 所有权、completing 和 #reconcile()

任务和会话组成一棵所有权树。每个任务在创建时指定所有者：会话（顶层任务）或同会话的另一个任务（子任务）。子任务不能是后台任务。

spec §5.5 定义了两条规则：

- **中止向下传。** 一个活的所有者有**取消意图**（中止标记，或一个非 `completed` 的 `completing` 结果）时，它下面的普通工作都会得到中止标记。后台任务是边界。
- **拥有的工作不能比所有者活得久。** 所有者提交终止结果时，如果它拥有的工作还活着，结果先存成 `completing`。等下面的工作都结束，调度器在后面的一次提交里写出 `terminal`。

取消意图的判断：

源码：[`packages/durable/src/harness/scheduler.ts:3022-3030`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L3022-L3030)

```ts
function withAbortMark(record: AnyTaskRecord, reason?: "restart"): AnyTaskRecord {
	const { abortReason: _reason, restarts: _restarts, ...rest } = record;
	return { ...rest, abortRequested: true, ...(reason === undefined ? {} : { abortReason: reason }) } as AnyTaskRecord;
}

/** A live owner's durable cancellation intent: its abort mark, or a held outcome other than `completed`. */
function cancellationIntent(record: AnyTaskRecord): boolean {
	return record.state.status !== "terminal" && (record.abortRequested || failedOutcome(record));
}
```

调度器自己写的结果（`faulted`、`orphaned`）也遵守这条规则：

源码：[`packages/durable/src/harness/scheduler.ts:2418-2431`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L2418-L2431)

```ts
	/**
	 * Write an outcome the scheduler decided. While the task's ordinary owned work is live it holds as `completing` and
	 * its Harness cleanup waits for the final commit (spec §5.5, rule 4); otherwise it is terminal with its cleanup.
	 */
	async #terminate(tx: Transaction, record: AnyTaskRecord, outcome: SchedulerOutcome): Promise<void> {
		await this.#loadScopes(false);
		if (await this.#region(() => this.#hasOwnedLive(record.id, overlayOf(tx)), this.#reloadScopes)) {
			tx.setTask(withState(record, { status: "completing", outcome }));
			return;
		}
		tx.setTask(withState(record, { status: "terminal", outcome }));
		await this.#settleOutcome(tx, record, outcome);
	}

```

级联标记不写在所有者的那次提交里，而是由 `#reconcile()` 在之后单独的一次提交里派生：

源码：[`packages/durable/src/harness/scheduler.ts:1015-1059`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/scheduler.ts#L1015-L1059)

```ts
	/**
	 * One commit that applies what committed records imply: abort marks below live owners with cancellation intent
	 * (spec §5.4), `failFast` marks (spec §5.5), withdrawn queued inputs below cancelled owners, and the final terminal
	 * record of every `completing` task whose ordinary owned work is gone. The durable records are the intent, so this
	 * also repairs whatever a crash left unapplied. Resolves idle waiters that the loaded edges decide.
	 */
	async #reconcile(): Promise<void> {
		this.#reconcileScheduled = false;
		const work = this.#reconcileWork();
		if (work) this.#reconcilingWork++;
		try {
			await this.#session.commitInternal(async (tx) => {
				if (this.#closing) return;
				// A cascade scans the queued inputs, which waits for every unpublished commit: a request made before the scan
				// is covered by it, and none is published while the job holds the line after it.
				const cascade = this.#cascadePending;
				const queued = await this.#loadScopes(cascade);
				if (cascade) this.#cascadePending = false;
				const marks = await this.#marks();
				for (const { record, reason } of marks.values()) tx.setTask(withAbortMark(record, reason));
				const withdrawn = await this.#region(
					() => queued.filter((id) => this.#belowCancelled({ conversation: id })),
					this.#reloadScopes,
				);
				for (const id of withdrawn) await this.#withdrawInputs(tx, id);
				await this.#finalize(tx);
			}, this.#context);
		} catch (error) {
			// No extension code runs in this commit: a throw is a storage failure, a host callback, or a bug. None is fixed by
			// running the pass again, so it fails the Session, which reports it.
			this.#failSession(error);
		}
		if (work) this.#reconcilingWork--;
		this.#scheduleWake();
		// Its commit's task changes were settled at their publication; a waiter that asked for this pass checks again, and
		// what its chain loads resolved can make any scope idle.
		if (this.#unknownsChanged) {
			this.#settleIdle(undefined);
			return;
		}
		this.#resolveIdleWaiters(undefined);
		// A pass checks expiry too, for hosts that decline the expiry timer.
		this.#expireContexts(this.#now(), this.#contextRetentionMs());
		this.#scheduleExpiry();
	}
```

**讲解**

`#reconcile` 做四件事，都在一次提交里：

1. `#marks()` 从 `#intent`（有取消意图的活任务集合）向下走，给每个非后台的活任务打中止标记；同时处理 `failFast` 等待：`on` 里有一个失败，其余活任务都打标记。
2. 级联到达的会话里，排队的输入被撤回（`withdrawInputs`，D5 的 inbox）。
3. `#finalize()` 把拥有的工作都已结束的 `completing` 任务写成 `terminal`。结束一个任务可能放开它的所有者，所以循环到没有变化为止。
4. 调度器自己写的结果，在最终提交里调用 `settleOutcome`（Harness 的清理）。

为什么单独提交：所有者的那次提交可能已经写了涉及的子任务，把标记分开写可以避免冲突；而且标记是从持久记录派生的，崩溃在两次提交之间也不会丢，下次 open 会再派生一次（§4.2 末尾的 `#cascadePending = true`）。

`23-child-tasks.ts` 的 checkout 任务演示了这些规则，见 §7.3。

**往哪走**：§4.12。

### 4.12 TaskGraphView：观察活任务

`Harness.watchTaskGraph()` 和 `observeTaskGraph()` 返回所有活任务的投影：

源码：[`packages/durable/src/harness/task-graph.ts:26-62`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/task-graph.ts#L26-L62)

```ts
/** A live task's durable status without its checkpoint and outcome payloads (spec §9.4). */
export type TaskGraphState =
	| { readonly status: "pending" | "running"; readonly phase: string }
	| {
			readonly status: "waiting";
			readonly phase: string;
			readonly on: readonly TaskId[];
			readonly policy: JoinPolicy;
	  }
	/** Asleep until the Harness clock reaches `until`. */
	| { readonly status: "sleeping"; readonly phase: string; readonly until: number }
	/** Outcome held until its ordinary owned work drains. */
	| { readonly status: "completing"; readonly outcome: TaskOutcome<JsonValue>["status"] };

export type TaskGraphNode = {
	readonly id: TaskId;
	readonly kind: string;
	readonly conversationId: ConversationId;
	/** Owner task; absent for a conversation-owned task. */
	readonly owner?: TaskId;
	readonly background: boolean;
	readonly abortRequested: boolean;
	readonly state: TaskGraphState;
	/**
	 * Why it cannot run here, when it cannot: no registered definition takes it (`missing_task`, `task_too_old`,
	 * `migration_failed`). Follows registry changes.
	 */
	readonly blocked?: BlockedReason;
	/** Conversations this task owns, in ID order. */
	readonly conversations: readonly ConversationId[];
};

/** Every live task of the Session (spec §9.4). */
export type TaskGraph = {
	/** Every live task, keyed by its decimal ID. */
	readonly tasks: Readonly<Record<string, TaskGraphNode>>;
};
```

**讲解**

任务图只包含活任务，不含检查点和结果的载荷，只有阶段名。第一个观察者挂上时在 Session line 上扫描建立，之后跟随每次已存储的提交增量更新（`advance`，`packages/durable/src/harness/task-graph.ts:220`）：任务变成 `terminal` 就删掉节点，否则重新计算节点；注册表变化时重新计算 `blocked` 原因。

[`lab/m11b-observe.ts`](../lab/m11b-observe.ts) 的 `graph` 模式在一次带工具调用的 run 里观察任务图：

```bash
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

第一列是毫秒，每次运行略有不同。可以看到本篇的机制在内置任务上的样子：

- `pi.generation` 在同一个调用里从 `prepare` 走到 `request`（规则 5，阶段之间不回 `pending`）。
- 模型返回工具调用后，生成任务在一次提交里创建子任务 `pi.tool`（`owner=9`），自己进入 `waiting on=[12]`，调用结束。
- 工具任务从 `call` 进入 `execute`，这是 D5 要讲的"意图"检查点。
- 工具结束后，生成任务的 `on` 都已终止，它被重新预留，在 `tools` 阶段收尾，并把 run 交给新的生成任务 14。

这些阶段的含义是 D5 的内容。

**往哪走**：→ D5 的内置任务。

---

## 5. 状态机

### 5.1 任务状态

```
                   tx.createTask()
                        │ initial(input)
                        ▼
                  ┌───────────┐  close: join() 写回 (scheduler.ts:576-583)
     ┌───────────▶│  pending  │◀─────────────────────────────┐
     │            └─────┬─────┘  open: running→pending，     │
     │                  │        restarts+1 (scheduler.ts:453-457)
     │                  │ #reserve() (scheduler.ts:1954-1960) │
     │ 交接              ▼                                   │
     │ (scheduler.ts:  ┌───────────┐ 阶段提交新检查点：保持    │
     │  2320-22)  ─────│  running  │ running (#commitState 2457)
     │                 └─┬───┬───┬─┘──────────────────────────┘
     │   提交 waiting    │   │   │ 提交 terminal
     │   (2443)          │   │   │ (#commitState 2445-2457)
     │        ┌──────────┘   │   └───────────────┐
     │        ▼              │ sleepUntil (2732)  │ 拥有的工作还活着？
     │  ┌───────────┐        ▼                    ├─ 是 ──▶ ┌────────────┐
     └──│  waiting  │  ┌───────────┐              │         │ completing │
 on 全部 └───────────┘  │ sleeping  │              │         └─────┬──────┘
 终止后预留             └─────┬─────┘              │  #finalize()   │
 (#waitingOn 2034)           │ 到 until 后预留     │  (1144)        │
                             │ (#notDue 1858)     ▼               ▼
                             └──▶ running      ┌───────────────────┐
                                               │     terminal      │
  调度器直接写 terminal：                        └───────────────────┘
   规则 4/6 → faulted (#step 2393-2398)
   restarts ≥ restartLimit → faulted (#reserve 1932-1943)
   中止时无定义可用 → orphaned (abort 608-611, #reserve 1947-1950)
```

### 5.2 一次调用

```
 #reserve()：pending/到期/可恢复 → running，创建 Invocation (1954-1966)
      │
      ▼
 #step(previous=undefined)  ──规则 1/2/3/级联──▶ #end()，不写 (2378-2388)
      │ CONTINUE
      ▼
 ┌─▶ 阶段函数 phases[checkpoint.phase] (2208)
 │      │ 抛异常 → failure (2211)
 │      ▼
 │  #step(previous)
 │      ├─ 规则 1/2/3 ─────────────▶ #end()，不写
 │      ├─ 规则 4 / 6 ─────────────▶ #terminate(faulted) (2393-2398)，#end()
 │      ├─ 规则 5，新定义可接手 ────▶ 写回 pending，#end() (2320-2322)
 │      └─ 规则 5，继续 ───┐
 └──────────────────────────┘
 abort 模式：#runAbort() 只调用一次 definition.abort (2353)，之后 #step 规则 1 或 fault (2357-2358)
```

---

## 6. 设计要点

### 6.1 effect sandwich：意图、副作用、结果

**做法。** spec §5.2 规定外部副作用夹在两次提交之间：

源码：[`packages/durable/docs/spec.md:2375-2377`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/docs/spec.md#L2375-L2377)

```text
commit intent phase
perform external effect
commit outcome or next phase
```

`11-tasks.ts` 的 `prepare` 阶段把幂等键写进检查点（意图），`charge` 阶段用这个键扣款（副作用），再提交收据（结果）。崩溃发生在扣款之后、提交之前时，重新打开后 `charge` 再跑一次，同一个键让支付服务识别出重复。

**理由。** 存储只能保证"提交了"或"没提交"，不能保证副作用和提交一起发生。把意图先写下来，恢复时就知道"副作用可能已经发生"，可以选择重试（幂等时）、查询外部状态、或者记录中断。D5 的工具任务正是这样：`call` 阶段先提交 `{ phase: "execute", replay }`，再执行工具；恢复时停在 `execute` 的任务就是"可能已经执行过"的那种。

**代码位置。** `packages/durable/src/harness/scheduler.ts:2455-2457`（同一检查点再提交不算进度，所以意图提交不会清掉重启计数），`packages/durable/src/harness/tool.ts:194-197`（D5）。

### 6.2 完整替换的检查点，加上"没进度就 faulted"

**做法。** 检查点每次整体替换，不做增量。阶段边界上比较检查点，没变化就 faulted（规则 6）。

**理由。** 恢复只需要一条记录：任务从 `state.checkpoint` 继续，不需要重放历史。规则 6 把"阶段函数忘了提交"这种 bug 变成一个可见的 faulted 结果，而不是一个反复执行同一阶段的死循环。大量进度数据应该放进任务作用域的文档（spec §5.2 最后一段），检查点只放恢复所需的小值。

**代码位置。** `packages/durable/src/harness/scheduler.ts:2297-2301`。

### 6.3 重启计数：区分崩溃和关闭

**做法。** open 把残留的 `running` 计为一次崩溃；`close()` 写回 `pending` 不计数；任务自己提交的不同状态清零。到 `restartLimit` 时，扩展的任务被调度器写成 faulted，内置任务自己用可见的方式结束（D5）。

**理由。** 一个每次都把进程搞崩的阶段（例如内存耗尽）如果无限重试，宿主会陷入崩溃循环。只计"连续、没有进度"的崩溃，正常的长任务不会被误杀。正常关闭不计数，所以部署重启不会消耗次数。

**代码位置。** `packages/durable/src/harness/scheduler.ts:453-457`、`packages/durable/src/harness/scheduler.ts:1929-1944`、`packages/durable/src/harness/scheduler.ts:576-583`。

### 6.4 调度器不认识任务种类

**做法。** 调度器只知道阶段、检查点和状态。run、工具槽位、压缩状态这些概念都在 Harness 里，通过 `settleOutcome`、`limitsRestarts`、`withdrawInputs` 三个回调接入。

**理由。** 内置任务和扩展任务走同一套规则，扩展可以定义自己的持久任务（例如 `23-child-tasks.ts` 的支付），享受同样的恢复、中止和所有权语义。这和 M2 用 hook 把循环和产品分开（M2 §6.1）是同一个思路。

**代码位置。** `packages/durable/src/harness/harness.ts:251-253`，`packages/durable/src/harness/live.ts:410`。

### 6.5 中止是持久标记，自底向上执行

**做法。** 中止先写标记，再信号通知；abort 处理函数在一次新的调用里运行，并且要等它拥有的工作结束。级联标记由 reconcile 从持久记录派生。

**理由。** 标记持久化后，崩溃在中止过程中也不丢：重新打开时 mode 从标记推出，reconcile 补齐级联。自底向上保证父任务的 abort 处理函数看到的是子任务的最终结果，可以基于它们做补偿。

**代码位置。** `packages/durable/src/harness/scheduler.ts:593`、`packages/durable/src/harness/scheduler.ts:2034-2041`、`packages/durable/src/harness/scheduler.ts:1021`。

---

## 7. 实验

### 7.1 每次都崩溃的任务

**问题。** [`lab/m11b-crashloop.ts`](../lab/m11b-crashloop.ts) 的 `crash` 模式定义了一个任务：`prepare` 阶段提交 `{ phase: "work", step: 1 }`，`work` 阶段一开始就 `SIGKILL` 自己。Harness 设置了 `settings.restartLimit: 2`。用同一个 SQLite 文件连续运行 4 次，每次会看到什么？

**预测。**

- 第 1 次：创建任务，`prepare` 提交，同一个调用进入 `work`，进程被杀。存储里留下 `running`。
- 第 2 次：open 把它改回 `pending`，`restarts=1`（§4.2）。`1 < 2`，预留并运行，`work` 再次杀掉进程。
- 第 3 次：`restarts=2`，达到上限。这个 kind 不是内置任务，`#reserve` 直接写 `faulted`，detail `restart_limit`，`work` 不再运行；`onReport` 收到 `task_faulted`。
- 第 4 次：存储里没有活的 `lab.crashy` 任务，脚本创建一个新任务，又从头开始。

**运行。**

```bash
mkdir -p /tmp/pi-lab/m11b-crash-1
for i in 1 2 3 4; do echo "=== 第 $i 次运行"; node --conditions=source --experimental-strip-types m11b-crashloop.ts crash /tmp/pi-lab/m11b-crash-1/t.sqlite; echo "exit=$?"; done
```

```
=== 第 1 次运行
[创建] task 7
[work] 开始执行，restarts=0，马上 SIGKILL
exit=137
=== 第 2 次运行
[open 之后] task 7 status=pending restarts=1 checkpoint={"phase":"work","step":1}
[work] 开始执行，restarts=1，马上 SIGKILL
exit=137
=== 第 3 次运行
[open 之后] task 7 status=pending restarts=2 checkpoint={"phase":"work","step":1}
[结束] outcome={"status":"faulted","error":{"message":"Task 7 (lab.crashy) was cut off 2 times without progress","detail":{"reason":"restart_limit"}}}
[onReport] task_faulted reason=restart_limit Task 7 (lab.crashy) was cut off 2 times without progress
exit=0
=== 第 4 次运行
[创建] task 8
[work] 开始执行，restarts=0，马上 SIGKILL
exit=137
```

`exit=137` 是被 SIGKILL 杀掉的进程的退出码。zsh 可能还会在中间打印一行 `killed …`。

**结论。** 预测成立。第 2 次运行时检查点已经是 `work`：`prepare` 的提交在崩溃前落盘了，所以恢复从 `work` 开始，不重跑 `prepare`。`restarts` 每次 open 加 1，`work` 阶段一直没有提交，计数不会清零。

### 7.2 阶段返回但没有进度

**问题。** `noprogress` 模式下，`work` 阶段打印一行后直接返回，不调用 `runtime.commit()`。会怎样？

**预测。** 规则 6：阶段返回后的 `#step` 发现检查点和阶段开始时相同，写 `faulted`，报告 `task_faulted`，reason 是 `threw`（`#step` 里 fault 的报告都用这个 reason）。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11b-crashloop.ts noprogress
```

```
[创建] task 7
[work] 返回，但没有提交新检查点（step=1）
[结束] outcome={"status":"faulted","error":{"message":"Task lab.crashy phase work returned without durable progress"}}
[onReport] task_faulted reason=threw Task lab.crashy phase work returned without durable progress
```

**结论。** `work` 只运行了一次。错误文本就是 `#rules` 里构造的那句话（`packages/durable/src/harness/scheduler.ts:2299`）。

### 7.3 子任务、failFast 和自底向上的中止

**问题。** `23-child-tasks.ts` 的 checkout 任务在一次提交里创建 4 个支付子任务，并提交 `waiting on=[4 个子任务], policy: "failFast"`。支付任务 `charge` 阶段扣款后 `sleepUntil(+100ms)` 进入 `confirm`；卡号以 `expired` 开头时直接提交 `failed`。三个场景：一张卡过期；用户中止 checkout；进程在支付进行中关闭再重新打开。分别会输出什么？

**预测。**

- 一张卡过期：`expired-2` 提交 `failed`。`#observe` 发现 failFast 等待者的一个成员失败（`packages/durable/src/harness/scheduler.ts:819-826`），reconcile 给其余 3 个活的支付打中止标记，它们的 abort 处理函数各自退款。checkout 自己**不被**标记，4 个都终止后它被唤醒，`outcomes()` 读到 `aborted, failed, aborted, aborted`，提交 `failed`。
- 用户中止：任务图里 checkout 是 `waiting`，4 个支付在 `sleeping`。`abortTask(checkout)` 标记 checkout；reconcile 级联标记 4 个支付；中止标记会结束睡眠。支付的 abort 先跑（自底向上），最后才打印 `checkout aborted`。
- 关闭再打开：`close()` 不写任何结果，睡眠的支付保持 `sleeping`，checkout 保持 `waiting`。重新打开后等它们到期继续，全部 `completed`。

**运行。**

```bash
cd $PI/packages/durable
node --conditions=source --experimental-strip-types test/examples/23-child-tasks.ts
```

```
One card is declined:
  payment visa-1 aborted, refunded
  payment visa-3 aborted, refunded
  payment visa-4 aborted, refunded
  payments: aborted, failed, aborted, aborted
  checkout: failed
The customer cancels:
  example.checkout 12: waiting on 13, 14, 15, 16
    example.payment 13: sleeping
    example.payment 14: sleeping
    example.payment 15: sleeping
    example.payment 16: sleeping
  payment visa-5 aborted, refunded
  payment visa-6 aborted, refunded
  payment visa-7 aborted, refunded
  payment visa-8 aborted, refunded
  checkout aborted
  checkout: aborted
The process stops while the payments run, and a new one continues:
  payments: completed, completed, completed, completed
  checkout: completed
```

**结论。** 三个预测都成立。第二个场景里 `checkout aborted` 在 4 行退款之后，这就是 §4.10 第 3 点：abort 调用要等拥有的工作结束才预留。第三个场景说明睡眠和等待都是持久状态，跨进程有效。

---

## 8. 自测与衔接

### 8.1 自测

1. `12-recovery.ts` 里，如果把 `tick` 阶段的 memo 去掉，§2.2 的输出会有什么不同？
2. 一个阶段先 `runtime.commit(() => ({ status: "running", checkpoint: same }))`（检查点不变），再执行外部调用，然后进程崩溃。重启计数会被这次提交清零吗？
3. 父任务 P 提交了 `completed`，但它的子任务 C 还在运行。P 的记录是什么状态？`waitForTask(P)` 什么时候返回？

<details>
<summary>答案</summary>

1. 重新打开后 `n: 2` 的阶段会再打印一次 `tick 2`，输出变成 `tick 1, tick 2, tick 2, tick 3, …`。关闭发生在打印之后、下一个检查点提交之前，阶段从头重跑（§2.2）。
2. 不会。`#commitState` 用 `jsonEqual` 比较新旧状态，相同时保留 `current`（带着 `restarts`），只有不同的状态才调用 `progressed()` 删掉计数（`packages/durable/src/harness/scheduler.ts:2455-2457`）。
3. P 存成 `{ status: "completing", outcome: completed }`（`packages/durable/src/harness/scheduler.ts:2445-2451`）。`completed` 不是取消意图，C 正常跑完。C 终止后，reconcile 的 `#finalize()` 写出 P 的 `terminal` 记录，等待者此时才被唤醒（spec §5.5 规则 5：等待者把 `completing` 看作活的）。

</details>

### 8.2 衔接

本篇讲的是任意任务都遵守的规则。D5 从内置的 `GenerationTask`（`packages/durable/src/harness/generation.ts:162`）读起：它把 M2 的 `runLoop` 拆成 `prepare`、`request`、`poll`、`retry`、`tools`、`finish`、`drain` 七个阶段；`ToolTask` 用 `call` → `execute` 两个检查点实现本篇 §6.1 的 effect sandwich，并按 `replay` 决定恢复时是否重跑。
