// M5 实验的查看工具。
// 用法：
//   node m5-inspect.mjs tokens <events.jsonl>       事件流里和 token 有关的部分：每条 assistant 消息的
//                                                  usage.totalTokens、每条 toolResult 的字符数和估算 token、
//                                                  compaction_start / compaction_end 事件
//   node m5-inspect.mjs compaction <session.jsonl>  会话文件里每个 compaction 条目的完整 JSON。
//                                                  systemMessage 字段很长，只显示它的结构
import { readFileSync } from "node:fs";

const [mode, file] = process.argv.slice(2);
const records = readFileSync(file, "utf8")
	.split("\n")
	.filter((line) => line.startsWith("{"))
	.map((line) => JSON.parse(line));

if (mode === "tokens") {
	for (const event of records) {
		const message = event.message;
		if (event.type === "message_end" && message.role === "assistant") {
			const blocks = message.content.map((b) => (b.type === "toolCall" ? `toolCall ${b.name}(${b.arguments.path})` : JSON.stringify(b.text)));
			console.log(`assistant   totalTokens=${message.usage.totalTokens}  ${blocks.join(" + ")}`);
		} else if (event.type === "message_end" && message.role === "toolResult") {
			const chars = message.content.reduce((sum, b) => sum + (b.text?.length ?? 0), 0);
			console.log(`toolResult  ${chars} 字符 ≈ ${Math.ceil(chars / 4)} token`);
		} else if (event.type === "compaction_start") {
			console.log(`compaction_start reason=${event.reason}`);
		} else if (event.type === "compaction_end") {
			const r = event.result;
			console.log(
				`compaction_end   reason=${event.reason} aborted=${event.aborted} willRetry=${event.willRetry}` +
					(r ? ` tokensBefore=${r.tokensBefore} estimatedTokensAfter=${r.estimatedTokensAfter} firstKeptEntryId=${r.firstKeptEntryId}` : "") +
					(event.errorMessage ? ` error=${event.errorMessage}` : ""),
			);
		}
	}
} else if (mode === "compaction") {
	for (const entry of records.filter((e) => e.type === "compaction")) {
		const { systemMessage, ...rest } = entry;
		console.log(JSON.stringify(rest, null, 2));
		if (systemMessage) {
			console.log(
				`systemMessage: role=${systemMessage.role} sections=[${Object.keys(systemMessage.sections ?? {}).join(",")}] ` +
					`toolsAdded=[${(systemMessage.toolsAdded ?? []).map((t) => t.name).join(",")}] timestamp=${systemMessage.timestamp}`,
			);
		}
	}
} else {
	console.error("用法：node m5-inspect.mjs tokens|compaction <file>");
	process.exit(1);
}
