// M7 实验用的假模型扩展，代替 faux-ext.ts。
// 注册名为 faux 的 provider，回复是预先写好的：
//   第 1 次请求：调用 bash，命令取自环境变量 M7_CMD（默认 "rm -rf build"）
//   第 2 次请求：把收到的工具结果原样复述一遍，方便看到工具结果的内容
// 用法：
//   EXT=$PWD/m7-faux-bash.ts PI=/path/to/pi ./run.sh [-e 其他扩展...] -p "清理构建目录"
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";

export default function (pi: any) {
	const command = process.env.M7_CMD ?? "rm -rf build";
	const faux = fauxProvider({ provider: "faux", api: "faux-api" });
	faux.setResponses([
		() =>
			fauxAssistantMessage([fauxToolCall("bash", { command }, { id: "call_1" })], {
				stopReason: "toolUse",
			}),
		(context) => {
			const last = context.messages[context.messages.length - 1] as any;
			const text = (last.content ?? []).map((c: any) => c.text ?? "").join("");
			return fauxAssistantMessage([fauxText(`工具结果（isError=${last.isError}）：${text.trim()}`)]);
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
