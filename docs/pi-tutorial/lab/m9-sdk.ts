// M9 实验：不经过 CLI，用 SDK 创建 AgentSession，再接上不同的"前端"。
//
// 用法（在 lab 目录运行，PI 指向 pi 仓库）：
//   export PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-sdk   # 不碰 ~/.pi
//   node --import "$(node -p 'require("node:url").pathToFileURL(process.argv[1]).href' \
//     "$PI/packages/coding-agent/src/experimental/source-resolver.ts")" m9-sdk.ts [场景]
// （--import 加载的是 pi-test.sh 用的同一个源码解析器，这样 "@earendil-works/pi-coding-agent"
//   会解析到 packages/coding-agent/src/index.ts。）
//
// 场景：
//   sdk    createAgentSessionServices + createAgentSessionFromServices，挂两个订阅者，调用 session.prompt()（默认）
//   print  用 createAgentSessionRuntime 包一层，再把 runtime 交给 runPrintMode（main() 在 print 模式下做的就是这件事），
//          同时在同一个 session 上再挂一个自己的订阅者
//
// 模型用 faux-ext.ts 注册的假模型，工作目录用 WORK（默认 /tmp/pi-lab/m9-work-sdk，会生成 README.md）。
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
	type CreateAgentSessionRuntimeFactory,
	createAgentSessionFromServices,
	createAgentSessionRuntime,
	createAgentSessionServices,
	getAgentDir,
	runPrintMode,
	SessionManager,
} from "@earendil-works/pi-coding-agent";

if (!process.env.PI_CODING_AGENT_DIR) throw new Error("请设置 PI_CODING_AGENT_DIR，避免读写 ~/.pi");
const labDir = dirname(fileURLToPath(import.meta.url));
const cwd = process.env.WORK ?? "/tmp/pi-lab/m9-work-sdk";
mkdirSync(cwd, { recursive: true });
if (!existsSync(join(cwd, "README.md"))) {
	writeFileSync(join(cwd, "README.md"), "# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n");
}
const agentDir = getAgentDir();
const prompt = "读 README.md，总结成三句话";
const scenario = process.argv[2] ?? "sdk";

// 和 main() 里的 createRuntime 同样的两步：先建 cwd 绑定的服务，再建 AgentSession
const createRuntime: CreateAgentSessionRuntimeFactory = async ({ cwd, agentDir, sessionManager, sessionStartEvent }) => {
	const services = await createAgentSessionServices({
		cwd,
		agentDir,
		resourceLoaderOptions: { additionalExtensionPaths: [join(labDir, "faux-ext.ts")] },
	});
	const model = services.modelRuntime.getModel("faux", "faux-1");
	const created = await createAgentSessionFromServices({ services, sessionManager, sessionStartEvent, model });
	return { ...created, services, diagnostics: services.diagnostics };
};

console.log(`agentDir = ${agentDir}`);

if (scenario === "sdk") {
	const sessionManager = SessionManager.inMemory(cwd);
	const { session } = await createRuntime({ cwd, agentDir, sessionManager });
	console.log(`model = ${session.model?.provider}/${session.model?.id}，sessionFile = ${session.sessionFile}`);

	// 订阅者 A：像 01-minimal.ts 一样把文本增量写到 stdout
	session.subscribe((event) => {
		if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
			process.stdout.write(event.assistantMessageEvent.delta);
		}
		if (event.type === "message_end" && event.message.role === "assistant") process.stdout.write("\n");
	});
	// 订阅者 B：统计每种事件出现的次数
	const counts = new Map<string, number>();
	session.subscribe((event) => counts.set(event.type, (counts.get(event.type) ?? 0) + 1));

	await session.prompt(prompt);
	console.log("[订阅者 B] 事件计数：", JSON.stringify(Object.fromEntries(counts)));
	console.log("[state] messages role：", session.state.messages.map((m) => m.role).join(", "));
	session.dispose();
} else if (scenario === "print") {
	const runtime = await createAgentSessionRuntime(createRuntime, {
		cwd,
		agentDir,
		sessionManager: SessionManager.inMemory(cwd),
	});
	// 自己的订阅者，和 runPrintMode 挂的订阅者共用同一个 runtime.session
	const seen: string[] = [];
	runtime.session.subscribe((event) => {
		if (event.type === "agent_start" || event.type === "agent_end" || event.type === "agent_settled") seen.push(event.type);
	});
	const exitCode = await runPrintMode(runtime, { mode: "text", initialMessage: prompt });
	console.log(`[自己的订阅者] 看到：${seen.join(" → ")}`);
	console.log(`runPrintMode 返回 ${exitCode}`);
} else {
	throw new Error(`未知场景：${scenario}`);
}
