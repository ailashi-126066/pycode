# pycode 第二轮重构：结构精简（设计确认稿）

- 基线：分支 `refactor-structure-slim`，本地 main `457c8b9`。
- 依据：11.1.md 第 21、23、32、33.2、34、41 节；`.agents/tasks/pycode-overdefensive-audit-2.md`（下称"审计 2"，行号基于 `5286c1b`，已偏移，实施时按代码内容重新定位）；`.agents/tasks/pycode-overcomplexity-audit-2026-11-01.md`（下称"复杂度审计"）；Pi 源码 `/tmp/pi-research`（HEAD `2c8d1fe`）。
- 本文只做设计，不改代码。标【已确认】的地方需要用户拍板，汇总见第 11 节。

## 0. 现状数字（457c8b9）

| 项 | 数值 |
|---|---|
| `src/pycode` 文件数 / 行数 | 321 个 `.py` / 26463 行（其中 46 个空 `__init__.py`） |
| 类数 | 379 |
| `Protocol` 子类（AST 统计） | 79。11.1 里写的"93"是更早基线的数字；`docs/design/port-implementations.json` 登记了 67 个 |
| `tests` | 234 个 `.py` |

最终报告（第 32.5 节第 3 条）按同样口径统计文件数、类数、Protocol 数和行数。

## 1. 总体方案

核心保持小：`kernel/` 只放纯数据、纯函数和 `Feature` 契约；`agent/ tools/ models/ context/ session/` 是核心包，只认识 `Feature` 接口，不认识任何具体功能模块。10 个可选功能模块（`sandbox memory skills mcp hooks plans tasks subagents snapshot loopguard`）各自实现 `Feature`，在 `app/composition.py` 的一个列表里组装。模块之间不互相 import，配合只靠事件和 kernel 里的共享类型。`ui/` 和 `app/` 在最上层，可以 import 功能模块的包根 API。

依赖方向（第 32.4 节）：

```
kernel  ←  models ← context ← tools ← agent ← session   ←  10 个功能模块  ←  ui ← app
          （核心包之间按这个顺序单向依赖）                （功能模块之间互不依赖）
```

技术栈不变，本轮锁定：Python 3.12、LangChain 1.x / LangGraph / Deep Agents 0.7.x（中间件、checkpointer、`interrupt`/`Command(resume)`、`CompositeBackend`）、langchain-openai / -anthropic / -deepseek、LangMem、Textual、fastmcp / langchain-mcp-adapters、jsonschema、pytest、ruff、pyright。不引入新依赖。

## 2. Feature 契约（`kernel/feature.py`）

### 2.1 接口

`Feature` 是一个普通基类（不是 Protocol），所有方法都有默认空实现，模块只覆盖自己需要的。实例按会话创建（每个 `AgentSession` 一组），`open` 时拿到会话范围的依赖，每次 run 再收到一个 `RunScope`。

```python
# Pi 没有，pycode 新增；对应 Pi 扩展系统 pi.on() 与 AgentLoopConfig 的回调（packages/agent/src/types.ts）
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, TypedDict

@dataclass(frozen=True)
class SessionScope:            # open() 收到；会话级只读依赖
    session_id: str
    workspace: Path
    user_data: Path
    history: SessionStore      # 核心 session 包的具体类，经 kernel 里的类型别名引用，见 2.3
    events: EventStream
    read_only: bool
    agent_id: str              # "lead" 或子 agent 名

@dataclass(frozen=True)
class RunScope:                # 每次 run 收到
    session: SessionScope
    run_id: str
    events: EventStream        # 本次 run 的事件流

class BeforeToolCallResult(TypedDict, total=False):   # Pi: BeforeToolCallResult
    block: bool
    reason: str

class AfterToolCallResult(TypedDict, total=False):    # Pi: AfterToolCallResult（已存在，从 tools 移来）
    text: str
    details: dict[str, Any]
    is_error: bool

class ToolGate(Protocol):      # 有多个实现，保留接口，见第 4 节
    async def authorize(self, run: RunScope, spec: ToolSpec, call: ToolCall) -> Literal["allow"] | None: ...
    # None = 没有意见；"allow" = 不需要审批；拒绝就抛 ToolError

class Feature:
    name: ClassVar[str]
    checkpoint_keys: ClassVar[frozenset[str]] = frozenset()

    # 生命周期
    async def open(self, session: SessionScope) -> None: ...
    async def prepare_run(self, run: RunScope) -> None: ...
    async def aclose(self) -> None: ...

    # 组装一次 run（agent / tools 在构建图之前读取）
    def tools(self, run: RunScope) -> tuple[AgentTool, ...]: return ()
    def mounts(self, run: RunScope) -> Mapping[str, object]: return {}        # 前缀 -> Deep Agents backend
    def gates(self, run: RunScope) -> tuple[ToolGate, ...]: return ()
    def middleware(self, run: RunScope) -> tuple[object, ...]: return ()       # LangChain AgentMiddleware；kernel 不 import 框架，所以写 object

    # 上下文与每次模型请求
    def prompt_sections(self, session: SessionScope) -> tuple[str, ...]: return ()
    def contribute(self, snapshot: SessionSnapshot, path: tuple[SessionEntry, ...],
                   checkpoint: Mapping[str, Any]) -> ProjectionContribution | None: return None
    def transform_context(self, run: RunScope, messages: tuple[Message, ...]) -> tuple[Message, ...]: return messages
    def select_tools(self, run: RunScope, specs: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]: return specs

    # 工具调用
    async def before_tool_call(self, run: RunScope, spec: ToolSpec, call: ToolCall) -> BeforeToolCallResult | None: return None
    def after_tool_call(self, run: RunScope, call: ToolCall, result: ToolResult) -> AfterToolCallResult | None: return None

    # 事件
    async def on_event(self, event: Event) -> None: ...
```

另外两个共享的类型别名（不是接口），放在同一文件：

```python
# 跑一条受沙箱约束的命令；sandbox 提供实现，hooks 通过组装注入使用
CommandRunner = Callable[[tuple[str, ...], str, Mapping[str, str] | None, float], Awaitable[ToolResult]]
```

`ProjectionContribution`（现在在 `context/domain/projection.py`）移到 kernel，因为 `contribute` 的返回值要被功能模块构造。

### 2.2 和第 32.3 节的差别【已确认】

第 32.3 节列了 7 个成员：`name、tools、prompt_sections、transform_context、gates、on_event、aclose`。按现有代码逐个模块对过，下面 7 个多出来的成员都有具体的使用方，去掉任何一个，就得在核心里给某个模块留特例：

| 新增成员 | 谁要用 | 取代的现有机制 |
|---|---|---|
| `checkpoint_keys` + `contribute` | tasks（`task_board`）、plans（`plan`）、mcp（`mcp`） | `ProjectionContributor` Protocol 及 `history_contributors`。压缩后要靠 checkpoint 把这些状态延续下去，`transform_context` 是每次请求的消息变换，存不了 checkpoint |
| `open` | memory 初始化、skills 建索引、mcp 连接、hooks 的 `session_start`、plans 初始化 | `open_agent_session` 里的逐个模块特例代码 |
| `prepare_run` | mcp 刷新工具目录、skills 扫描目录 | `RuntimeBindings.before_model`、`SkillsSession.prepare_run`、`MCPSession.prepare_run` |
| `mounts` | skills `/skills/`、memory `/memory/`、plans `/plans/` | `runtime.py` 里手写的 `routes` 拼装 |
| `middleware` | skills（Deep Agents `SkillsMiddleware`）、loopguard（`abefore_model` 里 `interrupt()` 问用户） | `FrameworkExtension` Protocol |
| `select_tools` | plans（规划模式只给只读工具）、skills（allowed-tools 范围） | `filter_model_tools` 里的 plans 特例、`model_filters` |
| `before_tool_call` / `after_tool_call` | snapshot（执行前后拍快照，写 `details.file_snapshot`）、hooks（`pre_tool_use` 拦截）、skills（SKILL.md 读取标记）、plans（写 plan 文件后更新 `document_exists`） | `ToolExtension`、`ToolHooks`、`ToolExecutor.snapshots`、`settlement.py` 里的 plans 特例 |

