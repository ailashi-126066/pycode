// M9 实验：打印入口层几个数据结构的真实实例。
//
// 用法（在 lab 目录运行，PI 指向 pi 仓库）：
//   export PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent-dump   # 不碰 ~/.pi
//   node --import "$(node -p 'require("node:url").pathToFileURL(process.argv[1]).href' \
//     "$PI/packages/coding-agent/src/experimental/source-resolver.ts")" m9-dump.ts [场景]
//
// 场景：
//   args     对几组命令行参数调用 parseArgs()，打印 Args 里和运行模式、会话有关的字段，
//            再对主线场景的参数调用 buildInitialMessage()，打印交给 runPrintMode 的 PrintModeOptions 的来源（默认）
//   runtime  用 createAgentSessionRuntime() 建一个 runtime（假模型、内存会话），打印 AgentSessionRuntime 和
//            AgentSessionServices 的字段
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
	type CreateAgentSessionRuntimeFactory,
	createAgentSessionFromServices,
	createAgentSessionRuntime,
	createAgentSessionServices,
	getAgentDir,
	SessionManager,
} from "@earendil-works/pi-coding-agent";
import { parseArgs } from "@earendil-works/pi-coding-agent/cli/args.ts";
import { buildInitialMessage } from "@earendil-works/pi-coding-agent/cli/initial-message.ts";

if (!process.env.PI_CODING_AGENT_DIR) throw new Error("请设置 PI_CODING_AGENT_DIR，避免读写 ~/.pi");
const labDir = dirname(fileURLToPath(import.meta.url));
const scenario = process.argv[2] ?? "args";

if (scenario === "args") {
	const ext = join(labDir, "faux-ext.ts");
	const cases: string[][] = [
		// run.sh 传给 cli.ts 的参数（--no-env 已被 pi-test.sh 去掉）
		["-e", ext, "--provider", "faux", "--model", "faux-1", "-p", "读 README.md，总结成三句话"],
		["--mode", "json", "读 README.md，总结成三句话"],
		["--mode", "rpc"],
		["-c", "--no-session", "第一条", "第二条"],
	];
	for (const args of cases) {
		const parsed = parseArgs(args);
		const picked = {
			print: parsed.print,
			mode: parsed.mode,
			continue: parsed.continue,
			noSession: parsed.noSession,
			provider: parsed.provider,
			model: parsed.model,
			extensions: parsed.extensions?.map((p) => p.replace(labDir, "<lab>")),
			messages: parsed.messages,
			fileArgs: parsed.fileArgs,
			unknownFlags: Object.fromEntries(parsed.unknownFlags),
			diagnostics: parsed.diagnostics,
		};
		console.log(`parseArgs(${JSON.stringify(args.map((a) => a.replace(labDir, "<lab>")))})`);
		console.log(`  → ${JSON.stringify(picked)}`);
	}
	const parsed = parseArgs(cases[0]);
	const initial = buildInitialMessage({ parsed });
	console.log(`buildInitialMessage → ${JSON.stringify(initial)}，之后 parsed.messages = ${JSON.stringify(parsed.messages)}`);
} else if (scenario === "runtime") {
	const cwd = process.env.WORK ?? "/tmp/pi-lab/m9-work-dump";
	mkdirSync(cwd, { recursive: true });
	let factoryCalls = 0;
	const createRuntime: CreateAgentSessionRuntimeFactory = async ({ cwd, agentDir, sessionManager, sessionStartEvent }) => {
		factoryCalls++;
		console.log(`[factory] 第 ${factoryCalls} 次调用，sessionStartEvent=${JSON.stringify(sessionStartEvent)}`);
		const services = await createAgentSessionServices({
			cwd,
			agentDir,
			resourceLoaderOptions: { additionalExtensionPaths: [join(labDir, "faux-ext.ts")] },
		});
		const model = services.modelRuntime.getModel("faux", "faux-1");
		const created = await createAgentSessionFromServices({ services, sessionManager, sessionStartEvent, model });
		return { ...created, services, diagnostics: services.diagnostics };
	};
	const runtime = await createAgentSessionRuntime(createRuntime, {
		cwd,
		agentDir: getAgentDir(),
		sessionManager: SessionManager.inMemory(cwd),
	});
	const { services } = runtime;
	console.log("AgentSessionRuntime：");
	console.log(`  cwd = ${runtime.cwd}`);
	console.log(`  diagnostics = ${JSON.stringify(runtime.diagnostics)}`);
	console.log(`  modelFallbackMessage = ${runtime.modelFallbackMessage}`);
	console.log(`  session.sessionId = ${runtime.session.sessionId}`);
	console.log(`  session.model = ${runtime.session.model?.provider}/${runtime.session.model?.id}`);
	console.log("AgentSessionServices：");
	console.log(`  keys = ${Object.keys(services).join(", ")}`);
	console.log(`  agentDir = ${services.agentDir}`);
	console.log(`  resourceLoader 加载的扩展 = ${services.resourceLoader.getExtensions().extensions.map((e) => e.path.replace(labDir, "<lab>")).join(", ")}`);
	const firstSession = runtime.session;
	await runtime.newSession();
	console.log("调用 runtime.newSession() 之后：");
	console.log(`  session 是同一个对象吗？ ${runtime.session === firstSession}`);
	console.log(`  session.sessionId = ${runtime.session.sessionId}`);
	await runtime.dispose();
} else {
	throw new Error(`未知场景：${scenario}`);
}
