// M10 实验用的权限扩展：MCP 工具调用和内置工具走同一条 tool_call 事件。
// 规则：工具名以 mcp__ 开头、并且服务器没有把它标成 readOnlyHint: true 的调用，一律拦截。
// 每次 tool_call 都在 stderr 打印一行：工具名、parentToolCallId（codemode 脚本里的嵌套调用才有）、判定结果。
// 用法：由 m10-run.sh 在 GATE=1 时用 -e 加载，也可以自己 -e m10-gate.ts。
export default function (pi: any) {
	pi.on("tool_call", async (event: any) => {
		const tool = pi.getAllTools().find((candidate: any) => candidate.name === event.toolName);
		const readOnly = tool?.annotations?.readOnlyHint === true;
		const blocked = event.toolName.startsWith("mcp__") && !readOnly;
		const parent = event.parentToolCallId ? ` parent=${event.parentToolCallId}` : "";
		process.stderr.write(
			`[gate] tool_call ${event.toolName} id=${event.toolCallId}${parent} readOnlyHint=${readOnly} → ${blocked ? "block" : "allow"}\n`,
		);
		if (blocked) return { block: true, reason: `MCP tool ${event.toolName} is not read-only` };
		return undefined;
	});
}
