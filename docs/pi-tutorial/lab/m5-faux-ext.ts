// M5 实验用的假模型扩展：上下文窗口很小，用来强制触发压缩。
// 和 faux-ext.ts 一样注册名为 faux、模型为 faux-1 的 provider，区别是：
//   - 上下文窗口由环境变量 M5_CTX 指定（默认 3000 token）。
//   - 回复不是固定两条，而是按请求内容决定：
//       普通请求：找到最后一条 user 消息，消息里按出现顺序提到的 *.txt 文件，
//                 还没读的就调用 read 读下一个；都读完了，回复"文件都读完了。"
//       压缩请求（系统提示词是 "You are a context summarization assistant"）：
//                 返回一段固定格式的假摘要。分割轮次前缀的请求返回前缀格式的假摘要。
// 设置 M5_TRACE=1 时，把每次请求的类型、收到的消息、压缩请求的提示词打印到 stderr。
// 用法：见 m5-run.sh。
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";

const contextWindow = Number(process.env.M5_CTX ?? 3000);
const trace = (text: string) => {
	if (process.env.M5_TRACE === "1") process.stderr.write(`${text}\n`);
};
let callIndex = 0;
let summaryIndex = 0;

function textOf(message: any): string {
	if (typeof message.content === "string") return message.content;
	return (message.content ?? []).map((block: any) => block.text ?? "").join("");
}

function respond(context: { messages: any[] }) {
	callIndex++;
	const system = context.messages.find((m) => m.role === "system");
	const systemText = system ? textOf(system) + Object.values(system.sections ?? {}).join("") : "";
	if (systemText.startsWith("You are a context summarization assistant")) {
		summaryIndex++;
		const prompt = textOf(context.messages[context.messages.length - 1]);
		const isPrefix = prompt.startsWith("# Conversation");
		const isUpdate = prompt.includes("<previous-summary>");
		const kind = isPrefix ? "分割轮次前缀" : isUpdate ? "更新摘要" : "首次摘要";
		trace(`[faux] 第 ${callIndex} 次请求：压缩请求（${kind}），提示词 ${prompt.length} 字符:`);
		// 文件内容每行都是 "line NN of x.txt: ..."，连续的这种行只打印一行说明。
		const shown: string[] = [];
		for (const line of prompt.split("\n")) {
			if (/^line \d+ of /.test(line)) {
				if (!shown[shown.length - 1]?.includes("行文件内容")) shown.push("  | （这里是 40 行文件内容，不逐行打印）");
				continue;
			}
			shown.push(`  | ${line.slice(0, 110)}`);
		}
		trace(shown.join("\n"));
		// 假摘要把对话里用户说过的话原样写进去，真实模型也会这样保留用户的请求。
		const requests = prompt
			.split("\n")
			.filter((line) => line.startsWith("[User]: "))
			.map((line) => `- ${line.slice("[User]: ".length)}`)
			.join("\n");
		if (isPrefix) {
			return fauxAssistantMessage(
				`## Original Request\n${requests}\n\n## Progress So Far\n- 前缀摘要 #${summaryIndex}\n\n## Context Needed to Continue\n- (none)`,
			);
		}
		return fauxAssistantMessage(
			`## Goal\n${requests || "- (见 previous-summary)"}\n\n## Constraints & Preferences\n- (none)\n\n## Progress\n### Done\n- [x] 摘要 #${summaryIndex}\n\n### In Progress\n- (none)\n\n### Blocked\n- (none)\n\n## Key Decisions\n- (none)\n\n## Next Steps\n1. 继续\n\n## Critical Context\n- (none)`,
		);
	}

	const lines = context.messages.map((m) => {
		if (m.role === "system") {
			const sections = Object.keys(m.sections ?? {}).join(",");
			return `  system            sections=[${sections}] content=${JSON.stringify(textOf(m)).slice(0, 40)}`;
		}
		return `  ${m.role.padEnd(17)} ${JSON.stringify(textOf(m)).slice(0, 60)}`;
	});
	trace(`[faux] 第 ${callIndex} 次请求：普通请求，收到 ${context.messages.length} 条消息:\n${lines.join("\n")}`);

	// 压缩摘要以 user 消息的形式出现，以这段前缀开头（messages.ts 的 COMPACTION_SUMMARY_PREFIX）。
	const isSummary = (m: any) => m.role === "user" && textOf(m).startsWith("The conversation history before");
	// 用户的请求：最后一条普通 user 消息。它被压缩掉时，就从摘要里找（去掉文件清单部分）。
	let lastUser = context.messages.findLastIndex((m) => m.role === "user" && !isSummary(m));
	if (lastUser < 0) lastUser = context.messages.findLastIndex(isSummary);
	const request = textOf(context.messages[lastUser]).replace(/<(read|modified)-files>[\s\S]*?<\/\1-files>/g, "");
	const files = [...new Set([...request.matchAll(/[\w-]+\.txt/g)].map((m) => m[0]))];
	// 已读的文件 = 上下文里 read 调用过的路径 + 压缩摘要 <read-files> 里列出的路径。
	// 压缩后前面的 read 调用被摘要替换了，只能从 <read-files> 知道它们读过。
	const done = new Set<string>();
	for (const m of context.messages) {
		if (m.role === "assistant") {
			for (const block of m.content) if (block.type === "toolCall") done.add(block.arguments.path);
		}
		if (isSummary(m)) {
			const list = textOf(m).match(/<read-files>\n([\s\S]*?)\n<\/read-files>/);
			for (const path of list?.[1].split("\n") ?? []) done.add(path);
		}
	}
	const next = files.find((file) => !done.has(file));
	if (next) {
		return fauxAssistantMessage([fauxText(`读 ${next}。`), fauxToolCall("read", { path: next }, { id: `call_${next}` })], {
			stopReason: "toolUse",
		});
	}
	return fauxAssistantMessage("文件都读完了。");
}

export default function (pi: any) {
	const faux = fauxProvider({ provider: "faux", api: "faux-api", models: [{ id: "faux-1", contextWindow, maxTokens: 1024 }] });
	faux.setResponses(Array.from({ length: 100 }, () => respond));
	pi.registerProvider(faux.provider.id, {
		api: faux.api,
		baseUrl: faux.getModel().baseUrl,
		apiKey: "faux-key",
		models: faux.models,
		streamSimple: faux.provider.streamSimple,
	});
}
