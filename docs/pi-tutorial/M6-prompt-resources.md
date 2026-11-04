# M6 提示词与资源：系统提示词怎么拼出来

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

M6 回答一个问题：主线场景里第一条 `system` 消息的 `sections` 是从哪来的。

这件事分三层：

- **拼装**：`core/system-prompt.ts` 是一组纯函数。输入一份选项（`BuildSystemPromptOptions`：工具名、工具的一句话说明、规则、AGENTS.md 内容、skills、cwd），输出一组按名字排好序的段落（`SystemPromptSections`）。
- **资源发现**：`core/resource-loader.ts` 在磁盘上找这些选项的原料：AGENTS.md（上下文文件）、skills、prompt templates、`SYSTEM.md` 和 `APPEND_SYSTEM.md`。
- **设置与信任**：`core/settings-manager.ts` 合并全局设置（`<agentDir>/settings.json`）和项目设置（`<cwd>/.pi/settings.json`）。项目目录里的设置和资源只有在项目被信任时才生效（`core/project-trust.ts`、`core/trust-manager.ts`）。

AgentSession（M8）把三层接起来：启动时用资源和工具算出一份"基础选项"，每次运行和每轮之间重新拼一次段落，和 transcript 里模型已经拥有的段落做差分。只有变化的段落会作为一条新的 `system` 消息进入 transcript。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M6 出现在三处：

```
main()                                                       [M9]
├─ SettingsManager.create(cwd, agentDir, { projectTrusted })  [M6] ← §4.14
├─ createAgentSessionServices()                               [M8]
│   └─ DefaultResourceLoader.reload()                         [M6] ← §4.5
│       ├─ resolveProjectTrust()  项目信任                     [M6] ← §4.15
│       ├─ packageManager.resolve() → addAutoDiscoveredResources() ← §4.6
│       ├─ loadSkills() / loadPromptTemplates()               ← §4.8、§4.10
│       ├─ loadProjectContextFiles()  AGENTS.md               ← §4.7
│       └─ SYSTEM.md / APPEND_SYSTEM.md                       ← §4.9
└─ new AgentSession()                                         [M8]
    └─ _buildRuntime() → _refreshToolRegistry()               收集工具的 snippet / guidelines ← §4.3
        └─ _setActiveTools() → _rebuildSystemPrompt()         基础选项 ← §4.4
session.prompt(text)                                          [M8]
├─ _expandSkillCommand() / expandPromptTemplate()             /skill:x、/模板 展开 ← §4.10
└─ _prepareRun()                                              [M8] ← §4.12
    ├─ emitBeforeAgentStart()  扩展可以改选项                   [M7]
    └─ _preparePromptAndToolLoadout()
        ├─ buildSystemPromptSections()                        ← 本模块入口 §4.1、§4.2
        └─ diffSystemPromptSections()                         ← §4.11
            → system 补丁消息随 user 消息交给 agent.prompt()   [M2]
prepareNextTurn（第 2 轮起）→ _installAgentNextTurnRefresh    ← §4.13
```

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/coding-agent/src/core/system-prompt.ts` | 228 | 选项类型、`buildSystemPromptSections`、`buildRules`、`diffSystemPromptSections` | 精读，全部 |
| `packages/coding-agent/src/core/resource-loader.ts` | 1319 | `ResourceLoader` 接口、`DefaultResourceLoader.reload()`、AGENTS.md 查找、`SYSTEM.md` 查找 | 精读 181–302、541–710、880–929、1245–1271；扩展加载部分 → M7 |
| `packages/coding-agent/src/core/skills.ts` | 505 | `Skill` 类型、目录扫描、frontmatter 校验、`formatSkillsForPrompt` | 精读 74–81、173–388 |
| `packages/coding-agent/src/core/prompt-templates.ts` | 325 | `PromptTemplate` 类型、参数替换、`expandPromptTemplate` | 精读 |
| `packages/coding-agent/src/core/settings-manager.ts` | 1456 | 设置的合并规则、加载、信任开关 | 精读 61–147、272–531；其余 getter/setter 略 |
| `packages/coding-agent/src/core/project-trust.ts` | 97 | `resolveProjectTrusted`：决定项目是否被信任 | 精读 |
| `packages/coding-agent/src/core/trust-manager.ts` | 254 | 哪些项目资源需要信任、`trust.json` 的读写 | 精读 30–39、186–254 |
| `packages/coding-agent/src/core/package-manager.ts` | 2906 | `addAutoDiscoveredResources`：自动发现的资源目录 | 只读 491–510、2559–2723；包安装略 |
| `packages/coding-agent/src/core/agent-session.ts` | 4479 | `_rebuildSystemPrompt`、`_preparePromptAndToolLoadout` 等 | 只读本文引用的函数，其余 → M8 |
| `packages/ai/src/utils/transcript.ts` | 263 | `getCurrentSystemMessage`：重放 system 消息 | 只读 73–96，其余 → M1 |

行数用 `wc -l` 统计。

### 1.4 读完能回答的问题

1. 系统提示词里的 `tools` 和 `rules` 两段，每一行分别来自哪里？
2. 会话进行到一半时修改了 AGENTS.md，pi 会怎么告诉模型？会重发整个系统提示词吗？
3. 全局目录、父目录和项目目录都有 AGENTS.md 时，模型看到几份？什么顺序？
4. 项目的 `.pi/settings.json` 和全局 `settings.json` 都设置了 `defaultTools`，结果是什么？
5. `pi -p` 在一个带 `.pi/` 目录的陌生项目里运行时，项目里的 skills 和设置会生效吗？

---

## 2. 先跑一次

### 2.1 主线场景的系统提示词

主线场景（README §5）里，第一条 `system` 消息有五段：`preamble,tools,rules,docs,cwd`。[`lab/m6-show-system.ts`](lab/m6-show-system.ts) 读会话文件，列出每条 `system` 消息带的段落和长度，再用 pi-ai 的 `getCurrentSystemMessage()` 重放全部 `system` 消息，打印指定段落的全文：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m6-work-1 PI_CODING_AGENT_DIR=/tmp/pi-lab/m6-agent-1 PI_TRACE=1 ./run.sh
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t /tmp/pi-lab/m6-agent-1/sessions/*/*.jsonl | head -1)" preamble tools rules cwd
```

```
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
[faux] 第 2 次请求，收到 4 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

```
system #1 id=53c9f8eb content=""
  sections: preamble(126) tools(274) rules(589) docs(1179) cwd(42)
  toolsAdded: read,bash,edit,write
重放后 sections 顺序: preamble,tools,rules,docs,cwd
重放后 tools: read,bash,edit,write
---- preamble ----
You are a coding assistant running inside pi, a coding agent harness. Help users inspect files, run commands, and modify code.
---- tools ----
<tools>
- read: Read file contents
- bash: Execute bash commands (ls, grep, find, etc.)
- edit: Make precise file edits with exact text replacement, including multiple disjoint edits in one call
- write: Create or overwrite files

Additional tools may be available.
</tools>
---- rules ----
<rules>
- Use bash for file operations like ls, rg, find
- Use read to examine files instead of cat or sed.
- You can inspect PI_* environment variables for current model and session details.
- Use edit for targeted changes. Batch changes to one file in a single call.
- Each edits[].oldText must exactly and uniquely match the original file. Keep matches short and non-overlapping; merge nearby changes without including large unchanged regions.
- Use write only for new files or complete rewrites.
- Be concise in your responses
- Show file paths clearly when working with files
</rules>
---- cwd ----
<cwd>
/private/tmp/pi-lab/m6-work-1
</cwd>
```

可以看到：

- `content` 是空字符串，提示词全部放在 `sections` 里。`preamble` 是裸文本，其余段落都用同名标签包起来（`<tools>…</tools>`）。
- `tools` 段每行一个工具，`rules` 段的规则来自各个工具和几条固定规则。§4.2 和 §4.3 会逐行对上来源。
- `cwd` 是 `/private/tmp/...`：macOS 上 `/tmp` 是 `/private/tmp` 的符号链接，pi 用的是进程的真实工作目录。

### 2.2 加上 AGENTS.md、skill 和 prompt template

在一个新的配置目录里放三样资源：全局 AGENTS.md、一个 skill、一个 prompt template。

```bash
A=/tmp/pi-lab/m6-agent-2; W=/tmp/pi-lab/m6-work-2
mkdir -p $A/skills/review $A/prompts $W
printf '# 全局约定\n\n回答用中文。\n' > $A/AGENTS.md
printf -- '---\nname: review\ndescription: Review a diff and list risky changes.\n---\n\n# Review\n\n1. Run git diff.\n2. List risky changes.\n' > $A/skills/review/SKILL.md
printf -- '---\ndescription: 总结一个文件\nargument-hint: <file>\n---\n读 $1，总结成${2:-三}句话\n' > $A/prompts/sum.md
```

然后用 prompt template 的名字 `/sum README.md` 代替原来的提示词。[`lab/m6-dump-ext.ts`](lab/m6-dump-ext.ts) 是一个扩展，`M6_DUMP=1` 时在 `before_agent_start` 事件里打印 AgentSession 准备好的选项（§3.1 用它的输出）。这里先只看 trace：

```bash
WORK=/tmp/pi-lab/m6-work-2 PI_CODING_AGENT_DIR=/tmp/pi-lab/m6-agent-2 PI_TRACE=1 M6_DUMP=1 ./run.sh -e "$PWD/m6-dump-ext.ts" -p "/sum README.md" 2>&1 | grep -v '^\[m6\]'
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t /tmp/pi-lab/m6-agent-2/sessions/*/*.jsonl | head -1)" project_context skills
```

（`-e` 要用绝对路径，因为 `run.sh` 会先 `cd` 到 `$WORK`。）

```
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,project_context,skills,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
[faux] 第 2 次请求，收到 4 条消息:
  system     sections=[preamble,tools,rules,docs,project_context,skills,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

```
system #1 id=0c1ec893 content=""
  sections: preamble(126) tools(274) rules(589) docs(1179) project_context(186) skills(506) cwd(42)
  toolsAdded: read,bash,edit,write
重放后 sections 顺序: preamble,tools,rules,docs,project_context,skills,cwd
重放后 tools: read,bash,edit,write
---- project_context ----
<project_context>
Project-specific instructions and guidelines:

<project_instructions path="/tmp/pi-lab/m6-agent-2/AGENTS.md">
# 全局约定

回答用中文。

</project_instructions>
</project_context>
---- skills ----
<skills>
The following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
Resolve relative paths against the skill's directory (parent of SKILL.md). Use absolute paths in tool calls.

<available_skills>
  <skill>
    <name>review</name>
    <description>Review a diff and list risky changes.</description>
    <location>/tmp/pi-lab/m6-agent-2/skills/review/SKILL.md</location>
  </skill>
</available_skills>
</skills>
```

三样资源去了三个不同的地方：

- AGENTS.md 的全文进了 `project_context` 段。
- skill 只有名字、描述和路径进了 `skills` 段。正文不在系统提示词里，模型需要时用 read 工具去读。
- prompt template 没有进系统提示词。它在 `session.prompt()` 里把用户输入 `/sum README.md` 展开成了 `读 README.md，总结成三句话`（`$1` 换成 `README.md`，`${2:-三}` 没有第 2 个参数，取默认值"三"）。模型收到的 user 消息就是展开后的文本。

---

## 3. 数据结构

### 3.1 BuildSystemPromptOptions：拼装的输入

源码：[`packages/coding-agent/src/core/system-prompt.ts:9-37`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/system-prompt.ts#L9-L37)

```ts
export interface BuildSystemPromptOptions {
	/** Custom system prompt (replaces the default prefix). */
	customPrompt?: string;
	/** Exact full prompt replacement set by a before_agent_start handler. */
	forceSystemPrompt?: string;
	/** Tools to include in prompt. Default: [read, bash, edit, write]. */
	selectedTools?: string[];
	/**
	 * Selected tools whose declarations requests leave out (`prepareLoadout` hidden declarations).
	 * They are reachable only through another tool, so the tool list and rules leave them out too.
	 */
	hiddenTools?: string[];
	/** Optional one-line tool snippets keyed by tool name. */
	toolSnippets?: Record<string, string>;
	/** Guideline bullets contributed by each tool, keyed by tool name. */
	toolGuidelines?: Record<string, string[]>;
	/** Additional guideline bullets appended to the default system prompt rules. */
	promptGuidelines?: string[];
	/** Text appended from user configuration before project context, skills, and cwd. */
	appendSystemPrompt?: string;
	/** Additional XML-wrapped prompt sections keyed by tag name. */
	sections?: Record<string, string>;
	/** Working directory. */
	cwd: string;
	/** Pre-loaded context files. */
	contextFiles?: Array<{ path: string; content: string }>;
	/** Pre-loaded skills. */
	skills?: Skill[];
}
```

| 字段 | 来源 | 进哪一段 |
|---|---|---|
| `customPrompt` | `--system-prompt` 或 `SYSTEM.md`（§4.9） | 替换 `preamble`，同时去掉 `tools`、`rules`、`docs` |
| `forceSystemPrompt` | 扩展的 `before_agent_start` 返回 `systemPrompt`（§4.12） | 不进 transcript，请求时整体替换（§4.13） |
| `selectedTools` | 当前激活的工具名 | 决定 `tools` 段列哪些工具、`rules` 段收哪些工具的规则 |
| `hiddenTools` | 工具的 `prepareLoadout` hook（→ M8） | 从 `tools`、`rules` 里去掉 |
| `toolSnippets` | 每个工具定义的 `promptSnippet` | `tools` 段每行冒号后的文字 |
| `toolGuidelines` | 每个工具定义的 `promptGuidelines` | `rules` 段 |
| `promptGuidelines` | 扩展在 `before_agent_start` 里追加 | `rules` 段 |
| `appendSystemPrompt` | `--append-system-prompt` 或 `APPEND_SYSTEM.md` | `addendum` 段 |
| `sections` | 扩展在 `before_agent_start` 里追加 | 自定义段，放在最后 |
| `cwd` | 会话的工作目录 | `cwd` 段 |
| `contextFiles` | AGENTS.md / CLAUDE.md（§4.7） | `project_context` 段 |
| `skills` | skill 目录扫描结果（§4.8） | `skills` 段 |

`normalizeBuildSystemPromptOptions`（`packages/coding-agent/src/core/system-prompt.ts:60`）把可选字段补成空数组、空对象，并把每个数组和对象复制一份。补全后的类型叫 `NormalizedBuildSystemPromptOptions`（`packages/coding-agent/src/core/system-prompt.ts:39`）。复制是因为这份对象会交给扩展修改（§4.12），不能让扩展改到 AgentSession 自己保存的那份。

**真实实例。** §2.2 那次运行里，`m6-dump-ext.ts` 打印的 `event.systemPromptOptions`（每行开头的 `[m6] ` 已去掉，中间省略了部分工具）：

```bash
WORK=/tmp/pi-lab/m6-work-2 PI_CODING_AGENT_DIR=/tmp/pi-lab/m6-agent-2 M6_DUMP=1 ./run.sh -e "$PWD/m6-dump-ext.ts" -p "/sum README.md" 2>&1 | grep '^\[m6\]'
```

```
{
  "selectedTools": [
    "read",
    "bash",
    "edit",
    "write"
  ],
  "hiddenTools": [],
  "toolSnippets": {
    "read": "Read file contents",
    "bash": "Execute bash commands (ls, grep, find, etc.)",
    "powershell": "Execute PowerShell commands",
    "edit": "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "write": "Create or overwrite files",
    "grep": "Search file contents for patterns (respects .gitignore)",
    "find": "Find files by glob pattern (respects .gitignore)",
    "ls": "List directory contents",
    "codemode": "Run JavaScript that calls other tools",
    "tool_search": "Search for tools that are not loaded yet and load the matches"
  },
  "toolGuidelines": {
    "read": [
      "Use read to examine files instead of cat or sed."
    ],
…省略 bash、powershell、edit、write、codemode…
  },
  "promptGuidelines": [],
  "appendSystemPrompt": "",
  "sections": {},
  "cwd": "/private/tmp/pi-lab/m6-work-2",
  "contextFiles": [
    {
      "path": "/tmp/pi-lab/m6-agent-2/AGENTS.md",
      "content": "# 全局约定\n\n回答用中文。\n"
    }
  ],
  "skills": [
    {
      "name": "review",
      "description": "Review a diff and list risky changes.",
      "filePath": "/tmp/pi-lab/m6-agent-2/skills/review/SKILL.md",
      "baseDir": "/tmp/pi-lab/m6-agent-2/skills/review",
      "sourceInfo": {
        "path": "/tmp/pi-lab/m6-agent-2/skills/review/SKILL.md",
        "source": "auto",
        "scope": "user",
        "origin": "top-level",
        "baseDir": "/tmp/pi-lab/m6-agent-2"
      },
      "disableModelInvocation": false
    }
  ]
}
```

注意 `toolSnippets` 有 10 个工具，`selectedTools` 只有 4 个。snippet 是从所有已注册的工具收集的，`tools` 段只列激活的那几个（§4.1、§4.3）。另外 `customPrompt` 和 `forceSystemPrompt` 是 `undefined`，`JSON.stringify` 没有输出它们。

### 3.2 SystemPromptSections 和 SystemMessage.sections：拼装的输出

源码：[`packages/coding-agent/src/core/system-prompt.ts:51-56`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/system-prompt.ts#L51-L56)

```ts
/**
 * Ordered system prompt sections, keyed by name. `preamble` is untagged text; every other
 * section is wrapped in a tag of the same name so the model can match later updates to it.
 * These become `SystemMessage.sections` in the transcript.
 */
