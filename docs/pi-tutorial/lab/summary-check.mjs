// 总结篇综合练习的检查脚本：读一个会话 JSONL 文件，检查练习要求是否满足。
//   1. 有 rm -rf 的 bash 调用，对应的工具结果 isError=true
//   2. 每个 assistant 消息条目后面都有一条 customType 为 "turn-usage" 的 custom 条目，
//      它的 messageEntryId 指向这个 assistant 条目，usage 和 assistant 消息上的一致
// 用法：node summary-check.mjs <session.jsonl>
// 全部通过时退出码为 0，否则为 1。
import { readFileSync } from "node:fs";

const entries = readFileSync(process.argv[2], "utf8")
	.trim()
	.split("\n")
	.map((line) => JSON.parse(line));
const byId = new Map(entries.map((e) => [e.id, e]));
let failures = 0;
function check(ok, label) {
	console.log(`${ok ? "PASS" : "FAIL"} ${label}`);
	if (!ok) failures++;
}

// 1. rm -rf 被拦下
const calls = entries
	.filter((e) => e.type === "message" && e.message.role === "assistant")
	.flatMap((e) => e.message.content.filter((c) => c.type === "toolCall" && c.name === "bash"));
const rmCalls = calls.filter((c) => /\brm\b/.test(c.arguments.command));
check(rmCalls.length > 0, `有 rm 调用：${rmCalls.map((c) => c.arguments.command).join(" | ")}`);
for (const call of rmCalls) {
	const result = entries.find((e) => e.type === "message" && e.message.role === "toolResult" && e.message.toolCallId === call.id);
	const text = (result?.message.content ?? []).map((c) => c.text ?? "").join("");
	check(result?.message.isError === true, `${call.id} 的工具结果 isError=${result?.message.isError}：${text}`);
}

// 2. 每轮的 usage
const assistants = entries.filter((e) => e.type === "message" && e.message.role === "assistant");
const usages = entries.filter((e) => e.type === "custom" && e.customType === "turn-usage");
check(usages.length === assistants.length, `assistant 条目 ${assistants.length} 个，turn-usage 条目 ${usages.length} 个`);
let total = 0;
for (const usageEntry of usages) {
	const target = byId.get(usageEntry.data.messageEntryId);
	const same = target && JSON.stringify(usageEntry.data.usage) === JSON.stringify({
		input: target.message.usage.input,
		output: target.message.usage.output,
		cacheRead: target.message.usage.cacheRead,
		cacheWrite: target.message.usage.cacheWrite,
		totalTokens: target.message.usage.totalTokens,
	});
	check(Boolean(same), `turnIndex=${usageEntry.data.turnIndex} → ${usageEntry.data.messageEntryId} ${JSON.stringify(usageEntry.data.usage)}`);
	total += usageEntry.data.usage.totalTokens;
}
console.log(`totalTokens 合计 ${total}`);
process.exit(failures === 0 ? 0 : 1);