`before_tool_call` / `after_tool_call` 和 Pi `AgentLoopConfig.beforeToolCall` / `afterToolCall` 同名同义；`transform_context` 和 Pi `transformContext` 一样是"消息进、消息出"。

### 2.3 核心在哪里调用每个成员

| 成员 | 调用方（核心包） | 时机与顺序 |
|---|---|---|
| `open` / `aclose` | `app/composition.py` | 打开会话时按列表顺序 `open`，每个都 `stack.push_async_callback(f.aclose)`，关闭按逆序（`AsyncExitStack`） |
| `prepare_run` | `agent`：`AgentRuntime.run/continue_task` 构图之前 | 列表顺序，逐个 await |
| `tools` / `mounts` / `middleware` | `agent`：构图时汇总 | 列表顺序；工具名重复由 `ToolRegistry` 报 `duplicate_tool`（已有） |
| `gates` | `tools`：`ToolExecutor` 的授权步骤 | 先跑全部功能模块的 gate（列表顺序），任一抛 `ToolError` 即拒绝；有一个返回 `"allow"` 就跳过审批；否则进核心审批门（`InterruptToolGate` 或 `ReadOnlyToolGate`）。用户配置的 `AppConfig.tool_gate` 排在功能模块之前 |
| `prompt_sections` | `context`：`PromptService` 组装系统提示 | 列表顺序拼接，空串跳过 |
| `contribute` | `context`：`ContextProjector.transform_context` | 列表顺序；只能写自己的 `checkpoint_keys` |
| `transform_context` | `agent`：`HistoryMiddleware.awrap_model_call`，在投影之后、`convert_to_llm` 之前 | 列表顺序串联。取代现在的 `task_board_messages`、`subagent_messages`、`loop_reminder` |
| `select_tools` | `agent`：`filter_model_tools` | 列表顺序串联，在 read_only 过滤之后 |
| `before_tool_call` | `tools`：校验、授权之后，执行之前 | 列表顺序；任一返回 `block=True` 即生成 `hook_rejected`/对应错误结果，不执行 |
| `after_tool_call` | `tools`：`settlement` 里 `finalize_result`，持久化之前，只对实际执行过的调用 | 列表顺序，后者看到前者覆盖后的结果 |
| `on_event` | `kernel.EventStream.emit` | 功能模块排在 UI 消费者之前，按列表顺序 await |

核心对功能模块的引用全部经过 `features: tuple[Feature, ...]`；`RunContext` 里的 `plans`、`hooks`、`subagent_view`、`progress_guard`，`ToolExecutor.snapshots`，`ToolSettlementMiddleware.tool_extensions/native_bindings` 全部删除。

UI 和 CLI 需要调用具体模块的命令（`/memory`、`/tasks`、`/agents`、`/revert`、`skill install`……），通过 `AgentSession.feature(MemoryFeature) -> MemoryFeature | None` 按类型取出实例（模块没装时返回 None，UI 隐藏对应命令）。`AgentSession` 不再有 `memory_*`、`skill_*`、`task_board`、`revert`、`answer_agent` 等转发方法。

### 2.4 错误处理

| 操作 | 失败情况 | 可恢复？ | 调用方收到 | 日志 |
|---|---|---|---|---|
| `open` | 配置文件非法（hooks.json、mcp.json）、I/O 错误 | 致命（会话打不开） | 原异常；`AsyncExitStack` 先逆序关掉已经 open 的模块 | 不记，由 CLI 错误边界打印 |
| `prepare_run` | MCP 服务器连不上 | 照现有 MCP 行为：在 MCP 边界里把该服务器标为不可用，不抛出 | — | 现有 warning 不变 |
| `prepare_run` 其他异常 | 编程错误 | 致命 | run 以 `error` 结束，写 `run_end` | 不另记 |
| `gates` | `ToolError` | 可恢复 | 模型收到错误结果（`permission_denied`、`plan_mode_read_only` 等现有错误码） | 不记 |
| `before_tool_call` | 返回 `block` | 可恢复 | 模型收到错误结果 | 不记 |
| `before_tool_call` 抛异常 | hooks 自己的命令失败已在 hooks 内部转成 `block`（fail-closed，第 26 节 #3）；其余异常是编程错误 | 致命 | run 以 `error` 结束 | 不另记 |
| `after_tool_call` / `transform_context` / `select_tools` / `contribute` | 编程错误 | 致命 | run 失败 | 不另记 |
| `on_event` | 任意异常 | 致命，第 3.2 节：订阅者失败让 run 失败；`BrokenPipeError` 例外（已有） | run 失败 | 不另记 |
| `aclose` | 任意异常 | — | `AsyncExitStack` 跑完所有回调，再把异常抛给关闭方（多个时按 stdlib 语义链在 `__context__` 上） | 不另记 |

契约里的输入都来自 pycode 自己，按第 21 节不做运行时校验。两个不变量用单元测试守住，不写运行时检查：功能模块 `name` 唯一、`checkpoint_keys` 互不重叠（审计 2 C8 把这两处运行时检查挪进测试）。

### 2.5 可测试性

- 契约本身：`tests/kernel/test_feature.py` 用一个记录调用顺序的假模块，验证所有默认实现是空操作、`gates` 的组合规则（拒绝优先、`allow` 跳过审批）。
- 核心调用点：`tests/agent`、`tests/tools`、`tests/context` 各自用假模块测"在什么时机、按什么顺序调用"。
- 每个功能模块：在 `tests/<模块>/` 里直接实例化 `XxxFeature`，用真实 kernel 类型、临时目录和真实 `SqliteSessionStore`（E4 之后 history 必填）测试，不需要组装整个应用。
- 组装：`tests/app/test_composition.py` 测"去掉列表里一行，对应工具、命令、挂载都消失，其余照常"。

## 3. 模块归类与迁移后的文件清单

每个文件第一行写 `# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/<路径>`，或者 `# Pi 没有，pycode 新增`（来自 OpenCode/MewCode 的写成 `# Pi 没有，pycode 新增；参照 <出处 URL>`，SHA-256 行保留在第二行）。不保留旧路径的 re-export，不加薄封装；包内不定义 Protocol。

### 3.1 核心