export type SystemPromptSections = Record<string, string>;
```

它就是一个有序的 `名字 → 文本` 对象。JavaScript 对象的字符串键按插入顺序排列，所以插入顺序就是段落在提示词里的顺序。

它最终放进 M1 定义的 `SystemMessage` 的 `sections` 字段：

源码：[`packages/ai/src/types.ts:561-577`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/types.ts#L561-L577)

```ts
export interface SystemMessage {
	role: "system";
	/** Instruction text. On the leading message this is the base prompt; later, additional instructions. */
	content: string | TextContent[];
	/**
	 * Named, ordered prompt sections rendered verbatim after `content`. The leading message
	 * declares them; later messages replace sections by name, and `null` removes one. Keep
	 * each section self-delimiting (a tag, a heading) so the model can relate an update to
	 * the original. Avoid integer-like names; JSON objects reorder those.
	 */
	sections?: Record<string, string | null>;
	/** Complete definitions of tools that become available at this point. */
	toolsAdded?: Tool[];
	/** Tools that stop being available at this point. */
	toolsRemoved?: ToolReference[];
	timestamp: number; // Unix timestamp in milliseconds
}
```

| 字段 | 在 M6 里的用法 |
|---|---|
| `content` | M6 产生的 system 消息总是 `""`。只有强制提示词（§4.13）会把文本放在这里，而且只在请求时出现 |
| `sections` | 第一条 system 消息带全部段落；后面的 system 消息只带变化的段落，值为 `null` 表示删除这一段 |
| `toolsAdded`/`toolsRemoved` | 不由 M6 写入，由 M2 的 `declareToolChanges` 写进同一条消息（→ M2 §4.4） |

`SystemPromptSections` 的值只能是字符串；`SystemMessage.sections` 的值可以是 `null`。差分函数（§4.11）的输出用的是后者。

**真实实例。** §2.1 里第一条 system 消息的 `sections` 是完整的段落集合；§7.3 里会话中途修改 AGENTS.md 之后，transcript 多了一条只带 `project_context` 的 system 消息，删除 AGENTS.md 之后又多了一条：

```
system #3 id=8acc3607 content=""
  sections: project_context=null
