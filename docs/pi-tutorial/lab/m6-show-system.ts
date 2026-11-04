// M6 实验：查看会话文件里的 system 消息，以及重放后模型"当前拥有"的系统提示词。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m6-show-system.ts <session.jsonl> [section 名...]
// 输出：
//   1. 每条 system 消息：它在会话文件里的 id、sections 的名字和长度（null 表示删除）、toolsAdded/toolsRemoved
//   2. 用 pi-ai 的 getCurrentSystemMessage() 重放所有 system 消息后的 sections 顺序
//   3. 命令行里点名的 section，打印重放后的全文
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const { getCurrentSystemMessage } = await import(pathToFileURL(`${PI}/packages/ai/src/index.ts`).href);

const [file, ...wanted] = process.argv.slice(2);
if (!file) throw new Error("用法：m6-show-system.ts <session.jsonl> [section 名...]");

const messages: any[] = [];
let index = 0;
for (const line of readFileSync(file, "utf8").trim().split("\n")) {
	const entry = JSON.parse(line);
	if (entry.type !== "message") continue;
	messages.push(entry.message);
	const message = entry.message;
	if (message.role !== "system") continue;
	index++;
	const sections = Object.entries(message.sections ?? {})
		.map(([name, text]) => (text === null ? `${name}=null` : `${name}(${(text as string).length})`))
		.join(" ");
	const added = (message.toolsAdded ?? []).map((t: any) => t.name).join(",");
	const removed = (message.toolsRemoved ?? []).map((t: any) => t.name).join(",");
	console.log(`system #${index} id=${entry.id} content=${JSON.stringify(message.content)}`);
	console.log(`  sections: ${sections || "(无)"}`);
	if (added) console.log(`  toolsAdded: ${added}`);
	if (removed) console.log(`  toolsRemoved: ${removed}`);
}

const current = getCurrentSystemMessage(messages);
console.log(`重放后 sections 顺序: ${Object.keys(current?.sections ?? {}).join(",")}`);
console.log(`重放后 tools: ${(current?.toolsAdded ?? []).map((t: any) => t.name).join(",")}`);
for (const name of wanted) {
	console.log(`---- ${name} ----`);
	console.log(current?.sections?.[name] ?? "(不存在)");
}
