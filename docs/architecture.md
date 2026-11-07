# 架构

pycode 的底层照 Pi（HEAD `2c8d1fe`）翻译成 Python，用 LangChain / LangGraph / Deep Agents 实现，再加上 pycode
自己的功能（后台压缩、MCP、subagent、任务板、Plan、长期记忆、快照与回退）。Pi 概念到 pycode 文件的对照见
[pi-map.md](pi-map.md)，存储见 [storage-format.md](storage-format.md)，一次请求的组成见
[context-request.md](context-request.md)。

源码物理路径以 `src/` 为根，发行包将它映射为 `pycode` 命名空间；例如 `src/agent/runtime.py` 对应 `pycode.agent.runtime`。

## 包与依赖

核心包按 kernel ← models ← context ← tools ← agent ← session 单向依赖。
业务模块为 sandbox、snapshot、memory、skills、mcp、hooks、plans、tasks、subagents、loopguard；
各模块按职责平铺，不再分 domain/ports/services/adapters。ui 和 app 在上层。

`app/composition.py` 明确创建具体服务并连接函数、工具、挂载和 framework middleware；
不使用 Feature 基类、通用注册器或按类型查找服务。AgentSession 暴露具体 memory/skills/plans/tasks 等入口。
app 通过 `SessionServices` 和 `build_run` 连接具体资源；`agent/launcher.py::RunLauncher` 管理运行的启动、恢复、暂停保留和关闭。
会话与运行分别使用 AsyncExitStack 关闭资源，初始化失败时先释放已经获得的资源。
固定模型/工具函数直接导入，不通过 RuntimeBindings 或单一实现的工厂转交。

架构脚本只检查依赖方向及跨包从包根导出导入；品牌检查独立保留。
不再维护 feature-dependencies.json、port-implementations.json，不规定 Protocol 数量。
共享 AgentTool、ToolGate、EventConsumer 保留，其他调用优先具体类型或函数。
纯类型引用允许 TYPE_CHECKING，不产生运行时反向依赖。pycode 不 import evals/scripts。

## 一次运行

1. `AgentSession.run` 校验输入、处理重复消息并写历史，直接调用 `RunLauncher.execute`。
   launcher 通过 app 提供的 `create_run` 构造 `AgentRuntime` 和 `RunContext`，随后调用 runtime。
   `AgentRuntime`（`agent/runtime.py`）拥有唯一的 `create_deep_agent` 图。
2. 循环由 LangGraph 跑，`HistoryMiddleware`（`agent/loop.py`）提供 Pi `AgentLoopConfig` 的几个钩子：
   `abefore_model`（轮次预算和运行状态）、`awrap_model_call`（准备输入、请求、保存回复）、
   `aafter_model` / `aafter_agent`（结束一轮、steering 与 follow-up、任务板结束前检查）。
   `awrap_model_call` 主体只按顺序调用 `prepare_request → request_model → save_response`；请求准备集中到
   `prepare_request`（实际工具与预算 → 历史维护 → 引导输入 → 最终消息）。`prepare_model_boundary` 只补齐、筛选、披露工具，
   calibration 只记录实际请求配置；模型记录、重试和回复校验集中在 `ModelRequestRunner`。
   `PycodeState`（`agent/context.py`）统一保存 `turn`、`phase`、`reason`，阶段转换通过纯函数校验。
   `ended_turn` 记录已发布结束事件的轮数；暂停由框架检查点的 interrupts 表达，恢复沿用图状态。
3. 工具调用由 `ToolSettlementMiddleware`（`tools/settlement.py`）包住 tools 节点：准备阶段按顺序串行
   （校验、审批一次一个、执行前钩子），执行阶段并发；有工具声明 `sequential` 时整批串行。`ToolExecutor`
   （`tools/executor.py`）负责校验、授权、明确注册的执行前后钩子和工具执行。每个调用有
   `ToolCallState`（issued → validating → awaiting_approval → running → settled），`begin` 发布开始事件，
   `save_result` 补充结果信息并交给 `settle_ready` 按顺序保存、发布结束事件。
   `awrap_tool_call` 直接组织等待、检查、`executor.execute` 和 `save_result`，没有逐层转交的执行链。
   `RunContext.history` 是唯一会话存储引用；工具直接返回 ToolResult，由 settlement 保存并返回图消息。
4. 审批用 LangGraph `interrupt` 暂停，`AgentRuntime.resume` 用 `Command(resume)` 送回答案；同一时间只有一个
   待答问题。
5. `AgentRuntime.finish` 是唯一结束出口：必要时发 `turn_end`，写 `run_end{reason}`，发 `agent_end`，触发
   `run_end` Hook。

## 转移 → 记录 → 事件 → Hook

工具结果经过执行后钩子修改后统一持久化，再发完成事件；生命周期 Hook 在明确的边界调用：

| 转移 | 记录（session.sqlite） | 事件 | Hook |
|---|---|---|---|
| 运行开始 | `custom{run_start}` | `agent_start` | `run_start` |
| 一轮开始 / 结束 | — | `turn_start` / `turn_end{turn, message_id, tool_result_ids}` | `turn_start` / `turn_end` |
| 请求模型 | — | `message_start{attempt}`、`message_update` | `pre_send` |
| 收到回复 | `message`（assistant，含 stop_reason） | `message_end{message, entry_id}` | `post_receive` |
| ToolCall issued → validating | — | `tool_execution_start` | — |
| validating → awaiting_approval | — | `needs_input`（运行停下等待时） | `permission_request` |
| 审批决定 | `custom{tool_approval}` | — | — |
| 授权通过 → running | `custom{tool_started}` | `tool_execution_update` | 执行前 `pre_tool_use` |
| → settled | `message`（tool_result，改文件时 details 带 file_snapshot） | `tool_execution_end{status, outcome}` | `post_tool_use`、`file_change`、`error` |
| 压缩采用 | `compaction` | `compaction` | `compact` |
| 子 agent 状态 | `custom{subagent_start}` | `subagent` | — |
| 运行结束 | `custom{run_end}` | `agent_end{reason}` | `run_end` |

