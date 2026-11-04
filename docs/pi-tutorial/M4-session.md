# M4 会话存储：会话文件与上下文重建

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

`SessionManager`（`packages/coding-agent/src/core/session-manager.ts`）负责会话存储。它做四件事：

- 把会话存成一个 JSONL 文件。文件第一行是会话头，后面每行是一个会话条目。
- 追加会话条目。每个条目的 `parentId` 指向前一个条目，所有条目组成一棵树。
- 沿当前分支把会话条目变回 Agent 消息，供下一次请求使用。这一步叫投影（projection）。
- 打开、继续、分支、复制会话文件。

`SessionManager` 不知道 agent 循环、模型和扩展。它只认会话条目。调用它的是 M8 的 AgentSession（写入消息、请求前重建上下文）、M9 的 `main()`（选择打开哪个会话文件）和 M5 的压缩（追加压缩条目）。

数据在这里有两个方向：

- **写入**：M2 的循环发出 `message_end` 事件，M8 调用 `appendMessage()`，会话文件多出一行。
- **读取**：每次请求模型前，M8 调用 `buildSessionProjection()`，从会话条目算出 Agent 消息列表，替换掉 transcript。

所以对 pi 来说，会话文件是上下文的唯一来源。内存里的 transcript 只是会话文件的一次投影。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M4 出现在三处：

```
cli.ts → main()                                                    [M9]
├─ createSessionManager()                                          [M9 → M4] ← 打开或新建会话文件（§4.9–§4.11）
├─ createAgentSessionRuntime(createRuntime)                        [M9]
│   └─ … createAgentSession()                                      [M8]
│       └─ sessionManager.buildSessionContext()                    [M8 → M4] ← 恢复已有会话（§4.12）
└─ runPrintMode() → session.prompt(text) → … → runLoop()
                    ├─ prepareRequest()                            [M8 → M4] ← buildSessionProjection()（§4.5）
                    └─ 每个事件 → AgentSession._handleAgentEvent()
                                 → SessionManager.appendMessage()  [M8 → M4] ← 本模块入口（§4.1）
```

README §3.1 三层消息里最上面的"会话条目"就是本模块的数据。本模块把会话条目变成中间那层 Agent 消息。Agent 消息再往下变成 LLM 消息，是 M8 `convertToLlm` 和 M1 的事。

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/coding-agent/src/core/session-manager.ts` | 2028 | 会话条目类型、投影函数、`SessionManager` 类 | 精读写入、投影、打开、分支四部分；会话列表（`buildSessionInfo`、`list`、`listAll`，给 `/resume` 选择界面用）和旧版本迁移只看结构 |
| `packages/coding-agent/docs/session-format.md` | 293 | 官方的会话文件格式说明，列出全部条目类型 | 对照阅读，§3.2 |
| `packages/coding-agent/src/core/messages.ts` | 196 | coding-agent 的四种自定义 Agent 消息，以及 `createCustomMessage` 等构造函数 | 只看投影用到的三个构造函数（§4.8） |
| `packages/coding-agent/src/main.ts` | 1010 | `createSessionManager()` 根据命令行参数选择会话 | 只看 `createSessionManager`（→ M9） |

行数用 `wc -l` 统计。

### 1.4 读完能回答的问题

1. 主线场景跑完，会话文件有几行？哪些行是在同一时刻写进磁盘的？为什么只启动 pi 不说话，不会留下会话文件？
2. 模型每次请求看到的消息是从哪里来的？会话文件里的 `label`、`custom` 条目会发给模型吗？
3. 回到一条旧消息重新提问，旧的回答去哪了？
4. `pi -c` 怎么选出要继续的会话？继续之后，模型第一次请求看到什么？
5. 写会话文件失败时，内存里的状态会怎样？

---

## 2. 先跑一次

### 2.1 主线场景留下的会话文件

运行主线场景，然后看配置目录里多了什么。`WORK` 和 `PI_CODING_AGENT_DIR` 用新目录，后面的命令都在同一个 shell 里执行。重复做这一节和 §7.1 时，要换一组新的目录名，否则会话目录里会有上一次留下的文件：

```bash
export WORK=/tmp/pi-lab/m4-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m4-agent
./run.sh
```

```
Running without API keys...
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
```

```bash
ls $PI_CODING_AGENT_DIR/sessions/ && ls $PI_CODING_AGENT_DIR/sessions/*/
```

```
--private-tmp-pi-lab-m4-work--
2026-11-01T07-05-32-450Z_01a195ed-8022-70cb-9024-a443607c2453.jsonl
```

会话文件在 `sessions/` 下按工作目录分子目录。目录名是把 cwd 的 `/` 换成 `-`，两头加 `--`（§4.9 `getDefaultSessionDirPath`）。macOS 上 `/tmp` 是 `/private/tmp` 的符号链接，所以目录名里是 `private-tmp`。文件名是"创建时间_会话 id"。

用 `show-session.mjs` 把文件打印成一行一个条目：

```bash
node show-session.mjs "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)"
```

```
session                id=01a195ed parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/m4-work"}
model_change           id=2c14b2d0 parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=bf773565 parent=2c14b2d0 {"thinkingLevel":"off"}
message                id=c28a59fb parent=bf773565 system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write
message                id=78be6f6a parent=c28a59fb user       [{"type":"text","text":"读 README.md，总结成三句话"}]
message                id=b1629b82 parent=78be6f6a assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_1","name":"read","ar stop=toolUse
message                id=8b71d46b parent=b1629b82 toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used 
message                id=dc56afaf parent=8b71d46b assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}] stop=stop
```

这 9 行分别由下面的代码写入。§4 会逐个读到这些位置：

| 行 | 条目 | 写入位置 | 说明 |
|---|---|---|---|
| 1 | `session` | `packages/coding-agent/src/core/session-manager.ts:1057` `newSession()` | 会话头，不是条目，没有 `id`/`parentId`（`show-session.mjs` 把 `parent` 显示成 `-`） |
| 2 | `model_change` | `packages/coding-agent/src/core/sdk.ts:450` | 新会话，记下初始模型。它是第一个条目，`parentId` 为 `null` |
| 3 | `thinking_level_change` | `packages/coding-agent/src/core/sdk.ts:452` | 记下初始思考级别 |
| 4–8 | `message` | `packages/coding-agent/src/core/agent-session.ts:1181` | 每个 `message_end` 事件一条：system、user、assistant、toolResult、assistant |

第 2–4 行在内存里等了一段时间。直到第 5 行 user 消息出现，这 5 行（加上会话头）才一起写进文件（§4.3）。

### 2.2 原始 JSONL

看几行原始内容。第 1、2、3、5 行：

```bash
sed -n '1p;2p;3p;5p' "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)"
```

```
{"type":"session","version":3,"id":"01a195ed-8022-70cb-9024-a443607c2453","timestamp":"2026-11-01T07:05:32.450Z","cwd":"/private/tmp/pi-lab/m4-work"}
{"type":"model_change","id":"2c14b2d0","parentId":null,"timestamp":"2026-11-01T07:05:32.483Z","provider":"faux","modelId":"faux-1"}
{"type":"thinking_level_change","id":"bf773565","parentId":"2c14b2d0","timestamp":"2026-11-01T07:05:32.483Z","thinkingLevel":"off"}
{"type":"message","id":"78be6f6a","parentId":"c28a59fb","timestamp":"2026-11-01T07:05:32.486Z","message":{"role":"user","content":[{"type":"text","text":"读 README.md，总结成三句话"}],"timestamp":1793516732485}}
```

第 4 行 system 消息很长，里面是完整的系统提示词。只看它的结构：条目的字段、消息的字段、每个 section 的字符数、声明的工具：

```bash
sed -n 4p "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)" | node -e 'const e=JSON.parse(require("fs").readFileSync(0,"utf8"));const m=e.message;console.log(Object.keys(e).join(","));console.log(Object.keys(m).join(","));for(const[k,v]of Object.entries(m.sections))console.log(k,v.length);console.log(m.toolsAdded.map(t=>t.name).join(","))'
```

```
type,id,parentId,timestamp,message
role,content,sections,timestamp,toolsAdded
preamble 126
tools 274
rules 589
docs 1179
cwd 40
read,bash,edit,write
```

可以看到：

- 条目有两个时间戳。条目的 `timestamp` 是 ISO 字符串，是写入条目的时间；消息里的 `timestamp` 是毫秒数，是消息本身的时间。
- 系统提示词和工具清单没有单独的条目，它们就是一条 `role: "system"` 的消息（M2 §6.2）。
- `message` 字段原样保存 Agent 消息。会话文件不做任何转换。

---

## 3. 数据结构

### 3.1 SessionHeader：会话头

源码：[`packages/coding-agent/src/core/session-manager.ts:43-50`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L43-L50)

```ts
export interface SessionHeader {
	type: "session";
	version?: number; // v1 sessions don't have this
	id: string;
	timestamp: string;
	cwd: string;
	parentSession?: string;
}
```

| 字段 | 说明 |
|---|---|
| `type` | 固定为 `"session"` |
| `version` | 文件格式版本，当前是 3（`CURRENT_SESSION_VERSION`，`packages/coding-agent/src/core/session-manager.ts:41`）。v1 文件没有这个字段 |
| `id` | 会话 id。默认用 `uuidv7()` 生成（`packages/coding-agent/src/core/session-manager.ts:264`），也可以用 `--session-id` 指定 |
| `timestamp` | 会话创建时间 |
| `cwd` | 创建会话时的工作目录。`pi -c` 用它判断会话属于哪个项目（§4.11） |
| `parentSession` | 可选。这个会话是从哪个会话文件分出来的（§4.13、§4.14） |

会话头和会话条目合起来叫 `FileEntry`，就是文件里任意一行的类型：

```ts
/** Raw file entry (includes header) */
export type FileEntry = SessionHeader | SessionEntry;
```

（`packages/coding-agent/src/core/session-manager.ts:196-197`）

**真实实例。** §2.2 输出的第一行：

```
{"type":"session","version":3,"id":"01a195ed-8022-70cb-9024-a443607c2453","timestamp":"2026-11-01T07:05:32.450Z","cwd":"/private/tmp/pi-lab/m4-work"}
```

### 3.2 SessionEntry：会话条目

所有会话条目都有同样的四个基础字段：

源码：[`packages/coding-agent/src/core/session-manager.ts:57-67`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L57-L67)

```ts
export interface SessionEntryBase {
	type: string;
	id: string;
	parentId: string | null;
	timestamp: string;
}

export interface SessionMessageEntry extends SessionEntryBase {
	type: "message";
	message: AgentMessage;
}
```

| 字段 | 说明 |
|---|---|
| `type` | 条目类型，见下表 |
| `id` | 8 位十六进制 id，在本会话内唯一（§4.1 `generateId`） |
| `parentId` | 父条目的 id。第一个条目为 `null` |
| `timestamp` | 写入时间，ISO 字符串 |

最常见的条目是 `SessionMessageEntry`，它的 `message` 字段保存一条 Agent 消息。全部条目类型是一个联合类型：

源码：[`packages/coding-agent/src/core/session-manager.ts:182-194`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L182-L194)

```ts
/** Session entry - has id/parentId for tree structure (returned by "read" methods in SessionManager) */
export type SessionEntry =
	| SessionMessageEntry
	| ThinkingLevelChangeEntry
	| ModelChangeEntry
	| UsageEntry
	| CompactionEntry
	| BranchSummaryEntry
	| CustomEntry
	| CustomMessageEntry
	| ContextEditEntry
	| LabelEntry
	| SessionInfoEntry;
```

这 11 种条目按"会不会变成发给模型的消息"分成三类。最后一列来自 §4.8 的 `sessionEntryToContextMessages`：

| 条目 `type` | 写入方法 | 典型调用者 | 投影成的 Agent 消息 |
|---|---|---|---|
| `message` | `appendMessage()` | M8，每个 `message_end` | 原样的 Agent 消息 |
| `custom_message` | `appendCustomMessageEntry()` | 扩展注入的消息（→ M7） | `role: "custom"` 的消息 |
| `compaction` | `appendCompaction()` | 压缩（→ M5） | 一条 system 消息 + 一条 `compactionSummary` |
| `branch_summary` | `branchWithSummary()` | `/tree` 切换分支并总结（→ M8） | 一条 `branchSummary` |
| `model_change` | `appendModelChange()` | 切换模型 | 无，但决定投影的 `model` |
| `thinking_level_change` | `appendThinkingLevelChange()` | 切换思考级别 | 无，但决定投影的 `thinkingLevel` |
| `context_edit` | `appendContextEdit()` | 重试前去掉失败的尝试（M8） | 无，但改写目标条目的投影（§3.3） |
| `usage` | `appendUsage()` | 缓存预热等不产生消息的模型调用 | 无，只计入用量统计 |
| `custom` | `appendCustomEntry()` | 扩展保存自己的状态 | 无 |
| `label` | `appendLabelChange()` | 用户给条目加书签 | 无 |
| `session_info` | `appendSessionInfo()` | `/name` 设置会话名 | 无 |

`custom` 和 `custom_message` 名字相近，作用不同。`custom` 只给扩展自己读，`custom_message` 会进入上下文。`session-manager.ts` 的注释专门说明了这一点（`packages/coding-agent/src/core/session-manager.ts:118-127`）。

每种条目的 JSON 示例见 `packages/coding-agent/docs/session-format.md` 的 "Entry Types" 一节。

**真实实例。** §2.2 输出的第 2–4 行分别是 `model_change`、`thinking_level_change` 和 user 消息的 `message` 条目。

### 3.3 ContextEditEntry 和 CompactionEntry：不改历史，只改投影

会话文件只追加，不修改。如果要改变模型看到的上下文，就追加一个新条目，让投影时跳过或替换旧条目。有两种条目做这件事。

`ContextEditEntry` 改一个条目：

源码：[`packages/coding-agent/src/core/session-manager.ts:167-180`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L167-L180)

```ts
/** Content that an append-only context edit may replace without changing message metadata. */
export type ContextEditableContent =
	| UserMessage["content"]
	| AssistantMessage["content"]
	| ToolResultMessage["content"]
	| CustomMessage["content"];