| 包 | 迁移后的文件 | 来源（现在的文件） | 现有文件数 |
|---|---|---|---|
| `kernel/` | `__init__.py`、`feature.py`（新）、`messages.py`、`events.py`（事件类型 + `EventStream` + 内存 journal）、`history.py`（含 `history_tree`）、`tools.py`（`ToolSpec`、`ToolContext`、`AgentTool`）、`work_graph.py`、`errors.py`、`values.py`（含 hashing、redaction）、`text.py`（truncation、token_estimate）、`io.py`（path_io、file_lease、timing） | `kernel/domain/*`、`kernel/ports/*`、`kernel/services/events.py`、`kernel/adapters/*` | 30 → 11 |
| `models/` | `__init__.py`、`types.py`（ProviderConfig、ModelDescriptor、Usage、RetryPolicy、network）、`catalog.py`（目录 + 目录文件 + SDK 发现）、`control.py`（ModelControl）、`factory.py`（含 DeepSeek 回传子类）、`messages.py`、`requests.py`（model_requests + failures + 请求账本 SQLite）、`metered.py`、`usage.py` | `models/**`，删除 `observation.py`、`ports/*`、`module.py`；`calibration.py` 移到 agent | 28 → 9 |
| `context/` | `__init__.py`、`types.py`、`projector.py`、`prompts.py`（PromptService + 指令文件 + 系统提示投影）、`summary.py`（服务 + 工作流 + 摘要源 + 摘要文件）、`compaction.py`（compaction、retention、history_boundaries、file_ops、tool_pairing）、`budget.py`（context_budget、context_meter）、`eviction.py` | `context/**`；`domain/tasks.py`、`services/task_projection.py` 移到 tasks | 32 → 8 |
| `tools/` | `__init__.py`、`executor.py`（ToolExecutor、ToolRegistry、错误边界、失败计数）、`settlement.py`、`permissions.py`（核心审批门、ReadOnlyToolGate、gate 组合）、`backend.py`（PycodeBackend、`ScopedBackend`）、`rg.py`、`output.py`（临时目录大输出）、`schema.py`、`call_state.py`、`framework_tool.py`、`discovery.py`（ToolSearch）、`ask_user.py`、`builtin/{read,write,edit,grep,find,ls,paths}.py` | `tools/**`；`progress_guard.py` 移到 loopguard，`plan_gate.py` 移到 plans，`MemoryToolGate` 移到 memory | 44 → 18 |
| `agent/` | `__init__.py`、`runtime.py`（AgentRuntime）、`loop.py`（HistoryMiddleware）、`context.py`（RunContext、RunState、turn_policy）、`boundaries.py`（事件回调、save_message、deliver_input、filter_model_tools）、`calibration.py` | `agent/**`、`models/adapters/calibration.py` | 15 → 6 |
| `session/` | `__init__.py`、`agent_session.py`、`store.py`（SqliteSessionStore + codec + repository + layout）、`inputs.py`（DurableInputs、PendingInputs）、`restart.py`、`view.py`、`sessions.py`（列表、解析、命名）、`storage_gc.py`（含 objects）、`history_backend.py` | `session/**`；revert 相关全部删除（移到 snapshot） | 37 → 9 |
| `ui/` | `ui/adapters/*.py` 平铺到 `ui/*.py`；删除 `ui/ports/`、`ui/module.py`、`recovery.py` 的 revert 恢复部分 | `ui/**` | 21 → 17 |
| `app/` | `__init__.py`、`__main__`（保留在包外）、`api.py`、`composition.py`（唯一组装入口，含原 `bootstrap.py`、`composition/*`、`lifecycle/*`）、`config.py`（settings + providers + credentials）、`cli/{__init__,main,parser,commands}.py` | `app/**` | 25 → 8 |

### 3.2 可选功能模块

| 模块 | 迁移后的文件 | 实现的 Feature 成员 | 来源 |
|---|---|---|---|
| `sandbox/` | `__init__.py`、`types.py`（SandboxSettings、SandboxStatus）、`sandbox.py`（`SandboxFeature`、Seatbelt profile、`run_command`）、`policy.py`（command_policy）、`process.py`（process_runner、subprocess_env、windows_job）、`tools.py`（bash） | `tools`、`gates`（命令拒绝名单、on-auto 只读命令自动放行） | `sandbox/**`；`SeatbeltSandbox/UnavailableSandbox/RequiredSandbox` 合并成一个 `Sandbox` 类（`required` 参数、非 darwin 时 status 为不可用） |
| `snapshot/` | `__init__.py`、`types.py`、`snapshot.py`（`SnapshotFeature`、`/revert`）、`store.py`（git tree I/O） | `before_tool_call`/`after_tool_call`（拍快照，写 `details.file_snapshot`）、`open` | `snapshot/**`、`session/services/revert.py`（重写，见第 6 节） |
| `memory/` | `__init__.py`、`types.py`、`memory.py`（`MemoryFeature`、服务、后台整理、写入 gate）、`store.py`（Markdown 记忆文件 + LangMem BaseStore）、`tools.py` | `open`、`tools`、`mounts`、`gates`（自己的工具免审批）、`prompt_sections`（索引）、`on_event`（`compaction` 采用后触发整理；B2）、`aclose` | `memory/**`、`tools/services/permissions.py::MemoryToolGate` |
| `skills/` | `__init__.py`、`types.py`、`skills.py`（`SkillsFeature`、Skills 服务、`SkillsMiddleware` 子类、scope gate）、`store.py`（catalog）、`install.py`、`tools.py` | `open`、`prepare_run`、`tools`、`mounts`、`middleware`、`gates`、`select_tools`、`before_tool_call`/`after_tool_call`、`on_event`（SkillSearch 结束后更新选择） | `skills/**`；`SkillBackend` 改用 `ScopedBackend` |
| `mcp/` | `__init__.py`、`types.py`（含配置解析）、`mcp.py`（`MCPFeature`、目录服务、投影）、`client.py`（连接）、`tools.py` | `open`、`prepare_run`、`tools`、`contribute`（`mcp`）、`aclose` | `mcp/**`；MCP 工具改为普通 `AgentTool`（`MCPTool.execute` 直接调连接），删除 `NativeToolBinding`/`MCPRun`【已确认】 |
| `hooks/` | `__init__.py`、`types.py`、`hooks.py`（`HooksFeature`、引擎、条件、动作）、`config.py` | `open`（`session_start`）、`before_tool_call`（`pre_tool_use`，fail-closed）、`on_event`（其余生命周期点）、`aclose`（`shutdown`） | `hooks/**` |
| `plans/` | `__init__.py`、`types.py`、`plans.py`（`PlansFeature`、服务、gate、投影）、`store.py`（plan 文件，基于 `ScopedBackend`）、`tools.py`（ExitPlanMode） | `open`、`tools`、`mounts`、`gates`、`select_tools`、`contribute`（`plan`）、`after_tool_call`（写 plan 文件后更新 `document_exists`）、`on_event`（`agent_end` 时结束 plan） | `plans/**`、`tools/services/plan_gate.py`、`agent/adapters/boundaries.py` 里的 planning 过滤、`settlement.py:570-575` |
| `tasks/` | `__init__.py`、`tasks.py`（`TasksFeature`、任务板视图、证据检查）、`tools.py`（TaskWrite、TaskUpdate） | `tools`、`contribute`（`task_board`）、`transform_context`（每轮视图、压缩后完整视图、结束前提醒）、`on_event`（`agent_end` 的 `loop_detected` 把任务板设为 failed） | `plans/adapters/tools/task_board.py`、`context/domain/tasks.py`、`context/services/task_projection.py`、`agent/adapters/history.py` 的 `fail_board`/`board_exit_reminder`、`boundaries.py::task_board_messages`。`Board/Step` 和 `work_graph` 留在 kernel（plans 审批也要用） |
| `subagents/` | `__init__.py`、`types.py`、`subagents.py`（`SubagentsFeature`、manager）、`store.py`（git worktrees）、`tools.py` | `open`、`tools`、`transform_context`（活跃子 agent 列表）、`on_event`（父 run 取消时停前台子 agent）、`aclose` | `subagents/**`、`app/composition/subagents.py` 的会话打开逻辑改为注入一个 `open_child` 可调用对象 |
| `loopguard/` | `__init__.py`、`loopguard.py`（`LoopGuardFeature`） | `on_event`（`tool_execution_end` 计数；`task` 开始新步骤、`compaction` 采用时清零）、`transform_context`（第一次提醒）、`middleware`（第二次在 `abefore_model` 里 `interrupt()` 问用户，或以 `loop_detected` 结束） | `tools/domain/progress_guard.py`、`agent/adapters/history.py` 的 loop 部分 |

`context_meter` 现在单独统计任务板视图的 token。迁移后由 agent 把 `transform_context` 产生的尾部消息交给 meter 一起估算，meter 不再认识任务板。

## 4. 保留的接口清单

规则（第 32.4 节）：只在生产代码里确实有多个实现时保留；测试替身不算实现。按这个规则，pycode 自己定义的接口只剩 4 个，另外直接使用 4 个框架接口：

