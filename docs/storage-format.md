# 当前存储格式

## 会话目录

每个会话目录包含 `session.sqlite` 与 `execution.sqlite`。

- `session.sqlite` 用 LangGraph 的 `SqliteStore`：`entries` namespace 按记录 ID 保存不可变的 `SessionEntry`
  和追加序号；`session/position` 保存 `SessionHeader`、当前 `leaf_id` 和 revision。新记录与 position 在一次
  `Store.batch` 事务里提交。记录树不因切分支、压缩或驱逐而删除。
- `execution.sqlite` 用 `AsyncSqliteSaver`，按 `session_id:run_id` 保存图状态、interrupt 和 pending writes，
  图以 `durability="sync"` 执行。
- 模型请求与计费在项目目录的 `runtime.sqlite`。摘要对象、Git 快照、Plan 文件是独立文件，记录里保存引用。

## 记录（`kernel/domain/history.py`）

`SessionEntry(id, parent_id, type, timestamp, data, message, selection, run_id, operation_id)`。
读取时 `session_codec` 严格校验，未知 type、未知 custom kind、未知字段都报 `invalid_session_entry`。

顶层 `EntryType` 只放投影或会话本身要读的类型：

| type | 用途 |
|---|---|
| `message` | user / assistant / tool_result 消息 |
| `input_queued` / `input_expired` | 运行中排队的 steer / follow_up 输入 |
| `custom_message` | 系统写给模型的文字（如结束前检查提醒） |
| `compaction` | 摘要、`first_kept_entry_id`、`tokens_before`、`details{read_files, modified_files}`、checkpoint |
| `context_edit` | 旧工具结果驱逐，只改投影 |
| `fork_context` | fork 时带过来的上下文 |
| `hook_context` | 需要进上下文的 Hook 输出 |
| `model_change` / `thinking_level_change` / `tool_set_change` | 模型、思考级别、可见工具集变化 |
| `system_prompt` | 系统提示快照 |
| `plan_state` | Plan 模式状态 |
| `mcp_catalog` / `skill_catalog` | MCP 与 Skill 目录快照 |
| `session_metadata` | 会话标题 |

不影响模型上下文的记录都是 `custom{kind}`（`CustomKind`）：`run_start` / `run_resume` / `run_wait` / `run_end`、
`tool_started`、`tool_approval`、`file_snapshot`、`tool_eviction_audit`、`summary_job`、`compact_request`、
`revert_started` / `revert_completed` / `revert_recovery`、`skill_created` / `skill_updated` / `skill_install`、
`subagent_start`、`task_board`、`plan_decision`。

旧会话里已删除的类型在读取时跳过，子记录接到被跳过记录的父节点上：`team_*`、`hook_call`、`hook_result`、
`memory_load`、`memory_recall_error`、`plan_review`、顶层的 `plan_decision` / `skill_install`、`skill_load`、
`session_deleted`、`session_restored`。旧消息格式（`text`/`thinking`/`tool_calls` 字段）在 `upgrade_entry`
里单向升级为有序内容块。

## 消息

`Message.content` 是字符串或有序块（`text` / `thinking` / `tool_call`）。`stop_reason` 取 `stop`、`tool_use`、
`length`、`error`、`aborted`。模型出错或被取消时，部分回复也保存为 `stop_reason=error|aborted` 的
assistant 消息（`error` 字段存错误码），发给模型时跳过它和它的工具结果。thinking 一直保存，只在发给同一
provider/api/模型时重放。

## 工具调用

- `tool_started`：调用真正开始执行时写，`data={kind, replay}`；
- `tool_approval`：审批决定，`data={kind, decision, spec_hash}`，`operation_id` 在记录字段上；
- 工具结果是 `role=tool_result` 的消息，`ToolResult` 带 `is_error`、`error_code`、`exit_code`、`details`。
  没执行就结束的调用得到 `tool_abandoned`；开始了但没有结果的调用得到 `tool_interrupted`
  （可能已有副作用）。重开进程时 `recover_conversation` 给未配对的调用补这两种结果，并写
  `run_end{reason}`。
- 大输出的完整内容写在 `$TMPDIR/pycode/<session_id>/<call_id>.log`，`details.full_output_path` 指向它；
  临时目录被系统清理后，超出预览的部分会丢失。

## 运行

`run_start` / `run_wait` / `run_end` 记录运行身份。`run_end.data = {kind, input_id, input_ids, checkpoint_id,
reason, final_entry_id, error_code?}`，`reason` 是 `RunEndReason`（completed、turn_limit、loop_detected、
needs_input、model_error、aborted、interrupted、error）。审批等待用 LangGraph `interrupt`，答案经
`Command(resume)` 送回。

## 任务板

任务板快照放在 TaskWrite/TaskUpdate 结果的 `details.task_board`（`plain_data(Board)`）。用户或系统改的
（`/tasks clear`、loop_detected 时设为失败）写 `custom{kind: task_board, task_board}`。当前分支最后一个
快照就是当前任务板；没有单独的任务文件。

## 存储清理

`pycode storage gc` 从所有会话记录出发标记仍被引用的摘要、快照和 Plan 文件，未被引用的直接删除；
`--dry-run` 只列出决定。`pycode storage delete-session ID` 确认后直接删除会话目录，会话正被打开时报
`session_busy`。没有隔离区，删除不能撤销。长期记忆目录不属于清理范围。

## 其他

长期记忆是 `~/.pycode/memory/<id>.md` 与 `<项目>/.pycode/memory/<id>.md`（frontmatter 有 description、updated），
另有 `settings.json`。评测结果可以是 `results.jsonl`；会话不再使用 JSONL 文件。
