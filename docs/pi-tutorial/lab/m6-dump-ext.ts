// M6 实验用的扩展：在 before_agent_start 事件里打印 AgentSession 准备好的系统提示词选项。
// 用法：和 faux-ext.ts 一起加载，例如
//   M6_DUMP=1 PI=/path/to/pi ./run.sh -e ./m6-dump-ext.ts -p "读 README.md，总结成三句话"
// 环境变量：
//   M6_DUMP=1      把 event.systemPromptOptions（NormalizedBuildSystemPromptOptions）以 JSON 打印到 stderr
//   M6_GUIDELINE   非空时，往 systemPromptOptions.promptGuidelines 追加这一条规则
//   M6_SECTION     形如 name=text，非空时往 systemPromptOptions.sections 加一个自定义 section
// 打印的每一行都以 [m6] 开头。
export default function (pi: any) {
	pi.on("before_agent_start", (event: any) => {
		const options = event.systemPromptOptions;
		const guideline = process.env.M6_GUIDELINE;
		if (guideline) options.promptGuidelines.push(guideline);
		const section = process.env.M6_SECTION;
		if (section) {
			const index = section.indexOf("=");
			options.sections[section.slice(0, index)] = section.slice(index + 1);
		}
		if (process.env.M6_DUMP === "1") {
			const json = JSON.stringify(options, null, 2);
			process.stderr.write(`${json.split("\n").map((line) => `[m6] ${line}`).join("\n")}\n`);
		}
	});
}
