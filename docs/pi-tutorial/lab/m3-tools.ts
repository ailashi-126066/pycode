// M3 实验：不经过 agent 循环，直接调用 coding-agent 的内置工具。
//
// 用法（在任意目录运行均可，PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m3-tools.ts [场景]
// 环境变量 WORK：实验文件所在的目录，默认 /tmp/pi-lab/m3-work（不存在会自动创建，已有的同名文件会被覆盖）。
// 场景：
//   shapes    同一个 read 工具的 ToolDefinition 和 AgentTool 各有哪些字段；执行一次 read
//   read      read 的截断：3000 行文件、100KB 文件、单行 60KB 文件、offset/limit
//   bash      bash 的输出截断和临时文件、非 0 退出码、onUpdate 次数
//   timeout   bash 超时：杀掉整个进程组，后台子进程也被杀掉；以及 abort
//   edit      edit：多处替换、唯一性、重叠、找不到、模糊匹配、BOM/CRLF、旧参数格式
//   fuzzy     直接调用 fuzzyFindText：一次精确匹配、一次模糊匹配，打印 FuzzyMatchResult
//   queue     文件修改队列：同一文件的并发读改写，不排队会丢更新，排队不会；edit 工具自带排队
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const load = (path: string) => import(pathToFileURL(`${PI}/${path}`).href);
const tools = await load("packages/coding-agent/src/core/tools/index.ts");
const { createReadTool, createReadToolDefinition, createBashTool, createEditTool, createEditToolDefinition, withFileMutationQueue } =
	tools;
const WORK = process.env.WORK ?? "/tmp/pi-lab/m3-work";
mkdirSync(WORK, { recursive: true });
const scenario = process.argv[2] ?? "shapes";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const show = (label: string, value: unknown) => console.log(`[${label}]`, typeof value === "string" ? value : JSON.stringify(value));
const text = (result: any) => result.content.map((c: any) => (c.type === "text" ? c.text : `<${c.type}>`)).join("");
async function tryRun(label: string, fn: () => Promise<any>) {
	try {
		const result = await fn();
		show(label, `成功 ${JSON.stringify(text(result))}`);
		return result;
	} catch (error: any) {
		show(label, `抛出 Error: ${error.message}`);
		return undefined;
	}
}

if (scenario === "shapes") {
	writeFileSync(join(WORK, "README.md"), "# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n");
	const def = createReadToolDefinition(WORK);
	const tool = createReadTool(WORK);
	show("ToolDefinition 字段", Object.keys(def));
	show("AgentTool 字段", Object.keys(tool));
	show("execute 参数个数 ToolDefinition / AgentTool", [def.execute.length, tool.execute.length]);
	const result = await tool.execute("call_1", { path: "README.md" });
	show("AgentToolResult", result);
}

if (scenario === "read") {
	const read = createReadTool(WORK);
	const lines = Array.from({ length: 3000 }, (_, i) => `line ${i + 1}`);
	writeFileSync(join(WORK, "lines.txt"), `${lines.join("\n")}\n`);
	const r1 = await read.execute("c1", { path: "lines.txt" });
	show("3000 行：最后三行", text(r1).split("\n").slice(-3));
	show("3000 行：details.truncation（content 字段略）", { ...r1.details.truncation, content: "…" });
	const r2 = await read.execute("c2", { path: "lines.txt", offset: 2995, limit: 3 });
	show("offset=2995 limit=3", text(r2));
	const r3 = await read.execute("c3", { path: "lines.txt", offset: 2999 });
	show("offset=2999", text(r3));
	await tryRun("offset=5000", () => read.execute("c4", { path: "lines.txt", offset: 5000 }));
	const wide = Array.from({ length: 1000 }, (_, i) => `${String(i + 1).padStart(4, "0")} ${"x".repeat(95)}`);
	writeFileSync(join(WORK, "bytes.txt"), `${wide.join("\n")}\n`);
	const r5 = await read.execute("c5", { path: "bytes.txt" });
	show("1000 行 x 100 字节：最后一行", text(r5).split("\n").at(-1));
	show("1000 行 x 100 字节：truncatedBy/outputLines/outputBytes", [
		r5.details.truncation.truncatedBy,
		r5.details.truncation.outputLines,
		r5.details.truncation.outputBytes,
	]);
	writeFileSync(join(WORK, "one-line.txt"), `${"y".repeat(60 * 1024)}\n`);
	const r6 = await read.execute("c6", { path: "one-line.txt" });
	show("单行 60KB", text(r6));
}

