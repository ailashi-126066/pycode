// M2 实验：通过 Agent 类（而不是直接调 runAgentLoop）运行一次 agent 循环，
// 打印 M2 核心数据结构的真实实例和运行生命周期。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m2-agent.ts [场景]
// 场景：
//   dump   （默认）打印 AgentState、AgentContext、AgentLoopConfig、AgentToolResult、
//          AgentToolCallOutcome 的实例；transcript 里放一条自定义 note 消息，
//          看它是否被 convertToLlm 过滤；agent_end 监听器里等 100ms，观察 isStreaming
//   block  beforeToolCall 拦截工具调用并设置 terminate: true，观察循环不再请求模型
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const { Agent } = await load("packages/agent/src/index.ts");
const { createAssistantMessageEventStream } = await load("packages/ai/src/index.ts");
const { Type } = await import(pathToFileURL(`${PI}/node_modules/typebox/build/index.mjs`).href);

// 自定义消息类型。写 TypeScript 应用时用声明合并把它加进 AgentMessage：
//   declare module "@earendil-works/pi-agent-core" {
//     interface CustomAgentMessages { note: { role: "note"; text: string; timestamp: number } }
//   }
// 本文件用 --experimental-strip-types 运行，不做类型检查，所以这里只在运行时放一条 note 消息。

const scenario = process.argv[2] ?? "dump";
const show = (label: string, value: unknown) => console.log(`${label} ${JSON.stringify(value)}`);

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
	timestamp: 1700000000000,
});
const replies = [
	assistant([{ type: "toolCall", id: "c1", name: "greet", arguments: { name: "pi" } }], "toolUse"),
	assistant([{ type: "text", text: "完成" }]),
];

let requests = 0;
const streamFn = (_model: unknown, context: { messages: any[] }) => {
	requests++;
	console.log(`[模型] 第 ${requests} 次请求，收到的 LLM 消息 role：${context.messages.map((m) => m.role).join(", ")}`);
	const stream = createAssistantMessageEventStream();
	const message = replies.shift() ?? assistant([{ type: "text", text: "(没有更多回复)" }]);
	queueMicrotask(() => {
		stream.push({ type: "start", partial: { ...message, content: [] } });
		stream.push({ type: "done", reason: message.stopReason, message });
		stream.end();
	});
	return stream;
};

const greet = {
	name: "greet",
	label: "Greet",
	description: "Say hello",
	parameters: Type.Object({ name: Type.String() }),
	async execute(_id: string, params: { name: string }) {
		return { content: [{ type: "text", text: `hello ${params.name}` }], details: { greeted: params.name } };
	},
};

const agent = new Agent({
	initialState: {
		systemPrompt: "You are a test agent.",
		model,
		tools: [greet],
		messages: [{ role: "note", text: "只给 UI 看的备注", timestamp: 1700000000000 }],
	},
	streamFn,
	prepareRequest: async (request: any) => {
		if (requests > 0) return;
		show("[prepareRequest] AgentContext.messages role：", request.context.messages.map((m: any) => m.role));
		show("[prepareRequest] AgentContext.tools：", request.context.tools.map((t: any) => t.name));
		show("[prepareRequest] thinkingLevel：", request.thinkingLevel);
	},
	beforeToolCall: async ({ toolCall, args }: any) => {
		show("[beforeToolCall] toolCall：", toolCall);
		show("[beforeToolCall] args：", args);
		if (scenario === "block") return { block: true, reason: "被 beforeToolCall 拦截", terminate: true };
		return undefined;
	},
	afterToolCall: async ({ result, isError }: any) => {
		show("[afterToolCall] AgentToolResult：", result);
		show("[afterToolCall] isError：", isError);
		return undefined;
	},
});

if (scenario === "dump") {
	const config = (agent as any).createLoopConfig();
	const hooks = ["convertToLlm", "transformContext", "getApiKey", "prepareNextTurn", "prepareRequest", "finishTurn", "getSteeringMessages", "getFollowUpMessages", "beforeToolCall", "afterToolCall"];
	console.log(`[AgentLoopConfig] 已设置的回调：${hooks.filter((k) => typeof config[k] === "function").join(", ")}`);
	console.log(`[AgentLoopConfig] 未设置的回调：${hooks.filter((k) => typeof config[k] !== "function").join(", ")}`);
	show("[AgentLoopConfig] toolExecution：", config.toolExecution);
	show("[AgentState] 运行前 messages：", agent.state.messages);
	show("[AgentState] 运行前 systemPrompt：", agent.state.systemPrompt);
}

agent.subscribe(async (event: any) => {
	if (event.type === "tool_execution_start" || event.type === "tool_execution_end") {
		console.log(`[事件] ${event.type} pendingToolCalls=${JSON.stringify([...agent.state.pendingToolCalls])}`);
	}
	if (event.type === "tool_execution_end") show("[事件] tool_execution_end：", event);
	if (event.type === "agent_end") {
		console.log(`[事件] agent_end，监听器开始等待 100ms，isStreaming=${agent.state.isStreaming}`);
		await new Promise((r) => setTimeout(r, 100));
		console.log(`[事件] agent_end 监听器结束，isStreaming=${agent.state.isStreaming}`);
	}
});

await agent.prompt("打个招呼");
console.log(`[prompt 返回] isStreaming=${agent.state.isStreaming} 模型请求次数=${requests}`);
show("[AgentState] 运行后 messages role：", agent.state.messages.map((m: any) => m.role));
const toolResult = agent.state.messages.find((m: any) => m.role === "toolResult");
show("[AgentState] toolResult 消息：", toolResult);
