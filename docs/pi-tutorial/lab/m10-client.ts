// M10 实验：不经过 coding-agent，直接用 pi-mcp 的 McpClient 连接 MCP 服务器，打印线上的每条 JSON-RPC 消息。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m10-client.ts [场景]
// 场景：
//   stdio    （默认）用 StdioTransport 启动 pi 仓库自带的测试服务器 packages/mcp/test/fixtures/stdio-server.mjs，
//            依次 connect、listTools、callTool("echo")、close
//   timeout  用 InMemoryTransport 在进程内放一个服务器，它不回答 tools/call；客户端设 200ms 超时
//   abort    同上，但客户端在 50ms 时用 AbortSignal 取消请求
//   content  不连接服务器，只演示 toLlmContent 把各种 MCP 内容块转成什么
//   names    不连接服务器，演示 coding-agent 的 createMcpToolName 怎么生成 mcp__<server>__<tool>
//   config <目录>  用 coding-agent 的 loadMcpConfig 读取 <目录>/mcp.json，打印 LoadedMcpConfig
//   wrap     连接本目录的 m10-server.mjs，用 coding-agent 的 createMcpToolDefinition 把它的工具包装成
//            ToolDefinition，打印主要字段，再直接调用两个工具的 execute()
// 每行开头的 "→" 表示客户端发出的消息，"←" 表示客户端收到的消息。
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);

const mcp = await load("packages/mcp/src/index.ts");
const { createInMemoryTransportPair } = await load("packages/mcp/src/transports/in-memory.ts");
const { McpClient, StdioTransport, toLlmContent } = mcp;

const scenario = process.argv[2] ?? "stdio";
const t0 = Date.now();
const ms = () => String(Date.now() - t0).padStart(4);
const show = (value: unknown) => JSON.stringify(value);

/** 包一层任意 transport，记录收发的每条消息，其余行为不变。 */
function logged(transport: any) {
	transport.onMessage((message: unknown) => console.log(`${ms()}ms ← ${show(message)}`));
	return {
		start: () => transport.start(),
		send: (message: unknown, options?: unknown) => {
			console.log(`${ms()}ms → ${show(message)}`);
			return transport.send(message, options);
		},
		close: () => transport.close(),
		onMessage: (listener: any) => transport.onMessage(listener),
		onError: (listener: any) => transport.onError(listener),
		onClose: (listener: any) => transport.onClose(listener),
	};
}

/** 进程内的假服务器：回答 initialize 和 tools/list，收到 tools/call 不回答。 */
async function silentServer() {
	const { client, server } = createInMemoryTransportPair();
	server.onMessage((message: any) => {
		if (message.method === "initialize") {
			void server.send({
				jsonrpc: "2.0",
				id: message.id,
				result: { protocolVersion: "2025-11-25", capabilities: { tools: {} }, serverInfo: { name: "silent", version: "0" } },
			});
		}
	});
	await server.start();
	return client;
}