| 接口 | 位置 | 实现数 | 实现 |
|---|---|---|---|
| `Feature`（基类） | `kernel/feature.py` | 10 | 10 个功能模块 |
| `ToolGate` | `kernel/feature.py` | 7 | 核心 `InterruptToolGate`、`ReadOnlyToolGate`；sandbox、memory、skills、plans 的 gate；用户经 `AppConfig.tool_gate` 传入的 gate |
| `AgentTool`（原 `ExecutableTool`） | `kernel/tools.py` | 20+ | 6 个内置文件工具、bash、AskUserQuestion、ToolSearch、TaskWrite/TaskUpdate、ExitPlanMode、记忆 2 个、Skill 3 个、MCP、子 agent 3 个 |
| `EventConsumer` | `kernel/events.py` | 4+ | stream-json `ConsoleOutput`、TUI、子 agent 转发、`Feature`（经 `on_event`） |
| 框架：`BaseChatModel` | LangChain | 3 | ChatOpenAI、ChatAnthropic、ChatDeepSeek（第 32.4 节说的"模型适配"就是它，不再自定义） |
| 框架：`BackendProtocol` | Deep Agents | 4 | `PycodeBackend`、`ScopedBackend`、`ConversationHistoryBackend`、`CompositeBackend` |
| 框架：`AgentMiddleware` | LangChain | 4 | HistoryMiddleware、settlement、Skills、loopguard |
| 框架：`BaseStore` | LangGraph | 1（LangMem 要求） | memory 的文件 store |

这比第 32.4 节估计的"十几个"少。原因是按"多个实现"逐个核对后，其余 75 个都只有一个生产实现。跨层需要回调的地方改用类型别名（不是接口）：`CommandRunner`（hooks 跑命令）、`OpenChild`（subagents 打开子会话）、`LaunchRun`（session 启动一次 run，由组装提供）。

删除的 75 个 Protocol（按现在位置）：

- kernel：`EventSink`、`EventJournal`、`MessageJournal`、`HistoryReader`、`HistoryJournal`、`Timing`、`ArgumentValidator`、`_DataclassValue`
- agent：`FrameworkExtension`、`InputQueue`、`DurableInputDelivery`、`HistoryProjector`、`RequestTracking`、`TitleAssignment`、`WorkspaceReadback`、`Runtime`
- app：`AsyncResource`
- context：`SummaryRequestRecords`、`ProjectionContributor`、`SummaryGenerator`、`SummaryStore`
- models：`Projector`、`RunOwnership`、`RegistryLookup`、`CalibrationReport`、`SummaryBudget`、`CalibrationContext`、`ModelFailureClassifier`、`RequestStore`、`ModelObservation`、`ModelDirectory`、`CatalogStore`
- tools：`WorkspaceTools`、`SettlementContext`、`ResolvingBackend`、`ToolExtension`、`NativeToolBinding`、`ToolHooks`、`OutputWriter`、`ToolOutputStore`、`MessagePublisher`、`QuestionPrompt`；`ToolGate` 移到 kernel（保留）
- session：`SessionStore`、`RuntimeLauncher`、`SessionRepository`、`StorageObjects`、`WorkspaceInspector`
- ui：`ModelChoice`、`ModelChoices`、`SandboxAvailability`、`InteractiveSession`、`InteractiveSessions`
- sandbox：`SandboxBackend`（3 个实现合并成 1 个类）、`ProcessRunner`、`StreamWriter`、`OutputSink`、`WindowsFunction`、`Kernel32`、`WindowsCtypes`
- hooks：`HookActions`、`LifecycleHooks`、`LifecycleContext`
- skills：`CatalogWriteGuard`、`SkillCatalog`、`SkillCatalogCommands`、`SkillInstaller`、`BackendOperations`
- plans：`BackendOperations`、`PlanFiles`、`QuestionPrompt`
- mcp：`MCPConnections`
- memory：`MemoryUpdater`
- snapshot：`SnapshotStore`
- subagents：`SubagentSession`、`SubagentSessions`、`Worktrees`

## 5. `app/composition.py` 的样子

公共阶段不提交这个文件的代码：那时功能模块还没迁移，提交就是 import 不存在的模块的死代码。下面的形状在本文里锁定，各组按它写构造函数，集成步骤照着写出来【已确认】。

```python
# Pi 没有，pycode 新增；对应 Pi coding-agent main.ts / sdk.ts 里创建 AgentSession 的部分
"""唯一的组装入口：核心 + features 列表。"""

def build_features(config: AppConfig, providers, *, interactive: bool) -> list[Feature]:
    sandbox = SandboxFeature(config.sandbox, required=config.isolated_worker,
                             passthrough=config.subprocess_passthrough, path_prefix=config.bash_path_prefix)
    features: list[Feature] = [
        sandbox,
        SnapshotFeature(excluded_roots=(config.user_data,)),
        LoopGuardFeature(threshold=config.loop_repeat_threshold, asks_user=config.loop_asks_user),
        TasksFeature(),
        PlansFeature(),
        MemoryFeature(trusted=config.project_trusted, mode=config.memory_mode,
                      model=partial(build_background_model, config, providers[0])),
        SkillsFeature(trusted=config.project_trusted, index_budget=2000),
        HooksFeature(trusted=config.project_trusted, worker=config.agent_id != "lead",
                     run_command=sandbox.run_command),
    ]
    if config.agent_id == "lead":
        features.append(MCPFeature(trusted=config.project_trusted,
                                   credentials=credential_environment(config, providers)))
    if config.allow_delegation:
        features.append(SubagentsFeature(open_child=partial(open_agent_session, config, providers),
                                         interactive=interactive))
    return features


async def open_agent_session(config, providers, session_id, *, create=False,
                             consumers=(), interactive=True) -> AgentSession:
    async with AsyncExitStack() as stack:
        store = await open_store(config, session_id, create=create)
        stack.push_async_callback(store.aclose)
        core = Core(config, providers, store, consumers)          # 模型目录、ModelControl(events=...)、投影器、提示
        stack.push_async_callback(core.aclose)
        features = tuple(build_features(config, providers, interactive=interactive))
        scope = core.session_scope()
        for feature in features:
            await feature.open(scope)
            stack.push_async_callback(feature.aclose)
        session = AgentSession(core, features,
                               launch=partial(build_run, config, core, features))
        session.closing = stack.pop_all()                          # 会话关闭时 aclose 这个 stack
        return session


async def build_run(config, core, features, run_id, inputs) -> AgentRuntime:
    run = core.run_scope(run_id)
    for feature in features:
        await feature.prepare_run(run)
    return AgentRuntime(core.run_core(run), features)
```

关掉一个模块就是删掉 `features` 列表里的一行；新增模块就是实现 `Feature` 再加一行。`app/api.py` 的嵌入入口 `runtime(...)` 也走 `build_run`，并且要求传入 store（E4）。

## 6. 第 33.2 节 #1：`/revert` 简化【已确认】

### 6.1 行为

`pycode revert <target> [--mode dialogue|code|both]`，TUI `/revert` 选目标和模式后弹一次确认（列出将被改回的文件），然后执行：