```

### 3.3 Skill 和 PromptTemplate：两种文件资源

源码：[`packages/coding-agent/src/core/skills.ts:74-81`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/skills.ts#L74-L81)

```ts
export interface Skill {
	name: string;
	description: string;
	filePath: string;
	baseDir: string;
	sourceInfo: SourceInfo;
	disableModelInvocation: boolean;
}
```

| 字段 | 说明 |
|---|---|
| `name` | frontmatter 的 `name`，没写就用所在目录名。只能用小写字母、数字和连字符，不合规只报警告 |
| `description` | frontmatter 的 `description`，必填。缺了就不加载 |
| `filePath` | skill 文件的路径。系统提示词里写的就是它 |
| `baseDir` | skill 文件所在目录，skill 正文里的相对路径相对它解析 |
| `sourceInfo` | 来源：自动发现、设置、CLI 参数、包、扩展。界面和诊断用 |
| `disableModelInvocation` | frontmatter 的 `disable-model-invocation: true`。为真时不写进系统提示词，只能用 `/skill:name` 手动调用 |

源码：[`packages/coding-agent/src/core/prompt-templates.ts:12-19`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/prompt-templates.ts#L12-L19)

```ts
export interface PromptTemplate {
	name: string;
	description: string;
	argumentHint?: string;
	content: string;
	sourceInfo: SourceInfo;
	filePath: string; // Absolute path to the template file
}
```

| 字段 | 说明 |
|---|---|
| `name` | 文件名去掉 `.md`。用户输入 `/name 参数…` 触发 |
| `description` | frontmatter 的 `description`，没写就取正文第一个非空行的前 60 个字符 |
| `argumentHint` | frontmatter 的 `argument-hint`，界面提示用 |
| `content` | 去掉 frontmatter 的正文，里面可以有 `$1`、`$@`、`${2:-默认值}` 等占位符 |

两者的区别：skill 写进系统提示词，由模型决定何时去读；prompt template 不进系统提示词，只在用户输入时展开。

**真实实例。** [`lab/m6-resources.ts`](lab/m6-resources.ts) 单独运行 `DefaultResourceLoader`（不加载扩展），打印它找到的资源：

```bash
node --conditions=source --experimental-strip-types m6-resources.ts /tmp/pi-lab/m6-work-2 /tmp/pi-lab/m6-agent-2
```

```
projectTrusted: true
agentsFiles: [{"path":"/tmp/pi-lab/m6-agent-2/AGENTS.md","content":"# 全局约定\n\n回答用中文。\n"}]
skills: [
  {
    "name": "review",
    "description": "Review a diff and list risky changes.",
    "filePath": "/tmp/pi-lab/m6-agent-2/skills/review/SKILL.md",
    "baseDir": "/tmp/pi-lab/m6-agent-2/skills/review",
    "sourceInfo": {
      "path": "/tmp/pi-lab/m6-agent-2/skills/review/SKILL.md",
      "source": "auto",
      "scope": "user",
      "origin": "top-level",
      "baseDir": "/tmp/pi-lab/m6-agent-2"
    },
    "disableModelInvocation": false
  }
]
prompts: [
  {
    "name": "sum",
    "description": "总结一个文件",
    "argumentHint": "<file>",
    "content": "读 $1，总结成${2:-三}句话",
    "sourceInfo": {
      "path": "/tmp/pi-lab/m6-agent-2/prompts/sum.md",
      "source": "auto",
      "scope": "user",
      "origin": "top-level",
      "baseDir": "/tmp/pi-lab/m6-agent-2"
    },
    "filePath": "/tmp/pi-lab/m6-agent-2/prompts/sum.md"
  }
]
systemPrompt: undefined
appendSystemPrompt: []
```

`sourceInfo.source` 是 `"auto"`、`scope` 是 `"user"`，表示这两个资源是在全局配置目录里自动发现的（§4.6）。

### 3.4 ResourceLoader：资源的查询接口

源码：[`packages/coding-agent/src/core/resource-loader.ts:181-193`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/resource-loader.ts#L181-L193)

```ts
export interface ResourceLoader {
	getExtensions(): LoadExtensionsResult;
	getSkills(): { skills: Skill[]; diagnostics: ResourceDiagnostic[] };
	getPrompts(): { prompts: PromptTemplate[]; diagnostics: ResourceDiagnostic[] };
	getThemes(): { themes: Theme[]; diagnostics: ResourceDiagnostic[] };
	getAgentsFiles(): { agentsFiles: Array<{ path: string; content: string }> };
	getSystemPrompt(): string | undefined;
	getSystemPromptSource(): { path: string } | undefined;
	getAppendSystemPrompt(): string[];
	getAppendSystemPromptSources(): Array<{ path: string }>;
	extendResources(paths: ResourceExtensionPaths): void;
	reload(options?: ResourceLoaderReloadOptions): Promise<void>;
}
```

接口分两类方法：`reload()` 和 `extendResources()` 做磁盘扫描，把结果存在实例字段里；其余 `get*()` 只返回上次扫描的结果，不读磁盘。AgentSession 拼提示词时调用的都是 `get*()`（§4.4），所以修改 AGENTS.md 之后要等下一次 `reload()` 才会生效。主线场景里 `reload()` 在启动时调用一次（`packages/coding-agent/src/core/agent-session-services.ts:154`）。`/reload` 命令会再调用一次（`packages/coding-agent/src/core/agent-session.ts:3757`）。

`DefaultResourceLoader` 是唯一的实现。SDK 用户可以传自己的实现，或者用构造参数里的 `skillsOverride`、`agentsFilesOverride`、`systemPromptOverride` 等回调改写某一类结果（`packages/coding-agent/src/core/resource-loader.ts:323-340`）。

### 3.5 Settings 和 SettingsManager：两层设置

`Settings` 是设置文件 JSON 的类型，由 TypeBox schema 生成，有几十个可选字段（`packages/coding-agent/src/core/settings-schema.ts:491`）。和 M6 相关的字段：

| 字段 | 说明 |
|---|---|
| `defaultTools` | 启动时激活的工具。普通名字是替换，`+name`/`-name` 是增删（§4.14） |
| `skills`、`prompts`、`extensions`、`themes` | 额外的资源路径，也可以用 `!路径` 关掉自动发现的资源 |
| `enableSkillCommands` | 是否注册 `/skill:name` 命令 |
| `defaultProjectTrust` | `"ask"`（默认）、`"always"`、`"never"`。只在全局设置里有效 |

`SettingsManager` 保存三份设置：

源码：[`packages/coding-agent/src/core/settings-manager.ts:272-277`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/settings-manager.ts#L272-L277)

```ts
export class SettingsManager {
	private storage: SettingsStorage;
	private globalSettings: Settings;
	private projectSettings: Settings;
	private settings: Settings;
	private projectTrusted: boolean;
```

| 字段 | 说明 |
|---|---|
| `globalSettings` | `<agentDir>/settings.json` 的内容 |
| `projectSettings` | `<cwd>/.pi/settings.json` 的内容。项目不被信任时是 `{}` |
| `settings` | 两者合并的结果，所有 getter 读它 |
| `projectTrusted` | 项目是否被信任 |

**真实实例。** [`lab/m6-settings.ts`](lab/m6-settings.ts) 用同一对目录分别以不信任和信任创建 `SettingsManager`：

```bash
A=/tmp/pi-lab/m6-agent-8; W=/tmp/pi-lab/m6-work-8
mkdir -p $A $W/.pi
printf '{\n  "defaultTools": ["-write"],\n  "compaction": { "reserveTokens": 8000 }\n}\n' > $A/settings.json
printf '{\n  "defaultTools": ["-edit"],\n  "compaction": { "keepRecentTokens": 4000 }\n}\n' > $W/.pi/settings.json
node --conditions=source --experimental-strip-types m6-settings.ts $W $A
```

```
projectTrusted=false
  global:       {"defaultTools":["-write"],"compaction":{"reserveTokens":8000}}
  project:      {}
  merged:       {"defaultTools":["-write"],"compaction":{"reserveTokens":8000}}
  getDefaultTools(): ["read","bash","edit"]
projectTrusted=true
  global:       {"defaultTools":["-write"],"compaction":{"reserveTokens":8000}}
  project:      {"defaultTools":["-edit"],"compaction":{"keepRecentTokens":4000}}
  merged:       {"defaultTools":["-write","-edit"],"compaction":{"reserveTokens":8000,"keepRecentTokens":4000}}
  getDefaultTools(): ["read","bash"]
```

不信任时项目设置是 `{}`，像没写一样。信任时 `compaction` 对象按键合并，`defaultTools` 两个修饰列表拼接。§4.14 读合并代码。

### 3.6 类型关系图

```
 磁盘                                   内存                                  transcript
 ─────                                  ────                                  ──────────
 <agentDir>/settings.json ─┐
 <cwd>/.pi/settings.json ──┴─(信任才读)─▶ SettingsManager.settings
                                             │ defaultTools、skills/prompts 路径
                                             ▼
 AGENTS.md / CLAUDE.md ────────────────▶ ResourceLoader ── getAgentsFiles() ─┐
 skills/**/SKILL.md ───────────────────▶   .skills: Skill[] ── getSkills() ───┤
 prompts/*.md ─────────────────────────▶   .prompts: PromptTemplate[]         │
 SYSTEM.md / APPEND_SYSTEM.md ─────────▶   .systemPrompt / .appendSystemPrompt┤
                                                                              │
 ToolDefinition.promptSnippet / promptGuidelines ─(_refreshToolRegistry)──────┤
                                                                              ▼
                                         BuildSystemPromptOptions（_baseSystemPromptOptions）
                                             │ before_agent_start 扩展可修改
                                             ▼
                                         buildSystemPromptSections()
                                             ▼
                                         SystemPromptSections（想要的）
                                             │ diffSystemPromptSections(已有的, 想要的)
                                             ▼                       ▲
                                         SystemMessage.sections 补丁 │ getCurrentSystemMessage()
                                             └────────────▶ system 消息 ─┘ 重放已有的 system 消息

 PromptTemplate 不走这条路：session.prompt() 用它展开用户输入
```

---

## 4. 从入口读起

入口是 `buildSystemPromptSections()`。先读它怎么把选项变成段落（§4.1–4.2），再往上游读选项的每个字段从哪来（§4.3–4.10），然后往下游读段落怎么进 transcript（§4.11–4.13），最后读设置和信任（§4.14–4.15）。

```
buildSystemPromptSections()                         §4.1
├─ normalizeBuildSystemPromptOptions()
├─ buildRules()                                     §4.2
├─ renderProjectContext()
└─ formatSkillsForPrompt()                          §4.8
上游：选项从哪来
├─ _refreshToolRegistry()  工具的 snippet/guidelines §4.3
├─ _rebuildSystemPrompt()  基础选项                  §4.4
└─ DefaultResourceLoader.reload()                   §4.5
    ├─ addAutoDiscoveredResources()                 §4.6
    ├─ loadProjectContextFiles()                    §4.7
    ├─ loadSkills()                                 §4.8
    ├─ SYSTEM.md / APPEND_SYSTEM.md                 §4.9
    └─ loadPromptTemplates() → expandPromptTemplate §4.10
下游：段落怎么进 transcript
├─ diffSystemPromptSections() + getCurrentSystemMessage()   §4.11
├─ _prepareRun() → emitBeforeAgentStart() → _preparePromptAndToolLoadout()  §4.12
└─ _installAgentNextTurnRefresh()、强制提示词投影           §4.13
设置和信任
├─ deepMergeSettings()、loadFromStorage()、setProjectTrusted()  §4.14
└─ resolveProjectTrusted()                          §4.15
```

### 4.1 buildSystemPromptSections()

**调用位置**：`AgentSession._preparePromptAndToolLoadout()`（§4.12）→ `buildSystemPromptSections(options)`。`buildSystemPromptState()` 和 `buildSystemPrompt()`（`packages/coding-agent/src/core/system-prompt.ts:198`、`packages/coding-agent/src/core/system-prompt.ts:207`）也调用它，后者用于 `session.systemPrompt` 和扩展的 `ctx.getSystemPrompt()`。

**① 默认段落。**

源码：[`packages/coding-agent/src/core/system-prompt.ts:127-169`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/system-prompt.ts#L127-L169)

```ts
/** Build the ordered, independently replaceable sections of the structured system prompt. */
export function buildSystemPromptSections(input: BuildSystemPromptOptions): SystemPromptSections {
	const options = normalizeBuildSystemPromptOptions(input);
	const {
		customPrompt,
		selectedTools,
		hiddenTools,
		toolSnippets,
		toolGuidelines,
		promptGuidelines,
		appendSystemPrompt,
		sections: customSections,
		cwd,
		contextFiles,
		skills,
	} = options;

	for (const name of Object.keys(customSections)) {
		if (!SYSTEM_PROMPT_SECTION_NAME.test(name) || name === "preamble") {
			throw new Error(`Invalid system prompt section name: ${name}`);
		}
	}

	const declaredTools = selectedTools.filter((name) => !hiddenTools.includes(name));
	const promptSections: Record<string, string> = {};
	if (customPrompt) {
		promptSections.preamble = customPrompt;
	} else {
		promptSections.preamble =
			"You are a coding assistant running inside pi, a coding agent harness. Help users inspect files, run commands, and modify code.";
		const visibleTools = declaredTools.filter((name) => !!toolSnippets[name]);
		const tools =
			visibleTools.length > 0 ? visibleTools.map((name) => `- ${name}: ${toolSnippets[name]}`).join("\n") : "(none)";
		promptSections.tools = `${tools}\n\nAdditional tools may be available.`;
		promptSections.rules = buildRules(declaredTools, toolGuidelines, promptGuidelines);
		promptSections.docs = `Pi documentation (read only when the user asks about the harness itself, its SDK, extensions, themes, skills, or TUI):
- Main documentation: ${getReadmePath()}
- Additional docs: ${getDocsPath()}
- Examples: ${getExamplesPath()} (extensions, custom tools, SDK)
- When reading pi docs or examples, resolve docs/... under Additional docs and examples/... under Examples, not the current working directory
- When asked about: extensions (docs/extensions.md, examples/extensions/), themes (docs/themes.md), skills (docs/skills.md), prompt templates (docs/prompt-templates.md), TUI components (docs/tui.md), keybindings (docs/keybindings.md), SDK integrations (docs/sdk.md), custom providers (docs/custom-provider.md), adding models (docs/models.md), packages (docs/packages.md), environment variables (docs/environment-variables.md), MCP servers (docs/mcp.md), codemode scripts and non-LLM models such as classifiers and image models (docs/codemode.md)
- Before implementing harness changes, read the relevant docs and examples in full, following related cross-references.`;
	}
```

**② 可选段落和包标签。**

源码：[`packages/coding-agent/src/core/system-prompt.ts:171-192`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/system-prompt.ts#L171-L192)

```ts
	if (appendSystemPrompt) promptSections.addendum = appendSystemPrompt;
	if (contextFiles.length > 0) promptSections.project_context = renderProjectContext(contextFiles);
	// A hidden reader is still reachable through another tool, so skills stay but the hint names no tool.
	const readers = ["read", "bash"] as const;
	const skillFileReadTool =
		readers.find((tool) => declaredTools.includes(tool)) ??
		(readers.some((tool) => selectedTools.includes(tool)) ? "indirect" : undefined);
	if (skillFileReadTool && skills.length > 0) {
		const skillsPrompt = formatSkillsForPrompt(skills, skillFileReadTool).trim();
		if (skillsPrompt) promptSections.skills = skillsPrompt;
	}
	promptSections.cwd = cwd.replace(/\\/g, "/");
	for (const [name, content] of Object.entries(customSections)) {
		if (content) promptSections[name] = content;
	}

	const sections: SystemPromptSections = { preamble: promptSections.preamble };
	for (const [name, content] of Object.entries(promptSections)) {
		if (name !== "preamble") sections[name] = `<${name}>\n${content}\n</${name}>`;
	}
	return sections;
}
```

**讲解**

输入是 `BuildSystemPromptOptions`，输出是 `SystemPromptSections`。函数是纯的：不读文件、不读设置，同样的输入总是得到同样的输出。这一点是 §4.11 差分能工作的前提。

段落按插入顺序排列，固定的顺序是：

| 顺序 | 段落 | 条件 |
|---|---|---|
| 1 | `preamble` | 总有。有 `customPrompt` 时用它，否则用默认的一句话 |
| 2 | `tools` | 没有 `customPrompt` 时。只列 `declaredTools` 里**有 snippet** 的工具 |
| 3 | `rules` | 没有 `customPrompt` 时，见 §4.2 |
| 4 | `docs` | 没有 `customPrompt` 时。pi 自己的文档路径，由 `getReadmePath()` 等函数按安装位置计算 |
| 5 | `addendum` | `appendSystemPrompt` 非空 |
| 6 | `project_context` | 有上下文文件 |
| 7 | `skills` | 有 skill，而且 read 或 bash 至少一个被选中 |
| 8 | `cwd` | 总有。Windows 的反斜杠换成 `/` |
| 9 | 自定义段 | 扩展通过 `sections` 加的，内容为空的跳过 |

几个细节：

- `customPrompt` 会把 `tools`、`rules`、`docs` 三段一起去掉，不只是替换开头一句。用了 `--system-prompt` 或 `SYSTEM.md`，模型就看不到工具列表和规则了（工具的 JSON schema 声明仍然在 `toolsAdded` 里，模型照样能调用）。
- `declaredTools` 去掉了 `hiddenTools`。被隐藏声明的工具不出现在 `tools` 和 `rules` 里，但只要它被选中，skill 段仍然保留，只是提示语不点名工具（`"indirect"`）。
- 自定义段的名字必须匹配 `/^[a-z][a-z0-9_-]*$/`（`packages/coding-agent/src/core/system-prompt.ts:58`），不能叫 `preamble`。这样名字能直接当 XML 标签用，也避开了 `SystemMessage.sections` 注释里说的"整数样的键会被 JSON 对象重新排序"。不合规时直接抛错。实测：用 `M6_SECTION="Plan=x"` 让扩展加一个大写开头的段：

```bash
WORK=/tmp/pi-lab/m6-work-7 PI_CODING_AGENT_DIR=/tmp/pi-lab/m6-agent-7 M6_SECTION="Plan=x" ./run.sh -e "$PWD/m6-dump-ext.ts" -p "读 README.md，总结成三句话"; echo "exit=$?"
```

```
Running without API keys...
Invalid system prompt section name: Plan
exit=1
```

- 最后一个循环给 `preamble` 以外的段落包上同名标签。标签让模型能把后面的更新和原来的段落对上：更新消息里的 `<project_context>` 替换的就是开头那个 `<project_context>`。

`renderProjectContext`（`packages/coding-agent/src/core/system-prompt.ts:79`）把每个上下文文件包成 `<project_instructions path="…">`，前面加一行 `Project-specific instructions and guidelines:`，用空行连接。§2.2 的 `project_context` 段就是它的输出。

**往哪走**：§4.2 `buildRules`；skill 段的格式见 §4.8。

### 4.2 buildRules()

**调用位置**：`buildSystemPromptSections()` 161 行。

源码：[`packages/coding-agent/src/core/system-prompt.ts:88-125`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/system-prompt.ts#L88-L125)

```ts
function buildRules(
	selectedTools: string[],
	toolGuidelines: Record<string, string[]>,
	promptGuidelines: string[],
): string {
	const rules: string[] = [];
	const seen = new Set<string>();
	const addRule = (rule: string): void => {
		const normalized = rule.trim();
		if (!normalized || seen.has(normalized)) return;
		seen.add(normalized);
		rules.push(normalized);
	};

	const hasBash = selectedTools.includes("bash");
	const hasPowerShell = selectedTools.includes("powershell");
	const hasGrep = selectedTools.includes("grep");
	const hasFind = selectedTools.includes("find");
	const hasLs = selectedTools.includes("ls");

	if ((hasBash || hasPowerShell) && !hasGrep && !hasFind && !hasLs) {
		if (hasBash && hasPowerShell) {
			addRule("Use bash or PowerShell for file operations like listing, searching, and finding files");
		} else if (hasPowerShell) {
			addRule("Use PowerShell for file operations like listing, searching, and finding files");
		} else {
			addRule("Use bash for file operations like ls, rg, find");
		}
	}

	for (const name of selectedTools) {
		for (const rule of toolGuidelines[name] ?? []) addRule(rule);
	}
	for (const rule of promptGuidelines) addRule(rule);
	addRule("Be concise in your responses");
	addRule("Show file paths clearly when working with files");
	return rules.map((rule) => `- ${rule}`).join("\n");
}
```

**讲解**

规则有四个来源，按这个顺序加入：

1. 一条和 shell 有关的规则：只在有 bash（或 PowerShell）、而没有 grep/find/ls 专用工具时加。有专用工具时，就不叫模型用 bash 去找文件了。
2. 每个选中工具的 guidelines，按工具顺序。
3. 扩展追加的 `promptGuidelines`。
4. 两条固定规则。

`addRule` 去掉首尾空白并去重，先加入的保留。§2.1 的 `rules` 段 8 条：第 1 条来自 1，第 2–6 条来自 read、bash、edit（两条）、write 的 guidelines，最后两条是固定规则。

去重的效果可以直接看到：扩展追加一条和固定规则一样的文字，规则数不变。

```bash
WORK=/tmp/pi-lab/m6-work-6 PI_CODING_AGENT_DIR=/tmp/pi-lab/m6-agent-6 M6_GUIDELINE="Be concise in your responses" M6_SECTION="plan=先读文件，再回答。" ./run.sh -e "$PWD/m6-dump-ext.ts" -p "读 README.md，总结成三句话"
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t /tmp/pi-lab/m6-agent-6/sessions/*/*.jsonl | head -1)" rules plan
```

```
Running without API keys...
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
system #1 id=5c50da16 content=""
  sections: preamble(126) tools(274) rules(589) docs(1179) cwd(42) plan(24)
  toolsAdded: read,bash,edit,write
重放后 sections 顺序: preamble,tools,rules,docs,cwd,plan
重放后 tools: read,bash,edit,write
---- rules ----
<rules>
- Use bash for file operations like ls, rg, find
- Use read to examine files instead of cat or sed.
- You can inspect PI_* environment variables for current model and session details.
- Use edit for targeted changes. Batch changes to one file in a single call.
- Each edits[].oldText must exactly and uniquely match the original file. Keep matches short and non-overlapping; merge nearby changes without including large unchanged regions.
- Use write only for new files or complete rewrites.
- Be concise in your responses
- Show file paths clearly when working with files
</rules>
---- plan ----
<plan>
先读文件，再回答。
</plan>
```

`rules` 仍是 589 个字符，和 §2.1 一样。扩展的那条规则先加入（来源 3），固定规则再加入时被去重，所以位置也没变。自定义段 `plan` 排在 `cwd` 后面。

换一组工具，第 1 条规则就消失了。§4.14 的 `m6-work-9` 用项目设置把工具换成 `read, grep, ls`，它的 `tools` 和 `rules` 段是：

```
---- tools ----
<tools>
- read: Read file contents
- grep: Search file contents for patterns (respects .gitignore)
- ls: List directory contents

Additional tools may be available.
</tools>
---- rules ----
<rules>
- Use read to examine files instead of cat or sed.
- Be concise in your responses
- Show file paths clearly when working with files
</rules>
```

grep 和 ls 有 snippet 没有 guidelines，所以 `tools` 有三行，`rules` 只有 read 的一条加两条固定规则。

**往哪走**：snippet 和 guidelines 从哪来，§4.3。

### 4.3 工具的 snippet 和 guidelines：_refreshToolRegistry()

**调用位置**：`new AgentSession()` → `_buildRuntime()`（`packages/coding-agent/src/core/agent-session.ts:534`）→ `_refreshToolRegistry()`（`packages/coding-agent/src/core/agent-session.ts:3735`）。

每个内置工具在自己的文件里导出一个"系统提示词贡献"：

源码：[`packages/coding-agent/src/core/tools/read.ts:20-23`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/read.ts#L20-L23)

```ts
export const readToolSystemPromptContribution = {
	snippet: "Read file contents",
	guidelines: ["Use read to examine files instead of cat or sed."],
} as const;
```

源码：[`packages/coding-agent/src/core/tools/edit.ts:43-49`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/tools/edit.ts#L43-L49)

```ts
export const editToolSystemPromptContribution = {
	snippet: "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
	guidelines: [
		"Use edit for targeted changes. Batch changes to one file in a single call.",
		"Each edits[].oldText must exactly and uniquely match the original file. Keep matches short and non-overlapping; merge nearby changes without including large unchanged regions.",
	],
} as const;
```

工具定义把它们放进 `ToolDefinition` 的 `promptSnippet` 和 `promptGuidelines` 字段（例如 `packages/coding-agent/src/core/tools/read.ts:97-98`）。这两个字段在扩展 API 里是公开的（`packages/coding-agent/src/core/extensions/types.ts:606-609`），所以扩展注册的工具也能往系统提示词里加一行说明和几条规则。字段注释写明：没有 `promptSnippet` 的自定义工具不出现在 `tools` 段。

AgentSession 在刷新工具注册表时收集它们（扩展运行时调用的 `refreshTools` 也指向这个函数，`packages/coding-agent/src/core/agent-session.ts:3507`）：

源码：[`packages/coding-agent/src/core/agent-session.ts:3610-3626`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3610-L3626)

```ts
		this._toolDefinitions = definitionRegistry;
		this._toolPromptSnippets = new Map(
			Array.from(definitionRegistry.values())
				.map(({ definition }) => {
					const snippet = this._normalizePromptSnippet(definition.promptSnippet);
					return snippet ? ([definition.name, snippet] as const) : undefined;
				})
				.filter((entry): entry is readonly [string, string] => entry !== undefined),
		);
		this._toolPromptGuidelines = new Map(
			Array.from(definitionRegistry.values())
				.map(({ definition }) => {
					const guidelines = this._normalizePromptGuidelines(definition.promptGuidelines);
					return guidelines.length > 0 ? ([definition.name, guidelines] as const) : undefined;
				})
				.filter((entry): entry is readonly [string, string[]] => entry !== undefined),
		);
```

**讲解**

`definitionRegistry` 是所有已注册的工具定义：内置工具（不管激活与否）、扩展工具、SDK 传入的工具。所以 §3.1 的实例里 `toolSnippets` 有 powershell、grep、codemode 等 10 项。

`_normalizePromptSnippet`（`packages/coding-agent/src/core/agent-session.ts:1717`）把换行和连续空白压成一个空格，保证 snippet 在 `tools` 段只占一行。`_normalizePromptGuidelines`（`packages/coding-agent/src/core/agent-session.ts:1726`）去空白、去空串、去重。

收集结果存在两个 Map 里，函数最后调用 `_setActiveTools()`（`packages/coding-agent/src/core/agent-session.ts:3673`），后者再调用 `_rebuildSystemPrompt()`（`packages/coding-agent/src/core/agent-session.ts:1566`）。

**往哪走**：§4.4 `_rebuildSystemPrompt`。

### 4.4 _rebuildSystemPrompt()：基础选项

**调用位置**：三处。`_setActiveTools()`（`packages/coding-agent/src/core/agent-session.ts:1566`，启动、`setActiveToolsByName()`、工具注册表刷新都经过它）；扩展的 `resources_discover` 事件加了新资源之后（`packages/coding-agent/src/core/agent-session.ts:3386`）。

源码：[`packages/coding-agent/src/core/agent-session.ts:1741-1767`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1741-L1767)

```ts
	private _rebuildSystemPrompt(toolNames: string[]): void {
		const validToolNames = toolNames.filter((name) => this._toolRegistry.has(name));
		const toolSnippets: Record<string, string> = {};
		for (const name of this._toolRegistry.keys()) {
			const snippet = this._toolPromptSnippets.get(name);
			// Tools without a snippet are not listed.
			if (snippet) toolSnippets[name] = snippet;
		}

		const loaderSystemPrompt = this._resourceLoader.getSystemPrompt();
		const loaderAppendSystemPrompt = this._resourceLoader.getAppendSystemPrompt();
		const appendSystemPrompt = loaderAppendSystemPrompt.length > 0 ? loaderAppendSystemPrompt.join("\n\n") : "";
		const loadedSkills = this._resourceLoader.getSkills().skills;
		const loadedContextFiles = this._resourceLoader.getAgentsFiles().agentsFiles;

		this._baseSystemPromptOptions = normalizeBuildSystemPromptOptions({
			cwd: this._cwd,
			skills: loadedSkills,
			contextFiles: loadedContextFiles,
			customPrompt: loaderSystemPrompt,
			appendSystemPrompt,
			selectedTools: validToolNames,
			hiddenTools: [...this._hiddenDeclarations],
			toolSnippets,
			toolGuidelines: Object.fromEntries(this._toolPromptGuidelines),
		});
	}
```

**讲解**

名字叫"rebuild system prompt"，但它不拼提示词，也不碰 transcript。它只做一件事：把工具和资源加载器的当前结果组装成一份 `NormalizedBuildSystemPromptOptions`，存进 `_baseSystemPromptOptions` 字段（`packages/coding-agent/src/core/agent-session.ts:494`）。

这份"基础选项"有两个用户：

- 每次运行开始时，`_prepareRun()` 把它交给扩展的 `before_agent_start` 事件（§4.12）。
- `session.systemPrompt` getter 用它渲染当前提示词（`packages/coding-agent/src/core/agent-session.ts:1505-1507`）。

所有资源都来自 `_resourceLoader.get*()`，这些方法只返回内存里的结果（§3.4）。这里能看到 M6 三层的接口：资源加载器提供 `customPrompt`、`appendSystemPrompt`、`skills`、`contextFiles`；工具注册表提供 `selectedTools`、`toolSnippets`、`toolGuidelines`；`cwd` 来自会话。

`promptGuidelines` 和 `sections` 没有设置，补成空的。这两个字段只留给扩展在 `before_agent_start` 里填。

**往哪走**：资源加载器怎么得到这些结果，§4.5。

### 4.5 DefaultResourceLoader.reload()

**调用位置**：`main()` → `createRuntime` → `createAgentSessionServices()` → `resourceLoader.reload(options.resourceLoaderReloadOptions)`（`packages/coding-agent/src/core/agent-session-services.ts:148-154`）。这发生在 `new AgentSession()` 之前，所以 §4.4 读到的已经是扫描结果。

**开头，先定项目信任。**

源码：[`packages/coding-agent/src/core/resource-loader.ts:541-557`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/resource-loader.ts#L541-L557)

```ts
	async reload(options?: ResourceLoaderReloadOptions): Promise<void> {
		resetTimings("extensions");

		if (this.loaded) {
			clearExtensionCache();
		}

		let preTrustExtensions: LoadExtensionsResult | undefined;
		if (options?.resolveProjectTrust) {
			preTrustExtensions = await this.loadProjectTrustExtensions();
			const projectTrusted = await options.resolveProjectTrust({ extensionsResult: preTrustExtensions });
			this.settingsManager.setProjectTrusted(projectTrusted);
		}

		// reload() preserves SettingsManager.projectTrusted and reloads settings for that trust state.
		await this.settingsManager.reload();
		const resolvedPaths = await this.packageManager.resolve();
```

**讲解**

`reload()` 有 170 行，按资源类型排成一条直线：信任 → 设置 → 包和自动发现的路径 → 扩展（→ M7）→ skills → prompt templates → themes（略）→ AGENTS.md → `SYSTEM.md` → `APPEND_SYSTEM.md`。

信任必须最先决定，因为后面每一步都依赖它：项目设置要不要读，`.pi/skills` 要不要扫描，`.pi/SYSTEM.md` 要不要用。

`resolveProjectTrust` 由 `main()` 只在"需要问"的时候传入（§4.15）。传入时先调用 `loadProjectTrustExtensions()`（`packages/coding-agent/src/core/resource-loader.ts:533-539`）：把 `SettingsManager` 临时设为不信任，只加载全局和 CLI 指定的扩展。这些扩展可以处理 `project_trust` 事件，替用户回答"信不信任这个项目"。项目自己的扩展不能参与这个决定。

`packageManager.resolve()` 返回各类资源的路径列表，每条带 `enabled` 和来源元数据。自动发现的目录在 §4.6。之后 627–676 行把这些路径和 CLI 参数（`--skill`、`--prompt-template`）给的路径合并，交给 `updateSkillsFromPaths()`（§4.8）和 `updatePromptsFromPaths()`（§4.10）。`--no-skills` 只关掉自动发现的 skill，CLI 显式给的仍然加载（631–633 行）。

**结尾，AGENTS.md。**

源码：[`packages/coding-agent/src/core/resource-loader.ts:678-687`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/resource-loader.ts#L678-L687)

```ts
		const agentsFiles = {
			agentsFiles: this.noContextFiles
				? []
				: loadProjectContextFiles({
						cwd: this.cwd,
						agentDir: this.agentDir,
					}),
		};
		const resolvedAgentsFiles = this.agentsFilesOverride ? this.agentsFilesOverride(agentsFiles) : agentsFiles;
		this.agentsFiles = resolvedAgentsFiles.agentsFiles;
```

每类资源都是同一个模式：扫描 → 可选的 `*Override` 回调改写 → 存进实例字段。`--no-context-files`（`-nc`）让 AGENTS.md 列表为空。

注意 AGENTS.md 的加载**不看项目信任**：`loadProjectContextFiles` 没有收到 `settingsManager`。§7.2 的实验验证了这一点。理由是 AGENTS.md 只是文本，进的是提示词；需要信任的是会执行代码或改变 pi 行为的东西（设置、扩展、skills、`SYSTEM.md`）。`trust-manager.ts` 列出的需要信任的项目资源里也没有 AGENTS.md：

源码：[`packages/coding-agent/src/core/trust-manager.ts:30-39`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/trust-manager.ts#L30-L39)

```ts
const TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES = [
	"settings.json",
	"mcp.json",
	"extensions",
	"skills",
	"prompts",
	"themes",
	"SYSTEM.md",
	"APPEND_SYSTEM.md",
] as const;
```

**往哪走**：§4.6 自动发现，§4.7 AGENTS.md，§4.8 skills，§4.9 `SYSTEM.md`。

### 4.6 addAutoDiscoveredResources()：自动发现的目录

**调用位置**：`reload()` → `packageManager.resolve()`（`packages/coding-agent/src/core/package-manager.ts:974`）→ `addAutoDiscoveredResources()`（`packages/coding-agent/src/core/package-manager.ts:1023`）。

源码：[`packages/coding-agent/src/core/package-manager.ts:2604-2658`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/package-manager.ts#L2604-L2658)

```ts
		const userAgentsSkillsDir = join(getHomeDir(), ".agents", "skills");
		const projectTrusted = this.settingsManager.isProjectTrusted();
		const projectAgentsSkillDirs = projectTrusted
			? collectAncestorAgentsSkillDirs(this.cwd).filter((dir) => !pathsEqual(dir, userAgentsSkillsDir))
			: [];

		const addResources = (
			resourceType: ResourceType,
			paths: string[],
			metadata: PathMetadata,
			overrides: string[],
			baseDir: string,
		) => {
			const target = this.getTargetMap(accumulator, resourceType);
			for (const path of paths) {
				const enabled = isEnabledByOverrides(path, overrides, baseDir);
				this.addResource(target, path, metadata, enabled);
			}
		};

		if (projectTrusted) {
			// Project extensions from .pi/
			addResources(
				"extensions",
				collectAutoExtensionEntries(projectDirs.extensions),
				projectMetadata,
				projectOverrides.extensions,
				projectBaseDir,
			);

			// Project skills from .pi/
			addResources(
				"skills",
				collectAutoSkillEntries(projectDirs.skills, "pi"),
				projectMetadata,
				projectOverrides.skills,
				projectBaseDir,
			);
		}

		// Project skills from .agents/ (each with its own baseDir)
		for (const agentsSkillsDir of projectAgentsSkillDirs) {
			const agentsBaseDir = dirname(agentsSkillsDir); // the .agents directory
			const agentsMetadata: PathMetadata = {
				...projectMetadata,
				baseDir: agentsBaseDir,
			};
			addResources(
				"skills",
				collectAutoSkillEntries(agentsSkillsDir, "agents"),
				agentsMetadata,
				projectOverrides.skills,
				agentsBaseDir,
			);
		}
```

**讲解**

函数后半段（2660–2722 行）用同样的方式加入项目 prompts/themes（也要信任）和全局资源。汇总成表：

| 资源 | 项目目录（需要信任） | 全局目录（总是扫描） |
|---|---|---|
| 扩展 | `<cwd>/.pi/extensions` | `<agentDir>/extensions` |
| skills | `<cwd>/.pi/skills`；从 cwd 往上到 git 仓库根的每一层 `.agents/skills` | `<agentDir>/skills`；`~/.agents/skills` |
| prompt templates | `<cwd>/.pi/prompts` | `<agentDir>/prompts` |

`<agentDir>` 默认是 `~/.pi/agent`，可以用 `PI_CODING_AGENT_DIR` 改（`packages/coding-agent/src/config.ts:630-636`），本教程的实验都这样隔离。项目资源先加入，全局资源后加入。同名 skill 先加入的胜出（§4.8），所以项目 skill 覆盖全局 skill。

`.agents/skills` 往上找的终点由 `collectAncestorAgentsSkillDirs`（`packages/coding-agent/src/core/package-manager.ts:491-510`）决定：在 git 仓库里到仓库根为止，不在仓库里就一直到文件系统根。

`isEnabledByOverrides` 读设置里的 `skills`、`prompts` 等数组。设置可以用 `!路径` 关掉某个自动发现的资源，所以每条路径都带 `enabled` 标志，`reload()` 只保留 `enabled` 的（`packages/coding-agent/src/core/resource-loader.ts:574-581`）。

**往哪走**：§4.7。

### 4.7 loadProjectContextFiles()：AGENTS.md

**调用位置**：`reload()` 681 行。

源码：[`packages/coding-agent/src/core/resource-loader.ts:212-231`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/resource-loader.ts#L212-L231)

```ts
function loadContextFileFromDir(dir: string): { path: string; content: string } | null {
	const candidates = ["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"];
	for (const filename of candidates) {
		const filePath = join(dir, filename);
		if (existsSync(filePath)) {
			try {
				if (!statSync(filePath).isFile()) {
					continue;
				}
				return {
					path: filePath,
					content: stripBom(readFileSync(filePath, "utf-8")),
				};
			} catch (error) {
				console.error(chalk.yellow(`Warning: Could not read ${filePath}: ${error}`));
			}
		}
	}
	return null;
}
```

源码：[`packages/coding-agent/src/core/resource-loader.ts:260-302`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/resource-loader.ts#L260-L302)

```ts
export function loadProjectContextFiles(options: {
	cwd: string;
	agentDir: string;
}): Array<{ path: string; content: string }> {
	const resolvedCwd = resolvePath(options.cwd);
	const resolvedAgentDir = resolvePath(options.agentDir);

	const contextFiles: Array<{ path: string; content: string }> = [];
	const seenPaths = new Set<string>();

	const globalContext = loadContextFileFromDir(resolvedAgentDir);
	if (globalContext) {
		contextFiles.push(globalContext);
		seenPaths.add(globalContext.path);
	}

	const ancestorContextFiles: Array<{ path: string; content: string }> = [];

	const shadowedContextFile = findShadowedContextFile(resolvedCwd);
	let currentDir = resolvedCwd;

	while (true) {
		const contextFile = loadContextFileFromDir(currentDir);
		// Compare the directory's real path, not the file's: a worktree AGENTS.md that
		// symlinks to the main repo's file resolves to the shadowed path itself.
		const isShadowed =
			shadowedContextFile !== undefined &&
			contextFile !== null &&
			pathsEqual(join(canonicalizePath(currentDir), basename(contextFile.path)), shadowedContextFile);
		if (contextFile && !isShadowed && !seenPaths.has(contextFile.path)) {
			ancestorContextFiles.unshift(contextFile);
			seenPaths.add(contextFile.path);
		}

		const parentDir = dirname(currentDir);
		if (parentDir === currentDir) break;
		currentDir = parentDir;
	}

	contextFiles.push(...ancestorContextFiles);

	return contextFiles;
}
```

**讲解**

规则有三条：

1. **每个目录最多一个文件。** `loadContextFileFromDir` 按 `AGENTS.override.md`、`AGENTS.md`、`AGENTS.MD`、`CLAUDE.md`、`CLAUDE.MD` 的顺序找，找到第一个就返回。同一个目录里既有 AGENTS.md 又有 CLAUDE.md 时，只读 AGENTS.md。
2. **全局在前，然后从根目录到 cwd。** 先读 `<agentDir>` 里的；再从 cwd 往上走到文件系统根，每找到一个就 `unshift` 到列表前面，结果是越靠近根的越靠前，cwd 自己的在最后。这个查找不在 git 仓库根停下。
3. **git worktree 去重。** 如果 cwd 是嵌套在主仓库里的 linked worktree，主仓库根目录的同名文件会被 worktree 自己的那份"遮住"，不重复加载（`findShadowedContextFile`，`packages/coding-agent/src/core/resource-loader.ts:242`）。

文件全文原样放进 `project_context` 段，不截断。越靠后的文件越"具体"，模型读到冲突的指令时一般以后出现的为准。§7.1 验证了顺序和"每个目录一个"。

`resolvePath` 不展开符号链接，所以全局文件路径是 `/tmp/pi-lab/...`，而 cwd 往上找到的路径是 `/private/tmp/...`（cwd 来自进程的真实目录）。§2.2 和 §7.1 的输出里能看到这个差别。

**往哪走**：§4.8。

### 4.8 skills：loadSkillFromFile() 和 formatSkillsForPrompt()

**调用位置**：`reload()` → `updateSkillsFromPaths()`（`packages/coding-agent/src/core/resource-loader.ts:880`）→ `loadSkills({ skillPaths, includeDefaults: false })`（`packages/coding-agent/src/core/skills.ts:414`）。`includeDefaults` 是 `false`，因为默认目录已经由 §4.6 变成了路径列表。

`loadSkills` 对每条路径：目录就用 `loadSkillsFromDirInternal` 扫描，`.md` 文件就直接加载。目录扫描的规则写在 `loadSkillsFromDir` 的注释里（`packages/coding-agent/src/core/skills.ts:160-167`）：目录里有 `SKILL.md` 就把这个目录当成一个 skill，不再往下找；否则顶层目录的 `.md` 文件各算一个 skill，子目录递归找 `SKILL.md`。扫描跳过 `.` 开头的条目和 `node_modules`，并遵守 `.gitignore`、`.ignore`、`.fdignore`。

单个文件的加载：

源码：[`packages/coding-agent/src/core/skills.ts:304-345`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/skills.ts#L304-L345)

```ts
	const description = frontmatter.description;
	const hasDescription = typeof description === "string" && description.trim() !== "";
	if (!isDeclaredSkill && !hasDescription) {
		return { skill: null, diagnostics };
	}

	const skillDir = dirname(filePath);
	const parentDirName = basename(skillDir);

	// Validate description
	const descErrors = validateDescription(description);
	for (const error of descErrors) {
		diagnostics.push({ type: "warning", message: error, path: filePath });
	}

	// Use name from frontmatter, or fall back to parent directory name
	const frontmatterName = typeof frontmatter.name === "string" ? frontmatter.name : undefined;
	const name = frontmatterName || parentDirName;

	// Validate name
	const nameErrors = validateName(name);
	for (const error of nameErrors) {
		diagnostics.push({ type: "warning", message: error, path: filePath });
	}

	// Still load the skill even with warnings, unless description is missing or empty.
	if (!hasDescription) {
		return { skill: null, diagnostics };
	}

	return {
		skill: {
			name,
			description,
			filePath,
			baseDir: skillDir,
			sourceInfo: createSkillSourceInfo(filePath, skillDir, source),
			disableModelInvocation: frontmatter["disable-model-invocation"] === true,
		},
		diagnostics,
	};
}
```

**讲解**

`description` 是唯一的硬性要求：没有它就不是 skill。`SKILL.md` 缺 description 会报警告；普通 `.md` 文件缺 description 则安静地跳过，因为它可能只是 skill 目录旁边的一份普通文档。名字不合规（大写、超过 64 个字符、连续连字符）只报警告，照样加载。§7.2 的 `deploy` skill 没写 `name`，名字就取了目录名。

`loadSkills` 里的 `addSkills`（`packages/coding-agent/src/core/skills.ts:426-455`）处理重复：同一个文件经符号链接出现两次，安静地跳过；不同文件同名，先加入的胜出，后来的记一条 `collision` 诊断。

然后 `formatSkillsForPrompt` 把 skill 列表变成 `skills` 段的正文：

源码：[`packages/coding-agent/src/core/skills.ts:358-388`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/skills.ts#L358-L388)

```ts
export function formatSkillsForPrompt(skills: Skill[], fileReadTool: "read" | "bash" | "indirect" = "read"): string {
	const visibleSkills = skills.filter((s) => !s.disableModelInvocation);

	if (visibleSkills.length === 0) {
		return "";
	}

	const lines = [
		"\n\nThe following skills provide specialized instructions for specific tasks.",
		fileReadTool === "read"
			? "Use the read tool to load a skill's file when the task matches its description."
			: fileReadTool === "bash"
				? "Use bash to load a skill's file when the task matches its description."
				: "Load a skill's file when the task matches its description.",
		"Resolve relative paths against the skill's directory (parent of SKILL.md). Use absolute paths in tool calls.",
		"",
		"<available_skills>",
	];

	for (const skill of visibleSkills) {
		lines.push("  <skill>");
		lines.push(`    <name>${escapeXml(skill.name)}</name>`);
		lines.push(`    <description>${escapeXml(skill.description)}</description>`);
		lines.push(`    <location>${escapeXml(skill.filePath)}</location>`);
		lines.push("  </skill>");
	}

	lines.push("</available_skills>");

	return lines.join("\n");
}
```

只有名字、描述和路径进提示词。skill 的正文要模型自己用 read（或 bash）去读，这叫"渐进加载"：系统提示词只多了几行，几十个 skill 也不会把上下文撑满。这也是 `buildSystemPromptSections` 在没有 read 和 bash 时整段不输出的原因：模型没有工具去读 skill 文件，列出来也没用。开头的 `\n\n` 会被调用方的 `.trim()` 去掉（`packages/coding-agent/src/core/system-prompt.ts:179`）。

**往哪走**：§4.9。

### 4.9 SYSTEM.md 和 APPEND_SYSTEM.md

**调用位置**：`reload()` 的最后一段。

源码：[`packages/coding-agent/src/core/resource-loader.ts:689-710`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/resource-loader.ts#L689-L710)

```ts
		const systemPromptSource = this.systemPromptSource ?? this.discoverSystemPromptFile();
		const baseSystemPrompt = resolvePromptInput(systemPromptSource, "system prompt");
		this.systemPrompt = this.systemPromptOverride ? this.systemPromptOverride(baseSystemPrompt) : baseSystemPrompt;
		this.systemPromptSourcePath =
			systemPromptSource && existsSync(systemPromptSource) ? resolvePath(systemPromptSource) : undefined;

		let appendSources = this.appendSystemPromptSource;
		if (!appendSources) {
			const discoveredAppendSystemPromptFile = this.discoverAppendSystemPromptFile();
			appendSources = discoveredAppendSystemPromptFile ? [discoveredAppendSystemPromptFile] : [];
		}
		const baseAppend = appendSources
			.map((s) => resolvePromptInput(s, "append system prompt"))
			.filter((s): s is string => s !== undefined);
		this.appendSystemPrompt = this.appendSystemPromptOverride
			? this.appendSystemPromptOverride(baseAppend)
			: baseAppend;
		this.appendSystemPromptSourcePaths = appendSources
			.filter((source) => existsSync(source))
			.map((source) => resolvePath(source));
		this.loaded = true;
	}
```

源码：[`packages/coding-agent/src/core/resource-loader.ts:1245-1257`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/resource-loader.ts#L1245-L1257)

```ts
	private discoverSystemPromptFile(): string | undefined {
		const projectPath = join(this.cwd, CONFIG_DIR_NAME, "SYSTEM.md");
		if (this.settingsManager.isProjectTrusted() && existsSync(projectPath)) {
			return projectPath;
		}

		const globalPath = join(this.agentDir, "SYSTEM.md");
		if (existsSync(globalPath)) {
			return globalPath;
		}

		return undefined;
	}
```

**讲解**

优先级：CLI 参数 `--system-prompt` > 项目 `.pi/SYSTEM.md`（需要信任）> 全局 `<agentDir>/SYSTEM.md`。三者只取一个，不叠加。`APPEND_SYSTEM.md` 的查找方式完全一样（`discoverAppendSystemPromptFile`，`packages/coding-agent/src/core/resource-loader.ts:1259`），但 `--append-system-prompt` 可以给多次，多份文本在 §4.4 用空行连接。

`resolvePromptInput`（`packages/coding-agent/src/core/resource-loader.ts:195-210`）让 CLI 参数既能是文件路径也能是文本：路径存在就读文件，否则把参数本身当提示词。

两者的效果在 §4.1 已经读过：`SYSTEM.md` 变成 `customPrompt`，替换 `preamble` 并去掉 `tools`、`rules`、`docs`；`APPEND_SYSTEM.md` 变成 `addendum` 段，排在 `docs` 和 `project_context` 之间。

**往哪走**：§4.10。

### 4.10 prompt templates 和 skill 命令：用户输入的展开

prompt template 的加载和 skill 一样走 §4.6 的路径列表：`updatePromptsFromPaths()`（`packages/coding-agent/src/core/resource-loader.ts:903`）→ `loadPromptTemplates()`（`packages/coding-agent/src/core/prompt-templates.ts:227`）。区别是目录只扫一层，不递归（`loadTemplatesFromDir`，`packages/coding-agent/src/core/prompt-templates.ts:164`），同名的由 `dedupePrompts`（`packages/coding-agent/src/core/resource-loader.ts:1192`）去重，先到先得。

单个文件：

源码：[`packages/coding-agent/src/core/prompt-templates.ts:134-159`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/prompt-templates.ts#L134-L159)

```ts
	const name = basename(filePath).replace(/\.md$/, "");

	// Get description from frontmatter or first non-empty line
	let description = typeof frontmatter.description === "string" ? frontmatter.description : "";
	if (!description) {
		const firstLine = body.split("\n").find((line) => line.trim());
		if (firstLine) {
			// Truncate if too long
			description = firstLine.slice(0, 60);
			if (firstLine.length > 60) description += "...";
		}
	}

	const argumentHint = typeof frontmatter["argument-hint"] === "string" ? frontmatter["argument-hint"] : undefined;
	return {
		template: {
			name,
			description,
			...(argumentHint && { argumentHint }),
			content: body,
			sourceInfo,
			filePath,
		},
		diagnostics,
	};
}
```

**调用位置**（展开）：`AgentSession.prompt()`。

源码：[`packages/coding-agent/src/core/agent-session.ts:2150-2155`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L2150-L2155)

```ts
		// Expand skill commands (/skill:name args) and prompt templates (/template args)
		let expandedText = currentText;
		if (expandPromptTemplates) {
			expandedText = this._expandSkillCommand(expandedText);
			expandedText = expandPromptTemplate(expandedText, [...this.promptTemplates]);
		}
```

源码：[`packages/coding-agent/src/core/prompt-templates.ts:309-325`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/prompt-templates.ts#L309-L325)

```ts
export function expandPromptTemplate(text: string, templates: PromptTemplate[]): string {
	if (!text.startsWith("/")) return text;

	const match = text.match(/^\/([^\s]+)(?:\s+([\s\S]*))?$/);
	if (!match) return text;

	const templateName = match[1];
	const argsString = match[2] ?? "";

	const template = templates.find((t) => t.name === templateName);
	if (template) {
		const args = parseCommandArgs(argsString);
		return substituteArgs(template.content, args);
	}

	return text;
}
```

**讲解**

`prompt()` 处理以 `/` 开头的输入有固定顺序（`packages/coding-agent/src/core/agent-session.ts:2122-2155`）：先试扩展命令（→ M7），再发 `input` 事件给扩展拦截，最后展开 skill 命令和 prompt template。展开发生在构造 user 消息之前，所以 transcript 和会话文件里只有展开后的文本。§2.2 的会话文件里，user 消息是 `读 README.md，总结成三句话`，看不到 `/sum`。

`parseCommandArgs`（`packages/coding-agent/src/core/prompt-templates.ts:25`）按 shell 的方式切分参数，支持引号。`substituteArgs`（`packages/coding-agent/src/core/prompt-templates.ts:76`）替换 `$1`、`$@`、`$ARGUMENTS`、`${N:-默认值}`、`${@:N}`、`${@:N:L}`。替换只做一遍，参数里的 `$1` 不会再被替换。名字找不到模板时原样返回，`/foo` 会作为普通文本发给模型。

`/skill:name 参数` 由 `_expandSkillCommand`（`packages/coding-agent/src/core/agent-session.ts:2232-2256`）展开：读出 skill 文件，去掉 frontmatter，包成 `<skill name="…" location="…">` 块，参数接在后面。这是 skill 的第二种用法：用户手动把正文塞进 user 消息，不等模型去读。`disable-model-invocation: true` 的 skill 只能这样用。

**往哪走**：资源和选项都齐了。§4.11 读段落怎么和 transcript 比较。

### 4.11 diffSystemPromptSections() 和 getCurrentSystemMessage()

**调用位置**：`_preparePromptAndToolLoadout()`（§4.12）。

源码：[`packages/coding-agent/src/core/system-prompt.ts:211-228`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/system-prompt.ts#L211-L228)

```ts
/**
 * Diff the sections the model currently has (replayed from the transcript, so never null)
 * against the desired ones. Returns a `SystemMessage.sections` patch, or undefined when
 * nothing changed.
 */
export function diffSystemPromptSections(
	previous: Record<string, string | null>,
	current: SystemPromptSections,
): Record<string, string | null> | undefined {
	const patch: Record<string, string | null> = {};
	for (const [name, text] of Object.entries(current)) {
		if (previous[name] !== text) patch[name] = text;
	}
	for (const name of Object.keys(previous)) {
		if (current[name] === undefined) patch[name] = null;
	}
	return Object.keys(patch).length > 0 ? patch : undefined;
}
```

`previous` 是"模型现在拥有的段落"，由 pi-ai 的 `getCurrentSystemMessage` 重放 transcript 里全部 system 消息得到：

源码：[`packages/ai/src/utils/transcript.ts:73-96`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/ai/src/utils/transcript.ts#L73-L96)

```ts
export function getCurrentSystemMessage(messages: TranscriptMessages): SystemMessage | undefined {
	const content: string[] = [];
	const sections = new Map<string, string>();
	let timestamp: number | undefined;
	for (const message of messages) {
		if (!isSystemMessage(message)) continue;
		timestamp ??= message.timestamp;
		const text = contentText(message.content);
		if (text.length > 0) content.push(text);
		for (const [name, value] of Object.entries(message.sections ?? {})) {
			if (value === null) sections.delete(name);
			else sections.set(name, value);
		}
	}
	const tools = getCurrentTools(messages);
	if (timestamp === undefined && tools.length === 0) return undefined;
	return {
		role: "system",
		content: content.join("\n\n"),
		...(sections.size > 0 ? { sections: Object.fromEntries(sections) } : {}),
		...(tools.length > 0 ? { toolsAdded: tools } : {}),
		timestamp: timestamp ?? 0,
	};
}
```

**讲解**

两个函数是一对：`getCurrentSystemMessage` 把一串补丁合成一份完整状态，`diffSystemPromptSections` 把两份完整状态的差别变成一个补丁。补丁的规则就是 `SystemMessage.sections` 注释里写的：按名字替换，`null` 删除。

差分的三种结果：

| 情况 | 结果 | 例子 |
|---|---|---|
| transcript 里还没有 system 消息 | `previous` 是 `{}`，每一段都"变了"，补丁就是全部段落 | 新会话的第一次 prompt（§2.1） |
| 段落文本完全相同 | 返回 `undefined`，不产生 system 消息 | 同一会话的第 2 次 prompt，资源没变 |
| 有段落增加、修改或消失 | 只含这些段落的补丁 | 中途加了 AGENTS.md（§7.3） |

比较用的是 `!==`，逐字符比较整段文本。所以 §4.1 的"纯函数"很重要：只要输入没变，输出就一字不差，不会因为时间戳或随机顺序产生假的变化。`cwd` 段只是路径，不带日期；`docs` 段的路径也是固定的。

重放时用 `Map` 记录段落：已有的名字原地替换，保持原来的位置；新名字追加到最后。所以 §7.3 中途加入的 `project_context` 在重放后排在 `cwd` 后面，和新会话里的位置（`docs` 和 `cwd` 之间）不同。模型实际看到的顺序取决于 provider：支持中途 system 消息的 API 看到"开头的提示词 + 后面的一条更新"；不支持的由 `collapseSystemMessages` 合成一条开头的 system 消息，顺序就是重放顺序（→ M1 `resolveTranscript`）。

**往哪走**：§4.12 看是谁调用这两个函数。

### 4.12 _prepareRun()、emitBeforeAgentStart()、_preparePromptAndToolLoadout()

**调用位置**：`session.prompt()` → `_startRun()` → `_prepareRun(trigger)`（→ M8）。每次运行调用一次。

**第一段，交给扩展。**

源码：[`packages/coding-agent/src/core/agent-session.ts:1955-1963`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1955-L1963)

```ts
		const selectedToolsBefore = this._baseSystemPromptOptions.selectedTools;
		const result = await this._extensionRunner.emitBeforeAgentStart(trigger, this._baseSystemPromptOptions);
		// Handlers may edit event.systemPromptOptions.selectedTools or call setActiveTools(),
		// which updates the live loadout instead. An explicit edit wins; otherwise the live
		// loadout is authoritative, so a setActiveTools() call is not undone here.
		const handlerEditedTools =
			result.systemPromptOptions.selectedTools.length !== selectedToolsBefore.length ||
			result.systemPromptOptions.selectedTools.some((name, index) => name !== selectedToolsBefore[index]);
		if (!handlerEditedTools) result.systemPromptOptions.selectedTools = this.getActiveToolNames();
```

`emitBeforeAgentStart` 在 M7 的 `ExtensionRunner` 里。和 M6 有关的是它怎么处理选项：

源码：[`packages/coding-agent/src/core/extensions/runner.ts:1427-1462`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/extensions/runner.ts#L1427-L1462)

```ts
	async emitBeforeAgentStart(
		trigger: BeforeAgentStartTrigger,
		systemPromptOptions: BuildSystemPromptOptions,
	): Promise<BeforeAgentStartCombinedResult> {
		const currentOptions = normalizeBuildSystemPromptOptions(systemPromptOptions);
		const renderCurrentSystemPrompt = (): string => buildSystemPrompt(currentOptions);
		const ctx = Object.defineProperties(
			{},
			Object.getOwnPropertyDescriptors(this.createContext()),
		) as ExtensionContext;
		ctx.getSystemPrompt = () => {
			this.assertActive();
			return renderCurrentSystemPrompt();
		};
		const messages: NonNullable<BeforeAgentStartEventResult["message"]>[] = [];

		for (const { ext, handlers } of snapshotEventHandlers(this.extensions, "before_agent_start")) {
			for (const handler of handlers) {
				try {
					const event: BeforeAgentStartEvent = {
						...trigger,
						type: "before_agent_start",
						get systemPrompt() {
							return renderCurrentSystemPrompt();
						},
						systemPromptOptions: currentOptions,
					};
					const handlerResult = await handler(event, ctx);

					if (handlerResult) {
						const result = handlerResult as BeforeAgentStartEventResult;
						if (result.message) messages.push(result.message);
						if (result.systemPrompt !== undefined) {
							currentOptions.forceSystemPrompt = result.systemPrompt;
						}
					}
```

**第二段，拼段落、做差分。**

源码：[`packages/coding-agent/src/core/agent-session.ts:1994-1997`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1994-L1997)

```ts
		const updateMessage = this._preparePromptAndToolLoadout(result.systemPromptOptions);
		this._runSystemPromptOptions = result.systemPromptOptions;
		if (updateMessage) messages.unshift(updateMessage);
		return messages;
```

源码：[`packages/coding-agent/src/core/agent-session.ts:1769-1791`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1769-L1791)

```ts
	/**
	 * Apply a prompt and tool loadout for the next request. Sets the executable tools and
	 * returns a system message patching the prompt sections the model currently has (replayed
	 * from `messages`), or undefined when the prompt is unchanged. Tool changes are declared by
	 * the agent loop before the request.
	 *
	 * A forced prompt does not affect the transcript: the structured sections are still diffed
	 * and persisted, and the forced text is projected onto the request by
	 * {@link _installAgentForcedPromptProjection}.
	 */
	private _preparePromptAndToolLoadout(
		options: NormalizedBuildSystemPromptOptions,
		messages: AgentMessage[] = this.agent.state.messages,
	): SystemMessage | undefined {
		options.selectedTools = this._applyToolLoadout(options.selectedTools).map((tool) => tool.name);
		// The tool list and rules must match the declarations the request carries.
		options.hiddenTools = [...this._hiddenDeclarations];
		const sections = diffSystemPromptSections(
			getCurrentSystemMessage(messages)?.sections ?? {},
			buildSystemPromptSections(options),
		);
		return sections ? { role: "system", content: "", sections, timestamp: Date.now() } : undefined;
	}
```

**讲解**

数据流是：`_baseSystemPromptOptions` → 复制一份交给扩展（`normalizeBuildSystemPromptOptions` 复制，§3.1）→ 扩展直接改 `event.systemPromptOptions` 的字段 → `_preparePromptAndToolLoadout` 拼段落、和 transcript 差分 → 得到一条 system 补丁消息或 `undefined`。

扩展有三种改法：

- 改选项字段：往 `promptGuidelines` 加规则、往 `sections` 加段落、改 `selectedTools`。`m6-dump-ext.ts` 就是这样做的（§4.2）。这些修改只影响这一次运行，`_baseSystemPromptOptions` 不变。
- 返回 `systemPrompt`：设成 `forceSystemPrompt`，整体替换提示词文本（§4.13）。
- 读 `event.systemPrompt` 或 `ctx.getSystemPrompt()`：它们是 getter，每次读都按当前选项重新渲染，能看到前面的扩展做的修改。

`_preparePromptAndToolLoadout` 先调用 `_applyToolLoadout`（`packages/coding-agent/src/core/agent-session.ts:1614`）把 `selectedTools` 设成可执行的工具，并更新 `agent.state.tools`。所以 `tools` 段列出的工具和 M2 的 `declareToolChanges` 写进 `toolsAdded` 的工具总是同一组。补丁消息被 `unshift` 到 user 消息前面，一起交给 `agent.prompt(messages)`。M2 的 `declareToolChanges` 发现 prompt 消息里已经有一条 system 消息，就把工具变化写进这一条（`packages/agent/src/agent-loop.ts:362-365`），而不是另插一条。这就是 §2.1 的第一条 system 消息同时带 `sections` 和 `toolsAdded` 的原因。

`_runSystemPromptOptions` 保存这次运行的选项，供 §4.13 的下一轮刷新和 `session.systemPrompt` 使用。

**往哪走**：§4.13。

### 4.13 _installAgentNextTurnRefresh() 和强制提示词投影

**调用位置**：两个 hook 都在 `AgentSession` 构造函数里安装（`packages/coding-agent/src/core/agent-session.ts:528`、`packages/coding-agent/src/core/agent-session.ts:532`）。前者在 M2 的 `prepareNextTurn` 时机执行（第 2 轮起，每轮之间），后者在 `transformContext` 时机执行（每次请求前）。

源码：[`packages/coding-agent/src/core/agent-session.ts:920-947`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L920-L947)

```ts
		this.agent.prepareNextTurnWithContext = async (turn, signal) => {
			const context = await this._compactBeforeNextAssistantResponse(turn.context);
			const previousSnapshot = await previousPrepareNextTurnWithContext?.({ ...turn, context }, signal);
			const nextContext = previousSnapshot?.context ?? context;
			const runOptions = this._runSystemPromptOptions ?? this._baseSystemPromptOptions;
			const options = normalizeBuildSystemPromptOptions({
				...runOptions,
				selectedTools: this.getActiveToolNames(),
				toolSnippets: { ...this._baseSystemPromptOptions.toolSnippets, ...runOptions.toolSnippets },
				toolGuidelines: { ...this._baseSystemPromptOptions.toolGuidelines, ...runOptions.toolGuidelines },
			});
			const updateMessage = this._preparePromptAndToolLoadout(options, nextContext.messages);
			// Keep session.systemPrompt and ctx.getSystemPrompt() in step with what the provider sees.
			this._runSystemPromptOptions = options;

			return {
				...previousSnapshot,
				context: {
					...nextContext,
					tools: this.agent.state.tools.slice(),
				},
				messages: updateMessage
					? [...(previousSnapshot?.messages ?? []), updateMessage]
					: previousSnapshot?.messages,
				model: this.agent.state.model,
				thinkingLevel: this.agent.state.thinkingLevel,
			};
		};
```

**讲解**

一次运行可能有很多轮。轮与轮之间工具可能变了：工具调用了 `tool_search` 加载新工具，或者扩展在 `turn_end` 里调用了 `setActiveTools()`。这个 hook 在每轮开始前用最新的激活工具重新拼一次段落，差分出补丁，作为待处理消息交给 M2（→ M2 §4.5 ①）。工具不变时 `updateMessage` 是 `undefined`，主线场景第 2 轮就是这样，所以 §2.1 只有一条 system 消息。

它沿用本次运行的选项（扩展在 `before_agent_start` 里加的规则和段落继续有效），只替换 `selectedTools`，并把基础选项的 snippet 和 guidelines 合并进来，这样运行中途新注册的工具也有说明。

强制提示词的处理在另一个 hook 里：

源码：[`packages/coding-agent/src/core/agent-session.ts:1828-1843`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1828-L1843)

```ts
	private _installAgentForcedPromptProjection(): void {
		const previousTransformContext = this.agent.transformContext;
		this.agent.transformContext = async (messages, signal) => {
			const transformed = previousTransformContext ? await previousTransformContext(messages, signal) : messages;
			const forced = this._runSystemPromptOptions?.forceSystemPrompt;
			if (forced === undefined) return transformed;
			const current = getCurrentSystemMessage(transformed);
			const head: SystemMessage = {
				role: "system",
				content: forced,
				...(current?.toolsAdded ? { toolsAdded: current.toolsAdded } : {}),
				timestamp: current?.timestamp ?? Date.now(),
			};
			return [head, ...transformed.filter((message) => message.role !== "system")];
		};
	}
```

扩展返回的 `systemPrompt` 不写进 transcript。transcript 里仍然按结构化选项差分、持久化段落（`_preparePromptAndToolLoadout` 的注释写明了这一点），只在请求前把所有 system 消息换成一条：`content` 是强制文本，`toolsAdded` 是当前工具。这样会话文件保持结构化，下一次没有强制提示词的运行可以接着差分；强制文本只影响它所在的那次运行的请求。

**往哪走**：§4.14 读设置。

### 4.14 SettingsManager：合并规则

**调用位置**：`main()` 为每个运行时创建一个 `SettingsManager`（`packages/coding-agent/src/main.ts:760`），信任状态由 §4.15 算出。SDK 不传时由 `createAgentSessionServices` 自己创建（`packages/coding-agent/src/core/agent-session-services.ts:147`）。

源码：[`packages/coding-agent/src/core/settings-manager.ts:61-82`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/settings-manager.ts#L61-L82)

```ts
function isMergeableObject(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function deepMergeObjects(base: Record<string, unknown>, overrides: Record<string, unknown>): Record<string, unknown> {
	const result = { ...base };

	for (const key of Object.keys(overrides)) {
		const overrideValue = overrides[key];
		if (overrideValue === undefined) {
			continue;
		}

		const baseValue = base[key];
		result[key] =
			isMergeableObject(baseValue) && isMergeableObject(overrideValue)
				? deepMergeObjects(baseValue, overrideValue)
				: overrideValue;
	}

	return result;
}
```

源码：[`packages/coding-agent/src/core/settings-manager.ts:122-147`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/settings-manager.ts#L122-L147)

```ts
/**
 * Merge `defaultTools` of two settings layers. A list with plain tool names replaces the inherited
 * one; a list of only `+name`/`-name` entries is appended, so it modifies the inherited selection.
 */
function mergeDefaultTools(base: string[] | undefined, overrides: string[] | undefined): string[] | undefined {
	if (overrides === undefined) return base;
	// Settings files are not validated; a malformed value replaces instead of throwing here.
	if (!Array.isArray(base) || !Array.isArray(overrides) || !overrides.every(isToolModifier)) return overrides;
	return [...base, ...overrides];
}

/**
 * Resolve a merged `defaultTools` list: plain names replace `DEFAULT_TOOL_NAMES`, then `+name` adds
 * and `-name` removes a tool, in list order.
 */
function resolveDefaultTools(entries: string[]): string[] {
	const plain = entries.filter((entry) => !isToolModifier(entry));
	return applyToolModifiers(plain.length > 0 || entries.length === 0 ? plain : DEFAULT_TOOL_NAMES, entries);
}

/** Deep merge settings: project/overrides take precedence, nested objects merge recursively */
function deepMergeSettings(base: Settings, overrides: Settings): Settings {
	const merged = deepMergeObjects(base as Record<string, unknown>, overrides as Record<string, unknown>) as Settings;
	const defaultTools = mergeDefaultTools(base.defaultTools, overrides.defaultTools);
	return defaultTools === undefined ? merged : { ...merged, defaultTools };
}
```

项目设置的读取受信任控制：

源码：[`packages/coding-agent/src/core/settings-manager.ts:366-382`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/settings-manager.ts#L366-L382)

```ts
	private static loadFromStorage(storage: SettingsStorage, scope: SettingsScope, projectTrusted = true): Settings {
		if (scope === "project" && !projectTrusted) {
			return {};
		}

		let content: string | undefined;
		storage.withLock(scope, (current) => {
			content = current;
			return undefined;
		});

		if (!content) {
			return {};
		}
		const settings = JSON.parse(stripBom(content));
		return SettingsManager.migrateSettings(settings);
	}
```

**讲解**

合并规则：

| 值的类型 | 规则 | 例子（全局 → 项目 → 结果） |
|---|---|---|
| 对象 | 按键递归合并 | `{reserveTokens:8000}` + `{keepRecentTokens:4000}` → 两个键都有 |
| 数组、字符串、数字、布尔 | 项目整个替换全局 | `skills: ["a"]` + `skills: ["b"]` → `["b"]` |
| `undefined` | 跳过，保留全局 | — |
| `defaultTools`，项目全是 `+x`/`-x` | 拼接到全局后面 | `["-write"]` + `["-edit"]` → `["-write","-edit"]` |
| `defaultTools`，项目有普通名字 | 整个替换 | `["-write"]` + `["read","grep","+ls"]` → `["read","grep","+ls"]` |

`getDefaultTools()`（`packages/coding-agent/src/core/settings-manager.ts:1357-1361`）再用 `resolveDefaultTools` 把合并结果解析成工具名：有普通名字就以它们为起点，否则以 `read, bash, edit, write` 为起点，再依次应用 `+`/`-`。§3.5 的实例是第一种，§4.2 引用的 `m6-work-9` 是第二种：

```bash
node --conditions=source --experimental-strip-types m6-settings.ts /tmp/pi-lab/m6-work-9 /tmp/pi-lab/m6-agent-9 | tail -4
```

```
  global:       {"defaultTools":["-write"]}
  project:      {"defaultTools":["read","grep","+ls"]}
  merged:       {"defaultTools":["read","grep","+ls"]}
  getDefaultTools(): ["read","grep","ls"]
```

`m6-work-9` 的两个设置文件这样创建：

```bash
A=/tmp/pi-lab/m6-agent-9; W=/tmp/pi-lab/m6-work-9
mkdir -p $A $W/.pi
printf '{\n  "defaultTools": ["-write"]\n}\n' > $A/settings.json
printf '{\n  "defaultTools": ["read", "grep", "+ls"]\n}\n' > $W/.pi/settings.json
```

用它运行主线场景（要加 `--approve`，原因见 §4.15），再查看段落，就得到 §4.2 末尾的 `tools` 和 `rules`：

```bash
WORK=$W PI_CODING_AGENT_DIR=$A PI_TRACE=1 ./run.sh --approve -p "读 README.md，总结成三句话" 2>&1 | head -4
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t $A/sessions/*/*.jsonl | head -1)" tools rules
```

```
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,grep,ls]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
```

（第二条命令的输出见 §4.2。）

`getDefaultTools()` 的结果在创建会话时成为初始激活工具（`packages/coding-agent/src/core/sdk.ts:282`），进而决定 `tools` 和 `rules` 段。

第三层是 `applyOverrides()`（`packages/coding-agent/src/core/settings-manager.ts:529-531`），用于 CLI 参数之类的运行时覆盖，同样走 `deepMergeSettings`，只改内存不落盘。

不信任时项目设置直接是 `{}`，文件根本不打开。`setProjectTrusted()`（`packages/coding-agent/src/core/settings-manager.ts:475-498`）在信任状态改变时重新读项目设置并重算合并结果，§4.5 的 `reload()` 就靠它在问完信任之后切换状态。

**往哪走**：§4.15 信任是怎么决定的。

### 4.15 resolveProjectTrusted()：项目信任

**调用位置**：`main()` 的 `createRuntime` 先算一个初始值，需要问的时候把 `resolveProjectTrusted` 包成 `resolveProjectTrust` 交给 `reload()`（§4.5）。

源码：[`packages/coding-agent/src/main.ts:754-760`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L754-L760)

```ts
		const hasTrustRequiringResources = hasTrustRequiringProjectResources(cwd);
		const shouldResolveProjectTrust =
			parsed.projectTrustOverride === undefined && cachedProjectTrust === undefined && hasTrustRequiringResources;
		const projectTrusted = shouldResolveProjectTrust
			? false
			: (cachedProjectTrust ?? parsed.projectTrustOverride ?? trustStore.get(cwd) ?? !hasTrustRequiringResources);
		const runtimeSettingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted });
```

源码：[`packages/coding-agent/src/core/project-trust.ts:47-97`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/project-trust.ts#L47-L97)

```ts
export async function resolveProjectTrusted(options: ResolveProjectTrustedOptions): Promise<boolean> {
	if (options.trustOverride !== undefined) {
		return options.trustOverride;
	}
	if (!hasTrustRequiringProjectResources(options.cwd)) {
		return options.trustStore.get(options.cwd) ?? true;
	}

	if (options.extensionsResult) {
		const { result, errors } = await emitProjectTrustEvent(
			options.extensionsResult,
			{ type: "project_trust", cwd: options.cwd },
			options.projectTrustContext,
		);
		for (const error of errors) {
			options.onExtensionError?.(`Extension "${error.extensionPath}" project_trust error: ${error.error}`);
		}
		if (result) {
			const trusted = result.trusted === "yes";
			if (result.remember === true) {
				options.trustStore.set(options.cwd, trusted);
			}
			return trusted;
		}
	}

	const decision = options.trustStore.get(options.cwd);
	if (decision !== null) {
		return decision;
	}

	switch (options.defaultProjectTrust ?? SETTINGS_DEFAULTS.defaultProjectTrust) {
		case "always":
			return true;
		case "never":
			return false;
		case "ask":
			break;
	}

	if (!options.projectTrustContext.hasUI) {
		return false;
	}

	const selected = await selectProjectTrustOption(options.cwd, options.projectTrustContext);
	if (selected !== undefined) {
		saveProjectTrustPromptResult(options.trustStore, selected);
		return selected.trusted;
	}
	return false;
}
```

**讲解**

决定按优先级从高到低：

1. CLI 参数：`--approve`（`-a`）信任，`--no-approve`（`-na`）不信任（`packages/coding-agent/src/cli/args.ts:241-244`）。只对这一次运行有效，不写文件。
2. 项目里没有需要信任的资源：直接信任（除非 `trust.json` 里记过"不信任"）。什么都没有，也就没什么可防的。
3. 全局扩展处理 `project_trust` 事件给出的答案，可以选择记住。
4. `<agentDir>/trust.json` 里记住的决定。`ProjectTrustStore.get()` 从 cwd 往上找最近的记录（`packages/coding-agent/src/core/trust-manager.ts:50-66`），所以"信任父目录"对所有子目录生效。
5. 全局设置 `defaultProjectTrust`：`always` 或 `never`。
6. 交互模式弹出选择框（信任、信任父目录、仅本次信任、不信任……，`packages/coding-agent/src/core/trust-manager.ts:74-104`），结果写进 `trust.json`。
7. 没有界面（print、json、rpc 模式）：不信任。

"需要信任的资源"由 `hasTrustRequiringProjectResources`（`packages/coding-agent/src/core/trust-manager.ts:193-215`）判断：`<cwd>/.pi/` 下有 §4.5 表里的任何一项，或者 cwd 及其祖先目录有 `.agents/skills`（`~/.agents/skills` 除外）。

所以在一个带 `.pi/settings.json` 的陌生项目里运行 `pi -p`，走到第 7 步，项目设置和 `.pi/skills` 都不生效，pi 照常运行，不报错。§7.2 验证了这一点。

**往哪走**：读完了。§5 把流程画成状态图。

---

## 5. 状态机

### 5.1 transcript 里的系统提示词

```
                       _prepareRun() → _preparePromptAndToolLoadout()
 ┌──────────────────┐  diff({}, 全部段落) 得到全部段落          ┌─────────────────────────┐
 │ 没有 system 消息  │ ─────────────────────────────────────▶ │ 有完整段落                │
 │ （新会话）         │  agent-session.ts:1786-1790            │ system #1: sections 全部  │
 └──────────────────┘  agent-loop.ts:362-365 写入 toolsAdded   │           + toolsAdded    │
                                                              └────────────┬────────────┘
             ┌─────────────────────────────────────────────────────────────┤
             │ 每次运行开始 (agent-session.ts:1994)                         │
             │ 每轮之间 (agent-session.ts:931)                              │
             ▼                                                             │
   buildSystemPromptSections(当前选项)                                      │
   diffSystemPromptSections(重放结果, 新段落) (system-prompt.ts:216)        │
             │                                                             │
             ├─ 全部相同 → 返回 undefined (227)，不追加消息 ──────────────────┤
             ├─ 有段落新增/修改 → 追加 system #n: sections {名字: 新文本} ───┤
             └─ 有段落消失 → 追加 system #n: sections {名字: null} (225) ────┘

 重放 getCurrentSystemMessage (transcript.ts:73)：按名字替换 (84)，null 删除 (83)

 forceSystemPrompt 不改变这张图：transcript 照常差分；
 请求前 _installAgentForcedPromptProjection 把所有 system 消息换成一条 (agent-session.ts:1841)
```

### 5.2 项目信任的决定

```
 main(): hasTrustRequiringProjectResources(cwd)? (main.ts:754)
   │ 否，或有 --approve / --no-approve，或本进程已经问过
   │   → projectTrusted 直接算出 (main.ts:759)，不再问
   │ 是，且都没有
   ▼
 SettingsManager.create(..., { projectTrusted: false }) (main.ts:760)
 reload(): loadProjectTrustExtensions() 只加载全局/CLI 扩展 (resource-loader.ts:550)
   ▼
 resolveProjectTrusted (project-trust.ts:47)
   扩展 project_trust 给出答案 ──────────────────▶ yes/no (65)
   trust.json 有记录 ───────────────────────────▶ 记录 (74)
   defaultProjectTrust = always / never ────────▶ true / false (79-82)
   没有 UI ─────────────────────────────────────▶ false (87-88)
   UI 选择 ─────────────────────────────────────▶ 选择结果，写 trust.json (91-94)
   ▼
 settingsManager.setProjectTrusted(结果) (resource-loader.ts:552)
   → 重新读项目设置 (settings-manager.ts:491)
   → 后续扫描 .pi/skills、.pi/prompts、.pi/SYSTEM.md、.agents/skills 时读取这个状态
```

---

## 6. 设计要点

### 6.1 提示词分段落，变化只追加补丁

**做法。** 系统提示词不是一个字符串，而是一组有名字的段落。每次运行和每轮之间重新拼一次，和 transcript 重放出的段落比较，只把变化的段落写成一条新的 system 消息。

**理由。** 已经发出的 transcript 前缀从不改写。对模型来说，开头的提示词和之前的对话保持原样，provider 的 prompt cache 继续命中（→ M1 prompt cache）；对会话文件来说，任意时刻模型拥有的提示词都能从文件里重放出来，`pi -c` 恢复会话时也是靠重放和差分接上的（§7.3）。代价是重放后的段落顺序可能和新会话不同（§4.11）。

**代码位置。** `packages/coding-agent/src/core/system-prompt.ts:216`，`packages/ai/src/utils/transcript.ts:73`。

### 6.2 扩展改的是选项，不是文本

**做法。** `before_agent_start` 事件把 `NormalizedBuildSystemPromptOptions` 交给扩展。扩展往数组和对象里加东西，最后统一由 `buildSystemPromptSections` 渲染。直接替换整个文本（`systemPrompt`）也可以，但它不进 transcript，只在请求时投影。

**理由。** 多个扩展可以各自加规则和段落，互不覆盖，规则还能自动去重。文本保持结构化，§6.1 的差分照常工作。如果扩展直接拿字符串拼接，pi 就无法知道哪一段变了。

**代码位置。** `packages/coding-agent/src/core/extensions/runner.ts:1427`，`packages/coding-agent/src/core/agent-session.ts:1828`。

### 6.3 skill 只放目录，正文按需读取

**做法。** 系统提示词里每个 skill 只占五行：名字、描述、文件路径。模型判断任务匹配时自己用 read 工具读正文。没有可用的读取工具时整段不输出。

**理由。** skill 正文可能很长，全部放进提示词会占满上下文，也会让每次请求都为用不到的说明付费。描述足够让模型判断要不要读。用户也可以用 `/skill:name` 直接把正文塞进对话。

**代码位置。** `packages/coding-agent/src/core/skills.ts:358`，`packages/coding-agent/src/core/system-prompt.ts:174-181`。

### 6.4 信任边界划在"能改变行为的东西"上，并且最先决定

**做法。** 项目设置、扩展、skills、prompt templates、`SYSTEM.md`、`APPEND_SYSTEM.md`、MCP 配置需要信任；AGENTS.md 不需要。信任在 `reload()` 的第一步决定，决定时只加载全局和 CLI 扩展。没有界面时默认不信任。

**理由。** 克隆一个陌生仓库后运行 pi，仓库里的 `.pi/extensions` 会执行任意代码，`.pi/settings.json` 能改 shell 命令前缀，`SYSTEM.md` 能换掉整个提示词。这些必须先问。AGENTS.md 只是给模型的文字，和用户自己粘贴到对话里的效果一样，而且它是项目说明的通用约定，默认就该读。信任先于一切加载，项目扩展就没有机会参与"要不要信任我"的决定。

**代码位置。** `packages/coding-agent/src/core/resource-loader.ts:549-553`，`packages/coding-agent/src/core/trust-manager.ts:30`，`packages/coding-agent/src/core/project-trust.ts:87`。

### 6.5 两层设置深合并，工具列表支持增量修改

**做法。** 对象按键递归合并，其他值项目覆盖全局。`defaultTools` 额外支持 `+name`/`-name`，全是修饰时在全局结果上叠加。

**理由。** 项目只需要写和全局不同的那几个键，例如只改 `compaction.keepRecentTokens`，不必复制整个 `compaction` 对象。工具列表是最常见的按项目调整的设置，"在默认基础上去掉 write"比"列出全部想要的工具"更不容易随版本过时。

**代码位置。** `packages/coding-agent/src/core/settings-manager.ts:143`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行。会话 id 每次运行都不同。

### 7.1 多个 AGENTS.md 的顺序

**问题。** 全局配置目录、项目的父目录、项目目录各有一个 AGENTS.md，项目目录里还有一个 CLAUDE.md。模型看到几份？什么顺序？

```bash
A=/tmp/pi-lab/m6-agent-3; P=/tmp/pi-lab/m6-e1; W=$P/proj
mkdir -p $A $W
printf '全局：回答用中文。\n' > $A/AGENTS.md
printf '父目录：提交信息用英文。\n' > $P/AGENTS.md
printf '项目：测试用 vitest。\n' > $W/AGENTS.md
printf '项目 CLAUDE.md：不会被读到。\n' > $W/CLAUDE.md
```

**预测。** 三份，顺序是全局、父目录、项目（§4.7：全局在前，祖先目录从根往下）。项目目录的 CLAUDE.md 不读，因为同一个目录里 AGENTS.md 排在前面。`/tmp/pi-lab` 及更上层没有 AGENTS.md。

**运行。**

```bash
WORK=$W PI_CODING_AGENT_DIR=$A ./run.sh
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t $A/sessions/*/*.jsonl | head -1)" project_context
```

```
Running without API keys...
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

```
system #1 id=1850c9a2 content=""
  sections: preamble(126) tools(274) rules(589) docs(1179) project_context(398) cwd(43)
  toolsAdded: read,bash,edit,write
重放后 sections 顺序: preamble,tools,rules,docs,project_context,cwd
重放后 tools: read,bash,edit,write
---- project_context ----
<project_context>
Project-specific instructions and guidelines:

<project_instructions path="/tmp/pi-lab/m6-agent-3/AGENTS.md">
全局：回答用中文。

</project_instructions>

<project_instructions path="/private/tmp/pi-lab/m6-e1/AGENTS.md">
父目录：提交信息用英文。

</project_instructions>

<project_instructions path="/private/tmp/pi-lab/m6-e1/proj/AGENTS.md">
项目：测试用 vitest。

</project_instructions>
</project_context>
```

**结论。** 预测成立。CLAUDE.md 的内容没有出现。全局文件的路径是 `/tmp/...`，祖先目录的路径是 `/private/tmp/...`（§4.7 最后一段）。

### 7.2 陌生项目在 print 模式下不被信任

**问题。** 项目里有 `.pi/settings.json`（`defaultTools: ["-edit"]`）、`.pi/skills/deploy/SKILL.md` 和 AGENTS.md，全局设置是 `defaultTools: ["-write"]`。不加参数和加 `--approve` 各运行一次，模型拿到的工具和段落有什么不同？

```bash
A=/tmp/pi-lab/m6-agent-10; W=/tmp/pi-lab/m6-work-10
mkdir -p $A $W/.pi/skills/deploy
printf '{\n  "defaultTools": ["-write"]\n}\n' > $A/settings.json
printf '{\n  "defaultTools": ["-edit"]\n}\n' > $W/.pi/settings.json
printf -- '---\ndescription: Deploy this project to staging.\n---\n\nRun ./deploy.sh staging.\n' > $W/.pi/skills/deploy/SKILL.md
printf '项目：测试用 vitest。\n' > $W/AGENTS.md
```

**预测。**

- 不加参数：项目有需要信任的资源，`trust.json` 不存在，`defaultProjectTrust` 是默认的 `ask`，print 模式没有界面，走到 §4.15 第 7 步，不信任。项目设置被忽略，工具是 `read, bash, edit`；`.pi/skills` 不扫描，没有 `skills` 段；AGENTS.md 不看信任，`project_context` 段存在。
- 加 `--approve`：信任。`defaultTools` 合并成 `["-write","-edit"]`，工具是 `read, bash`；多出 `skills` 段，`deploy` skill 的名字取自目录名。`--approve` 不写 `trust.json`。

**运行。**

```bash
WORK=$W PI_CODING_AGENT_DIR=$A PI_TRACE=1 ./run.sh -p "读 README.md，总结成三句话" 2>&1 | head -4
WORK=$W PI_CODING_AGENT_DIR=$A PI_TRACE=1 ./run.sh --approve -p "读 README.md，总结成三句话" 2>&1 | head -4
ls $A
```

```
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,project_context,cwd] toolsAdded=[read,bash,edit]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
```

```
Running without API keys...
[faux] 第 1 次请求，收到 2 条消息:
  system     sections=[preamble,tools,rules,docs,project_context,skills,cwd] toolsAdded=[read,bash]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
```

```
auth.json		sessions
models-store.json	settings.json
```

第二次运行的 `skills` 段：

```bash
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t $A/sessions/*/*.jsonl | head -1)" skills
```

```
system #1 id=c5a6fc6f content=""
  sections: preamble(126) tools(132) rules(281) docs(1179) project_context(194) skills(512) cwd(43)
  toolsAdded: read,bash
重放后 sections 顺序: preamble,tools,rules,docs,project_context,skills,cwd
重放后 tools: read,bash
---- skills ----
<skills>
The following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
Resolve relative paths against the skill's directory (parent of SKILL.md). Use absolute paths in tool calls.

<available_skills>
  <skill>
    <name>deploy</name>
    <description>Deploy this project to staging.</description>
    <location>/private/tmp/pi-lab/m6-work-10/.pi/skills/deploy/SKILL.md</location>
  </skill>
</available_skills>
</skills>
```

**结论。** 预测都成立。不信任时 pi 没有报错也没有提示，只是安静地忽略项目配置，这在脚本里运行 pi 时要注意：想让项目配置生效，要加 `--approve`，或者事先在交互模式里信任一次（写进 `trust.json`），或者把全局 `defaultProjectTrust` 设成 `always`。配置目录里没有 `trust.json`，说明 `--approve` 没有被记住。

### 7.3 会话中途修改 AGENTS.md

**问题。** 先跑一次主线场景（没有 AGENTS.md）。然后在项目里加一个 AGENTS.md，用 `-c` 继续这个会话。transcript 里会多什么？再删掉 AGENTS.md 继续一次呢？

**预测。** `-c` 启动新进程，`reload()` 读到新的 AGENTS.md。`_prepareRun` 重放旧会话的 system 消息，得到五个段落；新拼出的段落多了 `project_context`，其余五段一字不差（cwd 和工具都没变）。差分只有 `project_context`，所以新增一条只带这一段的 system 消息，没有 `toolsAdded`。重放后 `project_context` 排在 `cwd` 后面（§4.11）。删掉文件后再继续，差分得到 `project_context: null`。

**运行。**

```bash
export WORK=/tmp/pi-lab/m6-work-5 PI_CODING_AGENT_DIR=/tmp/pi-lab/m6-agent-5
./run.sh > /dev/null
printf '回答用中文。\n' > $WORK/AGENTS.md
PI_TRACE=1 ./run.sh -c -p "再总结一次"
```

```
Running without API keys...
[faux] 第 1 次请求，收到 7 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
  assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}]
  system     sections=[project_context] toolsAdded=[]
  user       [{"type":"text","text":"再总结一次"}]
[faux] 第 2 次请求，收到 9 条消息:
…省略…
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

（`faux-ext.ts` 的 trace 在消息没有 `toolsAdded` 时打印 `[]`。faux 模型的回复是写死的，所以第二次运行仍然先调用 read。）

```bash
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)" project_context
```

```
system #1 id=47e962bd content=""
  sections: preamble(126) tools(274) rules(589) docs(1179) cwd(42)
  toolsAdded: read,bash,edit,write
system #2 id=e246dfbf content=""
  sections: project_context(185)
重放后 sections 顺序: preamble,tools,rules,docs,cwd,project_context
重放后 tools: read,bash,edit,write
---- project_context ----
<project_context>
Project-specific instructions and guidelines:

<project_instructions path="/private/tmp/pi-lab/m6-work-5/AGENTS.md">
回答用中文。

</project_instructions>
</project_context>
```

再删掉 AGENTS.md（这里改名，避免删除命令）：

```bash
mv $WORK/AGENTS.md $WORK/AGENTS.md.off
./run.sh -c -p "第三次"
node --conditions=source --experimental-strip-types m6-show-system.ts "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)"
```

```
Running without API keys...
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
system #1 id=47e962bd content=""
  sections: preamble(126) tools(274) rules(589) docs(1179) cwd(42)
  toolsAdded: read,bash,edit,write
system #2 id=e246dfbf content=""
  sections: project_context(185)
system #3 id=8acc3607 content=""
  sections: project_context=null
重放后 sections 顺序: preamble,tools,rules,docs,cwd
重放后 tools: read,bash,edit,write
```

**结论。** 预测都成立。两次修改各追加了一条只有一个段落的 system 消息，第一条 system 消息和之前的对话都没有被改写。用同一个进程修改 AGENTS.md 时（交互模式），要先执行 `/reload` 让资源加载器重新扫描（§3.4），下一次运行才会产生这样的补丁。

---

## 8. 自测与衔接

### 8.1 自测

1. 在主线场景的同一个会话里紧接着再 `prompt()` 一次，资源和工具都没变。transcript 会新增 system 消息吗？
2. 用 `--system-prompt "You are a reviewer."` 启动，系统提示词有哪些段落？模型还能调用 read 吗？
3. 扩展在 `before_agent_start` 里返回 `{ systemPrompt: "..." }`，会话文件里能找到这段文本吗？

<details>
<summary>答案</summary>

1. 不会。`_prepareRun` 拼出的段落和重放结果完全相同，`diffSystemPromptSections` 返回 `undefined`（`packages/coding-agent/src/core/system-prompt.ts:227`），`_preparePromptAndToolLoadout` 也返回 `undefined`，只有 user 消息交给 `agent.prompt()`。工具也没变，`declareToolChanges` 不插入消息。
2. `preamble`（内容是 `You are a reviewer.`）和 `cwd`；有 AGENTS.md 时还有 `project_context`，有 skill 且 read/bash 被选中时还有 `skills`。`tools`、`rules`、`docs` 三段被去掉（`packages/coding-agent/src/core/system-prompt.ts:152-153`）。模型仍能调用 read：工具声明在 system 消息的 `toolsAdded` 里，和提示词文本无关。
3. 找不到。`forceSystemPrompt` 只存在 `_runSystemPromptOptions` 里，transcript 仍然按结构化选项差分（`packages/coding-agent/src/core/agent-session.ts:1775-1777` 的注释）。强制文本在请求前由 `_installAgentForcedPromptProjection` 投影到请求上（`packages/coding-agent/src/core/agent-session.ts:1841`），不经过会话文件。

</details>

### 8.2 衔接

本模块里有两处把控制交给了扩展：`before_agent_start` 改选项（§4.12），`project_trust` 决定信任（§4.15）。M7 从 `ExtensionRunner.emitToolCall()` 开始，读扩展事件怎么分发、哪些事件只是通知、哪些能改变流程，以及扩展怎么被 `loader.ts` 加载。

本模块里标了"→ M8"的 `_prepareRun`、`_applyToolLoadout` 和 hook 安装，在 M8 从 `createAgentSession()` 读起；`resolveTranscript` 和 prompt cache 在 M1。


