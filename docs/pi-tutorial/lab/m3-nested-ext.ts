// M3 实验：嵌套工具调用（ctx.executeTool）。
// 这个扩展做三件事：
//   1. 注册名为 faux 的假模型。第 1 次请求返回"调用 peek 看 README.md"，第 2 次请求返回一句话。
//   2. 注册工具 peek。它在 execute 里通过 ctx.executeTool() 调用两个工具：read（存在）和 no_such_tool（不存在）。
//   3. 设置 M3_BLOCK_NESTED=1 时，注册一个 tool_call 处理器，拦截"由别的工具发起的 read 调用"。
//   4. 设置 M3_SHOW_CTX=1 时，peek 把它收到的 ctx（ExtensionToolContext）的几个字段打印到 stderr。
// 用法（在 docs/pi-tutorial/lab 目录下）：
//   PI=/path/to/pi EXT=$PWD/m3-nested-ext.ts WORK=/tmp/pi-lab/m3-nested-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m3-nested-agent \
//     ./run.sh --mode json "用 peek 看 README.md"
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { Type } from "typebox";

const firstLine = (outcome: any) => String(outcome.result.content?.[0]?.text ?? "").split("\n")[0];

export default function (pi: any) {
	const faux = fauxProvider({ provider: "faux", api: "faux-api" });
	faux.setResponses([
		() =>
			fauxAssistantMessage(
				[fauxText("我用 peek 看一下。"), fauxToolCall("peek", { path: "README.md" }, { id: "call_1" })],
				{ stopReason: "toolUse" },
			),
		() => fauxAssistantMessage("看完了。"),
	]);
	pi.registerProvider(faux.provider.id, {
		api: faux.api,
		baseUrl: faux.getModel().baseUrl,
		apiKey: "faux-key",
		models: faux.models,
		streamSimple: faux.provider.streamSimple,
	});

	pi.registerTool({
		name: "peek",
		label: "peek",
		description: "Read a file through the read tool and report what happened.",
		parameters: Type.Object({ path: Type.String() }),
		async execute(_id: string, params: { path: string }, _signal: AbortSignal | undefined, _onUpdate: unknown, ctx: any) {
			if (process.env.M3_SHOW_CTX === "1") {
				// 把 ctx 的几个真实字段打印到 stderr，不影响 stdout 上的 json 事件流
				const shape = {
					ownKeys: Object.keys(ctx),
					cwd: ctx.cwd,
					model: ctx.model?.id,
					tools: ctx.tools.map((t: any) => t.name),
					executeTool: typeof ctx.executeTool,
				};
				console.error(`[ctx] ${JSON.stringify(shape)}`);
			}
			const a = await ctx.executeTool("read", { path: params.path });
			const b = await ctx.executeTool("no_such_tool", {});
			return {
				content: [
					{
						type: "text",
						text: `read: id=${a.toolCall.id} isError=${a.isError} ${firstLine(a)}\nno_such_tool: id=${b.toolCall.id} isError=${b.isError} ${firstLine(b)}`,
					},
				],
				details: {},
			};
		},
	});

	if (process.env.M3_BLOCK_NESTED === "1") {
		pi.on("tool_call", (event: any) => {
			if (event.toolName === "read" && event.parentToolCallId) {
				return { block: true, reason: `nested read blocked (parent ${event.parentToolCallId})` };
			}
			return undefined;
		});
	}
}
