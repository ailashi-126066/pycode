// M11 D4 实验：调度器怎么对待"每次都崩溃"的任务（restartLimit），以及"没有持久进度"的阶段（规则 6）。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11b-crashloop.ts crash DB
//       DB 是一个 SQLite 文件路径。第一次运行创建任务；之后每次运行都用同一个 DB 重新打开。
//       任务的 work 阶段每次都用 SIGKILL 杀掉进程。settings.restartLimit 设为 2。
//       每次运行先打印 open 之后的任务记录，再恢复调度；任务结束时打印它的 outcome 和收到的报告。
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11b-crashloop.ts noprogress
//       内存存储。一个阶段返回时没有提交新检查点，调度器把任务写成 faulted。
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11b-crashloop.ts record
//       内存存储。work 阶段写一个 memo，然后 sleepUntil 100ms 后进入 finish 阶段。
//       打印任务记录（TaskRecord）在 pending、sleeping、terminal 三个时刻的完整 JSON。
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const [mode, db] = process.argv.slice(2);
if (!(mode === "crash" && db) && mode !== "noprogress" && mode !== "record") {
	throw new Error("用法：m11b-crashloop.ts crash DB | noprogress | record");
}

const { BACKGROUND_CONTEXT } = await load("packages/chord/src/context/index.ts");
const { createModels } = await load("packages/ai/dist/models.js");
const { createRegistry, defineExtension, defineTask, Harness, MemoryStorage } = await load(
	"packages/durable/src/index.ts",
);
const { openNodeSqliteStorage } = await load("packages/durable/src/storage/sqlite/node.ts");
const context = BACKGROUND_CONTEXT;

type State = { phase: "prepare" } | { phase: "work"; step: number };
const Crashy = defineTask<Record<string, never>, State, string>({
	name: "lab.crashy",
	version: 1,
	initial: () => ({ phase: "prepare" }),
	phases: {
		prepare: async (_task: unknown, runtime: any, taskContext: unknown) => {
			// 意图：先把"要做 work"写进检查点
			await runtime.commit(() => ({ status: "running", checkpoint: { phase: "work", step: 1 } }), taskContext);
		},
		work: async (task: any, runtime: any, taskContext: unknown) => {
			if (mode === "noprogress") {
				console.log(`[work] 返回，但没有提交新检查点（step=${task.state.checkpoint.step}）`);
				return;
			}
			console.log(`[work] 开始执行，restarts=${task.restarts ?? 0}，马上 SIGKILL`);
			process.kill(process.pid, "SIGKILL");
			await runtime.commit(() => ({ status: "terminal", outcome: { status: "completed", result: "done" } }), taskContext);
		},
	},
	abort: async (_task: unknown, runtime: any, taskContext: unknown) => {
		await runtime.commit(() => ({ status: "terminal", outcome: { status: "aborted" } }), taskContext);
	},
});

type SleepState = { phase: "work" } | { phase: "finish"; note: string };
const Sleeper = defineTask<{ label: string }, SleepState, string>({
	name: "lab.sleeper",
	version: 1,
	initial: () => ({ phase: "work" }),
	phases: {
		work: async (_task: unknown, runtime: any, taskContext: unknown) => {
			await runtime.memo("effect-done", true, taskContext);
			await runtime.sleepUntil(runtime.now() + 100, { phase: "finish", note: "after sleep" }, taskContext);
		},
		finish: async (task: any, runtime: any, taskContext: unknown) => {
			const result = `${task.input.label}: ${task.state.checkpoint.note}`;
			await runtime.commit(() => ({ status: "terminal", outcome: { status: "completed", result } }), taskContext);
		},
	},
	abort: async (_task: unknown, runtime: any, taskContext: unknown) => {
		await runtime.commit(() => ({ status: "terminal", outcome: { status: "aborted" } }), taskContext);
	},
});

const registry = createRegistry();
registry.install(defineExtension({ name: "lab", tasks: [Crashy, Sleeper] }));

if (mode === "record") {
	const harness = await Harness.open(new MemoryStorage(), { models: createModels(), registry }, context);
	const root = await harness.root(context);
	const id = await root.commit(
		(tx: any) => tx.createTask(Sleeper, { label: "demo" }, { ownership: { kind: "conversation" } }),
		context,
	);
	console.log(`[pending]  ${JSON.stringify(await harness.getTask(id, context))}`);
	harness.resume();
	await new Promise((resolve) => setTimeout(resolve, 30));
	console.log(`[sleeping] ${JSON.stringify(await harness.getTask(id, context))}`);
	await harness.waitForTask(id, context);
	console.log(`[terminal] ${JSON.stringify(await harness.getTask(id, context))}`);
	await harness.close(context);
	process.exit(0);
}
const storage = mode === "crash" ? await openNodeSqliteStorage(db) : new MemoryStorage();
const harness = await Harness.open(
	storage,
	{
		models: createModels(),
		registry,
		settings: { restartLimit: 2 },
		onReport: (report: { kind: string; reason?: string; error: unknown }) =>
			console.log(`[onReport] ${report.kind} reason=${report.reason} ${String((report.error as Error).message)}`),
	},
	context,
);

let id;
const { tasks } = await harness.inspect(context);
const live = tasks.find((t: any) => t.record.kind === "lab.crashy");
if (live === undefined) {
	const root = await harness.root(context);
	id = await root.commit(
		(tx: any) => tx.createTask(Crashy, {}, { ownership: { kind: "conversation" } }),
		context,
	);
	console.log(`[创建] task ${id}`);
} else {
	const r = live.record;
	id = r.id;
	console.log(
		`[open 之后] task ${r.id} status=${r.state.status} restarts=${r.restarts ?? 0} checkpoint=${JSON.stringify(r.state.checkpoint)}`,
	);
}
const settled = await harness.waitForTask(id, context);
console.log(`[结束] outcome=${JSON.stringify(settled.state.outcome)}`);
await harness.close(context);
