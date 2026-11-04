// M11 D2 实验：单独运行 durable 用到的三块 chord：chord/context、chord/delta、chord/sqlite。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11a-chord.ts [场景] [参数]
// 场景：
//   context   BACKGROUND_CONTEXT、withAbortSignal、withoutAbortSignal、awaitWithContext
//   track     track() → beginChange() → 改 draft → prepare() → adopt()，看 ops、revision、结构共享
//   rules     draft 的规则：settled 之后不能用、非 JSON 值被拒、竞争的 prepared 被拒、+= 和 appendText 的 op
//   undefined 给 draft 的对象属性赋 undefined、往数组里放 undefined
//   pending   PendingOps 折叠慢消费者积压的 ops
//   sqlite <db>  openNodeSqliteDatabase + applySqliteMigrations，事务回滚；再用 durable 的 openNodeSqliteStorage 打开
import { mkdirSync, rmSync } from "node:fs";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);
const ctx = await load("packages/chord/src/context/index.ts");
const delta = await load("packages/chord/src/delta/index.ts");
const sqlite = await load("packages/chord/src/sqlite/index.ts");
const sqliteNode = await load("packages/chord/src/sqlite/node.ts");

const scenario = process.argv[2] ?? "context";

/** 每次运行都从空文件开始：删掉上一次留下的数据库文件（含 WAL 和 SHM）。 */
function freshFile(path: string): void {
	mkdirSync(dirname(path), { recursive: true });
	for (const suffix of ["", "-wal", "-shm"]) rmSync(`${path}${suffix}`, { force: true });
}


async function contextScenario() {
	const background = ctx.BACKGROUND_CONTEXT;
	console.log("BACKGROUND_CONTEXT:", String(background), "abortSignal =", background.abortSignal);
	const controller = new AbortController();
	const child = ctx.withAbortSignal(controller.signal, background);
	console.log("withAbortSignal:", String(child), "aborted =", child.abortSignal?.aborted);
	const cleanup = ctx.withoutAbortSignal(child);
	console.log("withoutAbortSignal:", String(cleanup), "abortSignal =", cleanup.abortSignal);

	// awaitWithContext：取消只让这个等待者放弃，底层的 promise 照常完成。
	let finished = false;
	const work = new Promise<string>((resolve) =>
		setTimeout(() => {
			finished = true;
			resolve("work done");
		}, 50),
	);
	const waiting = ctx.awaitWithContext(work, child);
	setTimeout(() => controller.abort(), 10);
	try {
		await waiting;
	} catch (error: any) {
		console.log("等待者被取消：", error.name, "；此时 work 完成了吗？", finished);
	}
	console.log("直接 await work：", await work, "；work 完成了吗？", finished);
}

function trackScenario() {
	const initial = { output: "", entries: [{ id: 1 }], meta: { big: { a: 1 } } };
	const tracker = delta.track(initial);
	console.log("track 后 revision =", tracker.revision, "；value === initial：", tracker.value === initial);
	const change = tracker.beginChange();
	change.state.output = "done\n";
	change.state.entries.push({ id: 2 });
	const prepared = change.prepare();
	console.log("prepared.ops =", JSON.stringify(prepared.ops));
	console.log("prepared.base === tracker.value：", prepared.base === tracker.value);
	console.log("没改的子树结构共享 prepared.value.meta === base.meta：", prepared.value.meta === prepared.base.meta);
	console.log("adopt 前 tracker.value.output =", JSON.stringify(tracker.value.output));
	tracker.adopt(prepared);
	console.log("adopt 后 revision =", tracker.revision, "；tracker.value === prepared.value：", tracker.value === prepared.value);
	const replica = delta.applyImmutable(prepared.base, prepared.ops);
	console.log("applyImmutable(base, ops) 得到同样的值：", JSON.stringify(replica) === JSON.stringify(tracker.value));
	const noop = tracker.beginChange();
	noop.state.output = "done\n";
	console.log("写回原值：ops =", JSON.stringify(noop.prepare().ops));
}

