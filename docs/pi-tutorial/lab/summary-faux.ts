// 总结篇综合练习用的假模型扩展，代替 faux-ext.ts。
// 注册名为 faux 的 provider，回复是预先写好的，一共 3 轮：
//   第 1 次请求：调用 bash，命令取自环境变量 SUMMARY_CMD（默认 "rm -rf build"，练习里的扩展应该拦下它）
//   第 2 次请求：复述上一个工具结果，再调用 bash 执行 "ls"（应该放行）
//   第 3 次请求：复述上一个工具结果，结束
// 每次请求都把模型收到的消息角色打印到 stderr，用来确认 custom 条目没有发给模型。
// 用法：
//   EXT=$PWD/summary-faux.ts PI=/path/to/pi ./run.sh -e "$PWD/summary-guard.ts" -p "清理构建目录"
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";

function lastToolResultText(context: { messages: any[] }): string {
	const last = context.messages[context.messages.length - 1];
	const text = (last?.content ?? []).map((c: any) => c.text ?? "").join("");
	return `isError=${last?.isError}：${text.trim().replace(/\n/g, " ")}`;
}

function trace(callIndex: number, context: { messages: any[] }) {
	const roles = context.messages.map((m) => m.role).join(",");
	process.stderr.write(`[faux] 第 ${callIndex} 次请求，${context.messages.length} 条消息：${roles}\n`);
}

export default function (pi: any) {
	const command = process.env.SUMMARY_CMD ?? "rm -rf build";
	const faux = fauxProvider({ provider: "faux", api: "faux-api" });
	faux.setResponses([
		(context) => {
			trace(1, context);
			return fauxAssistantMessage([fauxToolCall("bash", { command }, { id: "call_1" })], {
				stopReason: "toolUse",
			});
		},
		(context) => {
			trace(2, context);
			return fauxAssistantMessage(
				[fauxText(`第一个命令的结果（${lastToolResultText(context)}）。再看看目录。`), fauxToolCall("bash", { command: "ls" }, { id: "call_2" })],
				{ stopReason: "toolUse" },
			);
		},
		(context) => {
			trace(3, context);
			return fauxAssistantMessage(`目录内容（${lastToolResultText(context)}）。`);
		},
	]);
	pi.registerProvider(faux.provider.id, {
		api: faux.api,
		baseUrl: faux.getModel().baseUrl,
		apiKey: "faux-key",
		models: faux.models,
		streamSimple: faux.provider.streamSimple,
	});
}
