// 总结篇实验用的扩展：练习第 2 问的一个错误写法。
// 在 message_end 里用 pi.appendEntry() 记录 assistant 消息的 usage，而不是在 turn_end 里返回草稿。
// 用来观察 custom 条目在会话文件里的位置：AgentSession 先派发扩展事件，后写 message 条目。
// 用法（配合 summary-faux.ts 假模型）：
//   EXT=$PWD/summary-faux.ts PI=/path/to/pi ./run.sh -e "$PWD/summary-message-end.ts" -p "清理构建目录"
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	pi.on("message_end", async (event) => {
		const message = event.message;
		if (message.role !== "assistant") return undefined;
		pi.appendEntry("turn-usage", { totalTokens: message.usage.totalTokens });
		return undefined;
	});
}
