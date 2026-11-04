# M11 durable · D2 durable 用到的 chord：context、delta、sqlite

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](../README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。上一篇：[D1 概念](D1-concepts.md)。下一篇：[D3 存储与事务](D3-storage-transactions.md)。

---

## 1. 这个模块是什么

### 1.1 职责

chord（源码目录 `packages/chord`，npm 包 `@earendil-works/chord`）是一个约 5 万行的宿主运行时：插件、契约、远程状态、RPC。durable 只用到它的三小块：

- **`chord/context`**：`Context` 类型。durable 的每个异步调用都带一个 `Context`，用来传取消信号和 telemetry span。
- **`chord/delta`**：不可变 JSON 的修订追踪。durable 的每个文档在内存里是一个 `Tracker`，commit 回调拿到的是它的 `Draft`，改完以后得到一批 `Op`。存储里文档的 delta 就是这些 `Op`。
- **`chord/sqlite`**：一个很薄的异步 SQLite 接口，加上 Node 的实现和迁移工具。durable 的 SQLite 后端建在它上面。

另外，durable 从 chord 根入口导入 `Context`、`Draft`、`JsonValue` 等类型，以及 `copyJson()`（把任意值复制成严格 JSON，`packages/chord/src/json.ts:31`）。观察用的 `Watch` 类型在 D6 讲。本篇不讲 chord 的其他部分。

durable 的源码里，从 chord 各入口导入的次数：

```bash
cd $PI/packages/durable/src && grep -rhoE 'from "@earendil-works/chord[^"]*"' . | sort | uniq -c | sort -rn
```

```
  53 from "@earendil-works/chord"
  15 from "@earendil-works/chord/delta"
  13 from "@earendil-works/chord/context"
  11 from "@earendil-works/chord/sqlite"
   6 from "@earendil-works/chord/next"
   3 from "@earendil-works/chord/output"
   2 from "@earendil-works/chord/sqlite/node"
   2 from "@earendil-works/chord/sqlite/cloudflare"
```

`chord/next` 用在 `storage/store` 后端和 registry 的类型上，`chord/output` 是工具输出的截断工具函数（`truncate.ts`、`harness/tool.ts`、`tools/read.ts`）。两者都不在本篇范围内。

### 1.2 在调用链中的位置

D2 的代码不是一条调用链，而是被 D1、D3 各处调用的工具。下面标出 durable 在哪里调用它们：

```
Harness.open(storage, options, context)          context：调用方传入，取消只影响这次调用
└─ 失败时 close(withoutAbortSignal(context))      chord/context
Session.commit(change, context)                  [D3]
├─ tx.doc(token, id)
│   └─ loaded.tracker.beginChange() → Draft      chord/delta
├─ 回调里改 draft
├─ settleSuccess() → change.prepare() → Op[]     chord/delta
├─ Writer → Storage.commit(..., withoutAbortSignal(context))
│   ├─ MemoryStorage：applyImmutableBatches()    chord/delta
│   └─ SqliteStorage：db.transaction()           chord/sqlite
└─ 发布时 tracker.adopt(prepared)                 chord/delta
deferred commit 暂存的操作 → PendingOps           chord/delta
openNodeSqliteStorage(path)
└─ openNodeSqliteDatabase() + applySqliteMigrations()  chord/sqlite
```

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/chord/src/context/index.ts` | 161 | `Context`、`BACKGROUND_CONTEXT`、`withAbortSignal`、`awaitWithContext` | 精读 |
| `packages/chord/src/delta/README.md` | 262 | delta 的使用规则（所有权、可变性） | 通读 |
| `packages/chord/src/delta/index.ts` | 742 | `Op` 类型、`apply`、`applyImmutable(Batches)`、编码器 | 读 `Op` 和 `applyImmutableBatches` |
| `packages/chord/src/delta/tracker.ts` | 2319 | `track()`、overlay draft、生成 `Op` | 读公共接口、`prepare`、`adopt`、`appendText`；overlay 的内部实现略 |
| `packages/chord/src/delta/pending.ts` | 173 | `PendingOps`：折叠积压的操作 | 精读 |
| `packages/chord/src/delta/draft.ts` | 10 | `Draft<T>` 类型 | 通读 |
| `packages/chord/src/delta/diff.ts` | 525 | `diffRevisions()` | 略（durable 不用） |
| `packages/chord/src/sqlite/database.ts` | 41 | `SqliteExecutor`、`SqliteDatabase` 接口 | 精读 |
| `packages/chord/src/sqlite/node.ts` | 219 | `node:sqlite` 的实现 | 读 `transaction` 和打开 |
| `packages/chord/src/sqlite/migrations.ts` | 43 | `applySqliteMigrations` | 精读 |
| `packages/chord/src/sqlite/schema.ts` | 9 | 表名前缀校验 | 通读 |
| `packages/chord/src/sqlite/cloudflare.ts` | 192 | Durable Object 的实现 | 略 |

行数用 `wc -l` 统计。

### 1.4 读完能回答的问题

1. 取消一个 `Context` 会取消它正在等待的工作吗？
2. 一个文档 draft 在 commit 回调结束后还能写吗？为什么？
3. `prepare()` 和 `adopt()` 分成两步，对 durable 有什么用？
4. 一个流式回答每次追加几个字符，存储里的 delta 是什么样子？
5. durable 的 SQLite 表是谁建的？表结构怎样升级？

---

## 2. 先跑一次

[`lab/m11a-chord.ts`](../lab/m11a-chord.ts) 不经过 durable，直接调用这三块。先看 delta 最基本的一次修改：

```bash
node --conditions=source --experimental-strip-types m11a-chord.ts track
```

```
track 后 revision = 0 ；value === initial： true
prepared.ops = [["a",["output"],"done\n"],["p",["entries"],1,0,[{"id":2}]]]
prepared.base === tracker.value： true
没改的子树结构共享 prepared.value.meta === base.meta： true
adopt 前 tracker.value.output = ""
adopt 后 revision = 1 ；tracker.value === prepared.value： true
applyImmutable(base, ops) 得到同样的值： true
写回原值：ops = []
```

这就是 durable 每个文档每次修改时发生的事。D1 §2.1 的 `document.change … delta 1 ops [s]` 里的 `[s]` 就是这里的 `Op` 类型。

再看 `Context` 的取消：

```bash
node --conditions=source --experimental-strip-types m11a-chord.ts context
```

```
BACKGROUND_CONTEXT: [Context BACKGROUND_CONTEXT] abortSignal = undefined
withAbortSignal: [Context BACKGROUND_CONTEXT].WithValue(chord.abortSignal) aborted = false
withoutAbortSignal: [Context BACKGROUND_CONTEXT].WithValue(chord.abortSignal).WithValue(chord.abortSignal) abortSignal = undefined
等待者被取消： AbortError ；此时 work 完成了吗？ false
直接 await work： work done ；work 完成了吗？ true
```

取消让等待者先放弃，被等的工作照常完成。D1 里 "取消 `wait()` 只取消这次等待，不撤回 submission" 就建立在这个语义上。

---

## 3. 数据结构

### 3.1 Context：调用上下文

源码：[`packages/chord/src/context/index.ts:3-16`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/context/index.ts#L3-L16)

```ts
/** Typed identity for one value carried by a {@link Context}. */
export interface ContextKey<T> {
	readonly token: symbol;
	/** Type-only marker that prevents keys with different value types from being interchangeable. */
	readonly valueType?: (value: T) => T;
}

/** Immutable invocation-scoped values passed explicitly through operations. */
export interface Context {
	readonly abortSignal: AbortSignal | undefined;
	readonly telemetry: TelemetrySpan;
	value<T>(key: ContextKey<T>): T | undefined;
	toString(): string;
}
```

| 字段 | 说明 |
|---|---|
| `abortSignal` | 取消信号，没有就是 `undefined`（永不取消） |
| `telemetry` | 当前的 telemetry span。没有设置时是一个什么都不做的 NOOP span |
| `value(key)` | 按 `ContextKey` 取值。`abortSignal` 和 `telemetry` 本身也是用两个私有 key 存的 |
| `toString()` | 调试用，显示派生链 |

`Context` 是不可变的。派生一个新 `Context` 就是包一层 `ContextValue`，查找时沿父链往上找（`packages/chord/src/context/index.ts:56-76`）。

**真实实例**：§2 的输出。`BACKGROUND_CONTEXT` 的 `abortSignal` 是 `undefined`；`withAbortSignal` 派生出 `[Context BACKGROUND_CONTEXT].WithValue(chord.abortSignal)`；再 `withoutAbortSignal` 又包一层，把信号设回 `undefined`。

### 3.2 Op：一个修改操作

源码：[`packages/chord/src/delta/index.ts:14-40`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/index.ts#L14-L40)

```ts
export type Seg = string | number;
export type Path = readonly Seg[];
export type NonEmptyPath = readonly [Seg, ...Seg[]];

/** A path inline, or an id assigned by the encoder on second use. */
export type PathRef<P extends Path = Path> = P | number;

/**
 * Tuples are the form — in memory, on the wire, on disk.
 *
 * `r` is the ONLY op that replaces a whole value. `s`/`d`/`a`/`t` cannot target
 * the root: the type forbids it. `p` and `m` may, because a tracked value can
 * itself be an array. Operation shape is not canonical: an array may be emptied by either
 * a replacement or a root splice.
 *
 * `Op` knows nothing about the path dictionary. Interning, id references and
 * omitted paths live in `WireOp` and exist only between `encode` and `decode`.
 */
export type Op =
	| readonly ["r", JsonValue]
	| readonly ["s", NonEmptyPath, JsonValue]
	| readonly ["d", NonEmptyPath]
	| readonly ["a", NonEmptyPath, string]
	| readonly ["t", NonEmptyPath, number]
	| readonly ["p", Path, number, number, JsonValue[]]
	/** Reorder an array in place: `new[i] = old[permutation[i]]`. */
	| readonly ["m", Path, number[]];
```

| 元组 | 意义 |
|---|---|
| `["r", value]` | 替换整个值 |
| `["s", path, value]` | 设置对象属性或数组元素 |
| `["d", path]` | 删除属性或数组元素 |
| `["a", path, text]` | 在字符串末尾追加 |
| `["t", path, count]` | 从字符串开头删掉 `count` 个 UTF-16 单元 |
| `["p", path, index, remove, items]` | 数组 splice |
| `["m", path, permutation]` | 数组重排 |

`Op` 是普通的 JSON 元组，在内存里、存储里、网络上都是同一个形状。

**真实实例**：§2 里 `[["a",["output"],"done\n"],["p",["entries"],1,0,[{"id":2}]]]`。把 `output` 从 `""` 改成 `"done\n"` 被识别成追加；`entries.push({ id: 2 })` 变成一次 splice。

### 3.3 Tracker、Change、Prepared：修订追踪

源码：[`packages/chord/src/delta/tracker.ts:139-159`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/tracker.ts#L139-L159)

```ts
export interface Prepared<T extends object> {
	readonly base: T;
	readonly value: T;
	readonly ops: readonly Op[];
	readonly baseRevision: number;
	abort(): void;
}

export interface Change<T extends object> {
	readonly state: Draft<T>;
	prepare(): Prepared<T>;
	abort(): void;
}

export interface Tracker<T extends object> {
	readonly value: T;
	readonly revision: number;
	beginChange(): Change<T>;
	prepareReplace(value: T): Prepared<T>;
	adopt(prepared: Prepared<T>): void;
}
```

| 类型 | 字段 | 说明 |
|---|---|---|
| `Tracker` | `value` | 当前的不可变修订 |
| | `revision` | 修订号，每次 `adopt` 加 1 |
| | `beginChange()` | 在当前修订上开一个可写的 draft |
| | `prepareReplace(value)` | 整体替换，得到 `[["r", value]]` 或空批次 |
| | `adopt(prepared)` | 接受一个准备好的修改：只做一次指针替换 |
| `Change` | `state` | 可写的 draft（`Draft<T>`） |
| | `prepare()` | 封住 draft，算出新值和 `Op[]`，但不改 tracker |
| | `abort()` | 放弃 |
| `Prepared` | `base`、`value` | 修改前和修改后的值 |
| | `ops` | 从 `base` 到 `value` 的操作 |
| | `baseRevision` | 基于哪个修订，`adopt` 用它判断是否过期 |

`Draft<T>` 只是去掉 `readonly` 的递归类型：

源码：[`packages/chord/src/delta/draft.ts:1-10`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/draft.ts#L1-L10)

```ts
/** A mutable transaction-scoped view of a JSON value, preserving tuple positions. */
export type Draft<T, Depth extends readonly unknown[] = []> = Depth["length"] extends 8
	? T
	: T extends null | boolean | number | string
		? T
		: T extends (...args: never[]) => unknown
			? T
			: T extends object
				? { -readonly [Key in keyof T]: Draft<T[Key], [...Depth, unknown]> }
				: T;
```

**真实实例**：§2 的输出。`track` 后 `revision = 0`，`tracker.value === initial`（`track` 直接接管传入的对象，不复制）。`prepare` 之后 `tracker.value.output` 仍然是 `""`；`adopt` 之后 `revision = 1`，`tracker.value === prepared.value`。没改的 `meta` 子树和旧值是同一个对象。

### 3.4 PendingOps：积压的操作

源码：[`packages/chord/src/delta/pending.ts:5-12`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/pending.ts#L5-L12)

```ts
/**
 * What `PendingOps.take()` hands over: the pending ops, folded, or `replace` after an overflow, which the owner of the
 * queue turns into a root replacement with the current value.
 */
export type PendingTake = { readonly kind: "ops"; readonly ops: readonly Op[] } | { readonly kind: "replace" };

/** Pending ops past which only "replace with the current value" stays pending. */
export const MAX_PENDING_OPS = 10_000;
```

`PendingOps` 替一个暂时跟不上的消费者保存还没交出去的操作，并在等待期间折叠它们。durable 用它保存 deferred commit 还没写进存储的操作（D3 §4.10）。`take()` 返回折叠后的操作；积压超过 10,000 个操作时只返回 `replace`，拥有者改为发送当前的完整值。

**真实实例**：§7.2。

### 3.5 SqliteExecutor 和 SqliteDatabase

源码：[`packages/chord/src/sqlite/database.ts:1-41`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/sqlite/database.ts#L1-L41)

```ts
/** Values supported by the portable SQLite drivers. */
export type SqliteValue = null | number | bigint | string | Uint8Array;

/**
 * Asynchronous SQL operations shared by a database and its transaction handles.
 *
 * `exec` runs SQL text without bindings and may contain several statements. `run`, `get`, and `all`
 * execute one statement with positional bindings. `runCounted` runs one like `run` and resolves with the rows it
 * inserted, updated or deleted, as SQLite's `changes()` counts them (index writes and rows changed by triggers do not
 * count); it may cost a statement more than `run`, so callers that need no count use `run`. Adapters may cache
 * prepared statements by SQL text, so callers pass values as bindings instead of interpolating them.
 */
export interface SqliteExecutor {
	exec(sql: string): Promise<void>;
	run(sql: string, ...params: SqliteValue[]): Promise<void>;
	runCounted(sql: string, ...params: SqliteValue[]): Promise<number>;
	get<T extends object>(sql: string, ...params: SqliteValue[]): Promise<T | undefined>;
	all<T extends object>(sql: string, ...params: SqliteValue[]): Promise<T[]>;
}

/**
 * Minimal portable SQLite database facade.
 *
 * All operations are asynchronous so adapters may execute outside the caller's runtime.
 *
 * `transaction` passes the callback a transaction handle. All work in the transaction
 * must use that handle; the handle is invalid after the callback settles. Adapters must
 * queue unrelated operations and other transactions until the transaction finishes. The
 * returned promise settles after commit or rollback. Calling the database itself (including
 * `transaction` or `close`) from inside a callback therefore waits for that transaction and
 * never settles.
 *
 * When the callback rejects, the adapter must roll the transaction back before rejecting
 * with that same error. If rollback fails, it must reject with a different error (for
 * example an `AggregateError`) so callers cannot mistake the callback error for a
 * guaranteed rollback.
 */
export interface SqliteDatabase extends SqliteExecutor {
	transaction<T>(callback: (transaction: SqliteExecutor) => Promise<T>): Promise<T>;
	close(): Promise<void>;
}
```

| 方法 | 说明 |
|---|---|
| `exec(sql)` | 执行不带参数的 SQL，可以有多条语句 |
| `run` / `runCounted` | 执行一条带位置参数的语句；后者返回改动行数 |
| `get` / `all` | 查询一行 / 所有行 |
| `transaction(callback)` | 在一个事务里执行回调。回调抛错时先回滚，再用同一个错误拒绝 |
| `close()` | 关闭 |

接口全部是异步的，这样 Cloudflare Durable Object 这类在别处执行 SQL 的后端也能实现它。

**真实实例**：§7.3。

### 3.6 类型关系图

```
 Context ── abortSignal、telemetry ──▶ 每个 durable 调用
                                      │
 Tracker<T>（每个已加载的文档一个）     │ tx.doc()
   value: 修订 r0 ──beginChange()──▶ Change.state: Draft<T> ◀── commit 回调写它
                                      │ prepare()
                                      ▼
                         Prepared { base: r0, value: r1, ops: Op[] }
                           │                       │
            存储成功 adopt()│                       │ ops 写进存储（delta）
                           ▼                       ▼    或 value 写进存储（base）
                    Tracker.value = r1      Storage.commit()
                                                   │
                     deferred commit 的 ops ──▶ PendingOps（折叠，等下一次写入）

 SqliteDatabase ── transaction() ──▶ SqliteExecutor ──▶ SqliteStorage（durable）
```

---

## 4. 从入口读起

D2 没有单一入口。按 durable 一次 commit 用到它们的顺序读：

```
BACKGROUND_CONTEXT / withAbortSignal / withoutAbortSignal   §4.1
awaitWithContext                                            §4.2
track() → beginChange()                                     §4.3
写 draft 的规则                                              §4.4
Change.prepare()                                            §4.5
Tracker.adopt()                                             §4.6
appendText()                                                §4.7
applyImmutableBatches()                                     §4.8
PendingOps.push() / take()                                  §4.9
openNodeSqliteDatabase() / transaction()                    §4.10
applySqliteMigrations() → durable 的 SqliteStorage.open()   §4.11
```

### 4.1 BACKGROUND_CONTEXT、withAbortSignal、withoutAbortSignal

**调用位置**：宿主创建 `BACKGROUND_CONTEXT` 传给 `Harness.open()`；durable 内部在需要"必须完成"的清理和存储写入时用 `withoutAbortSignal`，例如 `Harness.open()` 失败后的关闭（`packages/durable/src/harness/harness.ts:606`）和写入器调用 `Storage.commit()`（`packages/durable/src/session/writer.ts:178`）。

源码：[`packages/chord/src/context/index.ts:78-120`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/context/index.ts#L78-L120)

```ts
export const BACKGROUND_CONTEXT: Context = new EmptyContext("[Context BACKGROUND_CONTEXT]");
export const TODO_CONTEXT: Context = new EmptyContext("[Context TODO_CONTEXT]");

export function createContextKey<T>(description: string): ContextKey<T> {
	return Object.freeze({ token: Symbol(description) });
}

/** Derive a context containing one additional or replaced value. */
export function withContextValue<T>(key: ContextKey<T>, value: T, parent: Context): Context {
	return new ContextValue(parent, key, value);
}

/** Derive an explicit telemetry parent without changing cancellation or application values. */
export function withTelemetry<T extends TelemetrySpan>(
	telemetry: T,
	context: Context,
): Context & { readonly telemetry: T } {
	return withContextValue(TELEMETRY_CONTEXT_KEY, telemetry, context) as Context & { readonly telemetry: T };
}

/** Run a child span with a derived context whose telemetry member is that span. */
export function traceIn<T>(
	options: SpanOptions,
	fn: (context: Context) => T | Promise<T>,
	context: Context,
): Promise<T> {
	return context.telemetry.startSpan(options, (span) => fn(withTelemetry(span, context)));
}

/**
 * Derive a context cancelled by either the parent signal or the supplied signal.
 * The parent context remains unchanged.
 */
export function withAbortSignal(signal: AbortSignal, context: Context): Context {
	const parentSignal = context.abortSignal;
	const combined = parentSignal === undefined ? signal : AbortSignal.any([parentSignal, signal]);
	return withContextValue(ABORT_SIGNAL_CONTEXT_KEY, combined, context);
}

/** Derive a context retaining all values except caller cancellation. Intended for mandatory cleanup only. */
export function withoutAbortSignal(context: Context): Context {
	return withContextValue(ABORT_SIGNAL_CONTEXT_KEY, undefined, context);
}
```

**讲解**

`BACKGROUND_CONTEXT` 是一个没有任何值的 `EmptyContext`，所以没有取消信号，telemetry 是 NOOP。

`withAbortSignal(signal, context)` 把新信号和父信号用 `AbortSignal.any` 合并：任何一个取消，派生的 context 都取消。durable 给任务里的对话句柄用它（`packages/durable/src/harness/harness.ts:544`），任务的调用一旦结束，句柄上的操作就被取消。

`withoutAbortSignal(context)` 保留其他值（主要是 telemetry span），只去掉取消信号。注释写明它只用于必须完成的清理。durable 的写入器在调用 `Storage.commit()` 时用它：一个 commit 一旦被接纳，调用者取消也不会中断存储（spec §4 "Once a commit is admitted, caller cancellation does not interrupt storage settlement or undo the commit"）。

**往哪走**：§4.2。

### 4.2 awaitWithContext()

**调用位置**：durable 等待一个不归调用者所有的 Promise 时，例如 `Session.close()` 的等待（`packages/durable/src/session/session.ts:829`）。

源码：[`packages/chord/src/context/index.ts:134-161`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/context/index.ts#L134-L161)

```ts
/**
 * Observe a promise until it settles or the invocation is cancelled.
 * Cancellation rejects only this waiter; it does not cancel the underlying promise.
 */
export function awaitWithContext<T>(promise: Promise<T>, context: Context): Promise<T> {
	const signal = context.abortSignal;
	if (signal === undefined) return promise;
	if (signal.aborted) return Promise.reject(abortError(signal));
	return new Promise<T>((resolve, reject) => {
		const onAbort = (): void => reject(abortError(signal));
		signal.addEventListener("abort", onAbort, { once: true });
		void promise.then(
			(value) => {
				signal.removeEventListener("abort", onAbort);
				resolve(value);
			},
			(error: unknown) => {
				signal.removeEventListener("abort", onAbort);
				reject(error);
			},
		);
	});
}

function abortError(signal: AbortSignal): Error {
	const reason: unknown = signal.reason;
	return reason instanceof Error ? reason : new DOMException("The operation was aborted", "AbortError");
}
```

**讲解**

`awaitWithContext` 返回一个新的 Promise：底层 Promise 先完成就跟着完成；信号先取消就用 `AbortError` 拒绝。底层 Promise 不受影响。§2 的输出里，等待者在 10ms 时被取消，此时 `work` 还没完成；随后直接 `await work` 仍然得到 `work done`。

durable 的规则"取消一次等待只取消这个等待者，不取消工作"（README Quick Start：Cancelling a wait only cancels that wait, never the work）就是这个函数的语义。关闭 Harness 时，取消 `close()` 只是不再等，关闭本身继续（spec §2.2）。

**往哪走**：§4.3 delta。

### 4.3 track() 和 beginChange()

**调用位置**：`Transaction.#acquire()` 新建文档时调用 `track(value)`，已加载的文档调用 `loaded.tracker.beginChange()`（`packages/durable/src/session/transaction.ts:689`、`packages/durable/src/session/transaction.ts:705`，D3 §4.5）。

源码：[`packages/chord/src/delta/tracker.ts:242-269`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/tracker.ts#L242-L269)

```ts
class TrackerImpl<T extends object> implements Tracker<T> {
	readonly #owner = {};
	#open: OverlayContext | undefined;
	#competitors: Set<WeakRef<OverlayContext>> | undefined;
	readonly #selfRef: WeakRef<TrackerImpl<object>>;
	#value: T;
	#revision = 0;
	#pruneBudget = 256;

	constructor(initial: T) {
		this.#value = initial;
		this.#selfRef = new WeakRef(this as unknown as TrackerImpl<object>);
	}

	get value(): T {
		return this.#value;
	}

	get revision(): number {
		return this.#revision;
	}

	beginChange(): Change<T> {
		const context = createContext(this.#selfRef, this.#owner, this.#revision, this.#value, false, this.#value);
		if (this.#open !== undefined) this.#demote(this.#open);
		this.#open = context;
		return new ChangeImpl(context);
	}
```

源码：[`packages/chord/src/delta/tracker.ts:358-361`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/tracker.ts#L358-L361)

```ts
/** Take immutable ownership of an alias-free strict-JSON root in O(1). */
export function track<T extends object>(initial: T): Tracker<T> {
	return new TrackerImpl(initial);
}
```

**讲解**

`track(initial)` 直接把 `initial` 当作第 0 个修订，O(1)，不复制也不遍历。代价是调用方不能再改 `initial`，也不能让它里面有共享的子对象（delta README "Roots and placements"）。durable 遵守这一点：传给 `track` 的值要么来自 `copyJson()`（新建文档，`packages/durable/src/session/transaction.ts:701-705`），要么来自存储的反序列化结果（加载文档）。

`beginChange()` 在当前修订上开一个 overlay：draft 的读穿透到旧值，写只记录在 overlay 里。旧修订本身不会被改。同一个 tracker 可以同时开多个 change，`#open` 只强引用最新的一个，旧的移进 `#competitors` 用 `WeakRef` 持有。

**往哪走**：§4.4。

### 4.4 写 draft 的规则

**调用位置**：commit 回调里的赋值，例如 `(await tx.doc(Notes, id)).text = "..."`。

overlay 是一个 Proxy。每次读写先检查 overlay 的状态：

源码：[`packages/chord/src/delta/tracker.ts:578-594`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/tracker.ts#L578-L594)

```ts
function isSettledContext(context: OverlayContext): boolean {
	return (
		context.overlayReleased ||
		context.status.value === "consumed" ||
		context.status.value === "aborted" ||
		context.status.value === "stale"
	);
}

function assertReadable(context: OverlayContext): void {
	if (isSettledContext(context)) throw new TypeError("Cannot use a settled overlay");
}

function assertWritable(context: OverlayContext): void {
	assertReadable(context);
	if (context.status.value !== "open") throw new TypeError("Prepared overlays are read-only");
}
```

放进 draft 的值会被复制并校验成严格 JSON。用 `m11a-chord.ts rules` 看几条规则：

```bash
node --conditions=source --experimental-strip-types m11a-chord.ts rules
```

```
放入函数： TypeError Value contains a non-JSON function; expected strict JSON
放入 NaN： TypeError Value contains a non-finite number and is not strict JSON
prepare 之后写 draft： TypeError Cannot use a settled overlay
adopt 竞争者： Error Prepared change is stale
text += "llo" 的 ops： [["a",["text"],"llo"]]
appendText("llo") 的 ops： [["a",["text"],"llo"]]
```

**讲解**

- 函数和 `NaN` 在赋值那一刻就抛 `TypeError`，draft 不变。所以文档里永远只有严格 JSON。
- `prepare()` 之后 overlay 的状态不再是 `open`，再用这个 draft 就抛 `Cannot use a settled overlay`。durable 在 commit 回调结束时对每个打开的 change 调用 `prepare()` 或 `abort()`（D3 §4.6），所以 draft 不可能逃出回调（spec §1 不变式 6）。D3 §7.2 在 durable 里验证这一点。
- 两个 change 从同一个修订开始，先 `adopt` 的那个让另一个过期。durable 的提交线保证每个 tracker 同时最多有一个打开的 change（spec §3.4），所以正常运行不会走到这里；这条检查防的是用错。
- `text += "llo"` 和 `appendText(state, "text", "llo")` 产生同一个 `["a",["text"],"llo"]`，区别在代价（§4.7）。

**往哪走**：§4.5。

### 4.5 Change.prepare()

**调用位置**：`Transaction.settleSuccess()`：`document.prepared = document.change.prepare()`（`packages/durable/src/session/transaction.ts:780`）。

源码：[`packages/chord/src/delta/tracker.ts:199-230`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/tracker.ts#L199-L230)

```ts
	prepare(): Prepared<T> {
		if (this.#settled) throw new Error("Change has already been settled");
		const context = this.#context!;
		assertWritable(context);
		context.status.value = "prepared";
		try {
			if (!context.replacement) context.ops = context.dirty.length === 0 ? [] : emitOperations(context);
			const prepared = materializePrepared<T>(context);
			this.#preparedStatus = context.status;
			this.#settled = true;
			return prepared;
		} catch (error) {
			context.status.value = "aborted";
			clearContext(context);
			this.#settled = true;
			throw error;
		} finally {
			this.#context = undefined;
		}
	}

	abort(): void {
		if (this.#settled) {
			if (this.#preparedStatus?.value === "prepared") this.#preparedStatus.value = "aborted";
			this.#preparedStatus = undefined;
			return;
		}
		this.#settled = true;
		abortContext(this.#context!);
		this.#context = undefined;
	}
}
```

**讲解**

`prepare()` 先把状态设成 `prepared`，这一步让所有 draft 句柄失效。然后：

- 没有任何写入（`dirty.length === 0`）时 `ops` 是空数组，`value` 就是 `base`。
- 否则 `emitOperations` 根据 overlay 生成操作，`materializePrepared` 算出新的不可变值。没改的子树和 `base` 共享。

失败时状态变成 `aborted`。`prepare()` 不修改 tracker：`tracker.value` 仍然是旧修订。§2 的输出里，`prepare` 之后 `tracker.value.output` 还是 `""`。

这一步发生在存储之前。durable 拿到 `prepared.ops` 和 `prepared.value` 以后，决定往存储里写 delta 还是 base（D3 §4.7），然后才调用 `Storage.commit()`。

**往哪走**：§4.6。

### 4.6 Tracker.adopt()

**调用位置**：commit 存储成功、发布时，`Transaction.adopt(seq)` 对每个改动的文档调用 `tracker.adopt(prepared)`（`packages/durable/src/session/transaction.ts:905`，D3 §4.12）。存储失败时改为 `prepared.abort()`。

源码：[`packages/chord/src/delta/tracker.ts:284-307`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/tracker.ts#L284-L307)

```ts
	adopt(prepared: Prepared<T>): void {
		const context = PREPARED.get(prepared as object);
		if (context?.owner !== this.#owner) throw new Error("Prepared change belongs to a different tracker");
		if (context.status.value === "consumed") throw new Error("Prepared change has already been used");
		if (context.status.value === "aborted") throw new Error("Prepared change has been aborted");
		if (context.status.value === "stale") throw new Error("Prepared change is stale");
		if (context.status.value !== "prepared") throw new Error("Prepared change is not ready");
		if (context.baseRevision !== this.#revision) {
			context.status.value = "stale";
			clearContext(context);
			throw new Error("Prepared change is stale");
		}

		if (this.#value !== prepared.base) {
			context.status.value = "stale";
			throw new Error("Prepared change is stale");
		}
		// Materialization happened during prepare. Adoption is an infallible pointer
		// swap so storage failure can discard the candidate without touching authority.
		this.#value = prepared.value;
		context.status.value = "consumed";
		this.#revision += 1;
		this.#invalidate(context);
	}
```

**讲解**

`adopt` 先做一串检查：是不是这个 tracker 的、有没有用过、有没有被放弃、基于的修订是不是当前修订。全部通过后只做三件事：替换 `#value` 指针、修订号加 1、让其他竞争者过期。

注释说明了为什么要分成 `prepare` 和 `adopt` 两步：所有可能失败的计算都在 `prepare` 里做完，`adopt` 是不会失败的指针替换，所以存储失败时可以直接丢掉候选值，不碰当前修订。对 durable 来说，这就是"内存只在存储成功后改变"（spec §3.4 "`adopt()` … performs only a synchronous pointer swap"）。

**往哪走**：§4.7。

### 4.7 appendText()

**调用位置**：durable 把流式回答的片段写进 `pi.live` 时（`packages/durable/src/harness/partial-changes.ts:174`，D6）。

源码：[`packages/chord/src/delta/tracker.ts:363-388`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/tracker.ts#L363-L388)

```ts
/**
 * Append `text` to the string at `draft[key]`, costing O(text.length). The change is recorded as one append op
 * (several appends to the same key in one change join into one op) without comparing the old and new strings. A plain
 * assignment such as `draft[key] += text` gives the same op, but `prepare()` then has to check that the new string
 * extends the old one, which reads the whole string: O(length) per change. A plain assignment or deletion of the key in
 * the same change falls back to that check.
 */
export function appendText<T extends object>(draft: Draft<T>, key: StringKeys<T>, text: string): void {
	// Trust boundary: JavaScript callers can pass any object and key; the types cover TypeScript callers.
	const node = (draft as Record<PropertyKey, unknown>)[NODE] as OverlayNode | undefined;
	if (node === undefined) throw new TypeError("appendText() needs a draft");
	assertWritable(node.context);
	const name = String(key);
	const current = Array.isArray(nodeBase(node)) || !objectHas(node, name) ? undefined : objectValue(node, name);
	if (typeof current !== "string") throw new TypeError(`appendText() needs a string at ${JSON.stringify(name)}`);
	if (text.length === 0) return;
	// The op and the new value keep the text; a copy, so they never keep a larger string the caller cut it from alive.
	const owned = ownedSlice(text, 0);
	const appended = node.appends?.get(name) ?? (hasObjectWrite(node, name) ? undefined : "");
	setObjectWrite(node, name, current + owned);
	markDirty(node);
	if (appended !== undefined) {
		node.appends ??= new Map();
		node.appends.set(name, appended + owned);
	}
}
```

**讲解**

`draft.text += chunk` 也能得到一个 `a` 操作，但 `prepare()` 看到的只是拼好的新字符串，要逐字符比较才能确认它以旧字符串开头，代价是字符串的全长。流式回答有 N 个片段，总代价是 O(N × 长度)。delta README 给了一个测量：2 万次追加、最终 800KB，用 `+=` 约 2.4 秒，用 `appendText` 约 1 毫秒（"Growing strings: use `appendText`" 一节）。

`appendText` 直接把片段记进 `node.appends`，`prepare()` 时不再比较。同一个 change 里对同一个 key 的多次追加合并成一个操作。

**往哪走**：§4.8。

### 4.8 applyImmutableBatches()

**调用位置**：`MemoryStorage.materializeDocument()` 从最近的 base 开始重放后面的 delta（`packages/durable/src/storage/memory.ts:699`，D3 §4.11）。

源码：[`packages/chord/src/delta/index.ts:422-455`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/index.ts#L422-L455)

```ts
/** Apply one decoded operation batch without mutating the previous immutable value. */
export function applyImmutable<T>(target: T | undefined, ops: readonly Op[]): T {
	return applyImmutableBatches(target, [ops]);
}

/**
 * Apply decoded operation batches as one final-result-only replay.
 *
 * Containers copied for an earlier batch may be mutated while applying a later
 * batch, so this deliberately exposes no intermediate revisions.
 */
export function applyImmutableBatches<T>(target: T | undefined, batches: Iterable<readonly Op[]>): T {
	let root = target as unknown as JsonValue;
	const owned = new WeakSet<object>();
	for (const ops of batches) {
		for (const op of ops) {
			assertValidOp(op);
			if (op[0] === "r") {
				root = op[1];
				continue;
			}
			if (op[0] === "p") {
				const appended = appendCopy(root, op, owned);
				if (appended !== undefined) {
					root = appended;
					continue;
				}
			}
			root = copyContainers(root, op[0] === "p" || op[0] === "m" ? op[1] : op[1].slice(0, -1), owned);
			root = applyOps(root, [op]);
		}
	}
	return root as unknown as T;
}
```

**讲解**

`applyImmutable` 只是 `applyImmutableBatches` 的单批版本。重放时只复制路径上要改的容器（`copyContainers`），其余部分和输入共享；`owned` 记住本次重放已经复制过的容器，同一个容器不会复制两次。`r` 操作直接换掉根。

结果和输入共享容器，所以输入和结果都不能被修改。MemoryStorage 存的 base 是冻结的（D3 §4.11），这条规则在那里自然满足。

**往哪走**：§4.9。

### 4.9 PendingOps.push() 和 take()

**调用位置**：`SessionImpl.#hold()` 把 deferred commit 的操作放进每个文档的 `PendingOps`，下一次存储时 `take()` 出来（`packages/durable/src/session/session.ts:1229`，D3 §4.10）。

源码：[`packages/chord/src/delta/pending.ts:51-105`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/pending.ts#L51-L105)

```ts
export class PendingOps {
	#state: State = EMPTY;

	/** Queue the ops of one commit. */
	push(ops: readonly Op[]): void {
		if (ops.length === 0) return;
		const state = this.#state;
		// A batch that starts with `r` (the tracker's and `diffRevisions()`'s only place for it) drops everything
		// pending, also an overflow, and is kept as received.
		if (ops[0]![0] === "r") {
			this.#state = { kind: "batch", ops };
			return;
		}
		switch (state.kind) {
			case "empty":
				this.#state = { kind: "batch", ops };
				return;
			case "batch": {
				const folding: Folding = { kind: "folding", ops: [], leaves: new Map() };
				this.#state = folding;
				if (this.#fold(state.ops, folding)) this.#fold(ops, folding);
				return;
			}
			case "folding":
				this.#fold(ops, state);
				return;
			case "replace":
				return;
		}
	}

	/**
	 * Pending ops after folding; 1 once overflowed (the replacement the owner sends). One batch is counted as received,
	 * so a single commit larger than `MAX_PENDING_OPS` counts in full until a second one arrives and overflows.
	 */
	get size(): number {
		const state = this.#state;
		if (state.kind === "empty") return 0;
		return state.kind === "replace" ? 1 : state.ops.length;
	}

	/** Hand over everything pending and start empty. */
	take(): PendingTake {
		const state = this.#state;
		this.#state = EMPTY;
		switch (state.kind) {
			case "empty":
				return NO_OPS;
			case "batch":
			case "folding":
				return { kind: "ops", ops: state.ops };
			case "replace":
				return REPLACE_TAKE;
		}
	}
```

折叠规则在 `#fold` 和 `merge` 里：

源码：[`packages/chord/src/delta/pending.ts:166-173`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/delta/pending.ts#L166-L173)

```ts
/** `next` folded into `previous` (same path), or undefined when they do not fold. */
function merge(previous: LeafOp, next: LeafOp): LeafOp | undefined {
	if (next[0] === "s") return next;
	if (previous[0] === "a") return ["a", previous[1], previous[2] + next[2]];
	// Only a string set takes an append; the tracker never appends to another primitive.
	if (typeof previous[2] === "string") return ["s", previous[1], previous[2] + next[2]];
	return undefined;
}
```

**讲解**

`PendingOps` 有四种状态：`empty`、`batch`（只有一批，原样保留引用）、`folding`（多批，已折叠）、`replace`（溢出）。

- 以 `r` 开头的批次丢掉之前的一切。
- 叶子操作（追加 `a`、设置原始值的 `s`）合并进同一路径上最后一个叶子操作：`a` + `a` 拼接，`s` 覆盖之前的 `s` 或 `a`，字符串 `s` 之后的 `a` 拼进 `s`。
- 其他操作（结构变化）清空叶子索引，之后的操作不再和它前面的合并，因为路径的含义可能变了。
- 超过 `MAX_PENDING_OPS` 进入 `replace`，只能发送当前完整值。

对 durable 来说，一个流式回答的 100 次 deferred commit 最后存成一个 `a` 操作。

**往哪走**：§4.10 sqlite。

### 4.10 openNodeSqliteDatabase() 和 transaction()

**调用位置**：`openNodeSqliteStorage(path)` → `openNodeSqliteDatabase(path, options)`（`packages/durable/src/storage/sqlite/node.ts:21`）。

源码：[`packages/chord/src/sqlite/node.ts:196-219`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/sqlite/node.ts#L196-L219)

```ts
/** Open and configure a Node-backed SQLite database facade. */
export async function openNodeSqliteDatabase(
	path: string,
	options: NodeSqliteDatabaseOptions = {},
): Promise<NodeSqliteDatabase> {
	const checkpointPages = options.walAutoCheckpointPages ?? DEFAULT_WAL_AUTO_CHECKPOINT_PAGES;
	const timeout = options.busyTimeoutMs ?? DEFAULT_BUSY_TIMEOUT_MS;
	if (path !== ":memory:") await mkdir(dirname(path), { recursive: true });
	const database = new DatabaseSync(path, { timeout });
	const adapter = new NodeSqliteDatabase(database);
	try {
		await adapter.exec("PRAGMA journal_mode = WAL");
		await adapter.exec("PRAGMA synchronous = NORMAL");
		await adapter.exec(`PRAGMA wal_autocheckpoint = ${checkpointPages}`);
		return adapter;
	} catch (error) {
		try {
			await adapter.close();
		} catch {
			// Preserve the configuration failure.
		}
		throw error;
	}
}
```

源码：[`packages/chord/src/sqlite/node.ts:157-176`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/sqlite/node.ts#L157-L176)

```ts
	transaction<T>(callback: (transaction: SqliteExecutor) => Promise<T>): Promise<T> {
		return this.access.runAsync(async () => {
			this.database.exec("BEGIN IMMEDIATE");
			const scope = { active: true };
			try {
				const result = await callback(new NodeSqliteTransaction(this.database, this.statements, scope));
				scope.active = false;
				this.database.exec("COMMIT");
				return result;
			} catch (error) {
				scope.active = false;
				try {
					this.database.exec("ROLLBACK");
				} catch (rollbackError) {
					throw new AggregateError([error, rollbackError], "SQLite transaction failed and rollback failed");
				}
				throw error;
			}
		});
	}
```

**讲解**

打开时设置三个 PRAGMA：WAL 日志模式、`synchronous = NORMAL`、自动检查点页数。`busyTimeoutMs` 默认 5 秒，用于等待别的进程持有的文件锁。

`node:sqlite` 的 `DatabaseSync` 是同步 API。`NodeSqliteDatabase` 用一个串行队列（`SerialOperationQueue`，`packages/chord/src/sqlite/node.ts:22-69`）把它包成异步接口：`transaction` 用 `runAsync` 占住队列直到回调结束，事务期间别的操作排队。回调成功就 `COMMIT`，抛错就 `ROLLBACK` 再抛出原来的错误；回滚本身也失败时抛 `AggregateError`，这样调用方不会误以为已经回滚。

事务句柄 `NodeSqliteTransaction` 在回调结束后失效（`scope.active = false`），之后再用它就报错。这和 delta 的 draft 规则是同一个思路。

**往哪走**：§4.11。

### 4.11 applySqliteMigrations() 和 durable 的 SqliteStorage.open()

**调用位置**：`SqliteStorage.open()` → `applySqliteMigrationsForSchema()` → chord 的 `applySqliteMigrations()`。

源码：[`packages/chord/src/sqlite/migrations.ts:11-43`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/chord/src/sqlite/migrations.ts#L11-L43)

```ts
/** Apply all pending migrations atomically, tracking their version in the named metadata table. */
export async function applySqliteMigrations(
	database: SqliteDatabase,
	migrations: readonly SqliteMigration[],
	options: { readonly table: string },
): Promise<void> {
	const { table } = options;
	validateSqliteSchemaPrefix(table);
	for (let index = 0; index < migrations.length; index++) {
		if (migrations[index]?.version !== index + 1) {
			throw new Error("SQLite migrations must have contiguous versions starting at 1");
		}
	}

	await database.transaction(async (transaction) => {
		await transaction.exec(`CREATE TABLE IF NOT EXISTS ${table} (
			singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
			version INTEGER NOT NULL CHECK (version >= 0)
		) STRICT`);
		await transaction.run(`INSERT OR IGNORE INTO ${table} (singleton, version) VALUES (1, 0)`);
		const row = await transaction.get<SchemaRow>(`SELECT version FROM ${table} WHERE singleton = 1`);
		if (row === undefined) throw new Error("SQLite schema metadata is missing");
		const currentVersion = migrations.at(-1)?.version ?? 0;
		if (row.version > currentVersion) {
			throw new Error(`SQLite schema version ${row.version} is newer than supported version ${currentVersion}`);
		}
		for (const migration of migrations) {
			if (migration.version <= row.version) continue;
			for (const statement of migration.statements) await transaction.exec(statement);
			await transaction.run(`UPDATE ${table} SET version = ? WHERE singleton = 1`, migration.version);
		}
	});
}
```

源码：[`packages/durable/src/storage/sqlite/node.ts:14-22`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/storage/sqlite/node.ts#L14-L22)

```ts
/** Open or create file-backed durable storage using Node's built-in SQLite, optionally under a table prefix. */
export async function openNodeSqliteStorage(
	path: string,
	options: NodeSqliteStorageOptions = {},
): Promise<SqliteStorage> {
	const { prefix, ...databaseOptions } = options;
	if (prefix !== undefined) validateSqliteSchemaPrefix(prefix);
	return SqliteStorage.open(await openNodeSqliteDatabase(path, databaseOptions), { prefix });
}
```

源码：[`packages/durable/src/storage/sqlite/storage.ts:193-211`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/durable/src/storage/sqlite/storage.ts#L193-L211)

```ts
	/** Initialize storage over an owned SQLite database facade. */
	static async open(db: SqliteDatabase, options: SqliteStorageOptions = {}): Promise<SqliteStorage> {
		try {
			const schema = createSqliteSchema(options.prefix);
			await applySqliteMigrationsForSchema(db, schema);
			const metadata = await db.get<MetadataRow>(
				`SELECT next_id, next_seq FROM ${schema.tables.durableMetadata} WHERE singleton = 1`,
			);
			if (metadata === undefined) throw new Error("Durable SQLite metadata is missing");
			return new SqliteStorage(db, schema, Number(metadata.next_id));
		} catch (error) {
			try {
				await db.close();
			} catch {
				// Preserve the initialization failure.
			}
			throw error;
		}
	}
```

**讲解**

chord 的迁移工具很小：要求迁移版本从 1 连续编号；在一个事务里建版本表（单行，`singleton = 1`），读出当前版本，执行所有更新的迁移，更新版本号。数据库版本比代码新时报错，不会降级。

durable 给它两个迁移（`packages/durable/src/storage/sqlite/migrations.ts:113-116`）：版本 1 建九张表，版本 2 重建 `tasks` 表以允许 `sleeping` 状态（SQLite 不能原地改 CHECK 约束）。`SqliteStorage.open()` 跑完迁移后读 `durable_metadata` 里的 `next_id`，作为 ID 分配的起点。

`prefix` 选项让多个 durable 存储共用一个数据库文件，表名加前缀。`validateSqliteSchemaPrefix` 拒绝不是标识符的前缀和 `sqlite_` 开头的前缀。

**往哪走**：durable 怎样往这些表里写 → D3 §4.11。

---

## 5. 状态机

### 5.1 一个 Change 的 overlay 状态

```
              beginChange()  tracker.ts:265（createContext，状态 open，tracker.ts:428）
                    │
                    ▼
               ┌────────┐  写 draft：assertWritable 要求 open (tracker.ts:593)
               │  open  │
               └───┬────┘
     prepare()     │      abort() tracker.ts:227 → abortContext tracker.ts:2258
     tracker.ts:203│      ────────────────────────────────────────▶ aborted
                   ▼
              ┌──────────┐  Prepared.abort() / Change.abort() tracker.ts:222
              │ prepared │ ──────────────────────────────────────▶ aborted
              └───┬──────┘
   adopt() 通过检查 │      另一个 change 先被 adopt：staleCompetitor tracker.ts:350
   tracker.ts:304  │      baseRevision 不等于当前修订 tracker.ts:292、298
                   ▼      ────────────────────────────────────────▶ stale
              ┌──────────┐
              │ consumed │
              └──────────┘
   consumed / aborted / stale：任何读写抛 "Cannot use a settled overlay"（tracker.ts:588）
   prepare 失败：aborted（tracker.ts:211）
```

### 5.2 PendingOps 的状态

```
  empty ──push(ops)──▶ batch ──push(ops)──▶ folding ──超过 10,000──▶ replace
   ▲     pending.ts:66  │      pending.ts:69-73 │       pending.ts:127-129 │
   │                    │                       │                         │
   │                    └── push 以 r 开头的批次：回到 batch（pending.ts:60-63）◀┘
   └──────── take() pending.ts:93-105（返回 ops 或 replace，回到 empty）
```

---

## 6. 设计要点

### 6.1 取消只属于等待者

**做法。** `Context` 只带取消信号，不带"可以取消的工作"。`awaitWithContext` 让等待者放弃，工作继续；必须完成的清理和存储写入用 `withoutAbortSignal`。

**理由。** durable 的工作是持久的：submission 已经存储，任务会在下一个进程里继续。调用者取消等待，不应该让这些工作处于一半的状态。真正中止工作要用显式的 `abort()`，它本身也是一次 commit。

**代码位置。** `packages/chord/src/context/index.ts:118`，`packages/chord/src/context/index.ts:138`。

### 6.2 准备和采纳分开

**做法。** `prepare()` 做完所有可能失败的计算，`adopt()` 只换指针。

**理由。** durable 在 `prepare` 和 `adopt` 之间调用存储。存储失败时丢弃候选值，内存里的文档保持旧修订，和存储一致。存储成功时 `adopt` 不会失败，内存一定能跟上存储。

**代码位置。** `packages/chord/src/delta/tracker.ts:199`、`packages/chord/src/delta/tracker.ts:284`。

### 6.3 不可变靠约定，不靠冻结

**做法。** tracker 的修订、prepared 的值和操作都不冻结，也不防御性复制。`track()` 和 `prepareReplace()` O(1) 接管根对象。只有放进 draft 的值会被复制和校验。

**理由。** 文档可能很大（`pi.live` 里有工具输出），每次修改都深复制或冻结代价太高。约定由 durable 自己遵守：传给 tracker 的根都来自 `copyJson()` 或存储的反序列化（spec §3.4）。代价是违规不会被发现，会静默地破坏状态（delta README 开头的警告）。

**代码位置。** `packages/chord/src/delta/tracker.ts:358-361`，`packages/durable/src/session/transaction.ts:701-705`。

### 6.4 操作就是存储格式

**做法。** `Op` 是 JSON 元组，durable 把 `prepared.ops` 原样写进存储作为 delta，重放时用 `applyImmutableBatches`。

**理由。** 不需要第二套序列化。同一批操作可以同时用于存储、发给观察者（D6）、在 `PendingOps` 里折叠。

**代码位置。** `packages/chord/src/delta/index.ts:32`，`packages/durable/src/types.ts:884-894`（`DocumentContent`）。

---

## 7. 实验

### 7.1 给 draft 赋 undefined

**问题。** 对 draft 的对象属性赋 `undefined`，再往一个数组 `push(undefined)`，分别会怎样？`prepare()` 得到什么操作？

**预测。** 按 delta README 的 `undefined` 规则：给对象属性赋 `undefined` 等于删除这个属性，不抛错，`prepare()` 得到一个 `d` 操作；数组里放 `undefined` 抛 `TypeError`，数组不变。这和 `JSON.stringify` 静默丢掉 `undefined` 的做法不同。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-chord.ts undefined
```

```
给属性赋 undefined 之后 'n' in draft： false
数组 push(undefined)： TypeError Value contains a non-JSON undefined; expected strict JSON
ops： [["d",["n"]]] value： {"text":"he","list":[1,2]}
```

**结论。** 预测成立。durable 的宿主代码可以用 `draft.field = undefined` 删除字段，D1 里 `configure()` 用 `delete state[key]` 做同样的事（`packages/durable/src/harness/agent.ts:125`）。

### 7.2 PendingOps 折叠

**问题。** 依次 push `[a text "he"]`、`[s n 1]`、`[a text "y"]`、`[s n 2]`，每次之后 `size` 是多少？`take()` 得到什么？之后 push 一个 `s` 再 push 一个 `r`，`take()` 得到什么？

**预测。** 第 1 批之后是 `batch`，size 1。第 2 批进入 `folding`，两条叶子操作在不同路径，size 2。第 3 批 `a text "y"` 合并进 `a text "he"`，size 仍为 2。第 4 批 `s n 2` 覆盖 `s n 1`，size 2。`take()` 得到 `[["a",["text"],"hey"],["s",["n"],2]]`。`r` 丢掉之前的 `s`，`take()` 只剩 `r`。

**运行。**

```bash
node --conditions=source --experimental-strip-types m11a-chord.ts pending
```

```
push [["a",["text"],"he"]] → size 1
push [["s",["n"],1]] → size 2
push [["a",["text"],"y"]] → size 2
push [["s",["n"],2]] → size 2
take(): {"kind":"ops","ops":[["a",["text"],"hey"],["s",["n"],2]]}
take 之后 size 0
r 之后 take(): {"kind":"ops","ops":[["r",{"text":"new","n":0}]]}
```

**结论。** 预测成立。

### 7.3 SQLite：事务回滚和 durable 的表

**问题。** 用 chord 的接口建一张表，迁移跑两次，版本号是多少？事务回调抛错后插入的行还在吗？再用 durable 的 `openNodeSqliteStorage` 打开一个新文件，会建哪些表？

**预测。** 迁移第二次跳过，版本仍为 1。回滚后行数为 0。durable 建 9 张表（`TABLE_NAMES`，`packages/durable/src/storage/sqlite/schema.ts:9-19`），`durable_schema` 版本为 2，`durable_metadata` 的 `next_id` 是 `"2"`（ID 1 留给根对话）、`next_seq` 是 1。

**运行。** 脚本每次从空文件开始。

```bash
node --conditions=source --experimental-strip-types m11a-chord.ts sqlite /tmp/pi-lab/m11a-sqlite-1/lab.sqlite
```

```
journal_mode: {"journal_mode":"wal"}
迁移跑两次后 lab_schema： {"version":1}
事务回调抛错： 回调失败
回滚后 notes 行数： {"n":0}
validateSqliteSchemaPrefix： SQLite schema prefix "sqlite_x" is reserved
durable 建的表： conversations, document_revisions, documents, durable_metadata, durable_schema, entries, record_ids, submissions, tasks
durable_schema： {"version":2}
durable_metadata： {"next_id":"2","next_seq":1}
```

**结论。** 预测成立。`next_id` 存成 TEXT，是因为 `node:sqlite` 拒绝超出 JavaScript 安全整数范围的 INTEGER 结果（`packages/durable/src/storage/sqlite/migrations.ts:13`）。

---

## 8. 自测与衔接

### 8.1 自测

1. 一个 commit 回调把 `tx.doc()` 拿到的 draft 存进外部变量，回调结束后再写它。会写进下一个 commit 吗？
2. 存储写入失败时，durable 怎样保证内存里的文档没有变？
3. 一次流式回答被 deferred commit 写了 50 次，每次追加一段文本。这 50 次修改最终怎样存储？

<details>
<summary>答案</summary>

1. 不会。回调结束时 durable 对每个 change 调用 `prepare()`（`packages/durable/src/session/transaction.ts:780`），overlay 状态离开 `open`，之后读写都抛 `Cannot use a settled overlay`（`packages/chord/src/delta/tracker.ts:588`）。D3 §7.2 实际运行了这个例子。
2. 内存修订只在 `adopt()` 时改变，而 `adopt()` 只在存储成功后的发布阶段调用。失败时 durable 调用 `abort()` 丢弃 prepared，`tracker.value` 仍是旧修订。
3. 每次的 `a` 操作进入那个文档的 `PendingOps`，在同一路径上合并成一个 `a`。下一次普通 commit 读写这个文档时，合并后的操作排在它自己的操作前面一起写入（D3 §7.3）。

</details>

### 8.2 衔接

本篇读到了文档的 draft、操作和 SQLite 事务。[D3](D3-storage-transactions.md) 从 `Session.commit()` 读起，看 durable 怎样把这些组合成一次原子 commit：事务对象怎样记录写入、何时 `prepare`、写入器怎样批量调用 `Storage.commit()`、`MemoryStorage` 怎样存储，最后怎样 `adopt`。
