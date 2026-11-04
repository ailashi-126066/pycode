# M5 压缩：上下文快满时把旧对话换成摘要

> 源码版本：提交 [`2c8d1feb`](https://github.com/earendil-works/pi/tree/2c8d1feb147f6b3d97e855da803bfe7b19f3c403)。本文的命令都在 `docs/pi-tutorial/lab` 目录下运行，并且已经按 [README §4.1](README.md#41-获取源码并安装) 设置了 `export PI=/path/to/pi`。

---

## 1. 这个模块是什么

### 1.1 职责

模型的上下文窗口有上限。对话越长，每次请求带的 transcript 越大，最后会超出窗口。压缩（compaction）解决这个问题：当估算的上下文 token 数超过阈值时，把较早的一段对话交给模型写成一份结构化摘要，然后在会话文件里追加一个 `compaction` 会话条目。之后重建上下文时，这段旧对话被摘要代替，只有最近的一段原样保留。

本模块（源码目录 `packages/coding-agent/src/core/compaction`）由一组纯函数组成，回答四个问题：

- 什么时候压缩：`estimateContextTokens` 估算上下文大小，`shouldCompact` 和阈值比较。
- 从哪里切：`findCutPoint` 从最新的消息往回累加 token，找到保留区的起点，并且绝不在工具结果处切开。
- 摘要怎么写：`generateSummary` 把要丢弃的消息序列化成文本，套上固定的摘要模板请求模型。
- 结果怎么存：`compact` 返回 `CompactionResult`，由 AgentSession（M8）交给 `SessionManager.appendCompaction()` 写进会话文件（M4）。

这些函数本身不读写文件，也不发事件。调用时机、事件、扩展拦截都在 M8 的 `AgentSession` 里。同一目录下的 `branch-summarization.ts` 处理另一种摘要：在会话树上切换分支时，把离开的那条分支写成摘要（§4.15）。

### 1.2 在调用链中的位置

在 [README §2](README.md#2-全局调用链) 的调用链里，M5 挂在两个位置：轮与轮之间的 `prepareNextTurn()`，和一次运行结束后的 `_handlePostAgentRun()`。

```
session.prompt(text)                                       [M8]
└─ _prepareRun()
│   └─ _checkCompaction(lastAssistant)   发 prompt 前检查一次   [M8 → M5]
└─ agent.prompt(messages)                                  [M2]
    └─ runLoop()
        ├─ …一轮：请求模型、执行工具…
        └─ prepareNextTurn()  ← AgentSession 安装的 hook        [M8]
            └─ _compactBeforeNextAssistantResponse()
                ├─ estimateProjectedContextTokens()          [M5] §4.2
                ├─ shouldCompact()                           [M5] §4.1 ← 本模块入口
                └─ _runAutoCompaction("threshold")           [M8]
                    ├─ prepareCompaction()                   [M5] §4.4
                    │   ├─ buildSessionProjection()          → M4
                    │   ├─ findCutPoint()                    [M5] §4.5
                    │   └─ extractFileOperations()           [M5] §4.6
                    ├─ compact()                             [M5] §4.7
                    │   ├─ generateSummary()                 [M5] §4.8
                    │   │   ├─ serializeConversation()       [M5] §4.9
                    │   │   └─ completeSummarization() → streamFn → M1
                    │   └─ generateTurnPrefixSummary()       [M5] §4.10
                    ├─ SessionManager.appendCompaction()     [M4] §4.12
                    └─ _refreshFinalizedContext()
                        └─ buildSessionProjection()
                            └─ buildContextEntries()         [M4] §4.13
└─ _handlePostAgentRun() → _checkCompaction()  运行结束后再检查一次 [M8 → M5]
```

主线场景 `pi -p "读 README.md，总结成三句话"` 不会触发压缩：上下文只有一千多 token，离阈值很远（§2.1）。所以本模块的实验用一个上下文窗口只有 3000 token 的假模型来强制触发（§2.2）。

### 1.3 文件清单

| 文件 | 行数 | 内容 | 阅读方式 |
|---|---|---|---|
| `packages/coding-agent/src/core/compaction/compaction.ts` | 1104 | token 估算、阈值判断、切点、摘要模板、`prepareCompaction`、`compact` | 精读，§4 的主体 |
| `packages/coding-agent/src/core/compaction/utils.ts` | 163 | 文件操作跟踪、`serializeConversation`、摘要请求的系统提示词 | 精读 |
| `packages/coding-agent/src/core/compaction/branch-summarization.ts` | 386 | 分支摘要：收集离开的条目、按预算挑消息、生成摘要 | 通读，§4.15 |
| `packages/coding-agent/src/core/compaction/index.ts` | 7 | 重新导出以上三个文件 | 略 |

行数用 `wc -l packages/coding-agent/src/core/compaction/*.ts` 统计。另外会读到其他文件里和压缩直接相关的几段：`agent-session.ts` 的触发和落盘（M8 的代码，这里只读压缩相关的部分），`session-manager.ts` 的 `appendCompaction` 和 `buildContextEntries`（M4），`messages.ts` 的 `CompactionSummaryMessage`。用户文档是 `packages/coding-agent/docs/compaction.md`。

### 1.4 读完能回答的问题

1. 压缩的阈值是怎么算的？上下文 token 数从哪里来，是精确值还是估算？
2. 保留区从哪条消息开始？为什么切点不能落在工具结果上？
3. 一个 user 消息之后的工具调用太长、一个人就超过保留预算时，怎么切？
4. 第二次压缩时，第一次的摘要去哪了？读过的文件清单会丢吗？
5. 压缩之后，会话文件里的旧消息被删了吗？模型看到的上下文是什么样子？

---

## 2. 先跑一次

### 2.1 主线场景：离阈值很远

主线场景的假模型（`lab/faux-ext.ts`）用 pi-ai 默认的模型定义：上下文窗口 128000 token。压缩的默认设置是 `reserveTokens = 16384`，所以阈值是 128000 − 16384 = 111616 token。[`lab/m5-inspect.mjs`](lab/m5-inspect.mjs) 的 `tokens` 模式把事件流里和 token 有关的部分摘出来：

```bash
mkdir -p /tmp/pi-lab
WORK=/tmp/pi-lab/m5-main-1-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m5-main-1-agent ./run.sh --mode json "读 README.md，总结成三句话" > /tmp/pi-lab/m5-main-1.jsonl
node m5-inspect.mjs tokens /tmp/pi-lab/m5-main-1.jsonl
```

```
assistant   totalTokens=1322  "我先读一下 README。" + toolCall read(README.md)
toolResult  75 字符 ≈ 19 token
assistant   totalTokens=1357  "这是一个演示项目。它只有一个 README。它用来跟踪 pi 的运行。"
```

faux provider 会按请求的字符数估算 `usage`（每 4 个字符算 1 token），所以假模型也有 `totalTokens`。第 2 次请求的上下文约 1357 token，远小于 111616，没有 `compaction_start` 事件。

### 2.2 强制压缩：3000 token 的上下文窗口

[`lab/m5-faux-ext.ts`](lab/m5-faux-ext.ts) 是另一个假模型扩展。它的上下文窗口只有 3000 token（环境变量 `M5_CTX` 可改），回复按请求内容决定：用户让它依次读哪些 `.txt` 文件，它就一次调用一个 read，读完了回复"文件都读完了。"；收到压缩请求时，返回一段格式正确的假摘要。[`lab/m5-run.sh`](lab/m5-run.sh) 在工作目录里生成 `a.txt` 到 `e.txt`（每个 1848 字符，约 462 token），并在配置目录写入 `settings.json`：

```json
{"compaction":{"reserveTokens":500,"keepRecentTokens":600}}
```

于是阈值是 3000 − 500 = 2500 token，压缩后保留最近约 600 token。系统提示词本身约 1300 token，读三个文件就会超过阈值。

```bash
WORK=/tmp/pi-lab/m5-force-1-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m5-force-1-agent M5_TRACE=1 ./m5-run.sh --mode json "依次读 a.txt b.txt c.txt" 2>&1 > /tmp/pi-lab/m5-force-1.jsonl
```

`2>&1 > 文件` 的顺序让 stdout（json 事件）进文件，stderr（`M5_TRACE=1` 打印的请求内容）留在终端。终端输出：

```
[faux] 第 1 次请求：普通请求，收到 2 条消息:
  system            sections=[preamble,tools,rules,docs,cwd] content=""
  user              "依次读 a.txt b.txt c.txt"
[faux] 第 2 次请求：普通请求，收到 4 条消息:
  system            sections=[preamble,tools,rules,docs,cwd] content=""
  user              "依次读 a.txt b.txt c.txt"
  assistant         "读 a.txt。"
  toolResult        "[a.txt]\nline 01 of a.txt: lorem ipsum dolor sit amet.\nlin
[faux] 第 3 次请求：普通请求，收到 6 条消息:
  system            sections=[preamble,tools,rules,docs,cwd] content=""
  user              "依次读 a.txt b.txt c.txt"
  assistant         "读 a.txt。"
  toolResult        "[a.txt]\nline 01 of a.txt: lorem ipsum dolor sit amet.\nlin
  assistant         "读 b.txt。"
  toolResult        "[b.txt]\nline 01 of b.txt: lorem ipsum dolor sit amet.\nlin
[faux] 第 4 次请求：压缩请求（分割轮次前缀），提示词 4589 字符:
  | # Conversation
  | [User]: 依次读 a.txt b.txt c.txt
  | 
  | [Assistant]: 读 a.txt。
  | 
  | [Assistant tool calls]: read(path="a.txt")
  | 
  | [Tool result]: [a.txt]
  | （这里是 40 行文件内容，不逐行打印）
  | 
  | 
  | [Assistant]: 读 b.txt。
  | 
  | [Assistant tool calls]: read(path="b.txt")
  | 
  | [Tool result]: [b.txt]
  | （这里是 40 行文件内容，不逐行打印）
  | 
  | 
  | # Instructions
  | The messages above are earlier context from an ongoing conversation. Later messages are stored separately and 
  | 
  | Create a concise checkpoint of the user's request and the progress shown above. This checkpoint will be placed
  | 
  | ## Original Request
  | [What did the user ask for?]
  | 
  | ## Progress So Far
  | - [Key decisions and work completed in these messages]
  | 
  | ## Context Needed to Continue
  | - [Information from these messages needed to understand the later work]
  | 
  | Only summarize information explicitly present above. Do not infer or recreate later messages.
[faux] 第 5 次请求：普通请求，收到 4 条消息:
  system            sections=[preamble,tools,rules,docs,cwd] content=""
  user              "The conversation history before this point was compacted in
  assistant         "读 c.txt。"
  toolResult        "[c.txt]\nline 01 of c.txt: lorem ipsum dolor sit amet.\nlin
```

可以看到：

- 前 3 次是普通请求，每次多一对 assistant + toolResult。
- 第 4 次是压缩请求。它的系统提示词不是 pi 的系统提示词，而是摘要专用的；它的 user 消息是对话的文本版本，后面跟着摘要模板。这里用的是"分割轮次前缀"模板（§4.10）。
- 第 5 次请求只有 4 条消息：系统提示词、一条以 "The conversation history before this point was compacted" 开头的 user 消息（摘要）、读 c.txt 的 assistant 和它的 toolResult。原来的 user 消息和读 a.txt、b.txt 的四条消息都不见了。

事件流里，压缩发生在第 3 轮 `turn_end` 和第 4 轮 `turn_start` 之间：

```bash
node summarize-events.mjs /tmp/pi-lab/m5-force-1.jsonl | tail -14
```

```
message_end (toolResult)
turn_end (assistant)
compaction_start
compaction_end
turn_start
message_start (assistant)
message_update [text_start]
message_update [text_delta]
message_update [text_end]
message_end (assistant)
turn_end (assistant)
agent_end
agent_settled
共 61 行（含第一行 session 头）
```

每次请求的 token 数：

```bash
node m5-inspect.mjs tokens /tmp/pi-lab/m5-force-1.jsonl
```

```
assistant   totalTokens=1321  "读 a.txt。" + toolCall read(a.txt)
toolResult  1848 字符 ≈ 462 token
assistant   totalTokens=1798  "读 b.txt。" + toolCall read(b.txt)
toolResult  1848 字符 ≈ 462 token
assistant   totalTokens=2275  "读 c.txt。" + toolCall read(c.txt)
toolResult  1848 字符 ≈ 462 token
compaction_start reason=threshold
compaction_end   reason=threshold aborted=false willRetry=false tokensBefore=2737 estimatedTokensAfter=1819 firstKeptEntryId=4ca81392
assistant   totalTokens=1866  "文件都读完了。"
```

第 3 次请求的 `totalTokens` 是 2275，再加上读 c.txt 的结果约 462 token，估算的上下文是 2737 token，超过 2500，于是压缩。压缩后估算值降到 1819，第 4 次请求的 `totalTokens` 是 1866。

### 2.3 会话文件里的 compaction 条目

```bash
S=$(ls /tmp/pi-lab/m5-force-1-agent/sessions/*/*.jsonl)
node show-session.mjs "$S"
```

```
session                id=01a1969c parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/m5-force-1-work"}
model_change           id=3a0c3305 parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=33100886 parent=3a0c3305 {"thinkingLevel":"off"}
message                id=0118698f parent=33100886 system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write
message                id=c93fc35f parent=0118698f user       [{"type":"text","text":"依次读 a.txt b.txt c.txt"}]
message                id=c766cd89 parent=c93fc35f assistant  [{"type":"text","text":"读 a.txt。"},{"type":"toolCall","id":"call_a.txt","name":"read","arg stop=toolUse
message                id=948722b6 parent=c766cd89 toolResult [{"type":"text","text":"[a.txt]\nline 01 of a.txt: lorem ipsum dolor sit amet.\nline 02 of
message                id=56c59afe parent=948722b6 assistant  [{"type":"text","text":"读 b.txt。"},{"type":"toolCall","id":"call_b.txt","name":"read","arg stop=toolUse
message                id=0c503438 parent=56c59afe toolResult [{"type":"text","text":"[b.txt]\nline 01 of b.txt: lorem ipsum dolor sit amet.\nline 02 of
message                id=4ca81392 parent=0c503438 assistant  [{"type":"text","text":"读 c.txt。"},{"type":"toolCall","id":"call_c.txt","name":"read","arg stop=toolUse
message                id=20c3ada5 parent=4ca81392 toolResult [{"type":"text","text":"[c.txt]\nline 01 of c.txt: lorem ipsum dolor sit amet.\nline 02 of
compaction             id=c4454a86 parent=20c3ada5 {"summary":"No prior history.\n\n---\n\n**Turn Context (split turn):**\n\n## Original Request\n- 依次读
message                id=3a8774c2 parent=c4454a86 assistant  [{"type":"text","text":"文件都读完了。"}] stop=stop
```

压缩没有删除任何条目。读 a.txt、b.txt 的消息还在文件里，只是多了一个 `compaction` 条目，它的 `parent` 是最后一个 toolResult。之后的 assistant 回复挂在 `compaction` 条目下面。完整的 `compaction` 条目：

```bash
node m5-inspect.mjs compaction "$S"
```

```
{
  "type": "compaction",
  "id": "c4454a86",
  "parentId": "20c3ada5",
  "timestamp": "2026-11-01T10:16:37.901Z",
  "summary": "No prior history.\n\n---\n\n**Turn Context (split turn):**\n\n## Original Request\n- 依次读 a.txt b.txt c.txt\n\n## Progress So Far\n- 前缀摘要 #1\n\n## Context Needed to Continue\n- (none)\n\n<read-files>\na.txt\nb.txt\n</read-files>",
  "firstKeptEntryId": "4ca81392",
  "tokensBefore": 2737,
  "details": {
    "readFiles": [
      "a.txt",
      "b.txt"
    ],
    "modifiedFiles": []
  },
  "usage": {
    "input": 1229,
    "output": 29,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 1258,
    "cost": {
      "input": 0,
      "output": 0,
      "cacheRead": 0,
      "cacheWrite": 0,
      "total": 0
    }
  },
  "fromHook": false
}
systemMessage: role=system sections=[preamble,tools,rules,docs,cwd] toolsAdded=[read,bash,edit,write] timestamp=1793528197901
```

`systemMessage` 字段是一份完整的系统提示词和工具清单，有几千字符，脚本只打印了它的结构。§3.4 逐个解释这些字段。

---

## 3. 数据结构

压缩的代码是一条流水线：会话条目 → token 估算 → 切点 → 准备结果 → 摘要结果 → 新的会话条目 → 新的 Agent 消息。下面按流水线的顺序认识这些类型。本节的真实实例都取自 §2.2 那次运行。

### 3.1 CompactionSettings 和 ContextUsageEstimate：要不要压缩

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:120-130`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L120-L130)

```ts
export interface CompactionSettings {
	enabled: boolean;
	reserveTokens: number;
	keepRecentTokens: number;
}

export const DEFAULT_COMPACTION_SETTINGS: CompactionSettings = {
	enabled: true,
	reserveTokens: 16384,
	keepRecentTokens: 20000,
};
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否自动压缩。关掉后 `/compact` 手动压缩仍可用 |
| `reserveTokens` | 16384 | 给新回复留的空间。上下文超过 `contextWindow - reserveTokens` 就压缩 |
| `keepRecentTokens` | 20000 | 压缩后原样保留的最近消息的 token 预算 |

设置来自 `settings.json` 的 `compaction` 字段，由 `SettingsManager.getCompactionSettings(model)`（`packages/coding-agent/src/core/settings-manager.ts:883`）读出，可以按模型覆盖（→ M6）。§2.2 的实例是 `{"enabled":true,"reserveTokens":500,"keepRecentTokens":600}`，见 §3.3 实例里的 `settings` 字段。

上下文大小的估算结果是 `ContextUsageEstimate`：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:177-182`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L177-L182)

```ts
export interface ContextUsageEstimate {
	tokens: number;
	usageTokens: number;
	trailingTokens: number;
	lastUsageIndex: number | null;
}
```

| 字段 | 说明 |
|---|---|
| `tokens` | 估算的上下文总 token 数，等于 `usageTokens + trailingTokens` |
| `usageTokens` | 最后一条有效 assistant 消息的 `usage` 给出的 token 数，这是 provider 报告的真实值 |
| `trailingTokens` | 那条 assistant 消息之后的消息，用"字符数 / 4"估算的 token 数 |
| `lastUsageIndex` | 那条 assistant 消息在消息数组里的下标；没有可用的 usage 时为 `null`，这时 `tokens` 全部是估算值 |

### 3.2 CutPointResult：从哪里切

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:421-428`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L421-L428)

```ts
export interface CutPointResult {
	/** Index of first entry to keep */
	firstKeptEntryIndex: number;
	/** Index of user message that starts the turn being split, or -1 if not splitting */
	turnStartIndex: number;
	/** Whether this cut splits a turn (cut point is not a user message) */
	isSplitTurn: boolean;
}
```

| 字段 | 说明 |
|---|---|
| `firstKeptEntryIndex` | 保留区第一个条目的下标。它之前的条目被摘要代替 |
| `turnStartIndex` | 切点落在一个"轮次"中间时，这个轮次开头那条 user 消息的下标；否则为 -1 |
| `isSplitTurn` | 切点是否把一个轮次切成了两半 |

这里的"轮次"（turn）和 M2 的"轮"不是一回事。M2 的一轮是"请求一次模型、执行这次回复里的工具"。这里的轮次是从一条 user 消息开始、到下一条 user 消息之前的所有消息，`docs/compaction.md` 叫它 user-message span。一个轮次可以包含 M2 的很多轮。本文把它叫做"轮次"，和 M2 的"轮"区分开。

### 3.3 CompactionPreparation：准备好的压缩

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:759-775`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L759-L775)

```ts
export interface CompactionPreparation {
	/** UUID of first entry to keep */
	firstKeptEntryId: string;
	/** Messages that will be summarized and discarded */
	messagesToSummarize: AgentMessage[];
	/** Messages that will be turned into turn prefix summary (if splitting) */
	turnPrefixMessages: AgentMessage[];
	/** Whether this is a split turn (cut point in middle of turn) */
	isSplitTurn: boolean;
	tokensBefore: number;
	/** Summary from previous compaction, for iterative update */
	previousSummary?: string;
	/** File operations extracted from messagesToSummarize */
	fileOps: FileOperations;
	/** Compaction settions from settings.jsonl	*/
	settings: CompactionSettings;
}
```

| 字段 | 说明 |
|---|---|
| `firstKeptEntryId` | 保留区第一个条目的 id，由 `CutPointResult.firstKeptEntryIndex` 换算 |
| `messagesToSummarize` | 要写成"历史摘要"的 Agent 消息：上次压缩的保留区起点（或会话开头）到切点之前的完整轮次 |
| `turnPrefixMessages` | 切点切开一个轮次时，这个轮次被切掉的前半段；否则为空数组 |
| `isSplitTurn` | 同 `CutPointResult.isSplitTurn` |
| `tokensBefore` | 压缩前的上下文估算值，来自 `ContextUsageEstimate.tokens` |
| `previousSummary` | 上一次压缩的摘要文本，第一次压缩时为 `undefined` |
| `fileOps` | 要丢弃的消息里读过、写过、改过的文件，加上上一次压缩记下的文件 |
| `settings` | 本次使用的 `CompactionSettings` |

`prepareCompaction` 的结果也是扩展事件 `session_before_compact` 的 `preparation` 字段（→ M7），扩展可以据此自己写摘要。注释里的 "settions from settings.jsonl" 是原文的笔误，设置文件是 `settings.json`。

`fileOps` 的类型是 `FileOperations`，三个集合：

源码：[`packages/coding-agent/src/core/compaction/utils.ts:12-16`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/utils.ts#L12-L16)

```ts
export interface FileOperations {
	read: Set<string>;
	written: Set<string>;
	edited: Set<string>;
}
```

**真实实例。** [`lab/m5-direct.ts`](lab/m5-direct.ts) 的 `prepare` 场景读入 §2.2 的会话文件，取 `compaction` 条目之前的所有条目（也就是压缩发生时的分支），直接调用本模块的纯函数，打印每一步的数据：

```bash
node --conditions=source --experimental-strip-types m5-direct.ts prepare "$S"
```

```
取第 1 个 compaction 条目之前的 10 个条目，keepRecentTokens=600
  [ 0] 3a0c3305 model_change           estimateTokens=0
  [ 1] 33100886 thinking_level_change  estimateTokens=0
  [ 2] 0118698f system                 estimateTokens=1297
  [ 3] c93fc35f user                   estimateTokens=6
  [ 4] c766cd89 assistant              estimateTokens=7
  [ 5] 948722b6 toolResult             estimateTokens=462
  [ 6] 56c59afe assistant              estimateTokens=7
  [ 7] 0c503438 toolResult             estimateTokens=462
  [ 8] 4ca81392 assistant              estimateTokens=7
  [ 9] 20c3ada5 toolResult             estimateTokens=462
ContextUsageEstimate {"tokens":2737,"usageTokens":2275,"trailingTokens":462,"lastUsageIndex":6}
shouldCompact(2737, 3000, reserve=500) = true
findCutPoint（原始条目） {"firstKeptEntryIndex":8,"turnStartIndex":3,"isSplitTurn":true}
CompactionPreparation {"firstKeptEntryId":"4ca81392","isSplitTurn":true,"tokensBefore":2737,"settings":{"enabled":true,"reserveTokens":500,"keepRecentTokens":600}}
  previousSummary: undefined
  messagesToSummarize (0):
  turnPrefixMessages (5):
    user: "依次读 a.txt b.txt c.txt"
    assistant: "读 a.txt。[toolCall read]"
    toolResult: "[a.txt]\nline 01 of a.txt: lorem ipsum dolor sit 
    assistant: "读 b.txt。[toolCall read]"
    toolResult: "[b.txt]\nline 01 of b.txt: lorem ipsum dolor sit 
  fileOps: read=["a.txt","b.txt"] written=[] edited=[]
```

`[ 0]` 到 `[ 9]` 是压缩时分支上的 10 个会话条目（不含文件头），右边是 §4.3 的 `estimateTokens` 对每条消息的估算。后面依次是 `ContextUsageEstimate`、`shouldCompact` 的结果、`CutPointResult` 和 `CompactionPreparation`。这次压缩的切点落在第 3 个 assistant 条目 `[ 8]`（读 c.txt），它和 `[ 3]` 的 user 消息属于同一个轮次，所以 `isSplitTurn` 为 `true`：`messagesToSummarize` 为空，`turnPrefixMessages` 是 `[ 3]` 到 `[ 7]` 的 5 条消息。

### 3.4 CompactionResult 和 CompactionEntry：摘要结果和它的会话条目

`compact()` 返回 `CompactionResult`：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:104-114`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L104-L114)

