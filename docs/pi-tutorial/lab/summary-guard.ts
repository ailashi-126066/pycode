// 总结篇综合练习的参考答案：一个 pi 扩展，做两件事。
//   1. tool_call：bash 命令里出现 rm 同时带递归和强制选项（rm -rf、rm -fr、rm -r -f、
//      rm --recursive --force 等）时拦下，工具不执行，模型收到一条 isError 的工具结果。
//   2. turn_end：把这一轮 assistant 消息的 usage 作为 custom 条目（customType "turn-usage"）
//      写进会话文件。custom 条目只用于保存状态，不会发给模型。
// 用法（配合 summary-faux.ts 假模型）：
//   EXT=$PWD/summary-faux.ts PI=/path/to/pi ./run.sh -e "$PWD/summary-guard.ts" -p "清理构建目录"
// 检查结果：
//   node summary-check.mjs "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)"
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { isToolCallEventType } from "@earendil-works/pi-coding-agent";

// 把命令按 shell 分隔符切成片段，逐段检查 rm 的选项。
// 这是字符串检查，不是沙箱：bash -c "$(...)"、变量展开、别名都能绕过它。
function isRecursiveForceRm(command: string): boolean {
	for (const segment of command.split(/[;&|\n]+/)) {
		const words = segment.trim().split(/\s+/);
		const rmIndex = words.findIndex((w) => w === "rm" || w.endsWith("/rm"));
		if (rmIndex === -1) continue;
		let recursive = false;
		let force = false;
		for (const word of words.slice(rmIndex + 1)) {
			if (word === "--") break;
			if (word === "--recursive") recursive = true;
			else if (word === "--force") force = true;
			else if (/^-[a-zA-Z]+$/.test(word)) {
				if (/[rR]/.test(word)) recursive = true;
				if (word.includes("f")) force = true;
			}
		}
		if (recursive && force) return true;
	}
	return false;
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event) => {
		if (!isToolCallEventType("bash", event)) return undefined;
		if (!isRecursiveForceRm(event.input.command)) return undefined;
		return { block: true, reason: `Blocked by summary-guard: recursive force rm is not allowed (${event.input.command})` };
	});

	pi.on("turn_end", async (event) => {
		const message = event.message;
		if (message.role !== "assistant") return undefined;
		const { input, output, cacheRead, cacheWrite, totalTokens } = message.usage;
		// event.entries 是前面的 handler 已经加的草稿。返回值会替换它，所以要在后面追加。
		return {
			entries: [
				...event.entries,
				{
					type: "custom",
					customType: "turn-usage",
					data: {
						turnIndex: event.turnIndex,
						messageEntryId: event.messageEntryId,
						model: `${message.provider}/${message.model}`,
						stopReason: message.stopReason,
						usage: { input, output, cacheRead, cacheWrite, totalTokens },
					},
				},
			],
		};
	});
}
