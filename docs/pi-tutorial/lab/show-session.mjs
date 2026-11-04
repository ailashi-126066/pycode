// 把会话 JSONL 文件打印成一行一个条目，显示 id、parentId 和内容摘要。
// 用法：node show-session.mjs <session.jsonl>
import { readFileSync } from "node:fs";

for (const line of readFileSync(process.argv[2], "utf8").trim().split("\n")) {
	const entry = JSON.parse(line);
	const message = entry.message;
	let summary;
	if (message) {
		summary = `${message.role.padEnd(10)} ${JSON.stringify(message.content).slice(0, 90)}`;
		if (message.sections) summary += ` sections=${Object.keys(message.sections).join(",")}`;
		if (message.toolsAdded) summary += ` toolsAdded=${message.toolsAdded.map((t) => t.name).join(",")}`;
		if (message.stopReason) summary += ` stop=${message.stopReason}`;
	} else {
		const { type, id, parentId, timestamp, ...rest } = entry;
		summary = JSON.stringify(rest).slice(0, 100);
	}
	const parent = entry.parentId === undefined ? "-" : String(entry.parentId);
	console.log(`${entry.type.padEnd(22)} id=${String(entry.id).slice(0, 8).padEnd(8)} parent=${parent.slice(0, 8).padEnd(8)} ${summary}`);
}