1. **定位**：`target` 是当前分支上的一个 entry id（或 `root`）。不在当前分支上时报 `StorageError("revert_branch_conflict")`，什么都不改。`mode` 的取值由 argparse `choices` 和 TUI 选项保证，代码内不再校验。
2. **算出要改回的文件**（`code`/`both`）：沿当前分支，取 `target` 之后每个工具结果 `details["file_snapshot"]`（`{before, after, paths}`），每个路径记下第一次出现的 `before` 和最后一次的 `after`。没有任何路径时只做对话部分。这个计算是纯函数，TUI 的确认弹窗也用它列出文件。
3. **拍当前快照**：`safety = capture()`。不是 git 项目时报 `SnapshotError("snapshot_unavailable")`（`dialogue` 模式不需要 git）。
4. **检查冲突**：每个路径当前内容的 blob 必须等于它最后的 `after`，否则报 `SnapshotError("revert_conflict")`，消息里列出冲突的路径，什么都不改。
5. **写回**：逐个路径从 `before` 树写回；`before` 里不存在的路径直接删除。
6. **对话**（`dialogue`/`both`）：`store.branch(target)`。
7. **记一条记录**：`custom` 记录 `{"kind": "revert", "target", "mode", "paths", "file_snapshot": {"before": safety, "after": <写回后的树>, "paths": [...]}}`。因为它带 `file_snapshot`，"撤销这次 revert"就是对这条记录再 `/revert` 一次，不需要单独的恢复机制。

**中途失败**（第 5 步写到一半，I/O 错误）：捕获后照样写第 7 步的记录（`after` 用此刻重新拍的快照），再抛 `SnapshotError("revert_partial", "…已改回 N/M 个文件；对这条 revert 记录执行 /revert 可以回到 revert 之前")`。这是 I/O 边界上有明确处理动作的 except，符合第 21 节。CLI 退出码 1，TUI 显示错误。不记日志。

### 6.2 删除

- `session/services/revert.py`、`session/domain/revert.py`、`session/domain/revert_state.py`；`AgentSession.preview_revert/revert/recover_revert/require_settled` 中只为 revert 的部分；所有 `pending_revert(...)` 检查和 `revert_recovery_required` 错误码。
- `snapshot/adapters/git_tree.py` 的 `preview/apply/restore/recover/restore_path`、`.partial` 暂存、逐文件 `hash-object` 复核、`restores/<id>.json`；`RestorePlan/RestoreFile/RestoreRecord`、`RevertPlan`。
- 记录类型 `revert_started/revert_completed/revert_recovery` 和 `custom:file_snapshot`（审计 2 C20）：加进 `upgrade_entry` 的 `REMOVED_ENTRY_TYPES`，旧会话打开时丢弃（D4 保留 `upgrade_entry`，旧格式兼容只放这一处）。
- CLI `revert preview/apply/recover` 子命令和 `--compensate`；TUI `RevertPanel` 的 apply/discard/recover 按钮和"Revert recovery required"提示。

### 6.3 用户可见的变化

- 不再有"先预览得到 plan 文件、再 apply"的两步；确认弹窗代替预览。
- 进程在写文件途中被杀，部分文件已改回且没有记录；用户可以对 revert 之前的最后一个工具调用再 `/revert`，或者用 git 自行恢复。这是复杂度审计 #1 接受的代价。
- 中途 I/O 失败时有记录，可以一键撤回。

### 6.4 测试

`tests/snapshot/test_revert.py` 用临时 git 仓库：冲突时不写任何文件；只对话；只代码；两者；写到第二个文件时注入 `OSError`，记录已写且错误码为 `revert_partial`；对 revert 记录再 revert 能回到原状；非 git 项目 `code` 模式报 `snapshot_unavailable`、`dialogue` 模式正常。

## 7. 第 33.2 节 #5：改读 LangChain `usage_metadata`【已确认】

### 7.1 方案

- **删除** `models/adapters/observation.py` 的 10 个 SDK 代理类和 `ProviderObservation`；`factory.py` 里给 ChatOpenAI/ChatAnthropic/ChatDeepSeek 传 `client/async_client/root_client/sdk_client` 包装对象的代码；`ModelObservation` Protocol；metadata 里的 4 个 `pycode_*` 观测字段（含 `pycode_usage_complete`）；`list_requests(session)` 全表读取算 attempt 偏移的逻辑（attempt 每个逻辑请求从 0 开始，靠 `request_id` 区分）。
- **用量**：只读最终 `AIMessage.usage_metadata`：`input_tokens`、`output_tokens`、`input_token_details.cache_read` / `cache_creation`、`output_token_details.reasoning`；`response_metadata` 取 `model_name`、`finish_reason`/`stop_reason`、`id`。流式时 OpenAI/DeepSeek 已经传 `stream_usage=True`（`factory.py:53`），Anthropic 默认 `stream_usage=True`。`models/messages.py::normalize_usage` 改为只读这两处。
- **中断**：流在中途断掉、拿不到最后的 usage 时，请求记录 `usage=None`、状态 `interrupted`。用量统计会少算这部分，复杂度审计 #5 已接受。
- **保留 DeepSeek `reasoning_content` 回传**：`langchain_deepseek` 1.1.1 会把响应里的 `reasoning_content` 放进 `AIMessage.additional_kwargs`（`chat_models.py:406-455`），但它的 `_get_request_payload`（`:350-383`）不会把它写回请求。所以在 `models/factory.py` 定义 `ChatDeepSeek` 子类，只覆盖 `_get_request_payload`：按顺序把输入里每条 `AIMessage.additional_kwargs["reasoning_content"]` 写到 payload 对应的 assistant 消息上，约 15 行。`to_framework` 保证把保存的 thinking 放回 `additional_kwargs["reasoning_content"]`。

### 7.2 DeepSeek 真机验证（r32-c 组完成 models 实现后做，需要 `DEEPSEEK_API_KEY`）

1. **离线单测**（不需要密钥）：构造"user → assistant(带 reasoning_content 和 tool_call) → tool"三条消息，调用子类 `_get_request_payload`，断言 payload 的 assistant 消息带 `reasoning_content`，其余字段和父类输出一致。
2. **真机正向**：`.venv/bin/python scripts/verify_live_provider.py --provider deepseek --model deepseek-reasoner --base-url https://api.deepseek.com --thinking enabled --output .acceptance/deepseek-thinking.json`。这个探针会先调用一次工具，再发第二次请求，第二次请求的历史里就有带 reasoning 的 tool-call 轮次。通过条件：`status == "passed"`；至少 2 条请求记录都是 `completed`；每条 `usage.input_tokens > 0` 且 `output_tokens > 0`；`reasoning_tokens` 不为空。
3. **真机反向对照**：脚本加一个 `--no-reasoning-passback` 开关，只在脚本进程里把子类的 `_get_request_payload` 换回父类实现，再跑一遍。
   - 第二次请求被 DeepSeek 以 400 拒绝：说明回传是必须的，而且第 2 步证明回传有效。
   - 没有被拒绝：说明 DeepSeek 目前不强制回传。报告给用户，由用户决定这 15 行留还是删（默认保留，照第 33.2 节）。
4. 有 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 时各跑一次第 2 步（不带 thinking），确认 usage 字段齐全；没有密钥时用离线固定的 `AIMessage` 样本测 `normalize_usage`，并在报告里写明"未真机验证"。
5. 没有 DeepSeek 密钥时，脚本输出 `status: unverified`，r32-c 组不能宣布完成，用 warning 向用户要密钥或豁免。

## 8. 两项待决定：D3 和 B2

**D3 provider 指纹（推荐：恢复时直接用当前配置）**。删除 `ModelCatalog.provider_hash`、`model_change` 记录里的 `catalog_hash`/`provider_hash` 字段和 `model_control.py` 里两段比对。打开会话时，按记录里的 provider 名和 model id 在当前目录里解析；找不到时照现有行为报 `model_unavailable`，让用户重新选。旧记录里多出来的两个字段读盘时忽略（`validate_entry` 不要求它们）。用户可见的变化：改了 provider 配置（base_url、密钥变量名、上下文窗口等）之后，会话不再要求重新选模型，直接用新配置。理由同第 33.1 节 #4（hooks、MCP 已经这样做）。约删 25 行。

