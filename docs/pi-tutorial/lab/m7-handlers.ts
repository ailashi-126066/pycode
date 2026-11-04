// M7 实验：几种 handler 写法，用环境变量 M7_VARIANT 选择。
//
// 用法（和 m7-faux-bash.ts 一起加载）：
//   M7_VARIANT=rewrite EXT=$PWD/m7-faux-bash.ts PI=/path/to/pi ./run.sh -e "$PWD/m7-handlers.ts" -p "清理构建目录"
// 变体：
//   rewrite  tool_call：把 bash 命令里的 "rm -rf build" 原地改成 "ls"（修改 event.input，不返回结果）
//   throw    tool_call 和 tool_execution_start 的 handler 各抛一个异常
//   trim     context：只保留最后 2 条非 system 消息（只影响发给模型的请求）
// 每个 handler 被调用时往 stderr 打印一行 [m7-handlers]。
export default function (pi: any) {
	const variant = process.env.M7_VARIANT;
	const log = (line: string) => process.stderr.write(`[m7-handlers] ${line}\n`);

	if (variant === "rewrite") {
		pi.on("tool_call", (event: any) => {
			log(`tool_call 收到 ${JSON.stringify(event.input)}`);
			if (event.toolName === "bash" && typeof event.input.command === "string") {
				event.input.command = event.input.command.replace("rm -rf build", "ls");
				log(`tool_call 改为 ${JSON.stringify(event.input)}`);
			}
		});
	} else if (variant === "throw") {
		pi.on("tool_execution_start", () => {
			log("tool_execution_start 抛异常");
			throw new Error("tool_execution_start handler 出错");
		});
		pi.on("tool_call", () => {
			log("tool_call 抛异常");
			throw new Error("tool_call handler 出错");
		});
	} else if (variant === "trim") {
		pi.on("context", (event: any) => {
			const kept = event.messages.slice(-2);
			log(`context 收到 [${event.messages.map((m: any) => m.role).join(",")}]，返回 [${kept.map((m: any) => m.role).join(",")}]`);
			return { messages: kept };
		});
	} else {
		throw new Error(`请设置 M7_VARIANT=rewrite|throw|trim，当前是 ${variant}`);
	}
}