if (scenario === "bash") {
	const bash = createBashTool(WORK);
	const r1 = await bash.execute("b1", { command: "seq 1 3000" });
	const t1 = text(r1);
	show("seq 1 3000：content 前两行", t1.split("\n").slice(0, 2));
	show("seq 1 3000：content 最后三行", t1.split("\n").slice(-3));
	show("seq 1 3000：details.truncation 部分字段", {
		truncated: r1.details.truncation.truncated,
		truncatedBy: r1.details.truncation.truncatedBy,
		totalLines: r1.details.truncation.totalLines,
		outputLines: r1.details.truncation.outputLines,
	});
	const full = readFileSync(r1.details.fullOutputPath, "utf8");
	show("临时文件行数 / 首行 / 末行", [full.split("\n").length - 1, full.split("\n")[0], full.trimEnd().split("\n").at(-1)]);
	const sc = r1.structuredContent;
	show("structuredContent", { ...sc, output: `<${sc.output.length} 字符>` });
	const r2 = await bash.execute("b2", { command: "echo out; echo err >&2; exit 3" });
	show("exit 3：isError", r2.isError);
	show("exit 3：content", text(r2));
	let updates = 0;
	const r3 = await bash.execute("b3", { command: "for i in 1 2 3; do echo $i; sleep 0.3; done" }, undefined, () => updates++);
	show("每 0.3 秒输出一行：onUpdate 次数 / 最终 content", [updates, text(r3)]);
}

if (scenario === "timeout") {
	const bash = createBashTool(WORK);
	const alive = (pid: number) => {
		try {
			process.kill(pid, 0);
			return true;
		} catch {
			return false;
		}
	};
	const t0 = Date.now();
	let pid = 0;
	try {
		await bash.execute("t1", { command: 'sleep 30 & echo "bg=$!"; wait', timeout: 1 });
	} catch (error: any) {
		show("timeout=1：耗时 ms", Date.now() - t0);
		show("timeout=1：Error.message", error.message);
		pid = Number(/bg=(\d+)/.exec(error.message)?.[1]);
	}
	await sleep(100);
	show(`后台 sleep（pid ${pid}）还活着吗`, alive(pid));
	const controller = new AbortController();
	setTimeout(() => controller.abort(), 300);
	const t1 = Date.now();
	try {
		await bash.execute("t2", { command: "echo start; sleep 30" }, controller.signal);
	} catch (error: any) {
		show("300ms 后 abort：耗时 ms", Date.now() - t1);
		show("300ms 后 abort：Error.message", error.message);
	}
	await tryRun("timeout=0", () => bash.execute("t3", { command: "echo hi", timeout: 0 }));
}

if (scenario === "edit") {
	const edit = createEditTool(WORK);
	const file = join(WORK, "a.ts");
	writeFileSync(file, 'const a = 1;\nconst b = 2;\nconst c = 3;\nconsole.log(a, b, c);\n');
	const r1 = await tryRun("两处替换", () =>
		edit.execute("e1", {
			path: "a.ts",
			edits: [
				{ oldText: "const a = 1;", newText: "const a = 10;" },
				{ oldText: "const c = 3;", newText: "const c = 30;" },
			],
		}),
	);
	show("details.diff", r1.details.diff);
	show("details.firstChangedLine", r1.details.firstChangedLine);
	show("details.patch", r1.details.patch);
	await tryRun("出现多次", () => edit.execute("e2", { path: "a.ts", edits: [{ oldText: "const", newText: "let" }] }));
	await tryRun("找不到", () => edit.execute("e3", { path: "a.ts", edits: [{ oldText: "const d = 4;", newText: "" }] }));
	await tryRun("两处重叠", () =>
		edit.execute("e4", {
			path: "a.ts",
			edits: [
				{ oldText: "const a = 10;\nconst b", newText: "x" },
				{ oldText: "const b = 2;", newText: "y" },
			],
		}),
	);
	await tryRun("替换成相同内容", () =>
		edit.execute("e5", { path: "a.ts", edits: [{ oldText: "const b = 2;", newText: "const b = 2;" }] }),
	);
	// 模糊匹配：文件里是弯引号和行尾空格，oldText 是直引号
	writeFileSync(join(WORK, "quote.ts"), "const greet = \u201chello\u201d;   \nconst other = \u201cworld\u201d;\n");
	await tryRun("模糊匹配", () =>
		edit.execute("e6", { path: "quote.ts", edits: [{ oldText: 'const greet = "hello";', newText: 'const greet = "hi";' }] }),
	);
	show("模糊匹配后的文件", readFileSync(join(WORK, "quote.ts"), "utf8"));
	// BOM + CRLF
	writeFileSync(join(WORK, "win.txt"), "\uFEFFfirst\r\nsecond\r\n");
	await tryRun("BOM+CRLF 文件", () => edit.execute("e7", { path: "win.txt", edits: [{ oldText: "second", newText: "SECOND" }] }));
	show("写回后的字节", readFileSync(join(WORK, "win.txt")).toString("hex").match(/../g)!.join(" "));
	// 旧参数格式：顶层 oldText/newText，由 prepareArguments 改写
	const def = createEditToolDefinition(WORK);
	show("prepareArguments 旧格式", def.prepareArguments({ path: "a.ts", oldText: "x", newText: "y" }));
	show("prepareArguments edits 是 JSON 字符串", def.prepareArguments({ path: "a.ts", edits: '[{"oldText":"x","newText":"y"}]' }));
}