```ts
/** Result from compact() - SessionManager adds uuid/parentUuid when saving */
export interface CompactionResult<T = unknown> {
	summary: string;
	firstKeptEntryId: string;
	tokensBefore: number;
	estimatedTokensAfter?: number;
	/** Usage from the LLM call(s) that generated this summary, if available */
	usage?: Usage;
	/** Extension-specific data (e.g., ArtifactIndex, version markers for structured compaction) */
	details?: T;
}
```

AgentSession 把它的字段交给 `SessionManager.appendCompaction()`，变成一个会话条目：

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
| `type`、`id`、`parentId`、`timestamp` | 所有会话条目都有的字段（→ M4）。`parentId` 是压缩时的叶子 |
| `summary` | 摘要文本。默认实现在模型的回复后面追加 `<read-files>`、`<modified-files>` 两段文件清单（§4.7） |
| `firstKeptEntryId` | 保留区第一个条目的 id。重建上下文时，从这里开始原样保留（§4.13） |
| `tokensBefore` | 压缩前的上下文估算值 |
| `details` | 实现自定义的数据。默认实现存 `CompactionDetails`，即 `{ readFiles, modifiedFiles }`（`packages/coding-agent/src/core/compaction/compaction.ts:52`），下次压缩时用来累积文件清单 |
| `usage` | 生成摘要的那次（或两次）模型调用的用量，计入会话的总用量 |
| `fromHook` | `true` 表示摘要由扩展提供。字段名是旧名，为了兼容旧的会话文件保留 |
| `systemMessage` | 压缩时的完整系统提示词和工具清单（§4.12） |

`docs/compaction.md` 里列出的 `CompactionEntry` 结构没有 `systemMessage` 字段，源码里有。

`CompactionResult` 没有 `id`、`parentId`、`timestamp`、`systemMessage`、`fromHook`，这些由 `appendCompaction` 补上；`CompactionEntry` 没有 `estimatedTokensAfter`，它只出现在 `compaction_end` 事件里（§2.2 的 `m5-inspect.mjs tokens` 输出）。

**真实实例**：§2.3 打印的就是这次运行的 `CompactionEntry`。`summary` 以 `No prior history.` 开头，因为这是第一次压缩，而且 `messagesToSummarize` 为空（§4.7）；`details.readFiles` 是 `["a.txt","b.txt"]`，对应被摘要代替的两次 read；`usage.input` 约 1229，是那次压缩请求的大小。

### 3.5 CompactionSummaryMessage：摘要变回 Agent 消息

重建上下文时，`compaction` 条目变成一条 `compactionSummary` 角色的 Agent 消息。这是 M2 §3.1 讲的四种自定义消息之一：

源码：[`packages/coding-agent/src/core/messages.ts:62-67`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/messages.ts#L62-L67)

```ts
export interface CompactionSummaryMessage {
	role: "compactionSummary";
	summary: string;
	tokensBefore: number;
	timestamp: number;
}
```

发给模型前，`convertToLlm` 把它变成一条 user 消息，摘要外面包上固定的前后缀（§4.13）。§2.2 第 5 次请求里那条 "The conversation history before this point was compacted…" 就是它。

### 3.6 类型关系图

```
 会话条目 SessionEntry[]（当前分支）                        [M4]
        │ buildSessionProjection()                         → M4
        ▼
 ProjectedSessionEntry[] ──estimateProjectedContextTokens()──▶ ContextUsageEstimate
        │                                                       │ .tokens
        │                       CompactionSettings ─────────────┤
        │                         .keepRecentTokens             ▼
        │                              │                 shouldCompact() → true
        ▼                              ▼
 findCutPoint / findProjectedCutPoint() ──▶ CutPointResult
        │
        ▼ prepareCompaction()
 CompactionPreparation
   messagesToSummarize ─┐ turnPrefixMessages ─┐  previousSummary   fileOps: FileOperations
                        ▼                     ▼        │                  │
         generateSummary()     generateTurnPrefixSummary()                │
                        └──────────┬──────────┘                           │
                                   ▼ compact()  拼接文本 + 文件清单 ◀──────┘
                           CompactionResult
                                   │ SessionManager.appendCompaction()    [M4]
                                   ▼   补 id / parentId / systemMessage
                           CompactionEntry（新的会话条目）
                                   │ buildContextEntries() / sessionEntryToContextMessages()
                                   ▼
               [systemMessage, CompactionSummaryMessage, 保留区的消息…]  Agent 消息
                                   │ convertToLlm()                       [M8]
                                   ▼
               [system, user("…compacted into the following summary…"), …]  LLM 消息
```

---

## 4. 从入口读起

调用树如下。缩进表示调用关系，右边是小节号。标 M8 的函数属于 AgentSession，这里只读和压缩有关的几行。

```
AgentSession（M8）的三个检查点                              §4.1
├─ prepareNextTurn → _compactBeforeNextAssistantResponse()
├─ _prepareRun / _handlePostAgentRun → _checkCompaction()
└─ 都会调用 shouldCompact()                                  §4.1 ← 入口
    └─ 参数来自 estimateProjectedContextTokens()
        / estimateContextTokens()                            §4.2
            └─ estimateTokens()                              §4.3
_runAutoCompaction()（M8）                                   §4.4
├─ prepareCompaction()                                       §4.4
│   ├─ buildSessionProjection()                              → M4
│   ├─ findProjectedCutPoint() / findCutPoint()              §4.5
│   └─ extractFileOperations() / extractFileOpsFromMessage() §4.6
├─ compact()                                                 §4.7
│   ├─ generateSummary() / generateSummaryWithUsage()        §4.8
│   │   ├─ serializeConversation()                           §4.9
│   │   └─ completeSummarization() → streamFn                §4.8 → M1
│   ├─ generateTurnPrefixSummary()                           §4.10
│   └─ computeFileLists() / formatFileOperations()           §4.6
├─ 落盘和刷新上下文                                          §4.11
│   ├─ SessionManager.appendCompaction()                     §4.12
│   └─ buildContextEntries() / convertToLlm()                §4.13
手动压缩 AgentSession.compact()                              §4.14
分支摘要 branch-summarization.ts                             §4.15
```

### 4.1 shouldCompact() 和它的三个调用点

**调用位置**：AgentSession 在三个时刻检查是否需要压缩。

1. 轮与轮之间：M2 的 `runLoop` 在第 2 轮起调用 `prepareNextTurn` hook（`packages/agent/src/agent-loop.ts:195`）。AgentSession 在 `_installAgentNextTurnRefresh` 里把它接到 `_compactBeforeNextAssistantResponse`（`packages/coding-agent/src/core/agent-session.ts:921`）。§2.2 的压缩就发生在这里。
2. 一次运行结束后：`_handlePostAgentRun()` 调用 `_checkCompaction(message)`（`packages/coding-agent/src/core/agent-session.ts:2029`），`message` 是最后一条 assistant 消息。
3. 发新 prompt 之前：`_prepareRun()` 调用 `_checkCompaction(lastAssistant, false)`（`packages/coding-agent/src/core/agent-session.ts:1952`）。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:264-270`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L264-L270)

```ts
/**
 * Check if compaction should trigger based on context usage.
 */
export function shouldCompact(contextTokens: number, contextWindow: number, settings: CompactionSettings): boolean {
	if (!settings.enabled) return false;
	return contextTokens > contextWindow - settings.reserveTokens;
}
```

第 1 个调用点：

源码：[`packages/coding-agent/src/core/agent-session.ts:782-801`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L782-L801)

```ts
	/** Whether `projection`, the current session projection, exceeds the compaction threshold of `model`. */
	private _exceedsCompactionThreshold(model: Model<any>, projection: SessionProjection): boolean {
		if (model.contextWindow <= 0) return false;
		return shouldCompact(
			estimateProjectedContextTokens(projection, this.sessionManager.getBranch()).tokens,
			model.contextWindow,
			this.settingsManager.getCompactionSettings(this.model),
		);
	}

	private async _compactBeforeNextAssistantResponse(context: AgentContext): Promise<AgentContext> {
		const projection = this.sessionManager.buildSessionProjection();
		// A virtual selection is checked in prepareRequest, against the model the request is routed to.
		const model = this.model;
		if (!model || isVirtualModel(model) || !this._exceedsCompactionThreshold(model, projection)) {
			return { ...context, messages: projection.messages };
		}
		await this._runAutoCompaction("threshold", false);
		return { ...context, messages: this.sessionManager.buildSessionProjection().messages };
	}
```

第 2、3 个调用点在 `_checkCompaction` 的最后一段（前面一大段处理上下文溢出错误和输出被截断的恢复，属于 M8）：

源码：[`packages/coding-agent/src/core/agent-session.ts:3130-3166`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3130-L3166)

```ts
		// Case 3: threshold compaction without retry.
		// For error messages or all-zero usage messages, estimate from the last valid response.
		// This ensures sessions that hit persistent API errors (e.g. 529) or malformed zero-usage
		// responses can still compact and do not reset context accounting.
		let contextTokens: number;
		const projection = currentProjection;
		const hasContextEdits = projection.entries.some((entry) => entry.sourceEntry.type === "context_edit");
		const directContextTokens = assistantMessage.usage ? calculateContextTokens(assistantMessage.usage) : 0;
		if (hasContextEdits) {
			contextTokens = estimateProjectedContextTokens(projection, branch).tokens;
		} else if (assistantMessage.stopReason === "error" || directContextTokens === 0) {
			const messages = this.agent.state.messages;
			const estimate = estimateContextTokens(messages);
			// Without provider usage, estimate.tokens is the pure message-size estimate.
			// Only usage-backed estimates need the stale pre-compaction check.
			if (estimate.lastUsageIndex !== null) {
				// Verify the usage source is post-compaction. Kept pre-compaction messages
				// have stale usage reflecting the old (larger) context and would falsely
				// trigger compaction right after one just finished.
				const usageMsg = messages[estimate.lastUsageIndex];
				if (
					compactionEntry &&
					usageMsg.role === "assistant" &&
					(usageMsg as AssistantMessage).timestamp <= new Date(compactionEntry.timestamp).getTime()
				) {
					return false;
				}
			}
			contextTokens = estimate.tokens;
		} else {
			contextTokens = directContextTokens;
		}
		if (shouldCompact(contextTokens, contextWindow, settings)) {
			return await this._runAutoCompaction("threshold", false);
		}
		return false;
	}
