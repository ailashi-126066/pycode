// 把 `pi --mode json` 的输出压缩成一行一个事件，连续重复的事件合并为 "xN"。
// 用法：node summarize-events.mjs events.jsonl
import { readFileSync } from "node:fs";

const lines = readFileSync(process.argv[2], "utf8")
	.split("\n")
	.filter((line) => line.startsWith("{"));

const labels = lines.map((line) => {
	const event = JSON.parse(line);
	let label = event.type;
	if (event.message) label += ` (${event.message.role})`;
	if (event.assistantMessageEvent) label += ` [${event.assistantMessageEvent.type}]`;
	if (event.toolName) label += ` ${event.toolName}`;
	return label;
});

let previous;
let count = 0;
for (const label of [...labels, undefined]) {
	if (label === previous) {
		count++;
		continue;
	}
	if (previous) console.log(count > 1 ? `${previous} x${count}` : previous);
	previous = label;
	count = 1;
}
console.log(`共 ${lines.length} 行（含第一行 session 头）`);
