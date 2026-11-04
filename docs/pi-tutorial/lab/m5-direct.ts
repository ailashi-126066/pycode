// M5 实验：不经过 AgentSession，直接调用 core/compaction 的纯函数，打印中间数据结构。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m5-direct.ts prepare <session.jsonl> [n] [keep]
//       取会话文件里第 n 个（默认 1）compaction 条目之前的全部条目，也就是那次压缩发生时的分支，
//       重新执行一遍压缩的准备阶段，打印 ContextUsageEstimate、shouldCompact、findCutPoint 和
//       CompactionPreparation。keep 是 keepRecentTokens（默认 600，和 m5-run.sh 一致）；
//       reserveTokens 固定为 500，上下文窗口固定为 3000。
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m5-direct.ts branch
//       在内存里建一个会话：读 a.txt、改 b.txt，然后回到第一条 user 消息换个做法。
//       按 AgentSession.navigateTree() 的步骤做分支摘要（模型用一个返回固定文本的 streamFn 代替），
//       打印 collectEntriesForBranchSummary、prepareBranchEntries 的结果、BranchSummaryEntry，
//       以及新分支的上下文。
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);
const compaction = await load("packages/coding-agent/src/core/compaction/index.ts");
const { SessionManager, buildSessionProjection } = await load("packages/coding-agent/src/core/session-manager.ts");
const { convertToLlm } = await load("packages/coding-agent/src/core/messages.ts");

const textOf = (m: any): string =>
	m.summary ?? (typeof m.content === "string" ? m.content : (m.content ?? []).map((b: any) => b.text ?? `[${b.type} ${b.name ?? ""}]`).join(""));
const brief = (m: any) => `${m.role}: ${JSON.stringify(textOf(m)).slice(0, 50)}`;

const scenario = process.argv[2];