```

**讲解**

`shouldCompact` 只有一个比较：`contextTokens > contextWindow - reserveTokens`。复杂的是第一个参数从哪里来。三个调用点用了两种来源：

- 轮间检查（`_exceedsCompactionThreshold`）用 `estimateProjectedContextTokens`：从会话文件重建的上下文出发估算（§4.2）。这时最后一条 assistant 消息之后还有刚追加的工具结果，`usage` 不包含它们，必须加上估算值。
- 运行结束后的检查（`_checkCompaction`）优先用最后一条 assistant 消息自己的 `usage`（`directContextTokens`）。这条消息就是最后一条消息，它的 `usage` 已经覆盖了完整的上下文。只有在三种情况下才改用估算：投影里有 `context_edit`（usage 不再和上下文对应），消息是错误消息，或 `usage` 全为 0。

`_compactBeforeNextAssistantResponse` 不管是否压缩，都返回一个 `messages` 换成会话投影的 context。所以轮间这一步同时是"用会话文件刷新上下文"的地方，和 M8 的 `prepareRequest` 投影是同一个思路：会话文件是上下文的唯一来源。

`contextWindow <= 0` 表示模型没有声明窗口大小，这时永远不压缩。虚拟模型（一个名字背后按规则路由到不同真实模型）在 `prepareRequest` 里按路由后的模型检查（`packages/coding-agent/src/core/agent-session.ts:854`），这里跳过。

`_checkCompaction` 里那句"消息早于最近一次压缩就跳过"（`assistantIsFromBeforeCompaction`，`packages/coding-agent/src/core/agent-session.ts:3052`）也很重要：压缩刚完成时，最后一条 assistant 消息的 `usage` 还是压缩前的大数字，如果不跳过，会立刻再压缩一次。

**往哪走**：§4.2 看 token 数怎么估算。

### 4.2 estimateContextTokens() 和 estimateProjectedContextTokens()

**调用位置**：`_exceedsCompactionThreshold()` → `estimateProjectedContextTokens()`；`_checkCompaction()` → `estimateContextTokens()`；`prepareCompaction()` → `estimateProjectedContextTokens()` 算 `tokensBefore`。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:192-224`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L192-L224)

```ts
/**
 * Estimate context tokens from messages, using the last assistant usage when available.
 * If there are messages after the last usage, estimate their tokens with estimateTokens.
 */
export function estimateContextTokens(messages: AgentMessage[]): ContextUsageEstimate {
	const usageInfo = getLastAssistantUsageInfo(messages);

	if (!usageInfo) {
		let estimated = 0;
		for (const message of messages) {
			estimated += estimateTokens(message);
		}
		return {
			tokens: estimated,
			usageTokens: 0,
			trailingTokens: estimated,
			lastUsageIndex: null,
		};
	}

	const usageTokens = calculateContextTokens(usageInfo.usage);
	let trailingTokens = 0;
	for (let i = usageInfo.index + 1; i < messages.length; i++) {
		trailingTokens += estimateTokens(messages[i]);
	}

	return {
		tokens: usageTokens + trailingTokens,
		usageTokens,
		trailingTokens,
		lastUsageIndex: usageInfo.index,
	};
}
```

`getLastAssistantUsageInfo`（`packages/coding-agent/src/core/compaction/compaction.ts:184`）从后往前找第一条"有效"的 assistant 消息。有效的条件在 `getAssistantUsage`（`packages/coding-agent/src/core/compaction/compaction.ts:148`）里：`stopReason` 不是 `aborted` 或 `error`，而且 `calculateContextTokens(usage) > 0`。`calculateContextTokens`（`packages/coding-agent/src/core/compaction/compaction.ts:140`）优先用 `usage.totalTokens`，没有时把 `input + output + cacheRead + cacheWrite` 加起来。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:226-262`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L226-L262)

```ts
/** Estimate projected context without trusting usage captured before a later edit or compaction. */
export function estimateProjectedContextTokens(
	projection: SessionProjection,
	branchEntries: SessionEntry[],
): ContextUsageEstimate {
	const estimate = estimateContextTokens(projection.messages);
	if (estimate.lastUsageIndex !== null) {
		let projectedMessageIndex = 0;
		let usageEntryId: string | undefined;
		for (const entry of projection.entries) {
			const nextMessageIndex = projectedMessageIndex + entry.messages.length;
			if (estimate.lastUsageIndex < nextMessageIndex) {
				usageEntryId = entry.sourceEntry.id;
				break;
			}
			projectedMessageIndex = nextMessageIndex;
		}

		const usageEntryIndex = usageEntryId ? branchEntries.findIndex((entry) => entry.id === usageEntryId) : -1;
		let latestInvalidatingEntryIndex = -1;
		for (let i = branchEntries.length - 1; i >= 0; i--) {
			const entry = branchEntries[i];
			if (entry.type === "context_edit" || entry.type === "compaction") {
				latestInvalidatingEntryIndex = i;
				break;
			}
		}
		if (usageEntryIndex > latestInvalidatingEntryIndex) return estimate;
	}

	const currentSystem = getCurrentSystemMessage(projection.messages);
	let tokens = currentSystem ? estimateTokens(currentSystem) : 0;
	for (const message of projection.messages) {
		if (message.role !== "system") tokens += estimateTokens(message);
	}
	return { tokens, usageTokens: 0, trailingTokens: tokens, lastUsageIndex: null };
}
```

**讲解**

`estimateContextTokens` 的思路是"真实值 + 增量估算"：

- 最后一条有效 assistant 消息的 `usage.totalTokens` 是 provider 算的，包含了那次请求的全部输入和这次输出。这是最准的数字。
- 它之后的消息（通常是工具结果）还没发给过模型，只能估算。

§2.2 的实例 `{"tokens":2737,"usageTokens":2275,"trailingTokens":462,"lastUsageIndex":6}` 就是这样算出来的：下标 6 是读 c.txt 的 assistant 消息（投影的消息数组里不含 `model_change` 这类没有消息的条目，所以下标和条目编号不同），它之后只有一个 462 token 的工具结果。

`estimateProjectedContextTokens` 在此基础上多一道检查：usage 是否还可信。usage 是在某次请求时记下的，之后如果出现了 `context_edit`（改写或删掉了上下文里的某条消息）或 `compaction`，那次 usage 对应的上下文已经不存在了。函数先找出 usage 所在的会话条目，再找分支上最后一个 `context_edit` 或 `compaction` 条目；usage 条目在它之后才信任 usage。否则放弃 usage，全部用估算：当前系统消息估一次，加上每条非 system 消息的估算。

只估"当前系统消息"是因为 transcript 里可能有多条 system 消息（M2 §4.4 的工具清单补丁），它们按顺序合并成一份当前状态，发给模型的只是这一份（→ M1 `getCurrentSystemMessage`）。

**往哪走**：§4.3 `estimateTokens`。

### 4.3 estimateTokens()

**调用位置**：§4.2 的两个估算函数、§4.5 的切点查找、§4.15 的分支摘要都用它估算单条消息。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:294-349`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L294-L349)

```ts
/**
 * Estimate token count for a message using chars/4 heuristic.
 * This is conservative (overestimates tokens).
 */
export function estimateTokens(message: AgentMessage): number {
	let chars = 0;

	switch (message.role) {
		case "system": {
			const system = message as SystemMessage;
			chars = estimateTextAndImageContentChars(system.content);
			if (system.sections) {
				for (const section of Object.values(system.sections)) {
					if (section) chars += section.length;
				}
			}
			if (system.toolsAdded) chars += JSON.stringify(system.toolsAdded).length;
			return Math.ceil(chars / 4);
		}
		case "user": {
			chars = estimateTextAndImageContentChars(
				(message as { content: string | Array<{ type: string; text?: string }> }).content,
			);
			return Math.ceil(chars / 4);
		}
		case "assistant": {
			const assistant = message as AssistantMessage;
			for (const block of assistant.content) {
				if (block.type === "text") {
					chars += block.text.length;
				} else if (block.type === "thinking") {
					chars += block.thinking.length;
				} else if (block.type === "toolCall") {
					chars += block.name.length + JSON.stringify(block.arguments).length;
				}
			}
			return Math.ceil(chars / 4);
		}
		case "custom":
		case "toolResult": {
			chars = estimateTextAndImageContentChars(message.content);
			return Math.ceil(chars / 4);
		}
		case "bashExecution": {
			chars = message.command.length + message.output.length;
			return Math.ceil(chars / 4);
		}
		case "branchSummary":
		case "compactionSummary": {
			chars = message.summary.length;
			return Math.ceil(chars / 4);
		}
	}

	return 0;
}
```

**讲解**

所有 role 都按"字符数 / 4，向上取整"估算。注释说这个估算偏保守（偏大）：英文平均一个 token 约 4 个字符，中文一个字符往往就是一个 token，所以对中文其实偏小。压缩只需要一个量级正确的数字，不需要精确。

几个细节：

- `system` 消息算上了 `sections` 的每一段和 `toolsAdded` 的 JSON。§3.3 实例里 system 条目估算为约 1300 token，主要是工具定义的 JSON schema。
- 图片按固定的 4800 字符（1200 token）算（`ESTIMATED_IMAGE_CHARS`，`packages/coding-agent/src/core/compaction/compaction.ts:276`）。
- `assistant` 的 `thinking` 内容也计入，因为它确实占上下文。
- 四种自定义消息各有算法：`bashExecution` 算命令和输出，`branchSummary`、`compactionSummary` 算摘要文本。这里没有算 `convertToLlm` 加上的前后缀，差几十个字符，无关紧要。

§2.2 里读一个 1848 字符的文件，工具结果估算为 ⌈1848 / 4⌉ = 462 token，和 `m5-inspect.mjs` 打印的一致。

**往哪走**：阈值判断为真之后，AgentSession 进入 `_runAutoCompaction`，§4.4。

### 4.4 _runAutoCompaction() 前半段和 prepareCompaction()

**调用位置**：`_compactBeforeNextAssistantResponse()` 或 `_checkCompaction()` → `_runAutoCompaction(reason, willRetry)`（M8）→ `prepareCompaction(pathEntries, settings)`。

源码：[`packages/coding-agent/src/core/agent-session.ts:3178-3201`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3178-L3201)

```ts
	private async _runAutoCompaction(reason: "overflow" | "threshold", willRetry: boolean): Promise<boolean> {
		const model = this.model;
		const settings = this.settingsManager.getCompactionSettings(model);
		let abortController: AbortController | undefined;
		let started = false;
		let fromExtension = false;
		let cancelledByExtension = false;

		try {
			if (!model) {
				return false;
			}

			const pathEntries = this.sessionManager.getBranch();
			const preparation = prepareCompaction(pathEntries, settings);
			if (!preparation) {
				return false;
			}

			abortController = new AbortController();
			this._autoCompactionAbortController = abortController;
			started = true;
			this._emit({ type: "compaction_start", reason });
			abortController.signal.throwIfAborted();
```

`pathEntries` 是 `getBranch()` 的结果：从根到当前叶子的所有会话条目，按时间顺序（→ M4）。`prepareCompaction` 返回 `undefined` 表示没什么可压缩的，这时连 `compaction_start` 事件都不发。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:859-901`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L859-L901)

```ts
export function prepareCompaction(
	pathEntries: SessionEntry[],
	settings: CompactionSettings,
): CompactionPreparation | undefined {
	if (pathEntries.length > 0 && pathEntries[pathEntries.length - 1].type === "compaction") {
		return undefined;
	}

	const projection = buildSessionProjection(pathEntries);
	const projectedEntries = projection.entries;
	const sourceEntries = projectedEntries.map((entry) => entry.sourceEntry);
	// The newest compaction is projected first. Older compaction entries can still
	// occur in its retained raw range, but their projected contribution is empty.
	const prevCompactionIndex = projectedEntries.findIndex(
		(entry) => entry.sourceEntry.type === "compaction" && entry.messages.length > 0,
	);

	let previousSummary: string | undefined;
	let boundaryStart = 0;
	if (prevCompactionIndex >= 0) {
		previousSummary = (projectedEntries[prevCompactionIndex].sourceEntry as CompactionEntry).summary;
		// The canonical projection has already selected the previous compaction's retained tail.
		boundaryStart = prevCompactionIndex + 1;
	}
	const boundaryEnd = projectedEntries.length;
	const tokensBefore = estimateProjectedContextTokens(projection, pathEntries).tokens;
	const cutPoint = findProjectedCutPoint(projectedEntries, boundaryStart, boundaryEnd, settings.keepRecentTokens);

	const firstKeptEntry = projectedEntries[cutPoint.firstKeptEntryIndex]?.sourceEntry;
	if (!firstKeptEntry?.id) return undefined;
	const firstKeptEntryId = firstKeptEntry.id;
	const historyEnd = cutPoint.isSplitTurn ? cutPoint.turnStartIndex : cutPoint.firstKeptEntryIndex;

	const messagesToSummarize = projectedEntries
		.slice(boundaryStart, historyEnd)
		.flatMap(getMessagesFromProjectedEntryForCompaction);
	const turnPrefixMessages = cutPoint.isSplitTurn
		? projectedEntries
				.slice(cutPoint.turnStartIndex, cutPoint.firstKeptEntryIndex)
				.flatMap(getMessagesFromProjectedEntryForCompaction)
		: [];

	if (messagesToSummarize.length === 0 && turnPrefixMessages.length === 0) return undefined;
```

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:903-923`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L903-L923)

