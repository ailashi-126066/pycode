// M11 D1/D3 实验：用 durable 跑一遍主线场景，并把每次 Storage.commit() 收到的写入打印出来。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11a-durable.ts [场景] [参数]
// 场景：
//   main        主线场景：faux 模型第 1 次返回"文本 + read 工具调用"，第 2 次返回总结。打印每次 Storage.commit()
//   records     同 main，但不打印提交，只在结束后打印各类记录（会话、条目、任务、提交、文档）
//   context     同 main，结束后打印 Conversation.context() 给下一次请求的 LLM 消息
//   persist <db>  在 SQLite 文件 <db> 上跑 main，close 后重新打开，用同一个 requestId 再 submit 一次
//   reset       同 main，结束后 reset()，再看 Conversation.context()
//   noregistry  用一个没有内置任务的 registry 打开 Harness
//   raw         在一个 commit 里先写表再读表，看 ReadAfterWrite 和回滚
//   read-first  同 raw，但把表读挪到表写入之前
//   revoked     把 tx.doc() 返回的 draft 带出回调，再写它
//   group       不 await，连续发起 3 个 commit，看它们怎样合并进 Storage.commit() 调用
//   checkpoint  同一个文档连续改 5 次，checkpointWhen 为 deltasSinceBase >= 2，看 base 和 delta 的选择
//   deferred-flush        两次 deferred commit 之后跟一次普通 commit，看被扣住的操作怎样随它一起存储
//   deferred-crash <db>   在 SQLite 文件 <db> 上做一次 deferred commit，不 close 直接退出进程
//   deferred-reopen <db>  重新打开 <db>，读出那个文档
// 环境变量 WORK：read 工具读取的目录，默认 /tmp/pi-lab/m11a-work-1（没有 README.md 时自动生成）。
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);
const durable = await load("packages/durable/src/index.ts");
const { openNodeSqliteStorage } = await load("packages/durable/src/storage/sqlite/node.ts");
const { BACKGROUND_CONTEXT } = await load("packages/chord/src/context/index.ts");
const ai = await load("packages/ai/dist/index.js");
const { createModels } = await load("packages/ai/dist/models.js");
const { createRegistry, defineDoc, defineExtension, defineTool, Harness, MemoryStorage } = durable;

const context = BACKGROUND_CONTEXT;

/** 每次运行都从空文件开始：删掉上一次留下的数据库文件（含 WAL 和 SHM）。 */
function freshFile(path: string): void {
	mkdirSync(dirname(path), { recursive: true });
	for (const suffix of ["", "-wal", "-shm"]) rmSync(`${path}${suffix}`, { force: true });
}

const scenario = process.argv[2] ?? "main";
const WORK = process.env.WORK ?? "/tmp/pi-lab/m11a-work-1";

