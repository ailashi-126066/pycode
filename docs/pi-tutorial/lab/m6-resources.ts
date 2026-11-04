// M6 实验：单独运行 DefaultResourceLoader，打印它发现的资源。
//
// 用法（PI 指向 pi 仓库）：
//   PI=/path/to/pi node --conditions=source --experimental-strip-types m6-resources.ts <cwd> <agentDir> [trusted|untrusted]
// 第三个参数决定 SettingsManager 的 projectTrusted，默认 trusted。
// 不加载扩展（noExtensions），只看 AGENTS.md、skills、prompt templates、SYSTEM.md / APPEND_SYSTEM.md。
import { pathToFileURL } from "node:url";

const PI = process.env.PI;
if (!PI) throw new Error("请设置 PI=pi 仓库路径");
const core = `${PI}/packages/coding-agent/src/core`;
const { SettingsManager } = await import(pathToFileURL(`${core}/settings-manager.ts`).href);
const { DefaultResourceLoader } = await import(pathToFileURL(`${core}/resource-loader.ts`).href);

const [cwd, agentDir, trust = "trusted"] = process.argv.slice(2);
if (!cwd || !agentDir) throw new Error("用法：m6-resources.ts <cwd> <agentDir> [trusted|untrusted]");

const settingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted: trust === "trusted" });
const loader = new DefaultResourceLoader({ cwd, agentDir, settingsManager, noExtensions: true });
await loader.reload();

console.log(`projectTrusted: ${settingsManager.isProjectTrusted()}`);
console.log(`agentsFiles: ${JSON.stringify(loader.getAgentsFiles().agentsFiles)}`);
const { skills, diagnostics: skillDiagnostics } = loader.getSkills();
console.log(`skills: ${JSON.stringify(skills, null, 2)}`);
if (skillDiagnostics.length > 0) console.log(`skill diagnostics: ${JSON.stringify(skillDiagnostics)}`);
const { prompts, diagnostics: promptDiagnostics } = loader.getPrompts();
console.log(`prompts: ${JSON.stringify(prompts, null, 2)}`);
if (promptDiagnostics.length > 0) console.log(`prompt diagnostics: ${JSON.stringify(promptDiagnostics)}`);
console.log(`systemPrompt: ${JSON.stringify(loader.getSystemPrompt())}`);
console.log(`appendSystemPrompt: ${JSON.stringify(loader.getAppendSystemPrompt())}`);