```ts
	// Extract file operations from edited model-visible messages and the previous compaction.
	const fileOps = extractFileOperations(messagesToSummarize, sourceEntries, prevCompactionIndex);

	// Also extract file ops from turn prefix if splitting
	if (cutPoint.isSplitTurn) {
		for (const msg of turnPrefixMessages) {
			extractFileOpsFromMessage(msg, fileOps);
		}
	}

	return {
		firstKeptEntryId,
		messagesToSummarize,
		turnPrefixMessages,
		isSplitTurn: cutPoint.isSplitTurn,
		tokensBefore,
		previousSummary,
		fileOps,
		settings,
	};
}
```

**讲解**

`prepareCompaction` 不调用模型，它只决定"摘要哪些、保留哪些"。步骤如下：

1. 分支的最后一个条目已经是 `compaction`，说明刚压缩过，返回 `undefined`。手动 `/compact` 会把它报告成 "Already compacted"（`packages/coding-agent/src/core/agent-session.ts:2870`）。
2. 用 `buildSessionProjection(pathEntries)` 把条目投影成 `ProjectedSessionEntry[]`（→ M4）。每个投影条目是 `{ sourceEntry, messages }`：原始条目，加上它在应用 `context_edit` 之后贡献给上下文的消息。投影已经应用了上一次压缩，所以如果之前压缩过，投影的第一个条目就是那个 `compaction` 条目，后面紧跟它的保留区（§4.13）。
3. 找上一次压缩：投影里第一个"`compaction` 且有消息"的条目。找到了就记下它的摘要作为 `previousSummary`，本次的扫描从它的下一个条目开始（`boundaryStart`）。这意味着上次保留下来的消息这次也会参与摘要，它们不会永远原样保留。
4. 用 §4.2 的方法算 `tokensBefore`。
5. 在 `[boundaryStart, boundaryEnd)` 里找切点（§4.5）。
6. 切出两段消息。不切开轮次时，`messagesToSummarize` 是 `[boundaryStart, 切点)`；切开轮次时，`messagesToSummarize` 是 `[boundaryStart, 轮次开头)`，`turnPrefixMessages` 是 `[轮次开头, 切点)`。两段都为空就返回 `undefined`。
7. 从两段消息和上一次压缩的 `details` 里提取文件操作（§4.6）。

第 6 步取消息时用 `getMessagesFromProjectedEntryForCompaction`：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:94-102`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L94-L102)

```ts
/**
 * Extract AgentMessage from an entry if it produces one.
 * Returns undefined for entries that don't contribute to LLM context.
 */
function getMessagesFromProjectedEntryForCompaction(entry: ProjectedSessionEntry): AgentMessage[] {
	if (entry.sourceEntry.type === "compaction") return [];
	// System messages are prompt state, not conversation; the compaction entry carries their replay.
	return entry.messages.filter((message) => message.role !== "system");
}
```

`compaction` 条目的消息（上一次的摘要）不放进要摘要的消息，因为它已经通过 `previousSummary` 单独传入。`system` 消息也去掉：系统提示词是状态，不是对话，新的 `compaction` 条目会把当前的系统消息整份存进 `systemMessage`（§4.12）。

**往哪走**：§4.5 切点。

### 4.5 findCutPoint()：不在工具结果处切开

**调用位置**：`prepareCompaction()` 调用的是 `findProjectedCutPoint()`，它是 `findCutPoint()` 在投影条目上的版本。`findCutPoint()` 本身是导出的公共函数（`packages/coding-agent/src/index.ts:41`），算法相同。先读 `findCutPoint`，再看投影版本多了什么。

先看两条规则，哪些消息可以作为切点，哪些消息开始一个轮次：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:351-379`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L351-L379)

```ts
function isCutPointMessage(message: AgentMessage): boolean {
	switch (message.role) {
		case "user":
		case "assistant":
		case "bashExecution":
		case "custom":
		case "branchSummary":
		case "compactionSummary":
			return true;
		case "toolResult":
			return false;
	}
	return false;
}

function isTurnStartMessage(message: AgentMessage): boolean {
	switch (message.role) {
		case "user":
		case "bashExecution":
		case "custom":
		case "branchSummary":
		case "compactionSummary":
			return true;
		case "assistant":
		case "toolResult":
			return false;
	}
	return false;
}
```

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:394-406`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L394-L406)

```ts
function findValidCutPoints(entries: SessionEntry[], startIndex: number, endIndex: number): number[] {
	const cutPoints: number[] = [];
	for (let i = startIndex; i < endIndex; i++) {
		const entry = entries[i];
		if (entry.type === "compaction") {
			continue;
		}
		if (sessionEntryToContextMessages(entry).some(isCutPointMessage)) {
			cutPoints.push(i);
		}
	}
	return cutPoints;
}
```

主体：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:446-501`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L446-L501)

```ts
export function findCutPoint(
	entries: SessionEntry[],
	startIndex: number,
	endIndex: number,
	keepRecentTokens: number,
): CutPointResult {
	const cutPoints = findValidCutPoints(entries, startIndex, endIndex);

	if (cutPoints.length === 0) {
		return { firstKeptEntryIndex: startIndex, turnStartIndex: -1, isSplitTurn: false };
	}

	// Walk backwards from newest, accumulating estimated message sizes
	let accumulatedTokens = 0;
	let cutIndex = cutPoints[0]; // Default: keep from first message (not header)

	for (let i = endIndex - 1; i >= startIndex; i--) {
		const entry = entries[i];
		const messageTokens = sessionEntryToContextMessages(entry).reduce(
			(sum, message) => sum + estimateTokens(message),
			0,
		);
		if (messageTokens === 0) continue;
		accumulatedTokens += messageTokens;

		// Check if we've exceeded the budget
		if (accumulatedTokens >= keepRecentTokens) {
			// Prefer the closest valid cut point at or after this entry. If trailing
			// tool results exceed the budget by themselves, keep their preceding
			// assistant tool call instead of falling back to the first message.
			cutIndex = cutPoints.find((candidate) => candidate >= i) ?? cutPoints[cutPoints.length - 1];
			break;
		}
	}

	// Scan backwards from cutIndex to include adjacent metadata entries that do not affect context.
	while (cutIndex > startIndex) {
		const prevEntry = entries[cutIndex - 1];
		// Stop at compaction boundaries or context-visible entries.
		if (prevEntry.type === "compaction" || sessionEntryToContextMessages(prevEntry).length > 0) {
			break;
		}
		cutIndex--;
	}

	// Determine if this is a split turn
	const cutEntry = entries[cutIndex];
	const startsTurn = isTurnStartEntry(cutEntry);
	const turnStartIndex = startsTurn ? -1 : findTurnStartIndex(entries, cutIndex, startIndex);

	return {
		firstKeptEntryIndex: cutIndex,
		turnStartIndex,
		isSplitTurn: !startsTurn && turnStartIndex !== -1,
	};
}
```

**讲解**

切点是保留区的第一个条目。可以作为切点的是 user、assistant 和四种自定义消息；`toolResult` 不行，`system` 也不行（`isCutPointMessage` 对它返回 `false`）。

为什么不能在 `toolResult` 处切：assistant 消息里的 `toolCall` 和随后的 `toolResult` 用 `toolCallId` 配对。各家 API 都要求每个工具结果前面有对应的工具调用。如果切点落在 toolResult 上，保留区就以一个"没有调用的结果"开头，请求会被 provider 拒绝。反过来，切在 assistant 上是安全的：它的工具结果都在它后面，会一起保留。

算法分四步：

1. `findValidCutPoints` 列出区间内所有可以作为切点的下标。没有就保留全部。
2. 从最新的条目往回累加每个条目的估算 token。累计值第一次达到 `keepRecentTokens` 时，停在当前条目 `i`，取"下标 ≥ i 的第一个合法切点"。当前条目是 toolResult 时，这会把切点往后挪到下一个 assistant 或 user，保留区因此略小于预算。如果 `i` 之后没有合法切点（最后一串工具结果本身就超出预算），取最后一个合法切点，即这串工具结果前面的 assistant。一直累加不到预算，切点保持默认值 `cutPoints[0]`：保留区间内的全部消息。
3. 切点前面紧挨着的、不产生消息的条目（`model_change`、`thinking_level_change`、`label` 等）一起划进保留区，碰到 `compaction` 或有消息的条目就停。这些元数据条目跟着它们后面的消息走。
4. 判断是否切开了轮次：切点本身是轮次开头（user 等）就没有切开；否则往前找最近的轮次开头，找到了就是切开（`isSplitTurn = true`）。注意 `isTurnStartMessage` 不把 assistant 和 toolResult 算作轮次开头，`compaction` 条目也不算。

用 §3.3 的实例走一遍，`keepRecentTokens = 600`：

| 下标 | 条目 | 估算 token | 累计 |
|---|---|---|---|
| 9 | toolResult（c.txt） | 462 | 462 |
| 8 | assistant（读 c.txt） | 7 | 469 |
| 7 | toolResult（b.txt） | 462 | 931 ≥ 600，停在 7 |

下标 7 是 toolResult，往后找第一个合法切点，得到 8。8 是 assistant，不是轮次开头；往前找到 3 的 user 消息，所以 `{"firstKeptEntryIndex":8,"turnStartIndex":3,"isSplitTurn":true}`。保留区是 8、9 两个条目，约 469 token。

投影版本 `findProjectedCutPoint`（`packages/coding-agent/src/core/compaction/compaction.ts:789`）的主体和上面一样，只是读 `entries[i].messages`（应用了 `context_edit` 的消息）而不是重新从原始条目算。它多了一段处理"恢复尝试"的逻辑：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:818-843`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L818-L843)

```ts
	// A recovery attempt and its omission edits are context-invisible after the last
	// visible input. Advance only for a closed suffix containing an omitted assistant
	// attempt; arbitrary metadata must not move the cut past unsent input.
	const suffix = entries.slice(cutIndex + 1, endIndex);
	const isIntrinsicallyVisible = (entry: ProjectedSessionEntry): boolean =>
		entry.sourceEntry.type !== "context_edit" && sessionEntryToContextMessages(entry.sourceEntry).length > 0;
	const isOmitted = (entry: ProjectedSessionEntry): boolean =>
		isIntrinsicallyVisible(entry) && entry.messages.length === 0;
	const omittedSuffixIds = new Set(suffix.filter(isOmitted).map((entry) => entry.sourceEntry.id));
	const hasExternalReplacement = suffix.some(
		(entry) =>
			entry.sourceEntry.type === "context_edit" &&
			entry.sourceEntry.replacement !== null &&
			!omittedSuffixIds.has(entry.sourceEntry.targetId),
	);
	const isRecoveryOmissionSuffix =
		exceededBudget &&
		!hasExternalReplacement &&
		suffix.some(
			(entry) =>
				entry.sourceEntry.type === "message" && entry.sourceEntry.message.role === "assistant" && isOmitted(entry),
		) &&
		suffix.every(
			(entry) => entry.sourceEntry.type !== "compaction" && (!isIntrinsicallyVisible(entry) || isOmitted(entry)),
		);
	if (isRecoveryOmissionSuffix) cutIndex++;
```

上下文溢出时，AgentSession 会先用 `context_edit` 把失败的那次 assistant 回复从上下文里删掉（`_omitRecoveryAttempt`，属于 M8），再压缩、重试。这时切点后面是一串"看不见"的条目：被删的 assistant 和删除它的 `context_edit`。如果切点停在这串条目前面，最后那条用户输入会原样保留，压缩后可能仍然放不下。这段代码在满足严格条件时把切点往后挪一个：后缀里有被删掉的 assistant，后缀里每个条目要么本来就不产生消息、要么被删掉了，并且没有改写后缀之外内容的 `context_edit`。`docs/compaction.md` 的 "Cut Point Rules" 一节描述的就是这个规则。普通的阈值压缩不会进入这个分支。

**往哪走**：§4.6 文件操作。

### 4.6 extractFileOperations() 和文件清单

**调用位置**：`prepareCompaction()` → `extractFileOperations(messagesToSummarize, sourceEntries, prevCompactionIndex)`，切开轮次时再对 `turnPrefixMessages` 逐条调用 `extractFileOpsFromMessage()`；`compact()` → `computeFileLists()` → `formatFileOperations()`。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:57-88`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L57-L88)

```ts
/**
 * Extract file operations from messages and previous compaction entries.
 */
function extractFileOperations(
	messages: AgentMessage[],
	entries: SessionEntry[],
	prevCompactionIndex: number,
): FileOperations {
	const fileOps = createFileOps();

	// Collect from previous compaction's details (if pi-generated)
	if (prevCompactionIndex >= 0) {
		const prevCompaction = entries[prevCompactionIndex] as CompactionEntry;
		if (!prevCompaction.fromHook && prevCompaction.details) {
			// fromHook field kept for session file compatibility
			const details = prevCompaction.details as CompactionDetails;
			if (Array.isArray(details.readFiles)) {
				for (const f of details.readFiles) fileOps.read.add(f);
			}
			if (Array.isArray(details.modifiedFiles)) {
				for (const f of details.modifiedFiles) fileOps.edited.add(f);
			}
		}
	}

	// Extract from tool calls in messages
	for (const msg of messages) {
		extractFileOpsFromMessage(msg, fileOps);
	}

	return fileOps;
}
```