if (scenario === "fuzzy") {
	const { fuzzyFindText } = await load("packages/coding-agent/src/core/tools/edit-diff.ts");
	// 和 edit 场景的 quote.ts 内容相同：弯引号，第一行末尾有三个空格
	const content = "const greet = \u201chello\u201d;   \nconst other = \u201cworld\u201d;\n";
	show("精确匹配 const other", fuzzyFindText(content, "const other"));
	show("模糊匹配 直引号", fuzzyFindText(content, 'const greet = "hello";'));
	show("找不到", { ...fuzzyFindText(content, "const missing"), contentForReplacement: "…" });
}

if (scenario === "queue") {
	const t0 = Date.now();
	const ms = () => String(Date.now() - t0).padStart(4);
	// 一个慢的读改写：读文件，等 50ms，追加一行，写回
	const appendLine = async (file: string, line: string) => {
		const before = readFileSync(file, "utf8");
		await sleep(50);
		writeFileSync(file, `${before}${line}\n`);
	};
	const file = join(WORK, "queue.txt");
	writeFileSync(file, "");
	await Promise.all([appendLine(file, "A"), appendLine(file, "B")]);
	show("不排队，并发追加 A、B 后的文件", JSON.stringify(readFileSync(file, "utf8")));
	writeFileSync(file, "");
	await Promise.all([
		withFileMutationQueue(file, () => appendLine(file, "A")),
		withFileMutationQueue(file, () => appendLine(file, "B")),
	]);
	show("排队，并发追加 A、B 后的文件", JSON.stringify(readFileSync(file, "utf8")));
	// edit 工具：用带延迟的 operations 观察读写顺序
	const { readFile, writeFile, access } = await import("node:fs/promises");
	const ops = {
		access: (p: string) => access(p),
		readFile: async (p: string) => {
			console.log(`${ms()}ms  read  ${p.split("/").at(-1)}`);
			const data = await readFile(p);
			await sleep(100);
			return data;
		},
		writeFile: async (p: string, content: string) => {
			await writeFile(p, content, "utf-8");
			console.log(`${ms()}ms  write ${p.split("/").at(-1)}`);
		},
	};
	const edit = createEditTool(WORK, { operations: ops });
	writeFileSync(join(WORK, "q1.txt"), "one\ntwo\n");
	writeFileSync(join(WORK, "q2.txt"), "three\n");
	await Promise.all([
		edit.execute("q-a", { path: "q1.txt", edits: [{ oldText: "one", newText: "ONE" }] }),
		edit.execute("q-b", { path: "q1.txt", edits: [{ oldText: "two", newText: "TWO" }] }),
		edit.execute("q-c", { path: "q2.txt", edits: [{ oldText: "three", newText: "THREE" }] }),
	]);
	show("q1.txt", JSON.stringify(readFileSync(join(WORK, "q1.txt"), "utf8")));
	show("q2.txt", JSON.stringify(readFileSync(join(WORK, "q2.txt"), "utf8")));
}
