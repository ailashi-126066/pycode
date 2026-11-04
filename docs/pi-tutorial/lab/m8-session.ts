// M8 实验：用 SDK 入口 createAgentSession() 创建一个 AgentSession，模型换成 faux 假模型，
// 打印 AgentSession 安装的 hook、AgentSessionEvent 序列、状态标志和会话文件。
//
// 用法（PI 指向 pi 仓库；WORK、PI_CODING_AGENT_DIR 请给全新的 /tmp 目录，不要指向 ~/.pi）：
//   PI=/path/to/pi WORK=/tmp/pi-lab/m8-work-1 PI_CODING_AGENT_DIR=/tmp/pi-lab/m8-agent-1 \
//     node --experimental-strip-types m8-session.ts [场景]
// 场景：
//   dump        （默认）主线场景：第 1 次回复调用 read，第 2 次回复总结。
//               打印构造后的 Agent 状态和 hook、每个事件、message_end 时会话文件里的条目数
//   projection  prompt 之前往 agent.state.messages 里直接塞一条 user 消息，
//               看模型能不能收到它（prepareRequest 会用会话文件重建上下文）
//   custom      prompt 之前用 session.sendCustomMessage() 发一条不触发轮次的自定义消息，看模型能不能收到它
//   retry       第 1 次请求抛出 "503 overloaded" 错误，看自动重试的事件和 context_edit 条目
//   retry-fail  maxRetries=1，前两次请求都失败，看重试用完后的事件
//   steer       在第 1 次请求里调用 session.steer()，看 queue_update 和插话进入上下文的时机
//   compact     把 compaction.reserveTokens 设得很大，看 _checkCompaction 在哪里触发阈值压缩
//   abort       在第 1 次请求里调用 session.abort()，看 aborted 回复和 agent_settled
//   services    用 createAgentSessionServices + createAgentSessionFromServices 创建会话，打印对象形状
// 脚本先加载 pi 仓库自带的 source-resolver，再用包名导入 pi 的 TypeScript 源码，不需要构建。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const WORK = process.env.WORK ?? "/tmp/pi-lab/m8-work";
const AGENT_DIR = process.env.PI_CODING_AGENT_DIR ?? "/tmp/pi-lab/m8-agent";
if (AGENT_DIR.includes("/.pi")) throw new Error("PI_CODING_AGENT_DIR 不能指向 ~/.pi");
await import(pathToFileURL(`${PI}/packages/coding-agent/src/experimental/source-resolver.ts`).href);
const { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } = await import("@earendil-works/pi-ai");
const {
	createAgentSession,
	createAgentSessionFromServices,
	createAgentSessionServices,
	ModelRuntime,
	SessionManager,
	SettingsManager,
} = await import("@earendil-works/pi-coding-agent");

const scenario = process.argv[2] ?? "dump";
mkdirSync(WORK, { recursive: true });
mkdirSync(AGENT_DIR, { recursive: true });

// services 场景：走 M9 用的那条路径。先建 services（这里加载 lab/faux-ext.ts 扩展来注册 faux 模型），
// 再用 createAgentSessionFromServices 创建会话，只打印对象形状，不发 prompt
if (scenario === "services") {
	const services = await createAgentSessionServices({
		cwd: WORK,
		agentDir: AGENT_DIR,
		resourceLoaderOptions: { additionalExtensionPaths: [new URL("./faux-ext.ts", import.meta.url).pathname] },
	});
	console.log(`[AgentSessionServices] 字段：${Object.keys(services).join(", ")}`);
	console.log(`[AgentSessionServices] cwd=${services.cwd.replace(WORK, "$WORK")} agentDir=${services.agentDir.replace(AGENT_DIR, "$PI_CODING_AGENT_DIR")}`);
	console.log(`[AgentSessionServices] diagnostics=${JSON.stringify(services.diagnostics)}`);
	console.log(`[AgentSessionServices] 扩展：${services.resourceLoader.getExtensions().extensions.map((e: any) => e.path.split("/").pop()).join(", ")}`);
	console.log(`[AgentSessionServices] faux 模型已注册=${services.modelRuntime.getModel("faux", "faux-1") !== undefined}`);
	const result = await createAgentSessionFromServices({
		services,
		sessionManager: SessionManager.inMemory(WORK),
		model: services.modelRuntime.getModel("faux", "faux-1"),
	});
	console.log(`[CreateAgentSessionResult] 字段：${Object.keys(result).join(", ")}`);
	console.log(`[AgentSession] model=${result.session.model.provider}/${result.session.model.id} tools=${JSON.stringify(result.session.getActiveToolNames())}`);
	result.session.dispose();
	process.exit(0);
}
if (!existsSync(join(WORK, "README.md"))) {
	writeFileSync(join(WORK, "README.md"), "# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n");
}
const t0 = Date.now();
const log = (text: string) => console.log(`${String(Date.now() - t0).padStart(4)}ms  ${text}`);
const roles = (messages: any[]) => messages.map((m) => m.role).join(", ");

