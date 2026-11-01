# 架构

pycode 的底层照 Pi（HEAD `2c8d1fe`）翻译成 Python，用 LangChain / LangGraph / Deep Agents 实现，再加上 pycode
自己的功能（后台压缩、MCP、subagent、任务板、Plan、长期记忆、快照与回退）。Pi 概念到 pycode 文件的对照见
[pi-map.md](pi-map.md)，存储见 [storage-format.md](storage-format.md)，一次请求的组成见
[context-request.md](context-request.md)。

## 包与依赖

`src/pycode/` 下按功能分包：kernel（消息、事件、记录、状态机、work_graph）、models（LangChain provider 适配）、
context（投影、压缩、驱逐、任务板投影）、agent（Deep Agents 图和 middleware）、tools（注册、执行、结算、
发现）、sandbox（Seatbelt、bash）、snapshot（Git 快照）、session（SQLite 会话、恢复、回退）、plans（Plan 与
任务板工具）、memory、skills、mcp、hooks、subagents、ui（Textual TUI、控制台）、app（CLI、配置、组装）。

分层包是 domain/ports/services/adapters/module；skills、mcp、hooks、subagents 是扁平包（types/contracts）。
跨包只能经包的 `__init__` 导出。允许的依赖在 `docs/design/feature-dependencies.json`，每个 Protocol 的生产实现
在 `docs/design/port-implementations.json`，两者都由 `scripts/check_architecture.py` 强制。pycode 不 import
evals 和 scripts。

组装只有一个入口 `app/bootstrap.py::compose_application`，返回带 `ApplicationModules` 的 Application；CLI、TUI、
评测都用它。`app/composition/` 连接各模块，`app/lifecycle/` 管会话和运行的资源生命周期。

## 一次运行

1. `AgentSession.run` 写用户消息，`ConfiguredRuntimeLauncher` 组装 `AgentRuntime`（`agent/adapters/agent.py`，
   唯一的 `create_deep_agent` 图）和 `RunContext`。
2. 循环由 LangGraph 跑，`HistoryMiddleware`（`agent/adapters/history.py`）提供 Pi `AgentLoopConfig` 的几个钩子：
   `abefore_model`（维护上下文、轮次预算、防打转）、`awrap_model_call`（投影、注入、请求、保存回复）、
   `aafter_model` / `aafter_agent`（结束一轮、steering 与 follow-up、任务板结束前检查）。
   `RunState`（`agent/domain/run_state.py`）显式记录 `starting → model_turn ⇄ tool_turn → awaiting_input →
   verifying → finished`。
3. 工具调用由 `ToolSettlementMiddleware`（`tools/adapters/settlement.py`）包住 tools 节点：准备阶段按顺序串行
   （校验、Hook、审批一次一个），执行阶段并发；有工具声明 `sequential` 时整批串行。`ToolExecutor`
   （`tools/services/tool_executor.py`）负责校验、`pre_tool_use`、授权、快照和执行。每个调用有
   `ToolCallState`（issued → validating → awaiting_approval → running → settled），`begin` / `settle` 是唯一出入口。
4. 审批用 LangGraph `interrupt` 暂停，`AgentRuntime.resume` 用 `Command(resume)` 送回答案；同一时间只有一个
   待答问题。
5. `AgentRuntime.finish` 是唯一结束出口：必要时发 `turn_end`，写 `run_end{reason}`，发 `agent_end`，触发
   `run_end` Hook。

## 转移 → 记录 → 事件 → Hook

先持久化，再发事件，最后触发 Hook：

| 转移 | 记录（session.sqlite） | 事件 | Hook |
|---|---|---|---|
| 运行开始 | `custom{run_start}` | `agent_start` | `run_start` |
| 一轮开始 / 结束 | — | `turn_start` / `turn_end{turn, message_id, tool_result_ids}` | `turn_start` / `turn_end` |
| 请求模型 | — | `message_start{attempt}`、`message_update` | `pre_send` |
| 收到回复 | `message`（assistant，含 stop_reason） | `message_end{message, entry_id}` | `post_receive` |
| ToolCall issued → validating | — | `tool_execution_start` | `pre_tool_use` |
| validating → awaiting_approval | — | `needs_input`（运行停下等待时） | `permission_request` |
| 审批决定 | `custom{tool_approval}` | — | — |
| → running | `custom{tool_started}` | `tool_execution_update` | — |
| → settled | `message`（tool_result，改文件时 details 带 file_snapshot） | `tool_execution_end{status, outcome}` | `post_tool_use`、`file_change`、`error` |
| 压缩采用 | `compaction` | `compaction` | `compact` |
| 子 agent 状态 | `custom{subagent_start}` | `subagent` | — |
| 运行结束 | `custom{run_end}` | `agent_end{reason}` | `run_end` |

