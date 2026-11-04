// M7 实验：订阅全部 41 种扩展事件，每收到一个就往 stderr 打印一行。
//
// 用法（和 faux-ext.ts 一起加载）：
//   PI=/path/to/pi ./run.sh -e "$PWD/m7-trace-ext.ts" -p "读 README.md，总结成三句话"
// 注意：run.sh 会先 cd 到 $WORK，-e 的相对路径按 WORK 解析，所以这里要写绝对路径。
// 环境变量：
//   M7_DUMP=tool_call,tool_result   把这些类型的事件对象完整打印成 JSON（turn_end 的 context 字段只打印键名）
// 本扩展的每个 handler 都返回 undefined（project_trust 返回 undecided），所以不改变任何流程。
const EVENTS = [
	"project_trust", "resources_discover", "mcp_servers_change",
	"session_start", "session_info_changed", "session_before_switch", "session_before_fork",
	"session_before_compact", "session_compact", "session_compact_failed", "session_shutdown",
	"session_before_tree", "session_tree",
	"context", "context_with_system", "cache_warming_decision",
	"before_provider_request", "before_provider_headers", "after_provider_response", "provider_stream_event",
	"before_agent_start", "agent_start", "agent_end", "agent_before_settle", "agent_settled",
	"ui_prompt_start", "ui_prompt_end",
	"turn_start", "turn_end", "message_start", "message_update", "message_end",
	"tool_execution_start", "tool_execution_update", "tool_execution_end",
	"model_select", "thinking_level_select",
	"tool_call", "tool_result", "user_bash", "input",
];

const dump = new Set((process.env.M7_DUMP ?? "").split(",").filter(Boolean));

function detail(e: any): string {
	switch (e.type) {
		case "message_start":
		case "message_end":
			return e.message.role;
		case "message_update":
			return e.assistantMessageEvent.type;
		case "tool_call":
		case "tool_result":
		case "tool_execution_start":
		case "tool_execution_end":
			return e.toolName;
		case "context":
		case "context_with_system":
			return `messages=[${e.messages.map((m: any) => m.role).join(",")}]`;
		case "session_start":
		case "session_shutdown":
		case "resources_discover":
			return `reason=${e.reason}`;
		case "turn_start":
		case "turn_end":
			return `turnIndex=${e.turnIndex}`;
		case "before_agent_start":
			return `messageType=${e.messageType}`;
		case "cache_warming_decision":
			return `action=${JSON.stringify(e.action)}`;
		default:
			return "";
	}
}

export default function (pi: any) {
	for (const type of EVENTS) {
		pi.on(type, (event: any, ctx: any) => {
			let line = `[ext] ${type} ${detail(event)}`.trimEnd();
			if (type === "session_start") line += ` mode=${ctx.mode} hasUI=${ctx.hasUI}`;
			process.stderr.write(`${line}\n`);
			if (dump.has(type)) {
				const shown = type === "turn_end" ? { ...event, context: Object.keys(event.context) } : event;
				process.stderr.write(`${JSON.stringify(shown)}\n`);
			}
			if (type === "project_trust") return { trusted: "undecided" };
			return undefined;
		});
	}
}
