# 当前存储格式

## 两种框架存储的职责

每个会话目录包含 `session.sqlite` 与 `execution.sqlite`。

- `session.sqlite` 使用 LangGraph 官方 `SqliteStore`。`entries` namespace 按记录 ID 保存不可变 SessionEntry 与追加顺序；`session/position` 保存 SessionHeader、当前 leaf_id 和 revision。
- 新记录与 position 使用一次 `Store.batch` 事务提交。完整记录树不因选择分支、摘要或清理投影而删除。
- `execution.sqlite` 使用 `AsyncSqliteSaver`，按 `session_id:run_id` 保存 State、执行位置、interrupt 和 pending writes。图以 durability="sync" 执行。
- Store 接收外部输入和邮箱记录；不向正在运行的图并发调用 update_state。
- 大输出、摘要对象和 Git 快照仍为独立文件，Store 保存稳定引用。

不再读取 entries.jsonl 或 position.json，不自动转换旧文件。旧文件不会被覆盖或删除。

## 记录结构与业务语义

SessionEntry 保留 id、parent_id、type、timestamp、data、message、selection、run_id、operation_id。完整原文和各分支保留在 Store 中，active leaf 只选择当前路径。message ID 去重、分支归属、上下文编辑目标与工具配对规则保持不变。

主要记录类型：message、input_queued/input_expired、model_change、thinking_level_change、system_prompt、tool_set_change、skill_catalog/skill_load/skill_install、mcp_catalog、memory_load、plan_state、hook_call/hook_result/hook_context、compaction、context_edit、team_transition，以及 custom 运行/快照/回退记录。

## 执行恢复与审批

单 Agent 的任务更新随助手消息一起提交：metadata.pycode_task_change 保存已校验的任务变更，pycode_task_action 保留模型的提案，正文只保存用户可读回复。TaskProjection 从当前分支生成 task_state，压缩快照和图 State 保留其派生视图。没有另一份可独立写入的 todos 或任务文件；Team 的任务板仍使用 team_transition。

run_start/run_wait/run_end 保存业务运行身份。打开会话不会自动执行任务；已有 checkpoint 时保留未结束运行，显式 resume 重建图和模型/工具资源。审批由原生 interrupt 的 response_schema 校验操作 ID 和决定值。

非只读工具实际调用前记录 tool_started。进程退出后，已开始但没有保存结果的操作记录 tool_interrupted；没有开始的审批不会被误标记成已经执行。已经保存的结果参与恢复，即使尚未成为完整 checkpoint，也不重复执行。

读文件版本凭据随工具结果保存。锁、Task、模型连接和正在运行的进程不保存到 State。

## 摘要、清理和分支

后台摘要预生成与采用规则不变，compaction 保存摘要和保留边界；context_edit 只改变发给模型的投影。原始消息不修改。分支通过 Store position 改变活动叶子，fork 将选定路径写到新会话，不复制未完成执行的 checkpoint。

## Team 与输入队列

TeamState 的 agents/tasks/mailboxes 业务变化继续通过 team_transition 保存，但底层为 Store。邮箱 ACK 仍表示内容已可靠接收，不代表完成。输入队列在运行期间独立持久化，在图边界消费。

## 会话列表、文件引用与 GC

会话列表读取 session.sqlite 的 header/position。文件输出使用 output_id 与 source_entry_id 关联。GC 遍历 Store 中所有分支记录收集引用；会话隔离移动 session.sqlite 与 execution.sqlite，保留原有 30 天隔离和恢复规则。

## 其他存储

模型请求与计费使用 runtime.sqlite。长期记忆的 JSON 文件及 changes.jsonl、存储/worktree 清理审计日志属于独立用途，未改为 Agent 执行 State。评测结果仍可使用 results.jsonl；会话证据导出改为 .session.sqlite.gz，不把数据库按文本行截断。

## 任务范围内的记忆参考

memory_load 保存 task_id、input_id、records 和 loaded_at；同任务复用，切换重新筛选并替换，完成或取消后追加空批次。记忆投影也会根据已保存的任务变更撤下过期批次。压缩保留 memories 与 memory_task_id，长期 JSON 记忆库不随任务结束删除。
