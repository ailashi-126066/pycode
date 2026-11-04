// M7 实验：不启动 pi，直接用 loader.ts 加载扩展、用 ExtensionRunner 派发事件。
//
// 用法（PI 指向 pi 仓库；需要 pi 自带的源码解析器，和 pi-test.sh 的做法一样）：
//   PI=/path/to/pi node --import "$PI/packages/coding-agent/src/experimental/source-resolver.ts" m7-runner.ts [场景]
// 场景：
//   load    加载 permission-gate.ts、一个不存在的文件、一个没有默认导出函数的文件，打印 LoadExtensionsResult
//   gate    对 permission-gate 调用 emitToolCall()：read、bash "ls"、bash "rm -rf build"
//   errors  两个 handler 都抛异常：tool_call 的异常从 emitToolCall() 抛出；
//           tool_execution_start 的异常被 emit() 捕获，交给 onError 监听器
import { mkdirSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const CA = `${PI}/packages/coding-agent`;
const load = (path: string) => import(pathToFileURL(`${CA}/${path}`).href);

const { loadExtensions, loadExtensionFromFactory, createExtensionRuntime } = await load("src/core/extensions/loader.ts");
const { ExtensionRunner } = await load("src/core/extensions/runner.ts");
const { createEventBus } = await load("src/core/event-bus.ts");
const { SessionManager } = await load("src/core/session-manager.ts");
const { AuthStorage } = await load("src/core/auth-storage.ts");
const { ModelRuntime } = await load("src/core/model-runtime.ts");
const { ModelRegistry } = await load("src/core/model-registry.ts");

const scenario = process.argv[2] ?? "load";
const cwd = "/tmp/pi-lab/m7-runner";
mkdirSync(cwd, { recursive: true });
const GATE = `${CA}/examples/extensions/permission-gate.ts`;

async function makeRunner(result: any) {
	const modelRuntime = await ModelRuntime.create({
		credentials: AuthStorage.inMemory(),
		modelsPath: null,
		allowModelNetwork: false,
	});
	return new ExtensionRunner(
		result.extensions,
		result.runtime,
		cwd,
		SessionManager.inMemory(cwd),
		new ModelRegistry(modelRuntime),
	);
}

function describeExtension(ext: any) {
	return {
		path: ext.path,
		handlers: Object.fromEntries([...ext.handlers].map(([event, list]: [string, unknown[]]) => [event, list.length])),
		tools: [...ext.tools.keys()],
		commands: [...ext.commands.keys()],
		flags: [...ext.flags.keys()],
		shortcuts: [...ext.shortcuts.keys()],
	};
}

if (scenario === "load") {
	writeFileSync(`${cwd}/no-factory.ts`, "export const x = 1;\n");
	const result = await loadExtensions([GATE, `${cwd}/missing.ts`, `${cwd}/no-factory.ts`], cwd);
	console.log("extensions:");
	for (const ext of result.extensions) console.log(" ", JSON.stringify(describeExtension(ext)));
	console.log("errors:");
	for (const e of result.errors) console.log(" ", JSON.stringify(e));
	console.log("runtime 的键:", Object.keys(result.runtime).join(", "));
	try {
		result.runtime.sendMessage({ customType: "x", content: "y", display: false });
	} catch (err: any) {
		console.log("bindCore 之前调用 runtime.sendMessage:", err.message);
	}
} else if (scenario === "gate") {
	const result = await loadExtensions([GATE], cwd);
	const runner = await makeRunner(result);
	console.log("hasUI:", runner.hasUI(), " hasHandlers(tool_call):", runner.hasHandlers("tool_call"));
	const events = [
		{ type: "tool_call", toolName: "read", toolCallId: "c1", input: { path: "README.md" } },
		{ type: "tool_call", toolName: "bash", toolCallId: "c2", input: { command: "ls" } },
		{ type: "tool_call", toolName: "bash", toolCallId: "c3", input: { command: "rm -rf build" } },
	];
	for (const event of events) {
		const r = await runner.emitToolCall(event);
		console.log(`emitToolCall(${event.toolName} ${JSON.stringify(event.input)}) →`, JSON.stringify(r));
	}
} else if (scenario === "errors") {
	const runtime = createExtensionRuntime();
	const ext = await loadExtensionFromFactory(
		(pi: any) => {
			pi.on("tool_execution_start", () => {
				throw new Error("通知类事件里的异常");
			});
			pi.on("tool_call", () => {
				throw new Error("tool_call 里的异常");
			});
		},
		cwd,
		createEventBus(),
		runtime,
		"<inline:thrower>",
	);
	const runner = await makeRunner({ extensions: [ext], runtime });
	runner.onError((e: any) => console.log("[onError]", JSON.stringify({ extensionPath: e.extensionPath, event: e.event, error: e.error })));
	const r1 = await runner.emit({ type: "tool_execution_start", toolCallId: "c1", toolName: "bash", args: {} });
	console.log("emit(tool_execution_start) 正常返回:", r1);
	try {
		await runner.emitToolCall({ type: "tool_call", toolName: "bash", toolCallId: "c1", input: { command: "ls" } });
		console.log("emitToolCall 正常返回");
	} catch (err: any) {
		console.log("emitToolCall 抛出:", err.message);
	}
} else {
	throw new Error(`未知场景 ${scenario}`);
}