事件共 18 种（`kernel/domain/events.py`），每个事件信封带 `turn`、`agent_id`、`parent_call_id`。订阅者抛异常会让
本次运行失败；只有 `BrokenPipeError` 例外，禁用该订阅者并抛 `OutputClosed`。Hook 共 14 种
（`hooks/types.py`），旧名字在加载配置时报错并提示新名字；`pre_tool_use` 出错按拦截处理。

## 工具

内置工具照 Pi：read、bash、edit、write、grep、find、ls，默认并行；edit 和 write 按文件排队。其余工具：
ToolSearch、TaskWrite/TaskUpdate、ExitPlanMode、AskUserQuestion、Agent/SendMessage/StopAgent、Skill 与 MCP
工具、记忆工具。延迟加载见 [design/tool-discovery.md](design/tool-discovery.md)。

文件访问走 `tools/module.py::composite` 构造的 `CompositeBackend`，虚拟路由有 `/workspace/`、`/plans/`、
`/skills/<source>/`、`/memory/user/`、`/memory/project/` 和大输出临时目录；write/edit 只能写 `/workspace/` 和
`/plans/`。授权链是 Interrupt（或 ReadOnly）→ Memory → Plan，嵌入方的 `AppConfig.tool_gate` 和 Skill 的
allowed-tools 范围包在链外层，先判断再交给默认链。

## 任务板与 Plan

任务板（`kernel/domain/work_graph.py`）是简单任务和 Plan 共用的模型：`Board` 和 `Step` 两个类，两张转移表，
`validate` / `apply` / `derived` 三个函数。`depends_on` 可选，写了就做环和悬空校验，依赖没完成的步骤不能开始；
同一时间只有一个 in_progress；完成要有证据：有 `check` 的验收项由系统在步骤开始之后找完全相同且成功的 bash
调用，引用的证据必须在步骤开始之后、而且是成功的调用。任务板的出口是 completed / failed / blocked
（结束运行并把问题交给用户）/ abandoned。Plan 模式只读规划、写计划文档，末尾的 `pycode-tasks` 代码块经用户
审批后变成执行任务板，执行中只能追加或跳过步骤。TUI `/tasks` 查看，`/tasks clear` 放弃。

## Subagent

照 Claude Code：Agent / SendMessage / StopAgent，readonly 或 worktree 模式，前台或后台。每个子 agent 是一个完整
的 `AgentSession`，管理层是 `subagents/manager.py`。见 [design/subagents.md](design/subagents.md)。

## 防打转与预算

`tools/domain/progress_guard.py` 检测"同一动作在工作区没变时重复"和"文件改回旧内容"，第一次提醒，再出现就以
`loop_detected` 结束运行，active 任务板同时设为 failed。最大轮次 `--max-turns`（默认 100），到达时
`turn_limit`。

## 记忆

AGENTS.md（`~/.pycode/AGENTS.md`，加 `--trust-project` 时还有项目根目录的）经 Deep Agents `MemoryMiddleware`
每次放进系统提示。长期记忆一条一个 Markdown 文件，系统提示里放索引，模型用 read 按需读取；模型用 LangMem 的
`manage_*_memory` 写入（单条 ≤2000 字，每作用域 ≤50 条，疑似密钥拒绝）。后台整理只在压缩被采用后跑一次，
另有手动 `/memory extract`。

## 沙箱

Seatbelt profile 默认拒绝：读允许工作区、系统目录、临时目录、解释器前缀和 git 全局配置，拒绝 ~/.ssh、~/.aws、
~/.gnupg、~/.config、~/.pycode 和 .env 文件；写只允许工作区（.git、.pycode、.env 除外）。`on-auto` 只自动批准
每一段都通过白名单（`sandbox/domain/command_policy.py::auto_approvable`）的命令，其余走正常审批。