**B2 后台记忆整理（推荐：删掉 `except Exception` 和 `return_exceptions=True`）**。`MemoryFeature` 在 `compaction` 事件（采用压缩）时启动后台整理任务，同一时间最多一个。后台任务不再捕获异常。`collect()` 直接 `await task`，失败就把异常抛出来。`collect()` 在三个时机调用：下一次 `agent_start` 事件（`on_event`）、下一次启动整理之前、`aclose`。所以整理失败时，下一次 run 一开始就以 `error` 结束，`run_end.error_code` 是整理的错误码（例如 `model_request_failed`），用户能看到原因；关闭会话时失败则由 CLI 错误边界打印。手动 `/memory extract` 照旧同步执行，失败直接报给用户。代价：一次模型限流导致的整理失败，会让下一次 run 失败一次；再次提交就正常（任务已被取走）。

## 9. 并行线

按用户最新要求，并行线最多 5 条，分 5 组（r32-a～r32-e），取代"每个模块一条"。

### 9.1 步骤总览

```
公共阶段（串行，本分支）
   └─▶ 5 组并行线同时开工：r32-a … r32-e（各自 worktree，从公共阶段提交拉出；组内按顺序做）
          └─▶ 集成步骤（串行，本分支：依次合并 5 组 + 跨包改动 + 文档）
                 └─▶ 全量验证（只跑一次）
                        └─▶ Pi 对齐审计（只读）
```

只有一批并行线，所以只有一个集成步骤。worktree 路径 `.worktrees/r32-a` … `.worktrees/r32-e`，分支同名，都从公共阶段的最后一个提交拉出。

### 9.2 公共阶段（串行）

1. `kernel/feature.py`（第 2 节）和 kernel 平铺（第 3.1 节）：kernel 包根导出全部公共类型；把全仓库约 20 处 `pycode.kernel.adapters.*` / `pycode.kernel.domain.*` 的 import 机械地改成包根 import。这一步要碰所有包，但只改 import 行，所以放在并行之前。
2. 第 33.2 节 #9 `ScopedBackend(inner, root, *, read_only, path_guard=None)`，加在 `tools/adapters/backend.py`（约 40 行，`a*` 方法用 `asyncio.to_thread`）。只加类和测试，不改使用方；skills（r32-d）、plans（r32-b）、session（r32-e）各自换用。放在公共阶段，是因为三个组都依赖它。
3. `scripts/check_architecture.py` 改成两条规则（第 10 节），连同 `tests/architecture` 的夹具测试。新检查在真实代码上要到集成后才会通过，公共阶段只要求它自己的夹具测试通过。
4. `tests/kernel/test_feature.py`：契约默认实现和 gate 组合规则。第 32.5 节建议先拿 memory 或 tasks 做样板，这里用这个契约测试代替，让 5 组同时开工【已确认】。
5. 记录基线：各包测试用例数、`src/pycode` 文件数 / 类数 / Protocol 数 / 行数。

公共阶段结束时跑 `tests/kernel`、`tests/tools`、`tests/architecture`、ruff、pyright（全仓库，此时代码结构未变，应当全绿）。

### 9.3 5 组并行线

分组依据：把互相搬代码最多的包放进同一组，让搬家在组内一次完成；再按现有行数大致拉平工作量（每组 3～6 千行）。组内先做核心包，再做功能模块，因为功能模块从核心包里接收被移出的代码。

| 组 | 组内顺序 | 现有行数 | 第 41 节条目 | 第 33.2 节条目 | 组内完成的搬家 |
|---|---|---|---|---|---|
| **r32-a** | ① tools → ② sandbox → ③ loopguard（新包） | 约 4.7k | C14、E11、A9（tools） | — | `tools/domain/progress_guard.py` → loopguard；`tools/adapters/permissions.py` 里的命令拒绝名单和 on-auto 自动放行 → sandbox 的 gate |
| **r32-b** | ① agent → ② tasks（新包）→ ③ plans → ④ subagents | 约 2.9k + tasks | C13、A3（agent） | 换用 `ScopedBackend`（plans） | `plans/adapters/tools/task_board.py`、agent 的 `fail_board`/`board_exit_reminder`/`task_board_messages` → tasks；agent `filter_model_tools` 的 planning 部分 → plans；`subagent_messages` → subagents |
| **r32-c** | ① models → ② memory → ③ mcp | 约 4.2k | A2、E2（models 侧：`ModelControl.events` 改必填并发出 `model_change`）、D3（models）；C12、B2（memory）；C16、C19（mcp） | #5 usage_metadata（含第 7.2 节真机验证） | — |
| **r32-d** | ① context → ② skills → ③ hooks | 约 4.5k | C11、C17、C9、C15、C8（context）；A7（skills）；A11（hooks） | 删除 `--revision`（skills 侧）；换用 `ScopedBackend`（skills） | `context/services/prompt_projection.py` 的特例并入 `prompts.py` |
| **r32-e** | ① session → ② snapshot → ③ ui | 约 5.9k（ui 只做一部分） | C1、C4、C2、A1、C5（`AgentSession` 侧：把 `start.run_id` 传给 `LaunchRun`）、D4（不改，确认 `upgrade_entry` 是旧格式兼容的唯一位置，并加入第 6.2 节的旧 revert 记录类型）（session）；B3（ui） | #10 输入只去重一次，连同第 34 节推迟的"输入路径三把锁合并成一把"（session）；#1 `/revert`（session 删旧 → snapshot 新实现 → ui 的 revert 确认弹窗）；`history_backend.py` 换用 `ScopedBackend`（session） | `session/services/revert.py` 等 → snapshot（第 6 节） |

跨组的搬家照用"**新包所在的组创建，原包所在的组删除**"：

| 代码 | 创建（组） | 删除（组） |
|---|---|---|
| `models/adapters/calibration.py` → `agent/calibration.py` | r32-b | r32-c |
| `context/domain/tasks.py`、`context/services/task_projection.py` → tasks | r32-b | r32-d |
| agent 里的防打转逻辑（`start_progress_guard`、`loop_reminder`、`loop_question`、`abefore_model` 的 loop 分支）→ loopguard | r32-a | r32-b |
| `tools/services/plan_gate.py`、`settlement.py` 里 plan 文件的 `update_exists` → plans | r32-b | r32-a |
| `tools/services/permissions.py::MemoryToolGate` → memory | r32-c | r32-a |
| `tools/ports/hooks.py`、`ToolHookBridge` → hooks 的 `before_tool_call` | r32-d | r32-a |
| `ToolExecutor.snapshots` 的拍快照逻辑 → snapshot 的 `before/after_tool_call` | r32-e | r32-a |
| `agent/adapters/boundaries.py` 的 `custom:file_snapshot` 记录（C20） | — | r32-b（删除） |
| `RunContext.plans/hooks/subagent_view/progress_guard` 字段 | — | r32-b（删除） |

通用规则：

- 每组只改组内各包的 `src/pycode/<包>/`、`tests/<包>/`（测试目录同样平铺，去掉 `adapters/domain/services` 子目录，删掉空目录）和包内 `README.md`。r32-c 另外可以改 `scripts/verify_live_provider.py`（加 `--no-reasoning-passback` 开关）。
- **组内每个包的第 41 节修复放在这个包的第一个提交里，先修后搬**：此时文件还在原位置，审计 2 里的位置最容易对上；第二个提交起再迁移这个包。
- 每个包迁移完就验收这个包：`.venv/bin/pytest tests/<包> -q`、`.venv/bin/ruff check src/pycode/<包> tests/<包>`、`.venv/bin/ruff format --check` 同范围、`.venv/bin/pyright src/pycode/<包>`（只看本包文件里的错误）。组内后面的包可以依赖前面已迁移的包的新 API，这是组内排顺序的原因。
- 不处理其他组的包对本组旧 API 的引用，那是集成步骤的事。
- 断言被删分支错误码的测试按第 21 节一起删。
- 不加新的 Optional 依赖和判空（E3/E4 在集成步骤统一处理）。