/** Append-only change to one earlier entry's contribution to model context. */
export interface ContextEditEntry extends SessionEntryBase {
	type: "context_edit";
	targetId: string;
	/** Null omits the target from model context. A value replaces only its content. */
	replacement: { content: ContextEditableContent } | null;
}
```

| 字段 | 说明 |
|---|---|
| `targetId` | 要改的条目。只能是 user、assistant、toolResult 消息条目或 `custom_message` 条目（§4.4） |
| `replacement` | `null` 表示从上下文里去掉目标；否则用 `content` 替换目标消息的内容，其余字段（role、时间戳、usage 等）不变 |

`CompactionEntry` 改一段条目，用一段摘要代替 `firstKeptEntryId` 之前的所有条目：

源码：[`packages/coding-agent/src/core/session-manager.ts:91-104`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L91-L104)

```ts
export interface CompactionEntry<T = unknown> extends SessionEntryBase {
	type: "compaction";
	summary: string;
	firstKeptEntryId: string;
	tokensBefore: number;
	/** Extension-specific data (e.g., ArtifactIndex, version markers for structured compaction) */
	details?: T;
	/** Usage from the LLM call(s) that generated this summary, if available */
	usage?: Usage;
	/** True if generated by an extension, undefined/false if pi-generated (backward compatible) */
	fromHook?: boolean;
	/** Complete prompt and tool state at this compaction boundary. */
	systemMessage?: SystemMessage;
}
```

| 字段 | 说明 |
|---|---|
| `summary` | 摘要文本 |
| `firstKeptEntryId` | 压缩之前的条目里，从这一条开始保留。等于自己的 id 时表示一条都不保留 |
| `tokensBefore` | 压缩前上下文的 token 数 |
| `systemMessage` | 压缩时刻完整的系统提示词和工具清单。压缩后的上下文用它做第一条 system 消息 |
| `details`、`usage`、`fromHook` | 实现细节、生成摘要的用量、是否由扩展生成（→ M5） |

`CompactionEntry` 怎么生成是 M5 的内容。本模块只读它在投影时怎么生效（§4.7）。

**真实实例。** [`lab/m4-session.ts`](lab/m4-session.ts) 直接调用 `SessionManager`，不经过 agent 循环。`project` 场景追加了一个替换工具结果的 `context_edit`，`compaction` 场景追加了一个 `compaction`。两个场景输出的第一行就是这两个条目的 JSON（完整输出在 §4.7 和 §4.8）：

```bash
node --conditions=source --experimental-strip-types m4-session.ts project | head -1
node --conditions=source --experimental-strip-types m4-session.ts compaction | head -1
```

```
context_edit 条目：{"type":"context_edit","id":"e08ceb35","parentId":"8f996146","timestamp":"2026-11-01T07:05:31.802Z","targetId":"28f0d197","replacement":{"content":[{"type":"text","text":"[工具输出已省略]"}]}}
compaction 条目：{"type":"compaction","id":"9ef4a778","parentId":"2b46a534","timestamp":"2026-11-01T07:05:31.924Z","summary":"用户让我总结 README，我已经总结过。","firstKeptEntryId":"d3b7c4e4","tokensBefore":12345,"systemMessage":{"role":"system","content":"You are pi.\n\n工具清单变了","timestamp":1793516731924}}
```

传入的替换内容是字符串 `"[工具输出已省略]"`，存下来的是一个 text 块数组。toolResult 消息的 `content` 必须是数组，`appendContextEdit` 在写入前做了转换（§4.4）。`compaction` 条目的 `systemMessage` 是把压缩前路径上的两条 system 消息合并后的结果。

### 3.4 SessionManager 的内存状态

源码：[`packages/coding-agent/src/core/session-manager.ts:987-998`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L987-L998)

```ts
export class SessionManager {
	private sessionId: string = "";
	private sessionFile: string | undefined;
	private sessionDir: string;
	private cwd: string;
	private persist: boolean;
	private flushed: boolean = false;
	private fileEntries: FileEntry[] = [];
	private byId: Map<string, SessionEntry> = new Map();
	private labelsById: Map<string, string> = new Map();
	private labelTimestampsById: Map<string, string> = new Map();
	private leafId: string | null = null;
```

| 字段 | 说明 |
|---|---|
| `sessionId`、`sessionFile`、`sessionDir`、`cwd` | 会话 id、文件路径、所在目录、工作目录 |
| `persist` | 是否写文件。`SessionManager.inMemory()`（`--no-session`）为 `false` |
| `flushed` | 会话文件是否已经创建。为 `false` 时追加的条目只留在内存里（§4.3） |
| `fileEntries` | 文件里的全部行，按顺序：会话头 + 所有分支的全部条目 |
| `byId` | id → 条目的索引，用于沿 `parentId` 往上走 |
| `labelsById`、`labelTimestampsById` | 每个条目当前的书签，从 `label` 条目重放得到 |
| `leafId` | 当前位置。下一个追加的条目以它为父条目；投影从它往根走 |

整个类就是围绕 `fileEntries` 和 `leafId` 两个字段工作：追加时两个都变，分支时只改 `leafId`，投影时只读。

**真实实例。** `persist` 场景逐条追加条目，每一步后打印文件是否存在、文件行数和内存里的条目数（`getEntries().length`，不含会话头）。完整输出见 §4.3。前 4 步的输出是：

```
create()                             文件存在=false 行数=0 内存条目=0
appendModelChange()                  文件存在=false 行数=0 内存条目=1
appendThinkingLevelChange()          文件存在=false 行数=0 内存条目=2
appendMessage(system)                文件存在=false 行数=0 内存条目=3
```

### 3.5 SessionProjection：投影的结果

源码：[`packages/coding-agent/src/core/session-manager.ts:209-227`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L209-L227)

```ts
export interface ProjectedSessionEntry {
	/** Raw append-only entry that owns this projected contribution. */
	sourceEntry: SessionEntry;
	/** Model-visible messages after context edits. Empty for state-only entries and omissions. */
	messages: AgentMessage[];
}

export interface SessionProjection {
	entries: ProjectedSessionEntry[];
	messages: AgentMessage[];
	thinkingLevel: string;
	model: { provider: string; modelId: string } | null;
}

export interface SessionContext {
	messages: AgentMessage[];
	thinkingLevel: string;
	model: { provider: string; modelId: string } | null;
}
```

| 类型 / 字段 | 说明 |
|---|---|
| `ProjectedSessionEntry.sourceEntry` | 被选中的原始条目 |
| `ProjectedSessionEntry.messages` | 这个条目贡献的 Agent 消息。大部分条目贡献 0 或 1 条，`compaction` 贡献 2 条 |
| `SessionProjection.entries` | 当前分支上被选中的条目，按顺序 |
| `SessionProjection.messages` | 所有 `entries[i].messages` 拼起来。M8 拿它替换 transcript |
| `thinkingLevel`、`model` | 沿当前分支重放 `thinking_level_change`、`model_change` 和 assistant 消息得到的设置 |
| `SessionContext` | `SessionProjection` 去掉 `entries`。`buildSessionContext()` 返回它 |

`entries` 保留了"每条消息来自哪个条目"。M8 靠它找到一条 Agent 消息对应的条目 id，再对这个 id 追加 `context_edit`（§6.5）。

**真实实例。** `project` 场景的投影。每行是一个被选中的条目，`⇒` 右边是它贡献的 Agent 消息：

```bash
node --conditions=source --experimental-strip-types m4-session.ts project
```

```
context_edit 条目：{"type":"context_edit","id":"e08ceb35","parentId":"8f996146","timestamp":"2026-11-01T07:05:31.802Z","targetId":"28f0d197","replacement":{"content":[{"type":"text","text":"[工具输出已省略]"}]}}
buildSessionProjection()：
  72a801fc model_change faux/faux-1  ⇒  （无）
  584a7921 thinking_level_change off  ⇒  （无）
  928cdf9d message/system "You are pi."  ⇒  system "You are pi."
  b2836863 message/user "读 README.md，总结成三句话"  ⇒  user "读 README.md，总结成三句话"
  3791d947 message/assistant "我先读一下 README。" <toolCall>  ⇒  assistant "我先读一下 README。" <toolCall>
  28f0d197 message/toolResult "# Demo ..."  ⇒  toolResult "[工具输出已省略]"
  46f64f28 message/assistant "这是一个演示项目。"  ⇒  assistant "这是一个演示项目。"
  2d3b199c custom my-ext {"count":1}  ⇒  （无）
  78f77873 custom_message my-ext "扩展注入的提示"  ⇒  （无）
  e2634d1a custom_message my-ext "第二条提示"  ⇒  custom(my-ext) "第二条提示"
  8f996146 label → b2836863 "起点"  ⇒  （无）
  e08ceb35 context_edit → 28f0d197 "[工具输出已省略]"  ⇒  （无）
  262080b0 context_edit → 78f77873 null  ⇒  （无）
  951d70d6 thinking_level_change high  ⇒  （无）
  messages: system, user, assistant, toolResult, assistant, custom
  model: {"provider":"faux","modelId":"faux-1"}  thinkingLevel: high
原始 toolResult 条目没有被改："# Demo ..."
```

逐行看：

- `model_change` 和两个 `thinking_level_change` 不贡献消息，但最后一个 `thinking_level_change high` 决定了 `thinkingLevel: high`。
- toolResult 条目的内容被 `context_edit` 替换成了 `"[工具输出已省略]"`，原始条目没变。
- 第一个 `custom_message` 被 `context_edit … null` 去掉了，第二个变成一条 `custom` 消息。
- `custom`、`label`、`context_edit` 自己不贡献消息。

### 3.6 类型关系图

```
 会话文件 *.jsonl（一行一个 FileEntry）
 ┌──────────────────────────────────────────────────────────────┐
 │ SessionHeader {type:"session", id, cwd, parentSession?}      │ 第 1 行
 │ SessionEntry  {type, id, parentId, timestamp, …}             │ 第 2 行起，所有分支混在一起
 └──────────────────────────────────────────────────────────────┘
        │ loadEntriesFromFile()（§4.10）          ▲ _persist()（§4.3）
        ▼                                         │
 SessionManager（内存）
 ├─ fileEntries: FileEntry[]  ─── byId: Map<id, SessionEntry>
 └─ leafId ──┐
             │ buildSessionPath()：从 leaf 沿 parentId 走到根（§4.6）
             ▼
     SessionEntry[]（一条分支）
             │ buildContextEntries()：有 compaction 时只留摘要之后的部分（§4.7）
             ▼
     SessionEntry[]（被选中的条目）
             │ projectContextEntry()：应用 context_edit（§4.8）
             │   └─ sessionEntryToContextMessages()：条目 → Agent 消息
             ▼
 SessionProjection { entries: ProjectedSessionEntry[], messages: AgentMessage[], model, thinkingLevel }
             │
             ▼  M8：prepareRequest 用 messages 替换 transcript；恢复会话时作为 Agent 的初始消息
     AgentMessage[] ──▶ M2 循环 ──▶ message_end ──▶ appendMessage() ──▶ 新的 SessionEntry
```

---

## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。

```
写入
AgentSession._handleAgentEvent()  message_end            [M8]
└─ SessionManager.appendMessage()                        §4.1
    ├─ generateId()                                      §4.1
    └─ _appendEntry()                                    §4.2
        └─ _persist()                                    §4.3
            └─ _hasConversation()                        §4.3
   其他 append*()：appendContextEdit、appendCompaction …  §4.4

读取
AgentSession prepareRequest hook                         [M8]
└─ SessionManager.buildSessionProjection()               §4.5
    └─ buildSessionProjection()                          §4.5
        ├─ buildSessionPath()                            §4.6
        ├─ getSessionContextSettings()                   §4.6
        ├─ buildContextEntries()                         §4.7
        │   └─ buildSessionPath()
        └─ projectContextEntry()                         §4.8
            └─ sessionEntryToContextMessages()           §4.8

打开
createSessionManager()                                   [M9]
├─ SessionManager.create() → constructor → newSession()  §4.9
├─ SessionManager.open() → _setSessionFile()             §4.10
│   ├─ loadEntriesFromFile()
│   └─ _loadEntries() → 迁移 → _buildIndex()
├─ SessionManager.continueRecent()  （pi -c）             §4.11
│   └─ findMostRecentSession()
└─ SessionManager.forkFrom()  （pi --fork）               §4.14
createAgentSession() → buildSessionContext()  恢复会话   §4.12

在树上移动
branch() / resetLeaf() / branchWithSummary()             §4.13
createBranchedSession()  （/fork、/clone）                §4.13
旁支：会话列表、迁移、findById                            §4.15
```

### 4.1 appendMessage() 和 generateId()

**调用位置**：M2 循环 emit `message_end` → `Agent.processEvents()` → `AgentSession._handleAgentEvent()` → `appendMessage()`。

源码：[`packages/coding-agent/src/core/agent-session.ts:1162-1183`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L1162-L1183)

```ts
		// Handle session persistence
		if (event.type === "message_end") {
			let entryId: string | undefined;
			// Check if this is a custom message from extensions
			if (event.message.role === "custom") {
				// Persist as CustomMessageEntry
				entryId = this.sessionManager.appendCustomMessageEntry(
					event.message.customType,
					event.message.content,
					event.message.display,
					event.message.details,
				);
			} else if (
				event.message.role === "system" ||
				event.message.role === "user" ||
				event.message.role === "assistant" ||
				event.message.role === "toolResult"
			) {
				// Regular LLM message - persist as SessionMessageEntry
				entryId = this.sessionManager.appendMessage(event.message);
			}
			if (entryId) this._entryIdsByMessage.set(event.message, entryId);
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1208-1224`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1208-L1224)

```ts
	/** Append a message as child of current leaf, then advance leaf. Returns entry id.
	 * Does not allow writing CompactionSummaryMessage and BranchSummaryMessage directly.
	 * Reason: we want these to be top-level entries in the session, not message session entries,
	 * so it is easier to find them.
	 * These need to be appended via appendCompaction() and appendBranchSummary() methods.
	 */
	appendMessage(message: Message | CustomMessage | BashExecutionMessage): string {
		const entry: SessionMessageEntry = {
			type: "message",
			id: generateId(this.byId),
			parentId: this.leafId,
			timestamp: new Date().toISOString(),
			message,
		};
		this._appendEntry(entry);
		return entry.id;
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:276-284`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L276-L284)

```ts
/** Generate a unique short ID (8 hex chars, collision-checked) */
function generateId(byId: { has(id: string): boolean }): string {
	for (let i = 0; i < 100; i++) {
		const id = randomUUID().slice(0, 8);
		if (!byId.has(id)) return id;
	}
	// Fallback to full UUID if somehow we have collisions
	return randomUUID();
}
```

**讲解**

M8 在每个 `message_end` 事件里按 role 分两路：`custom` 消息写成 `custom_message` 条目，四种标准 role 写成 `message` 条目。`bashExecution`、`compactionSummary`、`branchSummary` 三种自定义消息不走这里，它们由各自的功能直接写入（`compactionSummary` 和 `branchSummary` 实际上写成 `compaction`、`branch_summary` 条目）。返回的条目 id 记在 `_entryIdsByMessage` 里，后面要按消息找条目时用（§6.5）。

`appendMessage` 本身很短：

- 输入是一条 Agent 消息，输出是新条目的 id。
- 新条目的 `parentId` 是当前的 `leafId`。所以条目总是挂在当前位置下面。
- `id` 由 `generateId` 生成：取 `randomUUID()` 的前 8 个字符，和 `byId` 里已有的 id 比较，冲突就重试，100 次都冲突才用完整 UUID。8 位 id 让文件更短，也方便在 `/tree` 里显示；id 只需要在一个会话内唯一。

方法注释说明了为什么 `compactionSummary` 和 `branchSummary` 不能用 `appendMessage` 写：它们要作为顶层条目（`compaction`、`branch_summary`）存在，方便查找。参数类型 `Message | CustomMessage | BashExecutionMessage` 也排除了这两种消息。

**往哪走**：§4.2 `_appendEntry`。

### 4.2 _appendEntry()

**调用位置**：所有 `append*()` 方法和 `branchWithSummary()` 最后都调用它。

源码：[`packages/coding-agent/src/core/session-manager.ts:1191-1206`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1191-L1206)

```ts
	private _appendEntry(entry: SessionEntry): void {
		const previousLeafId = this.leafId;
		this.fileEntries.push(entry);
		this.byId.set(entry.id, entry);
		this.leafId = entry.id;
		try {
			this._persist(entry);
		} catch (error) {
			// Keep memory in line with the file: an entry that was not written must not become the
			// parent of later entries, or the file would hold entries pointing to a missing parent.
			this.fileEntries.pop();
			this.byId.delete(entry.id);
			this.leafId = previousLeafId;
			throw error;
		}
	}
```

**讲解**

`_appendEntry` 先改内存，再写文件：

1. 把条目放进 `fileEntries` 和 `byId`，`leafId` 指向它。
2. 调用 `_persist` 写文件。
3. 写文件抛异常时，撤销第 1 步，`leafId` 恢复成原来的值，然后把异常继续抛出。

第 3 步的理由写在注释里：没有写进文件的条目不能成为后续条目的父条目，否则文件里会出现 `parentId` 指向不存在的条目。§7.3 的实验会让写文件失败，验证这一点。

注意顺序是"先改内存再写文件"，不是反过来。因为 `_persist` 第一次创建文件时要把 `fileEntries` 整个写出去，其中要包括刚追加的这一条（§4.3）。

**往哪走**：§4.3 `_persist`。

### 4.3 _persist() 和 _hasConversation()

**调用位置**：`_appendEntry()` → `_persist()`。

源码：[`packages/coding-agent/src/core/session-manager.ts:1160-1189`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1160-L1189)

```ts
	/**
	 * A new session file is created only once the session contains a user or assistant message.
	 * Setup entries alone (model, thinking level, system prompt) stay in memory so opening and
	 * closing pi without chatting leaves no file behind. Starting at the user message (not the
	 * first assistant reply) keeps the prompt on disk if the first turn never completes (#10000).
	 */
	private _hasConversation(): boolean {
		return this.fileEntries.some(
			(e) => e.type === "message" && (e.message.role === "user" || e.message.role === "assistant"),
		);
	}

	_persist(entry: SessionEntry): void {
		if (!this.persist || !this.sessionFile) return;

		if (!this.flushed) {
			if (!this._hasConversation()) return;
			const fd = openSync(this.sessionFile, "wx");
			try {
				for (const e of this.fileEntries) {
					writeFileSync(fd, `${JSON.stringify(e)}\n`);
				}
			} finally {
				closeSync(fd);
			}
			this.flushed = true;
		} else {
			appendFileSync(this.sessionFile, `${JSON.stringify(entry)}\n`);
		}
	}
```

**讲解**

`_persist` 按 `flushed` 分两种情况：

- `flushed` 为 `false`，文件还没创建。如果内存里还没有 user 或 assistant 消息，直接返回，什么都不写。一旦有了，就用 `"wx"` 模式创建文件，把 `fileEntries` 全部写进去（会话头 + 之前积攒的条目 + 当前条目），然后把 `flushed` 设为 `true`。
- `flushed` 为 `true`，文件已经存在。只把当前条目追加一行。

`persist` 为 `false`（内存会话）或没有 `sessionFile` 时，一律不写。

`_hasConversation` 的注释说明了两个设计决定：

- 只有模型、思考级别、系统提示词这些设置条目时不建文件。打开 pi 不聊天就退出，不会留下空会话。
- 判断条件包括 user 消息，而不是等第一条 assistant 回复。这样即使第一轮请求失败或进程被杀，用户的输入也已经在磁盘上（注释里引用的是 issue #10000）。

`"wx"` 是"独占创建"：文件已存在时 `openSync` 抛 `EEXIST`。会话文件名包含毫秒时间戳和 UUID，正常情况不会冲突；万一冲突，异常会经过 §4.2 的回滚，而不是覆盖别人的文件。

**真实实例。** `persist` 场景按主线场景的顺序追加条目：

```bash
node --conditions=source --experimental-strip-types m4-session.ts persist
```

```
sessionFile: 2026-11-01T07-05-32-043Z_01a195ed-7e8b-768e-9a00-9cb3905dd210.jsonl
create()                             文件存在=false 行数=0 内存条目=0
appendModelChange()                  文件存在=false 行数=0 内存条目=1
appendThinkingLevelChange()          文件存在=false 行数=0 内存条目=2
appendMessage(system)                文件存在=false 行数=0 内存条目=3
appendMessage(user)                  文件存在=true 行数=5 内存条目=4
appendMessage(assistant)             文件存在=true 行数=6 内存条目=5
文件内容（每行截断到 110 个字符）：
  {"type":"session","version":3,"id":"01a195ed-7e8b-768e-9a00-9cb3905dd210","timestamp":"2026-11-01T07:05:32.043
  {"type":"model_change","id":"322f0c80","parentId":null,"timestamp":"2026-11-01T07:05:32.045Z","provider":"faux
  {"type":"thinking_level_change","id":"28602ad9","parentId":"322f0c80","timestamp":"2026-11-01T07:05:32.045Z","
  {"type":"message","id":"fa93d058","parentId":"28602ad9","timestamp":"2026-11-01T07:05:32.045Z","message":{"rol
  {"type":"message","id":"e1a6de20","parentId":"fa93d058","timestamp":"2026-11-01T07:05:32.045Z","message":{"rol
  {"type":"message","id":"b0dee05b","parentId":"e1a6de20","timestamp":"2026-11-01T07:05:32.045Z","message":{"rol
```

前三步之后文件都不存在。`appendMessage(user)` 这一步，文件一下子有了 5 行：会话头、`model_change`、`thinking_level_change`、system 消息、user 消息。之后每追加一条，文件多一行。主线场景（§2.1）的前 5 行就是这样一起写进去的。

**往哪走**：写入方向读完了。§4.4 看其他 `append*` 方法有什么不同，然后 §4.5 开始读取方向。

### 4.4 其他 append 方法

`appendThinkingLevelChange`、`appendModelChange`、`appendUsage`、`appendCustomEntry`、`appendSessionInfo`、`appendCustomMessageEntry`、`appendLabelChange` 和 `appendMessage` 的结构完全一样：构造条目，`parentId: this.leafId`，调用 `_appendEntry`。只有两个方法多做了事。

**appendCompaction()**（调用位置：M5 的压缩流程，`packages/coding-agent/src/core/agent-session.ts:2932`）

源码：[`packages/coding-agent/src/core/session-manager.ts:1270-1297`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1270-L1297)

```ts
	/** Append a compaction summary as child of current leaf, then advance leaf. Returns entry id. */
	appendCompaction<T = unknown>(
		summary: string,
		firstKeptEntryId: string | null,
		tokensBefore: number,
		details?: T,
		fromHook?: boolean,
		usage?: Usage,
	): string {
		const timestamp = new Date().toISOString();
		const systemMessage = getCurrentSystemMessage(this.buildSessionProjection().messages);
		const id = generateId(this.byId);
		const entry: CompactionEntry<T> = {
			type: "compaction",
			id,
			parentId: this.leafId,
			timestamp,
			summary,
			firstKeptEntryId: firstKeptEntryId ?? id,
			tokensBefore,
			details,
			usage,
			fromHook,
			...(systemMessage ? { systemMessage: { ...systemMessage, timestamp: new Date(timestamp).getTime() } } : {}),
		};
		this._appendEntry(entry);
		return entry.id;
	}
```

追加之前，它先对当前分支做一次投影，用 M1 的 `getCurrentSystemMessage`（`packages/ai/src/utils/transcript.ts:73`）把投影里所有 system 消息合并成一条完整的 system 消息，存进 `systemMessage`。压缩后的上下文会丢掉被总结掉的 system 消息，靠这一条恢复完整的系统提示词和工具清单（§4.7）。`firstKeptEntryId` 传 `null` 时取自己的 id，表示不保留压缩前的任何条目。

**appendContextEdit()**（调用位置：M8 的 `_omitRecoveryAttempt`，`packages/coding-agent/src/core/agent-session.ts:1283`。自动重试和上下文溢出恢复之前，M8 用 `replacement: null` 把失败的 assistant 消息和它的工具结果从上下文里去掉）

源码：[`packages/coding-agent/src/core/session-manager.ts:1369-1408`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1369-L1408)

```ts
	/** Append a branch-local edit to an earlier model-visible entry. */
	appendContextEdit(targetId: string, replacement: ContextEditEntry["replacement"]): string {
		if (
			replacement !== null &&
			(typeof replacement !== "object" ||
				!("content" in replacement) ||
				(typeof replacement.content !== "string" && !Array.isArray(replacement.content)))
		) {
			throw new Error("Context edit replacement must be null or contain string/array content");
		}
		const target = this.byId.get(targetId);
		if (!target) throw new Error(`Entry ${targetId} not found`);
		if (!this.getBranch().some((entry) => entry.id === targetId)) {
			throw new Error(`Entry ${targetId} is not on the active branch`);
		}
		const editable =
			target.type === "custom_message" ||
			(target.type === "message" &&
				(target.message.role === "user" ||
					target.message.role === "assistant" ||
					target.message.role === "toolResult"));
		if (!editable) throw new Error(`Entry ${targetId} does not contribute editable model content`);
		const targetRole = target.type === "message" ? target.message.role : "custom";
		const normalizedReplacement =
			replacement !== null &&
			(targetRole === "assistant" || targetRole === "toolResult") &&
			typeof replacement.content === "string"
				? { content: [{ type: "text" as const, text: replacement.content }] }
				: replacement;
		const entry: ContextEditEntry = {
			type: "context_edit",
			id: generateId(this.byId),
			parentId: this.leafId,
			timestamp: new Date().toISOString(),
			targetId,
			replacement: normalizedReplacement,
		};
		this._appendEntry(entry);
		return entry.id;
	}
```

写入前有四项检查：

1. `replacement` 要么是 `null`，要么是带字符串或数组 `content` 的对象。
2. 目标条目存在。
3. 目标条目在当前分支上。编辑只对当前分支有意义（§7.2）。
4. 目标条目会贡献可编辑的内容：user、assistant、toolResult 消息条目，或 `custom_message` 条目。system 消息不能编辑。

然后，如果目标是 assistant 或 toolResult，而替换内容是字符串，就把字符串包成一个 text 块。这两种消息的 `content` 必须是数组。§3.3 的实例里能看到这个转换。

**往哪走**：§4.5 `buildSessionProjection`。

### 4.5 buildSessionProjection() 和 buildSessionContext()

**调用位置**：每次请求模型前，M2 调用 `prepareRequest` hook，M8 安装的实现调用 `sessionManager.buildSessionProjection()`：

源码：[`packages/coding-agent/src/core/agent-session.ts:803-813`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L803-L813)

```ts
	private _installAgentRequestProjection(): void {
		const previousPrepareRequest = this.agent.prepareRequest;
		this.agent.prepareRequest = async (request, signal) => {
			const failed = this._failedResponse;
			this._failedResponse = undefined;
			const prepare = async () => {
				const projection = this.sessionManager.buildSessionProjection();
				const canonicalContext = {
					...request.context,
					messages: projection.messages,
					// Messages declare the provider-visible loadout; context.tools keeps executable implementations.
```

`SessionManager` 上的同名方法只是把自己的状态传给模块级函数：

源码：[`packages/coding-agent/src/core/session-manager.ts:1499-1510`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1499-L1510)

```ts
	/**
	 * Build the session context (what gets sent to the LLM).
	 * Uses tree traversal from current leaf.
	 */
	buildSessionProjection(): SessionProjection {
		return buildSessionProjection(this.getEntries(), this.leafId, this.byId);
	}

	buildSessionContext(): SessionContext {
		const { messages, thinkingLevel, model } = this.buildSessionProjection();
		return { messages, thinkingLevel, model };
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:542-573`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L542-L573)

```ts
/** Build provenance-preserving, compaction-aware model context. */
export function buildSessionProjection(
	entries: SessionEntry[],
	leafId?: string | null,
	byId?: Map<string, SessionEntry>,
): SessionProjection {
	const path = buildSessionPath(entries, leafId, byId);
	const { thinkingLevel, model } = getSessionContextSettings(path);
	const contextEntries = buildContextEntries(entries, leafId, byId);
	const edits = new Map<string, ContextEditEntry>();
	for (const entry of contextEntries) {
		if (entry.type === "context_edit") edits.set(entry.targetId, entry);
	}
	const projectedEntries = contextEntries.map(
		(sourceEntry, index): ProjectedSessionEntry => ({
			sourceEntry,
			// buildContextEntries() may retain an older compaction entry because its
			// raw ID lies inside the newest retained range. Only the newest compaction
			// at index zero contributes a checkpoint and summary.
			messages:
				sourceEntry.type === "compaction" && index > 0
					? []
					: projectContextEntry(sourceEntry, edits.get(sourceEntry.id)),
		}),
	);
	return {
		entries: projectedEntries,
		messages: projectedEntries.flatMap((entry) => entry.messages),
		thinkingLevel,
		model,
	};
}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:575-583`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L575-L583)

```ts
/** Build the finalized model context from the canonical session projection. */
export function buildSessionContext(
	entries: SessionEntry[],
	leafId?: string | null,
	byId?: Map<string, SessionEntry>,
): SessionContext {
	const { messages, thinkingLevel, model } = buildSessionProjection(entries, leafId, byId);
	return { messages, thinkingLevel, model };
}
```

**讲解**

`buildSessionProjection` 是三个模块级函数串起来：

1. `buildSessionPath`：从 `leafId` 沿 `parentId` 走到根，得到当前分支的完整路径（§4.6）。
2. `getSessionContextSettings`：在完整路径上重放设置，得到 `thinkingLevel` 和 `model`（§4.6）。
3. `buildContextEntries`：在路径上处理压缩，选出要进入上下文的条目（§4.7）。

然后它收集选中条目里的所有 `context_edit`，按 `targetId` 放进 Map。同一个目标被编辑多次时，后面的覆盖前面的，所以最新的编辑生效。最后对每个选中条目调用 `projectContextEntry`（§4.8），得到它贡献的消息。

注意设置是从完整路径算的，而上下文是从压缩后的条目算的。压缩会去掉旧消息，但不应该丢掉旧的 `model_change`。

中间那段注释说的是一个边界情况：路径上有多次压缩时，`buildContextEntries` 可能把一个较早的 `compaction` 条目当作"保留范围内的普通条目"留下来。只有排在第 0 位的最新压缩条目才贡献摘要，较早的压缩条目贡献空数组。

三个函数都接收 `byId` 参数。`SessionManager` 传入自己维护的索引，省得每次重建。

`buildSessionContext` 是 `buildSessionProjection` 去掉 `entries`。恢复会话时用的是它（§4.12）。

**往哪走**：§4.6 `buildSessionPath`。

### 4.6 buildSessionPath() 和 getSessionContextSettings()

**调用位置**：`buildSessionProjection()` 直接调用两者；`buildContextEntries()` 也调用 `buildSessionPath()`。

源码：[`packages/coding-agent/src/core/session-manager.ts:381-416`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L381-L416)

```ts
function buildEntryIndex(entries: SessionEntry[], byId?: Map<string, SessionEntry>): Map<string, SessionEntry> {
	if (byId) return byId;
	const index = new Map<string, SessionEntry>();
	for (const entry of entries) {
		index.set(entry.id, entry);
	}
	return index;
}

function buildSessionPath(
	entries: SessionEntry[],
	leafId?: string | null,
	byId?: Map<string, SessionEntry>,
): SessionEntry[] {
	const index = buildEntryIndex(entries, byId);
	let leaf: SessionEntry | undefined;
	if (leafId === null) {
		return [];
	}
	if (leafId) {
		leaf = index.get(leafId);
	}
	leaf ??= entries[entries.length - 1];
	if (!leaf) {
		return [];
	}

	const path: SessionEntry[] = [];
	let current: SessionEntry | undefined = leaf;
	while (current) {
		path.push(current);
		current = current.parentId ? index.get(current.parentId) : undefined;
	}
	path.reverse();
	return path;
}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:418-433`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L418-L433)

```ts
function getSessionContextSettings(path: SessionEntry[]): Pick<SessionContext, "thinkingLevel" | "model"> {
	let thinkingLevel = "off";
	let model: { provider: string; modelId: string } | null = null;

	for (const entry of path) {
		if (entry.type === "thinking_level_change") {
			thinkingLevel = entry.thinkingLevel;
		} else if (entry.type === "model_change") {
			model = { provider: entry.provider, modelId: entry.modelId };
		} else if (entry.type === "message" && entry.message.role === "assistant") {
			model = { provider: entry.message.provider, modelId: entry.message.model };
		}
	}

	return { thinkingLevel, model };
}
```

**讲解**

`buildSessionPath` 的 `leafId` 参数有三种取值：

- `null`：返回空路径。`resetLeaf()` 之后就是这种情况（§4.13）。
- 一个 id：从这个条目开始往根走。
- `undefined`：从 `entries` 的最后一个条目开始。只给条目数组、不给 leaf 的调用者用这个默认值。

从叶子往根走，每一步用 `byId` 查父条目，走到 `parentId` 为 `null`（或父条目不存在）为止，最后 `reverse()` 成从根到叶子的顺序。所有分支的条目都在 `entries` 里，但只有当前分支上的会被走到。

`getSessionContextSettings` 在路径上从前往后重放：

- `thinkingLevel` 默认 `"off"`，遇到 `thinking_level_change` 就更新。
- `model` 遇到 `model_change` 就更新，遇到 assistant 消息也更新成这条消息实际使用的模型。所以最后一个回答问题的模型就是投影的 `model`。

`SessionManager.getBranch()`（`packages/coding-agent/src/core/session-manager.ts:1479`）做同样的路径遍历，不需要传 `entries`。M8 在很多地方用它读当前分支。

**往哪走**：§4.7 `buildContextEntries`。

### 4.7 buildContextEntries()

**调用位置**：`buildSessionProjection()` → `buildContextEntries()`。M5 压缩完成后，下一次投影也走到这里。

源码：[`packages/coding-agent/src/core/session-manager.ts:468-512`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L468-L512)

```ts
/**
 * Build the active, compaction-aware session entry list.
 *
 * This follows the current leaf path. If the path contains compaction entries,
 * the latest compaction is represented by the compaction entry itself, followed
 * by the kept entries starting at firstKeptEntryId and all entries after the
 * compaction entry. Older summarized entries are omitted.
 */
export function buildContextEntries(
	entries: SessionEntry[],
	leafId?: string | null,
	byId?: Map<string, SessionEntry>,
): SessionEntry[] {
	const path = buildSessionPath(entries, leafId, byId);
	let compaction: CompactionEntry | null = null;

	for (const entry of path) {
		if (entry.type === "compaction") {
			compaction = entry;
		}
	}

	if (!compaction) {
		return path;
	}

	const compactionIdx = path.findIndex((entry) => entry.id === compaction.id);
	if (compactionIdx < 0) {
		return path;
	}

	const contextEntries: SessionEntry[] = [compaction];
	let foundFirstKept = false;
	for (let i = 0; i < compactionIdx; i++) {
		const entry = path[i];
		if (entry.id === compaction.firstKeptEntryId) {
			foundFirstKept = true;
		}
		if (foundFirstKept && !(entry.type === "message" && entry.message.role === "system")) {
			contextEntries.push(entry);
		}
	}
	contextEntries.push(...path.slice(compactionIdx + 1));
	return contextEntries;
}
```

**讲解**

没有压缩条目时，直接返回整条路径。

有压缩条目时，取路径上最后一个（最新的）压缩条目，返回的列表由三段组成：

1. 压缩条目自己，排在第 0 位。§4.5 只让第 0 位的压缩条目贡献摘要。
2. 压缩条目之前、从 `firstKeptEntryId` 开始的条目，但跳过其中的 system 消息。
3. 压缩条目之后的所有条目。

第 2 段跳过 system 消息，是因为压缩条目的 `systemMessage` 已经是那一刻完整的系统提示词（§4.4）。保留范围里的 system 消息只是增量，再放进去就重复了。压缩条目之后的 system 消息不跳过，它们是压缩之后的新增量。

`firstKeptEntryId` 在路径上找不到时（例如等于压缩条目自己的 id），`foundFirstKept` 一直是 `false`，第 2 段为空，也就是一条都不保留。

函数注释和 `session-format.md` 都强调，被选中的非消息条目（`label`、`custom` 等）也保留在列表里。它们不贡献消息，但交互模式要用这个列表渲染界面。

**真实实例。** `compaction` 场景先写 7 条消息和一条 system 消息，再追加压缩条目，`firstKeptEntryId` 指向第二个 user 消息：

```bash
node --conditions=source --experimental-strip-types m4-session.ts compaction
```

```
compaction 条目：{"type":"compaction","id":"9ef4a778","parentId":"2b46a534","timestamp":"2026-11-01T07:05:31.924Z","summary":"用户让我总结 README，我已经总结过。","firstKeptEntryId":"d3b7c4e4","tokensBefore":12345,"systemMessage":{"role":"system","content":"You are pi.\n\n工具清单变了","timestamp":1793516731924}}
getBranch()（整条路径）：
  307b59f5 message/system "You are pi."
  5f36c3cd message/user "读 README.md，总结成三句话"
  a3848d6e message/assistant "我先读一下 README。" <toolCall>
  9511729b message/toolResult "# Demo ..."
  1f3ac17d message/assistant "这是一个演示项目。"
  d3b7c4e4 message/user "再看看 package.json"
  73783c36 message/assistant "好的。"
  2b46a534 message/system "工具清单变了"
  9ef4a778 compaction firstKept=d3b7c4e4 "用户让我总结 README，我已经总结过。"
  007818f6 message/user "继续"
buildContextEntries()（压缩之后选中的条目）：
  9ef4a778 compaction firstKept=d3b7c4e4 "用户让我总结 README，我已经总结过。"
  d3b7c4e4 message/user "再看看 package.json"
  73783c36 message/assistant "好的。"
  007818f6 message/user "继续"
buildSessionProjection()：
  9ef4a778 compaction firstKept=d3b7c4e4 "用户让我总结 README，我已经总结过。"  ⇒  system "You are pi.\n\n工具清单变了" | compactionSummary "用户让我总结 README，我已经总结过。"
  d3b7c4e4 message/user "再看看 package.json"  ⇒  user "再看看 package.json"
  73783c36 message/assistant "好的。"  ⇒  assistant "好的。"
  007818f6 message/user "继续"  ⇒  user "继续"
  messages: system, compactionSummary, user, assistant, user
  model: {"provider":"faux","modelId":"faux-1"}  thinkingLevel: off
```

`getBranch()` 有 10 个条目，`buildContextEntries()` 只选出 4 个：压缩条目、保留范围里的 user 和 assistant、压缩之后的 user。保留范围里的 system 消息 "工具清单变了" 被跳过了。投影时，压缩条目贡献两条消息：合并后的 system 消息和 `compactionSummary`。所以最后发给 M8 的是 5 条消息。

**往哪走**：§4.8 `projectContextEntry`。

### 4.8 projectContextEntry() 和 sessionEntryToContextMessages()

**调用位置**：`buildSessionProjection()` 对每个选中的条目调用 `projectContextEntry(entry, edit)`。

源码：[`packages/coding-agent/src/core/session-manager.ts:514-540`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L514-L540)

```ts
/**
 * Build the session context from entries using tree traversal.
 * If leafId is provided, walks from that entry to root.
 * Handles compaction and branch summaries along the path.
 */
function projectContextEntry(entry: SessionEntry, edit: ContextEditEntry | undefined): AgentMessage[] {
	const messages = sessionEntryToContextMessages(entry);
	if (!edit) return messages;
	const replacement = edit.replacement;
	if (replacement === null) return [];

	return messages.map((message) => {
		if (
			message.role !== "user" &&
			message.role !== "assistant" &&
			message.role !== "toolResult" &&
			message.role !== "custom"
		) {
			return message;
		}
		const content =
			(message.role === "assistant" || message.role === "toolResult") && typeof replacement.content === "string"
				? [{ type: "text" as const, text: replacement.content }]
				: replacement.content;
		return { ...message, content } as AgentMessage;
	});
}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:435-466`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L435-L466)

```ts
/**
 * Project one selected session entry into LLM/runtime messages.
 * Plain custom entries are display/state entries and do not participate in context.
 */
export function sessionEntryToContextMessages(entry: SessionEntry): AgentMessage[] {
	if (entry.type === "message") {
		const message = entry.message;
		// Session files are parsed without validation; old versions, forks, or
		// hand-edited files can contain messages with null/missing content.
		if (message.role === "system" && message.content == null) return [{ ...message, content: "" }];
		if (
			(message.role === "user" || message.role === "assistant" || message.role === "toolResult") &&
			message.content == null
		) {
			return [{ ...message, content: [] }];
		}
		return [message];
	}
	if (entry.type === "custom_message") {
		return [
			createCustomMessage(entry.customType, entry.content ?? [], entry.display, entry.details, entry.timestamp),
		];
	}
	if (entry.type === "branch_summary" && entry.summary) {
		return [createBranchSummaryMessage(entry.summary, entry.fromId, entry.timestamp)];
	}
	if (entry.type === "compaction") {
		const summary = createCompactionSummaryMessage(entry.summary, entry.tokensBefore, entry.timestamp);
		return entry.systemMessage ? [entry.systemMessage, summary] : [summary];
	}
	return [];
}
```

**讲解**

`sessionEntryToContextMessages` 是条目到 Agent 消息的映射表，也就是 §3.2 表格最后一列：

- `message`：原样返回里面的 Agent 消息。会话文件读取时不做校验，旧版本或手改过的文件里 `content` 可能是 `null`，这里补成 `""` 或 `[]`，避免后面的代码出错。
- `custom_message`：用 `createCustomMessage`（`packages/coding-agent/src/core/messages.ts:123`）构造 `role: "custom"` 的消息。条目的 ISO 时间戳转成毫秒数。
- `branch_summary`：摘要非空时构造 `branchSummary` 消息（`packages/coding-agent/src/core/messages.ts:100`）。
- `compaction`：构造 `compactionSummary` 消息（`packages/coding-agent/src/core/messages.ts:109`）。有 `systemMessage` 时放在摘要前面。
- 其余条目：空数组。

`custom`、`branchSummary`、`compactionSummary` 这三种 Agent 消息模型不认识。M8 的 `convertToLlm` 会在请求前把它们转成 user 消息（→ M8）。

`projectContextEntry` 在这个映射之上应用编辑：

- 没有编辑：原样返回。
- `replacement` 为 `null`：返回空数组，这个条目不贡献消息。
- 否则只替换四种 role 的 `content`，其余字段用 `...message` 保留。assistant 和 toolResult 收到字符串时包成 text 块，和 §4.4 写入时的转换一致，这样即使文件里存的是字符串也能正确投影。

返回的是新对象，原始条目里的消息没有被修改。§3.5 的输出最后一行 "原始 toolResult 条目没有被改" 验证了这一点。

`projectContextEntry` 上面的注释（"Build the session context from entries using tree traversal…"）和函数本身不符，看起来是从 `buildSessionContext` 那里留下的旧注释。

**往哪走**：读取方向读完了。§4.9 开始读会话文件是怎么打开的。

### 4.9 SessionManager.create()、构造函数和 newSession()

**调用位置**：`main()` → `createSessionManager()`（`packages/coding-agent/src/main.ts:358`，→ M9）。主线场景没有 `-c`、`--session` 等参数，走到最后的 `SessionManager.create(cwd, sessionDir)`。SDK 用户不传 `sessionManager` 时，`createAgentSession` 也会调用它（`packages/coding-agent/src/core/sdk.ts:201`）。

源码：[`packages/coding-agent/src/core/session-manager.ts:1760-1768`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1760-L1768)

```ts
	/**
	 * Create a new session.
	 * @param cwd Working directory (stored in session header)
	 * @param sessionDir Optional session directory. If omitted, uses default (~/.pi/agent/sessions/<encoded-cwd>/).
	 */
	static create(cwd: string, sessionDir?: string, options?: NewSessionOptions): SessionManager {
		const dir = sessionDir ? normalizePath(sessionDir) : getDefaultSessionDir(cwd);
		return new SessionManager(cwd, dir, undefined, true, options);
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1000-1022`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1000-L1022)

```ts
	private constructor(
		cwd: string,
		sessionDir: string,
		sessionFile: string | undefined,
		persist: boolean,
		newSessionOptions?: NewSessionOptions,
		preloadedFileEntries?: FileEntry[],
	) {
		this.cwd = resolvePath(cwd);
		this.sessionDir = normalizePath(sessionDir);
		this.persist = persist;
		if (persist && this.sessionDir && !existsSync(this.sessionDir)) {
			mkdirSync(this.sessionDir, { recursive: true });
		}

		if (sessionFile) {
			this._setSessionFile(sessionFile, preloadedFileEntries);
		} else if (preloadedFileEntries?.length) {
			this._loadEntries(preloadedFileEntries, newSessionOptions);
		} else {
			this.newSession(newSessionOptions);
		}
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1057-1083`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1057-L1083)

```ts
	newSession(options?: NewSessionOptions): string | undefined {
		if (options?.id !== undefined) {
			assertValidSessionId(options.id);
		}
		this.sessionId = options?.id ?? createSessionId();
		const timestamp = new Date().toISOString();
		const header: SessionHeader = {
			type: "session",
			version: CURRENT_SESSION_VERSION,
			id: this.sessionId,
			timestamp,
			cwd: this.cwd,
			parentSession: options?.parentSession,
		};
		this.fileEntries = [header];
		this.byId.clear();
		this.labelsById.clear();
		this.labelTimestampsById.clear();
		this.leafId = null;
		this.flushed = false;

		if (this.persist) {
			const fileTimestamp = timestamp.replace(/[:.]/g, "-");
			this.sessionFile = join(this.getSessionDir(), `${fileTimestamp}_${this.sessionId}.jsonl`);
		}
		return this.sessionFile;
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:585-594`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L585-L594)

```ts
/**
 * Compute the default session directory for a cwd.
 * Encodes cwd into a safe directory name under ~/.pi/agent/sessions/.
 */
function getDefaultSessionDirPath(cwd: string, agentDir: string = getDefaultAgentDir()): string {
	const resolvedCwd = resolvePath(cwd);
	const resolvedAgentDir = resolvePath(agentDir);
	const safePath = `--${resolvedCwd.replace(/^[/\\]/, "").replace(/[/\\:]/g, "-")}--`;
	return join(resolvedAgentDir, "sessions", safePath);
}
```

**讲解**

所有静态工厂方法最后都调用私有构造函数，靠参数区分三种情况：

| 构造参数 | 走哪条分支 | 谁在用 |
|---|---|---|
| 有 `sessionFile` | `_setSessionFile()`，读已有文件（§4.10） | `open`、`continueRecent`、`forkFrom` |
| 没有文件，有 `preloadedFileEntries` | `_loadEntries()`，用给定的条目 | `inMemory(cwd, options, entries)` |
| 都没有 | `newSession()`，新会话 | `create`、`inMemory()` |

`newSession` 只在内存里准备会话头，把 `fileEntries` 设成只有会话头，清空索引，`leafId = null`，`flushed = false`。持久化模式下它只算出文件名（时间戳里的 `:` 和 `.` 换成 `-`），不创建文件。文件要等 §4.3 的 `_persist`。

`newSession` 是公开方法，`/new` 和 fork 会对已有的 `SessionManager` 调用它，切换到一个新会话（`packages/coding-agent/src/core/agent-session-runtime.ts:298`，→ M8）。

`getDefaultSessionDirPath` 把 cwd 编码成目录名：去掉开头的 `/`，把 `/`、`\`、`:` 换成 `-`，两头加 `--`。§2.1 的 `--private-tmp-pi-lab-m4-work--` 就是这样来的。`getDefaultSessionDir`（596 行）在这个基础上确保目录存在。

**往哪走**：§4.10 `open`。

### 4.10 SessionManager.open()、_setSessionFile()、loadEntriesFromFile()、_loadEntries()

**调用位置**：`pi --session <路径或 id>`、`pi -r` 选中一个会话之后，`createSessionManager()` 调用 `SessionManager.open()`（`packages/coding-agent/src/main.ts:340`、`packages/coding-agent/src/main.ts:426`）。

源码：[`packages/coding-agent/src/core/session-manager.ts:1776-1796`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1776-L1796)

```ts
	static open(path: string, sessionDir?: string, cwdOverride?: string): SessionManager {
		const resolvedPath = resolvePath(path);
		let header: SessionHeader | null = null;
		let preloadedFileEntries: FileEntry[] | undefined;
		if (cwdOverride === undefined && existsSync(resolvedPath)) {
			try {
				header = readSessionHeader(resolvedPath);
			} catch (error) {
				if (!(error instanceof SessionHeaderScanLimitError)) throw error;
				// The bounded scan is only a discovery optimization. A full load remains
				// authoritative for legacy files with very large headers or prefixes.
				preloadedFileEntries = loadEntriesFromFile(resolvedPath);
				const firstEntry = preloadedFileEntries[0];
				header = firstEntry?.type === "session" ? firstEntry : null;
			}
		}
		const cwd = cwdOverride ?? (header ? getSessionHeaderCwd(header) : undefined) ?? process.cwd();
		// If no sessionDir provided, derive from file's parent directory
		const dir = sessionDir ? normalizePath(sessionDir) : resolve(resolvedPath, "..");
		return new SessionManager(cwd, dir, resolvedPath, true, undefined, preloadedFileEntries);
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1029-1055`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1029-L1055)

```ts
	private _setSessionFile(sessionFile: string, preloadedFileEntries?: FileEntry[]): void {
		this.sessionFile = resolvePath(sessionFile);
		if (existsSync(this.sessionFile)) {
			const entries = preloadedFileEntries ?? loadEntriesFromFile(this.sessionFile);

			// If file was empty, initialize it with a valid session header. If it was
			// non-empty but did not parse as a pi session, fail without modifying it.
			if (entries.length === 0) {
				const explicitPath = this.sessionFile;
				if (statSync(explicitPath).size > 0) {
					throw new Error(`Session file is not a valid ${APP_NAME} session: ${explicitPath}`);
				}
				this.newSession();
				this.sessionFile = explicitPath;
				this._rewriteFile();
				this.flushed = true;
				return;
			}

			this._loadEntries(entries);
			this.flushed = true;
		} else {
			const explicitPath = this.sessionFile;
			this.newSession();
			this.sessionFile = explicitPath; // preserve explicit path from --session flag
		}
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:627-670`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L627-L670)

```ts
export function loadEntriesFromFile(filePath: string): FileEntry[] {
	const resolvedFilePath = normalizePath(filePath);
	if (!existsSync(resolvedFilePath)) return [];

	const entries: FileEntry[] = [];
	let pending = "";
	const fd = openSync(resolvedFilePath, "r");
	try {
		const decoder = new StringDecoder("utf8");
		const buffer = Buffer.allocUnsafe(SESSION_READ_BUFFER_SIZE);

		while (true) {
			const bytesRead = readSync(fd, buffer, 0, buffer.length, null);
			if (bytesRead === 0) break;

			pending += decoder.write(buffer.subarray(0, bytesRead));
			let lineStart = 0;
			let newlineIndex = pending.indexOf("\n", lineStart);
			while (newlineIndex !== -1) {
				const entry = parseSessionEntryLine(pending.slice(lineStart, newlineIndex));
				if (entry) entries.push(entry);
				lineStart = newlineIndex + 1;
				newlineIndex = pending.indexOf("\n", lineStart);
			}
			pending = pending.slice(lineStart);
		}

		pending += decoder.end();
		const finalEntry = parseSessionEntryLine(pending);
		if (finalEntry) entries.push(finalEntry);
	} finally {
		closeSync(fd);
	}

	// Validate session header before repairing the file.
	if (entries.length === 0) return entries;
	const header = entries[0];
	if (header.type !== "session" || typeof (header as { id?: unknown }).id !== "string") {
		return [];
	}

	if (pending) appendFileSync(resolvedFilePath, "\n");
	return entries;
}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1085-1122`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1085-L1122)

```ts
	private _loadEntries(entries: FileEntry[], options?: NewSessionOptions): void {
		const header = entries.find((e) => e.type === "session") as SessionHeader | undefined;

		if (header) {
			this.fileEntries = entries;
			this.sessionId = header.id;

			if (migrateToCurrentVersion(this.fileEntries)) {
				this._rewriteFile();
			}
		} else {
			this.newSession(options);
			this.fileEntries = this.fileEntries.concat(entries);
		}

		this._buildIndex();
	}

	private _buildIndex(): void {
		this.byId.clear();
		this.labelsById.clear();
		this.labelTimestampsById.clear();
		this.leafId = null;
		for (const entry of this.fileEntries) {
			if (entry.type === "session") continue;
			this.byId.set(entry.id, entry);
			this.leafId = entry.id;
			if (entry.type === "label") {
				if (entry.label) {
					this.labelsById.set(entry.targetId, entry.label);
					this.labelTimestampsById.set(entry.targetId, entry.timestamp);
				} else {
					this.labelsById.delete(entry.targetId);
					this.labelTimestampsById.delete(entry.targetId);
				}
			}
		}
	}
```

**讲解**

`open` 先只读会话头（`readSessionHeader`，最多扫描 1MB），从中取出 `cwd`。会话属于哪个项目，以会话头里记录的为准，不以当前目录为准。会话目录默认是文件所在的目录，`/new` 时新文件放在同一目录。

`_setSessionFile` 处理三种文件状态：

- 文件存在且有内容：`loadEntriesFromFile` 读入，`_loadEntries` 建索引，`flushed = true`。之后追加的条目直接追加到这个文件末尾。
- 文件存在但为空：写一个新的会话头。文件不为空却解析不出会话头时抛错，不修改文件。
- 文件不存在：新建会话，但保留调用者给的路径。`--session` 指定一个新路径时就是这种情况。

`loadEntriesFromFile` 每次读 1MB，按换行切分，逐行 `JSON.parse`。解析失败的行直接跳过，所以文件末尾一行写了一半（进程在写的过程中被杀）不会让整个会话打不开。第一行不是会话头时返回空数组。最后一行没有换行符时，它补一个换行，让下一次追加从新的一行开始。

`_loadEntries` 做两件事：

- 文件版本低于 3 时，调用 `migrateToCurrentVersion`（`packages/coding-agent/src/core/session-manager.ts:337`）就地迁移，再用 `_rewriteFile` 整个重写文件。v1 → v2 给线性的条目补上 `id`/`parentId`，v2 → v3 把 `hookMessage` 改名为 `custom`。这是会话文件唯一会被重写的情况。
- `_buildIndex` 按文件顺序遍历所有条目，建 `byId`，重放 `label` 条目得到书签。`leafId` 被设成文件里的最后一个条目。

最后这一点很重要：**打开文件后，当前位置是文件最后一行，不是某个分支的末端**。如果用户在 `/tree` 里切回一个旧分支，但没有追加任何条目就退出，下次打开时 leaf 仍是文件最后一行，回到了切换之前的分支。只要在旧分支上追加过一条，这一条就是文件最后一行，下次打开就在这个分支上。§4.13 的 `branch` 场景验证了这一点。

**往哪走**：§4.11 `continueRecent`。

### 4.11 SessionManager.continueRecent() 和 findMostRecentSession()：pi -c

**调用位置**：`pi -c`（`--continue`）→ `createSessionManager()`：

源码：[`packages/coding-agent/src/main.ts:432-434`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/main.ts#L432-L434)

```ts
	if (parsed.continue) {
		return SessionManager.continueRecent(cwd, sessionDir);
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1798-1811`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1798-L1811)

```ts
	/**
	 * Continue the most recent session, or create new if none.
	 * @param cwd Working directory
	 * @param sessionDir Optional session directory. If omitted, uses default (~/.pi/agent/sessions/<encoded-cwd>/).
	 */
	static continueRecent(cwd: string, sessionDir?: string): SessionManager {
		const dir = sessionDir ? normalizePath(sessionDir) : getDefaultSessionDir(cwd);
		const filterCwd = sessionDir !== undefined && !pathsEqual(dir, getDefaultSessionDirPath(cwd));
		const mostRecent = findMostRecentSession(dir, filterCwd ? cwd : undefined);
		if (mostRecent) {
			return new SessionManager(cwd, dir, mostRecent, true);
		}
		return new SessionManager(cwd, dir, undefined, true);
	}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:748-768`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L748-L768)

```ts
/** Exported for testing */
export function findMostRecentSession(sessionDir: string, cwd?: string): string | null {
	const resolvedSessionDir = normalizePath(sessionDir);
	const resolvedCwd = cwd ? resolvePath(cwd) : undefined;
	try {
		const files = readdirSync(resolvedSessionDir)
			.filter((file) => file.endsWith(".jsonl"))
			.map((file) => join(resolvedSessionDir, file))
			.map((path) => ({ path, mtime: statSync(path).mtimeMs }))
			.sort((a, b) => b.mtime - a.mtime);

		for (const { path } of files) {
			const header = readSessionHeaderForDiscovery(path);
			if (header && (!resolvedCwd || sessionCwdMatches(getSessionHeaderCwd(header), resolvedCwd))) return path;
		}
		return null;
	} catch {
		// Directory access and stat races make recent-session discovery unavailable.
		return null;
	}
}
```

**讲解**

`continueRecent` 在会话目录里找最近修改的会话文件：

- `findMostRecentSession` 列出目录里所有 `.jsonl` 文件，按修改时间（mtime）从新到旧排序，返回第一个会话头有效的文件。
- 找到就用这个文件构造 `SessionManager`，等同于 `open`。
- 找不到就新建会话。所以在一个新项目里运行 `pi -c` 不会报错，只是开始一个新会话。

按 mtime 而不是文件名排序，"最近"指的是最近写过的会话，不是最近创建的会话。每次追加条目都会更新 mtime。

`filterCwd` 处理会话目录是自定义的情况（`--session-dir` 或设置项）。默认目录本身就是按 cwd 分开的（§4.9），不需要过滤；自定义目录可能混着多个项目的会话，这时只选会话头 `cwd` 等于当前目录的文件。

`continueRecent` 只负责找到文件。继续之后模型看到什么，取决于 M8 怎么用这个 `SessionManager`，见 §4.12。

**往哪走**：§4.12 恢复会话。

### 4.12 恢复会话：createAgentSession() 读取投影

**调用位置**：`createAgentSession()`（→ M8），在 `SessionManager` 准备好之后。

源码：[`packages/coding-agent/src/core/sdk.ts:209-212`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L209-L212)

```ts
	// Check if session has existing data to restore
	const existingSession = sessionManager.buildSessionContext();
	const hasExistingSession = existingSession.messages.length > 0;
	const hasThinkingEntry = sessionManager.getBranch().some((entry) => entry.type === "thinking_level_change");
```

源码：[`packages/coding-agent/src/core/sdk.ts:403-410`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L403-L410)

```ts
	const agent = new Agent({
		initialState: {
			systemPrompt: "",
			model,
			thinkingLevel,
			tools: [],
			messages: existingSession.messages,
		},
```

源码：[`packages/coding-agent/src/core/sdk.ts:442-453`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/sdk.ts#L442-L453)

```ts
	// Restore missing settings metadata for older sessions.
	if (hasExistingSession) {
		if (!hasThinkingEntry) {
			sessionManager.appendThinkingLevelChange(thinkingLevel);
		}
	} else {
		// Save initial model and thinking level for new sessions so they can be restored on resume
		if (model) {
			sessionManager.appendModelChange(model.provider, model.id);
		}
		sessionManager.appendThinkingLevelChange(thinkingLevel);
	}
```

**讲解**

恢复会话不需要单独的代码路径，用的就是 §4.5 的投影：

1. `buildSessionContext()` 算出当前分支的 Agent 消息。消息不为空，就认为是在继续一个已有会话。
2. 这些消息成为 `Agent` 的初始 transcript（`messages: existingSession.messages`）。
3. 已有会话不再写 `model_change`；只有旧会话缺少 `thinking_level_change` 时补一条。新会话写入初始模型和思考级别，也就是 §2.1 的第 2、3 行。

`sdk.ts` 还用 `getBranch()` 上的 `model_change` 恢复模型（`packages/coding-agent/src/core/sdk.ts:219`），这是 M8 的内容。

之后每次请求前，`prepareRequest` 还会再投影一次（§4.5），所以初始 transcript 只在第一次请求前短暂使用。新会话和继续的会话在这之后没有任何区别。§7.1 的实验用 `pi -c` 验证这一点。

**往哪走**：§4.13 分支。

### 4.13 branch()、resetLeaf()、branchWithSummary()、createBranchedSession()

**调用位置**：交互模式的 `/tree` 选中一个条目 → `AgentSession.navigateTree()`（`packages/coding-agent/src/core/agent-session.ts:4045`，→ M8）：

源码：[`packages/coding-agent/src/core/agent-session.ts:4189-4210`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L4189-L4210)

```ts
			if (summaryText) {
				// Create summary at target position (can be null for root)
				const summaryId = this.sessionManager.branchWithSummary(
					newLeafId,
					summaryText,
					summaryDetails,
					fromExtension,
					summaryUsage,
				);
				summaryEntry = this.sessionManager.getEntry(summaryId) as BranchSummaryEntry;

				// Attach label to the summary entry
				if (label) {
					this.sessionManager.appendLabelChange(summaryId, label);
				}
			} else if (newLeafId === null) {
				// No summary, navigating to root - reset leaf
				this.sessionManager.resetLeaf();
			} else {
				// No summary, navigating to non-root
				this.sessionManager.branch(newLeafId);
			}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1583-1635`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1583-L1635)

```ts
	/**
	 * Start a new branch from an earlier entry.
	 * Moves the leaf pointer to the specified entry. The next appendXXX() call
	 * will create a child of that entry, forming a new branch. Existing entries
	 * are not modified or deleted.
	 */
	branch(branchFromId: string): void {
		if (!this.byId.has(branchFromId)) {
			throw new Error(`Entry ${branchFromId} not found`);
		}
		this.leafId = branchFromId;
	}

	/**
	 * Reset the leaf pointer to null (before any entries).
	 * The next appendXXX() call will create a new root entry (parentId = null).
	 * Use this when navigating to re-edit the first user message.
	 */
	resetLeaf(): void {
		this.leafId = null;
	}

	/**
	 * Start a new branch with a summary of the abandoned path.
	 * Same as branch(), but also appends a branch_summary entry that captures
	 * context from the abandoned conversation path.
	 */
	branchWithSummary(
		branchFromId: string | null,
		summary: string,
		details?: unknown,
		fromHook?: boolean,
		usage?: Usage,
	): string {
		if (branchFromId !== null && !this.byId.has(branchFromId)) {
			throw new Error(`Entry ${branchFromId} not found`);
		}
		const fromId = this.leafId ?? "root";
		this.leafId = branchFromId;
		const entry: BranchSummaryEntry = {
			type: "branch_summary",
			id: generateId(this.byId),
			parentId: branchFromId,
			timestamp: new Date().toISOString(),
			fromId,
			summary,
			details,
			usage,
			fromHook,
		};
		this._appendEntry(entry);
		return entry.id;
	}
```

**讲解**

三个方法都只移动 `leafId`，不删除、不修改任何已有条目：

- `branch(id)`：`leafId` 指向一个已有条目。下一个追加的条目成为它的新子条目，旧的子条目还在，于是树上多了一个分叉。
- `resetLeaf()`：`leafId = null`。下一个追加的条目 `parentId` 为 `null`，成为一个新的根。用于重新编辑第一条 user 消息。
- `branchWithSummary(id, summary)`：先记下旧的 leaf 作为 `fromId`，把 `leafId` 移到 `id`，再在那里追加一个 `branch_summary` 条目。投影时这个条目变成一条 `branchSummary` 消息，模型能看到被放弃的分支的摘要（摘要怎么生成 → M5 的 `branch-summarization.ts`）。

`branch` 只改内存，不写文件。§4.10 讲过，如果之后不追加条目，这次移动不会被保存。

`navigateTree` 移动 leaf 之后调用 `_refreshFinalizedContext()`（`packages/coding-agent/src/core/agent-session.ts:954`），用新分支的投影替换 `agent.state.messages`。

**真实实例。** `branch` 场景先写主线场景的 5 条消息，再问"用英文再说一遍"，然后 `branch` 回到第一次回答，改问"用日文再说一遍"：

```bash
node --conditions=source --experimental-strip-types m4-session.ts branch
```

```
branch(1cdfb914) 之后 leaf=1cdfb914
getTree()：
  f7c09091 message/system "You are pi."
    0490ec1f message/user "读 README.md，总结成三句话"
      44013c8a message/assistant "我先读一下 README。" <toolCall>
        bb889009 message/toolResult "# Demo ..."
          1cdfb914 message/assistant "这是一个演示项目。"
            e7e512f0 message/user "用英文再说一遍"
              9e142935 message/assistant "This is a demo project."
            10f1ada5 message/user "用日文再说一遍"
              88fc0c0a message/assistant "これはデモです。"  ← leaf
当前分支 buildSessionProjection()：
  f7c09091 message/system "You are pi."  ⇒  system "You are pi."
  0490ec1f message/user "读 README.md，总结成三句话"  ⇒  user "读 README.md，总结成三句话"
  44013c8a message/assistant "我先读一下 README。" <toolCall>  ⇒  assistant "我先读一下 README。" <toolCall>
  bb889009 message/toolResult "# Demo ..."  ⇒  toolResult "# Demo ..."
  1cdfb914 message/assistant "这是一个演示项目。"  ⇒  assistant "这是一个演示项目。"
  10f1ada5 message/user "用日文再说一遍"  ⇒  user "用日文再说一遍"
  88fc0c0a message/assistant "これはデモです。"  ⇒  assistant "これはデモです。"
  messages: system, user, assistant, toolResult, assistant, user, assistant
  model: {"provider":"faux","modelId":"faux-1"}  thinkingLevel: off
旧分支 buildSessionProjection(entries, 9e142935)：
  f7c09091 message/system "You are pi."  ⇒  system "You are pi."
  0490ec1f message/user "读 README.md，总结成三句话"  ⇒  user "读 README.md，总结成三句话"
  44013c8a message/assistant "我先读一下 README。" <toolCall>  ⇒  assistant "我先读一下 README。" <toolCall>
  bb889009 message/toolResult "# Demo ..."  ⇒  toolResult "# Demo ..."
  1cdfb914 message/assistant "这是一个演示项目。"  ⇒  assistant "这是一个演示项目。"
  e7e512f0 message/user "用英文再说一遍"  ⇒  user "用英文再说一遍"
  9e142935 message/assistant "This is a demo project."  ⇒  assistant "This is a demo project."
  messages: system, user, assistant, toolResult, assistant, user, assistant
  model: {"provider":"faux","modelId":"faux-1"}  thinkingLevel: off
文件行数=10（1 行 session 头 + 9 个条目，两条分支都在同一个文件里）
open() 之后 leaf=88fc0c0a（文件最后一行）
再次 open() 之后 leaf 的父条目=9e142935，是旧分支的最后一条：true
```

可以看到：

- 两条分支共用前 5 个条目，在 assistant "这是一个演示项目。" 处分叉。
- 当前分支的投影只有日文那一支；把旧分支的 leaf 传给模块级 `buildSessionProjection`，得到的是英文那一支。
- 文件有 10 行，两条分支都在里面。
- 重新 `open` 之后，leaf 是文件最后一行（日文分支的末端）。在新打开的实例上 `branch` 回英文分支并追加一条，再 `open` 一次，leaf 的父条目就是英文分支的最后一条。

**createBranchedSession()** 把一条分支抽出来，变成一个新的会话文件。交互模式的 `/fork` 和 `/clone` 用它（`packages/coding-agent/src/core/agent-session-runtime.ts:316`，→ M8）：

源码：[`packages/coding-agent/src/core/session-manager.ts:1642-1678`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1642-L1678)

```ts
	createBranchedSession(leafId: string): string | undefined {
		const previousSessionFile = this.sessionFile;
		const path = this.getBranch(leafId);
		if (path.length === 0) {
			throw new Error(`Entry ${leafId} not found`);
		}

		// Filter out LabelEntry from path - we'll recreate them from the resolved map.
		// Because labels are real tree entries, later entries can be children of labels;
		// removing labels requires re-chaining the retained path to avoid orphaned subtrees.
		const pathWithoutLabels: SessionEntry[] = [];
		const replacementByLabelId = new Map<string, string>();
		const pendingLabelIds: string[] = [];
		let pathParentId: string | null = null;
		for (const entry of path) {
			if (entry.type === "label") {
				pendingLabelIds.push(entry.id);
				continue;
			}
			for (const labelId of pendingLabelIds) {
				replacementByLabelId.set(labelId, entry.id);
			}
			pendingLabelIds.length = 0;
			pathWithoutLabels.push(
				entry.type === "compaction"
					? {
							...entry,
							parentId: pathParentId,
							firstKeptEntryId:
								entry.firstKeptEntryId === entry.id
									? entry.id
									: (replacementByLabelId.get(entry.firstKeptEntryId) ?? entry.firstKeptEntryId),
						}
					: { ...entry, parentId: pathParentId },
			);
			pathParentId = entry.id;
		}
```

源码：[`packages/coding-agent/src/core/session-manager.ts:1680-1737`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1680-L1737)

```ts
		const newSessionId = createSessionId();
		const timestamp = new Date().toISOString();
		const fileTimestamp = timestamp.replace(/[:.]/g, "-");
		const newSessionFile = join(this.getSessionDir(), `${fileTimestamp}_${newSessionId}.jsonl`);

		const header: SessionHeader = {
			type: "session",
			version: CURRENT_SESSION_VERSION,
			id: newSessionId,
			timestamp,
			cwd: this.cwd,
			parentSession: this.persist ? previousSessionFile : undefined,
		};

		// Collect labels for entries in the path
		const pathEntryIds = new Set(pathWithoutLabels.map((e) => e.id));
		const labelsToWrite: Array<{ targetId: string; label: string; timestamp: string }> = [];
		for (const [targetId, label] of this.labelsById) {
			if (pathEntryIds.has(targetId)) {
				labelsToWrite.push({ targetId, label, timestamp: this.labelTimestampsById.get(targetId)! });
			}
		}

		if (this.persist) {
			// Build label entries
			const lastEntryId = pathWithoutLabels[pathWithoutLabels.length - 1]?.id || null;
			let parentId = lastEntryId;
			const labelEntries: LabelEntry[] = [];
			for (const { targetId, label, timestamp: labelTimestamp } of labelsToWrite) {
				const labelEntry: LabelEntry = {
					type: "label",
					id: generateId(new Set(pathEntryIds)),
					parentId,
					timestamp: labelTimestamp,
					targetId,
					label,
				};
				pathEntryIds.add(labelEntry.id);
				labelEntries.push(labelEntry);
				parentId = labelEntry.id;
			}

			this.fileEntries = [header, ...pathWithoutLabels, ...labelEntries];
			this.sessionId = newSessionId;
			this.sessionFile = newSessionFile;
			this._buildIndex();

			// Use the same rule as _persist(): write now if the branched path already
			// has a conversation, otherwise let _persist() create the file later.
			if (this._hasConversation()) {
				this._rewriteFile();
				this.flushed = true;
			} else {
				this.flushed = false;
			}

			return newSessionFile;
		}
```

它做的事：

1. `getBranch(leafId)` 取出从根到 `leafId` 的路径。其他分支不要。
2. 去掉路径上的 `label` 条目，把剩下的条目重新串成一条链。`label` 也是树上的条目，后面的条目可能以它为父条目，所以去掉之后要重写 `parentId`。`compaction` 条目的 `firstKeptEntryId` 如果指向被去掉的 `label`，也要改成替代它的条目。
3. 新会话头的 `parentSession` 指向原文件。
4. 路径上条目的书签，重新生成为链尾的 `label` 条目。
5. 当前的 `SessionManager` 切换到新文件：替换 `fileEntries`、`sessionId`、`sessionFile`，重建索引。
6. 写文件的规则和 `_persist` 一样：路径里已经有对话就立即写，否则等到有对话时再建文件。

条目的 id 保持不变，所以新文件里的条目和原文件可以一一对应。

**往哪走**：§4.14 `forkFrom`。

### 4.14 SessionManager.forkFrom()：pi --fork

**调用位置**：`pi --fork <路径或 id>`，或者 `pi --session` 找到的会话属于另一个项目且用户同意复制时（`packages/coding-agent/src/main.ts:350`）。

源码：[`packages/coding-agent/src/core/session-manager.ts:1825-1881`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1825-L1881)

```ts
	static forkFrom(
		sourcePath: string,
		targetCwd: string,
		sessionDir?: string,
		options?: NewSessionOptions,
	): SessionManager {
		const resolvedSourcePath = resolvePath(sourcePath);
		const resolvedTargetCwd = resolvePath(targetCwd);
		const sourceEntries = loadEntriesFromFile(resolvedSourcePath);
		if (sourceEntries.length === 0) {
			throw new Error(`Cannot fork: source session file is empty or invalid: ${resolvedSourcePath}`);
		}

		const sourceHeader = sourceEntries.find((e) => e.type === "session") as SessionHeader | undefined;
		if (!sourceHeader) {
			throw new Error(`Cannot fork: source session has no header: ${resolvedSourcePath}`);
		}

		const dir = sessionDir ? normalizePath(sessionDir) : getDefaultSessionDir(resolvedTargetCwd);
		if (!existsSync(dir)) {
			mkdirSync(dir, { recursive: true });
		}

		// Create new session file with new ID but forked content
		if (options?.id !== undefined) {
			assertValidSessionId(options.id);
		}
		const newSessionId = options?.id ?? createSessionId();
		const timestamp = new Date().toISOString();
		const fileTimestamp = timestamp.replace(/[:.]/g, "-");
		const newSessionFile = join(dir, `${fileTimestamp}_${newSessionId}.jsonl`);

		// Write new header pointing to source as parent, with updated cwd
		const newHeader: SessionHeader = {
			type: "session",
			version: CURRENT_SESSION_VERSION,
			id: newSessionId,
			timestamp,
			cwd: resolvedTargetCwd,
			parentSession: resolvedSourcePath,
		};
		// Write the header and all non-header entries through one fd. Reopening the file per entry
		// is slow where open/close is expensive (e.g. filesystem security monitors).
		const fd = openSync(newSessionFile, "wx");
		try {
			writeFileSync(fd, `${JSON.stringify(newHeader)}\n`);
			for (const entry of sourceEntries) {
				if (entry.type !== "session") {
					writeFileSync(fd, `${JSON.stringify(entry)}\n`);
				}
			}
		} finally {
			closeSync(fd);
		}

		return new SessionManager(resolvedTargetCwd, dir, newSessionFile, true);
	}
```

**讲解**

`forkFrom` 把另一个项目（或同一个项目）的会话完整复制成当前项目下的新会话：

- 读入源文件的全部条目，包括所有分支。这一点和 `createBranchedSession` 不同，后者只取一条分支。
- 新会话头用新的 id、目标项目的 `cwd`，`parentSession` 指向源文件。
- 用一个文件描述符写出会话头和所有条目。注释说明了原因：每个条目开关一次文件，在有文件系统安全监控的机器上很慢。
- 最后 `open` 这个新文件（构造函数带 `sessionFile`），leaf 是最后一个条目。

和 `createBranchedSession` 一样，条目 id 不变。

**真实实例。** `fork` 场景依次调用 `createBranchedSession`、`forkFrom` 和 `continueRecent`：

```bash
node --conditions=source --experimental-strip-types m4-session.ts fork
```

```
原文件: 2026-11-01T07-05-32-807Z_01a195ed-8187-745f-8dc9-ca866f04ecfb.jsonl 条目=5
createBranchedSession(cf9e5bbe) → 2026-11-01T07-05-32-809Z_01a195ed-8189-745f-8dc9-ca8994a7baef.jsonl
  条目=4 leaf=cf9e5bbe parentSession=2026-11-01T07-05-32-807Z_01a195ed-8187-745f-8dc9-ca866f04ecfb.jsonl
  条目 id 和原文件相同：true
forkFrom(原文件, other-project) → 2026-11-01T07-05-32-809Z_01a195ed-8189-745f-8dc9-ca8a51b30e71.jsonl
  条目=5 cwd=other-project parentSession=2026-11-01T07-05-32-807Z_01a195ed-8187-745f-8dc9-ca866f04ecfb.jsonl
continueRecent(project) → 2026-11-01T07-05-32-809Z_01a195ed-8189-745f-8dc9-ca8994a7baef.jsonl（mtime 最新的文件）
continueRecent(空目录) → 新会话 2026-11-01T07-05-32-810Z_01a195ed-818a-745f-8dc9-ca8c0efd9ca6.jsonl，文件存在=false
```

- `createBranchedSession(toolResult)` 只保留了到工具结果为止的 4 个条目，leaf 是工具结果，`parentSession` 指向原文件。
- `forkFrom` 复制了全部 5 个条目，新文件放在 `other-project` 的会话目录里，会话头的 `cwd` 是 `other-project`。
- `continueRecent(project)` 选中的不是原文件，而是 `createBranchedSession` 刚写的文件，因为它的 mtime 最新。
- 在一个没有会话的目录里 `continueRecent`，得到一个新会话，文件还没有创建（§4.3）。

**往哪走**：§4.15 旁支。

### 4.15 旁支：会话列表、迁移、findById

这些代码和 agent 怎么工作无关，只列出位置：

- `SessionManager.list()` / `listAll()`（`packages/coding-agent/src/core/session-manager.ts:1915`、`packages/coding-agent/src/core/session-manager.ts:1936`）：给 `pi -r` 和 `/resume` 的选择界面列出会话。`buildSessionInfo`（`packages/coding-agent/src/core/session-manager.ts:799`）逐行流式读文件，统计消息数、取第一条 user 消息、取 `session_info` 里的会话名。
- `findById()`（`packages/coding-agent/src/core/session-manager.ts:1889`）：`--session-id` 用它按 id 找文件，只读会话头。
- `migrateV1ToV2` / `migrateV2ToV3`（`packages/coding-agent/src/core/session-manager.ts:287`、`packages/coding-agent/src/core/session-manager.ts:316`）：§4.10 讲过的版本迁移。
- `getTree()`（`packages/coding-agent/src/core/session-manager.ts:1539`）：把所有条目组成 `SessionTreeNode` 树，子条目按时间排序，给 `/tree` 界面用。§4.13 的 `branch` 场景用它打印树。

---

## 5. 状态机

### 5.1 会话文件的生命周期

```
                       newSession()  flushed=false (session-manager.ts:1076)
                       只算出文件名，不建文件 (1078-1080)
                              │
                              ▼
                 ┌─────────────────────────┐
                 │ 未创建                    │  append*() → _persist()：
                 │ 条目只在 fileEntries 里    │  没有 user/assistant 消息 → return (1176)
                 └────────────┬────────────┘
                              │ 第一条 user 或 assistant 消息 (1166-1170)
                              │ openSync(file, "wx")，写出全部 fileEntries (1177-1184)
                              │ flushed=true (1185)
                              ▼
                 ┌─────────────────────────┐
                 │ 已创建                    │  append*() → appendFileSync 一行 (1187)
                 │ 文件 = fileEntries        │  写失败 → 回滚内存，抛错 (1198-1204)
                 └─────────────────────────┘
                              ▲
   open() / continueRecent() / forkFrom()：文件已存在 → _loadEntries，flushed=true (1048-1049)
   文件存在但为空 → 写会话头，flushed=true (1036-1045)
   版本 < 3 → 迁移后 _rewriteFile 整个重写 (1092-1094)
   createBranchedSession()：路径里有对话 → _rewriteFile，flushed=true (1729-1731)
                            否则 flushed=false，回到"未创建" (1733)
   persist=false（inMemory）：_persist 直接 return (1173)，永远停在"未创建"
```

### 5.2 leaf 指针

```
                 ┌──────────────────────────────────────────────┐
                 │ leafId = X                                    │
                 └──┬───────────┬─────────────┬────────────┬────┘
   append*() 成功    │           │ branch(Y)   │ resetLeaf()│ branchWithSummary(Y)
   (1195)           │           │ (1593)      │ (1602)     │ (1621) 再 append 摘要 (1633)
                    ▼           ▼             ▼            ▼
              leafId = 新条目  leafId = Y   leafId = null  leafId = 摘要条目
              parentId = X    （只改内存）   下一个条目成为   parentId = Y
                                            新的根
   append*() 失败：leafId 恢复为 X (1203)
   _buildIndex()（open 时）：leafId = 文件最后一个条目 (1108-1111)
   newSession()：leafId = null (1075)
```

### 5.3 一个条目在投影里的去向

```
 SessionEntry
   │ 不在 leaf 到根的路径上 (buildSessionPath 390-416) ─────────────▶ 不贡献
   ▼
 在当前分支上
   │ 路径上有 compaction，且条目在最新 compaction 之前：
   │   在 firstKeptEntryId 之前 ──────────────────────────────────▶ 不贡献（被摘要代替）
   │   在保留范围内但是 system 消息 (506) ─────────────────────────▶ 不贡献（由 systemMessage 代替）
   ▼
 被选中 (buildContextEntries 476-512)
   │ 是排在第 0 位之后的 compaction 条目 (562-563) ─────────────────▶ 不贡献
   │ 最新的 context_edit 是 null (523) ───────────────────────────▶ 不贡献
   │ 最新的 context_edit 有内容 (525-538) ─────────────────────────▶ 贡献替换内容后的消息
   ▼
 sessionEntryToContextMessages (439-466)
   message → 原消息；custom_message → custom；branch_summary → branchSummary；
   compaction → [systemMessage, compactionSummary]；其他 → 不贡献
```

---

## 6. 设计要点

### 6.1 只追加的条目树

**做法。** 会话文件只追加。分支（`branch`）只移动 leaf 指针；改上下文（`context_edit`）、总结旧消息（`compaction`）、记录放弃的分支（`branch_summary`）都是追加新条目，投影时再生效。唯一的重写发生在版本迁移（§4.10）。

**理由。** 历史永远可以找回：回到 `context_edit` 之前的条目，原内容又出现了（§7.2）；被压缩掉的消息仍在文件里，界面可以照常显示。追加一行的写法也简单可靠：进程被杀最多留下半行，读取时跳过即可（§4.10）。代价是文件只增不减，所有分支都在一个文件里。

**代码位置。** `packages/coding-agent/src/core/session-manager.ts:1191`（唯一的写入口 `_appendEntry`），`packages/coding-agent/src/core/session-manager.ts:1589`（`branch`）。

### 6.2 会话文件是上下文的唯一来源

**做法。** M8 不维护自己的上下文。每次请求模型前，`prepareRequest` 都用 `buildSessionProjection()` 重新算出 transcript（§4.5）；恢复会话、切换分支、压缩、编辑之后，也用同一个函数刷新 `agent.state.messages`（`_refreshFinalizedContext`，`packages/coding-agent/src/core/agent-session.ts:954`）。

**理由。** 只有一个函数决定模型看到什么，所以"继续会话后模型看到的"和"一直没退出时模型看到的"一定相同（§7.1）。压缩、编辑、分支只需要写条目，不需要同步修改内存里的 transcript。代价是每次请求都要遍历一次路径，对当前的会话规模来说成本很低。

**代码位置。** `packages/coding-agent/src/core/agent-session.ts:809`，`packages/coding-agent/src/core/sdk.ts:210`。

### 6.3 有对话才建文件

**做法。** 新会话的设置条目先留在内存里，第一条 user 或 assistant 消息出现时，才把会话头和所有积攒的条目一起写进文件（§4.3）。

**理由。** 打开 pi 又直接退出，不会在会话目录里留下空会话，`pi -c` 和 `/resume` 列表也不会被这些空会话干扰。以 user 消息为界而不是 assistant 回复，保证用户的输入一定落盘，即使第一轮请求失败。

**代码位置。** `packages/coding-agent/src/core/session-manager.ts:1166`，`packages/coding-agent/src/core/session-manager.ts:1729`（`createBranchedSession` 用同样的规则）。

### 6.4 内存和文件保持一致

**做法。** `_appendEntry` 先改内存再写文件，写失败就撤销内存的修改（§4.2）。第一次建文件用 `"wx"`，不覆盖已存在的文件（§4.3）。

**理由。** `parentId` 把条目串成树，一个没写进文件的条目如果留在内存里当父条目，之后写进文件的条目都会指向一个不存在的父条目，下次打开时这些条目就从当前分支上消失了。回滚让这种情况不可能发生（§7.3）。

**代码位置。** `packages/coding-agent/src/core/session-manager.ts:1198-1204`，`packages/coding-agent/src/core/session-manager.ts:1177`。

### 6.5 投影保留来源

**做法。** `SessionProjection.entries` 记录每条消息来自哪个条目（`sourceEntry`）。M8 刷新上下文时，把每条投影出的消息和条目 id 的对应关系存进 `_entryIdsByMessage`（`packages/coding-agent/src/core/agent-session.ts:954-959`），需要按消息找条目时，再用投影兜底（`_findPersistedMessageEntryId`，`packages/coding-agent/src/core/agent-session.ts:1232`）。

**理由。** 被 `context_edit` 替换过的消息是新对象，按对象身份在条目里找不到它。有了 `sourceEntry`，M8 仍然能找到要编辑的条目 id，例如重试时去掉失败的尝试（`packages/coding-agent/src/core/agent-session.ts:1260`）。

**代码位置。** `packages/coding-agent/src/core/session-manager.ts:209`，`packages/coding-agent/src/core/session-manager.ts:555-566`。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行。id、时间戳和文件名每次运行都不同。

### 7.1 pi -c 之后模型看到什么

**问题。** 在 §2.1 的 shell 里（同样的 `WORK` 和 `PI_CODING_AGENT_DIR`），运行 `pi -c -p "再总结一次"`。假模型的两条回复和主线场景一样：先调用 read，再给出总结。

1. 会新建一个会话文件，还是追加到原来的文件？
2. 第 1 次请求，模型收到几条消息？会多出一条 system 消息吗？
3. 会话文件会多出 `model_change` 或 `thinking_level_change` 吗？
4. 新的 user 消息条目的 `parentId` 是什么？

**预测。**

1. `continueRecent` 找到 mtime 最新的文件，也就是 §2.1 的文件（§4.11）。不新建文件，目录里仍然只有一个文件。
2. 6 条。前 5 条来自投影：system、user、assistant、toolResult、assistant；第 6 条是新的 user 消息。系统提示词和工具清单没变，M8 不需要写新的 system 补丁，所以不会多出 system 消息（→ M6 `diffSystemPromptSections`）。
3. 不会。已有会话不写 `model_change`，而且已经有 `thinking_level_change`（§4.12）。
4. 原文件最后一个条目，也就是第一次运行的最后一条 assistant 消息。`open` 之后 leaf 是文件最后一行（§4.10）。

**运行。**

```bash
PI_TRACE=1 ./run.sh -c -p "再总结一次"
ls $PI_CODING_AGENT_DIR/sessions/*/
node show-session.mjs "$(ls -t $PI_CODING_AGENT_DIR/sessions/*/*.jsonl | head -1)"
```

```
Running without API keys...
[faux] 第 1 次请求，收到 6 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
  assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}]
  user       [{"type":"text","text":"再总结一次"}]
[faux] 第 2 次请求，收到 8 条消息:
  system     sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write]
  user       [{"type":"text","text":"读 README.md，总结成三句话"}]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
  assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}]
  user       [{"type":"text","text":"再总结一次"}]
  assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_
  toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one 
这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。
2026-11-01T07-05-32-450Z_01a195ed-8022-70cb-9024-a443607c2453.jsonl
session                id=01a195ed parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/m4-work"}
model_change           id=2c14b2d0 parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=bf773565 parent=2c14b2d0 {"thinkingLevel":"off"}
message                id=c28a59fb parent=bf773565 system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write
message                id=78be6f6a parent=c28a59fb user       [{"type":"text","text":"读 README.md，总结成三句话"}]
message                id=b1629b82 parent=78be6f6a assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_1","name":"read","ar stop=toolUse
message                id=8b71d46b parent=b1629b82 toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used 
message                id=dc56afaf parent=8b71d46b assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}] stop=stop
message                id=da6b5bad parent=dc56afaf user       [{"type":"text","text":"再总结一次"}]
message                id=a9a7321a parent=da6b5bad assistant  [{"type":"text","text":"我先读一下 README。"},{"type":"toolCall","id":"call_1","name":"read","ar stop=toolUse
message                id=647c65e4 parent=a9a7321a toolResult [{"type":"text","text":"# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used 
message                id=65d43c5c parent=647c65e4 assistant  [{"type":"text","text":"这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"}] stop=stop
```

**结论。** 四个预测都成立。目录里仍是一个文件。第 1 次请求的 6 条消息里，前 5 条和主线场景第一次运行结束时的 transcript 相同。文件只多了 4 个 `message` 条目，新 user 消息的 `parent` 是第一次运行最后那条 assistant 消息的 id。第 2 次请求收到 8 条消息，说明第 1 次请求的回复和工具结果已经写进会话文件，又被 `prepareRequest` 投影了出来。

### 7.2 context_edit 只在它所在的分支上生效

**问题。** `edit-branch` 场景先写主线场景的 5 条消息，再追加一个 `context_edit`，用 `null` 去掉 toolResult，然后追加 user 消息"继续"。之后 `branch` 回到第二条 assistant 消息（在 `context_edit` 之前）。两次投影分别是什么？

**预测。**

- 编辑之后：toolResult 条目被选中，但贡献空数组（§4.8）。消息是 system、user、assistant、assistant、user，共 5 条。
- `branch` 回去之后：`context_edit` 条目不在新的路径上，`buildSessionProjection` 收集编辑时看不到它（§4.5），toolResult 重新出现。消息是 system、user、assistant、toolResult、assistant。
- 编辑所在的分支没有被删除，从它的 leaf 往根走仍有 7 个条目。

**运行。**

```bash
node --conditions=source --experimental-strip-types m4-session.ts edit-branch
```

```
编辑之后（leaf 在 context_edit 之后）：
  4851c29f message/system "You are pi."  ⇒  system "You are pi."
  f669ea85 message/user "读 README.md，总结成三句话"  ⇒  user "读 README.md，总结成三句话"
  479a680e message/assistant "我先读一下 README。" <toolCall>  ⇒  assistant "我先读一下 README。" <toolCall>
  a9bec1f3 message/toolResult "# Demo ..."  ⇒  （无）
  5bc9bd8a message/assistant "这是一个演示项目。"  ⇒  assistant "这是一个演示项目。"
  ef7b770b context_edit → a9bec1f3 null  ⇒  （无）
  6489dddd message/user "继续"  ⇒  user "继续"
  messages: system, user, assistant, assistant, user
  model: {"provider":"faux","modelId":"faux-1"}  thinkingLevel: off
branch(5bc9bd8a) 回到编辑之前：
  4851c29f message/system "You are pi."  ⇒  system "You are pi."
  f669ea85 message/user "读 README.md，总结成三句话"  ⇒  user "读 README.md，总结成三句话"
  479a680e message/assistant "我先读一下 README。" <toolCall>  ⇒  assistant "我先读一下 README。" <toolCall>
  a9bec1f3 message/toolResult "# Demo ..."  ⇒  toolResult "# Demo ..."
  5bc9bd8a message/assistant "这是一个演示项目。"  ⇒  assistant "这是一个演示项目。"
  messages: system, user, assistant, toolResult, assistant
  model: {"provider":"faux","modelId":"faux-1"}  thinkingLevel: off
编辑后的分支仍在会话里：leaf 6489dddd 的路径长度=7
```

**结论。** 预测成立。`context_edit` 只是路径上的一个条目，它的效果随路径变化。这就是 `session-format.md` 说的"编辑是相对分支的"：回到编辑之前的位置，目标条目的原始内容又会出现。

### 7.3 写文件失败时内存回滚

**问题。** `fail` 场景先写 5 条消息（文件已创建），然后把会话文件改成只读，再 `appendMessage`。抛错之后，`leafId` 和内存里的条目数是多少？恢复写权限后再追加一条，它的父条目是谁？

**预测。** `appendFileSync` 抛 `EACCES`。`_appendEntry` 的 catch 分支把条目从 `fileEntries` 和 `byId` 里删掉，`leafId` 恢复原值（§4.2）。所以抛错后 leaf 不变，内存条目仍是 5 个，文件仍是 6 行。恢复权限后追加的条目，父条目是写入失败之前的 leaf，文件变成 7 行。

如果没有回滚，失败的条目会成为新条目的父条目，文件里新条目的 `parentId` 指向一个不存在的 id。

**运行。**

```bash
node --conditions=source --experimental-strip-types m4-session.ts fail
```

```
写入前 leaf=98b345e3 内存条目=5 文件行数=6
appendMessage 抛错：EACCES
抛错后 leaf=98b345e3 内存条目=5 文件行数=6
恢复后新条目的 parent=98b345e3 文件行数=7
```

**结论。** 预测成立。写入失败的条目没有留下任何痕迹，后续条目接在失败之前的位置上，文件里的树仍然完整。`appendMessage` 不吞掉异常，而是在回滚之后继续抛给调用者。在 pi 里调用者是 M8 的 `_handleAgentEvent`，它怎么处理见 M8。

---

## 8. 自测与衔接

### 8.1 自测

1. 用 `pi --no-session` 运行主线场景，会话目录里会出现文件吗？`buildSessionProjection` 还能工作吗？
2. 一个会话路径上有两个 `compaction` 条目 C1、C2（C2 更新），C2 的 `firstKeptEntryId` 指向 C1 之前的某个条目。投影里会出现几条 `compactionSummary` 消息？
3. 用户在 `/tree` 里切到一条旧分支，没有发消息就退出了。下次 `pi -c`，会在哪条分支上继续？

<details>
<summary>答案</summary>

1. 不会出现文件。`--no-session` 让 `createSessionManager` 返回 `SessionManager.inMemory()`（`packages/coding-agent/src/main.ts:365`），`persist` 为 `false`，`_persist` 第一行就返回（`packages/coding-agent/src/core/session-manager.ts:1173`）。投影照常工作，因为它只读内存里的 `fileEntries` 和 `byId`，和文件无关。
2. 一条。`buildContextEntries` 以 C2 为准，C1 在 C2 的保留范围内，作为普通条目被选中，排在第 0 位之后。`buildSessionProjection` 只让第 0 位的压缩条目贡献消息，C1 贡献空数组（`packages/coding-agent/src/core/session-manager.ts:562-564`）。
3. 在切换之前的分支上。`branch()` 只改内存里的 `leafId`，不写文件（`packages/coding-agent/src/core/session-manager.ts:1593`）。下次打开时 `_buildIndex` 把 leaf 设为文件最后一个条目（`packages/coding-agent/src/core/session-manager.ts:1108-1111`），而最后一个条目在切换之前的分支上。

</details>

### 8.2 衔接

本模块在 §4.4 和 §4.7 读到了压缩条目怎么写入、怎么在投影里生效，但没有读压缩条目怎么产生。M5 从 `shouldCompact()`（`packages/coding-agent/src/core/compaction/compaction.ts:267`）开始，读 pi 什么时候决定压缩、怎么选 `firstKeptEntryId`、怎么生成摘要，最后调用 `SessionManager.appendCompaction()`，又回到本模块的 `buildContextEntries()`。

本模块里标了"→ M8"的调用者（`_handleAgentEvent`、`prepareRequest` hook、`navigateTree`、`_omitRecoveryAttempt`），在 M8 从 `createAgentSession()` 读起；标了"→ M9"的 `createSessionManager()`，在 M9 从 `main()` 读起。
