// M1 实验：不联网运行真实的 Anthropic 适配器（packages/ai/src/api/anthropic-messages.ts）。
// 通过 options.fetch 注入一个假的 fetch：它记录请求体，并返回一段预先写好的 SSE 响应。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m1-anthropic.ts [场景]
// 场景：
//   sse        一段 thinking + text + tool_use 的 SSE，打印原始 SSE 事件、统一事件、最终消息（默认）
//   request    跨 provider 的历史 + 对话中途的 system 消息，分别发给两个模型，打印请求体的关键部分
//   thinking   不同思考级别映射成什么请求参数（预算型模型 vs 自适应模型）
//   cache      cacheRetention 和 cache_control 的位置
//   error      四种失败：SSE 缺少 message_stop、HTTP 400、直接调用 api 模块时没有 API key（同步抛出）、经 anthropicProvider() 调用时没有 API key（error 事件）
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
await import(pathToFileURL(`${PI}/packages/coding-agent/src/experimental/source-resolver.ts`).href);
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const anthropic = await load("packages/ai/src/api/anthropic-messages.ts");
const { normalizeContext } = await load("packages/ai/src/utils/transcript.ts");
const { anthropicProvider } = await load("packages/ai/src/providers/anthropic.ts");
const { AssistantMessageEventStream } = await load("packages/ai/src/utils/event-stream.ts");

const scenario = process.argv[2] ?? "sse";
const catalog = anthropicProvider().getModels();
const getModel = (id: string) => {
	const model = catalog.find((m: any) => m.id === id);
	if (!model) throw new Error(`catalog 里没有 ${id}`);
	return model;
};

// ---- 假的 fetch ----
type Sse = [event: string, data: object];
const sseText = (events: Sse[]) => events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
let lastBody: any;
let lastBeta: string | null = null;
function fakeFetch(events: Sse[] | { status: number; body: object }) {
	return async (_url: string, init: { body: string; headers: HeadersInit }) => {
		lastBody = JSON.parse(init.body);
		// SDK 把 params.betas 从请求体移到 anthropic-beta 请求头
		lastBeta = new Headers(init.headers).get("anthropic-beta");
		if (!Array.isArray(events)) {
			return new Response(JSON.stringify(events.body), { status: events.status, headers: { "content-type": "application/json" } });
		}
		const text = sseText(events);
		const bytes = new TextEncoder().encode(text);
		// 故意按 37 字节切块，模拟网络把一行 SSE 拆成多段
		const body = new ReadableStream({
			start(controller) {
				for (let i = 0; i < bytes.length; i += 37) controller.enqueue(bytes.slice(i, i + 37));
				controller.close();
			},
		});
		return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
	};
}

const short = (value: unknown, n = 150) => {
	const text = JSON.stringify(value);
	return text.length > n ? `${text.slice(0, n)}…` : text;
};
function describe(event: any): string {
	const block = event.partial?.content?.[event.contentIndex];
	switch (event.type) {
		case "start":
			return `start           responseId=${event.partial.responseId} usage.input=${event.partial.usage.input}`;
		case "thinking_delta":
		case "text_delta":
			return `${event.type.padEnd(15)} [${event.contentIndex}] delta=${JSON.stringify(event.delta)}`;
		case "toolcall_start":
			return `toolcall_start  [${event.contentIndex}] id=${block.id} name=${block.name} arguments=${short(block.arguments)}`;
		case "toolcall_delta":
			return `toolcall_delta  [${event.contentIndex}] delta=${JSON.stringify(event.delta).padEnd(16)} arguments=${short(block.arguments)}`;
		case "thinking_end":
			return `thinking_end    [${event.contentIndex}] signature=${JSON.stringify(block.thinkingSignature)}`;
		case "text_end":
			return `text_end        [${event.contentIndex}] content=${JSON.stringify(event.content)}`;
		case "toolcall_end":
			return `toolcall_end    [${event.contentIndex}] toolCall=${short(event.toolCall)}`;
		case "done":
			return `done            reason=${event.reason}`;
		case "error":
			return `error           reason=${event.reason} errorMessage=${JSON.stringify(event.error.errorMessage.slice(0, 120))}`;
		default:
			return `${event.type.padEnd(15)} [${event.contentIndex}]`;
	}
}
// 在 push() 发生的那一刻打印事件。partial 是同一个对象，会继续被修改，
// 用 for await 消费时读到的可能已经是后来的状态（见 m1-runtime.ts live 场景）。
let printEvents = false;
const originalPush = AssistantMessageEventStream.prototype.push;
AssistantMessageEventStream.prototype.push = function (event: any) {
	if (printEvents) console.log(`[事件] ${describe(event)}`);
	return originalPush.call(this, event);
};
async function drain(stream: any) {
	printEvents = true;
	try {
		return await stream.result();
	} finally {
		printEvents = false;
	}
}