事件共 18 种（`kernel/events.py`），每个事件信封带 `turn`、`agent_id`、`parent_call_id`。订阅者抛异常会让
本次运行失败；只有 `BrokenPipeError` 例外，禁用该订阅者并抛 `OutputClosed`。Hook 共 14 种
（`hooks/types.py`），旧名字在加载配置时报错并提示新名字；`pre_tool_use` 出错按拦截处理。

## 工具

内置工具照 Pi：read、bash、edit、write、grep、find、ls，默认并行；edit 和 write 按文件排队。其余工具：
ToolSearch、TaskWrite/TaskUpdate、ExitPlanMode、AskUserQuestion、Agent/SendMessage/StopAgent、Skill 与 MCP
工具、记忆工具。延迟加载见 [design/tool-discovery.md](design/tool-discovery.md)。

文件访问由 app 构造 CompositeBackend，虚拟路由有 `/workspace/`、`/plans/`、
`/skills/<source>/`、`/memory/user/`、`/memory/project/` 和大输出临时目录。
授权依次检查用户 gate 和实际业务 gate；任一拒绝优先，有 allow 也不能跳过后续 gate。
没有自动放行时再进入核心审批。授权后运行 hooks/snapshot/skills 等实际准备函数；被拦截不执行工具，
不运行 after。执行后 snapshot、skills、plans、hooks 按显式顺序处理结果，后者看到前者修改后的值。

## 任务板与 Plan

任务板（`kernel/work_graph.py` 的值及 `tasks/` 服务）是简单任务和 Plan 共用的模型：`Board` 和 `Step` 两个类，两张转移表，
`validate` / `apply` / `derived` 三个函数。`depends_on` 可选，写了就做环和悬空校验，依赖没完成的步骤不能开始；
同一时间只有一个 in_progress；完成要有证据：有 `check` 的验收项由系统在步骤开始之后找完全相同且成功的 bash
调用，引用的证据必须在步骤开始之后、而且是成功的调用。任务板的出口是 completed / failed / blocked
（结束运行并把问题交给用户）/ abandoned。Plan 模式只读规划、写计划文档，末尾的 `pycode-tasks` 代码块经用户
审批后变成执行任务板，执行中只能追加或跳过步骤。TUI `/tasks` 查看，`/tasks clear` 放弃。

## Subagent

照 Claude Code：Agent / SendMessage / StopAgent，readonly 或 worktree 模式，前台或后台。每个子 agent 是一个完整
的 `AgentSession`，管理层是 `subagents/manager.py`。见 [design/subagents.md](design/subagents.md)。

## 防打转与预算

`loopguard/loopguard.py` 按指纹计数：动作（工具名 + 参数哈希）加结果（是否出错、error_code、
exit_code、把连续数字换成 `#` 后的输出哈希）。阈值默认 3：
- 会改东西或执行命令的工具：同一指纹出现 3 次；
- 只读工具（read、grep、find、ls、ToolSearch 等）：同一指纹连续出现 3 次，中间没有其他工具调用；
- 文件改回旧内容：工作区回到某个出现过 3 次的快照。

失败的调用不计数，交给 `repeated_tool_failure`。开始新步骤（TaskUpdate 设为 in_progress）和压缩被采用后，
重复计数清零。第一次检测到时注入提醒，建议用一条带超时的循环命令等待外部状态。再出现时，`abefore_model` 用
LangGraph `interrupt()` 暂停运行（`needs_input`，问题形状同 AskUserQuestion），问用户"是在正常等待吗？"：
选继续，计数清零接着跑；选停止，以 `loop_detected` 结束，active 任务板同时设为 failed。非交互模式下以退出码 2
结束，可以 `pycode resume --answers` 回答。子 agent 在 TUI 下把问题转到主会话，在非交互模式下直接以
`loop_detected` 结束。阈值只能在嵌入时通过 `AppConfig.loop_repeat_threshold` 配置（必须 ≥ 1），没有 CLI 参数。
最大轮次 `--max-turns`（默认 100），到达时
`turn_limit`。

## 记忆

AGENTS.md（`~/.pycode/AGENTS.md`，加 `--trust-project` 时还有项目根目录的）经 Deep Agents `MemoryMiddleware`
每次放进系统提示。长期记忆一条一个 Markdown 文件，系统提示里放索引，模型用 read 按需读取；模型用 LangMem 的
`manage_*_memory` 写入（单条 ≤2000 字，每作用域 ≤50 条，疑似密钥拒绝）。后台整理只在压缩被采用后跑一次，
另有手动 `/memory extract`。

## 沙箱

Seatbelt profile 默认拒绝：读允许工作区、系统目录、临时目录、解释器前缀和 git 全局配置，拒绝 ~/.ssh、~/.aws、
~/.gnupg、~/.config、~/.pycode 和 .env 文件；写只允许工作区（.git、.pycode、.env 除外）。`on-auto` 只自动批准
每一段都通过白名单（`sandbox/policy.py::auto_approvable`）的命令，其余走正常审批。
