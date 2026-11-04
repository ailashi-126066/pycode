// M10 实验用的假模型扩展：让 faux 模型调用 MCP 工具。
// 用法：EXT=m10-faux-ext.ts ./run.sh -p "..."（还需要在 PI_CODING_AGENT_DIR 里放 mcp.json，见 M10 §2）
// 环境变量 M10_SCENARIO 选择假模型的脚本：
//   direct   （默认）第 1 次请求调用 mcp__demo__echo，第 2 次请求把工具结果复述出来
//   fail     第 1 次请求调用 mcp__demo__fail（服务器返回 isError: true），第 2 次请求复述结果
//   deferred 第 1 次请求调用 tool_search 搜索 echo，第 2 次请求调用 mcp__demo__echo，第 3 次请求复述结果
//   codemode 第 1 次请求调用 codemode，脚本里调用 tools.mcp__demo__echo，第 2 次请求复述结果
// 设置 PI_TRACE=1 时，把每次请求收到的消息打印到 stderr：
//   system 消息打印 sections 的名字、toolsAdded / toolsRemoved；有 mcp_servers 段时打印它的内容
//   toolResult 消息打印工具名、isError 和文本内容
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

function describe(m: any): string {
	if (m.role === "system") {
		const sections = Object.keys(m.sections ?? {}).join(",");
		const added = (m.toolsAdded ?? []).map((t: any) => t.name).join(",");
		const removed = (m.toolsRemoved ?? []).join(",");
		let line = `  system     sections=[${sections}] toolsAdded=[${added}]`;
		if (removed) line += ` toolsRemoved=[${removed}]`;
		const servers = m.sections?.mcp_servers;
		if (servers) line += `\n             mcp_servers: ${JSON.stringify(servers)}`;
		return line;
	}
	if (m.role === "toolResult") {
		const text = m.content.map((c: any) => (c.type === "text" ? c.text : `[${c.type}]`)).join("");
		return `  toolResult ${m.toolName} isError=${m.isError} ${JSON.stringify(text).slice(0, 90)}`;
	}
	return `  ${m.role.padEnd(10)} ${JSON.stringify(m.content).slice(0, 90)}`;
}

function trace(callIndex: number, context: { messages: any[] }) {
	if (process.env.PI_TRACE !== "1") return;
	const lines = context.messages.map(describe);
	process.stderr.write(`[faux] 第 ${callIndex} 次请求，收到 ${context.messages.length} 条消息:\n${lines.join("\n")}\n`);
}

function lastToolText(context: { messages: any[] }): string {
	const last = [...context.messages].reverse().find((m) => m.role === "toolResult");
	return last ? last.content.map((c: any) => c.text ?? "").join("") : "(没有工具结果)";
}

type Step = (context: any) => any;
const call = (name: string, args: Record<string, unknown>, id: string): Step => () =>
	fauxAssistantMessage([fauxToolCall(name, args, { id })], { stopReason: "toolUse" });
const answer: Step = (context) => fauxAssistantMessage(`工具返回：${lastToolText(context)}`);

const scripts: Record<string, Step[]> = {
	direct: [call("mcp__demo__echo", { text: "你好 MCP" }, "call_1"), answer],
	fail: [call("mcp__demo__fail", {}, "call_1"), answer],
	deferred: [
		call("tool_search", { query: "echo text" }, "call_1"),
		call("mcp__demo__echo", { text: "你好 MCP" }, "call_2"),
		answer,
	],
	codemode: [
		call(
			"codemode",
			{ code: 'const r = await tools.mcp__demo__echo({ text: "来自脚本" });\nreturn r.content[0].text;' },
			"call_1",
		),
		answer,
	],
};

export default function (pi: any) {
	const scenario = process.env.M10_SCENARIO ?? "direct";
	const steps = scripts[scenario];
	if (!steps) throw new Error(`未知的 M10_SCENARIO：${scenario}`);
	const faux = fauxProvider({ provider: "faux", api: "faux-api" });
	faux.setResponses(
		steps.map((step, index) => (context: any) => {
			trace(index + 1, context);
			return step(context);
		}),
	);
	pi.registerProvider(faux.provider.id, {
		api: faux.api,
		baseUrl: faux.getModel().baseUrl,
		apiKey: "faux-key",
		models: faux.models,
		streamSimple: faux.provider.streamSimple,
	});
}
