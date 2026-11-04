// M1 实验：从 ModelRuntime.streamSimple() 进入 pi-ai，观察一次请求的完整事件流。
//
// 用法（PI 指向 pi 仓库；PI_CODING_AGENT_DIR 指向一个 /tmp 下的新目录，不碰 ~/.pi）：
//   PI=/path/to/pi PI_CODING_AGENT_DIR=/tmp/pi-lab/m1-agent node --conditions=source --experimental-strip-types m1-runtime.ts [场景]
// 场景：
//   events       注册 faux provider，调用 runtime.streamSimple()，打印 provider 收到的上下文、每个事件、最终消息（默认）
//   unknown      模型的 provider 不存在：streamSimple 不抛异常，而是返回一个以 error 事件结束的流
//   live         同 events，但 faux 不在片段之间等待：消费者落后于生产者，读到的 partial 已经是最终值
//   eventstream  直接操作 AssistantMessageEventStream：不迭代也能拿到 result()，done 之后的事件被丢弃
// faux provider 用 tokenSize {min:1,max:1}，每个片段固定 4 个字符，所以输出是确定的（时间戳和 durationMs 除外）。
// events 场景设置 tokensPerSecond=1000，片段之间隔 1ms，消费者每次都能读到当时的 partial。
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
if (!process.env.PI_CODING_AGENT_DIR?.startsWith("/tmp/")) throw new Error("请把 PI_CODING_AGENT_DIR 设为 /tmp 下的目录");
// 先装上源码解析器，让 @earendil-works/* 指向 TypeScript 源码而不是 dist
await import(pathToFileURL(`${PI}/packages/coding-agent/src/experimental/source-resolver.ts`).href);
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const { ModelRuntime } = await load("packages/coding-agent/src/core/model-runtime.ts");
const ai = await load("packages/ai/src/index.ts");
const { fauxProvider, fauxAssistantMessage, fauxText, fauxToolCall, createAssistantMessageEventStream } = ai;

const scenario = process.argv[2] ?? "events";
const agentDir = process.env.PI_CODING_AGENT_DIR;
const short = (value: unknown, n = 160) => {
	const text = JSON.stringify(value);
	return text.length > n ? `${text.slice(0, n)}…` : text;
};

function describe(event: any): string {
	switch (event.type) {
		case "start":
			return `start          partial.content=${short(event.partial.content)} stopReason=${event.partial.stopReason}`;
		case "text_delta":
		case "thinking_delta":
			return `${event.type.padEnd(14)} [${event.contentIndex}] delta=${JSON.stringify(event.delta).padEnd(12)} partial 文本=${JSON.stringify(event.partial.content[event.contentIndex].text ?? event.partial.content[event.contentIndex].thinking)}`;
		case "toolcall_delta":
			return `toolcall_delta [${event.contentIndex}] delta=${JSON.stringify(event.delta).padEnd(12)} arguments=${short(event.partial.content[event.contentIndex].arguments)}`;
		case "text_end":
			return `text_end       [${event.contentIndex}] content=${JSON.stringify(event.content)}`;
		case "toolcall_end":
			return `toolcall_end   [${event.contentIndex}] toolCall=${short(event.toolCall)}`;
		case "done":
			return `done           reason=${event.reason}`;
		case "error":
			return `error          reason=${event.reason} errorMessage=${JSON.stringify(event.error.errorMessage)}`;
		default:
			return `${event.type.padEnd(14)} [${event.contentIndex}]`;
	}
}

