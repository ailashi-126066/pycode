// M11 D5/D6 实验：观察一次带工具调用的 run 里，任务图（task graph）和会话视图（conversation view）怎么变化。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m11b-observe.ts [graph|live|dup]
// graph（默认）：用 harness.observeTaskGraph() 同步观察任务图，每次变化打印一行：哪些任务、什么状态、在哪个阶段。
//                可以看到 pi.generation 依次经过的阶段，以及 pi.tool 是它的子任务。
// live：         用 root.watch() 观察会话视图，每次回调打印 pi.live 的摘要和新追加的条目。
//                可以看到流式 partial、工具槽位 pending → running → done、以及 run 结束时 pi.live 被清空。
// dup：          用同一个 requestId 提交两次，打印两次拿到的 submission id 和 entry 数量。
// hook：         另装一个扩展，用 hook(ToolTask, …) 注册 beforeTool（拦截 slow）和 afterTool（打印结果），
//                再用 hook(GenerationTask, …) 注册 beforeRequest（打印发给模型的消息 role）。
// 内存存储，假模型：第 1 次回复 "我来读一下" + 一个 slow 工具调用，第 2 次回复 "读完了"。
// slow 工具输出两段文本，中间等 GAP_MS 毫秒（环境变量，默认 300），让节流的进度提交有机会写入 pi.live。
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const mode = process.argv[2] ?? "graph";
const { BACKGROUND_CONTEXT } = await load("packages/chord/src/context/index.ts");
const { Type } = await load("packages/ai/dist/index.js");
const { createModels } = await load("packages/ai/dist/models.js");
const { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } = await load("packages/ai/dist/providers/faux.js");
const { createRegistry, defineExtension, defineTool, GenerationTask, Harness, hook, MemoryStorage, ToolTask } = await load(
	"packages/durable/src/index.ts",
);
const context = BACKGROUND_CONTEXT;
const GAP_MS = Number(process.env.GAP_MS ?? 300);
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const t0 = Date.now();
const ms = () => String(Date.now() - t0).padStart(4);

const slow = defineTool({
	name: "slow",
	description: "Read slowly.",
	parameters: Type.Object({ path: Type.String() }),
	execute: async (args: { path: string }, api: { output(text: string): void }) => {
		api.output(`reading ${args.path}\n`);
		await sleep(GAP_MS);
		api.output("half done\n");
		await sleep(300);
		return { output: [{ type: "text", text: `${args.path}: 3 lines` }] };
	},
});

const models = createModels();
// tokensPerSecond 让假模型慢慢吐字，生成阶段的 partial 才会被节流提交
const faux = fauxProvider({ tokensPerSecond: 40 });
models.setProvider(faux.provider);
faux.setResponses([
	fauxAssistantMessage([fauxText("我来读一下 README。"), fauxToolCall("slow", { path: "README.md" }, { id: "call-1" })], {
		stopReason: "toolUse",
	}),
	fauxAssistantMessage([fauxText("读完了，一共三行。")]),
]);
const registry = createRegistry();
registry.install(defineExtension({ name: "lab", tools: [slow] }));
if (mode === "hook") {
	registry.install(
		defineExtension({
			name: "policy",
			hooks: [
				hook(ToolTask, {
					beforeTool: (call: { name: string; arguments: unknown }) => {
						console.log(`${ms()}  [beforeTool] ${call.name} ${JSON.stringify(call.arguments)} → block`);
						return { block: "lab policy forbids slow" };
					},
					afterTool: (call: { name: string }, result: any) => {
						const codes = (result.diagnostics ?? []).map((d: any) => d.code).join(",");
						console.log(`${ms()}  [afterTool] ${call.name} isError=${result.isError} diagnostics=${codes}`);
						return undefined;
					},
				}),
				hook(GenerationTask, {
					beforeRequest: ({ messages }: { messages: { role: string }[] }) => {
						console.log(`${ms()}  [beforeRequest] ${messages.map((m) => m.role).join(", ")}`);
						return undefined;
					},
				}),
			],
		}),
	);
}
const harness = await Harness.open(new MemoryStorage(), { models, registry }, context);
const root = await harness.root(context, { agent: { model: { provider: "faux", modelId: "faux-1" } } });

