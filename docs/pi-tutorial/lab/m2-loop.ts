// M2 实验：不经过 coding-agent，直接驱动 pi-agent-core 的 agentLoop。
//
// 用法（在任意目录运行均可，PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m2-loop.ts [场景]
// 场景：
//   basic       一次工具调用，然后回答（默认）
//   parallel    一次返回两个工具调用：slow 耗时 300ms，fast 耗时 50ms，并行执行
//   sequential  同上，但把 toolExecution 设为 "sequential"
//   mixed       同 parallel，toolExecution 仍是 "parallel"，但 fast 工具自己声明 executionMode: "sequential"
//   length      模型输出被截断（stopReason: "length"），带一个工具调用
//   steer       第一轮工具执行时插入一条 steering 消息
// 环境变量 STREAM=1：改用 agentLoop + for await 消费事件（对比打印顺序）
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const { agentLoop, runAgentLoop } = await load("packages/agent/src/index.ts");
const { createAssistantMessageEventStream } = await load("packages/ai/src/index.ts");
const { Type } = await import(pathToFileURL(`${PI}/node_modules/typebox/build/index.mjs`).href);

const scenario = process.argv[2] ?? "basic";
const t0 = Date.now();
const ms = () => String(Date.now() - t0).padStart(4);

// ---- 模型：按顺序返回预先写好的回复 ----
const model = {
	id: "script",
	name: "script",
	api: "script-api",
	provider: "script",
	baseUrl: "",
	reasoning: false,
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	contextWindow: 8192,
	maxTokens: 1024,
};
const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const assistant = (content: any[], stopReason = "stop") => ({
	role: "assistant",
	content,
	api: model.api,
	provider: model.provider,
	model: model.id,
	usage,
	stopReason,
	timestamp: Date.now(),
});
const call = (id: string, name: string, args: object) => ({ type: "toolCall", id, name, arguments: args });

const scripts: Record<string, any[]> = {
	basic: [assistant([call("c1", "fast", {})], "toolUse"), assistant([{ type: "text", text: "完成" }])],
	parallel: [
		assistant([call("c1", "slow", {}), call("c2", "fast", {})], "toolUse"),
		assistant([{ type: "text", text: "完成" }]),
	],
	length: [assistant([call("c1", "fast", {})], "length"), assistant([{ type: "text", text: "重新来过" }])],
	steer: [assistant([call("c1", "slow", {})], "toolUse"), assistant([{ type: "text", text: "收到插话" }]), assistant([{ type: "text", text: "多余" }])],
};
scripts.sequential = scripts.parallel;
scripts.mixed = scripts.parallel;
const replies = [...scripts[scenario]];

// StreamFn 契约：不抛异常，返回一个事件流，最后以 done 或 error 结束。
const streamFn = (_model: unknown, context: { messages: any[] }) => {
	const stream = createAssistantMessageEventStream();
	const message = replies.shift() ?? assistant([{ type: "text", text: "(没有更多回复)" }]);
	console.log(`${ms()}ms  [模型] 收到 ${context.messages.length} 条消息，返回 stopReason=${message.stopReason}`);
	queueMicrotask(() => {
		stream.push({ type: "start", partial: { ...message, content: [] } });
		stream.push({ type: "done", reason: message.stopReason, message });
		stream.end();
	});
	return stream;
};

// ---- 工具 ----
const sleep = (n: number) => new Promise((r) => setTimeout(r, n));
const tool = (name: string, delay: number) => ({
	name,
	label: name,
	description: `${name} tool`,
	parameters: Type.Object({}),
	...(scenario === "mixed" && name === "fast" ? { executionMode: "sequential" } : {}),
	async execute() {
		await sleep(delay);
		return { content: [{ type: "text", text: `${name} done` }], details: {} };
	},
});

// ---- steering 队列：第一次轮询返回空，工具执行完后返回一条插话 ----
let steerPolls = 0;
const getSteeringMessages = async () => {
	if (scenario !== "steer") return [];
	steerPolls++;
	console.log(`${ms()}ms  [steering] 第 ${steerPolls} 次轮询`);
	return steerPolls === 2 ? [{ role: "user", content: [{ type: "text", text: "插话：顺便看下测试" }], timestamp: Date.now() }] : [];
};

const config = {
	model,
	convertToLlm: (messages: any[]) => messages,
	toolExecution: scenario === "sequential" ? "sequential" : "parallel",
	getSteeringMessages,
	getFollowUpMessages: async () => [],
};

const context = { messages: [], tools: [tool("slow", 300), tool("fast", 50)] };
const prompt = { role: "user", content: [{ type: "text", text: "开始" }], timestamp: Date.now() };

function log(event: any) {
	let extra = "";
	if (event.message) {
		extra = ` (${event.message.role})`;
		if (event.message.role === "toolResult") extra += ` ${event.message.toolName} isError=${event.message.isError}`;
		if (event.message.role === "user") extra += ` ${JSON.stringify(event.message.content[0].text)}`;
	}
	if (event.toolName && !event.message) extra = ` ${event.toolName}`;
	if (event.type === "tool_execution_end") {
		extra += ` isError=${event.isError}`;
		if (event.isError) extra += ` ${JSON.stringify(event.result.content[0].text.slice(0, 60))}…`;
	}
	console.log(`${ms()}ms  ${event.type}${extra}`);
}

console.log(`场景：${scenario}${process.env.STREAM === "1" ? "（agentLoop + for await）" : "（runAgentLoop + await emit）"}`);
let newMessages: any[];
if (process.env.STREAM === "1") {
	// agentLoop：事件推进 EventStream 队列，循环不等消费者，打印顺序会落后于循环
	const stream = agentLoop([prompt], context, config, undefined, streamFn);
	for await (const event of stream) log(event);
	newMessages = await stream.result();
} else {
	// runAgentLoop：每个事件都 await emit 后循环才继续，Agent 类就是这样调用的
	newMessages = await runAgentLoop([prompt], context, config, async (event: any) => log(event), undefined, streamFn);
}
console.log(`结束：本次运行新增 ${newMessages.length} 条消息：${newMessages.map((m: any) => m.role).join(", ")}`);