源码：[`packages/coding-agent/src/core/compaction/utils.ts:26-61`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/utils.ts#L26-L61)

```ts
/**
 * Extract file operations from tool calls in an assistant message, or from the nested calls
 * recorded on a tool result.
 */
export function extractFileOpsFromMessage(message: AgentMessage, fileOps: FileOperations): void {
	if (message.role === "toolResult") {
		// Calls made from codemode scripts are recorded on the script's result.
		for (const call of message.nestedCalls?.calls ?? []) addFileOp(call.name, call.arguments, fileOps);
		return;
	}
	if (message.role !== "assistant") return;
	if (!("content" in message) || !Array.isArray(message.content)) return;

	for (const block of message.content) {
		if (typeof block !== "object" || block === null) continue;
		if (!("type" in block) || block.type !== "toolCall") continue;
		if (!("arguments" in block) || !("name" in block)) continue;
		addFileOp(block.name, block.arguments as Record<string, unknown> | undefined, fileOps);
	}
}

function addFileOp(toolName: string, args: Record<string, unknown> | undefined, fileOps: FileOperations): void {
	const path = typeof args?.path === "string" ? args.path : undefined;
	if (!path) return;
	switch (toolName) {
		case "read":
			fileOps.read.add(path);
			break;
		case "write":
			fileOps.written.add(path);
			break;
		case "edit":
			fileOps.edited.add(path);
			break;
	}
}
```

源码：[`packages/coding-agent/src/core/compaction/utils.ts:63-87`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/utils.ts#L63-L87)

```ts
/**
 * Compute final file lists from file operations.
 * Returns readFiles (files only read, not modified) and modifiedFiles.
 */
export function computeFileLists(fileOps: FileOperations): { readFiles: string[]; modifiedFiles: string[] } {
	const modified = new Set([...fileOps.edited, ...fileOps.written]);
	const readOnly = [...fileOps.read].filter((f) => !modified.has(f)).sort();
	const modifiedFiles = [...modified].sort();
	return { readFiles: readOnly, modifiedFiles };
}

/**
 * Format file operations as XML tags for summary.
 */
export function formatFileOperations(readFiles: string[], modifiedFiles: string[]): string {
	const sections: string[] = [];
	if (readFiles.length > 0) {
		sections.push(`<read-files>\n${readFiles.join("\n")}\n</read-files>`);
	}
	if (modifiedFiles.length > 0) {
		sections.push(`<modified-files>\n${modifiedFiles.join("\n")}\n</modified-files>`);
	}
	if (sections.length === 0) return "";
	return `\n\n${sections.join("\n\n")}`;
}
```

**讲解**

摘要由模型写，可能漏掉文件路径。所以 pi 不依赖模型，而是从工具调用的参数里机械地提取文件路径，附在摘要后面：

- 只看 `read`、`write`、`edit` 三个内置工具，并且只看 `path` 参数。bash 里的 `cat foo.txt` 不会被记录。
- assistant 消息看 `toolCall` 块。toolResult 消息看 `nestedCalls`：codemode 脚本在一次工具调用里又调用了其他工具，这些嵌套调用记在脚本结果上（→ M3、M10）。
- 上一次压缩的 `details` 里的 `readFiles` 和 `modifiedFiles` 被并入。`fromHook` 为 `true`（扩展写的摘要）时不并入，因为扩展的 `details` 格式由扩展自己决定。这就是"累积文件清单"：每次压缩都继承上一次的清单，文件清单不会因为多次压缩而丢失。
- `computeFileLists` 把 `written` 和 `edited` 合成 `modifiedFiles`；`readFiles` 只保留读过但没改过的文件。两个列表都排序。

上一次的 `modifiedFiles` 并入的是 `edited` 集合。因为最后 `written` 和 `edited` 会合并，放进哪个都一样。

§2.2 的实例里，`fileOps` 是 `read=["a.txt","b.txt"]`，最后的摘要以 `<read-files>\na.txt\nb.txt\n</read-files>` 结尾。c.txt 的 read 在保留区里，没有被摘要代替，所以不在清单里。§7.2 会看到第二次压缩时清单怎么累积。

**往哪走**：§4.7 `compact`。

### 4.7 compact()：生成摘要并拼接

**调用位置**：`_runAutoCompaction()` → `_runDefaultCompaction()`（`packages/coding-agent/src/core/agent-session.ts:2804`）→ `compact(preparation, model, apiKey, …, this.agent.streamFunction, …)`。扩展在 `session_before_compact` 里返回了自己的摘要时，不调用 `compact`（§4.11）。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:952-979`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L952-L979)

```ts
export async function compact(
	preparation: CompactionPreparation,
	model: Model<any>,
	apiKey: string | undefined,
	headers?: Record<string, string>,
	customInstructions?: string,
	signal?: AbortSignal,
	thinkingLevel?: ThinkingLevel,
	streamFn?: StreamFn,
	env?: Record<string, string>,
	retry?: RetryPolicy,
	callbacks?: RetryCallbacks,
	sessionId?: string,
): Promise<CompactionResult> {
	const {
		firstKeptEntryId,
		messagesToSummarize,
		turnPrefixMessages,
		isSplitTurn,
		tokensBefore,
		previousSummary,
		fileOps,
	} = preparation;

	// Generate summaries and merge into one
	let summary: string;
	let summaryUsage: Usage;

```

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:980-1019`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L980-L1019)

```ts
	if (isSplitTurn && turnPrefixMessages.length > 0) {
		let historyText = previousSummary ?? "No prior history.";
		let historyUsage: Usage | undefined;
		if (messagesToSummarize.length > 0) {
			const historyResult = await generateSummaryWithUsage(
				messagesToSummarize,
				model,
				undefined,
				apiKey,
				headers,
				signal,
				customInstructions,
				previousSummary,
				thinkingLevel,
				streamFn,
				env,
				retry,
				callbacks,
				sessionId,
			);
			historyText = historyResult.text;
			historyUsage = historyResult.usage;
		}
		const turnPrefixResult = await generateTurnPrefixSummary(
			turnPrefixMessages,
			model,
			apiKey,
			headers,
			env,
			signal,
			thinkingLevel,
			streamFn,
			retry,
			callbacks,
			sessionId,
			customInstructions,
		);
		// Merge into single summary
		summary = `${historyText}\n\n---\n\n**Turn Context (split turn):**\n\n${turnPrefixResult.text}`;
		summaryUsage = historyUsage ? combineUsage(historyUsage, turnPrefixResult.usage) : turnPrefixResult.usage;
```

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:1020-1057`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L1020-L1057)

```ts
	} else {
		// Just generate history summary
		const result = await generateSummaryWithUsage(
			messagesToSummarize,
			model,
			undefined,
			apiKey,
			headers,
			signal,
			customInstructions,
			previousSummary,
			thinkingLevel,
			streamFn,
			env,
			retry,
			callbacks,
			sessionId,
		);
		summary = result.text;
		summaryUsage = result.usage;
	}

	// Compute file lists and append to summary
	const { readFiles, modifiedFiles } = computeFileLists(fileOps);
	summary += formatFileOperations(readFiles, modifiedFiles);

	if (!firstKeptEntryId) {
		throw new Error("First kept entry has no UUID - session may need migration");
	}

	return {
		summary,
		firstKeptEntryId,
		tokensBefore,
		usage: summaryUsage,
		details: { readFiles, modifiedFiles } as CompactionDetails,
	};
}
```

**讲解**

`compact` 分两种情况：

- 没有切开轮次：一次模型调用，把 `messagesToSummarize` 写成历史摘要（有 `previousSummary` 时是"更新"摘要，§4.8）。
- 切开了轮次：最多两次模型调用。历史部分有消息时照常生成历史摘要，没有时用上一次的摘要，再没有就用字符串 `"No prior history."`。被切掉的轮次前半段单独用 `generateTurnPrefixSummary` 写一份前缀摘要（§4.10）。两份用固定的分隔拼接：

```
<历史摘要>

---

**Turn Context (split turn):**

<前缀摘要>
```

两次调用的 `usage` 用 `combineUsage` 相加。最后，无论哪种情况，都在摘要后面追加文件清单，并把同样的清单存进 `details`。

§2.3 的 `summary` 就是第二种情况的产物：`No prior history.`（第一次压缩，历史部分为空），分隔线，假模型写的前缀摘要，`<read-files>`。§2.2 的终端输出里只有一次压缩请求，因为历史部分为空，省掉了历史摘要那次调用。§7.2 会看到两次调用都发生的情况。

传给 `compact` 的 `streamFn` 是 `this.agent.streamFunction`，也就是 M8 给 `Agent` 装的那个函数（→ M8 `createAgentSession`）。摘要请求因此和普通请求走同一条路径：同样的认证、请求头、provider，但不经过 Agent 循环，不发 Agent 事件，也不进 transcript。

**往哪走**：§4.8 `generateSummary`。

### 4.8 generateSummary()、generateSummaryWithUsage() 和 completeSummarization()

**调用位置**：`compact()` → `generateSummaryWithUsage()`。`generateSummary()`（`packages/coding-agent/src/core/compaction/compaction.ts:644`）是旧的导出接口，参数相同，调用 `generateSummaryWithUsage()` 后只返回文本，供只需要文本的调用方使用。

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:712-753`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L712-L753)

```ts
): Promise<{ text: string; usage: Usage }> {
	// Use update prompt if we have a previous summary, otherwise initial prompt
	let basePrompt = previousSummary ? UPDATE_SUMMARIZATION_PROMPT : SUMMARIZATION_PROMPT;
	if (customInstructions) {
		basePrompt = `${basePrompt}\n\nAdditional focus: ${customInstructions}`;
	}

	// Serialize conversation to text so model doesn't try to continue it
	// Convert to LLM messages first (handles custom types like bashExecution, custom, etc.)
	const llmMessages = convertToLlm(currentMessages);
	const conversationText = serializeConversation(llmMessages);

	// Build the prompt with conversation wrapped in tags
	let promptText = `<conversation>\n${conversationText}\n</conversation>\n\n`;
	if (previousSummary) {
		promptText += `<previous-summary>\n${previousSummary}\n</previous-summary>\n\n`;
	}
	promptText += basePrompt;

	const completionOptions = createSummarizationOptions(model, apiKey, headers, env, signal, thinkingLevel, sessionId);

	const response = await completeSummarization(
		model,
		buildSummarizationContext(promptText),
		completionOptions,
		streamFn,
		retry,
		callbacks,
	);

	const failure = getSummarizationFailure(response, "Summarization");
	if (failure) {
		throw new Error(failure);
	}
	if (response.content.some((block) => block.type === "toolCall")) {
		throw new Error("Summarization attempted to call a tool");
	}

	const textContent = contentText(response.content);

	return { text: textContent, usage: response.usage };
}
```

请求的上下文只有两条消息：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:681-693`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L681-L693)

```ts
/** Build the provider context for a standalone summary request. */
function buildSummarizationContext(promptText: string): TranscriptContext {
	return normalizeContext({
		systemPrompt: SUMMARIZATION_SYSTEM_PROMPT,
		messages: [
			{
				role: "user",
				content: [{ type: "text", text: promptText }],
				timestamp: Date.now(),
			},
		],
	});
}
```

`SUMMARIZATION_SYSTEM_PROMPT` 在 `utils.ts` 里：

源码：[`packages/coding-agent/src/core/compaction/utils.ts:161-163`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/utils.ts#L161-L163)

```ts
export const SUMMARIZATION_SYSTEM_PROMPT = `You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary.`;
```

真正发请求的地方：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:618-638`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L618-L638)

```ts
export async function completeSummarization(
	model: Model<any>,
	context: TranscriptContext,
	options: SimpleStreamOptions,
	streamFn?: StreamFn,
	retry?: RetryPolicy,
	callbacks?: RetryCallbacks,
): Promise<AssistantMessage> {
	// Avoid cache writes for one-off summaries. Reuse caller-supplied routing when available;
	// callers without a session ID, including branch summaries, receive a fresh routing ID.
	const requestOptions: SimpleStreamOptions = {
		...options,
		cacheRetention: "none",
		sessionId: options.sessionId ?? uuidv7(),
	};
	const produce = async (): Promise<AssistantMessage> =>
		streamFn
			? (await streamFn(model, context, requestOptions)).result()
			: completeSimple(model, context, requestOptions);
	return retryAssistantCall(produce, retry, requestOptions.signal, callbacks);
}
```

**讲解**

摘要请求是一次独立的、单轮的模型调用：

- 系统提示词换成专门的摘要提示词，要求"不要继续对话，只输出摘要"。`normalizeContext`（→ M1）把 `systemPrompt` 变成 transcript 开头的 system 消息，这就是 `m5-faux-ext.ts` 能从系统消息内容认出压缩请求的原因。
- 要摘要的消息不是作为多条消息发送，而是先 `convertToLlm`（自定义消息变成 user 消息，→ M8），再 `serializeConversation` 拼成一段文本，放进 `<conversation>` 标签，作为一条 user 消息发出（§4.9）。
- 有上一次的摘要时，放进 `<previous-summary>` 标签，并改用"更新"模板。
- 手动 `/compact 关注 X` 的附加说明以 `Additional focus: …` 追加在模板后面。

`completeSummarization` 做两件事。第一，把 `cacheRetention` 设为 `"none"`：摘要请求是一次性的，写 prompt cache 只会白花钱。faux provider 也模拟了这一点（`packages/ai/src/providers/faux.ts:262`），所以 §2.3 的 `usage` 里 `cacheWrite` 为 0、`input` 为 1229，而普通请求的 `input` 都是 0（全部算作 cache 读写）。第二，用 `retryAssistantCall`（→ M1）包一层，网络瞬断时按 `settings.retry` 重试。

回复的检查很严格：`stopReason` 为 `error` 或 `length` 都抛错。`length` 表示摘要被截断了，不完整的摘要不能成为检查点。回复里有工具调用也抛错。抛出的错误在 `_runAutoCompaction` 里被捕获，变成 `compaction_end` 事件的 `errorMessage`，会话文件不变（§5.1）。

参数 `_reserveTokens` 已经不用了。注释说摘要请求用模型自己的输出上限（#9512）。

**往哪走**：§4.9 `serializeConversation`，然后 §4.10 看两个模板。

### 4.9 serializeConversation()

**调用位置**：`generateSummaryWithUsage()`、`generateTurnPrefixSummary()`、`generateBranchSummary()` 都在 `convertToLlm()` 之后调用它。