// ---- 一个会打印每次 commit 调用的 Storage：把 MemoryStorage 或 SqliteStorage 包一层 ----
const docKinds = new Map<number, string>();
let calls = 0;
let logCommits = true;
/** group 场景用：每次 Storage.commit() 人为慢 20ms，让后面的 commit 有机会排队。 */
let slowMs = 0;
/** deferred-flush 场景用：打印 delta 的完整操作。 */
let showOps = false;
function describeWrite(write: any): string {
	switch (write.type) {
		case "conversation":
			return `conversation #${write.value.id} ${JSON.stringify(write.value)}`;
		case "entry": {
			const roles = (write.value.model ?? []).map((m: any) => m.role).join(",");
			const by = write.value.byTaskId === undefined ? "" : ` byTaskId=${write.value.byTaskId}`;
			return `entry #${write.value.id} ${write.value.kind} model=[${roles}]${by}`;
		}
		case "task": {
			const s = write.value.state;
			const detail = s.checkpoint !== undefined ? ` phase=${s.checkpoint.phase}` : s.outcome ? ` outcome=${s.outcome.status}` : "";
			const owner = write.value.owner === undefined ? "" : ` owner=${write.value.owner}`;
			return `task #${write.value.id} ${write.value.kind} ${s.status}${detail}${owner}`;
		}
		case "submission":
			return `submission #${write.value.id} ${write.value.type} ${write.value.status}`;
		case "document.create":
			docKinds.set(write.record.id, write.record.kind);
			return `document.create #${write.record.id} ${write.record.kind} base`;
		case "document.change": {
			const c = write.content;
			const what =
				c.kind === "base"
					? `base${showOps ? ` ${JSON.stringify(c.value)}` : ""}`
					: `delta ${c.ops.length} ops ${showOps ? JSON.stringify(c.ops) : `[${c.ops.map((op: any) => op[0]).join("")}]`}`;
			return `document.change #${write.id} ${docKinds.get(write.id) ?? "?"} ${what}`;
		}
		case "document.retire":
			return `document.retire #${write.id} ${docKinds.get(write.id) ?? "?"}`;
		default:
			return write.type;
	}
}
function logged<S extends object>(inner: S): S {
	return new Proxy(inner, {
		get(target: any, property) {
			if (property === "commit") {
				return async (batches: any[], receive: (index: number, seq: number) => void, ctx: unknown) => {
					const call = ++calls;
					const seqs: number[] = [];
					if (slowMs > 0) {
						console.log(`[Storage.commit 第 ${call} 次开始] ${batches.length} 个 batch`);
						await new Promise((resolve) => setTimeout(resolve, slowMs));
					}
					const result = await target.commit(batches, (index: number, seq: number) => {
						seqs[index] = seq;
						receive(index, seq);
					}, ctx);
					if (logCommits) {
						console.log(`[Storage.commit 第 ${call} 次] ${batches.length} 个 batch`);
						batches.forEach((writes, index) => {
							console.log(`  seq=${seqs[index]}`);
							for (const write of writes) console.log(`    ${describeWrite(write)}`);
						});
					}
					return result;
				};
			}
			const value = target[property];
			return typeof value === "function" ? value.bind(target) : value;
		},
	});
}

// ---- 主线场景：faux 模型 + 一个 read 工具 ----
async function mainScenario(inner: object = new MemoryStorage()) {
	mkdirSync(WORK, { recursive: true });
	if (!existsSync(join(WORK, "README.md"))) {
		writeFileSync(join(WORK, "README.md"), "# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n");
	}
	const faux = ai.fauxProvider();
	const models = createModels();
	models.setProvider(faux.provider);
	faux.setResponses([
		ai.fauxAssistantMessage(
			[ai.fauxText("我先读一下 README。"), ai.fauxToolCall("read", { path: "README.md" }, { id: "call_1" })],
			{ stopReason: "toolUse" },
		),
		ai.fauxAssistantMessage("这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"),
	]);
	const read = defineTool({
		name: "read",
		description: "Read a file",
		parameters: ai.Type.Object({ path: ai.Type.String() }),
		execute: async (args: { path: string }) => ({
			output: [{ type: "text", text: readFileSync(join(WORK, args.path), "utf8") }],
		}),
	});
	const registry = createRegistry();
	registry.install(defineExtension({ name: "files", tools: [read] }));
	const memory = inner as any;
	const storage = logged(memory);

	if (logCommits) console.log("== Harness.open()");
	const harness = await Harness.open(storage, { models, registry }, context);
	if (logCommits) console.log("== harness.root()");
	const root = await harness.root(context, { agent: { model: { provider: "faux", modelId: "faux-1" } } });
	if (logCommits) console.log("== root.submit()");
	const submission = await root.submit(
		{ type: "input", content: "读 README.md，总结成三句话", requestId: "main-1" },
		context,
	);
	if (logCommits) console.log(`== submit() 返回 Submission id=${submission.id}，开始 wait()`);
	const settled = await submission.wait(context);
	if (logCommits) console.log("== wait() 返回", JSON.stringify(settled));
	return { harness, root, memory, submission, models, registry };
}

function short(value: unknown, max = 160): string {
	const text = JSON.stringify(value);
	return text.length > max ? `${text.slice(0, max)}…` : text;
}

