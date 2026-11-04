// M9 实验：以 RPC 模式启动 pi（假模型），通过 stdin 发命令，打印 stdout 上收到的每一行。
//
// 用法（在 lab 目录运行，PI 指向 pi 仓库）：
//   PI=/path/to/pi WORK=/tmp/pi-lab/m9-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m9-agent node m9-rpc.mjs
//
// 依次发送：
//   1. 一行不是 JSON 的文本         → 预期收到 command=parse 的错误响应
//   2. {"id":"s1","type":"get_state"}
//   3. {"id":"p1","type":"prompt",...}  主线场景的提示词
//   4. 收到 agent_settled 后：get_state、get_last_assistant_text、一个不存在的命令
//   5. 关闭 stdin                   → pi 收到 end 事件后退出
// 输出：→ 表示发给 pi 的行，← 表示 pi 写到 stdout 的行。连续重复的事件合并为 "xN"。
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const labDir = dirname(fileURLToPath(import.meta.url));
const child = spawn(join(labDir, "run.sh"), ["--mode", "rpc"], { stdio: ["pipe", "pipe", "pipe"], env: process.env });

const t0 = Date.now();
const ms = () => String(Date.now() - t0).padStart(5);
let previous;
let count = 0;
const flush = () => {
	if (previous) console.log(count > 1 ? `${previous} x${count}` : previous);
	previous = undefined;
	count = 0;
};
const print = (line) => {
	if (line === previous) {
		count++;
		return;
	}
	flush();
	previous = line;
	count = 1;
};

const send = (text) => {
	flush();
	console.log(`${ms()} → ${text.length > 90 ? `${text.slice(0, 90)}…` : text}`);
	child.stdin.write(`${text}\n`);
};

const describe = (obj) => {
	if (obj.type === "response" && obj.command === "get_state" && obj.success) {
		// RpcSessionState 很长，只挑几个字段
		const { model, isStreaming, messageCount, pendingMessageCount, sessionFile } = obj.data;
		const state = { model: `${model?.provider}/${model?.id}`, isStreaming, messageCount, pendingMessageCount };
		state.sessionFile = sessionFile ? sessionFile.split("/").slice(-1)[0].replace(/^.*_/, "…_") : sessionFile;
		return `← response id=${obj.id} command=get_state success=true ${JSON.stringify(state)}`;
	}
	if (obj.type === "response") {
		const body = obj.success ? JSON.stringify(obj.data ?? null) : `error=${JSON.stringify(obj.error)}`;
		const short = body.length > 100 ? `${body.slice(0, 100)}…` : body;
		return `← response id=${obj.id ?? "-"} command=${obj.command} success=${obj.success} ${short}`;
	}
	let label = `← ${obj.type}`;
	if (obj.message) label += ` (${obj.message.role})`;
	if (obj.assistantMessageEvent) label += ` [${obj.assistantMessageEvent.type}]`;
	if (obj.toolName) label += ` ${obj.toolName}`;
	return label;
};

let buffer = "";
let settledOnce = false;
child.stdout.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
	buffer += chunk;
	let index;
	while ((index = buffer.indexOf("\n")) !== -1) {
		const line = buffer.slice(0, index);
		buffer = buffer.slice(index + 1);
		if (!line.startsWith("{")) {
			// pi-test.sh 自己 echo 的 "Running without API keys..."，不是 pi 的输出
			flush();
			console.log(`      （非 JSON 行，来自 pi-test.sh）${line}`);
			continue;
		}
		const obj = JSON.parse(line);
		// 只有响应和 agent_settled 带时间，事件按行合并
		if (obj.type === "response" || obj.type === "agent_settled") {
			flush();
			console.log(`${ms()} ${describe(obj)}`);
		} else {
			print(`      ${describe(obj)}`);
		}
		if (obj.type === "agent_settled" && !settledOnce) {
			settledOnce = true;
			send('{"id":"s2","type":"get_state"}');
			send('{"id":"t1","type":"get_last_assistant_text"}');
			send('{"id":"x1","type":"no_such_command"}');
			setTimeout(() => child.stdin.end(), 200);
		}
	}
});
child.stderr.setEncoding("utf8");
child.stderr.on("data", (chunk) => process.stderr.write(`[stderr] ${chunk}`));
child.on("exit", (code) => {
	flush();
	console.log(`${ms()} pi 退出，exit code=${code}`);
});

// 等 pi 启动完成再发命令：启动期间 stdin 的数据会留在管道里，所以这里不需要等待
send("this is not json");
send('{"id":"s1","type":"get_state"}');
send('{"id":"p1","type":"prompt","message":"读 README.md，总结成三句话"}');