if (scenario === "stdio") {
	const transport = new StdioTransport({
		command: process.execPath,
		args: [`${PI}/packages/mcp/test/fixtures/stdio-server.mjs`],
	});
	const client = new McpClient({ name: "m10-lab", version: "1.0.0" });
	console.log(`connectionState=${client.connectionState}`);
	const init = await client.connect(logged(transport));
	console.log(`connectionState=${client.connectionState} protocolVersion=${client.protocolVersion}`);
	console.log(`InitializeResult: ${show(init)}`);
	const tools = await client.listTools();
	console.log(`listTools(): ${show(tools)}`);
	const result = await client.callTool("echo", { text: "hello" });
	console.log(`callTool(): ${show(result)}`);
	console.log(`toLlmContent(): ${show(toLlmContent(result))}`);
	console.log(`stderr of server: ${show(transport.stderr)}`);
	await client.close();
	console.log(`connectionState=${client.connectionState}`);
} else if (scenario === "timeout" || scenario === "abort") {
	const client = new McpClient({ name: "m10-lab", version: "1.0.0" });
	await client.connect(logged(await silentServer()));
	const controller = new AbortController();
	if (scenario === "abort") setTimeout(() => controller.abort("user pressed Esc"), 50);
	try {
		await client.callTool("echo", { text: "hello" }, { timeoutMs: 200, signal: controller.signal });
	} catch (error: any) {
		console.log(`${ms()}ms callTool rejected: ${error.name}: ${error.message}`);
	}
	await new Promise((resolve) => setTimeout(resolve, 10));
	await client.close();
} else if (scenario === "content") {
	const result = {
		content: [
			{ type: "text", text: "plain text" },
			{ type: "image", data: "iVBORw0KGgo=", mimeType: "image/png" },
			{ type: "audio", data: "AAAA", mimeType: "audio/wav" },
			{ type: "resource_link", uri: "file:///notes.md", name: "notes.md" },
			{ type: "resource", resource: { uri: "file:///a.txt", text: "embedded text" } },
			{ type: "resource", resource: { uri: "file:///b.bin", blob: "AAEC", mimeType: "application/octet-stream" } },
		],
	};
	for (const block of result.content) {
		console.log(`${block.type.padEnd(13)} → ${show(toLlmContent({ content: [block] }))}`);
	}
	console.log(`只有 structuredContent → ${show(toLlmContent({ content: [], structuredContent: { total: 3 } }))}`);
} else if (scenario === "config") {
	const agentDir = process.argv[3];
	if (!agentDir) throw new Error("用法：m10-client.ts config <PI_CODING_AGENT_DIR>");
	const { loadMcpConfig } = await load("packages/coding-agent/src/extensions/mcp/config.ts");
	console.log(JSON.stringify(loadMcpConfig({ agentDir, cwd: process.cwd(), projectTrusted: false }), null, 2));
} else if (scenario === "wrap") {
	const { createMcpToolDefinition, createMcpToolName } = await load("packages/coding-agent/src/extensions/mcp/tools.ts");
	const labDir = new URL(".", import.meta.url).pathname;
	const client = new McpClient({ name: "m10-lab", version: "1.0.0" });
	await client.connect(new StdioTransport({ command: process.execPath, args: [`${labDir}m10-server.mjs`] }));
	const tools = await client.listTools();
	console.log(`listTools(): ${show(tools)}`);
	const definitions = tools.map((tool: any) =>
		createMcpToolDefinition({
			server: "demo",
			tool,
			name: createMcpToolName("demo", tool.name),
			exposure: "direct",
			namespace: { name: "mcp__demo", aliases: ["demo"] },
			timeoutMs: 60_000,
			getClient: async () => client,
		}),
	);
	for (const d of definitions) {
		const { name, label, description, parameters, exposure, namespace, annotations } = d;
		console.log(`ToolDefinition: ${show({ name, label, description, parameters, exposure, namespace, annotations })}`);
		console.log(`  outputSchema.properties 的键: ${show(Object.keys(d.outputSchema.properties))}`);
	}
	for (const [d, args] of [[definitions[0], { text: "hi" }], [definitions[1], {}]] as const) {
		const result = await d.execute("call_x", args, undefined, undefined);
		console.log(`${d.name}.execute() → ${show(result)}`);
	}
	await client.close();
} else if (scenario === "names") {
	const { createMcpToolName } = await load("packages/coding-agent/src/extensions/mcp/tools.ts");
	const cases: [string, string][] = [
		["demo", "echo"],
		["my-server", "get.item"],
		["github", "search_code_in_every_repository_of_the_whole_organization"],
	];
	for (const [server, tool] of cases) console.log(`${server} / ${tool} → ${createMcpToolName(server, tool)}`);
	const taken = createMcpToolName("demo", "a_b");
	console.log(`demo / a-b（${taken} 已被 a_b 占用）→ ${createMcpToolName("demo", "a-b", (name: string) => name === taken)}`);
} else {
	throw new Error(`未知场景：${scenario}`);
}