const sonnet = getModel("claude-sonnet-4-5");
const opus = getModel("claude-opus-4-8");
const simpleContext = normalizeContext({
	systemPrompt: "You are a test agent.",
	messages: [{ role: "user", content: "读 README.md", timestamp: 1 }],
	tools: [{ name: "read", description: "Read a file", parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } }],
});
const usage = (extra: object = {}) => ({ input_tokens: 120, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, ...extra });
const sseScript: Sse[] = [
	["message_start", { type: "message_start", message: { id: "msg_01", type: "message", role: "assistant", model: "claude-sonnet-4-5", content: [], stop_reason: null, usage: usage({ cache_creation_input_tokens: 1000 }) } }],
	["ping", { type: "ping" }],
	["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "thinking", thinking: "", signature: "" } }],
	["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: "需要先读文件。" } }],
	["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "signature_delta", signature: "sig-abc" } }],
	["content_block_stop", { type: "content_block_stop", index: 0 }],
	["content_block_start", { type: "content_block_start", index: 1, content_block: { type: "text", text: "" } }],
	["content_block_delta", { type: "content_block_delta", index: 1, delta: { type: "text_delta", text: "我先读" } }],
	["content_block_delta", { type: "content_block_delta", index: 1, delta: { type: "text_delta", text: "一下。" } }],
	["content_block_stop", { type: "content_block_stop", index: 1 }],
	["content_block_start", { type: "content_block_start", index: 2, content_block: { type: "tool_use", id: "toolu_01", name: "read", input: {} } }],
	["content_block_delta", { type: "content_block_delta", index: 2, delta: { type: "input_json_delta", partial_json: '{"pa' } }],
	["content_block_delta", { type: "content_block_delta", index: 2, delta: { type: "input_json_delta", partial_json: 'th": "READ' } }],
	["content_block_delta", { type: "content_block_delta", index: 2, delta: { type: "input_json_delta", partial_json: 'ME.md"}' } }],
	["content_block_stop", { type: "content_block_stop", index: 2 }],
	["message_delta", { type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 42 } }],
	["message_stop", { type: "message_stop" }],
];

if (scenario === "sse") {
	console.log("[SSE] 服务器发出的事件：" + sseScript.map(([e, d]: any) => (d.delta?.type ? `${e}(${d.delta.type ?? ""})` : e)).join(" "));
	const stream = anthropic.streamSimple(sonnet, simpleContext, { apiKey: "sk-test", fetch: fakeFetch(sseScript) });
	const message = await drain(stream);
	console.log(`[result] ${JSON.stringify({ ...message, timestamp: "<ts>", durationMs: "<ms>" })}`);
} else if (scenario === "request") {
	// 一段跨 provider 的历史：第 1 轮由 OpenAI Responses 模型回答（带 thinking 和 OpenAI 风格的长工具调用 id），
	// 之后插入一条中途的 system 消息（更新 rules 段落、新增 grep 工具），再由 Anthropic 模型继续。
	const history = normalizeContext({
		messages: [
			{ role: "system", content: "You are a test agent.", sections: { rules: "<rules>Be brief.</rules>" }, toolsAdded: simpleContext.messages[0].toolsAdded, timestamp: 0 },
			{ role: "user", content: "读 README.md", timestamp: 1 },
			{
				role: "assistant",
				content: [
					{ type: "thinking", thinking: "user wants the file", thinkingSignature: "{\"id\":\"rs_1\",\"encrypted_content\":\"…\"}" },
					{ type: "text", text: "好的。", textSignature: "msg_1" },
					{ type: "toolCall", id: "call_1|fc_0123456789abcdef", name: "read", arguments: { path: "README.md" } },
				],
				api: "openai-responses",
				provider: "openai",
				model: "gpt-5",
				usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
				stopReason: "toolUse",
				timestamp: 2,
			},
			{ role: "toolResult", toolCallId: "call_1|fc_0123456789abcdef", toolName: "read", content: [{ type: "text", text: "# Demo" }], isError: false, timestamp: 3 },
			{
				role: "system",
				content: "",
				sections: { rules: "<rules>Answer in English.</rules>" },
				toolsAdded: [{ name: "grep", description: "Search files", parameters: { type: "object", properties: { pattern: { type: "string" } } } }],
				timestamp: 4,
			},
			{ role: "user", content: "总结一下", timestamp: 5 },
		],
	});
	for (const model of [sonnet, opus]) {
		const stream = anthropic.streamSimple(model, history, { apiKey: "sk-test", fetch: fakeFetch(sseScript) });
		await stream.result();
		console.log(`=== ${model.id}（supportsMidConvoSystemMessages=${model.compat?.supportsMidConvoSystemMessages ?? false}）`);
		console.log(`system: ${JSON.stringify(lastBody.system.map((b: any) => b.text))}`);
		for (const m of lastBody.messages) {
			const blocks = typeof m.content === "string" ? [{ type: "text", text: m.content }] : m.content;
			console.log(
				`  ${m.role.padEnd(9)} ${blocks
					.map((b: any) => {
						if (b.type === "text") return `text(${JSON.stringify(b.text.slice(0, 60))})`;
						if (b.type === "tool_use") return `tool_use(id=${b.id})`;
						if (b.type === "tool_result") return `tool_result(tool_use_id=${b.tool_use_id})`;
						if (b.type === "tool_addition") return `tool_addition(${b.tool.definition.name})`;
						return b.type;
					})
					.join(" ")}`,
			);
		}
		console.log(`tools: ${lastBody.tools.map((t: any) => t.name).join(", ")}`);
		console.log(`anthropic-beta: ${lastBeta}`);
	}
} else if (scenario === "thinking") {
	const fable = getModel("claude-opus-5");
	for (const model of [sonnet, opus, fable]) {
		console.log(`=== ${model.id} maxTokens=${model.maxTokens} forceAdaptiveThinking=${model.compat?.forceAdaptiveThinking ?? false} supportsMidConvoEffort=${model.compat?.supportsMidConvoEffort ?? false} thinkingLevelMap=${JSON.stringify(model.thinkingLevelMap ?? {})}`);
		for (const reasoning of [undefined, "minimal", "medium", "high", "xhigh"]) {
			const stream = anthropic.streamSimple(model, simpleContext, { apiKey: "sk-test", fetch: fakeFetch(sseScript), reasoning, maxTokens: 32000 });
			const result = await stream.result();
			const effort = lastBody.messages.filter((m: any) => m.output_config).map((m: any) => m.output_config.effort);
			console.log(
				`  reasoning=${String(reasoning).padEnd(9)} max_tokens=${String(lastBody.max_tokens).padEnd(6)} thinking=${JSON.stringify(lastBody.thinking)} output_config=${JSON.stringify(lastBody.output_config)}${effort.length ? ` effort 标记=${JSON.stringify(effort)}` : ""}${result.providerThinkingLevel ? ` providerThinkingLevel=${result.providerThinkingLevel}` : ""}`,
			);
		}
	}
} else if (scenario === "cache") {
	for (const cacheRetention of [undefined, "long", "none"]) {
		const stream = anthropic.streamSimple(sonnet, simpleContext, { apiKey: "sk-test", fetch: fakeFetch(sseScript), cacheRetention, sessionId: "s1" });
		const result = await stream.result();
		const marks: string[] = [];
		lastBody.system?.forEach((b: any, i: number) => b.cache_control && marks.push(`system[${i}]`));
		lastBody.tools?.forEach((t: any) => t.cache_control && marks.push(`tools.${t.name}`));
		lastBody.messages.forEach((m: any, i: number) =>
			Array.isArray(m.content) && m.content.forEach((b: any, j: number) => b.cache_control && marks.push(`messages[${i}].content[${j}]`)),
		);
		const controls = new Set<string>();
		JSON.stringify(lastBody, (k, v) => (k === "cache_control" && controls.add(JSON.stringify(v)), v));
		console.log(`cacheRetention=${String(cacheRetention).padEnd(9)} cache_control 位置=${JSON.stringify(marks)} 取值=${[...controls].join(" ") || "-"}`);
		if (cacheRetention === undefined) console.log(`  usage（来自 message_start/message_delta）：${JSON.stringify(result.usage)}`);
	}
} else if (scenario === "error") {
	console.log("--- 1. SSE 在 message_stop 之前断开");
	const truncated = sseScript.slice(0, 9);
	await drain(anthropic.streamSimple(sonnet, simpleContext, { apiKey: "sk-test", fetch: fakeFetch(truncated) }));
	console.log("--- 2. 服务器返回 HTTP 400");
	const r2 = await drain(
		anthropic.streamSimple(sonnet, simpleContext, {
			apiKey: "sk-test",
			fetch: fakeFetch({ status: 400, body: { type: "error", error: { type: "invalid_request_error", message: "max_tokens: too large" } } }),
		}),
	);
	console.log(`[result] stopReason=${r2.stopReason} content=${JSON.stringify(r2.content)}`);
	console.log("--- 3. 直接调用 api 模块的 streamSimple，没有 API key");
	try {
		anthropic.streamSimple(sonnet, simpleContext, { fetch: fakeFetch(sseScript) });
		console.log("没有抛异常");
	} catch (error) {
		console.log(`同步抛出：${(error as Error).message}`);
	}
	console.log("--- 4. 同样没有 API key，但经过 anthropicProvider()（lazyApi 包了一层 lazyStream）");
	const provider = anthropicProvider();
	await drain(provider.streamSimple(sonnet, simpleContext, { fetch: fakeFetch(sseScript) }));
} else {
	throw new Error(`未知场景：${scenario}`);
}
