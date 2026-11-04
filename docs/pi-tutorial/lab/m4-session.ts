// M4 实验：直接调用 coding-agent 的 SessionManager，观察会话文件怎么写、上下文怎么从会话条目重建。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m4-session.ts <场景>
// 场景：
//   persist      逐条追加 model_change、thinking_level_change、system、user、assistant，
//                每一步后检查会话文件是否存在、有几行（文件要等到第一条 user/assistant 消息才创建）
//   project      一条分支上放各种条目（custom、custom_message、label、context_edit），
//                打印 buildSessionProjection() 里每个条目贡献了哪些 Agent 消息
//   compaction   追加一个 compaction 条目，打印 buildContextEntries() 选中的条目
//   branch       branch() 回到旧条目再追加，形成两条分支；打印树、两条分支的投影、
//                重新 open 文件后的 leaf
//   fork         createBranchedSession()、forkFrom()、continueRecent() 生成和选择的文件
//   edit-branch  实验：context_edit 隐藏一条工具结果，再 branch 回到编辑之前，看它是否重新出现
//   fail         实验：会话文件变成只读后 appendMessage() 抛错，内存里的 leaf 是否回滚
// 环境变量 OUT：会话文件放在哪个目录下（默认 /tmp/pi-lab/m4-out），每次运行在里面新建一个子目录。
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import { pathToFileURL } from "node:url";
const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const { SessionManager, buildSessionProjection } = await import(
	pathToFileURL(`${PI}/packages/coding-agent/src/core/session-manager.ts`).href
);

const scenario = process.argv[2] ?? "persist";
const OUT = process.env.OUT ?? "/tmp/pi-lab/m4-out";
mkdirSync(OUT, { recursive: true });
const runDir = mkdtempSync(join(OUT, `${scenario}-`));
const cwd = join(runDir, "project");
mkdirSync(cwd, { recursive: true });
const sessionDir = join(runDir, "sessions");

// ---- 消息构造 ----
const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
let clock = 1_700_000_000_000;
const system = (text: string) => ({ role: "system", content: text, timestamp: clock++ });
const user = (text: string) => ({ role: "user", content: [{ type: "text", text }], timestamp: clock++ });
const assistant = (text: string, toolCall?: { id: string; name: string; arguments: object }) => ({
	role: "assistant",
	content: toolCall ? [{ type: "text", text }, { type: "toolCall", ...toolCall }] : [{ type: "text", text }],
	api: "faux-api",
	provider: "faux",
	model: "faux-1",
	usage,
	stopReason: toolCall ? "toolUse" : "stop",
	timestamp: clock++,
});
const toolResult = (toolCallId: string, text: string) => ({
	role: "toolResult",
	toolCallId,
	toolName: "read",
	content: [{ type: "text", text }],
	isError: false,
	timestamp: clock++,
});

// ---- 打印辅助 ----
const short = (id: string | null | undefined) => (id == null ? String(id) : id.slice(0, 8));
const text = (content: unknown) => {
	if (typeof content === "string") return JSON.stringify(content);
	if (!Array.isArray(content)) return String(content);
	return content.map((b: any) => (b.type === "text" ? JSON.stringify(b.text) : `<${b.type}>`)).join(" ");
};
const describeMessage = (m: any) => {
	if (m.role === "compactionSummary") return `compactionSummary ${JSON.stringify(m.summary)}`;
	if (m.role === "branchSummary") return `branchSummary ${JSON.stringify(m.summary)}`;
	if (m.role === "custom") return `custom(${m.customType}) ${text(m.content)}`;
	return `${m.role} ${text(m.content)}`;
};
const describeEntry = (e: any) => {
	switch (e.type) {
		case "message":
			return `message/${describeMessage(e.message)}`;
		case "custom":
			return `custom ${e.customType} ${JSON.stringify(e.data)}`;
		case "custom_message":
			return `custom_message ${e.customType} ${text(e.content)}`;
		case "label":
			return `label → ${short(e.targetId)} ${JSON.stringify(e.label)}`;
		case "context_edit":
			return `context_edit → ${short(e.targetId)} ${e.replacement === null ? "null" : text(e.replacement.content)}`;
		case "compaction":
			return `compaction firstKept=${short(e.firstKeptEntryId)} ${JSON.stringify(e.summary)}`;
		case "model_change":
			return `model_change ${e.provider}/${e.modelId}`;
		case "thinking_level_change":
			return `thinking_level_change ${e.thinkingLevel}`;
		default:
			return e.type;
	}
};
const fileLines = (file: string | undefined) => (file && existsSync(file) ? readFileSync(file, "utf8").trim().split("\n").length : 0);
const printProjection = (title: string, projection: any) => {
	console.log(title);
	for (const { sourceEntry, messages } of projection.entries) {
		const contributed = messages.length === 0 ? "（无）" : messages.map(describeMessage).join(" | ");
		console.log(`  ${short(sourceEntry.id)} ${describeEntry(sourceEntry)}  ⇒  ${contributed}`);
	}
	console.log(`  messages: ${projection.messages.map((m: any) => m.role).join(", ")}`);
	console.log(`  model: ${JSON.stringify(projection.model)}  thinkingLevel: ${projection.thinkingLevel}`);
};
const printTree = (sm: any) => {
	const leaf = sm.getLeafId();
	const walk = (nodes: any[], indent: string) => {
		for (const node of nodes) {
			const mark = node.entry.id === leaf ? "  ← leaf" : "";
			console.log(`${indent}${short(node.entry.id)} ${describeEntry(node.entry)}${mark}`);
			walk(node.children, `${indent}  `);
		}
	};
	walk(sm.getTree(), "  ");
};