function rules() {
	const tracker = delta.track({ text: "he", n: 0 } as { text: string; n: number; bad?: unknown });
	const change = tracker.beginChange();
	try {
		change.state.bad = () => 1;
	} catch (error: any) {
		console.log("放入函数：", error.name, error.message);
	}
	try {
		change.state.n = Number.NaN;
	} catch (error: any) {
		console.log("放入 NaN：", error.name, error.message);
	}
	const draft = change.state;
	change.prepare();
	try {
		draft.n = 1;
	} catch (error: any) {
		console.log("prepare 之后写 draft：", error.name, error.message);
	}
	// 两个 change 从同一个 revision 开始，先 adopt 的赢。
	const a = tracker.beginChange();
	const b = tracker.beginChange();
	a.state.n = 1;
	b.state.n = 2;
	const pa = a.prepare();
	const pb = b.prepare();
	tracker.adopt(pa);
	try {
		tracker.adopt(pb);
	} catch (error: any) {
		console.log("adopt 竞争者：", error.name, error.message);
	}
	const plus = tracker.beginChange();
	plus.state.text += "llo";
	console.log("text += \"llo\" 的 ops：", JSON.stringify(plus.prepare().ops));
	const append = tracker.beginChange();
	delta.appendText(append.state, "text", "llo");
	console.log("appendText(\"llo\") 的 ops：", JSON.stringify(append.prepare().ops));
}

function undef() {
	const tracker = delta.track({ text: "he", n: 0, list: [1, 2] } as { text: string; n?: number; list: unknown[] });
	const change = tracker.beginChange();
	change.state.n = undefined;
	console.log("给属性赋 undefined 之后 'n' in draft：", "n" in change.state);
	try {
		change.state.list.push(undefined);
	} catch (error: any) {
		console.log("数组 push(undefined)：", error.name, error.message);
	}
	const prepared = change.prepare();
	console.log("ops：", JSON.stringify(prepared.ops), "value：", JSON.stringify(prepared.value));
}

function pending() {
	const queue = new delta.PendingOps();
	const batches = [
		[["a", ["text"], "he"]],
		[["s", ["n"], 1]],
		[["a", ["text"], "y"]],
		[["s", ["n"], 2]],
	];
	for (const ops of batches) {
		queue.push(ops);
		console.log("push", JSON.stringify(ops), "→ size", queue.size);
	}
	const taken = queue.take();
	console.log("take():", JSON.stringify(taken));
	console.log("take 之后 size", queue.size);
	queue.push([["s", ["n"], 3]]);
	queue.push([["r", { text: "new", n: 0 }]]);
	console.log("r 之后 take():", JSON.stringify(queue.take()));
}

async function sqliteScenario(path: string) {
	freshFile(path);
	freshFile(`${path}.durable`);
	const db = await sqliteNode.openNodeSqliteDatabase(path);
	console.log("journal_mode:", JSON.stringify(await db.get("PRAGMA journal_mode")));
	const migrations = [
		{ version: 1, statements: ["CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL) STRICT"] },
	];
	await sqlite.applySqliteMigrations(db, migrations, { table: "lab_schema" });
	await sqlite.applySqliteMigrations(db, migrations, { table: "lab_schema" });
	console.log("迁移跑两次后 lab_schema：", JSON.stringify(await db.get("SELECT version FROM lab_schema")));
	try {
		await db.transaction(async (tx: any) => {
			await tx.run("INSERT INTO notes (text) VALUES (?)", "会被回滚");
			throw new Error("回调失败");
		});
	} catch (error: any) {
		console.log("事务回调抛错：", error.message);
	}
	console.log("回滚后 notes 行数：", JSON.stringify(await db.get("SELECT count(*) AS n FROM notes")));
	try {
		sqlite.validateSqliteSchemaPrefix("sqlite_x");
	} catch (error: any) {
		console.log("validateSqliteSchemaPrefix：", error.message);
	}
	await db.close();

	const { openNodeSqliteStorage } = await load("packages/durable/src/storage/sqlite/node.ts");
	const storage = await openNodeSqliteStorage(`${path}.durable`);
	await storage.close(ctx.BACKGROUND_CONTEXT);
	const check = await sqliteNode.openNodeSqliteDatabase(`${path}.durable`);
	const tables = await check.all("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name");
	console.log("durable 建的表：", tables.map((row: any) => row.name).join(", "));
	console.log("durable_schema：", JSON.stringify(await check.get("SELECT version FROM durable_schema")));
	console.log("durable_metadata：", JSON.stringify(await check.get("SELECT next_id, next_seq FROM durable_metadata")));
	await check.close();
}

switch (scenario) {
	case "context":
		await contextScenario();
		break;
	case "track":
		trackScenario();
		break;
	case "rules":
		rules();
		break;
	case "undefined":
		undef();
		break;
	case "pending":
		pending();
		break;
	case "sqlite":
		await sqliteScenario(process.argv[3] ?? "/tmp/pi-lab/m11a-sqlite-1/lab.sqlite");
		break;
	default:
		throw new Error(`未知场景 ${scenario}`);
}