if (mode === "graph") {
	let last = "";
	const show = (graph: { tasks: Record<string, any> }) => {
		const parts = Object.values(graph.tasks).map((n: any) => {
			const s = n.state;
			const where = s.status === "completing" ? `outcome=${s.outcome}` : `phase=${s.phase}`;
			const on = s.status === "waiting" ? ` on=[${s.on}]` : "";
			const owner = n.owner === undefined ? "" : ` owner=${n.owner}`;
			return `${n.id}:${n.kind} ${s.status} ${where}${on}${owner}`;
		});
		const line = parts.length === 0 ? "(没有活任务)" : parts.join("  |  ");
		if (line !== last) console.log(`${ms()}  ${line}`);
		last = line;
	};
	const observation = await harness.observeTaskGraph(
		{ advance: (call: { value: any }) => show(call.value), end: () => {} },
		context,
	);
	show(observation.value);
	const submission = await root.submit({ type: "input", content: "读 README.md" }, context);
	await submission.wait(context);
	await harness.waitForIdle(context);
	observation.detach();
} else if (mode === "live") {
	const watch = await root.watch(context);
	let newest = watch.value.entries.at(-1)?.id ?? 0;
	let last = "";
	watch.start(async (call: any) => {
		if (call.kind !== "value") return;
		const { value } = call;
		for (const entry of value.entries) {
			if (entry.id <= newest) continue;
			console.log(`${ms()}  [条目] ${entry.id} ${entry.kind}`);
		}
		newest = value.entries.at(-1)?.id ?? newest;
		const live = value.docs["pi.live"] ?? {};
		const parts: string[] = [];
		if (live.run) parts.push(`run={taskId:${live.run.taskId},inputs:[${live.run.inputs}],turns:${live.run.turns}}`);
		if (live.generation) {
			const text = (live.generation.message?.content ?? [])
				.filter((c: any) => c.type === "text")
				.map((c: any) => c.text)
				.join("");
			parts.push(`generation={attempt:${live.generation.attempt},partial:${JSON.stringify(text)}}`);
		}
		for (const slot of live.tools ?? []) {
			parts.push(`tool=${slot.name}#${slot.taskId} ${slot.status} output=${JSON.stringify(slot.output ?? "")}${slot.entry ? ` entry=${slot.entry}` : ""}`);
		}
		const line = parts.length === 0 ? "pi.live = {}" : `pi.live: ${parts.join("  ")}`;
		if (line !== last) console.log(`${ms()}  ${line}`);
		last = line;
	});
	const submission = await root.submit({ type: "input", content: "读 README.md" }, context);
	await submission.wait(context);
	await harness.waitForIdle(context);
	await sleep(0);
	await watch.stop();
} else if (mode === "hook") {
	const submission = await root.submit({ type: "input", content: "读 README.md" }, context);
	await submission.wait(context);
	await harness.waitForIdle(context);
	const page = await root.entries({ order: "ascending" }, 50, undefined, context);
	for (const entry of page.items) {
		const message = entry.model?.[0];
		if (message?.role !== "toolResult") continue;
		const text = message.content.map((c: any) => c.text).join("");
		console.log(`[transcript] ${entry.id} ${entry.kind} isError=${message.isError}: ${JSON.stringify(text)}`);
	}
} else {
	const first = await root.submit({ type: "input", content: "读 README.md", requestId: "req-1" }, context);
	const second = await root.submit({ type: "input", content: "读 README.md", requestId: "req-1" }, context);
	console.log(`第 1 次 submit：id=${first.id}`);
	console.log(`第 2 次 submit：id=${second.id}`);
	const settled = await second.wait(context);
	console.log(`settled: ${JSON.stringify(settled)}`);
	await harness.waitForIdle(context);
	const page = await root.entries({ order: "ascending" }, 50, undefined, context);
	console.log(`条目：${page.items.map((e: any) => `${e.id}:${e.kind}`).join(", ")}`);
}
await harness.close(context);