async function records() {
	logCommits = false;
	const { harness, root, memory, submission } = await mainScenario();
	console.log("[ConversationRecord]", JSON.stringify(await memory.conversation(root.id, context)));
	const page = await root.entries({ order: "ascending" }, 100, undefined, context);
	console.log("[EntryRecord] 共", page.items.length, "条");
	for (const entry of page.items) {
		const roles = (entry.model ?? []).map((m: any) => m.role).join(",");
		console.log(`  #${entry.id} ${entry.kind.padEnd(14)} model=[${roles}] byTaskId=${entry.byTaskId ?? "-"}`);
	}
	const assistant = page.items.find((e: any) => e.kind === "pi.assistant");
	console.log("[EntryRecord] 第一条 pi.assistant：", JSON.stringify(assistant));
	const tasks = await memory.scanTasks({}, 100, undefined, context);
	console.log("[TaskRecord] 共", tasks.items.length, "个");
	for (const t of tasks.items) {
		console.log(`  #${t.id} ${t.kind.padEnd(14)} owner=${t.owner ?? "-"} ${t.state.status} outcome=${t.state.outcome?.status}`);
	}
	const tool = tasks.items.find((t: any) => t.kind === "pi.tool");
	console.log("[TaskRecord] pi.tool：", JSON.stringify(tool));
	const generation = tasks.items.find((t: any) => t.kind === "pi.generation");
	console.log("[TaskRecord] 第一个 pi.generation：", short(generation, 400));
	console.log("[SubmissionRecord]", JSON.stringify(await submission.status(context)));
	for (const token of [durable.AgentDoc, durable.ProviderDoc, durable.LiveDoc, durable.InboxDoc, durable.UsageDoc]) {
		console.log(`[文档 ${token.definition.kind}]`, short(await harness.snapshot(token, root.id, context), 220));
	}
	const docs = await memory.scanDocuments({ scope: { kind: "conversation", conversationId: root.id }, at: "current" }, 100, undefined, context);
	console.log("[DocumentRecord]");
	for (const record of docs.items) console.log("  ", JSON.stringify(record));
	await harness.close(context);
}

async function contextScenario() {
	logCommits = false;
	const { harness, root } = await mainScenario();
	const view = await root.context(context);
	console.log("head:", view.head?.id ?? "无");
	console.log("entries:", view.entries.map((e: any) => `#${e.id} ${e.kind}`).join(", "));
	console.log("下一次请求的 LLM 消息：");
	for (const m of view.messages) {
		const content = m.role === "system" ? `sections=[${Object.keys(m.sections ?? {})}] toolsAdded=[${(m.toolsAdded ?? []).map((t: any) => t.name)}]` : short(m.content, 70);
		console.log(`  ${m.role.padEnd(10)} ${content}`);
	}
	await harness.close(context);
}

async function persist(path: string) {
	freshFile(path);
	logCommits = false;
	const first = await mainScenario(await openNodeSqliteStorage(path));
	console.log("第一个进程：submission id =", first.submission.id);
	await first.harness.close(context);
	// 同一个 registry 和 models；faux 已经没有预设回复，再请求模型就会失败。
	const harness = await Harness.open(logged(await openNodeSqliteStorage(path)), { models: first.models, registry: first.registry }, context);
	const root = await harness.root(context);
	logCommits = true;
	console.log("== 重新打开后，用同一个 requestId 再 submit 一次");
	const again = await root.submit({ type: "input", content: "读 README.md，总结成三句话", requestId: "main-1" }, context);
	console.log("submission id =", again.id, "status =", JSON.stringify(await again.status(context)));
	const page = await root.entries({}, 100, undefined, context);
	console.log("条目数：", page.items.length);
	await harness.close(context);
}

async function resetScenario() {
	logCommits = false;
	const { harness, root } = await mainScenario();
	logCommits = true;
	console.log("== root.reset(\"接着上次的总结继续\")");
	await root.reset("接着上次的总结继续", context);
	const view = await root.context(context);
	console.log("head:", JSON.stringify(view.head));
	console.log("entries:", view.entries.map((e: any) => `#${e.id} ${e.kind}`).join(", "));
	console.log("messages:", view.messages.map((m: any) => `${m.role} ${short(m.content, 60)}`).join(" | "));
	const page = await root.entries({ order: "ascending" }, 100, undefined, context);
	console.log("存储里的条目：", page.items.map((e: any) => `#${e.id} ${e.kind}`).join(", "));
	await harness.close(context);
}

