// M6 实验：查看 SettingsManager 怎么合并全局设置和项目设置。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m6-settings.ts <cwd> <agentDir>
// 分别用 projectTrusted=false 和 true 创建 SettingsManager，打印：
//   全局设置、项目设置、合并后的 defaultTools 和 compaction 字段，以及 getDefaultTools() 的结果。
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const { SettingsManager } = await import(
	pathToFileURL(`${PI}/packages/coding-agent/src/core/settings-manager.ts`).href
);

const [cwd, agentDir] = process.argv.slice(2);
if (!cwd || !agentDir) throw new Error("用法：m6-settings.ts <cwd> <agentDir>");

for (const projectTrusted of [false, true]) {
	const manager = SettingsManager.create(cwd, agentDir, { projectTrusted });
	const merged = manager.getSettings();
	console.log(`projectTrusted=${projectTrusted}`);
	console.log(`  global:       ${JSON.stringify(manager.getGlobalSettings())}`);
	console.log(`  project:      ${JSON.stringify(manager.getProjectSettings())}`);
	console.log(`  merged:       ${JSON.stringify({ defaultTools: merged.defaultTools, compaction: merged.compaction })}`);
	console.log(`  getDefaultTools(): ${JSON.stringify(manager.getDefaultTools())}`);
}
