// 主线场景用的假模型扩展。
// 注册一个名为 faux 的 provider，回复是预先写好的：
//   第 1 次请求：一段文本 + 调用 read 读 README.md
//   第 2 次请求：输出总结
// 设置 PI_TRACE=1 时，把每次请求收到的上下文打印到 stderr。
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";

function trace(callIndex: number, context: { messages: any[] }) {
	if (process.env.PI_TRACE !== "1") return;
	const lines = context.messages.map((m) => {
		if (m.role === "system") {
			const sections = Object.keys(m.sections ?? {}).join(",");
			const tools = (m.toolsAdded ?? []).map((t: any) => t.name).join(",");
			return `  system     sections=[${sections}] toolsAdded=[${tools}]`;
		}
		return `  ${m.role.padEnd(10)} ${JSON.stringify(m.content).slice(0, 70)}`;
	});
	process.stderr.write(`[faux] 第 ${callIndex} 次请求，收到 ${context.messages.length} 条消息:\n${lines.join("\n")}\n`);
}

export default function (pi: any) {
	const faux = fauxProvider({ provider: "faux", api: "faux-api" });
	faux.setResponses([
		(context) => {
			trace(1, context);
			return fauxAssistantMessage(
				[fauxText("我先读一下 README。"), fauxToolCall("read", { path: "README.md" }, { id: "call_1" })],
				{ stopReason: "toolUse" },
			);
		},
		(context) => {
			trace(2, context);
			return fauxAssistantMessage("这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。");
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
