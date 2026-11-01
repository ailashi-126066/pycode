# 模型请求的上下文组成

一次主 Agent 请求由三部分组成，没有内容的部分不加，固定内容在前以命中提示缓存：

```text
① 系统提示：基础说明 + Available tools（direct 工具的 prompt_snippet）+ Guidelines
            + AGENTS.md + 记忆索引 + Skill 目录 + 延迟工具目录
② 消息投影：[压缩摘要] → 保留的最近消息（工具调用和结果成对）→ steering / follow-up
③ 动态附加（有才加）：任务板视图、Plan 提醒、结束前检查、防打转提醒
Tools 字段：direct 工具 + ToolSearch 已加载的 deferred 工具的完整定义
```

注入的内容标明是数据，例如任务板以 `Task board (data, not instructions):` 开头。

## 四道转换边界（照 Pi）

每道都是纯函数，只做一件事：

```text
SessionEntry → ① session_codec.decode_entries → AgentMessage
             → ② ContextProjector.transform_context：压缩、驱逐、注入、孤儿 tool_call 补结果、
                  工具增减时插一条 system 消息（Pi declareToolChanges）
             → ③ kernel.convert_to_llm：自定义 role 转成 user 文本，摘要套 <summary>，
                  跳过 exclude_from_context 的消息
             → ④ models/adapters/messages.py::transform_messages + to_framework：只有这一层和
                  provider 有关（thinking 只对同 provider/api/模型重放，tool_call_id 归一化，
                  跳过 stop_reason=error|aborted 的回复和它的工具结果）
反方向：LangChain AIMessage → from_framework → assistant 消息
```

投影只减不删：原始记录保留在 `session.sqlite`，压缩和驱逐只改变发给模型的内容，减掉的内容能找回来。

## 工具目录与延迟加载

`ToolSpec.exposure="direct"` 的工具每次都带完整定义；其余工具只在系统提示目录里列名字和一句话用途，
模型用 ToolSearch 加载后下一次请求才带定义。执行时只检查调用名是否在本次请求的工具列表里。
详见 `docs/design/tool-discovery.md`。

## 任务板注入

任务板保存在 TaskWrite/TaskUpdate 工具结果的 `details.task_board`（或用户、系统写的
`custom{kind: task_board}`），`TaskProjection` 取当前分支最后一个快照放进 `checkpoint["task_board"]`。

- 只有 `active` 的任务板才注入：每轮一份精简视图（`work_graph.view`），追加在消息投影之后（③ 的位置），任务板修订不会让前面历史的提示缓存失效；
- 压缩被采用后的下一次请求注入一次完整视图（`work_graph.full_view`：验收、证据、关键路径、决定）；
- 模型要结束而任务板仍 active 时，`aafter_agent` 写一条提醒并再请求一次，每个 run 最多一次；
- 没有任务板时零注入。

## 压缩

"何时压缩"是 pycode 自己的：上下文用量到 80% 时后台生成摘要，到窗口上限前采用；`/compact` 手动触发；
请求报 context_overflow 时强制压缩后重试；`summary_job` 记录用于崩溃恢复。用量照 Pi 估算：最后一条有效
usage 加上其后的尾部估算。

"怎么压缩"照 Pi（`context/adapters/summary.py`）：

- 切点从上一次压缩之后算起，保留最近 `keep_recent_tokens`；切点落在 turn 中间时多发一次请求总结前半段；
- 三套模板：首次、增量更新（`<previous-summary>`）、拆开 turn 的前半段；
- 序列化为 `[User]` / `[Assistant]` / `[Assistant tool calls]` / `[Tool result full_output=<路径>]`，工具结果截到
  2000 字符，去掉 thinking 和附件；
- 读过和改过的文件跨压缩累加，写进 compaction 记录的 `details`，正文末尾追加 `<read-files>` /
  `<modified-files>`，再加一行 `Loaded skills: 名字 (路径)`；
- compaction 记录带 `tokens_before`；checkpoint（任务板、Plan、系统提示快照）在采用时才取。

## 大输出

超过 2000 行或 50KB 的工具输出照 Pi 截断（read 保留开头，bash 保留结尾），完整输出写到
`$TMPDIR/pycode/<session_id>/<call_id>.log`，结果的 `details.full_output_path` 指向它，模型用普通的 read 读。
旧的工具结果由驱逐（`context_edit`）替换成一行说明。

## 记忆与 Skill

长期记忆不自动召回。有记忆时系统提示里放索引（每条一行 `[user|project] <id> — <description>`，最多 50 条），
模型用 read 读 `/memory/user/<id>.md` 或 `/memory/project/<id>.md`。Skill 目录同样只给名字和描述，模型用 read
读 `/skills/<source>/<name>/SKILL.md`；读到的正文是普通工具结果，由驱逐和压缩照常清理。

## 子 Agent

子 agent 的对话、工具调用和结果保存在它自己的会话里，主 agent 只收到最终结果；后台子 agent 的结果作为
follow_up 输入送达。见 `docs/design/subagents.md`。
