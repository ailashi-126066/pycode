# M11 durable · D1 概念：Harness、对话、条目、文档、任务、submission

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](../README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

M11 分成 6 篇：

| 篇 | 内容 |
|---|---|
| **D1 概念**（本篇） | 核心记录类型，`Harness.open()`、`root()`、`submit()` 的上层流程，和 M2 内存循环的对照 |
| [D2 chord](D2-chord.md) | durable 用到的 chord 三块：`chord/context`、`chord/delta`、`chord/sqlite` |
| [D3 存储与事务](D3-storage-transactions.md) | 一次 commit 从回调到存储再到发布的全过程 |
| [D4 任务与调度](D4-tasks-scheduler.md) | 任务定义、检查点、调度器、恢复、durable sleep |
| [D5 内置任务](D5-builtin-tasks.md) | `pi.generation`、`pi.tool`、inbox、submission、压缩 |
| [D6 扩展与观察](D6-extensions-observation.md) | `pi.live`、对话视图、code mode 的确定性重放 |

---

## 1. 这个模块是什么

### 1.1 职责

pi-durable（源码目录 `packages/durable`）是另一个 agent harness。它和 M2 的 agent 循环做同一件事：把 transcript 发给模型，执行模型调用的工具，把结果写回 transcript，再请求模型。区别在于 durable 把每一步都先写进存储，再让外界看到。进程在任何时刻被杀掉，重新打开同一份存储后，工作从上次存储的位置继续。

spec 开头把这条规则写成一句话：

源码：[`packages/durable/docs/spec.md:44-46`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/docs/spec.md#L44-L46)

```md
> A Session atomically commits immutable entries, full task records, and
> Chord-tracked documents. Only committed state is observable. A commit marked
> deferred may be observed before it is stored (section 4.1).
```

durable 不依赖 pi-agent-core，也不依赖 coding-agent。它的依赖是 pi-ai（模型访问）、chord（文档状态，D2）、pi-codemode 和 pi-telemetry：

```bash
cd $PI/packages/durable && node -e 'console.log(Object.keys(require("./package.json").dependencies).join("\n"))'
```

```
@earendil-works/chord
@earendil-works/pi-ai
@earendil-works/pi-codemode
@earendil-works/pi-telemetry
@silvia-odwyer/photon
diff
typebox
```

coding-agent 的源码里也没有引用 durable。所以 durable 是和 M2 + M8 平行的另一套实现，不是 pi 命令行的一部分：

```bash
cd $PI/packages && grep -rl 'pi-durable' coding-agent/src agent/src | wc -l
```

```
       0
```

### 1.2 本教程的用词

durable 的几个名字和 pi 其他包撞车。本模块固定这样称呼：

| durable 类型 | 本文用词 | 说明 |
|---|---|---|
| `Session` | Session | 一份存储上的提交线、记录和文档。和 M4 的会话文件无关 |
| `Harness` | Harness | Session 加上对话句柄、registry 和任务调度器 |
| `Conversation` | 对话 | 一个 transcript 的范围。不叫"会话"，避免和 M4 的会话文件混淆 |
| `EntryRecord` | 条目 | 对话里一条不可变的记录。和 M4 的会话条目（`SessionEntry`）是两种东西 |
| document | 文档 | 和 transcript 放在一起的可变 JSON 状态 |
| `TaskRecord` | 任务 | 一个持久化的状态机 |
| `SubmissionRecord` | submission | 交给对话的输入或写入，可以等它完成 |
| `Session.commit()` | commit | 一次原子写入。不翻译成"提交"，避免和 submission 混淆 |

### 1.3 在调用链中的位置

README §2 的调用链属于 coding-agent，durable 不在里面。下面是 durable 跑主线场景"读 README.md，总结成三句话"时的调用链，方括号里是讲它的那一篇：

```
Harness.open(storage, { models, registry })               [D1] ← 本篇入口
├─ new HarnessImpl()
│   ├─ super() → SessionImpl 构造函数：缓存、写入器、提交线     [D3]
│   ├─ new TaskScheduler()                                 [D4]
│   └─ new Submissions()                                   [D5]
└─ openTasks() → TaskScheduler.open()  把上次没跑完的 running 改回 pending [D4]
harness.root(context, { agent })                           [D1]
└─ #create() → commitWith()                                [D3]
    ├─ tx.createRootConversation()
    ├─ conversationCreated()  建 pi.live / pi.inbox / pi.usage / pi.provider / pi.agent
    └─ configure()  把 agent 选择写进 pi.agent
root.submit({ type: "input", content })                    [D1]
└─ Submissions.submit() → commitWith(admitSubmission)      [D5]
    └─ pi.user 条目 + submission(placed) + pi.generation 任务(pending)
submission.wait()                                          [D1]
└─ 调度器运行任务                                           [D4]
    ├─ pi.generation：写 pi.system、请求模型、写 pi.assistant [D5] → M1
    ├─ pi.tool：执行 read，写 pi.tool-result                 [D5]
    └─ pi.generation：再请求模型，写最终 pi.assistant，submission done [D5]
```

每一行写操作都是一次 commit。§2 会看到这次运行一共调用了 13 次 `Storage.commit()`。

### 1.4 文件清单

durable 的 `src` 有 33,594 行 TypeScript，`docs/spec.md` 有 6,531 行。M11 六篇只读和 agent 机制有关的部分。本篇读的文件：

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/durable/src/harness/harness.ts` | 611 | `Harness.open()`、`HarnessImpl`、`ConversationImpl` | 精读，§4 的主体 |
| `packages/durable/src/types.ts` | 1445 | Session 层的记录类型、`Tx`、`Storage` | §3 读记录类型，D3 读 `Tx` 和 `Storage` |
| `packages/durable/src/harness/types.ts` | 1070 | Harness 层的公共类型：`Conversation`、`Harness`、`SubmissionDraft` | 选读 |
| `packages/durable/src/entries.ts` | 34 | 内置条目种类 `pi.user`、`pi.assistant` 等 | 通读 |
| `packages/durable/src/harness/submissions.ts` | 317 | `submit()` 和 `wait()` | 只读入口，细节在 D5 |
| `packages/durable/README.md` | 973 | 用户文档 | 读 Concepts、Persist and Resume |
| `packages/durable/docs/spec.md` | 6531 | 规范 | 读 §1、§2 |

行数用 `wc -l` 统计。其余文件在 D2 到 D6 读。不读的部分：`env/`（执行环境）、`images/`、`tools/`（内置的 read/bash/edit/write，结构和 M3 相同）、`storage/jsonl`、`storage/indexeddb`、`storage/store`、`testing/`。

### 1.5 读完能回答的问题

1. durable 的 transcript 由什么组成？它和 M2 的 `AgentMessage[]` 有什么不同？
2. 一次 `submit()` 返回时，模型被调用了吗？返回值保证了什么？
3. 对话的模型、工具选择存在哪里？为什么只存名字？
4. 进程重启后，同一个 `requestId` 再 submit 一次会怎样？
5. M2 里循环的"状态"在内存中，durable 里这些状态分别存在哪种记录里？

---

## 2. 先跑一次

### 2.1 主线场景：每一次 Storage.commit()

[`lab/m11a-durable.ts`](../lab/m11a-durable.ts) 用 durable 跑主线场景。faux 模型的两次回复和 README §4.3 的 `faux-ext.ts` 相同：第 1 次是一段文本加一个 `read` 工具调用，第 2 次是总结。工具是一个读 `$WORK` 目录文件的 `read`。存储是 `MemoryStorage`，外面包了一层代理，每次 `Storage.commit()` 被调用时打印它收到的写入：

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts main
```

```
== Harness.open()
== harness.root()
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #1 {"id":1}
    document.create #2 pi.live base
    document.create #3 pi.inbox base
    document.create #4 pi.usage base
    document.create #5 pi.provider base
    document.create #6 pi.agent base
== root.submit()
[Storage.commit 第 2 次] 1 个 batch
  seq=2
    entry #7 pi.user model=[user]
    submission #8 input placed
    task #9 pi.generation pending phase=prepare
    document.change #2 pi.live base
== submit() 返回 Submission id=8，开始 wait()
[Storage.commit 第 3 次] 1 个 batch
  seq=3
    task #9 pi.generation running phase=prepare
[Storage.commit 第 4 次] 1 个 batch
  seq=4
    entry #10 pi.system model=[system] byTaskId=9
    task #9 pi.generation running phase=request
[Storage.commit 第 5 次] 1 个 batch
  seq=5
    entry #11 pi.assistant model=[assistant] byTaskId=9
    task #9 pi.generation waiting phase=tools
    task #12 pi.tool pending phase=call owner=9
    document.change #2 pi.live base
    document.change #4 pi.usage base
[Storage.commit 第 6 次] 1 个 batch
  seq=6
    task #12 pi.tool running phase=call owner=9
[Storage.commit 第 7 次] 1 个 batch
  seq=7
    task #12 pi.tool running phase=execute owner=9
[Storage.commit 第 8 次] 1 个 batch
  seq=8
    entry #13 pi.tool-result model=[toolResult] byTaskId=12
    task #12 pi.tool terminal outcome=completed owner=9
    document.change #2 pi.live base
[Storage.commit 第 9 次] 1 个 batch
  seq=9
    task #9 pi.generation running phase=tools
[Storage.commit 第 10 次] 1 个 batch
  seq=10
    task #14 pi.generation pending phase=prepare
    task #9 pi.generation terminal outcome=completed
    document.change #2 pi.live base
[Storage.commit 第 11 次] 1 个 batch
  seq=11
    task #14 pi.generation running phase=prepare
[Storage.commit 第 12 次] 1 个 batch
  seq=12
    task #14 pi.generation running phase=request
[Storage.commit 第 13 次] 1 个 batch
  seq=13
    entry #15 pi.assistant model=[assistant] byTaskId=14
    submission #8 input done
    task #14 pi.generation terminal outcome=completed
    document.change #2 pi.live base
    document.change #4 pi.usage base
== wait() 返回 {"conversationId":1,"requestId":"main-1","type":"input","status":"done","entry":7,"id":8,"answer":15}
== harness.close()
```

几点观察：

- `Harness.open()` 没有写任何东西。`root()` 的一次 commit 建了对话 #1 和 5 个文档。
- `submit()` 返回前只做了一次 commit（seq=2）：`pi.user` 条目、状态为 `placed` 的 submission、一个 `pending` 的 `pi.generation` 任务。这时模型还没有被调用。`submit()` 保证的是"输入已经持久化"，不是"已经回答"。
- 后面 11 次 commit 都是任务在推进。每个任务的每一步（`prepare`、`request`、`tools`、`call`、`execute`）都先把状态写进任务记录，再做下一步。D4 讲为什么。
- 所有 ID（对话、条目、submission、任务、文档）共用一个数字序列：1、2、…、15。
- `pi.tool` 任务 #12 的 `owner=9`：工具调用归它的 `pi.generation` 所有。第 1 个 generation（#9）结束时（seq=10）创建了第 2 个 generation（#14），后者接着跑下一轮。M2 里这是 `runLoop` 内层 `while` 的下一次迭代。

### 2.2 同一场景在 M2 里的样子

M2 的 [§2.1](../M2-agent-core.md#21-主线场景里-m2-发出的事件) 里，同一个场景产生 31 到 33 个事件，没有一次写存储（存储由 M8 在 `message_end` 时做）。下表把两边对齐：

| M2（`runLoop`，内存） | durable（任务，每步一次 commit） |
|---|---|
| `agent.prompt(messages)` | `root.submit()`：seq=2 写 `pi.user` 条目和 `pi.generation` 任务 |
| `declareToolChanges()` 插入 system 消息 | seq=4：`pi.system` 条目 |
| `streamAssistantResponse()` → `message_end (assistant)` | seq=5：`pi.assistant` 条目 |
| `executeToolCalls()` → `tool_execution_start/end` | seq=5 创建 `pi.tool` 任务，seq=6–8 执行并写 `pi.tool-result` 条目 |
| `turn_end` → 内层 `while` 下一轮 | seq=10：第 1 个 generation 结束，第 2 个 generation 创建 |
| `agent_end` | seq=13：最终 `pi.assistant` 条目，submission 变为 `done` |

M2 的事件是给内存里的订阅者看的。durable 没有事件流，外界看到的是已经存储的记录和文档（D6 讲怎样观察它们）。

### 2.3 关掉再打开：12-recovery 示例

durable 自带的示例 `test/examples/12-recovery.ts` 定义了一个从 1 数到 5 的任务，数到 2 时关闭 Harness，再用新的 Harness 打开同一个 SQLite 文件：

```bash
cd $PI/packages/durable && node --conditions=source --experimental-strip-types test/examples/12-recovery.ts
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

第二个 Harness 没有任何"恢复"代码，只调用了 `waitForTask()`。任务从存储里的检查点 `{ phase: 'tick', n: 2 }` 继续。tick 2 的打印没有重复，是因为任务在打印之前把意图写成了 memo（D4 的"effect sandwich"）。

---

## 3. 数据结构

durable 把 M2 放在内存里的东西拆成五种持久化记录：对话、条目、任务、submission、文档。下面的真实实例都来自 `m11a-durable.ts records`，它跑完 §2.1 的场景后把各类记录读出来：

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts records
```

```
[ConversationRecord] {"id":1}
[EntryRecord] 共 5 条
  #7 pi.user        model=[user] byTaskId=-
  #10 pi.system      model=[system] byTaskId=9
  #11 pi.assistant   model=[assistant] byTaskId=9
  #13 pi.tool-result model=[toolResult] byTaskId=12
  #15 pi.assistant   model=[assistant] byTaskId=14
[EntryRecord] 第一条 pi.assistant： {"model":[{"role":"assistant","content":[{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_1","name":"read","arguments":{"path":"README.md"}}],"api":"faux:1793528610566:2kjnf72tpz4","provider":"faux","model":"faux-1","usage":{"input":0,"output":10,"cacheRead":0,"cacheWrite":43,"totalTokens":53,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"toolUse","timestamp":1793528610566,"thinkingLevel":"off"}],"kind":"pi.assistant","id":11,"conversationId":1,"byTaskId":9}
[TaskRecord] 共 3 个
  #9 pi.generation  owner=- terminal outcome=completed
  #12 pi.tool        owner=9 terminal outcome=completed
  #14 pi.generation  owner=- terminal outcome=completed
[TaskRecord] pi.tool： {"id":12,"conversationId":1,"kind":"pi.tool","version":3,"input":{"kind":"model","assistant":11,"callId":"call_1"},"owner":9,"background":false,"abortRequested":false,"state":{"status":"terminal","outcome":{"status":"completed","result":{"kind":"model","entryId":13}}},"startedAt":1793528610580,"endedAt":1793528610583}
[TaskRecord] 第一个 pi.generation： {"id":9,"conversationId":1,"kind":"pi.generation","version":2,"input":{},"background":false,"abortRequested":false,"state":{"status":"terminal","outcome":{"status":"completed","result":{"entryId":11}}},"startedAt":1793528610574,"endedAt":1793528610584}
[SubmissionRecord] {"conversationId":1,"requestId":"main-1","type":"input","status":"done","entry":7,"id":8,"answer":15}
[文档 pi.agent] {"model":{"provider":"faux","modelId":"faux-1"}}
[文档 pi.provider] {"sessionId":"01a196a2-bf09-71c3-bea2-7a3ac4d78fd2"}
[文档 pi.live] {}
[文档 pi.inbox] {"items":[]}
[文档 pi.usage] {"models":{"faux/faux-1":{"input":0,"output":19,"cacheRead":43,"cacheWrite":79,"totalTokens":141,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}}},"tools":{}}
[DocumentRecord]
   {"id":2,"kind":"pi.live","scope":{"kind":"conversation","conversationId":1},"history":"latest","fork":"initial","createdAt":1}
   {"id":3,"kind":"pi.inbox","scope":{"kind":"conversation","conversationId":1},"history":"latest","fork":"initial","createdAt":1}
   {"id":4,"kind":"pi.usage","scope":{"kind":"conversation","conversationId":1},"history":"latest","fork":"initial","createdAt":1}
   {"id":5,"kind":"pi.provider","scope":{"kind":"conversation","conversationId":1},"history":"latest","fork":"initial","createdAt":1}
   {"id":6,"kind":"pi.agent","scope":{"kind":"conversation","conversationId":1},"history":"rewindable","fork":"asOf","createdAt":1}
```

### 3.1 ConversationRecord：对话

源码：[`packages/durable/src/types.ts:426-439`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L426-L439)

```ts
/** Immutable identity, history ancestry, and task ownership of a transcript scope. */
export type ConversationRecord = {
	readonly id: ConversationId;
	/** Fork source and inclusive parent entry through which history is inherited. */
	readonly parent?: {
		readonly conversationId: ConversationId;
		readonly at: EntryId;
	};
	/** Creator edge used for attribution, subtree abort, and subtree idle waits. */
	readonly owner?: {
		readonly conversationId: ConversationId;
		readonly taskId: TaskId;
	};
};
```

| 字段 | 说明 |
|---|---|
| `id` | 对话 ID。根对话固定是 `ROOT_CONVERSATION_ID`，即 `1`（`packages/durable/src/types.ts:40`） |
| `parent` | fork 的来源：父对话和 fork 点的条目 ID。子对话看到父对话到 `at` 为止的条目 |
| `owner` | 创建它的任务。子 agent（D5）的对话由一个任务拥有，用于级联中止和等待空闲 |

**真实实例**：`{"id":1}`。根对话没有父对话，也没有所有者。

对话记录只有身份和两条边，不保存 transcript，也不保存配置。`Conversation` 句柄同样不带状态（`packages/durable/src/harness/harness.ts:112-119`），比较两个句柄要比较 `id`。

### 3.2 EntryRecord：条目

源码：[`packages/durable/src/types.ts:457-473`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L457-L473)

```ts
/** Immutable transcript event with separate model-facing and application-facing payloads. */
export type EntryRecord = {
	readonly id: EntryId;
	readonly conversationId: ConversationId;
	/** Application-defined entry discriminator. */
	readonly kind: string;
	/** Messages contributed to model context; absent for display or bookkeeping entries. */
	readonly model?: readonly Message[];
	/** JSON payload consumed by views, extensions, or bookkeeping logic. */
	readonly data?: JsonValue;
	/** First entry in the active context selected by this entry. */
	readonly head?: EntryId;
	/** Context-only overrides of earlier visible entries. */
	readonly edits?: readonly ContextEdit[];
	/** Task that appended this entry, when it was produced by durable work. */
	readonly byTaskId?: TaskId;
};
```

| 字段 | 说明 |
|---|---|
| `id` | 全局递增的 ID，也是条目的顺序 |
| `conversationId` | 所属对话 |
| `kind` | 条目种类。内置种类见下面的 `entries.ts`；应用可以用自己的种类 |
| `model` | 这个条目贡献给模型的 LLM 消息（M1 的 `Message`）。没有它的条目模型看不到 |
| `data` | 给界面、扩展和簿记看的 JSON |
| `head` | 有它的条目开启一段新的上下文：模型只看从 `head` 指向的条目开始的部分（§4.6） |
| `edits` | 对之前条目的替换或省略，只影响模型上下文 |
| `byTaskId` | 写入这个条目的任务 |

内置条目种类：

源码：[`packages/durable/src/entries.ts:14-34`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/entries.ts#L14-L34)

```ts
/** User input: `model` is `[UserMessage]`. Written by submissions. */
export const UserEntry = defineEntry("pi.user");
/** Provider result with any stop reason: `model` is `[AssistantMessage]`. Written by generation. */
export const AssistantEntry = defineEntry("pi.assistant");
/** Positional prompt and tool change: `model` is `[SystemMessage]` with empty `content`. */
export const SystemEntry = defineEntry("pi.system");
/**
 * Tool result: `model` is `[ToolResultMessage]`, whose content ends with the rendered diagnostics block; `data` holds
 * the structured diagnostics, possibly none. Written by tool tasks, and by generation for calls it did not offer.
 */
export const ToolResultEntry = defineEntry<{ diagnostics: ToolDiagnostic[] }>("pi.tool-result");
/**
 * Start of a new context: always `head: "self"`, with `model` absent for a plain reset or `[UserMessage]` carrying the
 * handoff text. Written by `Conversation.reset()` and the `handoff` tool control.
 */
export const ResetEntry = defineEntry("pi.reset");
/**
 * Compaction summary: `model` is `[UserMessage]` with the wrapped summary, `head` the first kept entry. Written by
 * compaction tasks, directly or through a write submission.
 */
export const CompactionEntry = defineEntry<{ reason: CompactionReason }>("pi.compaction");
```

**真实实例**：§3 开头输出里的第一条 `pi.assistant`：

```
{"model":[{"role":"assistant","content":[{"type":"text","text":"我先读一下 README。"},{"type":"toolCall",…}],…,"stopReason":"toolUse",…}],"kind":"pi.assistant","id":11,"conversationId":1,"byTaskId":9}
```

`model` 里就是 M1 的 `AssistantMessage`。和 M2 对比：M2 的 transcript 是 `AgentMessage[]`，用 `role` 区分消息；durable 的 transcript 是条目列表，用 `kind` 区分条目，模型消息是条目的一个字段。条目一旦写入就不再修改（spec §1 不变式 5）。

### 3.3 TaskRecord：任务

源码：[`packages/durable/src/types.ts:750-764`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L750-L764)

```ts
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

`TaskRecordBase` 是所有状态共有的字段：

源码：[`packages/durable/src/types.ts:705-747`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L705-L747)

```ts
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
```

`state` 是下面六种之一：

源码：[`packages/durable/src/types.ts:658-702`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L658-L702)

```ts
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

| 字段 | 说明 |
|---|---|
| `id`、`conversationId` | 任务 ID 和它所在的对话 |
| `kind`、`version` | 任务定义的名字和版本。代码不存进存储，打开时按名字到 registry 里找 |
| `input` | 创建时的输入，不再改变 |
| `owner` | 拥有它的任务；没有就是对话拥有 |
| `state.status` | `pending` 等待调度、`running` 正在执行、`waiting` 等子任务、`sleeping` 等时间、`completing` 结果已定但子任务未完、`terminal` 结束 |
| `state.checkpoint` | 任务从这里继续的完整状态，必须带 `phase` 字段 |
| `state.outcome` | 结束时的结果：`completed`、`failed`、`aborted`、`orphaned`、`faulted` |
| `memos` | 先写者赢的小值，用来记录"这个副作用已经做过了"（§2.3） |
| `restarts` | 连续被崩溃打断的次数，到 `restartLimit` 时任务结束（D4） |

**真实实例**：工具任务 #12：

```
{"id":12,"conversationId":1,"kind":"pi.tool","version":3,"input":{"kind":"model","assistant":11,"callId":"call_1"},"owner":9,"background":false,"abortRequested":false,"state":{"status":"terminal","outcome":{"status":"completed","result":{"kind":"model","entryId":13}}},…}
```

`input` 只记录"第 11 号条目里 id 为 `call_1` 的工具调用"，结果只记录"写进了第 13 号条目"。工具参数和输出都在条目里，任务记录里只有引用。任务的定义、调度和检查点在 D4 读。

### 3.4 SubmissionRecord：submission

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

（`type: "write"` 和 `type: "continue"` 的分支结构相同，到 `packages/durable/src/types.ts:599` 结束。）

| 字段 | 说明 |
|---|---|
| `type` | `input` 是用户输入，会启动或加入一次运行；`write` 是被动写入一个条目；`continue` 是不带新输入地再跑一次 |
| `status` | `queued` 排队、`placed` 已写进 transcript、`done` 已回答、`unanswered` 不会再有回答 |
| `entry` | 放进 transcript 的那个条目（输入的 `pi.user`） |
| `answer` | 回答它的那个 `pi.assistant` 条目 |
| `requestId` | 宿主给的去重键，在一个对话内唯一（§7.1） |

**真实实例**：

```
{"conversationId":1,"requestId":"main-1","type":"input","status":"done","entry":7,"id":8,"answer":15}
```

`entry: 7` 是 `pi.user`，`answer: 15` 是最后的 `pi.assistant`。M2 没有对应的类型：`agent.prompt()` 的 Promise 就是这次输入的全部生命周期，进程退出就没了。

### 3.5 文档：DocDefinition 和 DocumentRecord

文档是和 transcript 放在一起、在 commit 里修改的 JSON 对象。一个文档的行为由它的定义决定：

源码：[`packages/durable/src/types.ts:43-82`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L43-L82)

```ts
export type LatestConversationSemantics = {
	readonly scope: "conversation";
	readonly history: "latest";
	readonly fork: "current" | "initial";
};

/** Conversation document whose history remains addressable for as-of reads. */
export type RewindableConversationSemantics = {
	readonly scope: "conversation";
	readonly history: "rewindable";
	readonly fork: "asOf" | "current" | "initial";
};

/** Ownership and lifetime of a document; only conversation documents declare history and fork behavior. */
export type DocumentSemantics =
	| { readonly scope: "session"; readonly history?: never; readonly fork?: never }
	| LatestConversationSemantics
	| RewindableConversationSemantics
	| { readonly scope: "task"; readonly history?: never; readonly fork?: never };

/** Stored replay state supplied to a document's checkpoint predicate. */
export type CheckpointInfo = {
	/** Deltas already stored after the newest base, excluding the change being evaluated. */
	readonly deltasSinceBase: number;
};

/** Definition fields shared by singleton documents and document families. */
export type CommonDocDefinition<T extends JsonObject> = {
	/** Stable persisted kind; part of the public protocol. */
	readonly kind: string;
	/** Positive integer version of the stored value shape. */
	readonly version: number;
	initial(): T;
	migrate?(value: JsonObject, fromVersion: number): T;
	/** Required policy: return true to store this ordinary change as a complete base instead of a delta. */
	checkpointWhen(value: Readonly<T>, ops: readonly Op[], info: CheckpointInfo): boolean;
};

/** Singleton document definition. */
export type DocDefinition<T extends JsonObject> = CommonDocDefinition<T> & DocumentSemantics;
```

| 字段 | 说明 |
|---|---|
| `kind`、`version` | 文档种类和值的形状版本 |
| `scope` | 属于谁：`session`（整个 Session）、`conversation`（一个对话）、`task`（一个任务，任务结束时一起退役） |
| `history` | 对话文档才有。`latest` 只保留当前值；`rewindable` 保留历史，可以问"在某个条目时它是什么值" |
| `fork` | 对话被 fork 时子对话拿到什么：`asOf` fork 点时的值、`current` 当前值、`initial` 重新初始化 |
| `initial()`、`migrate()` | 初始值和旧版本迁移 |
| `checkpointWhen()` | 每次修改时决定存完整值（base）还是只存操作（delta），D3 读 |

存储里每个文档实例有一条 `DocumentRecord`：

源码：[`packages/durable/src/types.ts:767-803`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L767-L803)

```ts
export type DocumentRecord = {
	/** Unique incarnation ID; never reused when the same logical document is recreated. */
	readonly id: DocumentId;
	/** Stable document definition kind. */
	readonly kind: string;
	/** Family member key; absent for singleton documents. */
	readonly key?: string;
	/** Commit that created the incarnation, stamped by storage. */
	readonly createdAt: Seq;
	/** Commit that retired the incarnation; absent while it is current. */
	readonly retiredAt?: Seq;
} & (
	| {
			readonly scope: { readonly kind: "session" };
			readonly history?: never;
			readonly fork?: never;
	  }
	| ({ readonly scope: { readonly kind: "conversation"; readonly conversationId: ConversationId } } & (
			| {
					/** Retain only current state. */
					readonly history: "latest";
					/** Initialize a fork from current source state or the definition's initial value. */
					readonly fork: "current" | "initial";
			  }
			| {
					/** Retain history needed for as-of reads. */
					readonly history: "rewindable";
					/** Initialize a fork at its cutoff, from current state, or from the initial value. */
					readonly fork: "asOf" | "current" | "initial";
			  }
	  ))
	| {
			readonly scope: { readonly kind: "task"; readonly taskId: TaskId };
			readonly history?: never;
			readonly fork?: never;
	  }
);
```

**真实实例**：§3 开头输出的 `[DocumentRecord]` 部分。Harness 给每个对话建 5 个内置文档：

| 文档 | 内容（真实值） | 定义 |
|---|---|---|
| `pi.agent` | `{"model":{"provider":"faux","modelId":"faux-1"}}`，对话的模型、工具、扩展选择，只存名字 | `packages/durable/src/harness/agent.ts:44` |
| `pi.provider` | `{"sessionId":"…"}`（UUIDv7，每次不同），发给 provider 的会话 ID，用于 prompt cache | `packages/durable/src/harness/provider.ts:12` |
| `pi.live` | 运行中的 generation 和工具；空闲时是 `{}` | `packages/durable/src/harness/live.ts:194`，D6 |
| `pi.inbox` | 对话忙时排队的输入 | `packages/durable/src/harness/inbox.ts:21`，D5 |
| `pi.usage` | 按模型和工具累计的 token 用量 | `packages/durable/src/harness/usage.ts:14` |

只有 `pi.agent` 是 `rewindable` + `asOf`：fork 一个对话时，子对话拿到 fork 点那一刻的 agent 选择。其余四个都是 `latest` + `initial`，fork 时重新初始化。

### 3.6 类型关系图

```
 Session（一条 commit 线，一份 Storage）
 │
 ├─ ConversationRecord #1 ─────────────── parent? ──▶ 另一个对话（fork）
 │    │                    └──────────── owner?  ──▶ TaskRecord（子 agent）
 │    │
 │    ├─ EntryRecord #7  pi.user        model=[user]
 │    ├─ EntryRecord #10 pi.system      model=[system]      byTaskId=9
 │    ├─ EntryRecord #11 pi.assistant   model=[assistant]   byTaskId=9
 │    ├─ EntryRecord #13 pi.tool-result model=[toolResult]  byTaskId=12
 │    ├─ EntryRecord #15 pi.assistant   model=[assistant]   byTaskId=14
 │    │       ▲ entry / answer
 │    ├─ SubmissionRecord #8 input done ── entry=7, answer=15
 │    │
 │    ├─ TaskRecord #9  pi.generation ──owns──▶ TaskRecord #12 pi.tool
 │    ├─ TaskRecord #14 pi.generation
 │    │
 │    └─ 文档（DocumentRecord + base/delta）
 │         pi.agent  pi.provider  pi.live  pi.inbox  pi.usage
 │
 └─ Session / 任务范围的文档
```

transcript 是条目的有序列表。任务记录描述"正在做什么、做到哪一步"，文档描述"现在的配置和进度"。三者在同一个 commit 里一起改变（spec §1 不变式 1）。

---

## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。

```
Harness.open()                                  §4.1
└─ new HarnessImpl()                            §4.2
    └─ super() → SessionImpl                    → D3
└─ openTasks() → TaskScheduler.open()           → D4
harness.root()                                  §4.3
└─ #create() → commitWith()                     §4.3（commitWith → D3）
    ├─ tx.createRootConversation()              → D3
    └─ conversationCreated() / createAgent()    §4.4
conversation.submit()                           §4.5
└─ Submissions.submit() → admitSubmission()     §4.5（细节 → D5）
submission.wait()                               §4.5
conversation.context()                          §4.6
```

### 4.1 Harness.open()

**调用位置**：宿主代码。`m11a-durable.ts` 里是 `await Harness.open(storage, { models, registry }, context)`。

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

`open` 接受三个参数：

- `storage`：实现了 `Storage` 接口的后端（D3）。`MemoryStorage` 把一切放在内存，`openNodeSqliteStorage(path)` 用 SQLite 文件。
- `options`：至少要有 `models`（pi-ai 的模型访问，M1）和 `registry`（本进程装了哪些扩展）。
- `context`：chord 的 `Context`，带取消信号和 telemetry span（D2）。durable 的每个异步调用都要传它。示例里都用 `BACKGROUND_CONTEXT`，表示永不取消。

第一件事是检查 registry 里有没有三个内置任务。`BUILTIN_TASKS` 定义在 `packages/durable/src/harness/registry.ts:10`，是 `GenerationTask`、`ToolTask`、`CompactionTask`。存储里的任务记录只有名字（`kind`），打开时要靠 registry 找回代码。没有内置任务，之前存下的 `pi.generation` 任务就无法继续，所以直接报错。`createRegistry()` 创建的 registry 总是带着它们。§7.3 的实验验证这一点。

然后构造 `HarnessImpl`，再调用 `openTasks()`。后者转到 `TaskScheduler.open()`（D4），把上一个进程留下的 `running` 任务改回 `pending`。这一步失败时，`open` 用去掉取消信号的 context 关闭 Harness，再把原来的错误抛出去。

注意 `open` 不会启动任务调度。调度要等 `resume()`，或者等第一个"要求进展"的调用（`submit()`、`wait()`、`waitForTask()` 等）。这样宿主可以在任何任务运行之前先装好扩展（spec §2.2）。

**往哪走**：§4.2 构造函数。

### 4.2 HarnessImpl 构造函数

**调用位置**：`Harness.open()` → `new HarnessImpl(storage, options, context)`。

源码：[`packages/durable/src/harness/harness.ts:208-275`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/harness.ts#L208-L275)

```ts
/** Session kernel extended with conversation handles and a registry. */
class HarnessImpl<Tool extends ToolRegistration> extends SessionImpl implements HarnessType {
	readonly #options: HarnessOptions<Tool>;
	readonly #hostSettings: () => HarnessSettings | undefined;
	readonly #report: (error: unknown, info: ReportInfo, context?: Context) => void;
	readonly #host: ConversationHost<Tool>;
	readonly #tasks: TaskScheduler;
	readonly #submissions: Submissions;
	readonly #taskGraph: TaskGraphView;

	constructor(rawStorage: Storage, options: HarnessOptions<Tool>, context: Context) {
		const report = safeReport(options.onReport, options.telemetry ?? NOOP_TELEMETRY_CONTEXT);
		const now = safeNow(options.now, report);
		const hostSettings = currentSettings(options.settings);
		// Read at each use, as all settings are, but only these fields: they are read on every commit.
		super(
			rawStorage,
			now,
			() => hostSettings()?.documentRetentionMs,
			options.setTimer,
			options.readOnly === true,
			() => hostSettings()?.progress?.persistWithinMs,
		);
		// Every component reads and writes through the Session's failure guard: `rawStorage` serves only `super()`.
		const storage = this.storage;
		this.#options = options;
		this.#report = report;
		this.#hostSettings = hostSettings;
		const settings = () => resolveSettings(hostSettings());
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
		this.#host = {
			harness: this,
			storage,
			tasks: this.#tasks,
			submissions: this.#submissions,
			views: new ConversationViews(this),
			now,
			create: (target, createOptions, context) => this.#create(target, createOptions, context),
		};
	}
```

**讲解**

`HarnessImpl` 继承 `SessionImpl`。`super(...)` 建起 Session 的全部机制：读缓存、写入器、提交线（D3）。传进去的几个设置是函数，比如 `() => hostSettings()?.documentRetentionMs`，每次用的时候才读，所以宿主改了设置不需要重新打开 Harness。

构造完 Session 之后装三个组件：

- `TaskScheduler`（D4）：运行任务。它拿到的 `agent`、`settings`、`env`、`models` 都是函数，任务运行时再解析。
- `Submissions`（D5）：`submit()` 和 `wait()` 的实现。第四个参数 `() => this.#tasks.resume()` 让 submit 和 wait 能启动调度。
- `TaskGraphView`（D6）：活动任务的结构视图。

`#host` 是给 `ConversationImpl` 用的一组服务。对话句柄自己只有 `id` 和这个 `#host`（`packages/durable/src/harness/harness.ts:112-119`），所有操作都转给 Harness。

注释里说，所有组件都通过 `this.storage` 读写，它是包了失败保护的存储（D3 的 `GuardedStorage`）。原始的 `rawStorage` 只交给 `super()`。

**往哪走**：§4.3 `root()`。

### 4.3 root() 和 #create()

**调用位置**：宿主调用 `harness.root(context, { agent: { model } })`。`root()` 本身只有一行，转给 `#create({ kind: "root" }, ...)`（`packages/durable/src/harness/harness.ts:397-402`）。`createConversation()` 和 `fork()` 也走 `#create()`。

源码：[`packages/durable/src/harness/harness.ts:428-445`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/harness.ts#L428-L445)

```ts
	async #create(target: CreateTarget, options: CreateOptions, context: Context): Promise<Conversation> {
		this.assertUsable();
		const id = await this.commitWith(async (tx) => {
			if (target.kind === "root" && (await tx.conversation(ROOT_CONVERSATION_ID)) !== undefined) {
				return ROOT_CONVERSATION_ID;
			}
			const record =
				target.kind === "root"
					? await tx.createRootConversation()
					: target.kind === "fork"
						? await tx.forkConversation(target.parentId, target.at, { ownership: target.ownership })
						: await tx.createConversation({ ownership: target.ownership });
			if (options.agent !== undefined) await configure(tx, record.id, options.agent);
			if (options.init !== undefined) await options.init(tx, record.id);
			return record.id;
		}, context);
		return new ConversationImpl(id, this.#host);
	}
```

**讲解**

整个创建过程在一次 `commitWith()` 里完成。`commitWith` 是 Session 的内部 commit，回调拿到的 `tx` 是事务对象（D3）。回调里的所有写入要么全部存储，要么全部丢弃。

根对话的 ID 固定是 1。回调先读一次 `tx.conversation(1)`：已经存在就直接返回，不写任何东西。所以第二次调用 `root()`（包括重启后）会忽略 `agent` 和 `init` 参数。这个读必须在第一次表写入之前，因为事务规定"表写入之后不能再读表"（D3 §4.4 的 `ReadAfterWrite`）。

不存在时调用 `tx.createRootConversation()`。它会在同一个事务里调用 `conversationCreated()` 钩子（§4.4）。然后：

- `configure(tx, id, options.agent)` 把宿主给的 agent 选择写进 `pi.agent` 文档（`packages/durable/src/harness/agent.ts:90-93`）。
- `options.init` 是宿主的初始化回调，可以在同一个 commit 里写自己的文档。

§2.1 输出的 seq=1 就是这个 commit：一条 `conversation` 写入和 5 条 `document.create`。`pi.agent` 的 `configure` 改动和它的创建合并成了一个 base，因为创建本身就要存完整值。

返回的 `ConversationImpl` 只包着 ID。

**往哪走**：§4.4 创建钩子；`commitWith` → D3 §4.1。

### 4.4 conversationCreated() 和 createAgent()

**调用位置**：`tx.createRootConversation()` → `Transaction.#stageConversation()`（`packages/durable/src/session/transaction.ts:405`）→ `this.#host.conversationCreated(this, record)` → 这里。

源码：[`packages/durable/src/harness/harness.ts:447-459`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/harness.ts#L447-L459)

```ts
	/**
	 * The built-in creation hook, in every commit that creates or forks a conversation: empty `pi.live`, `pi.inbox`, and
	 * `pi.usage`, a fresh `pi.provider`, the conversation's `pi.agent` (see `createAgent()`), then
	 * `HarnessOptions.conversationCreated`.
	 */
	protected override async conversationCreated(tx: Transaction, record: ConversationRecord): Promise<void> {
		await tx.doc(LiveDoc, record.id);
		await tx.doc(InboxDoc, record.id);
		await tx.doc(UsageDoc, record.id);
		await tx.doc(ProviderDoc, record.id);
		await createAgent(tx, record);
		await this.#options.conversationCreated?.(tx, record);
	}
```

源码：[`packages/durable/src/harness/agent.ts:181-187`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/agent.ts#L181-L187)

```ts
export async function createAgent(tx: Tx, conversation: ConversationRecord): Promise<void> {
	if (conversation.parent !== undefined) return;
	const agent = await tx.doc(AgentDoc, conversation.id);
	if (conversation.owner === undefined) return;
	const owner = await tx.doc(AgentDoc, conversation.owner.conversationId);
	Object.assign(agent, copyJson(owner) as AgentState);
}
```

**讲解**

每个新对话，不管是根对话、普通对话、fork，还是工具在自己的 commit 里用 `tx.createConversation()` 建的子对话，都在创建它的那个 commit 里跑这个钩子。`tx.doc(LiveDoc, id)` 是"取或建"：文档不存在就用定义的 `initial()` 创建（D3 §4.5）。所以这四行就是"建四个空文档"。`pi.provider` 的 `initial()` 生成一个新的 UUIDv7。

`createAgent` 处理 `pi.agent`：

- fork（有 `parent`）直接返回。fork 的文档复制由存储层按 `fork` 策略完成（spec §3.7），`pi.agent` 是 `asOf`，子对话拿到 fork 点的值。
- 没有所有者的对话得到空的 `{}`。
- 由任务拥有的对话（子 agent）复制所有者对话的 `pi.agent`。之后所有者再改，不影响子对话。

`pi.agent` 只存选择的名字，不存代码：

源码：[`packages/durable/docs/spec.md:908-910`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/docs/spec.md#L908-L910)

```md
It stores only what someone chose for the conversation: extension and tool
names, never code, and no prompt text other than `instructions`. Code comes
from the registry by name when the agent is resolved (section 7.1).
```

每次用到时，`resolveAgent()`（`packages/durable/src/harness/harness.ts:278-288`）用当前的 registry 把名字解析成扩展和工具对象。重启后、扩展重新加载后，存储里的选择不变，新代码自动生效。M8 里对应的东西是 AgentSession 的状态加会话文件里的 `model_change` 条目。

**往哪走**：§4.5 `submit()`。

### 4.5 submit() 和 wait()

**调用位置**：宿主调用 `root.submit({ type: "input", content, requestId }, context)`。

`ConversationImpl.submit` 有两个重载：

源码：[`packages/durable/src/harness/harness.ts:129-135`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/harness.ts#L129-L135)

```ts
	submit(submission: SubmissionDraft, context: Context): Promise<Submission>;
	submit(tx: Tx, submission: SubmissionDraft): Promise<SubmissionId>;
	submit(first: SubmissionDraft | Tx, second: Context | SubmissionDraft): Promise<Submission | SubmissionId> {
		// The overloads pair a draft with a context, and anything else (a transaction) with a draft.
		if ("type" in first) return this.#host.submissions.submit(this.id, first, second as Context);
		return this.#host.submissions.admit(first, this.id, second as SubmissionDraft);
	}
```

第一个重载自己开一个 commit；第二个把 submission 放进调用者已有的事务 `tx`，和调用者的其他写入一起存储。

源码：[`packages/durable/src/harness/submissions.ts:64-73`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/submissions.ts#L64-L73)

```ts
	/** Admit a submission in one commit; see `admitSubmission()`. */
	async submit(conversationId: ConversationId, draft: SubmissionDraft, context: Context): Promise<Submission> {
		const { id } = await this.#traced(conversationId, draft, context, (span) =>
			this.#session.commitWith(
				(tx) => admitSubmission(tx, conversationId, draft, this.#now(), this.#queueModes(), span),
				withTelemetry(span, context),
			),
		);
		return new SubmissionHandle(id, this);
	}
```

`#traced`（`packages/durable/src/harness/submissions.ts:90-119`）先调用 `this.#resume()` 启动调度，再在一个 telemetry span 里执行 admission。admission 本身是 `admitSubmission()`。对话空闲、没有排队输入时，它走最后一段：

源码：[`packages/durable/src/harness/submissions.ts:312-316`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/submissions.ts#L312-L316)

```ts
	const entry = await appendInput(tx, conversationId, draft.content, draft.entry, now);
	const input = { ...fields, type: "input", status: "placed", entry: entry.id } as const;
	const { id } = await tx.createSubmission(input);
	await startRun(tx, conversationId, live, { inputs: [id], first: entry.id });
	return { id, status: "placed" };
```

`appendInput` 写一个 `pi.user` 条目，`createSubmission` 写一条 `placed` 的 submission，`startRun` 创建 `pi.generation` 任务并在 `pi.live` 里记下这次运行。这就是 §2.1 里 seq=2 的四条写入。对话忙时，输入进 `pi.inbox` 排队，submission 是 `queued`（D5）。

`submit()` 在这个 commit 存储之后返回 `Submission` 句柄。句柄只有 `id`，`wait()` 转到 `Submissions.wait()`：

源码：[`packages/durable/src/harness/submissions.ts:133-145`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/submissions.ts#L133-L145)

```ts
	async wait(id: SubmissionId, context: Context): Promise<SettledSubmissionRecord> {
		this.#resume();
		// Check and register on the line so no settling publication falls between them.
		const found = await this.#session.readOnLine(async (storage) => {
			const record = await storage.submission(id, context);
			if (record === undefined) throw new Error(`Submission ${id} does not exist`);
			if (isSettled(record)) return { promise: Promise.resolve(record) };
			// Close rejects registered waiters synchronously and may begin during the read.
			if (this.#closed) throw closedError(this.#session);
			return { promise: this.#waiters.add(id, context) };
		});
		return found.promise;
	}
```

`wait` 也先启动调度。然后在提交线上读 submission 记录：已经结束（`done` 或 `unanswered`）就直接返回，否则登记一个等待者，等某个 commit 把它改成结束状态。"读"和"登记"在同一个线上任务里，中间不会漏掉一次发布。

取消 `wait()` 的 context 只取消这次等待，不撤回 submission。撤回要用 `Submission.abort()`。

**往哪走**：任务怎样跑 → D4、D5。§4.6 看模型最终收到的上下文。

### 4.6 Conversation.context()：条目怎样变成模型上下文

**调用位置**：宿主或 generation 任务读对话的上下文。`ConversationImpl.context()`（`packages/durable/src/harness/harness.ts:156-158`）→ `readContext()`。

源码：[`packages/durable/src/harness/context.ts:133-158`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/harness/context.ts#L133-L158)

```ts
export async function readContext(
	session: SessionImpl,
	storage: Storage,
	conversationId: ConversationId,
	context: Context,
	at?: EntryId,
): Promise<ContextView> {
	const bounds = await session.readOnLine((line) => captureContextBounds(session, line, conversationId, context, at));
	return deriveContext(storage, conversationId, bounds, context);
}

/**
 * Derive the active transcript and model context of one conversation within captured bounds.
 *
 * H = newest visible head marker; the range runs from `H.head` (or transcript start) through the tail. Per target,
 * the newest edit in the range wins. Context entries are H followed by the range's non-head entries.
 */
export async function deriveContext(
	storage: Storage,
	conversationId: ConversationId,
	bounds: ContextBounds | undefined,
	context: Context,
): Promise<ContextView> {
	if (bounds === undefined) return emptyView();
	return deriveRange(bounds, await scanRange(storage, conversationId, rangeQuery(bounds), context), false).view;
}
```

**讲解**

`readContext` 分两步。第一步在提交线上捕获边界：最新的 head 标记和最新条目的 ID。第二步在线外扫描这个范围内的条目，推导上下文。spec §2.1 写了推导规则，主要几条：

源码：[`packages/durable/docs/spec.md:313-325`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/docs/spec.md#L313-L325)

```md
Context derivation:

1. Find the newest visible entry `H` at or before the cutoff that has `head`.
2. Let `from = H.head`, or transcript start when `H` is absent.
3. Scan visible entries from `from` through the cutoff.
4. For each target, the newest edit in that range wins. `omit` contributes no
   model messages; `replace` contributes its `messages` instead of the target's.
5. If `H` exists, context entries are `H` followed by non-head entries in the
   range. Otherwise they are the range.
6. Keep every positional system message and its tool/section changes.
7. Move each assistant's tool results, found before the next assistant message,
   directly after it in tool-call order, ahead of any intervening user or system
   message. The provider protocol requires results to follow their call.
```

用 `m11a-durable.ts context` 看主线场景跑完后的上下文：

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts context
```

```
head: 无
entries: #7 pi.user, #10 pi.system, #11 pi.assistant, #13 pi.tool-result, #15 pi.assistant
下一次请求的 LLM 消息：
  system     sections=[] toolsAdded=[read]
  user       "读 README.md，总结成三句话"
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_…
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one …
  assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}]
```

五个条目各贡献一条 LLM 消息。和 README §5.1 里 coding-agent 发给模型的消息对比，形状相同：一条 system 消息声明工具，然后是 user、assistant、toolResult、assistant。这里 `sections=[]`，因为实验没有装任何提供 prompt section 的扩展。

durable 用 `head` 实现"重新开始"。`reset()` 写一个带 `head: "self"` 的 `pi.reset` 条目，之后模型只看到从它开始的部分，旧条目仍然留在存储里（§6.3、§7.2）。M4 的会话文件用树和分支做类似的事。

**往哪走**：§5 状态机。

---

## 5. 状态机

### 5.1 一个输入 submission

```
 submit({ type: "input" })
   │
   ├─ 对话空闲 ──────────────────────────────▶ placed
   │  submissions.ts:313-315                  │ pi.user 条目已写，run 已开始
   │                                          │
   ├─ 对话忙 / 有排队 ──▶ queued ──────────────┤ 边界处放入 transcript
   │  submissions.ts:278-282   inbox.ts:106    │
   │                         │                │ 最后一个 generation 写出回答
   │                         │                ▼
   │                         │              done   generation.ts:907 / live.ts:295
   │                         │                      entry=pi.user，answer=pi.assistant
   │                         │
   │                         ├─ Submission.abort() ──▶ unanswered reason=aborted
   │                         │  submissions.ts:155
   │                         └─ 过期 ───────────────▶ unanswered reason=stale
   │                            inbox.ts:101
   └─ 运行失败（无模型、轮数上限、崩溃）──▶ unanswered   live.ts:303
```

`done` 和 `unanswered` 是结束状态，`wait()` 在这两种状态时返回（`packages/durable/src/harness/submissions.ts:139`）。

### 5.2 Harness 的调度开关

```
            Harness.open()
            harness.ts:600-602  构造，open 时的恢复
                  │
                  ▼
  ┌──────────────────────────┐   resume() harness.ts:303-306
  │ paused                   │   submit/wait 里的 #resume()
  │ 只读调用不启动调度：       │   submissions.ts:96、134
  │ inspect、getTask、snapshot│ ───────────────────────────▶ ┌─────────┐
  └──────────────────────────┘                                │ running │
                  │                                           └────┬────┘
                  │ close()                                         │ close()
                  ▼                                                 ▼
             closing：封住新的 commit 和任务预约，等已接纳的 commit 存储完，
                      把被打断的任务写回 pending，再关闭 Storage（spec §2.2）
```

一个任务记录自己的状态机（`pending → running → waiting/sleeping → completing → terminal`）在 D4 读。

---

## 6. 设计要点

### 6.1 先存储，再显示

**做法。** 所有改变都通过 commit，commit 在存储确认之后才对外可见（spec §1 不变式 2、3）。§2.1 里，`submit()` 返回之前 seq=2 已经写完；模型请求、工具执行这些外部副作用都不在 commit 回调里跑（不变式 4）。

**理由。** 外界看到的任何状态都已经在存储里，进程崩溃不会让界面上出现过的东西消失。代价是每一步都要等一次存储写入。D3 讲怎样用 group commit 和 deferred commit 降低这个代价。

**代码位置。** `packages/durable/docs/spec.md:48-99`（不变式），`packages/durable/src/session/session.ts:464`（`commitWith`）。

### 6.2 记录里只有引用，代码按名字解析

**做法。** 任务记录存 `kind` 和 `version`，`pi.agent` 存扩展名和工具名，都不存代码。`Harness.open()` 只检查内置任务是否在 registry 里。

**理由。** 存储可以在不同版本的进程之间传递。扩展被卸载时，存下的名字不会消失，重新安装后自动恢复（spec §2.2 "Nothing checks that a stored extension or tool name is installed"）。代价是 registry 必须在 `resume()` 之前装好，否则任务会被标记为 blocked（D4）。

**代码位置。** `packages/durable/src/harness/harness.ts:594-599`，`packages/durable/src/harness/agent.ts:229`（`resolveAgent`）。

### 6.3 条目不可变，用 head 切换上下文

**做法。** 条目写入后不改。压缩和 reset 都通过追加一个带 `head` 的新条目来改变模型看到的范围。

**理由。** transcript 是只追加的日志，存储后端不需要支持修改，fork 可以安全地引用父对话的条目。UI 显示和模型上下文可以分别推导（spec §2.1 末尾）。

**代码位置。** `packages/durable/src/types.ts:468`（`head` 字段），`packages/durable/src/harness/context.ts:340-342`（`selectActive`）。

### 6.4 transcript、任务、文档各管一件事

**做法。** transcript 里只有模型消息和展示数据；"做到哪一步"在任务的检查点里；"现在的配置和进度"在文档里。工具任务的记录只引用条目 ID（§3.3）。

**理由。** 每种状态有不同的生命周期：条目永久保留，任务结束后只剩一个小的结果，任务范围的文档随任务一起退役，`latest` 文档只保留当前值。分开存储，每种都可以按自己的方式回收。

**代码位置。** `packages/durable/src/types.ts:705`（任务），`packages/durable/src/types.ts:767`（文档）。

### 6.5 requestId 让 submit 可以安全重试

**做法。** submission 可以带宿主给的 `requestId`。同一个对话里，相同 `requestId` 的第二次 submit 返回已有的 submission，不再写入。

**理由。** 宿主在"发出 submit"和"收到返回"之间崩溃时，不知道 submit 是否成功。带着同一个 `requestId` 重试，结果总是恰好一次（§7.1）。实现在 D5。

**代码位置。** `packages/durable/src/types.ts:501-502`，`packages/durable/src/session/transaction.ts:339`（`submissionByRequest`）。

---

## 7. 实验

### 7.1 重启后用同一个 requestId 再 submit

**问题。** 在 SQLite 文件上跑完主线场景，关闭 Harness。再打开同一个文件，用相同的 `requestId: "main-1"` 再 submit 一次。会再请求模型吗？

**预测。** 不会。submission 的 `requestId` 存在 SQLite 里，第二次 submit 找到已有的 #8，直接返回它。它已经是 `done`。没有新的 commit，条目数仍然是 5。faux 模型已经没有预设回复，如果真的再请求模型，运行会失败。

**运行。** `persist` 场景每次都从一个空文件开始。

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts persist /tmp/pi-lab/m11a-persist-1/session.sqlite
```

```
第一个进程：submission id = 8
== 重新打开后，用同一个 requestId 再 submit 一次
submission id = 8 status = {"conversationId":1,"requestId":"main-1","type":"input","status":"done","entry":7,"id":8,"answer":15}
条目数： 5
```

**结论。** 预测成立。脚本在重新打开、取得根对话之后打开提交日志，之后没有出现任何 `[Storage.commit …]` 行，第二次 submit 没有写入。submission 的 ID 仍是 8，状态是第一个进程留下的 `done`。

### 7.2 reset 之后模型看到什么

**问题。** 主线场景跑完后调用 `root.reset("接着上次的总结继续")`。存储里的条目会变少吗？`context()` 给出的消息是什么？

**预测。** 条目不会变少，多了一个 `pi.reset` 条目，它的 `head` 指向自己。`context()` 的 `head` 是这个条目，`entries` 只有它一个，`messages` 只有一条 user 消息，内容是 handoff 文本。对话空闲，所以写入立即放置，submission 是 `write done`。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts reset
```

```
== root.reset("接着上次的总结继续")
[Storage.commit 第 14 次] 1 个 batch
  seq=14
    entry #16 pi.reset model=[user]
    submission #17 write done
head: {"kind":"pi.reset","model":[{"role":"user","content":"接着上次的总结继续","timestamp":1793528611188}],"id":16,"conversationId":1,"head":16}
entries: #16 pi.reset
messages: user "接着上次的总结继续"
存储里的条目： #7 pi.user, #10 pi.system, #11 pi.assistant, #13 pi.tool-result, #15 pi.assistant, #16 pi.reset
```

**结论。** 预测成立。存储里 6 个条目都在，模型上下文只剩 reset 之后的部分。

### 7.3 registry 缺少内置任务

**问题。** 用一个 `snapshot().task()` 总是返回 `undefined` 的 registry 打开 Harness，会发生什么？存储会被写吗？

**预测。** `Harness.open()` 在构造 `HarnessImpl` 之前就抛错（§4.1），错误信息列出三个内置任务名，`Storage.commit()` 一次也没有被调用。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts noregistry
```

```
Harness.open() 拒绝： Registry lacks built-in tasks pi.generation, pi.tool, pi.compaction; create it with createRegistry()
Storage.commit() 调用次数： 0
```

**结论。** 预测成立。检查发生在打开 Session 之前。

---

## 8. 自测与衔接

### 8.1 自测

1. §2.1 里，`submit()` 返回时有几个条目？模型被调用过几次？
2. 两个进程先后打开同一个 SQLite 文件，第二个进程装的扩展比第一个少了一个工具扩展。`pi.agent` 文档会被改写吗？
3. M2 的 `AgentState.messages`、`isStreaming`、`pendingToolCalls` 在 durable 里大致对应什么？

<details>
<summary>答案</summary>

1. 1 个条目（#7 `pi.user`），模型被调用 0 次。seq=2 的 commit 存储后 `submit()` 就返回；第一次模型请求发生在 seq=4 之后的 `request` 阶段。
2. 不会。`pi.agent` 只记录名字，解析时跳过没装的扩展，文档本身不会因为 registry 变化而改写（spec §2.2）。那个扩展的工具不会提供给模型；如果模型调用了它，得到 `tool_unavailable` 错误结果。
3. `messages` 对应对话的条目（经 `context()` 推导成 LLM 消息）；`isStreaming` 对应"对话有活动的 run"，记在 `pi.live` 里；`pendingToolCalls` 对应 generation 拥有的、未结束的 `pi.tool` 任务，运行中的工具也显示在 `pi.live` 里（D6）。

</details>

### 8.2 衔接

本篇的 `#create()`、`submit()` 都落在 `commitWith()` 上，`Harness.open()` 落在 `SessionImpl` 的构造函数上。D3 从 `Session.commit()` 读起，看一次 commit 怎样从回调变成存储里的一批写入。D3 用到的 `Context`、`Tracker`、`Draft` 和 SQLite 封装来自 chord，先在 [D2](D2-chord.md) 读。