async function createRuntime(tokensPerSecond?: number) {
	const runtime = await ModelRuntime.create({
		authPath: `${agentDir}/auth.json`,
		modelsPath: null,
		refreshOnCreate: false,
	});
	const faux = fauxProvider({ provider: "faux", api: "faux-api", tokenSize: { min: 1, max: 1 }, tokensPerSecond });
	// 包一层，记录 provider 实际收到的上下文（TranscriptContext）和选项
	const providerStreamSimple = (model: any, context: any, options: any) => {
		console.log("[provider] 收到 TranscriptContext.messages：");
		for (const m of context.messages) console.log(`  ${short(m, 150)}`);
		console.log(`[provider] 收到 options 的键：${Object.keys(options ?? {}).sort().join(", ")}`);
		console.log(`[provider] options.apiKey=${JSON.stringify(options?.apiKey)} options.reasoning=${JSON.stringify(options?.reasoning)}`);
		return faux.provider.streamSimple(model, context, options);
	};
	// 与 lab/faux-ext.ts 相同的注册方式：扩展调用 pi.registerProvider() 最终就是调用这个方法
	runtime.registerProvider("faux", {
		api: faux.api,
		baseUrl: faux.getModel().baseUrl,
		apiKey: "faux-key",
		models: faux.models,
		streamSimple: providerStreamSimple,
	});
	return { runtime, faux };
}

if (scenario === "events" || scenario === "live") {
	const { runtime, faux } = await createRuntime(scenario === "events" ? 1000 : undefined);
	faux.setResponses([
		fauxAssistantMessage([fauxText("先读文件。"), fauxToolCall("read", { path: "README.md", limit: 20 }, { id: "call_1" })], {
			stopReason: "toolUse",
		}),
	]);
	const model = runtime.getModel("faux", "faux-1");
	console.log(`[runtime] getModel("faux","faux-1")：${short(model, 400)}`);
	const context = {
		systemPrompt: "You are a test agent.",
		messages: [{ role: "user", content: "读 README.md", timestamp: 1 }],
		tools: [{ name: "read", description: "Read a file", parameters: { type: "object", properties: { path: { type: "string" } } } }],
	};
	console.log(`[调用方] Context 的键：${Object.keys(context).join(", ")}`);
	const stream = runtime.streamSimple(model, context, { sessionId: "s1" });
	console.log(`[调用方] streamSimple 已同步返回：${stream.constructor.name}`);
	for await (const event of stream) console.log(`[事件] ${describe(event)}`);
	const message = await stream.result();
	console.log(`[result] ${JSON.stringify({ ...message, timestamp: "<ts>", durationMs: "<ms>" })}`);
} else if (scenario === "unknown") {
	const { runtime } = await createRuntime();
	const model = { ...runtime.getModel("faux", "faux-1"), provider: "nope" };
	let stream;
	try {
		stream = runtime.streamSimple(model, { messages: [] });
		console.log("[调用方] streamSimple 没有抛异常，返回了一个流");
	} catch (error) {
		console.log(`[调用方] streamSimple 同步抛出：${error}`);
		process.exit(0);
	}
	for await (const event of stream) console.log(`[事件] ${describe(event)}`);
	const message = await stream.result();
	console.log(`[result] stopReason=${message.stopReason} errorMessage=${JSON.stringify(message.errorMessage)} content=${JSON.stringify(message.content)}`);
} else if (scenario === "eventstream") {
	const stream = createAssistantMessageEventStream();
	const message = fauxAssistantMessage("hi");
	stream.push({ type: "start", partial: { ...message, content: [] } });
	stream.push({ type: "text_start", contentIndex: 0, partial: message });
	stream.push({ type: "done", reason: "stop", message });
	stream.push({ type: "text_delta", contentIndex: 0, delta: "迟到的事件", partial: message });
	stream.end();
	const result = await stream.result();
	console.log(`[result] 还没有人迭代，result() 已经 resolve：stopReason=${result.stopReason} durationMs 是数字=${typeof result.durationMs === "number"}`);
	const seen: string[] = [];
	for await (const event of stream) seen.push(event.type);
	console.log(`[for await] 之后再迭代，拿到排队的事件：${seen.join(", ")}`);
	const again: string[] = [];
	for await (const event of stream) again.push(event.type);
	console.log(`[for await] 第二次迭代拿到 ${again.length} 个事件（队列已被取空）`);
} else {
	throw new Error(`未知场景：${scenario}`);
}