if (scenario === "prepare") {
	const [file, nArg, keepArg] = process.argv.slice(3);
	const n = Number(nArg ?? 1);
	const keep = Number(keepArg ?? 600);
	const all = readFileSync(file, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line))
		.filter((entry) => entry.type !== "session");
	const compactionIndexes = all.flatMap((entry, index) => (entry.type === "compaction" ? [index] : []));
	const end = compactionIndexes[n - 1] ?? all.length;
	const entries = all.slice(0, end);
	const settings = { enabled: true, reserveTokens: 500, keepRecentTokens: keep };
	console.log(`取第 ${n} 个 compaction 条目之前的 ${entries.length} 个条目，keepRecentTokens=${keep}`);
	entries.forEach((entry: any, i: number) => {
		const label = entry.type === "message" ? entry.message.role : entry.type;
		const tokens = entry.type === "message" ? compaction.estimateTokens(entry.message) : 0;
		console.log(`  [${String(i).padStart(2)}] ${entry.id} ${label.padEnd(22)} estimateTokens=${tokens}`);
	});

	const projection = buildSessionProjection(entries);
	const estimate = compaction.estimateProjectedContextTokens(projection, entries);
	console.log(`ContextUsageEstimate ${JSON.stringify(estimate)}`);
	console.log(`shouldCompact(${estimate.tokens}, 3000, reserve=500) = ${compaction.shouldCompact(estimate.tokens, 3000, settings)}`);
	console.log(`findCutPoint（原始条目） ${JSON.stringify(compaction.findCutPoint(entries, 0, entries.length, keep))}`);

	const preparation = compaction.prepareCompaction(entries, settings);
	if (!preparation) {
		console.log("prepareCompaction 返回 undefined");
	} else {
		const { messagesToSummarize, turnPrefixMessages, fileOps, previousSummary, ...rest } = preparation;
		console.log(`CompactionPreparation ${JSON.stringify(rest)}`);
		console.log(`  previousSummary: ${previousSummary === undefined ? "undefined" : JSON.stringify(previousSummary.slice(0, 40)) + "…"}`);
		console.log(`  messagesToSummarize (${messagesToSummarize.length}):`);
		for (const m of messagesToSummarize) console.log(`    ${brief(m)}`);
		console.log(`  turnPrefixMessages (${turnPrefixMessages.length}):`);
		for (const m of turnPrefixMessages) console.log(`    ${brief(m)}`);
		console.log(`  fileOps: read=${JSON.stringify([...fileOps.read])} written=${JSON.stringify([...fileOps.written])} edited=${JSON.stringify([...fileOps.edited])}`);
	}
} else if (scenario === "branch") {
	const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
	const assistant = (content: any[], stopReason = "toolUse") => ({
		role: "assistant",
		content,
		api: "faux-api",
		provider: "faux",
		model: "faux-1",
		usage,
		stopReason,
		timestamp: Date.now(),
	});
	const sm = SessionManager.inMemory("/tmp/pi-lab/m5-branch");
	const userId = sm.appendMessage({ role: "user", content: [{ type: "text", text: "把 b.txt 改成大写" }], timestamp: Date.now() });
	sm.appendMessage(assistant([{ type: "toolCall", id: "c1", name: "read", arguments: { path: "a.txt" } }]));
	sm.appendMessage({ role: "toolResult", toolCallId: "c1", toolName: "read", content: [{ type: "text", text: "a 的内容" }], isError: false, timestamp: Date.now() });
	sm.appendMessage(assistant([{ type: "toolCall", id: "c2", name: "edit", arguments: { path: "b.txt", edits: [] } }]));
	sm.appendMessage({ role: "toolResult", toolCallId: "c2", toolName: "edit", content: [{ type: "text", text: "ok" }], isError: false, timestamp: Date.now() });
	sm.appendMessage(assistant([{ type: "text", text: "改好了。" }], "stop"));
	const oldLeafId = sm.getLeafId();
	console.log("原分支：");
	for (const entry of sm.getBranch()) console.log(`  ${entry.id} parent=${entry.parentId} ${brief(entry.message)}`);

	// navigateTree(targetId = 第一条 user 消息)：目标是 user 消息时，新的叶子是它的父节点。
	const target = sm.getEntry(userId);
	const { entries, commonAncestorId } = compaction.collectEntriesForBranchSummary(sm, oldLeafId, userId);
	console.log(`collectEntriesForBranchSummary: commonAncestorId=${commonAncestorId} entries=[${entries.map((e: any) => e.id).join(", ")}]`);
	const prep = compaction.prepareBranchEntries(entries, 3000 - 500);
	console.log(
		`prepareBranchEntries: totalTokens=${prep.totalTokens} messages=[${prep.messages.map((m: any) => m.role).join(", ")}] ` +
			`fileOps.read=${JSON.stringify([...prep.fileOps.read])} fileOps.edited=${JSON.stringify([...prep.fileOps.edited])}`,
	);

	let receivedPrompt = "";
	const streamFn = async (_model: any, context: any) => {
		receivedPrompt = textOf(context.messages[context.messages.length - 1]);
		return { result: async () => assistant([{ type: "text", text: "## Goal\n把 b.txt 改成大写（假摘要）" }], "stop") };
	};
	const model = { id: "faux-1", provider: "faux", api: "faux-api", reasoning: false, contextWindow: 3000, maxTokens: 1024 };
	const result = await compaction.generateBranchSummary(entries, {
		model,
		signal: new AbortController().signal,
		reserveTokens: 500,
		streamFn,
	});
	console.log("摘要请求的提示词：");
	console.log(receivedPrompt.split("\n").slice(0, 12).map((line) => `  | ${line}`).join("\n"));
	console.log("  | …（后面是 BRANCH_SUMMARY_PROMPT 的其余部分）");

	const newLeafId = target.parentId;
	const summaryId = sm.branchWithSummary(newLeafId, result.summary, { readFiles: result.readFiles, modifiedFiles: result.modifiedFiles }, false, result.usage);
	const { timestamp, ...summaryEntry } = sm.getEntry(summaryId);
	console.log(`BranchSummaryEntry ${JSON.stringify(summaryEntry)}`);
	const projection = sm.buildSessionProjection();
	console.log(`新分支的 Agent 消息：[${projection.messages.map((m: any) => m.role).join(", ")}]`);
	console.log(`convertToLlm 之后：${JSON.stringify(convertToLlm(projection.messages)[0].content[0].text.slice(0, 120))}…`);
} else {
	console.error("用法：m5-direct.ts prepare <session.jsonl> [n] [keep] | branch");
	process.exit(1);
}