// 1. 假模型：注册成 ModelRuntime 里的 faux provider
const faux = fauxProvider({ provider: "faux", api: "faux-api" });
let session: any;
let requests = 0;
const traced = (reply: (context: any) => any) => (context: any) => {
	requests++;
	log(`[模型] 第 ${requests} 次请求，收到 ${context.messages.length} 条 LLM 消息：${roles(context.messages)}`);
	return reply(context);
};
const readCall = () =>
	fauxAssistantMessage([fauxText("我先读一下 README。"), fauxToolCall("read", { path: "README.md" }, { id: "call_1" })], {
		stopReason: "toolUse",
	});
const summary = () => fauxAssistantMessage("这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。");
const overloaded = () => {
	throw new Error("503 Service Unavailable: overloaded");
};
const responses: Record<string, any[]> = {
	dump: [readCall, summary],
	projection: [readCall, summary],
	custom: [readCall, summary],
	retry: [overloaded, readCall, summary],
	"retry-fail": [overloaded, overloaded],
	steer: [
		() => {
			void session.steer("插话：顺便看下测试");
			log("[steer] 调用 session.steer()");
			return readCall();
		},
		summary,
	],
	// 第 2、4 次请求是压缩时生成摘要的请求，不是对话请求
	compact: [
		readCall,
		() => fauxAssistantMessage("[摘要 1] 用户让 agent 读 README.md。"),
		// 等 5ms 再生成回复，让回复的 timestamp 晚于压缩条目。
		// 否则两者可能落在同一毫秒，_checkCompaction 会把这条回复当成"压缩之前的消息"而跳过检查
		async () => {
			await new Promise((resolve) => setTimeout(resolve, 5));
			return summary();
		},
		() => fauxAssistantMessage("[摘要 2] agent 读了 README.md 并给出总结。"),
	],
	abort: [
		() => {
			void session.abort();
			log("[abort] 调用 session.abort()");
			return readCall();
		},
	],
};
faux.setResponses((responses[scenario] ?? responses.dump).map(traced));

const modelRuntime = await ModelRuntime.create({
	authPath: join(AGENT_DIR, "auth.json"),
	modelsPath: join(AGENT_DIR, "models.json"),
});
modelRuntime.registerProvider("faux", {
	api: faux.api,
	baseUrl: faux.getModel().baseUrl,
	apiKey: "faux-key",
	models: faux.models,
	streamSimple: faux.provider.streamSimple,
} as any);

// 2. 设置：重试等待缩短到 10ms，便于观察
const settingsManager = SettingsManager.inMemory({
	retry: { enabled: true, maxRetries: scenario === "retry-fail" ? 1 : 3, baseDelayMs: 10 },
	// compact 场景：faux-1 的 contextWindow 是 128000，预留 127000 token，超过 1000 token 就触发阈值压缩
	...(scenario === "compact" ? { compaction: { enabled: true, reserveTokens: 127000, keepRecentTokens: 1 } } : {}),
});
const sessionManager = SessionManager.create(WORK, join(AGENT_DIR, "sessions"));

// 3. 入口
const { session: created } = await createAgentSession({
	cwd: WORK,
	agentDir: AGENT_DIR,
	modelRuntime,
	model: modelRuntime.getModel("faux", "faux-1"),
	settingsManager,
	sessionManager,
});
session = created;
const agent = session.agent;

const flags = () =>
	`isStreaming=${session.isStreaming} isCompacting=${session.isCompacting} isRetrying=${session.isRetrying} isBashRunning=${session.isBashRunning}`;
const entryCount = () => sessionManager.getEntries().length;

if (scenario === "dump") {
	const hooks = [
		"convertToLlm",
		"transformContext",
		"prepareRequest",
		"prepareNextTurnWithContext",
		"finishTurn",
		"beforeToolCall",
		"afterToolCall",
	];
	console.log(`[Agent] 已设置的 hook：${hooks.filter((k) => typeof agent[k] === "function").join(", ")}`);
	console.log(`[Agent] state.systemPrompt=${JSON.stringify(agent.state.systemPrompt)}`);
	console.log(`[Agent] state.tools=${JSON.stringify(agent.state.tools.map((t: any) => t.name))}`);
	console.log(`[Agent] state.messages.length=${agent.state.messages.length}`);
	console.log(`[AgentSession] ${flags()} isIdle=${session.isIdle}`);
	console.log(
		`[SessionManager] 内存中的条目：${sessionManager
			.getEntries()
			.map((e: any) => e.type)
			.join(", ")}；会话文件已存在=${existsSync(sessionManager.getSessionFile())}`,
	);
}

if (scenario === "projection") {
	agent.state.messages = [
		...agent.state.messages,
		{ role: "user", content: [{ type: "text", text: "这条消息只在 agent.state 里" }], timestamp: Date.now() },
	];
	log(`[projection] prompt 前 agent.state.messages=${roles(agent.state.messages)}`);
	const original = agent.prepareRequest;
	agent.prepareRequest = async (request: any, signal: any) => {
		log(`[prepareRequest] 循环传入的 context.messages=${roles(request.context.messages)}`);
		const result = await original(request, signal);
		log(`[prepareRequest] 返回的 context.messages=${roles(result.context.messages)}`);
		return result;
	};
}

