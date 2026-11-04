// M11 D5 实验：工具执行到一半时进程被 SIGKILL 杀掉，重新打开后对比 replay: "safe" 和 "unsafe"（默认值）。
//
// 用法（PI 指向 pi 仓库，DB 是一个不存在的 SQLite 文件路径，每次实验换一个新名字）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11b-kill.ts run    DB safe|unsafe
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11b-kill.ts reopen DB safe|unsafe
//
// run：    打开 Harness，假模型返回一个 work 工具调用。work 先输出 "step 1 done"，等 1.5 秒
//          （让节流的进度提交落盘），然后用 SIGKILL 杀掉自己，模拟进程崩溃。
// reopen： 用同一个 DB 重新打开。先打印 open 之后的任务记录（restarts 计数），再恢复调度，
//          等会话空闲后打印 transcript 和 work 实际执行的次数。
// work 每次执行都在 DB.attempts 文件里追加一行，代表一个"外部副作用"。
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const [mode, db, replay] = process.argv.slice(2);
if ((mode !== "run" && mode !== "reopen") || !db || (replay !== "safe" && replay !== "unsafe")) {
	throw new Error("用法：m11b-kill.ts run|reopen DB safe|unsafe");
}

const { BACKGROUND_CONTEXT } = await load("packages/chord/src/context/index.ts");
const { Type } = await load("packages/ai/dist/index.js");
const { createModels } = await load("packages/ai/dist/models.js");
const { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } = await load("packages/ai/dist/providers/faux.js");
const { createRegistry, defineExtension, defineTool, Harness } = await load("packages/durable/src/index.ts");
const { openNodeSqliteStorage } = await load("packages/durable/src/storage/sqlite/node.ts");

const context = BACKGROUND_CONTEXT;
const attemptsFile = `${db}.attempts`;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const work = defineTool({
	name: "work",
	description: "Do some work with an external effect.",
	parameters: Type.Object({}),
	// "unsafe" 等于不写 replay（默认值）。
	...(replay === "safe" ? { replay: "safe" as const } : {}),
	execute: async (_args: unknown, api: { output(text: string): void }) => {
		appendFileSync(attemptsFile, `${mode}\n`);
		console.log(`[work] 第 ${readFileSync(attemptsFile, "utf8").trim().split("\n").length} 次执行（${mode} 进程）`);
		api.output("step 1 done\n");
		if (mode === "run") {
			await sleep(1500);
			console.log("[work] SIGKILL 自己");
			process.kill(process.pid, "SIGKILL");
		}
		return { output: [{ type: "text", text: "all steps done" }] };
	},
});

const models = createModels();
const faux = fauxProvider();
models.setProvider(faux.provider);
const registry = createRegistry();
registry.install(defineExtension({ name: "lab", tools: [work] }));
const harness = await Harness.open(await openNodeSqliteStorage(db), { models, registry }, context);

if (mode === "run") {
	faux.setResponses([fauxAssistantMessage([fauxToolCall("work", {}, { id: "call-1" })], { stopReason: "toolUse" })]);
	const root = await harness.root(context, { agent: { model: { provider: "faux", modelId: "faux-1" } } });
	await root.submit({ type: "input", content: "do the work" }, context);
	await sleep(10_000); // 不会走到这里：work 会杀掉进程
} else {
	faux.setResponses([fauxAssistantMessage([fauxText("ok")])]);
	const inspection = await harness.inspect(context);
	for (const { record, state } of inspection.tasks) {
		const checkpoint = "checkpoint" in record.state ? JSON.stringify(record.state.checkpoint) : "";
		console.log(
			`[open 之后] task ${record.id} ${record.kind} status=${record.state.status} restarts=${record.restarts ?? 0} ${checkpoint} (${state.kind})`,
		);
	}
	harness.resume();
	await harness.waitForIdle(context);
	const root = await harness.root(context);
	const page = await root.entries({ order: "ascending" }, 50, undefined, context);
	for (const entry of page.items) {
		const message = entry.model?.[0];
		if (message === undefined) continue;
		const text =
			typeof message.content === "string"
				? message.content
				: message.content
						.map((c: { type: string; text?: string; name?: string }) =>
							c.type === "text" ? c.text : c.type === "toolCall" ? `toolCall(${c.name})` : c.type,
						)
						.join(" | ");
		const extra = message.role === "toolResult" ? ` isError=${message.isError}` : "";
		console.log(`[transcript] ${entry.id} ${entry.kind} ${message.role}${extra}: ${JSON.stringify(text)}`);
	}
	const lines = existsSync(attemptsFile) ? readFileSync(attemptsFile, "utf8").trim().split("\n") : [];
	console.log(`[外部副作用] work 一共执行了 ${lines.length} 次：${lines.join(", ")}`);
	await harness.close(context);
}