// ---- 只用 Session：不需要模型 ----
const Notes = defineDoc({
	kind: "lab.notes",
	version: 1,
	scope: "conversation",
	history: "latest",
	fork: "initial",
	initial: () => ({ text: "", n: 0 }),
	checkpointWhen: (_value: unknown, _ops: unknown, info: { deltasSinceBase: number }) => info.deltasSinceBase >= 2,
});
/** 和 Notes 只差 history：rewindable 的文档保留全部历史。 */
const RewindNotes = defineDoc({
	kind: "lab.rewind-notes",
	version: 1,
	scope: "conversation",
	history: "rewindable",
	fork: "asOf",
	initial: () => ({ n: 0 }),
	checkpointWhen: (_value: unknown, _ops: unknown, info: { deltasSinceBase: number }) => info.deltasSinceBase >= 2,
});

async function openPlain() {
	const memory = new MemoryStorage();
	const session = durable.createSession(logged(memory));
	const chat = await session.commit((tx: any) => tx.createConversation({ ownership: { kind: "ownerless" } }), context);
	return { memory, session, chat };
}

async function raw() {
	const { session, chat } = await openPlain();
	const before = calls;
	try {
		await session.commit(async (tx: any) => {
			const entry = await tx.appendEntry(chat.id, { kind: "lab.note", data: "hello" });
			console.log("appendEntry 返回", JSON.stringify(entry));
			(await tx.doc(Notes, chat.id)).text = "写在表写入之后，文档仍然可写";
			await tx.conversation(chat.id); // 第一次表写入之后的表读
		}, context);
	} catch (error: any) {
		console.log("commit 被拒绝：", error.name, error.message);
	}
	console.log("这次 commit 调用 Storage.commit() 的次数：", calls - before);
	const page = await session.commit((tx: any) => tx.scanEntries({ conversationId: chat.id }, 10), context);
	console.log("会话里的条目数：", page.items.length);
	console.log("lab.notes 文档：", JSON.stringify(await session.snapshot(Notes, chat.id, context)));
	await session.close(context);
}

async function readFirst() {
	const { session, chat } = await openPlain();
	await session.commit(async (tx: any) => {
		await tx.conversation(chat.id); // 第一次表写入之前的表读
		await tx.appendEntry(chat.id, { kind: "lab.note", data: "hello" });
		(await tx.doc(Notes, chat.id)).text = "写在表写入之后";
	}, context);
	const page = await session.commit((tx: any) => tx.scanEntries({ conversationId: chat.id }, 10), context);
	console.log("会话里的条目数：", page.items.length);
	console.log("lab.notes 文档：", JSON.stringify(await session.snapshot(Notes, chat.id, context)));
	await session.close(context);
}

async function revoked() {
	const { session, chat } = await openPlain();
	let escaped: any;
	await session.commit(async (tx: any) => {
		escaped = await tx.doc(Notes, chat.id);
		escaped.text = "回调里写";
	}, context);
	try {
		escaped.text = "回调结束后写";
	} catch (error: any) {
		console.log("回调结束后写 draft：", error.name, error.message);
	}
	console.log("lab.notes 文档：", JSON.stringify(await session.snapshot(Notes, chat.id, context)));
	await session.close(context);
}

async function group() {
	const { session, chat } = await openPlain();
	slowMs = 20;
	console.log("== 连续发起 3 个 commit，不 await");
	const done = [1, 2, 3].map((n) =>
		session.commit(async (tx: any) => {
			console.log(`  回调 ${n} 运行`);
			await tx.appendEntry(chat.id, { kind: "lab.note", data: n });
			return n;
		}, context).then((n: number) => console.log(`  commit ${n} resolve`)),
	);
	await Promise.all(done);
	await session.close(context);
}

async function checkpoint() {
	const { memory, session, chat } = await openPlain();
	for (let n = 1; n <= 5; n++) {
		await session.commit(async (tx: any) => {
			(await tx.doc(Notes, chat.id)).n = n;
			(await tx.doc(RewindNotes, chat.id)).n = n;
		}, context);
	}
	const state = (memory as any).state;
	for (const [id, stored] of state.documents) {
		console.log(`MemoryStorage 里 #${id} ${stored.record.kind} 保留的 revisions：`, stored.revisions.map((r: any) => `${r.kind}@seq${r.seq}`).join(" "));
	}
	console.log("当前值：", JSON.stringify(await session.snapshot(Notes, chat.id, context)));
	await session.close(context);
}