if (scenario === "custom") {
	await session.sendCustomMessage({ customType: "note", content: "这条备注通过 sendCustomMessage 写进会话文件", display: false });
	log(`[custom] prompt 前 agent.state.messages=${roles(agent.state.messages)} 会话条目=${sessionManager.getEntries().map((e: any) => e.type).join(", ")}`);
}

// 4. 订阅 AgentSessionEvent
let updates = 0;
session.subscribe((event: any) => {
	if (event.type === "message_update") {
		updates++;
		return;
	}
	if (updates > 0) {
		log(`message_update x${updates}`);
		updates = 0;
	}
	let label = event.type;
	if (event.message) label += ` (${event.message.role})`;
	if (event.toolName) label += ` ${event.toolName}`;
	if (event.type === "message_end" && event.message.role === "assistant") label += ` stopReason=${event.message.stopReason}`;
	if (event.type === "message_end" && event.message.errorMessage) label += ` errorMessage=${JSON.stringify(event.message.errorMessage)}`;
	if (event.type === "message_end") label += `  [此刻会话条目数=${entryCount()}]`;
	if (event.type === "agent_end") label += ` willRetry=${event.willRetry} messages=[${roles(event.messages)}]`;
	if (event.type === "agent_settled") label += ` aborted=${event.aborted}`;
	if (event.type === "queue_update") label += ` steering=${JSON.stringify(event.steering)} followUp=${JSON.stringify(event.followUp)}`;
	if (event.type === "auto_retry_start") label += ` attempt=${event.attempt}/${event.maxAttempts} delayMs=${event.delayMs} errorMessage=${JSON.stringify(event.errorMessage)}`;
	if (event.type === "auto_retry_end") label += ` success=${event.success} attempt=${event.attempt}${event.finalError ? ` finalError=${JSON.stringify(event.finalError)}` : ""}`;
	if (event.type === "entry_appended") label += ` ${event.entry.type}${event.entry.type === "context_edit" ? ` targetId=${event.entry.targetId.slice(0, 8)} replacement=${JSON.stringify(event.entry.replacement)}` : ""}`;
	if (event.type === "compaction_start") label += ` reason=${event.reason}`;
	if (event.type === "compaction_end") label += ` reason=${event.reason} willRetry=${event.willRetry} aborted=${event.aborted} tokensBefore=${event.result?.tokensBefore}${event.errorMessage ? ` errorMessage=${JSON.stringify(event.errorMessage)}` : ""}`;
	if (["agent_start", "agent_end", "agent_settled", "auto_retry_start", "compaction_start"].includes(event.type)) label += `  {${flags()}}`;
	log(label);
	// isRetrying 在 auto_retry_start 发出之后才变成 true（等待期间），隔 5ms 再看一次
	if (event.type === "auto_retry_start") setTimeout(() => log(`[5ms 后] {${flags()}}`), 5);
});

console.log(`场景：${scenario}`);
try {
	await session.prompt("读 README.md，总结成三句话");
} catch (error) {
	log(`[prompt 抛错] ${(error as Error).message}`);
}
log(`[prompt 返回] ${flags()} isIdle=${session.isIdle} 模型请求次数=${requests}`);
console.log(`[AgentState] messages=${roles(agent.state.messages)}`);
if (scenario === "compact") {
	const usages = agent.state.messages.filter((m: any) => m.role === "assistant").map((m: any) => m.usage.totalTokens);
	console.log(`[usage] 各条 assistant 消息的 usage.totalTokens=${JSON.stringify(usages)}，阈值=128000-127000=1000`);
}
const file = sessionManager.getSessionFile();
console.log(`[会话文件] ${file.replace(AGENT_DIR, "$PI_CODING_AGENT_DIR")}`);
for (const line of readFileSync(file, "utf8").trim().split("\n")) {
	const entry = JSON.parse(line);
	let text = entry.type.padEnd(22);
	if (entry.type === "message") {
		const m = entry.message;
		text += `${m.role.padEnd(10)} ${JSON.stringify(m.content).slice(0, 60)}`;
		if (m.stopReason) text += ` stop=${m.stopReason}`;
	} else if (entry.type === "compaction") {
		text += `firstKeptEntryId=${entry.firstKeptEntryId.slice(0, 8)} tokensBefore=${entry.tokensBefore} summary=${JSON.stringify(entry.summary).slice(0, 40)}`;
	} else if (entry.type === "context_edit") {
		text += `targetId=${entry.targetId.slice(0, 8)} replacement=${JSON.stringify(entry.replacement)}`;
	}
	console.log(`  ${text}  id=${String(entry.id).slice(0, 8)}`);
}
session.dispose();