### 9.4 不能独立迁移的核心包和组内顺序的原因

- **kernel**：所有组都依赖它，放在公共阶段，不进任何组。
- **app**：它就是组装入口，要等所有模块的新构造函数都在，所以不进任何组，全部在集成步骤做（composition、lifecycle、CLI）。
- **ui 的一部分**：调用 memory、tasks、plans、agents、skills 命令的代码依赖其他组的新 API，放到集成步骤。r32-e 里的 ui 只做平铺、删 `ui/ports`、B3，以及组内 snapshot 已经提供的 `/revert` 确认弹窗。
- **组内顺序**：r32-a 先 tools，sandbox 和 loopguard 才能接收从 tools 移出的 gate 和防打转代码。r32-b 先 agent，再 tasks（接收 agent 和 plans 的任务板代码），然后 plans（删掉已移到 tasks 的 `task_board.py`），最后 subagents。r32-c 先 models，因为 memory 用 `RetryPolicy`、mcp 用模型侧的类型，都从 models 包根导入。r32-d 先 context，因为 skills、hooks 的 `prompt_sections`/`contribute` 要用 context 平铺后的 `ProjectionContribution` 位置（已移到 kernel）和提示组装。r32-e 先 session，删掉旧 revert，再由 snapshot 写新实现，最后 ui 接上弹窗。
- **组之间没有依赖**：跨组的代码搬家按上面的"新包所在的组创建、原包所在的组删除"拆开；sandbox→hooks 的命令执行靠 kernel 的 `CommandRunner` 类型别名和组装注入；三个组对 `ScopedBackend` 的依赖靠公共阶段先做。所以 5 组同时开工。

### 9.5 集成步骤（串行，本分支）

1. 依次合并：r32-c（models 在最底层）→ r32-d（context）→ r32-a（tools）→ r32-b（agent）→ r32-e（session、ui）。按"新包所在的组创建、原包所在的组删除"的规则，各组改的文件不重叠；如果合并时出现冲突，以本文为准解决。
2. 修跨包引用：核心包里还指向旧 API 的地方（例如 `context_meter` 接收 agent 给的尾部消息）。
3. 写 `app/composition.py`（第 5 节），删掉 `app/composition/`、`app/lifecycle/`、`app/bootstrap.py`；第 41 节 **B1、C18、E1、C5（launcher 侧）** 随这些文件一起消失或改写；**E2** 把会话事件流传给 `ModelControl`。
4. 第 33.2 节 **#7 AsyncExitStack**：会话和 run 各用一个 stack，删除 `_close_task`/shield 循环、`_close_all`、`RunAssembly`、`RuntimeContainer.aclose` 的三层 try/finally、`SessionCleanup`、`models/module.py` 的 `_finish_owned_close`。
5. 跨包的第 41 节条目：
   - **E3**：`AgentSession` 的 `durable_inputs/view/storage` 改必填；`revert/plans/memory` 随功能模块移出后不再是 `AgentSession` 的参数；测试改用真实对象或 fake。
   - **E4**：`history_store`、`projector`、`durable_inputs`、`eviction` 改必填；`app/api.py` 嵌入入口要求传 store；`tests/models/adapters/test_provider_wire.py` 等测试和 `scripts/verify_live_provider.py` 改用临时目录里的 `SqliteSessionStore`；删掉 agent/tools/models 里约 20 处判空。
   - **C7**：删除 `ToolSettlementState` 的 4 个 failure 字段、`Command` 里的 `**context.failure_guard` 和 `or request.state`；`FailureGuard` 改 `total=True` 并写全初始值。
   - **C10**：全仓库去掉对已冻结对象的 `clone_value`（tool_registry、model_catalog、summary_source + summary、mcp），注册或构造时 `freeze_value` 一次。
6. CLI：`revert` 子命令改成 `pycode revert <target> --mode`（#1）；删除 `skill create/update --revision`（33.2 最后一项）；ui 里调用功能模块命令的代码改成 `session.feature(X)`。
7. 第 34 节推迟的"错误码收敛"：grep 全仓库，删除只有抛出方、没有任何分支判断它的错误码（第 34 节第 10 条），对应测试一起删。
8. `scripts/check_architecture.py` 在真实代码上通过；删除 `docs/design/port-implementations.json`、`docs/design/feature-dependencies.json`。
9. 文档：`docs/architecture.md`（新结构、依赖方向、Feature 契约）、`docs/pi-map.md`（阅读顺序：kernel → agent（对照 Pi agent-loop）→ tools → `kernel/feature.py` → 任选一个功能模块 → `app/composition.py`）、`docs/source-map.md`、`README.md`、`docs/design/subagents.md` 与 `tool-discovery.md` 里的路径；`docs/interview` 重新生成，通过 `docs/interview/tools/build.py --check` 和 `check_index.py`。
10. 跑相关测试：全部 `tests/`（为全量验证做准备，不计时、不出报告），ruff、pyright、架构检查，修到全绿。

第 33.2 节 6 项、第 41 节全部范围、第 32 节的结构目标都在集成步骤结束前完成；之后才进入全量验证。

### 9.6 每一项归到哪一步

| 项 | 步骤 |
|---|---|
| 第 32 节：Feature 契约、kernel 平铺 | 公共阶段 |
| 第 32 节：架构检查改成两条 | 公共阶段（写）+ 集成（在真实代码上通过、删两个 json） |
| 第 32 节：各包平铺、删单实现 Protocol | 包所在的组：tools/sandbox/loopguard → r32-a；agent/tasks/plans/subagents → r32-b；models/memory/mcp → r32-c；context/skills/hooks → r32-d；session/snapshot/ui → r32-e |
| 第 32 节：`app/composition.py` | 集成 |
| 33.2 #1 `/revert` | r32-e（session 删旧 → snapshot 新实现 → ui 弹窗）+ 集成（CLI 子命令） |
| 33.2 #5 usage_metadata | r32-c（含真机验证） |
| 33.2 #7 AsyncExitStack | 集成 |
| 33.2 #9 ScopedBackend | 公共阶段（类）+ r32-b（plans）、r32-d（skills）、r32-e（session `history_backend`）换用 |
| 33.2 #10 输入只去重一次 | r32-e（session） |
| 33.2 删 `--revision` | r32-d（skills）+ 集成（CLI） |
| 41：C14、E11、A9 | r32-a（tools） |
| 41：C13、A3 | r32-b（agent） |
| 41：A2、D3 | r32-c（models） |
| 41：E2 | r32-c（models）+ 集成（传入事件流） |
| 41：C12、B2 | r32-c（memory） |
| 41：C16、C19 | r32-c（mcp） |
| 41：C11、C17、C9、C15、C8 | r32-d（context） |
| 41：A7 | r32-d（skills） |
| 41：A11 | r32-d（hooks） |
| 41：C1、C4、C2、A1、D4 | r32-e（session） |
| 41：C5 | r32-e（session）+ 集成（launcher） |
| 41：B3 | r32-e（ui） |
| 41：B1、C18、E1 | 集成 |
| 41：E3、E4、C7、C10 | 集成 |
| 34：错误码收敛 | 各组删自己包里的 + 集成全仓库收尾 |
| 34：输入路径三把锁合并 | r32-e（session） |
| 全量验证 | 以上全部完成后跑一次 |
| Pi 对齐审计 | 全量验证之后，只读 |

### 9.7 第 41 节修复放在迁移前还是后

建议：**放在组内每个包的第一个提交，先修后搬**，不另设一个"迁移前统一小修"的串行阶段。理由：