源码：[`packages/coding-agent/src/core/compaction/utils.ts:93-104`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/utils.ts#L93-L104)

```ts
/** Maximum characters for a tool result in serialized summaries. */
const TOOL_RESULT_MAX_CHARS = 2000;

/**
 * Truncate text to a maximum character length for summarization.
 * Keeps the beginning and appends a truncation marker.
 */
function truncateForSummary(text: string, maxChars: number): string {
	if (text.length <= maxChars) return text;
	const truncatedChars = text.length - maxChars;
	return `${text.slice(0, maxChars)}\n\n[... ${truncatedChars} more characters truncated]`;
}
```

源码：[`packages/coding-agent/src/core/compaction/utils.ts:120-155`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/utils.ts#L120-L155)

```ts
export function serializeConversation(messages: Message[]): string {
	const parts: string[] = [];

	for (const msg of messages) {
		if (msg.role === "user") {
			const content = contentText(msg.content, "");
			if (content) parts.push(`[User]: ${content}`);
		} else if (msg.role === "assistant") {
			const toolCalls: string[] = [];

			for (const block of msg.content) {
				if (block.type === "toolCall") {
					const args = block.arguments as Record<string, unknown>;
					const argsStr = Object.entries(args)
						.map(([k, v]) => `${k}=${JSON.stringify(v)}`)
						.join(", ");
					toolCalls.push(`${block.name}(${argsStr})`);
				}
			}

			if (msg.content.some((block) => block.type === "text")) {
				parts.push(`[Assistant]: ${contentText(msg.content)}`);
			}
			if (toolCalls.length > 0) {
				parts.push(`[Assistant tool calls]: ${toolCalls.join("; ")}`);
			}
		} else if (msg.role === "toolResult") {
			const content = contentText(msg.content, "");
			if (content) {
				parts.push(`[Tool result]: ${truncateForSummary(content, TOOL_RESULT_MAX_CHARS)}`);
			}
		}
	}

	return parts.join("\n\n");
}
```

**讲解**

每条 LLM 消息变成一行带标签的文本：`[User]:`、`[Assistant]:`、`[Assistant tool calls]: read(path="a.txt")`、`[Tool result]:`。段与段之间空一行。几个取舍：

- 为什么要变成文本：如果把原对话作为多条消息发送，模型会以为自己在对话里，倾向于"接着做"，而不是"写摘要"。变成一段被标签包住的文本，它就只是材料。
- 工具结果截断到 2000 字符。工具结果（read、bash 的输出）是上下文里最大的部分，摘要不需要全文。§2.2 的文件 1848 字符，没有触发截断。
- `thinking` 块被丢掉。provider 返回的 thinking 往往是推理过程的摘要，回放时也经常被丢弃，算进来可能让摘要请求比被摘要的上下文还大。
- `system` 消息不输出。它们在 `prepareCompaction` 里已经被过滤掉了（§4.4）。

§2.2 终端输出里 `# Conversation` 下面那几行就是这个函数的输出。

**往哪走**：§4.10。

### 4.10 摘要模板和 generateTurnPrefixSummary()

**调用位置**：`generateSummaryWithUsage()` 选择 `SUMMARIZATION_PROMPT` 或 `UPDATE_SUMMARIZATION_PROMPT`；`compact()` 在切开轮次时调用 `generateTurnPrefixSummary()`。

首次摘要的模板：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:507-538`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L507-L538)

```ts
const SUMMARIZATION_PROMPT = `The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages.`;
```

更新摘要的模板（`UPDATE_SUMMARIZATION_INSTRUCTIONS`，`packages/coding-agent/src/core/compaction/compaction.ts:540`）结构相同，前面多了一组规则：保留旧摘要的全部信息，加入新进展，把完成的事项从 In Progress 移到 Done，更新 Next Steps。它前面再加一句说明，告诉模型上面是"新的"消息，旧摘要在 `<previous-summary>` 里：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:577-579`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L577-L579)

```ts
const UPDATE_SUMMARIZATION_PROMPT = `The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

${UPDATE_SUMMARIZATION_INSTRUCTIONS}`;
```

分割轮次前缀的模板：

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:929-942`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L929-L942)

```ts
const TURN_PREFIX_SUMMARIZATION_PROMPT = `The messages above are earlier context from an ongoing conversation. Later messages are stored separately and do not need to be reconstructed.

Create a concise checkpoint of the user's request and the progress shown above. This checkpoint will be placed before the later messages so the conversation can continue with the necessary context.

## Original Request
[What did the user ask for?]

## Progress So Far
- [Key decisions and work completed in these messages]

## Context Needed to Continue
- [Information from these messages needed to understand the later work]

Only summarize information explicitly present above. Do not infer or recreate later messages.`;
```

源码：[`packages/coding-agent/src/core/compaction/compaction.ts:1062-1081`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/compaction.ts#L1062-L1081)

```ts
async function generateTurnPrefixSummary(
	messages: AgentMessage[],
	model: Model<any>,
	apiKey: string | undefined,
	headers?: Record<string, string>,
	env?: Record<string, string>,
	signal?: AbortSignal,
	thinkingLevel?: ThinkingLevel,
	streamFn?: StreamFn,
	retry?: RetryPolicy,
	callbacks?: RetryCallbacks,
	sessionId?: string,
	customInstructions?: string,
): Promise<{ text: string; usage: Usage }> {
	const llmMessages = convertToLlm(messages);
	const conversationText = serializeConversation(llmMessages);
	let promptText = `# Conversation\n${conversationText}\n\n# Instructions\n${TURN_PREFIX_SUMMARIZATION_PROMPT}`;
	if (customInstructions) {
		promptText = `${promptText}\n\nAdditional focus: ${customInstructions}`;
	}
```

函数余下的部分（`packages/coding-agent/src/core/compaction/compaction.ts:1083-1104`）和 `generateSummaryWithUsage` 一样：调用 `completeSummarization`，检查 `stopReason` 和工具调用，返回文本和用量。

**讲解**

首次摘要的模板有 6 节：Goal、Constraints & Preferences、Progress（Done / In Progress / Blocked）、Key Decisions、Next Steps、Critical Context。这个结构是给"另一个 LLM"看的：压缩后，模型读到的就是这份摘要，它需要知道目标、已做的事和下一步。模板最后要求保留准确的文件路径、函数名和错误信息，因为这些是摘要最容易丢、又最难重新找到的东西。

前缀模板只有 3 节：Original Request、Progress So Far、Context Needed to Continue。它的处境不同：轮次的后半段原样保留在摘要后面，所以前缀摘要只需要交代"用户要什么、前面做到哪了"，并且明确要求"不要推测或重建后面的消息"。

格式上，首次和更新模板用 `<conversation>…</conversation>` 包住对话，模板放在最后；前缀模板用 `# Conversation` 和 `# Instructions` 两个标题。§2.2 的 `m5-faux-ext.ts` 就是靠这个区别（是否以 `# Conversation` 开头）认出前缀请求的。

**往哪走**：摘要生成完，回到 AgentSession 落盘，§4.11。

### 4.11 _runAutoCompaction() 后半段：拦截、落盘、刷新、事件

**调用位置**：`_runAutoCompaction()` 在 `prepareCompaction()` 之后。

源码：[`packages/coding-agent/src/core/agent-session.ts:3203-3226`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3203-L3226)

```ts
			let extensionCompaction: CompactionResult | undefined;

			if (this._extensionRunner.hasHandlers("session_before_compact")) {
				const extensionResult = (await this._extensionRunner.emit({
					type: "session_before_compact",
					preparation,
					branchEntries: pathEntries,
					customInstructions: undefined,
					reason,
					willRetry,
					signal: abortController.signal,
				})) as SessionBeforeCompactResult | undefined;

				if (extensionResult?.cancel) {
					cancelledByExtension = true;
					throw new Error("Compaction cancelled");
				}

				if (extensionResult?.compaction) {
					extensionCompaction = extensionResult.compaction;
					fromExtension = true;
				}
			}
			abortController.signal.throwIfAborted();
```

源码：[`packages/coding-agent/src/core/agent-session.ts:3256-3292`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/agent-session.ts#L3256-L3292)

```ts
			abortController.signal.throwIfAborted();

			this.sessionManager.appendCompaction(summary, firstKeptEntryId, tokensBefore, details, fromExtension, usage);
			const newEntries = this.sessionManager.getEntries();
			this._refreshFinalizedContext();
			const estimatedTokensAfter = estimateMessagesTokens(this.sessionManager.buildSessionProjection().messages);

			// Get the saved compaction entry for the extension event
			const savedCompactionEntry = newEntries.find((e) => e.type === "compaction" && e.summary === summary) as
				| CompactionEntry
				| undefined;

			if (this._extensionRunner && savedCompactionEntry) {
				await this._extensionRunner.emit({
					type: "session_compact",
					compactionEntry: savedCompactionEntry,
					fromExtension,
					reason,
					willRetry,
				});
			}

			const result: CompactionResult = {
				summary,
				firstKeptEntryId,
				tokensBefore,
				estimatedTokensAfter,
				usage,
				details,
			};
			this._emit({ type: "compaction_end", reason, result, aborted: false, willRetry });

			if (willRetry) return true;

			// Auto-compaction can complete while follow-up/steering/custom messages are waiting.
			// Continue once so queued messages are delivered.
			return this.agent.hasQueuedMessages();
```

**讲解**

这一段属于 M8，这里只关心数据怎么流动：

1. 扩展可以在 `session_before_compact` 里取消压缩，或者直接给出一个 `CompactionResult`（→ M7）。给出了就不调用 `compact`，并记 `fromExtension = true`，最后写进条目的 `fromHook`。
2. `appendCompaction` 写入新条目（§4.12）。
3. `_refreshFinalizedContext()`（`packages/coding-agent/src/core/agent-session.ts:954`）用 `buildSessionProjection()` 重建上下文，直接替换 `agent.state.messages`。从这一刻起，Agent 持有的 transcript 就是"摘要 + 保留区"。
4. `estimatedTokensAfter` 用 `estimateMessagesTokens`（`packages/coding-agent/src/core/agent-session.ts:380`，对每条消息调用 `estimateTokens` 求和）算出，只放进 `compaction_end` 事件，不存盘。
5. 发出扩展事件 `session_compact` 和 AgentSession 事件 `compaction_end`。
6. 返回值告诉调用方要不要 `agent.continue()`：溢出恢复（`willRetry`）时要，或者压缩期间有排队的消息时要。轮间压缩的调用方 `_compactBeforeNextAssistantResponse` 不看这个返回值，因为循环本来就会继续。

任何一步抛错（包括摘要请求失败、被中止、被扩展取消），都进入 catch：发出带 `errorMessage` 或 `aborted` 的 `compaction_end`，返回 `false`。这时还没有写入任何条目，会话文件保持原样。

**往哪走**：§4.12 `appendCompaction`。

### 4.12 SessionManager.appendCompaction()

**调用位置**：`_runAutoCompaction()`、`compact()`（手动）和扩展的边界草稿（`_applyBoundaryDrafts`，`packages/coding-agent/src/core/agent-session.ts:981`）都调用它。

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

**讲解**

`appendCompaction` 和 `appendMessage` 一样，挂在当前叶子下面，然后推进叶子，经过 `_appendEntry` → `_persist` 追加一行 JSON 到会话文件（→ M4）。和普通消息条目相比有两点特别：

- `firstKeptEntryId` 传 `null` 时，用新条目自己的 id。这是"一条都不保留"的压缩：重建上下文时，保留区从这个条目本身开始，于是只剩摘要。
- `systemMessage`：用 `getCurrentSystemMessage`（→ M1）把投影里所有 system 消息合并成一份当前状态，拷贝进条目，时间戳改成条目的时间。压缩后，旧的 system 消息（包括工具清单）在被摘要的范围里，不会再出现在上下文中；这份拷贝保证摘要之后的上下文仍然以完整的系统提示词和工具清单开头。§2.2 第 5 次请求的第一条 system 消息就来自这里。

注意它没有修改或删除任何已有条目。会话文件是只追加的，压缩只是在树上多加一个节点。

**往哪走**：§4.13 看重建上下文时怎么用这个条目。

### 4.13 回到 buildContextEntries()：压缩后的上下文

**调用位置**：`_refreshFinalizedContext()`、`prepareRequest` 投影（→ M8）、下一次 `prepareCompaction()` 都调用 `buildSessionProjection()` → `buildContextEntries()`（→ M4）。

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

`compaction` 条目产生的消息：

源码：[`packages/coding-agent/src/core/session-manager.ts:461-464`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L461-L464)

```ts
	if (entry.type === "compaction") {
		const summary = createCompactionSummaryMessage(entry.summary, entry.tokensBefore, entry.timestamp);
		return entry.systemMessage ? [entry.systemMessage, summary] : [summary];
	}
```

发给模型前的转换：

源码：[`packages/coding-agent/src/core/messages.ts:11-17`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/messages.ts#L11-L17)

```ts
export const COMPACTION_SUMMARY_PREFIX = `The conversation history before this point was compacted into the following summary:

<summary>
`;

export const COMPACTION_SUMMARY_SUFFIX = `
</summary>`;
```

源码：[`packages/coding-agent/src/core/messages.ts:176-183`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/messages.ts#L176-L183)

```ts
				case "compactionSummary":
					return {
						role: "user",
						content: [
							{ type: "text" as const, text: COMPACTION_SUMMARY_PREFIX + m.summary + COMPACTION_SUMMARY_SUFFIX },
						],
						timestamp: m.timestamp,
					};
```

**讲解**

`buildContextEntries` 沿当前分支取出条目。分支上有 `compaction` 条目时，结果由三部分组成：

1. 最新的 `compaction` 条目本身，放在最前面。
2. 压缩之前的条目中，从 `firstKeptEntryId` 开始到压缩条目之前的部分，但去掉 system 消息条目（它们已经合并进了 `systemMessage`）。
3. 压缩条目之后的所有条目。

然后 `sessionEntryToContextMessages` 把 `compaction` 条目变成两条 Agent 消息：`systemMessage`（有的话）和 `compactionSummary`。最后 `convertToLlm` 把 `compactionSummary` 变成 user 消息，摘要包在 `<summary>` 标签里，前面一句说明"之前的对话被压缩成了下面的摘要"。

所以 §2.2 第 5 次请求收到的 4 条消息是：

| 消息 | 来源 |
|---|---|
| system | `compaction` 条目的 `systemMessage` |
| user "The conversation history before this point was compacted…" | `compaction` 条目的 `summary` |
| assistant "读 c.txt。" | 保留区：`firstKeptEntryId` 指向的条目 |
| toolResult c.txt | 保留区 |

之后模型的回复"文件都读完了。"挂在 `compaction` 条目之后，属于第 3 部分。

只有最新的 `compaction` 条目产生摘要。如果上一次的 `compaction` 条目正好落在本次的保留区里，`buildSessionProjection` 把它的消息置空（`packages/coding-agent/src/core/session-manager.ts:562`）。第二次压缩的摘要已经通过 `previousSummary` 吸收了第一次的摘要，旧摘要不需要再出现。

**往哪走**：§4.14 手动压缩，§4.15 分支摘要。

### 4.14 手动压缩 AgentSession.compact()

**调用位置**：TUI 的 `/compact [说明]`、RPC 的 `compact` 命令、扩展 API 的 `ctx.compact()`。

`compact(customInstructions)`（`packages/coding-agent/src/core/agent-session.ts:2849`）和 `_runAutoCompaction` 的结构几乎一样，区别有四点：

- 先 `await this.abort()`，中止正在进行的运行。手动压缩之后不重试、不继续被中止的那一轮。
- 不检查阈值，直接 `prepareCompaction`。返回 `undefined` 时抛错：最后一个条目是 `compaction` 时是 "Already compacted"，否则是 "Nothing to compact (session too small)"。
- `customInstructions` 传给 `compact()`，以 `Additional focus:` 追加在摘要模板后面。
- 事件的 `reason` 是 `"manual"`，失败时向调用方抛出错误，而不是只发事件。

两条路径共用 `_runDefaultCompaction`，摘要的生成逻辑完全相同。

### 4.15 分支摘要：branch-summarization.ts

**调用位置**：TUI 的 `/tree` 导航到另一个条目时，用户选择"总结离开的分支"，调用 `AgentSession.navigateTree(targetId, { summarize: true })`（`packages/coding-agent/src/core/agent-session.ts:4045`）→ `collectEntriesForBranchSummary()` → `generateBranchSummary()` → `SessionManager.branchWithSummary()`。

压缩是"同一条分支变长了，压缩前面的部分"。分支摘要是"离开当前分支，去树上另一个位置继续"，把离开的那段写成摘要，带到新位置。两者共用 `estimateTokens`、`serializeConversation`、`completeSummarization` 和文件跟踪。

收集离开的条目：

源码：[`packages/coding-agent/src/core/compaction/branch-summarization.ts:110-150`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/branch-summarization.ts#L110-L150)

```ts
export function collectEntriesForBranchSummary(
	session: ReadonlySessionManager,
	oldLeafId: string | null,
	targetId: string,
): CollectEntriesResult {
	// If no old position, nothing to summarize
	if (!oldLeafId) {
		return { entries: [], commonAncestorId: null };
	}

	// Find common ancestor (deepest node that's on both paths)
	const oldPath = new Set(session.getBranch(oldLeafId).map((e) => e.id));
	const targetPath = session.getBranch(targetId);

	// targetPath is root-first, so iterate backwards to find deepest common ancestor
	let commonAncestorId: string | null = null;
	for (let i = targetPath.length - 1; i >= 0; i--) {
		if (oldPath.has(targetPath[i].id)) {
			commonAncestorId = targetPath[i].id;
			break;
		}
	}

	// Collect entries from old leaf back to common ancestor
	const entries: SessionEntry[] = [];
	let current: string | null = oldLeafId;

	while (current && current !== commonAncestorId) {
		const entry = session.getEntry(current);
		if (!entry) break;
		entries.push(entry);
		current = entry.parentId;
	}

	// Reverse to get chronological order
	entries.reverse();

	return { entries, commonAncestorId };
}

// ============================================================================
```

按预算挑消息：

源码：[`packages/coding-agent/src/core/compaction/branch-summarization.ts:197-249`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/branch-summarization.ts#L197-L249)

```ts
export function prepareBranchEntries(entries: SessionEntry[], tokenBudget: number = 0): BranchPreparation {
	const messages: AgentMessage[] = [];
	const fileOps = createFileOps();
	let totalTokens = 0;

	// First pass: collect file ops from ALL entries (even if they don't fit in token budget)
	// This ensures we capture cumulative file tracking from nested branch summaries
	// Only extract from pi-generated summaries (fromHook !== true), not extension-generated ones
	for (const entry of entries) {
		if (entry.type === "branch_summary" && !entry.fromHook && entry.details) {
			const details = entry.details as BranchSummaryDetails;
			if (Array.isArray(details.readFiles)) {
				for (const f of details.readFiles) fileOps.read.add(f);
			}
			if (Array.isArray(details.modifiedFiles)) {
				// Modified files go into both edited and written for proper deduplication
				for (const f of details.modifiedFiles) {
					fileOps.edited.add(f);
				}
			}
		}
	}

	// Second pass: walk from newest to oldest, adding messages until token budget
	for (let i = entries.length - 1; i >= 0; i--) {
		const entry = entries[i];
		const message = getMessageFromEntry(entry);
		if (!message) continue;

		// Extract file ops from assistant messages (tool calls)
		extractFileOpsFromMessage(message, fileOps);

		const tokens = estimateTokens(message);

		// Check budget before adding
		if (tokenBudget > 0 && totalTokens + tokens > tokenBudget) {
			// If this is a summary entry, try to fit it anyway as it's important context
			if (entry.type === "compaction" || entry.type === "branch_summary") {
				if (totalTokens < tokenBudget * 0.9) {
					messages.unshift(message);
					totalTokens += tokens;
				}
			}
			// Stop - we've hit the budget
			break;
		}

		messages.unshift(message);
		totalTokens += tokens;
	}

	return { messages, fileOps, totalTokens };
}
```

生成摘要的后半段：

源码：[`packages/coding-agent/src/core/compaction/branch-summarization.ts:352-385`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/compaction/branch-summarization.ts#L352-L385)

```ts
	const context = normalizeContext({ systemPrompt: SUMMARIZATION_SYSTEM_PROMPT, messages: summarizationMessages });
	const requestOptions: SimpleStreamOptions = { apiKey, headers, env, signal };
	if (model.reasoning && thinkingLevel && thinkingLevel !== "off") {
		requestOptions.reasoning = thinkingLevel;
	}
	const response = await completeSummarization(model, context, requestOptions, streamFn, retry, callbacks);

	// Check if aborted or errored
	if (response.stopReason === "aborted") {
		return { aborted: true };
	}
	const failure = getSummarizationFailure(response, "Branch summarization");
	if (failure) {
		return { error: failure };
	}
	if (response.content.some((block) => block.type === "toolCall")) {
		return { error: "Branch summarization attempted to call a tool" };
	}

	let summary = contentText(response.content);

	// Prepend preamble to provide context about the branch summary
	summary = BRANCH_SUMMARY_PREAMBLE + summary;

	// Compute file lists and append to summary
	const { readFiles, modifiedFiles } = computeFileLists(fileOps);
	summary += formatFileOperations(readFiles, modifiedFiles);

	return {
		summary: summary || "No summary generated",
		usage: response.usage,
		readFiles,
		modifiedFiles,
	};
```

写入新条目：

源码：[`packages/coding-agent/src/core/session-manager.ts:1610-1635`](https://github.com/earendil-works/pi/blob/2c8d1feb147f6b3d97e855da803bfe7b19f3c403/packages/coding-agent/src/core/session-manager.ts#L1610-L1635)

```ts
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

1. `collectEntriesForBranchSummary` 找旧叶子和目标的最近公共祖先，从旧叶子往上走到公共祖先（不含），得到离开的条目，再反转成时间顺序。它不在 `compaction` 条目处停下：离开的分支里如果有压缩，压缩摘要本身也是要带走的上下文。
2. `prepareBranchEntries` 先扫一遍全部条目，并入嵌套的 `branch_summary` 条目的 `details` 文件清单（累积跟踪，和压缩的做法一样）。然后从最新往最旧挑消息，直到预算用完。预算是 `contextWindow - reserveTokens`，`reserveTokens` 来自设置 `branchSummary.reserveTokens`，和压缩的 `reserveTokens` 是两个设置。超预算时，如果当前条目是 `compaction` 或 `branch_summary`，并且已用不到 90%，仍然把它塞进去，因为摘要是信息密度最高的上下文。
3. `getMessageFromEntry`（`packages/coding-agent/src/core/compaction/branch-summarization.ts:158`）跳过 `toolResult`，只看 assistant 里的工具调用。分支摘要比压缩更粗：它只需要知道"在那条分支上做了什么"。这个函数的注释说它"类似 compaction.ts 里的 getMessageFromEntry"，但 compaction.ts 里已经没有这个函数了，对应的是 `getMessagesFromProjectedEntryForCompaction`。
4. `generateBranchSummary` 用 `BRANCH_SUMMARY_PROMPT`（`packages/coding-agent/src/core/compaction/branch-summarization.ts:260`）：和压缩模板相同的前 5 节，没有 Critical Context。结果前面加一段固定说明 `BRANCH_SUMMARY_PREAMBLE`，后面加文件清单。失败时返回 `{ error }` 或 `{ aborted: true }`，不抛异常。
5. `branchWithSummary(branchFromId, …)` 把叶子移到 `branchFromId`，在它下面追加 `branch_summary` 条目。`fromId` 记的是离开时的旧叶子。

`navigateTree` 决定新叶子的位置：目标是 user 消息时，新叶子是它的父节点，user 消息的文本放回输入框，让用户修改后重新发送；目标是其他条目时，新叶子就是目标本身。

**真实实例。** `m5-direct.ts branch` 在内存里建了一个会话：user 让模型把 b.txt 改成大写，模型读 a.txt、改 b.txt、回复"改好了。"。然后按 `navigateTree` 的步骤回到那条 user 消息，模型用一个返回固定文本的 `streamFn` 代替：

```bash
node --conditions=source --experimental-strip-types m5-direct.ts branch
```

```
原分支：
  7ee055db parent=null user: "把 b.txt 改成大写"
  73f3842a parent=7ee055db assistant: "[toolCall read]"
  083d2301 parent=73f3842a toolResult: "a 的内容"
  d2c098ea parent=083d2301 assistant: "[toolCall edit]"
  636fdeab parent=d2c098ea toolResult: "ok"
  cf1ca90f parent=636fdeab assistant: "改好了。"
collectEntriesForBranchSummary: commonAncestorId=7ee055db entries=[73f3842a, 083d2301, d2c098ea, 636fdeab, cf1ca90f]
prepareBranchEntries: totalTokens=14 messages=[assistant, assistant, assistant] fileOps.read=["a.txt"] fileOps.edited=["b.txt"]
摘要请求的提示词：
  | <conversation>
  | [Assistant tool calls]: read(path="a.txt")
  | 
  | [Assistant tool calls]: edit(path="b.txt", edits=[])
  | 
  | [Assistant]: 改好了。
  | </conversation>
  | 
  | Create a structured summary of this conversation branch for context when returning later.
  | 
  | Use this EXACT format:
  | 
  | …（后面是 BRANCH_SUMMARY_PROMPT 的其余部分）
BranchSummaryEntry {"type":"branch_summary","id":"33aaf71e","parentId":null,"fromId":"cf1ca90f","summary":"The user explored a different conversation branch before returning here.\nSummary of that exploration:\n\n## Goal\n把 b.txt 改成大写（假摘要）\n\n<read-files>\na.txt\n</read-files>\n\n<modified-files>\nb.txt\n</modified-files>","details":{"readFiles":["a.txt"],"modifiedFiles":["b.txt"]},"usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"fromHook":false}
新分支的 Agent 消息：[branchSummary]
convertToLlm 之后："The following is a summary of a branch that this conversation came back from:\n\n<summary>\nThe user explored a different c"…
```

可以看到：

- 目标是第一条 user 消息，它本身就是公共祖先，所以离开的条目是它之后的 5 个。
- `prepareBranchEntries` 只留下 3 条 assistant 消息，两个 toolResult 被跳过；文件清单仍然从工具调用里提取到了 `a.txt`（读）和 `b.txt`（改）。
- 新叶子是 user 消息的父节点 `null`（会话的根），所以 `branch_summary` 条目的 `parentId` 是 `null`。新分支的上下文只有一条 `branchSummary` 消息，`convertToLlm` 把它变成以 "The following is a summary of a branch…" 开头的 user 消息（`BRANCH_SUMMARY_PREFIX`，`packages/coding-agent/src/core/messages.ts:19`）。

---

## 5. 状态机

### 5.1 一次自动压缩

```
                     轮间 prepareNextTurn (agent-session.ts:921)
                     运行结束 _handlePostAgentRun (2029)
                     发 prompt 前 _prepareRun (1952)
                                   │
                                   ▼
                     ┌──────────────────────────┐
                     │ 检查                      │ shouldCompact (compaction.ts:267)
                     └────────────┬─────────────┘
              不超过阈值 / 未启用 │ 超过阈值
         ◀────────────────────────┤ → _runAutoCompaction (agent-session.ts:3178)
          (不压缩，无事件)        ▼
                     ┌──────────────────────────┐
                     │ 准备                      │ prepareCompaction (3192)
                     └────────────┬─────────────┘
     返回 undefined (3193-3195)   │ 有可压缩的消息
         ◀────────────────────────┤
          (不压缩，无事件)        ▼ compaction_start (3200)
                     ┌──────────────────────────┐
                     │ 扩展拦截                  │ session_before_compact (3205-3214)
                     └──┬─────────┬──────────┬──┘
           cancel (3216)│  给出摘要 │(3221)    │ 无处理函数 / 不处理
                        │          │          ▼
                        │          │  ┌───────────────────────┐
                        │          │  │ 生成摘要               │ _runDefaultCompaction → compact
                        │          │  │ 1 或 2 次模型调用      │ (compaction.ts:980-1040)
                        │          │  └───┬───────────────┬───┘
                        │          │      │ 抛错 / 中止    │ 成功
                        │          ▼      │               ▼
                        │     ┌───────────┴──────────────────────┐
                        │     │ 落盘 appendCompaction (3258)       │
                        │     │ 刷新 _refreshFinalizedContext(3260)│
                        │     └─────────────────┬────────────────┘
                        ▼                       ▼
   compaction_end { aborted 或 errorMessage }   compaction_end { result } (3286)
   (catch 3293 起，会话文件不变)               session_compact 已发给扩展
```

### 5.2 压缩前后的上下文

下面用 §7.2 的三次运行说明上下文怎么一层层变化。`C1`、`C2` 是两个 `compaction` 条目，方括号是 `buildContextEntries` 的结果（session-manager.ts:476）。

```
 没有压缩：上下文 = 整条分支
   [sys, u1, a, tr, a, u2, a(b), tr(b), a(c), tr(c)]

 C1 之后（firstKeptEntryId = a(c)）：session-manager.ts:499-510
   [C1, a(c), tr(c), …C1 之后的条目]
     │ C1 投影成 [C1.systemMessage, compactionSummary(C1.summary)]   session-manager.ts:461-463
     │ 保留区里的 system 条目被跳过                                   session-manager.ts:504
     ▼
 第二次压缩的准备：boundaryStart = C1 的下一个 (compaction.ts:878-882)
   要摘要：a(c) … 第二个切点之前     previousSummary = C1.summary
   fileOps = C1.details 的文件 + 新消息里的文件 (compaction.ts:68-78)

 C2 之后（firstKeptEntryId 在 C1 之后）：
   [C2, 保留区…, C2 之后的条目]       C1 不在结果里，或者落在保留区里时消息置空
                                       (session-manager.ts:562)
```

每次压缩，上下文的开头都换成最新的一份摘要；旧摘要的内容通过 `previousSummary` 并进新摘要，旧的文件清单通过 `details` 并进新清单。

---

## 6. 设计要点

### 6.1 只追加：压缩是会话树上的一个节点

**做法。** 压缩不删除、不改写任何已有条目，只追加一个 `compaction` 条目，用 `firstKeptEntryId` 记录保留区的起点。上下文由 `buildContextEntries` 在读取时现算。

**理由。** 原始对话完整保留在会话文件里，导出、计费统计、历史搜索都能看到压缩前的内容。会话树的其他分支不受影响：从压缩之前的某个条目分叉出去，那条新分支上没有这个 `compaction` 条目，看到的就是完整的旧上下文。写入只需要追加一行，崩溃时不会留下改了一半的文件。

**代码位置。** `packages/coding-agent/src/core/session-manager.ts:1271`（`appendCompaction`），`packages/coding-agent/src/core/session-manager.ts:476`（`buildContextEntries`）。

### 6.2 真实用量加增量估算，并且知道什么时候不能信用量

**做法。** 上下文大小优先用 provider 报告的 `usage.totalTokens`，只对它之后新增的消息用"字符数 / 4"估算。上下文被 `context_edit` 或 `compaction` 改过之后，旧的 usage 作废，全部改用估算。

**理由。** provider 的数字最准，但只覆盖已经发出的请求。完全靠估算会有误差，完全靠 usage 又看不到刚追加的工具结果。混合使用两者，误差只出现在最后一小段上。usage 作废的规则避免了一个具体的 bug：压缩刚完成时，最后一条 assistant 消息的 usage 还是压缩前的大数字，信任它会立刻再次压缩。

**代码位置。** `packages/coding-agent/src/core/compaction/compaction.ts:196`，`packages/coding-agent/src/core/compaction/compaction.ts:227`，`packages/coding-agent/src/core/agent-session.ts:3052`。

### 6.3 按"轮次"切，必要时切开轮次并分开摘要

**做法。** 切点只能落在 user、assistant 或自定义消息上，绝不落在 toolResult 上。一个轮次本身超过保留预算时，切点落在轮次中间的 assistant 上，轮次前半段用专门的前缀模板单独摘要，和历史摘要拼在一起。

**理由。** 工具调用和工具结果必须成对出现，否则 provider 拒绝请求。长时间的 agent 工作通常是"一条 user 消息 + 几十轮工具调用"，如果只能在 user 消息处切，这种轮次永远无法压缩。前缀摘要的模板强调"用户要什么、做到哪了"，并禁止推测后续内容，因为后半段原样保留在它后面。

**代码位置。** `packages/coding-agent/src/core/compaction/compaction.ts:351`（`isCutPointMessage`），`packages/coding-agent/src/core/compaction/compaction.ts:446`（`findCutPoint`），`packages/coding-agent/src/core/compaction/compaction.ts:980`（拼接两份摘要）。

### 6.4 摘要请求和对话隔离

**做法。** 摘要请求用专门的系统提示词，把对话序列化成带标签的文本放进一条 user 消息，关闭 prompt cache，不经过 Agent 循环，不发 Agent 事件，不进 transcript。但它复用 `agent.streamFunction`，所以认证和 provider 配置与普通请求一致。

**理由。** 对话作为多条消息发送时，模型容易接着干活而不是写摘要；变成文本材料后，它的任务只有一个。摘要请求只发一次，写 cache 没有收益。不进 transcript，是因为摘要请求本身不是对话的一部分，只有它的结果（`compaction` 条目）是。

**代码位置。** `packages/coding-agent/src/core/compaction/compaction.ts:682`（`buildSummarizationContext`），`packages/coding-agent/src/core/compaction/compaction.ts:630`（`cacheRetention: "none"`），`packages/coding-agent/src/core/compaction/utils.ts:120`（`serializeConversation`）。

### 6.5 文件清单机械提取、逐次累积；准备和生成分开，留给扩展

**做法。** 读过和改过的文件从 read、write、edit 的参数里提取，不靠模型记忆；每次压缩把上一次 `details` 里的清单并进来。`prepareCompaction` 只算"摘要什么、保留什么"，不调用模型，它的结果原样交给扩展的 `session_before_compact`，扩展可以换用别的模型或别的格式写摘要。

**理由。** 文件路径是继续工作最需要、模型最容易写错的信息，机械提取保证不丢。准备和生成分开以后，切点、文件清单、`tokensBefore` 这些规则只有一份实现，扩展只需要替换"写摘要"这一步（示例 `packages/coding-agent/examples/extensions/custom-compaction.ts`）。

**代码位置。** `packages/coding-agent/src/core/compaction/compaction.ts:60`（`extractFileOperations`），`packages/coding-agent/src/core/compaction/utils.ts:30`，`packages/coding-agent/src/core/agent-session.ts:3205`（扩展拦截）。

---

## 7. 实验

每个实验先写预测，再运行验证。命令都在 `docs/pi-tutorial/lab` 目录下运行。会话 id、时间戳每次运行都不同。token 数和工作目录路径的长度有关（系统提示词里有 cwd），换了目录名会差一两个 token。重复运行时把目录名里的 `-1` 换成新编号，不要复用旧目录。

### 7.1 改变 keepRecentTokens

**问题。** 对 §2.2 的同一个分支，`keepRecentTokens` 从 600 改成 1000 和 5000，切点分别落在哪里？

**预测。** 按 §4.5 的算法从后往前累加：9（462）、8（7）、7（462，累计 931）、6（7，累计 938）、5（462，累计 1400）。

- 1000：累计在下标 5（toolResult）第一次超过 1000，往后找到合法切点 6（读 b.txt 的 assistant）。仍然切开轮次，`turnPrefixMessages` 只剩 user、读 a.txt、a.txt 的结果 3 条，`fileOps.read` 只有 a.txt。
- 5000：累加到开头也不到 5000，切点保持 `cutPoints[0]`，即下标 3 的 user 消息。它是轮次开头，不切开；`messagesToSummarize` 是下标 0–2，其中只有 system 消息，被过滤掉后为空。两段都为空，`prepareCompaction` 返回 `undefined`。这时 `shouldCompact` 仍为 `true`，但不会压缩。

**运行。**

```bash
node --conditions=source --experimental-strip-types m5-direct.ts prepare "$S" 1 1000 | tail -n +12
```

```
ContextUsageEstimate {"tokens":2737,"usageTokens":2275,"trailingTokens":462,"lastUsageIndex":6}
shouldCompact(2737, 3000, reserve=500) = true
findCutPoint（原始条目） {"firstKeptEntryIndex":6,"turnStartIndex":3,"isSplitTurn":true}
CompactionPreparation {"firstKeptEntryId":"56c59afe","isSplitTurn":true,"tokensBefore":2737,"settings":{"enabled":true,"reserveTokens":500,"keepRecentTokens":1000}}
  previousSummary: undefined
  messagesToSummarize (0):
  turnPrefixMessages (3):
    user: "依次读 a.txt b.txt c.txt"
    assistant: "读 a.txt。[toolCall read]"
    toolResult: "[a.txt]\nline 01 of a.txt: lorem ipsum dolor sit 
  fileOps: read=["a.txt"] written=[] edited=[]
```

```bash
node --conditions=source --experimental-strip-types m5-direct.ts prepare "$S" 1 5000 | tail -n +12
```

```
ContextUsageEstimate {"tokens":2737,"usageTokens":2275,"trailingTokens":462,"lastUsageIndex":6}
shouldCompact(2737, 3000, reserve=500) = true
findCutPoint（原始条目） {"firstKeptEntryIndex":3,"turnStartIndex":-1,"isSplitTurn":false}
prepareCompaction 返回 undefined
```

（`tail -n +12` 去掉前面逐条列出条目的 11 行，它们和 §3.3 相同。）

**结论。** 两个预测都成立。`keepRecentTokens` 大于整个当前轮次时，压缩什么也做不了：保留区就是整个轮次，前面没有可以摘要的对话。在真实会话里，这意味着阈值检查每轮都为真，`_runAutoCompaction` 每次都安静地返回 `false`，直到上下文真的溢出，进入 M8 的溢出恢复。

### 7.2 第二次、第三次压缩

**问题。** 同一个会话分三次运行（`-c` 继续上一个会话）：先读 a.txt；再依次读 b、c、d；再读 e.txt。会发生几次压缩？第二次压缩的摘要请求里有什么？文件清单怎么变化？

**预测。**

- 第 1 次运行：上下文只有系统提示词和一个文件，不到 2000 token，不压缩。
- 第 2 次运行：读完 c.txt 后估算约 2745 token，超过 2500，压缩。保留预算 600 从后往前累加到 b.txt 的结果，切点是读 c.txt 的 assistant。它属于第二个轮次（"依次读 b c d"），切开轮次。这次历史部分不为空：第一个轮次（读 a.txt）整个进入 `messagesToSummarize`。所以有两次摘要请求：一次首次摘要（历史），一次前缀摘要。文件清单是 a.txt、b.txt。
- 第 3 次运行：读完 e.txt 后又超过阈值，压缩。这次有 `previousSummary`，用更新模板。要摘要的范围从第一次压缩的保留区开始，即读 c.txt 的 assistant。保留区从后往前：e 的结果 462、读 e 的 assistant、user "读 e.txt"、"文件都读完了。"，再加 d 的结果就超过 600，切点是"文件都读完了。"那条 assistant。往前找轮次开头，一直到 `boundaryStart` 都没有 user 消息（第二个轮次的 user 消息已经被第一次压缩摘要掉了），所以 `turnStartIndex = -1`，不算切开轮次，只有一次摘要请求。文件清单是 a、b（来自第一次压缩的 `details`）加 c、d（来自新摘要的消息）。

**运行。**

```bash
export WORK=/tmp/pi-lab/m5-rep-1-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m5-rep-1-agent
./m5-run.sh -p "读 a.txt"
./m5-run.sh -c -p "依次读 b.txt c.txt d.txt"
M5_TRACE=1 ./m5-run.sh -c -p "读 e.txt" 2>&1
```

```
Running without API keys...
文件都读完了。
```

```
Running without API keys...
文件都读完了。
```

第 3 次运行的输出（`M5_TRACE=1`）：

```
Running without API keys...
[faux] 第 1 次请求：普通请求，收到 8 条消息:
  system            sections=[preamble,tools,rules,docs,cwd] content=""
  user              "The conversation history before this point was compacted in
  assistant         "读 c.txt。"
  toolResult        "[c.txt]\nline 01 of c.txt: lorem ipsum dolor sit amet.\nlin
  assistant         "读 d.txt。"
  toolResult        "[d.txt]\nline 01 of d.txt: lorem ipsum dolor sit amet.\nlin
  assistant         "文件都读完了。"
  user              "读 e.txt"
[faux] 第 2 次请求：压缩请求（更新摘要），提示词 5601 字符:
  | <conversation>
  | [Assistant]: 读 c.txt。
  | 
  | [Assistant tool calls]: read(path="c.txt")
  | 
  | [Tool result]: [c.txt]
  | （这里是 40 行文件内容，不逐行打印）
  | 
  | 
  | [Assistant]: 读 d.txt。
  | 
  | [Assistant tool calls]: read(path="d.txt")
  | 
  | [Tool result]: [d.txt]
  | （这里是 40 行文件内容，不逐行打印）
  | 
  | </conversation>
  | 
  | <previous-summary>
  | ## Goal
  | - 读 a.txt
  | 
  | ## Constraints & Preferences
  | - (none)
  | 
  | ## Progress
  | ### Done
  | - [x] 摘要 #1
  | 
  | ### In Progress
  | - (none)
  | 
  | ### Blocked
  | - (none)
  | 
  | ## Key Decisions
  | - (none)
  | 
  | ## Next Steps
  | 1. 继续
  | 
  | ## Critical Context
  | - (none)
  | 
  | ---
  | 
  | **Turn Context (split turn):**
  | 
  | ## Original Request
  | - 依次读 b.txt c.txt d.txt
  | 
  | ## Progress So Far
  | - 前缀摘要 #2
  | 
  | ## Context Needed to Continue
  | - (none)
  | 
  | <read-files>
  | a.txt
  | b.txt
  | </read-files>
  | </previous-summary>
  | 
  | The messages above are NEW conversation messages to incorporate into the existing summary provided in <previou
…省略…
[faux] 第 3 次请求：普通请求，收到 6 条消息:
  system            sections=[preamble,tools,rules,docs,cwd] content=""
  user              "The conversation history before this point was compacted in
  assistant         "文件都读完了。"
  user              "读 e.txt"
  assistant         "读 e.txt。"
  toolResult        "[e.txt]\nline 01 of e.txt: lorem ipsum dolor sit amet.\nlin
文件都读完了。
```

会话文件和两个 `compaction` 条目的关键字段：

```bash
R=$(ls /tmp/pi-lab/m5-rep-1-agent/sessions/*/*.jsonl)
node show-session.mjs "$R"
node m5-inspect.mjs compaction "$R" | grep -E '"(id|summary|firstKeptEntryId|tokensBefore)"'
```

```
session                id=01a1969c parent=-        {"version":3,"cwd":"/private/tmp/pi-lab/m5-rep-1-work"}
model_change           id=ba94709b parent=null     {"provider":"faux","modelId":"faux-1"}
thinking_level_change  id=d9ab2efb parent=ba94709b {"thinkingLevel":"off"}
message                id=42eec642 parent=d9ab2efb system     "" sections=preamble,tools,rules,docs,cwd toolsAdded=read,bash,edit,write
message                id=f5e2156a parent=42eec642 user       [{"type":"text","text":"读 a.txt"}]
message                id=e5f1e597 parent=f5e2156a assistant  [{"type":"text","text":"读 a.txt。"},{"type":"toolCall","id":"call_a.txt","name":"read","arg stop=toolUse
message                id=ece1118e parent=e5f1e597 toolResult [{"type":"text","text":"[a.txt]\nline 01 of a.txt: lorem ipsum dolor sit amet.\nline 02 of
message                id=302d01b6 parent=ece1118e assistant  [{"type":"text","text":"文件都读完了。"}] stop=stop
message                id=d6ff5626 parent=302d01b6 user       [{"type":"text","text":"依次读 b.txt c.txt d.txt"}]
message                id=39abd2a9 parent=d6ff5626 assistant  [{"type":"text","text":"读 b.txt。"},{"type":"toolCall","id":"call_b.txt","name":"read","arg stop=toolUse
message                id=12ee3e42 parent=39abd2a9 toolResult [{"type":"text","text":"[b.txt]\nline 01 of b.txt: lorem ipsum dolor sit amet.\nline 02 of
message                id=b07d4c0d parent=12ee3e42 assistant  [{"type":"text","text":"读 c.txt。"},{"type":"toolCall","id":"call_c.txt","name":"read","arg stop=toolUse
message                id=e5f16985 parent=b07d4c0d toolResult [{"type":"text","text":"[c.txt]\nline 01 of c.txt: lorem ipsum dolor sit amet.\nline 02 of
compaction             id=590a66a2 parent=e5f16985 {"summary":"## Goal\n- 读 a.txt\n\n## Constraints & Preferences\n- (none)\n\n## Progress\n### Done\n-
message                id=138a96c4 parent=590a66a2 assistant  [{"type":"text","text":"读 d.txt。"},{"type":"toolCall","id":"call_d.txt","name":"read","arg stop=toolUse
message                id=9afd27c0 parent=138a96c4 toolResult [{"type":"text","text":"[d.txt]\nline 01 of d.txt: lorem ipsum dolor sit amet.\nline 02 of
message                id=37780302 parent=9afd27c0 assistant  [{"type":"text","text":"文件都读完了。"}] stop=stop
message                id=aac5947d parent=37780302 user       [{"type":"text","text":"读 e.txt"}]
message                id=db3a42c7 parent=aac5947d assistant  [{"type":"text","text":"读 e.txt。"},{"type":"toolCall","id":"call_e.txt","name":"read","arg stop=toolUse
message                id=14c95a0c parent=db3a42c7 toolResult [{"type":"text","text":"[e.txt]\nline 01 of e.txt: lorem ipsum dolor sit amet.\nline 02 of
compaction             id=0e94348d parent=14c95a0c {"summary":"## Goal\n- (见 previous-summary)\n\n## Constraints & Preferences\n- (none)\n\n## Progress
message                id=58f92137 parent=0e94348d assistant  [{"type":"text","text":"文件都读完了。"}] stop=stop
```

```
  "id": "590a66a2",
  "summary": "## Goal\n- 读 a.txt\n\n## Constraints & Preferences\n- (none)\n\n## Progress\n### Done\n- [x] 摘要 #1\n\n### In Progress\n- (none)\n\n### Blocked\n- (none)\n\n## Key Decisions\n- (none)\n\n## Next Steps\n1. 继续\n\n## Critical Context\n- (none)\n\n---\n\n**Turn Context (split turn):**\n\n## Original Request\n- 依次读 b.txt c.txt d.txt\n\n## Progress So Far\n- 前缀摘要 #2\n\n## Context Needed to Continue\n- (none)\n\n<read-files>\na.txt\nb.txt\n</read-files>",
  "firstKeptEntryId": "b07d4c0d",
  "tokensBefore": 2745,
  "id": "0e94348d",
  "summary": "## Goal\n- (见 previous-summary)\n\n## Constraints & Preferences\n- (none)\n\n## Progress\n### Done\n- [x] 摘要 #1\n\n### In Progress\n- (none)\n\n### Blocked\n- (none)\n\n## Key Decisions\n- (none)\n\n## Next Steps\n1. 继续\n\n## Critical Context\n- (none)\n\n<read-files>\na.txt\nb.txt\nc.txt\nd.txt\n</read-files>",
  "firstKeptEntryId": "37780302",
  "tokensBefore": 2868,
```

**结论。** 两次压缩，和预测一致：

- 第 3 次运行的摘要请求是"更新摘要"：`<conversation>` 里只有读 c.txt、d.txt 的四条消息（第一次压缩的保留区和之后的消息），没有 `[User]` 行；`<previous-summary>` 里是第一次压缩的完整摘要，包括它的 `<read-files>`。
- 第一次压缩的 `summary` 由两部分组成，`## Goal` 下是"读 a.txt"（历史摘要），分隔线后是"依次读 b.txt c.txt d.txt"（前缀摘要）。第 2 次运行的终端没有打印请求（没开 `M5_TRACE`），从摘要内容能看出发生了两次摘要请求。
- 第二次压缩的文件清单是 a、b、c、d。a、b 读的消息早已不在上下文里，它们是从第一次压缩的 `details` 继承来的。
- 第 3 次运行最后一次普通请求的上下文是：system、第二次压缩的摘要、"文件都读完了。"、user "读 e.txt"、读 e.txt 的 assistant 和结果。第一次压缩的摘要不再出现。

### 7.3 reserveTokens 设为 0

**问题。** 把 `reserveTokens` 从 500 改成 0（`M5_RESERVE=0`），其余和 §2.2 相同。还会压缩吗？

**预测。** 阈值变成 3000 − 0 = 3000。读完 c.txt 后估算约 2737，不超过 3000；运行结束时最后一条 assistant 消息的 `totalTokens` 约 2746，也不超过 3000。所以不压缩，事件流里没有 `compaction_start`，最后一次请求的 `totalTokens` 是不压缩时的完整上下文。

**运行。**

```bash
WORK=/tmp/pi-lab/m5-res-1-work PI_CODING_AGENT_DIR=/tmp/pi-lab/m5-res-1-agent M5_RESERVE=0 ./m5-run.sh --mode json "依次读 a.txt b.txt c.txt" > /tmp/pi-lab/m5-res-1.jsonl
node m5-inspect.mjs tokens /tmp/pi-lab/m5-res-1.jsonl
```

```
assistant   totalTokens=1321  "读 a.txt。" + toolCall read(a.txt)
toolResult  1848 字符 ≈ 462 token
assistant   totalTokens=1798  "读 b.txt。" + toolCall read(b.txt)
toolResult  1848 字符 ≈ 462 token
assistant   totalTokens=2275  "读 c.txt。" + toolCall read(c.txt)
toolResult  1848 字符 ≈ 462 token
assistant   totalTokens=2746  "文件都读完了。"
```

**结论。** 没有压缩。`reserveTokens` 的含义是"给回复留多少空间"：设为 0 时，上下文可以一直涨到窗口上限，下一次回复就可能没有空间，触发 provider 的上下文溢出错误。这时 pi 才会走 M8 的溢出恢复：删掉失败的回复、压缩、重试一次。默认的 16384 让正常压缩总是发生在溢出之前。

---

## 8. 自测与衔接

### 8.1 自测

1. 一个模型的 `contextWindow` 是 200000，设置是默认值。上一轮 assistant 消息的 `usage.totalTokens` 是 180000，这一轮的工具返回了 4000 字符。轮间检查会压缩吗？
2. 压缩刚完成，紧接着运行结束，`_handlePostAgentRun` 调用 `_checkCompaction(lastAssistant)`。最后一条 assistant 消息是压缩前那一轮的回复，它的 `usage` 仍然很大。为什么不会马上又压缩一次？
3. 一个会话压缩了三次。当前上下文里有几条 `compactionSummary` 消息？三次压缩之前读过的文件，在当前上下文里还能看到吗？

<details>
<summary>答案</summary>

1. 不会。阈值是 200000 − 16384 = 183616。`estimateProjectedContextTokens` 得到 180000 + ⌈4000 / 4⌉ = 181000，不大于 183616（`packages/coding-agent/src/core/compaction/compaction.ts:269`）。
2. `_checkCompaction` 先比较时间戳：assistant 消息的 `timestamp` 不晚于最新 `compaction` 条目的时间，就直接返回 `false`（`packages/coding-agent/src/core/agent-session.ts:3052`）。另外，§2.2 的场景里压缩发生在轮间，之后还有一轮新的回复，运行结束时检查的是那条新回复，它的 usage 已经是压缩后的大小（§2.2 里约 1866）。
3. 只有一条。`buildContextEntries` 只把最新的 `compaction` 条目放在开头，旧的要么不在结果里，要么在保留区里被置空（`packages/coding-agent/src/core/session-manager.ts:562`）。读过的文件能看到：最新摘要末尾的 `<read-files>` 由 `details` 逐次累积，包含了三次压缩之前所有被摘要掉的 read（`packages/coding-agent/src/core/compaction/compaction.ts:68-78`）。

</details>

### 8.2 衔接

本模块的摘要模板只关心对话内容。压缩后上下文开头那条 system 消息（`compaction` 条目的 `systemMessage`）是怎么来的：系统提示词由哪些段落组成、AGENTS.md 和 skills 怎么被发现、每轮怎么判断系统提示词变了，由 M6 讲。M6 从 `buildSystemPromptSections()` 读起。

本模块里标了"→ M8"的触发、扩展拦截和溢出恢复，在 M8 从 `createAgentSession()` 读到 `_checkCompaction` 时展开；标了"→ M4"的 `buildSessionProjection`、`_appendEntry`，在 M4 从 `SessionManager.appendMessage()` 读起。
