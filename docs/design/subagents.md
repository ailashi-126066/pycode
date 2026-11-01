# Subagents（11.1 第 10 节，替代 Team）

照 Claude Code 的 subagent 做，不做 agent teams（子 agent 之间不通信，不共享任务板）。

- 工具：`Agent(description, prompt, mode: readonly|worktree, background, max_turns)`、
  `SendMessage(agent_id, message)`、`StopAgent(agent_id)`，都在 `src/pycode/subagents/tools.py`。
- 每个子 agent 是一个完整的 `AgentSession`，由 `app/composition/subagents.py` 打开，有自己的历史和
  checkpoint，内部照常用 `create_deep_agent` 运行。主 agent 只看到最终结果。
- 管理层 `SubagentManager` 是自己写的：Deep Agents 0.7.32 的 `task` 子代理是同步一次性调用，
  不支持后台、恢复、停止和 worktree；`AsyncSubAgentMiddleware` 支持后台，但要连 Agent Protocol
  服务器，也没有 worktree。
- 状态 `SubagentState`：`running / waiting / completed / partial / failed / cancelled`
  （`subagents/types.py` 的 StateMachine）。达到 `max_turns` 时为 `partial`，可以用 `SendMessage` 继续。
- 前台调用等子 agent 结束；父运行取消或失败时前台子 agent 一起停止，结局 `cancelled`。
  `background=true` 立即返回，不受父运行影响；完成后结果作为 durable `follow_up` 输入交给正在运行的
  主 agent，主会话空闲时则开一个新运行送达。
- 审批：子 agent 的待审批项出现在主会话 `inspect()["subagents"]["approvals"]`，TUI 弹出审批面板，
  用 `answer_agent` 回答。**非交互模式（`pycode run` / `-p`）没人能回答，子 agent 使用
  `ReadOnlyToolGate`，需要审批的调用直接得到 `approval_required` 错误结果，子 agent 继续运行。**
- 事件：子 agent 的每个事件包成一条 `subagent` 事件转发，`data = {status, description, event}`，
  信封的 `agent_id`、`parent_call_id` 指向子 agent 和启动它的 Agent 调用；状态变化时不带 `event`。
- worktree 模式：在 `<项目状态目录>/worktrees/<agent_id>` 建临时 worktree（分支 `pycode/agent/<id>`），
  命令必须在可用的系统沙箱里运行。每用 TaskUpdate 完成一个步骤，就在 worktree 里提交一次。
  结束时没有改动就删除 worktree 和分支；有改动就保留，结果里写明分支和路径，由主 agent 用 bash
  执行 `git merge <分支>`（走正常审批），合并后跑相关测试；冲突或测试失败时自己修、或用 SendMessage
  让子 agent 在自己的分支上修，修不好就撤销合并并告诉用户。没有自动合并，也没有专门的冲突处理代码。
- 并发上限 `--max-agents`（`AppConfig.max_agents`，1–4）。
- 旧会话里的 `team_*` 记录在读取时跳过（`session_codec.decode_entries`）。