- 用户关心的是"报告里的位置还对得上"。包的第一个提交时文件还在原位置，效果和迁移前做一样。
- 如果在公共阶段集中修，要按包拆开并行再合并一次，等于多一轮并行和集成；而这些改动和这个包后面的迁移改的是同一批文件，放在同一组里不会冲突。
- 位置在 app 里、文件本身会被删掉或重写的（B1、C18、E1），以及跨包的 E3/E4/C7/C10，放在集成步骤。

## 10. 架构检查（`scripts/check_architecture.py`）

只保留两条规则：

1. **依赖方向**：`kernel` 不 import 任何其他 pycode 包，也不 import 框架（`kernel/io.py` 允许 `os/pathlib/asyncio`，这是 kernel 里唯一做 I/O 的文件）；核心包按 `models ← context ← tools ← agent ← session` 单向依赖；功能模块只能 import kernel 和核心包，功能模块之间不能互相 import；`ui` 可以 import 除 `app` 外的所有包；`app` 可以 import 所有包。依赖表写在脚本里，不再另放 json。
2. **只能 import 包根导出的内容**：跨包 import 只能是 `from pycode.<包> import <名字>`，且名字在该包 `__init__.py` 的字面量 `__all__` 里。

删除：层级 rank（domain/ports/services/adapters）、`port-implementations.json` 及"Protocol 必须登记实现"、app 子包布局规则、`FLAT_FEATURES/FLAT_IO_MODULES`、`symbol_origin` 的"用包装类洗白 adapter"追踪、函数内 import 禁令、动态 import 与 `sys.modules` 禁令、`__getattr__` 禁令、`feature-dependencies.json` 对比。

品牌检查（`check_branding`，查旧产品名）不是架构规则，作为同一脚本里独立的一项保留【已确认】。

## 11. 需要用户拍板的事项

1. **Feature 契约**（第 2 节）：在第 32.3 节 7 个成员之外，增加 `checkpoint_keys/contribute`、`open`、`prepare_run`、`mounts`、`middleware`、`select_tools`、`before_tool_call/after_tool_call`，每个都有具体使用方（第 2.2 节）。另外，MCP 工具改为普通 `AgentTool`，删除 `NativeToolBinding`。
2. **模块归类**（第 3 节）：核心 = kernel、agent、tools、models、context、session（另加 ui、app 在最上层）；可选功能模块 = sandbox、snapshot、memory、skills、mcp、hooks、plans、tasks、subagents、loopguard。保留的自定义接口只有 4 个（第 4 节），少于"十几个"的估计。`app/composition.py` 不在公共阶段提交骨架代码，形状以第 5 节为准。
3. **样板模块**：不先拿 memory/tasks 做样板，改用公共阶段的契约测试验证契约，让 5 组同时开工。
4. **并行线划分与依赖**（第 9 节）：按用户最新要求分 5 组同时开工——r32-a：tools → sandbox → loopguard；r32-b：agent → tasks → plans → subagents；r32-c：models → memory → mcp；r32-d：context → skills → hooks；r32-e：session → snapshot → ui（ui 只做一部分）。kernel 在公共阶段，app 在集成步骤。组内按顺序做；组之间的代码搬家按"新包所在的组创建、原包所在的组删除"拆开，组之间没有依赖。每一项归到哪一步见第 9.6 节。
5. **架构检查**：两条规则之外，品牌检查作为独立一项保留。
6. **#1 `/revert`**（第 6 节）：先拍快照、检查冲突、写回、记一条带 `file_snapshot` 的记录（可以对它再 revert 撤销）；删除 preview/apply/recover、补偿事务、restore record；中途 I/O 失败也写记录并报 `revert_partial`。
7. **#5 usage_metadata**（第 7 节）：删除 10 个 SDK 代理类，只读 `usage_metadata` 和 `response_metadata`，保留约 15 行 DeepSeek `reasoning_content` 回传；验证方法是离线单测 + 真机正向 + 真机反向对照（第 7.2 节），没有 DeepSeek 密钥时这条线停下来问用户。
8. **D3**（推荐）：恢复时直接用当前配置，删除 `catalog_hash`、`provider_hash`。
9. **B2**（推荐）：删掉 `except Exception` 和 `return_exceptions=True`；整理失败的异常在下一次 `agent_start`、下一次启动整理或关闭会话时抛出，run 失败。
10. **第 41 节修复的时机**（第 9.7 节）：放在组内每个包的第一个提交里先修后搬；app 内文件会被删除或重写的 B1、C18、E1，以及跨包的 E3/E4/C7/C10，放在集成步骤。

## 12. 已确认决定（用户 2026-11-01 确认）

1. **Feature 契约**：同意第 2 节的接口。在第 32.3 节 7 个成员之外，增加 `checkpoint_keys/contribute`、`open`、`prepare_run`、`mounts`、`middleware`、`select_tools`、`before_tool_call/after_tool_call`。MCP 工具改为普通 `AgentTool`，删除 `NativeToolBinding`/`MCPRun`。
2. **模块归类与接口**：核心是 kernel、agent、tools、models、context、session，ui、app 在最上层；可选功能模块是 sandbox、snapshot、memory、skills、mcp、hooks、plans、tasks、subagents、loopguard。自定义接口只保留 4 个：`Feature`、`ToolGate`、`AgentTool`、`EventConsumer`，另外直接用 4 个框架接口（第 4 节）。`app/composition.py` 不在公共阶段提交骨架代码，写法以第 5 节为准。
3. **不单独做样板模块**：用公共阶段的契约测试 `tests/kernel/test_feature.py` 代替。
4. **并行线**：最多 5 组，同时开工，组内按顺序做（第 9.3 节）。
   - r32-a：tools → sandbox → loopguard
   - r32-b：agent → tasks → plans → subagents
   - r32-c：models → memory → mcp
   - r32-d：context → skills → hooks
   - r32-e：session → snapshot → ui（ui 只做一部分）

   kernel、`ScopedBackend` 类、架构检查改造放在公共阶段；app 放在集成步骤。跨组的代码搬家按"新包所在的组创建、原包所在的组删除"拆开，组之间没有依赖。每一项归到哪一步以第 9.6 节为准。
5. **架构检查**：只留两条规则，即依赖方向、只能 import 包根导出的内容。删除 `port-implementations.json`、`feature-dependencies.json` 和"Protocol 必须登记实现"的检查。品牌检查作为同一脚本里独立的一项保留。
6. **#1 `/revert`**：照第 6 节做。先拍快照，检查冲突，写回文件，再记一条带 `file_snapshot` 的记录，对这条记录再 `/revert` 就能撤销。删除 preview/apply/recover、补偿事务和 restore record。写到一半出错时也写记录，并报 `revert_partial`。
7. **#5 usage_metadata**：照第 7 节做。删除 10 个 SDK 代理类，只读 `usage_metadata`/`response_metadata`，用 `ChatDeepSeek` 子类保留约 15 行 `reasoning_content` 回传。按第 7.2 节验证：离线单测、真机正向、`--no-reasoning-passback` 真机反向对照。没有 `DEEPSEEK_API_KEY` 时 r32-c 停下来问用户。
8. **D3**：恢复时直接用当前配置，删除 `catalog_hash`、`provider_hash`（第 8 节）。
9. **B2**（用户 2026-11-01 改定，取代第 8 节的推荐方案）：后台记忆整理失败就算失败，不提示用户，也不让 run 失败。只在 `background()` 里保留一层 `except Exception`，用 `logger.debug` 记一行，方便以后排查；不发事件，也不抛出。删掉 `collect()` 里 `return_exceptions=True` 的第二层吞异常。
10. **第 41 节修复的时机**：放在组内每个包的第一个提交里，先修后搬。B1、C18、E1 和 E3、E4、C7、C10 放在集成步骤。
11. **完成顺序**：第 32 节、第 33.2 节 6 项、第 41 节全部范围都在全量验证之前做完；全量验证只在全部完成后跑一次，之后做只读的 Pi 对齐审计。