// 一段和主线场景相同形状的对话：system, user, assistant(toolCall), toolResult, assistant
const appendMainScenario = (sm: any) => {
	const ids: Record<string, string> = {};
	ids.system = sm.appendMessage(system("You are pi."));
	ids.u1 = sm.appendMessage(user("读 README.md，总结成三句话"));
	ids.a1 = sm.appendMessage(assistant("我先读一下 README。", { id: "call_1", name: "read", arguments: { path: "README.md" } }));
	ids.t1 = sm.appendMessage(toolResult("call_1", "# Demo ..."));
	ids.a2 = sm.appendMessage(assistant("这是一个演示项目。"));
	return ids;
};

if (scenario === "persist") {
	const sm = SessionManager.create(cwd, sessionDir);
	const file = sm.getSessionFile();
	console.log(`sessionFile: ${basename(file)}`);
	const step = (label: string) => console.log(`${label.padEnd(36)} 文件存在=${existsSync(file)} 行数=${fileLines(file)} 内存条目=${sm.getEntries().length}`);
	step("create()");
	sm.appendModelChange("faux", "faux-1");
	step("appendModelChange()");
	sm.appendThinkingLevelChange("off");
	step("appendThinkingLevelChange()");
	sm.appendMessage(system("You are pi."));
	step("appendMessage(system)");
	sm.appendMessage(user("读 README.md，总结成三句话"));
	step("appendMessage(user)");
	sm.appendMessage(assistant("这是一个演示项目。"));
	step("appendMessage(assistant)");
	console.log("文件内容（每行截断到 110 个字符）：");
	for (const line of readFileSync(file, "utf8").trim().split("\n")) console.log(`  ${line.slice(0, 110)}`);
} else if (scenario === "project") {
	const sm = SessionManager.inMemory(cwd);
	sm.appendModelChange("faux", "faux-1");
	sm.appendThinkingLevelChange("off");
	const ids = appendMainScenario(sm);
	sm.appendCustomEntry("my-ext", { count: 1 });
	const hint = sm.appendCustomMessageEntry("my-ext", "扩展注入的提示", false);
	sm.appendCustomMessageEntry("my-ext", "第二条提示", true);
	sm.appendLabelChange(ids.u1, "起点");
	const editId = sm.appendContextEdit(ids.t1, { content: "[工具输出已省略]" });
	sm.appendContextEdit(hint, null);
	sm.appendThinkingLevelChange("high");
	console.log(`context_edit 条目：${JSON.stringify(sm.getEntry(editId))}`);
	printProjection("buildSessionProjection()：", sm.buildSessionProjection());
	const raw = sm.getEntry(ids.t1);
	console.log(`原始 toolResult 条目没有被改：${text(raw.message.content)}`);
} else if (scenario === "compaction") {
	const sm = SessionManager.inMemory(cwd);
	const ids = appendMainScenario(sm);
	ids.u2 = sm.appendMessage(user("再看看 package.json"));
	ids.a3 = sm.appendMessage(assistant("好的。"));
	sm.appendMessage(system("工具清单变了"));
	// 保留从 u2 开始的条目，之前的由摘要代替
	const compactionId = sm.appendCompaction("用户让我总结 README，我已经总结过。", ids.u2, 12345);
	sm.appendMessage(user("继续"));
	console.log(`compaction 条目：${JSON.stringify(sm.getEntry(compactionId))}`);
	console.log("getBranch()（整条路径）：");
	for (const e of sm.getBranch()) console.log(`  ${short(e.id)} ${describeEntry(e)}`);
	console.log("buildContextEntries()（压缩之后选中的条目）：");
	for (const e of sm.buildContextEntries()) console.log(`  ${short(e.id)} ${describeEntry(e)}`);
	printProjection("buildSessionProjection()：", sm.buildSessionProjection());
} else if (scenario === "branch") {
	const sm = SessionManager.create(cwd, sessionDir);
	const ids = appendMainScenario(sm);
	ids.u2 = sm.appendMessage(user("用英文再说一遍"));
	ids.a3 = sm.appendMessage(assistant("This is a demo project."));
	sm.branch(ids.a2);
	console.log(`branch(${short(ids.a2)}) 之后 leaf=${short(sm.getLeafId())}`);
	ids.u3 = sm.appendMessage(user("用日文再说一遍"));
	ids.a4 = sm.appendMessage(assistant("これはデモです。"));
	console.log("getTree()：");
	printTree(sm);
	printProjection("当前分支 buildSessionProjection()：", sm.buildSessionProjection());
	printProjection(
		`旧分支 buildSessionProjection(entries, ${short(ids.a3)})：`,
		buildSessionProjection(sm.getEntries(), ids.a3),
	);
	const file = sm.getSessionFile();
	console.log(`文件行数=${fileLines(file)}（1 行 session 头 + ${sm.getEntries().length} 个条目，两条分支都在同一个文件里）`);
	const reopened = SessionManager.open(file);
	console.log(`open() 之后 leaf=${short(reopened.getLeafId())}（文件最后一行）`);
	reopened.branch(ids.a3);
	reopened.appendMessage(user("回到英文分支继续"));
	const parentId = SessionManager.open(file).getLeafEntry().parentId;
	console.log(`再次 open() 之后 leaf 的父条目=${short(parentId)}，是旧分支的最后一条：${parentId === ids.a3}`);
} else if (scenario === "fork") {
	const sm = SessionManager.create(cwd, sessionDir);
	const ids = appendMainScenario(sm);
	const source = sm.getSessionFile();
	console.log(`原文件: ${basename(source)} 条目=${sm.getEntries().length}`);
	const branched = SessionManager.open(source);
	const branchedFile = branched.createBranchedSession(ids.t1);
	const branchedHeader = branched.getHeader();
	console.log(`createBranchedSession(${short(ids.t1)}) → ${basename(branchedFile)}`);
	console.log(`  条目=${branched.getEntries().length} leaf=${short(branched.getLeafId())} parentSession=${basename(branchedHeader.parentSession)}`);
	console.log(`  条目 id 和原文件相同：${branched.getEntries().every((e: any) => sm.getEntry(e.id)?.id === e.id)}`);
	const otherCwd = join(runDir, "other-project");
	mkdirSync(otherCwd, { recursive: true });
	const forked = SessionManager.forkFrom(source, otherCwd, join(runDir, "other-sessions"));
	const forkedHeader = forked.getHeader();
	console.log(`forkFrom(原文件, other-project) → ${basename(forked.getSessionFile())}`);
	console.log(`  条目=${forked.getEntries().length} cwd=${basename(forkedHeader.cwd)} parentSession=${basename(forkedHeader.parentSession)}`);
	const recent = SessionManager.continueRecent(cwd, sessionDir);
	console.log(`continueRecent(project) → ${basename(recent.getSessionFile())}（mtime 最新的文件）`);
	const empty = SessionManager.continueRecent(join(runDir, "empty-project"), join(runDir, "empty-sessions"));
	console.log(`continueRecent(空目录) → 新会话 ${basename(empty.getSessionFile())}，文件存在=${existsSync(empty.getSessionFile())}`);
} else if (scenario === "edit-branch") {
	const sm = SessionManager.inMemory(cwd);
	const ids = appendMainScenario(sm);
	sm.appendContextEdit(ids.t1, null);
	const edited = sm.appendMessage(user("继续"));
	printProjection("编辑之后（leaf 在 context_edit 之后）：", sm.buildSessionProjection());
	sm.branch(ids.a2);
	printProjection(`branch(${short(ids.a2)}) 回到编辑之前：`, sm.buildSessionProjection());
	console.log(`编辑后的分支仍在会话里：leaf ${short(edited)} 的路径长度=${sm.getBranch(edited).length}`);
} else if (scenario === "fail") {
	const sm = SessionManager.create(cwd, sessionDir);
	appendMainScenario(sm);
	const file = sm.getSessionFile();
	const before = sm.getLeafId();
	console.log(`写入前 leaf=${short(before)} 内存条目=${sm.getEntries().length} 文件行数=${fileLines(file)}`);
	chmodSync(file, 0o444);
	try {
		sm.appendMessage(user("这条写不进去"));
	} catch (error: any) {
		console.log(`appendMessage 抛错：${error.code}`);
	}
	console.log(`抛错后 leaf=${short(sm.getLeafId())} 内存条目=${sm.getEntries().length} 文件行数=${fileLines(file)}`);
	chmodSync(file, 0o644);
	const id = sm.appendMessage(user("恢复写权限后再写"));
	console.log(`恢复后新条目的 parent=${short(sm.getEntry(id).parentId)} 文件行数=${fileLines(file)}`);
} else {
	throw new Error(`未知场景 ${scenario}`);
}
