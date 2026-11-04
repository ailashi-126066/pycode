// M1 实验：provider 适配器发请求前对 transcript 做的几种纯函数转换。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m1-transcript.ts [场景]
// 场景：
//   collapse   resolveTranscript：provider 不接受对话中途的 system 消息时，把所有 system 消息重放成开头一条
//   tools      resolveTranscriptTools："none" / "additions" / "changes" 三种工具变更能力下，工具清单怎么拆分
//   transform  transformMessages：跨 provider 的 thinking、工具调用 id、缺失的工具结果、失败的 assistant 消息
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
await import(pathToFileURL(`${PI}/packages/coding-agent/src/experimental/source-resolver.ts`).href);
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);
const { normalizeContext, resolveTranscript, resolveTranscriptTools, getCurrentTools } = await load("packages/ai/src/utils/transcript.ts");
const { transformMessages } = await load("packages/ai/src/api/transform-messages.ts");

const scenario = process.argv[2] ?? "collapse";
const tool = (name: string) => ({ name, description: `${name} tool`, parameters: { type: "object", properties: {} } });
const names = (tools: any[] | undefined) => (tools ?? []).map((t) => t.name).join(",");
function show(m: any): string {
	if (m.role === "system") {
		const parts = [`content=${JSON.stringify(m.content)}`];
		if (m.sections) parts.push(`sections=${JSON.stringify(m.sections)}`);
		if (m.toolsAdded) parts.push(`toolsAdded=[${names(m.toolsAdded)}]`);
		if (m.toolsRemoved) parts.push(`toolsRemoved=[${names(m.toolsRemoved)}]`);
		return `system     ${parts.join(" ")}`;
	}
	if (m.role === "assistant") {
		const blocks = m.content.map((b: any) => (b.type === "toolCall" ? `toolCall(${b.id})` : b.type === "text" ? `text${b.textSignature ? "+sig" : ""}` : `thinking${b.thinkingSignature ? "+sig" : ""}`));
		return `assistant  ${m.provider}/${m.model} stop=${m.stopReason} [${blocks.join(", ")}]`;
	}
	if (m.role === "toolResult") return `toolResult toolCallId=${m.toolCallId} isError=${m.isError} ${JSON.stringify(m.content[0]?.text ?? "")}`;
	return `user       ${JSON.stringify(typeof m.content === "string" ? m.content : m.content.map((b: any) => b.text ?? b.type))}`;
}

const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const assistant = (provider: string, api: string, model: string, content: any[], stopReason = "toolUse") => ({ role: "assistant", content, api, provider, model, usage, stopReason, timestamp: 2 });

if (scenario === "collapse") {
	const context = normalizeContext({
		systemPrompt: "You are pi.",
		tools: [tool("read"), tool("write")],
		messages: [
			{ role: "user", content: "第一个问题", timestamp: 1 },
			{ role: "system", content: "", sections: { rules: "<rules>Answer in English.</rules>" }, toolsAdded: [tool("grep")], toolsRemoved: [{ name: "write" }], timestamp: 2 },
			{ role: "user", content: "第二个问题", timestamp: 3 },
		],
	});
	console.log("normalizeContext 之后（provider 收到的 TranscriptContext）：");
	for (const m of context.messages) console.log(`  ${show(m)}`);
	console.log(`getCurrentTools：[${names(getCurrentTools(context.messages))}]`);
	console.log("resolveTranscript(context, true)，模型接受中途 system 消息，原样保留：");
	for (const m of resolveTranscript(context, true).messages) console.log(`  ${show(m)}`);
	console.log("resolveTranscript(context, false)，即 collapseSystemMessages，重放成开头一条：");
	for (const m of resolveTranscript(context, false).messages) console.log(`  ${show(m)}`);
} else if (scenario === "tools") {
	const additionsOnly = [
		{ role: "system", content: "p", toolsAdded: [tool("read"), tool("write")], timestamp: 0 },
		{ role: "user", content: "u1", timestamp: 1 },
		{ role: "system", content: "", toolsAdded: [tool("grep")], timestamp: 2 },
		{ role: "user", content: "u2", timestamp: 3 },
	];
	const withRemoval = [
		...additionsOnly,
		{ role: "system", content: "", toolsRemoved: [{ name: "write" }], timestamp: 4 },
		{ role: "user", content: "u3", timestamp: 5 },
		{ role: "system", content: "", toolsAdded: [tool("ls")], timestamp: 6 },
	];
	for (const [label, messages] of [["只有新增", additionsOnly], ["中间有一次删除", withRemoval]] as const) {
		const changes = messages
			.filter((m: any) => m.role === "system")
			.map((m: any) => `+[${names(m.toolsAdded)}]${m.toolsRemoved ? ` -[${names(m.toolsRemoved)}]` : ""}`)
			.join(" → ");
		console.log(`=== ${label}：system 消息的工具变更 ${changes}`);
		for (const support of ["none", "additions", "changes"]) {
			const r = resolveTranscriptTools(messages, support);
			console.log(`  ${support.padEnd(9)} requestTools=[${names(r.requestTools)}] foldedSystemMessages=${r.foldedSystemMessages}`);
		}
	}
} else if (scenario === "transform") {
	const target = { id: "claude-sonnet-4-5", provider: "anthropic", api: "anthropic-messages", input: ["text", "image"] };
	const messages = [
		{ role: "system", content: "You are pi.", timestamp: 0 },
		{ role: "user", content: "读 README.md", timestamp: 1 },
		// 1. 另一个模型（OpenAI Responses）产生的回复：带签名的 thinking、带签名的 text、长工具调用 id
		assistant("openai", "openai-responses", "gpt-5", [
			{ type: "thinking", thinking: "plan", thinkingSignature: "{\"id\":\"rs_1\"}" },
			{ type: "text", text: "好的", textSignature: "msg_1" },
			{ type: "toolCall", id: "call_1|fc_abc", name: "read", arguments: { path: "README.md" } },
		]),
		{ role: "toolResult", toolCallId: "call_1|fc_abc", toolName: "read", content: [{ type: "text", text: "# Demo" }], isError: false, timestamp: 3 },
		// 2. 同一个模型的回复：thinking 保留
		assistant("anthropic", "anthropic-messages", "claude-sonnet-4-5", [
			{ type: "thinking", thinking: "next", thinkingSignature: "sig-1" },
			{ type: "toolCall", id: "toolu_2", name: "read", arguments: { path: "a.md" } },
			{ type: "toolCall", id: "toolu_3", name: "read", arguments: { path: "b.md" } },
		]),
		// toolu_3 的结果缺失（例如进程在工具执行时退出），中间还夹了一条 system 消息
		{ role: "system", content: "", sections: { rules: "x" }, timestamp: 4 },
		{ role: "toolResult", toolCallId: "toolu_2", toolName: "read", content: [{ type: "text", text: "A" }], isError: false, timestamp: 5 },
		{ role: "user", content: "继续", timestamp: 6 },
		// 3. 中途失败的回复：整条跳过
		assistant("anthropic", "anthropic-messages", "claude-sonnet-4-5", [{ type: "text", text: "半截" }], "error"),
		{ role: "user", content: "再试一次", timestamp: 7 },
	];
	const normalizeToolCallId = (id: string) => id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
	console.log("输入：");
	for (const m of messages) console.log(`  ${show(m)}`);
	console.log(`transformMessages(messages, ${target.provider}/${target.id}, normalizeToolCallId) 输出：`);
	for (const m of transformMessages(messages, target, normalizeToolCallId)) console.log(`  ${show(m)}`);
} else {
	throw new Error(`未知场景：${scenario}`);
}