async function deferredFlush() {
	const { session, chat } = await openPlain();
	showOps = true;
	await session.commit(async (tx: any) => {
		(await tx.doc(Notes, chat.id)).text = "";
	}, context);
	const deferred = { persistence: { kind: "deferred", withinMs: 60_000 } };
	console.log("== deferred commit 1：text = \"A\"");
	await session.commit(async (tx: any) => {
		(await tx.doc(Notes, chat.id)).text = "A";
	}, context, deferred);
	console.log("== deferred commit 2：text = \"AB\"");
	await session.commit(async (tx: any) => {
		(await tx.doc(Notes, chat.id)).text = "AB";
	}, context, deferred);
	console.log("snapshot：", JSON.stringify(await session.snapshot(Notes, chat.id, context)));
	console.log("== 普通 commit：n = 1");
	await session.commit(async (tx: any) => {
		(await tx.doc(Notes, chat.id)).n = 1;
	}, context);
	await session.close(context);
}

async function deferredCrash(path: string) {
	freshFile(path);
	const session = durable.createSession(logged(await openNodeSqliteStorage(path)));
	const chat = await session.commit((tx: any) => tx.createConversation({ ownership: { kind: "ownerless" } }), context);
	await session.commit(async (tx: any) => {
		(await tx.doc(Notes, chat.id)).text = "已存储";
	}, context);
	console.log("== deferred commit，withinMs=60000");
	await session.commit(
		async (tx: any) => {
			(await tx.doc(Notes, chat.id)).text = "只在内存里";
		},
		context,
		{ persistence: { kind: "deferred", withinMs: 60_000 } },
	);
	console.log("deferred commit 已 resolve；snapshot：", JSON.stringify(await session.snapshot(Notes, chat.id, context)));
	console.log("不 close，直接退出进程（模拟崩溃）");
	process.exit(0);
}

async function deferredReopen(path: string) {
	const session = durable.createSession(logged(await openNodeSqliteStorage(path)));
	const page = await session.commit((tx: any) => tx.scanConversations({}, 10), context);
	const chat = page.items.at(-1);
	console.log("重新打开后 snapshot：", JSON.stringify(await session.snapshot(Notes, chat.id, context)));
	await session.close(context);
}

switch (scenario) {
	case "main": {
		const { harness } = await mainScenario();
		console.log("== harness.close()");
		await harness.close(context);
		break;
	}
	case "records":
		await records();
		break;
	case "context":
		await contextScenario();
		break;
	case "persist":
		await persist(process.argv[3] ?? "/tmp/pi-lab/m11a-persist-1/session.sqlite");
		break;
	case "reset":
		await resetScenario();
		break;
	case "noregistry": {
		// 一个 snapshot 里什么任务都没有的 registry：Harness.open() 应当在打开存储之前就拒绝。
		const empty = { snapshot: () => ({ task: () => undefined }) };
		try {
			await Harness.open(logged(new MemoryStorage()), { models: createModels(), registry: empty }, context);
		} catch (error: any) {
			console.log("Harness.open() 拒绝：", error.message);
		}
		console.log("Storage.commit() 调用次数：", calls);
		break;
	}
	case "raw":
		await raw();
		break;
	case "read-first":
		await readFirst();
		break;
	case "revoked":
		await revoked();
		break;
	case "group":
		await group();
		break;
	case "checkpoint":
		await checkpoint();
		break;
	case "deferred-flush":
		await deferredFlush();
		break;
	case "deferred-crash":
		await deferredCrash(process.argv[3] ?? "/tmp/pi-lab/m11a-deferred-1/session.sqlite");
		break;
	case "deferred-reopen":
		await deferredReopen(process.argv[3] ?? "/tmp/pi-lab/m11a-deferred-1/session.sqlite");
		break;
	default:
		throw new Error(`未知场景 ${scenario}`);
}
