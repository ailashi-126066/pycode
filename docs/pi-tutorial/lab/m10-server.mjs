// M10 实验用的最小 stdio MCP 服务器，不依赖任何包。
// 写法参照 packages/mcp/test/fixtures/stdio-server.mjs：每行一条 JSON-RPC 消息。
// 在它的基础上加了三样东西，方便观察 pi 的行为：
//   - 工具带 description、inputSchema.properties 和 annotations
//   - 两个工具：echo 原样返回 text；fail 返回 isError: true 的结果
//   - 设置环境变量 M10_SERVER_LOG=文件路径 时，把收到的每条消息的 method（或响应 id）追加到这个文件
// 用法：不需要手动运行。M10 的 mcp.json 用 "command": "node", "args": ["<本文件路径>"] 启动它。
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";

const logFile = process.env.M10_SERVER_LOG;
const log = (text) => {
	if (logFile) appendFileSync(logFile, `${text}\n`);
};

const tools = [
	{
		name: "echo",
		description: "Echo the given text back",
		inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
		annotations: { readOnlyHint: true },
	},
	{
		name: "fail",
		description: "Always fails",
		inputSchema: { type: "object", properties: {} },
	},
];

function reply(id, result) {
	process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`);
}

console.error("m10 server ready");
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
	const message = JSON.parse(line);
	log(message.method ?? `response id=${message.id}`);
	if (!("id" in message) || message.method === undefined) continue;
	switch (message.method) {
		case "initialize":
			reply(message.id, {
				protocolVersion: "2025-06-18",
				capabilities: { tools: {} },
				serverInfo: { name: "m10-demo", version: "1.0.0" },
				instructions: "Demo server for the pi tutorial.",
			});
			break;
		case "tools/list":
			reply(message.id, { tools });
			break;
		case "tools/call": {
			const { name, arguments: args = {} } = message.params;
			if (name === "echo") reply(message.id, { content: [{ type: "text", text: `echo: ${args.text}` }] });
			else reply(message.id, { content: [{ type: "text", text: "something went wrong" }], isError: true });
			break;
		}
		case "ping":
			reply(message.id, {});
			break;
		default:
			process.stdout.write(
				`${JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "not found" } })}\n`,
			);
	}
}
