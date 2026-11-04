# M11 durable · D3 存储与事务：一次 commit 的全过程

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](../README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。上一篇：[D2 chord](D2-chord.md)。下一篇：[D4 任务与调度](D4-tasks-scheduler.md)。

---

## 1. 这个模块是什么

### 1.1 职责

D1 里每一个写操作最后都落在 `commitWith()` 上。本篇读这一层：Session 怎样把一个 commit 回调变成一批存储写入，怎样保证原子性和顺序，以及存储后端要满足什么契约。

Session 层不知道对话、任务、模型是什么意思。它只认四张表（对话、条目、任务、submission）和文档，提供三个保证：

- **原子**：一个 commit 的所有表写入和文档修改，要么全部存储，要么全部不存（spec §1 不变式 1）。
- **先存后见**：commit 在存储确认之后才发布到内存缓存和观察者（不变式 2、3）。例外是 deferred commit（§4.10）。
- **一条线**：commit 回调一次只运行一个，按顺序发布（不变式 7）。存储调用可以和后面的回调重叠（group commit）。

### 1.2 在调用链中的位置

```
Conversation.commit / Session.commit / commitWith()           §4.1
└─ #enqueue()  排进提交线，按顺序交付结果                       §4.2
    └─ #runCommit()                                            §4.3
        ├─ new Transaction(host, context, scope)
        ├─ change(tx)  调用者的回调
        │   ├─ tx.conversation() / tx.entry() …  表读           §4.4
        │   ├─ tx.appendEntry() / createTask() …  表写          §4.4
        │   └─ tx.doc(token, id) → #acquire() → Draft          §4.5 → D2
        ├─ tx.settleSuccess() → prepare() → #assemble()        §4.6、§4.7
        │   └─ checkpointWhen() 选 base 或 delta
        ├─ deferred 且可暂存 → tx.holdable() → #hold()         §4.10
        └─ Writer.admit(part)                                  §4.8
            └─ #pump → #flush → #send
                ├─ Storage.commit(batches, receive)            §4.9
                │   └─ MemoryStorage / SqliteStorage           §4.11
                └─ #publish → part.publish(seq) → #adopt()     §4.12
                    └─ tx.adopt(seq) → tracker.adopt()         → D2
```

D1 的 `root()`、`submit()` 和 D4、D5 里任务的每一步都走这条路。

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/durable/src/session/session.ts` | 2104 | `SessionImpl`：提交线、`#runCommit`、发布、文档缓存、deferred commit | 精读提交路径；缓存回收、观察者、close 略读 |
| `packages/durable/src/session/transaction.ts` | 1292 | `Transaction`：`Tx` 的实现，记录写入并组装成一批 | 精读 |
| `packages/durable/src/session/writer.ts` | 361 | `Writer`：批量调用 `Storage.commit()`，按顺序发布 | 精读 |
| `packages/durable/src/session/keys.ts` | 274 | 读写键和 `GatedStorage` | 读键的定义 |
| `packages/durable/src/session/cached-storage.ts` | 378 | 记录的读缓存 | 略 |
| `packages/durable/src/storage/memory.ts` | 892 | `MemoryStorage`：参考实现 | 精读 `commit` 和文档存储 |
| `packages/durable/src/storage/sqlite/storage.ts` | 1049 | `SqliteStorage` | 只读 `commit` |
| `packages/durable/src/types.ts` | 1445 | `Tx`、`Storage`、`StorageWrite` | 选读 |
| `packages/durable/docs/spec.md` | 6531 | 规范 | 读 §3、§4 |

行数用 `wc -l` 统计。

### 1.4 读完能回答的问题

1. commit 回调抛错时，已经调用过的 `tx.appendEntry()` 会怎样？
2. 为什么一个 commit 里先 `appendEntry` 再 `tx.conversation()` 会报错，而先读后写可以？
3. 三个 commit 几乎同时发起，`Storage.commit()` 会被调用几次？
4. 一个文档被修改了很多次，存储里是每次一个完整值，还是每次一组操作？谁决定？
5. deferred commit 的修改在进程崩溃时会丢吗？丢多少？

---

## 2. 先跑一次

### 2.1 主线场景的 13 次 Storage.commit()

D1 §2.1 已经看过 `m11a-durable.ts main` 的输出。这里只看它和本篇有关的部分：

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts main | sed -n '3,34p'
```

```
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
```

- 每次 `Storage.commit()` 收到一个数组的数组：外层是 batch，每个 batch 是一个 commit 的全部写入，有自己的 `seq`。这里每次都只有一个 batch，因为 `MemoryStorage` 是同步完成的，后面的 commit 还没来得及排队（§7.4 让它排队）。
- 一个 batch 里，表写入（`conversation`、`entry`、`submission`、`task`）和文档写入（`document.create`、`document.change`）混在一起。
- `seq=1` 的 `document.create #6 pi.agent base`：`configure()` 写的模型选择和文档的创建合成了一个 base。
- `seq=2`、`seq=5` 等处 `pi.live` 每次都是 `base`。这是 `pi.live` 的 `checkpointWhen` 决定的：没有运行中的 generation 和工具时存完整值（`packages/durable/src/harness/live.ts:219-222`）。
- generation 在流式输出过程中把部分回答写进 `pi.live`，用的是 deferred commit，并受 `settings.progress.partialIntervalMs` 节流。这类写入即使发生，也不会单独出现在这个日志里：它们暂存在内存中，和下一个读写 `pi.live` 的普通 commit（例如 `seq=5`）一起存储（§4.10）。

### 2.2 一个事务的失败

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts raw
```

```
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #2 {"id":2}
appendEntry 返回 {"kind":"lab.note","data":"hello","id":3,"conversationId":2}
commit 被拒绝： ReadAfterWrite Tx.conversation() cannot read tables after the first table write
这次 commit 调用 Storage.commit() 的次数： 0
会话里的条目数： 0
lab.notes 文档： undefined
```

回调里 `appendEntry` 成功返回了一个带 ID 的记录（#3），文档也写了，但之后的表读抛出 `ReadAfterWrite`。整个 commit 被丢弃：`Storage.commit()` 没有被调用，条目数是 0，文档不存在。ID #3 已经分配出去，但永远不会被使用（spec §2 允许 ID 有间隔）。

---

## 3. 数据结构

### 3.1 StorageWrite：一条存储写入

源码：[`packages/durable/src/types.ts:911-928`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L911-L928)

```ts
/** One record or document mutation in an atomic storage commit. */
export type StorageWrite =
	| { readonly type: "conversation"; readonly value: ConversationRecord }
	| { readonly type: "entry"; readonly value: EntryRecord }
	| { readonly type: "task"; readonly value: TaskRecord<JsonValue, JsonValue, JsonValue> }
	| { readonly type: "submission"; readonly value: SubmissionRecord }
	| {
			readonly type: "document.create";
			readonly record: DocumentCreate;
			readonly content: Extract<DocumentContent, { readonly kind: "base" }>;
	  }
	| { readonly type: "document.copy"; readonly record: DocumentCreate; readonly source: DocumentCopySource }
	| {
			readonly type: "document.change";
			readonly id: DocumentId;
			readonly content: DocumentContent;
	  }
	| { readonly type: "document.retire"; readonly id: DocumentId };
```

| 种类 | 说明 |
|---|---|
| `conversation`、`entry` | 新建的对话和条目，存后不再改 |
| `task`、`submission` | 完整记录。同一个 ID 再次写入就是整条替换 |
| `document.create` | 新文档实例，必须带 base |
| `document.copy` | fork 时复制父对话的文档，由存储在服务端完成 |
| `document.change` | 已有文档的一次修改，内容是 base 或 delta |
| `document.retire` | 文档实例退役，例如任务结束时它的任务文档 |

**真实实例**：§2.1 每一行缩进的输出都是一条 `StorageWrite`，例如 `entry #11 pi.assistant model=[assistant] byTaskId=9`、`document.change #2 pi.live base`。

### 3.2 DocumentContent：base 和 delta

源码：[`packages/durable/src/types.ts:883-894`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L883-L894)

```ts
/** Complete checkpoint or Chord operation batch selected by the owning Session. */
export type DocumentContent =
	| {
			readonly version: number;
			readonly kind: "base";
			readonly value: JsonObject;
	  }
	| {
			readonly version: number;
			readonly kind: "delta";
			readonly ops: readonly Op[];
	  };
```

| 种类 | 内容 |
|---|---|
| `base` | 完整值。重放从最近的 base 开始 |
| `delta` | D2 的 `Op[]`，在上一个状态上应用 |

`version` 是写入时文档定义的版本。存储要求跨版本必须写 base（`packages/durable/src/storage/memory.ts:826-829`）。

**真实实例**：§7.3 的 `delta 2 ops [["a",["text"],"AB"],["s",["n"],1]]`。

### 3.3 Storage：后端契约

源码：[`packages/durable/src/types.ts:1316-1345`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/types.ts#L1316-L1345)

```ts
/**
 * Atomic persistence boundary for Session records.
 *
 * Storage trusts the owning Session to supply semantically valid records, references,
 * ancestry, and transitions. Implementations enforce atomicity, global ID ownership,
 * immutable conversation/entry creation, document record consistency, and detached values;
 * Session serializes commits.
 *
 * An error a method throws is final: it fails the Session, which nothing retries, so retry transient failures inside
 * the method. Two errors of a read fail only that read: a `StorageRequestError` for an invalid request (an unknown
 * conversation, a foreign or malformed cursor, history a document does not keep), which must have no durable effect, and a
 * rejection because its caller's context was aborted. Any error from `commit()` or `mintId()` is fatal.
 */
export interface Storage {
	/**
	 * Persist `batches` in order, each as one ordinary atomic commit with its own seq, checked against the state the
	 * earlier ones left, in as few round trips as the backend can. Every batch has at least one write: a commit without
	 * writes never reaches Storage, so the Session never sends an empty batch. Stops at the first batch Durable's own checks refuse
	 * (global IDs, document actions, copy resolution): the batches before it are durable, it and the ones after are not.
	 * Calls `receive(index, seq)` once per durable batch, in order, synchronously, after all of them are durable and
	 * before the next commit starts. Resolves with the number of durable batches and the refusal, if any. Rejects for an
	 * unknown outcome (connection lost, statement or commit failed); `receive` calls made before the rejection still
	 * stand. `receive` is a durability receipt, not publication: the Session publishes later. Once it resolved, later
	 * reads through this storage observe the stored batches.
	 */
	commit(
		batches: readonly (readonly StorageWrite[])[],
		receive: (index: number, seq: Seq) => void,
		context: Context,
	): Promise<CommitResult>;
```

| 方法 | 说明 |
|---|---|
| `commit(batches, receive, context)` | 按顺序存储多个 batch，每个 batch 原子、有自己的 `seq`。在第一个被拒绝的 batch 处停下。存好的 batch 用 `receive(index, seq)` 回执 |
| `publishing?(publish)` | 可选。在后端自己的作用域里运行一次发布 |
| `mintId()` | 分配全局唯一、递增的数字 ID |
| `conversation`、`entry`、`scanEntries`、`task`、`submission`、`submissionByRequest` … | 表的读 |
| `findDocument`、`document`、`scanDocuments` | 文档的读：`document(id, at)` 把 base 和后续 delta 重放成值 |
| `close()` | 关闭 |

注释里有两条重要约定。第一，`commit()` 或 `mintId()` 抛出的任何错误都是致命的，Session 进入失败状态，必须重新打开（spec §1 不变式 8）；存储自己负责重试暂时性错误。第二，`receive` 是"已经持久化"的回执，不是发布，发布由 Session 稍后做。

**真实实例**：§2.1 的 `[Storage.commit 第 N 次] 1 个 batch` 就是一次 `commit()` 调用，`seq=…` 来自 `receive`。

### 3.4 LoadedDocument：内存里的文档

源码：[`packages/durable/src/session/transaction.ts:89-100`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L89-L100)

```ts
/** One committed document incarnation owned by the Session tracker cache. */
export type LoadedDocument = {
	readonly addressId: string;
	readonly record: DocumentRecord;
	/** Persisted definition version; older while the tracked value is migrated only in memory. */
	storedVersion: number;
	/** Definition version whose shape the tracked value has; access with another version reloads from Storage. */
	readonly valueVersion: number;
	/** Stored deltas after the newest base; advanced by adoption so the next predicate call needs no read. */
	deltasSinceBase: number;
	readonly tracker: Tracker<JsonObject>;
};
```

| 字段 | 说明 |
|---|---|
| `addressId` | 逻辑地址：种类 + 范围 + family key |
| `record` | 存储里的 `DocumentRecord`（D1 §3.5） |
| `storedVersion` / `valueVersion` | 存储里的版本 / 内存值的版本。读迁移只在内存里做时两者不同 |
| `deltasSinceBase` | 最近一个 base 之后存了几个 delta，传给 `checkpointWhen` |
| `tracker` | D2 的 `Tracker`，持有当前修订 |

Session 用 `#documents` 这个 Map 缓存已加载的文档（`packages/durable/src/session/session.ts:227`）。读写文档不需要访问存储；一段时间不用的文档会被回收（spec §4 开头，默认 10 分钟）。

**真实实例**：`checkpoint` 场景（§7.3）里 `lab.notes` 的 `deltasSinceBase` 依次是 0、1、2、0、1，对应存储里的 base、delta、delta、base、delta。

### 3.5 PartSpec：写入器的一个单元

源码：[`packages/durable/src/session/writer.ts:10-22`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/writer.ts#L10-L22)

```ts
/** One logical commit the line admitted: stored (`writes`, never empty), or held in memory only (no `writes`). */
export type PartSpec = {
	/** Line index of the job that admitted it: a failure it causes makes only later jobs' outcomes `SessionFailed`. */
	readonly index: number;
	readonly writes: readonly StorageWrite[] | undefined;
	/** Keys it writes: a read of one waits for its publication. */
	readonly keys: readonly string[];
	readonly context: Context;
	/** Publish it in memory, in line order, once it is durable (with its seq) or, held, once the parts before it are. */
	readonly publish: (seq: Seq | undefined) => PublishFailure | undefined;
	/** It will never be published: abort what it prepared. */
	readonly discard: () => void;
};
```

| 字段 | 说明 |
|---|---|
| `index` | 这个 commit 在提交线上的序号 |
| `writes` | 要存储的写入；`undefined` 表示 deferred commit 暂存在内存里 |
| `keys` | 它写了哪些键。还没发布时，读这些键的后续任务要等它 |
| `publish(seq)` | 存储成功后在内存里采纳它，并通知观察者 |
| `discard()` | 不会发布了，丢掉准备好的修改 |

**真实实例**：§7.4。三个 commit 各是一个 part，后两个合进同一次 `Storage.commit()`。

### 3.6 类型关系图

```
 commit 回调 change(tx)
   │ 表读写                      文档
   ▼                              ▼
 Transaction ── #writes: StorageWrite[]（表）
             └─ #documents: DocumentEntry ── target: LoadedDocument（缓存里的 Tracker）
                                         └─ change → prepared（D2）
   │ settleSuccess() → #assemble()：每个文档一个 DocumentPlan
   ▼                     content: document.create / change(base|delta) / copy / retire
 StorageWrite[]（一个 batch）
   │ Writer.admit(PartSpec { writes, keys, publish })
   ▼
 Storage.commit([batch, batch, …], receive(index, seq))
   │ 回执 seq
   ▼
 part.publish(seq) → Transaction.adopt(seq) → tracker.adopt(prepared)
                   → 读缓存、观察者（D6）、等待者
```

---

## 4. 从入口读起

### 4.1 Session.commit() 和 commitWith()

**调用位置**：宿主调用 `harness.commit(...)` 或 `conversation.commit(...)`；D1 的 `#create()`、`Submissions.submit()`、D4 的任务运行时都调用 `commitWith()`。

源码：[`packages/durable/src/session/session.ts:436-438`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/session.ts#L436-L438)

```ts
	commit<T>(change: (tx: Tx) => T | Promise<T>, context: Context, options?: CommitOptions): Promise<T> {
		return this.commitWith(change, context, undefined, options);
	}
```

源码：[`packages/durable/src/session/session.ts:464-478`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/session.ts#L464-L478)

```ts
	commitWith<T>(
		change: (tx: Transaction) => T | Promise<T>,
		context: Context,
		scope?: TransactionScope,
		options?: CommitOptions,
	): Promise<T> {
		let withinMs: number | undefined;
		try {
			this.assertUsable();
			withinMs = this.#deferral(options?.persistence);
		} catch (error) {
			return Promise.reject(error);
		}
		return this.#enqueue((index) => this.#runCommit(change, context, scope, withinMs, index));
	}
```

**讲解**

公开的 `commit` 只是 `commitWith` 去掉 `scope` 参数。`scope` 是内部用的：`conversationId` 给 `tx.createTask()` 当默认对话，`taskId` 写进追加的条目的 `byTaskId`（`packages/durable/src/session/transaction.ts:168-172`）。`Conversation.commit()` 传 `{ conversationId: this.id }`（`packages/durable/src/harness/harness.ts:152-154`）。

`commitWith` 先做两个同步检查：Session 是否可用（没有失败、没有在关闭），以及 deferred 选项是否合法（`#deferral`，`packages/durable/src/session/session.ts:515-524`）。然后把 `#runCommit` 交给 `#enqueue`。

**往哪走**：§4.2。

### 4.2 #enqueue()：提交线

**调用位置**：`commitWith()`、`readOnLine()`、`commitInternal()`。

源码：[`packages/durable/src/session/session.ts:1595-1652`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/session.ts#L1595-L1652)

```ts
	#enqueue<T>(work: (index: number) => Promise<Ran<T>>, exempt = false): Promise<T> {
		const job: Job = { index: this.#nextIndex++, hit: false };
		const ran = this.#tail.then(async () => {
			while (this.#outstanding >= MAX_OUTSTANDING) {
				this.#room ??= Promise.withResolvers();
				await this.#room.promise;
			}
			this.#outstanding++;
			return this.#onLine(job, work);
		});
		this.#tail = ran.then(ignore, ignore);
		const outcome = ran.then(
			(result) => {
				if (result.after === undefined) {
					this.#ended(job);
					return result.value;
				}
				return result.after.then(
					() => {
						this.#ended(job);
						return result.value;
					},
					(error: unknown) => {
						this.#ended(job);
						throw error;
					},
				);
			},
			(error: unknown) => {
				this.#ended(job);
				throw error;
			},
		);
		// Delivered later, after the outcomes before it: handled now, so an early rejection is not reported unhandled.
		outcome.catch(ignore);
		const fence = this.#fence;
		this.#fence = fence.then(() => outcome).then(ignore, ignore);
		const index = job.index;
		const deliver = (fenced: boolean) =>
			(fenced ? fence.then(() => outcome) : outcome).then(
				(value) => {
					this.#delivered(index, fenced);
					if (this.#poisoned(index)) throw new SessionFailed(this.#failure!.error);
					return value;
				},
				(error: unknown) => {
					this.#delivered(index, fenced);
					throw this.#poisoned(index) ? new SessionFailed(this.#failure!.error) : error;
				},
			);
		// Only a job without writes is exempt: one with writes waits for its own publication and the outcomes before it.
		return exempt
			? ran.then(
					(result) => deliver(job.hit || result.after !== undefined),
					() => deliver(job.hit),
				)
			: deliver(true);
	}
```

**讲解**

提交线（mutation line）就是 `#tail` 这条 Promise 链。每个任务接在 `#tail` 后面运行，`#onLine` 把它设为 `#current`，所以回调一次只运行一个。

一个任务的结果分两段：

- `ran`：回调和组装完成。对有写入的 commit，`result.after` 是这个 commit 的发布 Promise（§4.8 的 `part.outcome`）。
- `outcome`：等 `after` 完成。也就是说，commit 的 Promise 在存储并发布之后才 resolve。

`#tail` 只等 `ran`，不等 `outcome`。这就是 group commit 的关键：一个 commit 组装完、交给写入器后，提交线马上运行下一个回调，不等存储。

`#fence` 保证结果按线上顺序交付：每个任务的结果要等它前面所有任务的结果交付后才交付。所以即使后一个 commit 先存好，它的调用者也晚于前一个 commit 的调用者得到结果。

`MAX_OUTSTANDING` 限制还没交付结果的任务数，超过时线停下来等（spec §4："At most 256 admitted jobs wait for their outcome"）。

**往哪走**：§4.3。

### 4.3 #runCommit()

**调用位置**：`#enqueue()` → `#onLine()` → `work(index)`。

前半段：运行回调，结算事务。

源码：[`packages/durable/src/session/session.ts:1078-1117`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/session.ts#L1078-L1117)

```ts
	async #runCommit<T>(
		change: (tx: Transaction) => T | Promise<T>,
		context: Context,
		scope: TransactionScope | undefined,
		withinMs: number | undefined,
		index: number,
	): Promise<Ran<T>> {
		this.#assertHealthy();
		context.abortSignal?.throwIfAborted();
		const tx = new Transaction(this.#host, context, scope);
		let result: T;
		try {
			result = await change(tx);
			// A callback that caught a failed read must not commit, nor succeed as if it had.
			this.#assertHealthy();
		} catch (error) {
			await tx.settleFailure();
			this.#sweepDocuments();
			throw error;
		}
		let writes: readonly StorageWrite[];
		try {
			writes = await tx.settleSuccess();
		} catch (error) {
			// Settlement aborted every change already.
			this.#sweepDocuments();
			throw error;
		}
		if (writes.length === 0) {
			tx.discard();
			this.#sweepDocuments();
			return { value: result };
		}
		if (this.#readOnly) {
			// Refused before Storage: the Session stays healthy, and the caller sees why.
			tx.discard();
			this.#sweepDocuments();
			throw new ReadOnlySession("a commit with writes");
		}
		const discard = (): void => tx.discard();
```

后半段：deferred 暂存，或者交给写入器。

源码：[`packages/durable/src/session/session.ts:1118-1160`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/session.ts#L1118-L1160)

```ts
		if (withinMs !== undefined) {
			const held = tx.holdable();
			if (held !== undefined) {
				// Adopted in memory when the commits before it are published, as a part of no storage call.
				const part = this.#writer.admit({
					index,
					writes: undefined,
					keys: tx.writeKeys(),
					context,
					publish: () => this.#hold(tx, held, withinMs, context),
					discard,
				});
				this.#sweepDocuments();
				return { value: result, after: part.outcome };
			}
		}
		let membership = this.membershipKeys(tx);
		if (!Array.isArray(membership)) {
			try {
				membership = await membership;
			} catch (error) {
				tx.discard();
				this.#sweepDocuments();
				throw error;
			}
		}
		// Logical admission. Held changes leave their queue only here, once nothing but Storage can fail; a commit never
		// stores a hold adopted after it in line order. Taken before the keys, so the documents it stores held changes of
		// are its writes too: a later job that reads one waits for it instead of storing them again.
		const taken = this.#holds.size === 0 ? NO_HOLDS : tx.takeHeld((id) => this.#holds.get(id));
		const keys = tx.writeKeys();
		for (const key of membership as readonly string[]) keys.push(key);
		const part = this.#writer.admit({
			index,
			writes,
			keys,
			context,
			publish: (seq) => this.#adopt(tx, writes, taken, seq!, context),
			discard,
		});
		this.#sweepDocuments();
		return { value: result, after: part.outcome };
	}
```

**讲解**

1. 新建 `Transaction`，调用回调 `change(tx)`。
2. 回调抛错：`tx.settleFailure()` 放弃所有文档修改，再把错误抛给调用者。表写入只记在 `tx` 里，从未离开它，所以什么都不用撤销。§2.2 就是这条路径。
3. 回调成功：`tx.settleSuccess()` 封住事务、准备所有文档修改、组装写入（§4.6）。
4. 没有写入：直接返回，不碰存储。
5. 只读 Session：拒绝（D1 提到的 `readOnly`）。
6. deferred 且 `tx.holdable()` 成立：作为没有 `writes` 的 part 交给写入器，发布时只在内存里采纳（§4.10）。
7. 否则：算出写入的键，`tx.takeHeld()` 把这个 commit 涉及的文档的暂存修改拿出来放进它自己的写入里，再 `Writer.admit()`。

注释里的"logical admission"就是第 7 步的 `admit`：从这一刻起，commit 只可能因为存储失败而失败。暂存的修改也是在这一刻才离开队列，所以前面任何一步失败都不会丢掉它们。

**往哪走**：§4.4 回调里的表读写。

### 4.4 表读和表写：ReadAfterWrite

**调用位置**：回调里的 `tx.conversation()`、`tx.appendEntry()` 等。所有表读都经过 `#read`，所有表写都经过 `#write`。

源码：[`packages/durable/src/session/transaction.ts:1132-1150`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L1132-L1150)

```ts
	#read<T>(method: string, read: () => Promise<T>): Promise<T> {
		try {
			this.#assertOpen();
			if (this.#hasTableWrite) throw new ReadAfterWrite(`Tx.${method}()`);
			return this.#track(read());
		} catch (error) {
			return Promise.reject(error);
		}
	}

	#write<T>(write: () => Promise<T>): Promise<T> {
		try {
			this.#assertOpen();
			this.#hasTableWrite = true;
			return this.#track(write());
		} catch (error) {
			return Promise.reject(error);
		}
	}
```

`appendEntry` 是一个典型的表写：

源码：[`packages/durable/src/session/transaction.ts:425-446`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L425-L446)

```ts
	#appendEntry(conversationId: ConversationId, value: EntryDraft): Promise<EntryRecord> {
		return this.#write(async () => {
			await this.#requireConversation(conversationId);
			this.#assertOpen();
			const id = await this.#host.storage.mintId<EntryId>();
			this.#assertOpen();
			const { head, ...rest } = value;
			// Undefined `head` and `byTaskId` are omitted by the copy.
			const record = copyJson(
				{
					...rest,
					id,
					conversationId,
					head: head === "self" ? id : head,
					byTaskId: this.#scope.taskId,
				},
				TABLE_JSON_COPY_OPTIONS,
			) as unknown as EntryRecord;
			this.#writes.push({ type: "entry", value: record });
			return record;
		});
	}
```

**讲解**

`#write` 设置 `#hasTableWrite = true`；之后 `#read` 一律抛 `ReadAfterWrite`。为什么这样规定？表写入在回调运行时只记进 `#writes`，没有写到存储。如果允许之后的表读，读到的是存储里的旧状态，看不到自己刚写的东西，很容易出错。反过来，先读后写没问题：读到的是已发布的状态。spec §4 的规则：

源码：[`packages/durable/docs/spec.md:1962-1972`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/docs/spec.md#L1962-L1972)

```md
Table rules:

- Tables are conversations, entries, tasks, and submissions.
- Table reads are allowed before the first table write.
- Any table read after the first table write throws `ReadAfterWrite`.
- Document access and read-your-writes remain available after table writes.
- Creation methods return their created ID/record; callers do not read it back.
- `Conversation.submit(tx, draft)` and `ConversationHandle.submit(tx, draft)`
  come before the commit's first table write, another admission's included;
  otherwise they throw `ReadAfterWrite`, whether or not the admission would read
  a table (section 6).
```

文档不受这条限制，因为文档的 draft 本身就是"读自己的写"。

`#track` 把每个异步操作登记进 `#pendingOperations`。回调结束时还有没完成的操作（比如忘了 `await`），`settleSuccess` 会拒绝整个 commit（§4.6）。

`appendEntry` 立即通过 `mintId()` 拿到 ID，复制成严格 JSON，推进 `#writes`。`head: "self"` 在这里换成新条目自己的 ID（D1 的 `reset()` 用到它）。ID 分配是异步的，因为远程存储可能要持久地分配全局 ID（spec §3.3）。

**往哪走**：§4.5 文档。

### 4.5 tx.doc() 和 #acquire()

**调用位置**：回调里的 `await tx.doc(LiveDoc, conversationId)`；D1 的 `conversationCreated()`。

源码：[`packages/durable/src/session/transaction.ts:603-632`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L603-L632)

```ts
	doc(token: AnyDocToken, ...args: readonly unknown[]): Promise<Draft<JsonObject>> {
		try {
			this.#assertOpen();
			const definition = token.definition;
			const resolved = resolveAddress(definition, args);
			this.#assertTaskDocumentsOpen(resolved);
			const latest = this.#latestDocumentByAddress.get(resolved.id);
			if (latest !== undefined && !latest.retireOnCommit) {
				if (latest.draftPromise !== undefined) return latest.draftPromise;
				if (latest.target?.kind === "fork-copy") {
					latest.draftPromise = this.#track(this.#acquireForkCopy(latest, definition, latest.target));
					return latest.draftPromise;
				}
			}
			const seed = definition.family === true ? copyJson(args[resolved.nextArgument]) : undefined;
			const docEntry: DocumentEntry = {
				addressId: resolved.id,
				address: resolved.address,
				definition,
				retireOnCommit: false,
			};
			this.#documents.push(docEntry);
			this.#latestDocumentByAddress.set(docEntry.addressId, docEntry);
			// Capture retirement before awaiting so a pending old acquisition and its replacement stay distinct.
			docEntry.draftPromise = this.#track(this.#acquire(docEntry, seed, latest?.retireOnCommit === true));
			return docEntry.draftPromise;
		} catch (error) {
			return Promise.reject(error);
		}
	}
```

源码：[`packages/durable/src/session/transaction.ts:679-714`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L679-L714)

```ts
	async #acquire(entry: DocumentEntry, seed: JsonValue | undefined, skipLoad: boolean): Promise<Draft<JsonObject>> {
		const definition = entry.definition!;
		const loaded = skipLoad
			? undefined
			: await this.#host.load(definition, entry.addressId, entry.address, this.#context);
		this.#assertOpen();
		if (loaded !== undefined) {
			checkRecordScope(definition, loaded.record);
			checkRecordVersion(definition, loaded.record, loaded.storedVersion);
			entry.target = { kind: "loaded", document: loaded };
			entry.change = loaded.tracker.beginChange();
			return entry.change.state;
		}
		const scope = entry.address.scope;
		if (scope.kind === "conversation") await this.#requireConversation(scope.conversationId);
		if (scope.kind === "task") {
			const task = await this.#currentTask(scope.taskId);
			if (task === undefined) throw new Error(`Task ${scope.taskId} does not exist`);
			if (task.state.status === "terminal") throw new Error(`Task ${scope.taskId} is terminal`);
		}
		this.#assertOpen();
		const value = copyJson(
			definition.family === true ? definition.initial(seed) : definition.initial(),
		) as JsonObject;
		const id = await this.#host.storage.mintId<DocumentId>();
		this.#assertOpen();
		const tracker = track(value);
		entry.target = {
			kind: "created",
			record: documentCreate(definition, entry.address, id),
			version: definition.version,
			tracker,
		};
		entry.change = tracker.beginChange();
		return entry.change.state;
	}
```

**讲解**

`doc()` 先用定义和参数解析出逻辑地址（`resolveAddress`，`packages/durable/src/documents.ts:109`）。同一个事务里对同一个地址的第二次 `doc()` 返回同一个 draft Promise（`latest.draftPromise`），所以多处代码拿到的是同一个 draft。

`#acquire` 有两条路：

- 文档已存在：`#host.load()` 从缓存取，没有就从存储加载（`SessionImpl.#loadDocument`，`packages/durable/src/session/session.ts:1545-1585`）。检查范围和版本，然后 `loaded.tracker.beginChange()`。
- 文档不存在：确认所属的对话或任务存在且未结束，用 `initial()` 生成初始值，`copyJson` 复制成严格 JSON，分配文档 ID，`track(value)` 新建 tracker。

两条路都返回 `entry.change.state`，也就是 D2 的 `Draft`。回调对它的赋值只进 overlay。

`#loadDocument` 开头先 `touch(documentKey(addressId))`：如果有一个已接纳但还没发布的 commit 写过这个文档，就等它发布。这保证读到的总是已存储的状态（spec §1 不变式 7）。

**往哪走**：§4.6 结算。

### 4.6 settleSuccess()

**调用位置**：`#runCommit()` 回调成功之后（`packages/durable/src/session/session.ts:1100`）。

源码：[`packages/durable/src/session/transaction.ts:765-789`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L765-L789)

```ts
	/**
	 * Seal after callback success, prepare every open change, and assemble the atomic batch.
	 * Any failure aborts every change before Storage admission.
	 */
	async settleSuccess(): Promise<readonly StorageWrite[]> {
		this.#sealed = true;
		if (this.#pendingOperations.size > 0) {
			this.#abortChanges();
			await Promise.allSettled(this.#pendingOperations);
			this.#context = BACKGROUND_CONTEXT;
			throw new Error("Session commit callback settled before its pending Tx operations");
		}
		try {
			// Synchronously prepare every open change; this revokes every draft.
			for (const document of this.#documents) {
				if (document.change !== undefined) document.prepared = document.change.prepare();
			}
			return await this.#assemble();
		} catch (error) {
			this.#abortChanges();
			throw error;
		} finally {
			this.#context = BACKGROUND_CONTEXT;
		}
	}
```

**讲解**

1. `#sealed = true`：之后对 `tx` 的任何调用都抛 `Transaction has settled`（`#assertOpen`，`packages/durable/src/session/transaction.ts:1111-1113`）。
2. 还有未完成的异步操作：放弃所有修改，等它们结束，拒绝 commit。
3. 对每个打开的 change 调用 `prepare()`。这一步同步完成，所有 draft 在这里失效（D2 §4.5）。§7.2 验证了逃出回调的 draft 不能再写。
4. `#assemble()` 组装写入。任何一步失败都放弃所有修改。

最后把 `#context` 换成 `BACKGROUND_CONTEXT`，事务不再持有调用者的 context。

**往哪走**：§4.7。

### 4.7 #assemble() 和 checkpointWhen

**调用位置**：`settleSuccess()`。

`#assemble` 先为每个文档建一个 `DocumentPlan`，再做一串校验：fork 源文档不能在同一个 commit 里修改，新任务和新对话的所有者必须活着，被替换的任务不能已经结束；结束的任务的所有任务文档要退役。这些校验和 D4、D5 有关，这里略过（`packages/durable/src/session/transaction.ts:957-1037`）。最后一段把写入排好：

源码：[`packages/durable/src/session/transaction.ts:1039-1058`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L1039-L1058)

```ts
		const writes = this.#writes;
		for (const value of this.#submissions.values()) writes.push({ type: "submission", value });
		for (const task of this.#tasksById.values()) {
			if (task.write !== undefined) writes.push({ type: "task", value: task.write.record });
		}
		for (const plan of plans) {
			const change = plan.change;
			// Checkpoint predicates run last, after every validation.
			if (plan.content?.type === "document.change" && plan.content.content.kind === "delta" && change?.loaded) {
				const { prepared, version } = change;
				const info = { deltasSinceBase: change.loaded.deltasSinceBase };
				if (change.definition!.checkpointWhen(prepared.value, prepared.ops, info)) {
					plan.content = { ...plan.content, content: { version, kind: "base", value: prepared.value } };
				}
			}
			if (plan.content !== undefined) writes.push(plan.content);
			if (plan.retire) writes.push({ type: "document.retire", id: plan.record.id });
		}
		return writes;
	}
```

一个已加载文档的 plan 怎样产生：

源码：[`packages/durable/src/session/transaction.ts:1222-1244`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L1222-L1244)

```ts
		case "loaded": {
			const loaded = target.document;
			const definition = document.definition!;
			const prepared = document.prepared!;
			const version = definition.version;
			const id = loaded.record.id;
			// A version change stores a base even without operations; otherwise only a change stores a delta.
			const content =
				loaded.storedVersion < version
					? ({ type: "document.change", id, content: { version, kind: "base", value: prepared.value } } as const)
					: prepared.ops.length > 0
						? ({ type: "document.change", id, content: { version, kind: "delta", ops: prepared.ops } } as const)
						: undefined;
			const change = {
				tracker: loaded.tracker,
				prepared,
				version,
				loaded,
				definition,
			};
			return { ...plan, record: loaded.record, change, ...(content === undefined ? {} : { content }) };
		}
	}
```

**讲解**

`planDocument` 对已加载的文档：

- 存储版本比定义旧：必须写 base（迁移）。
- 有操作：先写 delta。
- 没有操作：什么都不写。D2 §2 的"写回原值，ops 为空"在这里就是零写入。

新建的文档总是 `document.create` + base。

然后 `#assemble` 的最后一段对每个 delta 调用一次定义的 `checkpointWhen(value, ops, { deltasSinceBase })`。返回 `true` 就把这次写入换成 base。注释说它"最后运行，在所有校验之后"：checkpoint 判断失败也会放弃整个 commit（spec §3.5）。

存储怎样处理 base：对 `latest` 文档和任务文档，新的 base 允许丢掉更早的记录；对 `rewindable` 文档，旧记录都要留着以支持按条目回读（spec §3.5）。§7.3 在 `MemoryStorage` 里验证这一点。

写入的顺序：表写入（对话、条目在回调里就进了 `#writes`），然后 submission、任务，最后文档。

**往哪走**：§4.8 写入器。

### 4.8 Writer.admit()、#pump()、#flush()

**调用位置**：`#runCommit()` 的最后（`packages/durable/src/session/session.ts:1150`）。

源码：[`packages/durable/src/session/writer.ts:96-158`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/writer.ts#L96-L158)

```ts
	/** Admit a part: register its keys, queue it, and send it at once unless a call is in flight. */
	admit(spec: PartSpec): Part {
		const outcome = Promise.withResolvers<void>();
		outcome.promise.catch(() => {});
		const part: Admitted = {
			...spec,
			outcome: outcome.promise,
			settle: {
				resolve: () => {
					part.done = true;
					outcome.resolve();
				},
				reject: (error) => {
					part.done = true;
					outcome.reject(error);
				},
			},
			done: false,
			gate: Promise.withResolvers<void>(),
		};
		for (const key of spec.keys) this.#unpublished.set(key, part);
		this.#newest = part;
		this.#queue.push(part);
		this.#pump();
		return part;
	}

	/** The gate of the newest unpublished writer of `key`, or `undefined` when it has none. */
	gate(key: string): Promise<void> | undefined {
		return this.#unpublished.get(key)?.gate.promise;
	}

	/** Opens once every part admitted so far is published or never will be. */
	drained(): Promise<void> | undefined {
		return this.#newest?.gate.promise;
	}

	/** Keys with an unpublished writer, for tests that bound memory. */
	get unpublished(): number {
		return this.#unpublished.size;
	}

	#pump(): void {
		if (this.#flight || this.#queue.length === 0) return;
		this.#flight = true;
		const parts = this.#queue;
		this.#queue = [];
		void this.#flush(parts);
	}

	/**
	 * Send and publish `parts`, then what was admitted meanwhile. Never rejects: a throw the writer did not expect fails
	 * the Session and settles the parts (`#abandon()`), so no outcome or gate is stranded.
	 */
	async #flush(parts: readonly Admitted[]): Promise<void> {
		try {
			await this.#send(parts);
		} catch (error) {
			this.#abandon(parts, error);
		}
		this.#flight = false;
		this.#pump();
	}
```

**讲解**

`admit` 给 part 加上两个 Promise：

- `outcome`：发布或失败时结算，`#enqueue` 用它决定 commit 什么时候 resolve。
- `gate`：发布或确定不会发布时打开，后续读这个 part 写过的键时等它。

`#unpublished` 记录每个键最新的未发布写者。`gate(key)` 返回它的 gate，`SessionImpl.touch(key)`（`packages/durable/src/session/session.ts:429-434`）就是在等这个。键的种类在 `keys.ts` 里，例如 `task:9`、`entry-append:1`（一个对话的最新条目）。

`#pump` 是 group commit：没有存储调用在进行时，把队列里所有 part 一起交给 `#flush`；有调用在进行时什么都不做，新 part 留在队列里，等这次调用结束后 `#flush` 末尾再 `#pump()` 一次。所以存储慢的时候，后面的 commit 自动攒成一批（§7.4）。

**往哪走**：§4.9。

### 4.9 Writer.#send() 和 #publish()

源码：[`packages/durable/src/session/writer.ts:160-192`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/writer.ts#L160-L192)

```ts
	/** Store `parts` in one call, then publish them in order; settles every one of them. */
	async #send(parts: readonly Admitted[]): Promise<void> {
		const stored = parts.filter((part) => part.writes !== undefined);
		const seqs: (Seq | undefined)[] = [];
		let result: CommitResult | undefined;
		let rejected: { readonly error: unknown } | undefined;
		const failed = this.#host.failure();
		if (failed !== undefined) {
			for (const part of parts) this.#unpublish(part, new SessionFailed(failed.error));
			return;
		}
		if (stored.length > 0) {
			try {
				result = await this.#host.commit(
					stored.map((part) => part.writes!),
					(index, seq) => {
						seqs[index] = seq;
					},
					withoutAbortSignal(stored[0]!.context),
				);
			} catch (error) {
				rejected = { error };
			}
		}
		// A call that settles once another failure failed the Session reports `SessionFailed` with that first error,
		// whatever it returned (spec §10.1), and publishes nothing.
		const failedMeanwhile = this.#host.failure();
		if (failedMeanwhile !== undefined) {
			for (const part of parts) this.#unpublish(part, new SessionFailed(failedMeanwhile.error));
			return;
		}
		await this.#publish(parts, seqs, result, rejected);
	}
```

`#publish` 按顺序发布每个 part。主循环的前半部分：

源码：[`packages/durable/src/session/writer.ts:215-258`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/writer.ts#L215-L258)

```ts
		for (const part of parts) {
			const index = part.writes === undefined ? undefined : storedIndex++;
			if (stop !== undefined) {
				// A part after a rejected call's last receipt has the call's error; any other part after a stop was not sent,
				// nor any part once something else failed the Session.
				const outcome: FailedOutcome =
					rejected !== undefined && index !== undefined && stop.external !== true
						? { kind: "own", error: rejected.error }
						: SESSION_FAILED;
				failing.push({ part, outcome, published: false });
				continue;
			}
			const seq = index === undefined ? undefined : seqs[index];
			if (index !== undefined && seq === undefined) {
				const error =
					rejected !== undefined
						? rejected.error
						: index === result?.stored && "refused" in result
							? result.refused
							: new Error("Storage committed without adopting the batch");
				failing.push({ part, outcome: { kind: "own", error }, published: false });
				stop = { error, at: part.index };
				continue;
			}
			// A macrotask between two parts: what the first resolves runs before the second is published.
			if (published++ > 0) await hop();
			let failure: PublishFailure | undefined;
			// Failed by something else meanwhile, during the hop or before a storage's scope runs the publication: nothing
			// more of the call is published. Checked right before it would be, after every wait.
			let external: { readonly error: unknown } | undefined;
			const publication = this.#host.publishing(() => {
				external = this.#host.failure();
				if (external !== undefined) return;
				// Its keys leave first, so what its publication schedules sees it published; its waiters wake after it.
				this.#unregister(part);
				// A throw anywhere in it, the read caches included, leaves memory behind what is stored: like an adoption's.
				try {
					if (part.writes !== undefined) this.#host.stored(part.writes);
					failure = part.publish(seq);
				} catch (error) {
					failure = { kind: "adoption", error };
				}
			}, part.context);
			if (publication !== undefined) await publication;
```

**讲解**

`#send` 把有写入的 part 的 `writes` 收集成 `batches`，一次调用 `Storage.commit()`。`receive(index, seq)` 记下每个 batch 的 `seq`。注意传给存储的 context 是 `withoutAbortSignal(...)`：已接纳的 commit 不能被调用者取消（D2 §4.1）。

`#publish` 对每个 part：

- 前面已经停下（某个 part 失败）：后面的 part 都不发布。
- 有写入但没有 `seq`：这个 batch 被存储拒绝或调用失败，Session 将会失败。
- 两个 part 之间 `await hop()`，让出一个宏任务：前一个 commit 解开的等待者先运行，再发布下一个（spec §4："Two commits of one storage call are published a macrotask apart"）。
- `publishing(...)` 里：先注销它的键，再更新读缓存（`stored`），再调用 `part.publish(seq)`，也就是 `SessionImpl.#adopt`（§4.12）。

失败处理（循环后半段和 `#abandon`）规定了哪些调用者得到自己的错误、哪些得到 `SessionFailed`。规则在 spec §10.1，这里不展开。要点是：存储错误让 Session 失败，失败之后不再发布任何东西，重新打开后从已存储的状态恢复。

**往哪走**：§4.11 存储后端（§4.10 是 deferred 分支）。

### 4.10 deferred commit：holdable() 和 #hold()

**调用位置**：`#runCommit()` 第 6 步。Harness 只在两种情况下用 deferred commit：流式回答的部分内容和运行中工具的输出，都写在 `pi.live` 里（`PROGRESS_COMMIT`，`packages/durable/src/harness/live.ts:235`）。

源码：[`packages/durable/src/session/transaction.ts:796-818`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L796-L818)

```ts
	/**
	 * Internal: what a deferred commit holds in memory instead of storing (`CommitPersistence`), each changed incarnation
	 * with its operations and whether they selected a base; `undefined` when the commit must be stored. It may be held
	 * only when every write changes an existing `latest`, `initial`-fork conversation document at its stored version.
	 */
	holdable(): { readonly loaded: LoadedDocument; readonly ops: readonly Op[]; readonly base: boolean }[] | undefined {
		if (this.#writes.some((write) => write.type !== "document.change")) return undefined;
		const held = [];
		for (const plan of this.#plans) {
			const { content, change } = plan;
			if (content === undefined && !plan.retire) continue;
			if (plan.retire || change?.loaded === undefined || content?.type !== "document.change") return undefined;
			const { loaded, prepared, version } = change;
			const record = loaded.record;
			if (record.scope.kind !== "conversation" || record.history !== "latest" || record.fork !== "initial") {
				return undefined;
			}
			// A migration stores a base at once.
			if (loaded.storedVersion !== version) return undefined;
			held.push({ loaded, ops: prepared.ops, base: content.content.kind === "base" });
		}
		return held;
	}
```

源码：[`packages/durable/src/session/session.ts:1206-1248`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/session.ts#L1206-L1248)

```ts
	/**
	 * Hold a deferred commit (`Transaction.holdable()`): adopt it in memory, queue its changes per document, pinned, by
	 * the earliest deadline of the document's held commits, and show it to the Session's own observers.
	 */
	#hold(
		tx: Transaction,
		held: NonNullable<ReturnType<Transaction["holdable"]>>,
		withinMs: number,
		context: Context,
	): PublishFailure | undefined {
		let documents: DocumentCommitChange[];
		try {
			documents = tx.adoptHeld();
		} catch (error) {
			// As a stored commit's: memory could not adopt it; the writer fails the Session.
			return { kind: "adoption", error };
		}
		const deadline = this.#clock() + withinMs;
		for (const { loaded, ops, base } of held) {
			const id = loaded.record.id;
			let hold = this.#holds.get(id);
			if (hold === undefined) {
				const unpin = this.#pinDocument(loaded.addressId, id);
				hold = { loaded, pending: new PendingOps(), base: false, deadline, unpin };
				this.#holds.set(id, hold);
			}
			hold.pending.push(ops);
			if (base) hold.base = true;
			if (deadline < hold.deadline) hold.deadline = deadline;
		}
		this.#armHoldTimer();
		const publication: ObservedCommit = { kind: "memory", changes: documents };
		let failure: PublishFailure | undefined;
		for (const listener of [...this.#internalListeners]) {
			// Our own observers are synchronous.
			try {
				listener(publication, context);
			} catch (error) {
				failure ??= { kind: "listener", error };
			}
		}
		return failure;
	}
```

**讲解**

`holdable()` 判断这个 commit 能不能暂存：所有写入都必须是已存在的、`history: "latest"`、`fork: "initial"` 的对话文档的修改，并且没有版本迁移。任何表写入、文档创建、退役都让它变回普通 commit。`pi.live` 正好满足条件（D1 §3.5）。

`#hold()` 在内存里采纳它（`tx.adoptHeld()`，tracker 前进），把它的操作推进这个文档的 `PendingOps`（D2 §4.9），记下最早的截止时间，通知 Session 自己的观察者（文档 watch 和对话视图）。外部的 `observeCommits()` 观察者看不到它，因为它还没存储。

暂存的操作在以下任一时刻存储（spec §4.1）：

1. 下一个读或写这个文档的普通 commit：`tx.takeHeld()` 把它们放在这个 commit 自己的操作前面，一起写入（§4.3 第 7 步）。
2. 截止时间到：`#armHoldTimer` 的定时器触发 `#storeHolds`，所有暂存的文档在一次存储调用里写入。
3. `close()`。
4. 文档被从缓存中丢弃之前。

崩溃时丢失的是还没存储的暂存修改，最多 `withinMs` 加上存储延迟（默认 1 秒）。§7.3 演示合并和丢失。

**往哪走**：§4.11。

### 4.11 MemoryStorage

**调用位置**：`Writer.#send()` → `GuardedStorage.commit()` → `CachedStorage` → `MemoryStorage.commit()`。

源码：[`packages/durable/src/storage/memory.ts:264-317`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/storage/memory.ts#L264-L317)

```ts
	/**
	 * Apply `batches` one by one, each checked against the state the earlier ones left (`prepareCommit()`, which only
	 * checks and detaches, so a batch it refuses changed nothing); stops at the first one refused. Memory is durable at
	 * once; the receipts follow the last batch.
	 */
	async commit(
		batches: readonly (readonly StorageWrite[])[],
		receive: (index: number, seq: Seq) => void,
		_context: Context,
	): Promise<CommitResult> {
		this.assertOpen();
		const seqs: Seq[] = [];
		let refused: { readonly error: unknown } | undefined;
		try {
			for (const writes of batches) {
				let prepared: PreparedMemoryCommit;
				try {
					prepared = this.prepareCommit(writes);
				} catch (error) {
					refused = { error };
					break;
				}
				seqs.push(prepared.apply());
			}
		} finally {
			// Also when a batch's apply throws: the batches applied before it are stored, and their receipts stand.
			for (const [index, seq] of seqs.entries()) receive(index, seq);
		}
		return refused === undefined ? { stored: seqs.length } : { stored: seqs.length, refused: refused.error };
	}

	/** Validate and detach one commit without changing observable state. */
	prepareCommit(writes: readonly StorageWrite[], seq: Seq = seqFromNumber(this.nextSeq)): PreparedMemoryCommit {
		this.assertOpen();
		if (!Number.isSafeInteger(seq) || seq < this.nextSeq) {
			throw new Error(`Commit sequence ${seq} does not strictly increase`);
		}
		const detachedWrites = freeze(this.resolveDocumentCopies(writes.map((write) => clone(write))));
		this.checkGlobalIds(detachedWrites);
		const documentActions = this.prepareDocumentActions(detachedWrites);
		this.checkDocumentActions(documentActions);
		let applied = false;
		return {
			seq,
			writes: detachedWrites,
			apply: () => {
				if (!applied) {
					applied = true;
					this.applyPreparedCommit(detachedWrites, documentActions, seq);
				}
				return seq;
			},
		};
	}
```

文档内容的存储：

源码：[`packages/durable/src/storage/memory.ts:845-887`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/storage/memory.ts#L845-L887)

```ts
	private applyDocumentActions(actions: ReadonlyMap<DocumentId, DocumentAction>, seq: Seq): void {
		for (const [id, action] of actions) {
			let stored = this.state.documents.get(id);
			if (action.create !== undefined) {
				const record: DocumentRecord = {
					...action.create,
					createdAt: seq,
					...(action.retire ? { retiredAt: seq } : {}),
				};
				stored = { record, revisions: [{ ...action.content!, seq }] };
				this.state.recordTypes.set(id, "document");
				this.state.documents.set(id, stored);

				const key = recordAddressKey(record);
				let address = this.state.documentAddresses.get(key);
				if (address === undefined) {
					address = { ids: [] };
					this.state.documentAddresses.set(key, address);
				}
				insertSorted(address.ids, id);

				let scopeIds = this.state.documentIdsByScope.get(scopeKey(record.scope));
				if (scopeIds === undefined) {
					scopeIds = [];
					this.state.documentIdsByScope.set(scopeKey(record.scope), scopeIds);
				}
				insertSorted(scopeIds, id);
				this.nextId = Math.max(this.nextId, id + 1);
			} else if (action.content !== undefined) {
				const revision = { ...action.content, seq } as DocumentRevision;
				if (revision.kind === "base" && isCurrentOnly(stored!.record)) stored!.revisions = [revision];
				else stored!.revisions.push(revision);
			}

			if (action.retire && action.create === undefined) stored!.record = { ...stored!.record, retiredAt: seq };
			if (action.retire && isCurrentOnly(stored!.record)) stored!.revisions = [];
			if (action.create !== undefined || action.retire) {
				const address = this.state.documentAddresses.get(recordAddressKey(stored!.record))!;
				if (action.retire && address.currentId === id) delete address.currentId;
				if (action.create !== undefined && !action.retire) address.currentId = id;
			}
		}
	}
```

读文档时重放：

源码：[`packages/durable/src/storage/memory.ts:687-704`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/storage/memory.ts#L687-L704)

```ts
	private materializeDocument(id: DocumentId, at: DocumentPoint): StoredDocument | undefined {
		const stored = this.state.documents.get(id);
		if (stored === undefined) return undefined;
		if (at !== "current" && isCurrentOnly(stored.record)) {
			throw new StorageRequestError(`Document ${id} does not retain historical content`);
		}
		if (!isAliveAt(stored.record, at)) return undefined;
		const revisions = at === "current" ? stored.revisions : stored.revisions.filter((revision) => revision.seq <= at);
		let baseIndex = revisions.length - 1;
		while (baseIndex >= 0 && revisions[baseIndex]!.kind !== "base") baseIndex--;
		const base = revisions[baseIndex];
		if (base?.kind !== "base") throw new Error(`Document ${id} is missing a required base`);
		const value = applyImmutableBatches(
			base.value,
			documentDeltaBatches(id, base.version, revisions, baseIndex + 1),
		) as JsonObject;
		return { record: stored.record, version: base.version, value, deltasSinceBase: revisions.length - baseIndex - 1 };
	}
```

**讲解**

`MemoryStorage` 是参考实现（类注释："Detached in-memory reference implementation"）。它的价值在于把契约写成可读的代码：

- `commit` 对每个 batch 先 `prepareCommit`：深复制并冻结写入、解析 `document.copy`、检查全局 ID 不重复、检查文档动作合法（delta 前必须有 base、跨版本必须 base、一个地址同时只有一个当前实例）。任何一项失败就是"拒绝"，这个 batch 什么都没改，前面的 batch 已经存好。
- 检查全部通过后 `apply()` 才改状态。所以每个 batch 是原子的。
- 回执在 `finally` 里发出：即使后面某个 batch 的 apply 抛错，前面已存的 batch 的回执也照发。
- `applyDocumentActions`：对 `latest`、任务、Session 文档（`isCurrentOnly`），新的 base 替换掉所有旧 revision；`rewindable` 文档追加。退役的 current-only 文档清空 revision。
- `materializeDocument`：找到最近的 base，用 `applyImmutableBatches` 重放后面的 delta（D2 §4.8），同时算出 `deltasSinceBase`。

存储进来的值都被深复制，读出去的值也被复制（`clone`），模拟序列化后端的所有权边界：调用者改不到存储里的数据（spec §4 "Storage ownership"）。

`SqliteStorage.commit()` 的结构相同：一个 SQL 事务里依次检查、写入每个 batch，第一个被拒绝的 batch 处停下，提交事务后才发回执。

源码：[`packages/durable/src/storage/sqlite/storage.ts:213-259`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/storage/sqlite/storage.ts#L213-L259)

```ts
	/**
	 * Commit `batches` in one SQL transaction, each with its own seq. Each batch is checked before it writes anything,
	 * against the state the earlier ones left, and the first one refused ends the batch: the transaction commits the ones
	 * before it. No savepoints, which Durable Objects do not allow: the checks run before the writes instead.
	 */
	async commit(
		batches: readonly (readonly StorageWrite[])[],
		receive: (index: number, seq: Seq) => void,
		_context: Context,
	): Promise<CommitResult> {
		this.assertOpen();
		const seqs: Seq[] = [];
		let refused: { readonly error: unknown } | undefined;
		let candidateNextId = this.nextId;
		await this.db.transaction(async (transaction) => {
			const metadata = await this.readMetadata(transaction);
			let next = metadata.next_seq;
			// Whether a database call of a batch's checks failed: then the failure is an unknown outcome, not a refusal.
			const calls: CheckCalls = { failed: false };
			const checked = watchCalls(transaction, calls);
			for (const writes of batches) {
				let documentActions: Map<DocumentId, DocumentAction>;
				// Only Durable's own checks refuse a batch, before it wrote anything: the ones before it stand. A failure of
				// the database rejects the call, and rolls back the batches before it with the transaction.
				calls.failed = false;
				try {
					documentActions = this.prepareDocumentActions(writes);
					await this.checkBatch(checked, writes, documentActions);
				} catch (error) {
					if (calls.failed) throw error;
					refused = { error };
					break;
				}
				const seq = seqFromNumber(next++);
				await this.applyBatch(transaction, writes, documentActions, seq);
				seqs.push(seq);
				candidateNextId = Math.max(candidateNextId, this.candidateNextId(writes));
			}
			if (seqs.length > 0) {
				await this.writeMetadata(transaction, Math.max(Number(metadata.next_id), candidateNextId), next);
			}
		});
		this.nextId = Math.max(this.nextId, candidateNextId);
		// `COMMIT` returned: the stored batches are durable.
		for (const [index, seq] of seqs.entries()) receive(index, seq);
		return refused === undefined ? { stored: seqs.length } : { stored: seqs.length, refused: refused.error };
	}
```

注释说它不用 savepoint，因为 Cloudflare Durable Object 不支持：检查放在写入之前做，被拒绝的 batch 什么都没写。

**往哪走**：§4.12 回到 Session 发布。

### 4.12 发布：#adopt() 和 Transaction.adopt()

**调用位置**：`Writer.#publish()` → `part.publish(seq)` → `SessionImpl.#adopt()`。

源码：[`packages/durable/src/session/session.ts:1162-1184`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/session.ts#L1162-L1184)

```ts
	/**
	 * Publish a stored commit (`Writer`), after the read caches took its writes: adopt it in memory and run its
	 * publication. A throw of the adoption leaves memory behind durable state; the writer fails the Session with it.
	 */
	#adopt(
		tx: Transaction,
		writes: readonly StorageWrite[],
		taken: readonly Hold[],
		seq: Seq,
		context: Context,
	): PublishFailure | undefined {
		let documents: AdoptedDocuments;
		try {
			documents = tx.adopt(seq);
		} catch (error) {
			return { kind: "adoption", error };
		}
		this.#indexTaskDocuments(tx, writes, documents, seq);
		const failure = this.#publish(seq, writes, documents, context);
		if (taken.length > 0) this.#release(taken);
		for (const document of documents.stored) this.#touchDocument(addressId(document.record));
		return failure === undefined ? undefined : { kind: "listener", error: failure.error };
	}
```

`Transaction.adopt` 的前半部分：

源码：[`packages/durable/src/session/transaction.ts:904-936`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/session/transaction.ts#L904-L936)

```ts
	/** Adopt every prepared change by pointer swap after Storage success and describe the publication. */
	adopt(seq: Seq): AdoptedDocuments {
		const publications: DocumentCommitChange[] = [];
		// Only when the commit stored held changes: then what Storage got differs from what memory adopted.
		let stored: DocumentCommitChange[] | undefined;
		for (const plan of this.#plans) {
			const committed = "createdAt" in plan.record;
			let record: DocumentRecord = committed ? (plan.record as DocumentRecord) : { ...plan.record, createdAt: seq };
			if (plan.retire) record = { ...record, retiredAt: seq };
			const change = plan.change;
			if (change !== undefined) {
				const { tracker, prepared, loaded, version } = change;
				// A new incarnation is adopted unless it retires in the same commit; a loaded one only when it changed.
				if (loaded === undefined ? !plan.retire : prepared.ops.length > 0) tracker.adopt(prepared);
				else prepared.abort();
				if (loaded !== undefined) {
					if (loaded.storedVersion < version) loaded.storedVersion = version;
					if (plan.content?.type === "document.change") {
						if (plan.content.content.kind === "base") loaded.deltasSinceBase = 0;
						else loaded.deltasSinceBase++;
					}
				} else if (!plan.retire) {
					this.#host.install({
						addressId: plan.addressId,
						record,
						storedVersion: version,
						valueVersion: version,
						deltasSinceBase: 0,
						tracker,
					});
				}
			}
			if (plan.retire && committed) this.#host.evict(plan.addressId, record.id);
```

**讲解**

`Transaction.adopt(seq)` 对每个文档 plan：

- 新建的文档：`tracker.adopt(prepared)`，然后 `#host.install()` 放进 Session 的文档缓存，`createdAt` 填成这次的 `seq`。
- 已加载的文档：有操作就 `adopt`，没有就 `abort`。更新 `storedVersion`；写了 base 就把 `deltasSinceBase` 归零，写了 delta 就加 1。
- 退役的文档：从缓存中移除。

这一步之后内存里的文档才变成新值。D2 §4.6 说过，`adopt` 只是指针替换，不会失败，所以存储成功后内存一定能跟上。

`#adopt` 接着维护任务文档的索引，调用 `#publish`（发 telemetry 事件、通知 `observeCommits()` 观察者和 Session 自己的观察者：文档 watch、对话视图、调度器、submission 等待者），释放这个 commit 带走的暂存修改。D6 读观察者。

发布完成后 `part.outcome` resolve，`#enqueue` 交付结果，调用者的 `await commit(...)` 返回。

**往哪走**：§5 状态机。

---

## 5. 状态机

### 5.1 一个 commit

```
 commitWith()  session.ts:464
   │ assertUsable / #deferral 失败 → 立即拒绝 (session.ts:474-475)
   ▼
 ┌──────────┐ #enqueue：接在 #tail 后 (session.ts:1597)
 │ 排队      │
 └────┬─────┘
      ▼ #onLine (session.ts:1603)
 ┌──────────┐ 回调抛错 → settleFailure，拒绝 (session.ts:1093-1097)
 │ 回调运行  │ settleSuccess 失败 → 拒绝 (session.ts:1101-1104)
 └────┬─────┘
      │ 没有写入 → 直接 resolve (session.ts:1106-1110)
      │ 只读 → ReadOnlySession (session.ts:1111-1116)
      │
      ├─ deferred 且 holdable ─▶ ┌────────┐ 前面的发布完 → #hold (session.ts:1127)
      │  (session.ts:1118-1132)  │ 暂存    │ resolve；修改留在 PendingOps，
      │                          └────────┘ 等下一个 commit / 定时器 / close 存储
      ▼ Writer.admit (session.ts:1150)
 ┌──────────┐ #pump：无调用在进行时立即发送，否则等 (writer.ts:138-144)
 │ 已接纳    │ 提交线继续运行下一个回调
 └────┬─────┘
      ▼ Storage.commit (writer.ts:173-179)
 ┌──────────┐ 被拒绝 / 调用失败 → Session 失败，不发布 (writer.ts:228-237)
 │ 存储中    │
 └────┬─────┘
      ▼ receive(index, seq)
 ┌──────────┐ publishing → stored → part.publish → #adopt (writer.ts:245-257)
 │ 已发布    │ adoption 抛错 → Session 失败 (writer.ts:264-267)
 └────┬─────┘
      ▼ part.settle.resolve，gate 打开 (writer.ts:281-282)
 commit 的 Promise resolve（按线上顺序，session.ts:1633-1644）
```

### 5.2 一个已加载文档的 deltasSinceBase

```
            document.create（base）transaction.ts:1205
                   │  install deltasSinceBase = 0 (transaction.ts:926-933)
                   ▼
   ┌──────────────────────────────┐
   │ deltasSinceBase = k           │ ── 有操作，checkpointWhen 为 false ──▶ k + 1
   │                               │    delta (transaction.ts:1232-1233, 923)
   │                               │ ── checkpointWhen 为 true / 版本迁移 ──▶ 0
   │                               │    base (transaction.ts:1050-1051, 1230-1231, 922)
   └──────────────────────────────┘ ── 没有操作 ──▶ 不写，k 不变 (transaction.ts:1234)
```

---

## 6. 设计要点

### 6.1 表写入只进内存，到最后才组装

**做法。** 回调里的表写入推进 `#writes`，文档修改留在 overlay 里。回调结束后一次性准备、校验、组装成一个 batch。

**理由。** 原子性不需要存储支持回滚：回调失败时什么都没离开事务。存储只看到完整的、已经校验过的 batch。代价是"写后读"不可用，用 `ReadAfterWrite` 明确禁止，而不是返回过时的结果。

**代码位置。** `packages/durable/src/session/transaction.ts:1132-1150`，`packages/durable/src/session/transaction.ts:769`。

### 6.2 一条线运行回调，存储和线重叠

**做法。** 回调串行，存储调用在线外进行。存储调用进行时，新接纳的 commit 排队，下一次调用一起发送，各自有 `seq`，按顺序发布。

**理由。** 串行的回调不需要锁，也不会有冲突的写入。group commit 让慢存储（SQLite 的 fsync、远程存储的往返）的吞吐量随负载提高。读未发布的键时等它发布（`touch`），所以没有任务会看到还没存储的状态。设计说明在 `packages/durable/docs/group-commit.md`。

**代码位置。** `packages/durable/src/session/session.ts:1595`，`packages/durable/src/session/writer.ts:138`。

### 6.3 文档的存储表示由定义决定

**做法。** 每次修改存 delta 还是 base，由文档定义的 `checkpointWhen` 决定，而且没有默认值。存储不数字节、不自己生成检查点（spec §3.5）。

**理由。** 只有定义知道什么时候值小、什么时候适合存完整值。`pi.live` 在空闲时存 base，所以它的 delta 链最长覆盖一次 generation；`pi.usage` 和 `pi.agent` 每次都存 base。一个永不 checkpoint 的定义会让重放无限变长，spec 把它定义为定义的 bug。

**代码位置。** `packages/durable/src/session/transaction.ts:1046-1053`，`packages/durable/src/harness/live.ts:219-222`。

### 6.4 deferred commit 是"允许"，不是"要求"

**做法。** 调用者标记 deferred，但只有满足 `holdable()` 条件的 commit 才真的暂存，其余照常立即存储。

**理由。** 流式输出每秒很多次，每次都写存储代价太高；丢掉最后一秒的流式输出可以接受。限制在 `latest` + `initial` 的对话文档上，保证暂存的东西没有历史意义、不会被 fork 复制。任何依赖它的持久写入都要在 commit 里读它，读它的 commit 会先把它存储（spec §4.1 "Consistency"）。

**代码位置。** `packages/durable/src/session/transaction.ts:801`，`packages/durable/src/session/session.ts:1210`。

---

## 7. 实验

### 7.1 先读后写

**问题。** §2.2 的 `raw` 场景里，表写入之后的表读让整个 commit 失败。把同一个表读挪到 `appendEntry` 之前（`read-first` 场景），结果会怎样？

**预测。** 表读发生在第一次表写入之前，`#read` 不抛错（§4.4）。commit 成功：一次 `Storage.commit()`，里面有一个条目和一个文档创建。条目数 1，`lab.notes` 有值。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts read-first
```

```
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #2 {"id":2}
[Storage.commit 第 2 次] 1 个 batch
  seq=2
    entry #3 lab.note model=[]
    document.create #4 lab.notes base
会话里的条目数： 1
lab.notes 文档： {"text":"写在表写入之后","n":0}
```

**结论。** 预测成立。同样的三个操作，只是顺序不同，结果从"全部丢弃"变成"全部存储"。

### 7.2 draft 逃出回调

**问题。** 把 `tx.doc()` 返回的 draft 存到外部变量，回调结束后再写它。会怎样？文档的值是哪一次写的？

**预测。** `settleSuccess()` 已经对 change 调用了 `prepare()`，overlay 不再 `open`，写入抛 `TypeError: Cannot use a settled overlay`（D2 §4.4）。文档值是回调里写的。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts revoked
```

```
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #2 {"id":2}
[Storage.commit 第 2 次] 1 个 batch
  seq=2
    document.create #3 lab.notes base
回调结束后写 draft： TypeError Cannot use a settled overlay
lab.notes 文档： {"text":"回调里写","n":0}
```

**结论。** 预测成立。spec §3.4 里的示例 `escaped!.generation = undefined; // throws: the draft was revoked` 说的就是这个。

### 7.3 base、delta 和 deferred commit

**问题 1。** 两个文档 `lab.notes`（`latest`）和 `lab.rewind-notes`（`rewindable`），`checkpointWhen` 都是 `deltasSinceBase >= 2`。连续 5 次各改一个数字。每次存 base 还是 delta？最后 `MemoryStorage` 里各留下哪些 revision？

**预测。** 脚本的第 1 次 commit 通过 `tx.doc()` 创建文档，写 `document.create` + base，`deltasSinceBase = 0`。后面 4 次是修改：`deltasSinceBase` 依次是 0、1、2、0，所以依次存 delta、delta、base、delta。`latest` 文档的 base 替换掉旧 revision，只留下最后一个 base 和它之后的 delta；`rewindable` 文档全部保留。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts checkpoint
```

```
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #2 {"id":2}
[Storage.commit 第 2 次] 1 个 batch
  seq=2
    document.create #3 lab.notes base
    document.create #4 lab.rewind-notes base
[Storage.commit 第 3 次] 1 个 batch
  seq=3
    document.change #3 lab.notes delta 1 ops [s]
    document.change #4 lab.rewind-notes delta 1 ops [s]
[Storage.commit 第 4 次] 1 个 batch
  seq=4
    document.change #3 lab.notes delta 1 ops [s]
    document.change #4 lab.rewind-notes delta 1 ops [s]
[Storage.commit 第 5 次] 1 个 batch
  seq=5
    document.change #3 lab.notes base
    document.change #4 lab.rewind-notes base
[Storage.commit 第 6 次] 1 个 batch
  seq=6
    document.change #3 lab.notes delta 1 ops [s]
    document.change #4 lab.rewind-notes delta 1 ops [s]
MemoryStorage 里 #3 lab.notes 保留的 revisions： base@seq5 delta@seq6
MemoryStorage 里 #4 lab.rewind-notes 保留的 revisions： base@seq2 delta@seq3 delta@seq4 base@seq5 delta@seq6
当前值： {"text":"","n":5}
```

**结论。** 预测成立。`lab.notes` 只剩 `base@seq5 delta@seq6`，`lab.rewind-notes` 保留了从 seq2 起的全部 5 个 revision。

**问题 2。** 两次 deferred commit 把 `text` 改成 `"A"`、`"AB"`，然后一次普通 commit 改 `n`。会调用几次 `Storage.commit()`？写入是什么？

**预测。** 两次 deferred commit 不调用存储（§4.10），`snapshot` 已经是 `"AB"`。普通 commit 读写同一个文档，`takeHeld()` 把暂存的操作放在它自己的操作前面：`PendingOps` 把 `a "A"` 和 `a "B"` 合并成 `a "AB"`（D2 §4.9），加上自己的 `s n 1`，一次 delta 两个操作。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts deferred-flush
```

```
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #2 {"id":2}
[Storage.commit 第 2 次] 1 个 batch
  seq=2
    document.create #3 lab.notes base
== deferred commit 1：text = "A"
== deferred commit 2：text = "AB"
snapshot： {"text":"AB","n":0}
== 普通 commit：n = 1
[Storage.commit 第 3 次] 1 个 batch
  seq=3
    document.change #3 lab.notes delta 2 ops [["a",["text"],"AB"],["s",["n"],1]]
```

**结论。** 预测成立。日志里两次 deferred commit 之间没有 `Storage.commit`。

**问题 3。** 在 SQLite 文件上做一次 deferred commit，`withinMs = 60000`，然后不 close 直接 `process.exit(0)`。重新打开后读到哪个值？

**预测。** 暂存的修改还没到截止时间，也没有后续 commit 和 close，所以没存储。重新打开读到上一次普通 commit 存的 `"已存储"`。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts deferred-crash /tmp/pi-lab/m11a-deferred-1/session.sqlite; node --conditions=source --experimental-strip-types m11a-durable.ts deferred-reopen /tmp/pi-lab/m11a-deferred-1/session.sqlite
```

```
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #2 {"id":2}
[Storage.commit 第 2 次] 1 个 batch
  seq=2
    document.create #3 lab.notes base
== deferred commit，withinMs=60000
deferred commit 已 resolve；snapshot： {"text":"只在内存里","n":0}
不 close，直接退出进程（模拟崩溃）
重新打开后 snapshot： {"text":"已存储","n":0}
```

**结论。** 预测成立。这就是 spec §1 不变式 3 说的"a crash loses it"。Harness 只把流式部分回答和运行中工具输出放进 deferred commit，所以崩溃只丢这一部分；D5 读 generation 怎样在重启后把被打断的请求当成一次失败的尝试。

### 7.4 group commit

**问题。** 存储每次调用慢 20ms。不 await，连续发起 3 个 commit。`Storage.commit()` 被调用几次？回调和 resolve 的顺序是什么？

**预测。** 第 1 个 commit 的回调先运行，接纳后立即发送（没有调用在进行）。存储调用进行的 20ms 里，提交线接着运行回调 2 和 3，它们留在写入器队列里。第 1 次调用结束后，`#flush` 再 `#pump` 一次，把 2 和 3 作为两个 batch 一起发送。所以总共 2 次调用（不算建对话的那次），第 2 次有 2 个 batch、`seq` 连续。resolve 顺序是 1、2、3。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-durable.ts group
```

```
[Storage.commit 第 1 次] 1 个 batch
  seq=1
    conversation #2 {"id":2}
== 连续发起 3 个 commit，不 await
  回调 1 运行
[Storage.commit 第 2 次开始] 1 个 batch
  回调 2 运行
  回调 3 运行
[Storage.commit 第 2 次] 1 个 batch
  seq=2
    entry #3 lab.note model=[]
[Storage.commit 第 3 次开始] 2 个 batch
  commit 1 resolve
[Storage.commit 第 3 次] 2 个 batch
  seq=3
    entry #4 lab.note model=[]
  seq=4
    entry #5 lab.note model=[]
  commit 2 resolve
  commit 3 resolve
```

**结论。** 预测成立。`回调 2 运行` 和 `回调 3 运行` 出现在第 2 次存储调用开始之后、结束之前，说明提交线没有等存储。

---

## 8. 自测与衔接

### 8.1 自测

1. 一个 commit 回调里先 `tx.doc(LiveDoc, id)` 修改文档，再 `tx.appendEntry(...)`，再读 `tx.doc(LiveDoc, id)`。第二次 `doc()` 会抛 `ReadAfterWrite` 吗？
2. 两个 commit 在同一次 `Storage.commit()` 里存储。第一个 commit 的调用者的 `await` 返回时，第二个 commit 发布了吗？
3. `MemoryStorage` 的 `commit` 收到 3 个 batch，第 2 个的文档检查失败。哪些 batch 被存储了？Session 会怎样？

<details>
<summary>答案</summary>

1. 不会。`ReadAfterWrite` 只限制表读（`#read`），文档访问不受影响（spec §4 "Document access and read-your-writes remain available after table writes"）。而且第二次 `doc()` 返回的是同一个 draft（`packages/durable/src/session/transaction.ts:609`）。
2. 不一定。两个 part 的发布之间隔一个宏任务（`hop()`，`packages/durable/src/session/writer.ts:240`），第一个 commit 解开的等待者在第二个发布之前运行。
3. 第 1 个 batch 存储了并收到回执；第 2、3 个没有。`commit()` 返回 `{ stored: 1, refused }`。写入器发布第 1 个 part，然后让 Session 失败（`packages/durable/src/session/writer.ts:228-237`）。第 2、3 个 commit 的调用者得到错误，之后的所有操作得到 `SessionFailed`，必须重新打开。

</details>

### 8.2 衔接

本篇读的是 commit 本身。D4 从 `defineTask()` 读起，看任务怎样用 `runtime.commit()` 把状态变化和副作用的意图写进同一个 commit，调度器怎样在 `Harness.open()` 时把 `running` 的任务改回 `pending` 并继续它们：[D4 任务与调度](D4-tasks-scheduler.md)。
