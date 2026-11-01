# pycode 第二轮重构：普通服务与显式组装（2026-11-01 修订）

- 基线：分支 `refactor-structure-slim`，本地 main `457c8b9`。
- 依据：11.1.md 第 21、23、32、33.2、34、41 节；`.agents/tasks/pycode-overdefensive-audit-2.md`（下称"审计 2"，行号基于 `5286c1b`，已偏移，实施时按代码内容重新定位）；`.agents/tasks/pycode-overcomplexity-audit-2026-11-01.md`（下称"复杂度审计"）；Pi 源码 `/tmp/pi-research`（HEAD `2c8d1fe`）。
- 本文已按根目录 11.1.md 的最新决定同步，替代旧 Feature 方案。原设计可从提交 f1460be 查阅。已完成的业务实现和精简保留；本文描述目标，不代表代码全部完成。

## 0. 现状数字（457c8b9）

| 项 | 数值 |
|---|---|
| `src/pycode` 文件数 / 行数 | 321 个 `.py` / 26463 行（其中 46 个空 `__init__.py`） |
| 类数 | 379 |
| `Protocol` 子类（AST 统计） | 79。11.1 里写的"93"是更早基线的数字；`docs/design/port-implementations.json` 登记了 67 个 |
| `tests` | 234 个 `.py` |

最终报告（第 32.5 节第 3 条）按同样口径统计文件数、类数、Protocol 数和行数。

## 1. 总体方案

保留既有功能、目录平铺、用量与记忆改造以及审计修复。取消统一 Feature 基类、features 列表遍历和 session.feature(X)。内置功能使用普通服务；app 显式创建、初始化、连接具体服务，不实现插件注册器、通用生命周期调度器或新的多层包装。

kernel 放共享数据、纯函数和确有多个使用方的契约。核心按 kernel ← models ← context ← tools ← agent ← session 单向运行时依赖；业务模块依赖核心，彼此的协作由 app 注入函数或明确对象；ui、app 在上层。session 可以通过 TYPE_CHECKING 注解具体服务入口，不为避免类型引用新增查询容器。包内直接调用具体实现，跨包从包根导入。

技术栈保持 Python、LangChain/LangGraph、Deep Agents、LangMem、Textual、fastmcp 等现有锁定依赖，不引入新依赖。文件数和 Protocol 数作为报告数据，不作为硬性设计目标。

## 2. 公共接入点与调用语义

### 2.1 工具权限和钩子

ToolExecutor 接收 registry、validator、核心 approval、run，以及明确的 gates、before_tool_call、after_tool_call 函数序列；不接收任何 Feature 对象列表。函数类型用 Callable/Awaitable 别名，返回 BeforeToolCallResult/AfterToolCallResult；两个钩子都为 async。

1. 参数转换和校验完成后，按 app 指定顺序执行 gate：用户 gate、sandbox、plans、skills、memory。任何拒绝优先；有 allow 不能提前跳过后续 gate；无拒绝且至少一个 allow 才跳过核心审批。
2. 授权通过后执行 before 钩子。先运行 hooks 的拦截，再做 snapshot 和 skills 的执行准备；loopguard 如需工具分类由明确的准备函数接入。任一 block 阻止执行，after 钩子不运行。
3. 仅对实际执行过的工具执行 after 钩子，包括执行返回错误结果的情况。顺序由 app 固定为 snapshot、skills、plans、hooks（只注册确有实现的处理函数）；每个收到前者修改后的结果，最后统一持久化，再发 tool_execution_end。
4. 返回字段按整体替换语义处理，不做隐式深合并；修改 details 的处理方须保留其他处理方已写入的元数据。测试验证快照与技能信息可以同时保留。
5. 工具钩子是业务接入点，不延伸为统一服务基类。注册真实函数，不用默认空方法填满列表。

共享 ToolGate、工具钩子结果类型放 kernel/tools.py；必要运行范围数据放 kernel/runtime.py（只存数据，无服务容器或查找功能）。CommandRunner 等别名归实际调用边界。已有 SessionScope/RunScope 如有多个真实使用者可保留数据形态，不因删 Feature 机械重建所有参数。

### 2.2 上下文和事件

- ContextProjector 接收实际投影函数及它们处理的 checkpoint 键；只注册 tasks、plans、MCP 等实际贡献者。顺序固定，测试真实键不冲突，不创建新的投影插件层。
- PromptService 接收实际提示提供函数。上下文变换、工具筛选只接收所需函数；既有只读过滤先于 plans/skills 过滤。
- EventStream 复用 EventConsumer.on_event。仅注册真正消费事件的对象，业务消费者在 UI 之前；不要扫描所有服务。
- tasks 的步骤推进与 loopguard 重置由 app 明确连接，不新增依赖工具名的跨模块协议。保留已有对外事件格式。

### 2.3 生命周期与错误

app 按真实依赖初始化服务，使用 AsyncExitStack 注册实际资源的关闭动作，关闭按逆序。构造时资源尚未打开，初始化若部分失败，由该服务清理其已获得资源；已完成初始化的服务由外层 stack 清理。会话与 run 生命周期分开，不能重复关闭共享服务。

编程异常正常向外传播，不 log 后继续；明确工具错误按工具边界转为结果。hooks 命令失败保持 fail-closed。MCP 的服务器连接失败保留现有服务器不可用处理。唯一后台异常例外是 memory.background 的一层 except Exception + logger.debug，不影响 run；手动 extract 异常仍传给调用方。

### 2.4 验收

测试真实行为：gate 拒绝优先、审批后才执行准备、block 不执行工具/after、after 链式覆盖、事件与持久化顺序、初始化失败清理、运行资源关闭、可选配置的实际行为。删除 Feature 默认空实现和“删除任意列表一行即可卸载”的测试目标，不删除业务回归覆盖。

## 3. 模块归类与迁移后的文件清单

每个文件第一行写 `# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/<路径>`，或者 `# Pi 没有，pycode 新增`（来自 OpenCode/MewCode 的写成 `# Pi 没有，pycode 新增；参照 <出处 URL>`，SHA-256 行保留在第二行）。不保留旧路径的 re-export，不加薄封装；无用途的单实现 Protocol 删除，必要契约按实际用途保留。文件清单是职责划分，不硬凑数量。

### 3.1 核心

| 包 | 迁移后的文件 | 来源（现在的文件） | 现有文件数 |
|---|---|---|---|
| `kernel/` | `__init__.py`、`runtime.py`（共享范围数据）、`messages.py`、`events.py`（事件类型 + `EventStream` + 内存 journal）、`history.py`（含 `history_tree`）、`tools.py`（`ToolSpec`、`ToolContext`、`AgentTool`）、`work_graph.py`、`errors.py`、`values.py`（含 hashing、redaction）、`text.py`（truncation、token_estimate）、`io.py`（path_io、file_lease、timing） | `kernel/domain/*`、`kernel/ports/*`、`kernel/services/events.py`、`kernel/adapters/*` | 30 → 11 |
| `models/` | `__init__.py`、`types.py`（ProviderConfig、ModelDescriptor、Usage、RetryPolicy、network）、`catalog.py`（目录 + 目录文件 + SDK 发现）、`control.py`（ModelControl）、`factory.py`（含 DeepSeek 回传子类）、`messages.py`、`requests.py`（model_requests + failures + 请求账本 SQLite）、`metered.py`、`usage.py` | `models/**`，删除 `observation.py`、`ports/*`、`module.py`；`calibration.py` 移到 agent | 28 → 9 |
| `context/` | `__init__.py`、`types.py`、`projector.py`、`prompts.py`（PromptService + 指令文件 + 系统提示投影）、`summary.py`（服务 + 工作流 + 摘要源 + 摘要文件）、`compaction.py`（compaction、retention、history_boundaries、file_ops、tool_pairing）、`budget.py`（context_budget、context_meter）、`eviction.py` | `context/**`；`domain/tasks.py`、`services/task_projection.py` 移到 tasks | 32 → 8 |
| `tools/` | `__init__.py`、`executor.py`（ToolExecutor、ToolRegistry、错误边界、失败计数）、`settlement.py`、`permissions.py`（核心审批门、ReadOnlyToolGate、gate 组合）、`backend.py`（PycodeBackend、`ScopedBackend`）、`rg.py`、`output.py`（临时目录大输出）、`schema.py`、`call_state.py`、`framework_tool.py`、`discovery.py`（ToolSearch）、`ask_user.py`、`builtin/{read,write,edit,grep,find,ls,paths}.py` | `tools/**`；`progress_guard.py` 移到 loopguard，`plan_gate.py` 移到 plans，`MemoryToolGate` 移到 memory | 44 → 18 |
| `agent/` | `__init__.py`、`runtime.py`（AgentRuntime）、`loop.py`（HistoryMiddleware）、`context.py`（RunContext、RunState、turn_policy）、`boundaries.py`（事件回调、save_message、deliver_input、filter_model_tools）、`calibration.py` | `agent/**`、`models/adapters/calibration.py` | 15 → 6 |
| `session/` | `__init__.py`、`agent_session.py`、`store.py`（SqliteSessionStore + codec + repository + layout）、`inputs.py`（DurableInputs、PendingInputs）、`restart.py`、`view.py`、`sessions.py`（列表、解析、命名）、`storage_gc.py`（含 objects）、`history_backend.py` | `session/**`；revert 相关全部删除（移到 snapshot） | 37 → 9 |
| `ui/` | `ui/adapters/*.py` 平铺到 `ui/*.py`；删除 `ui/ports/`、`ui/module.py`、`recovery.py` 的 revert 恢复部分 | `ui/**` | 21 → 17 |
| `app/` | `__init__.py`、`__main__`（保留在包外）、`api.py`、`composition.py`（唯一组装入口，含原 `bootstrap.py`、`composition/*`、`lifecycle/*`）、`config.py`（settings + providers + credentials）、`cli/{__init__,main,parser,commands}.py` | `app/**` | 25 → 8 |

### 3.2 可选功能模块

| 模块 | 迁移后的文件 | 服务提供的实际能力（由 app 显式连接） | 来源 |
|---|---|---|---|
| `sandbox/` | `__init__.py`、`types.py`（SandboxSettings、SandboxStatus）、`sandbox.py`（`SandboxService`、Seatbelt profile、`run_command`）、`policy.py`（command_policy）、`process.py`（process_runner、subprocess_env、windows_job）、`tools.py`（bash） | `tools`、`gates`（命令拒绝名单、on-auto 只读命令自动放行） | `sandbox/**`；`SeatbeltSandbox/UnavailableSandbox/RequiredSandbox` 合并成一个 `Sandbox` 类（`required` 参数、非 darwin 时 status 为不可用） |
| `snapshot/` | `__init__.py`、`types.py`、`snapshot.py`（`SnapshotService`、`/revert`）、`store.py`（git tree I/O） | `before_tool_call`/`after_tool_call`（拍快照，写 `details.file_snapshot`）、`open` | `snapshot/**`、`session/services/revert.py`（重写，见第 6 节） |
| `memory/` | `__init__.py`、`types.py`、`memory.py`（`MemoryService`、服务、后台整理、写入 gate）、`store.py`（Markdown 记忆文件 + LangMem BaseStore）、`tools.py` | `open`、`tools`、`mounts`、`gates`（自己的工具免审批）、`prompt_sections`（索引）、`on_event`（`compaction` 采用后触发整理；B2）、`aclose` | `memory/**`、`tools/services/permissions.py::MemoryToolGate` |
| `skills/` | `__init__.py`、`types.py`、`skills.py`（`SkillsSession`、Skills 服务、`SkillsMiddleware` 子类、scope gate）、`store.py`（catalog）、`install.py`、`tools.py` | `open`、`prepare_run`、`tools`、`mounts`、`middleware`、`gates`、`select_tools`、`before_tool_call`/`after_tool_call`、`on_event`（SkillSearch 结束后更新选择） | `skills/**`；`SkillBackend` 改用 `ScopedBackend` |
| `mcp/` | `__init__.py`、`types.py`（含配置解析）、`mcp.py`（`MCPService`、目录服务、投影）、`client.py`（连接）、`tools.py` | `open`、`prepare_run`、`tools`、`contribute`（`mcp`）、`aclose` | `mcp/**`；MCP 工具改为普通 `AgentTool`（`MCPTool.execute` 直接调连接），删除 `NativeToolBinding`/`MCPRun`【已确认】 |
| `hooks/` | `__init__.py`、`types.py`、`hooks.py`（`HooksService`、引擎、条件、动作）、`config.py` | `open`（`session_start`）、`before_tool_call`（`pre_tool_use`，fail-closed）、`on_event`（其余生命周期点）、`aclose`（`shutdown`） | `hooks/**` |
| `plans/` | `__init__.py`、`types.py`、`plans.py`（`PlansService`、服务、gate、投影）、`store.py`（plan 文件，基于 `ScopedBackend`）、`tools.py`（ExitPlanMode） | `open`、`tools`、`mounts`、`gates`、`select_tools`、`contribute`（`plan`）、`after_tool_call`（写 plan 文件后更新 `document_exists`）、`on_event`（`agent_end` 时结束 plan） | `plans/**`、`tools/services/plan_gate.py`、`agent/adapters/boundaries.py` 里的 planning 过滤、`settlement.py:570-575` |
| `tasks/` | `__init__.py`、`tasks.py`（`TaskBoardService`、任务板视图、证据检查）、`tools.py`（TaskWrite、TaskUpdate） | `tools`、`contribute`（`task_board`）、`transform_context`（每轮视图、压缩后完整视图、结束前提醒）、`on_event`（`agent_end` 的 `loop_detected` 把任务板设为 failed） | `plans/adapters/tools/task_board.py`、`context/domain/tasks.py`、`context/services/task_projection.py`、`agent/adapters/history.py` 的 `fail_board`/`board_exit_reminder`、`boundaries.py::task_board_messages`。`Board/Step` 和 `work_graph` 留在 kernel（plans 审批也要用） |
| `subagents/` | `__init__.py`、`types.py`、`subagents.py`（`SubagentService`、manager）、`store.py`（git worktrees）、`tools.py` | `open`、`tools`、`transform_context`（活跃子 agent 列表）、`on_event`（父 run 取消时停前台子 agent）、`aclose` | `subagents/**`、`app/composition/subagents.py` 的会话打开逻辑改为注入一个 `open_child` 可调用对象 |
| `loopguard/` | `__init__.py`、`loopguard.py`（`LoopGuard`） | `on_event`（`tool_execution_end` 计数；`task` 开始新步骤、`compaction` 采用时清零）、`transform_context`（第一次提醒）、`middleware`（第二次在 `abefore_model` 里 `interrupt()` 问用户，或以 `loop_detected` 结束） | `tools/domain/progress_guard.py`、`agent/adapters/history.py` 的 loop 部分 |

`context_meter` 现在单独统计任务板视图的 token。迁移后由 agent 把 `transform_context` 产生的尾部消息交给 meter 一起估算，meter 不再认识任务板。

## 4. 保留的接口

保留 AgentTool、ToolGate、EventConsumer，以及直接使用的框架 BaseChatModel、BackendProtocol、AgentMiddleware、BaseStore。具体服务使用具体类型，传行为使用函数；不得为减少 Protocol 数改成 Any，或为每种能力新增 Protocol。最终数量由实际使用决定。

删除 Feature 基类和只服务于它的通用查找/分发代码。仍使用的范围数据和工具结果类型迁到对应文件，所有 import 同步修改，无兼容 re-export。原计划中的单实现接口清理继续按调用方逐项实施，删除前确认没有实际多实现需求。

## 5. app 的显式组装

app/composition.py 创建 store、model control、plans、skills、memory、sandbox、snapshot、hooks、tasks、loopguard 和按配置启用的 MCP/subagents。它负责：

- 将 sandbox.run_command 直接注入 hooks，明确生命周期依赖。
- 将有状态服务提供的实际工具加入 registry，将 memory/skills/plans 的挂载合并交给框架 backend；冲突使用现有 registry/挂载边界处理。
- 构图前明确刷新 skills 和 MCP，不遍历所有服务的 prepare_run。
- 将实际 middleware、上下文变换、投影和工具筛选函数交给对应核心对象。
- 按第 2 节组装工具 gate 和前后钩子；连接任务推进和 loopguard 重置。
- 将会话事件流传给 ModelControl，确保 model_change 发出。
- 以 AsyncExitStack 管理实际资源，保留已存在的 read_only、worker、allow_delegation 等配置含义。

AgentSession 提供 memory、skills、plans、tasks、subagents、snapshots 等具体入口；可选性仅来自真实配置，不提供 feature(type) 查询。CLI/UI 直接调用这些服务，不增加一层同名转发方法。app/api.py 的嵌入入口仍要求传入 store。

集成测试覆盖实际配置下的工具/命令/挂载、事件接线、初始化失败和正常关闭，不要求任意模块都能拔掉。

## 6. 第 33.2 节 #1：`/revert` 简化【已确认】

### 6.1 行为与目标边界

CLI 为 `pycode revert <target> --mode dialogue|code|both`；TUI 选目标和模式后确认一次。以下是本轮实现采用的明确语义：

1. 普通 target 表示“回到该条目之后”，不包含 target 自己的改动；root 表示分支根之前。target 必须在当前分支。code/both 收集 target 之后的工具 details.file_snapshot，以及 custom:revert 的 file_snapshot，每条路径取第一次 before 和最后一次 after。
2. target 如果是 custom:revert，表示“撤销该次 revert”，读取该记录自身的 file_snapshot，恢复到它的 before。为避免覆盖之后的改动，只有该记录为当前 leaf 时才允许这种撤销，否则报 revert_branch_conflict；UI 明确显示该限制。撤销也生成新 revert 记录，因此可以撤销刚发生的撤销。
3. code/both 先 capture 当前树，再比较受影响路径的当前 blob 与预期 after；任一冲突则不写文件。逐条从目标 before 树写回；原先不存在的路径删除。dialogue 不需要 git，也不生成 file_snapshot。
4. 每次操作记录 previous_leaf 和 resulting_leaf。普通 dialogue/both 分支到 target；撤销 revert 的 dialogue/both 恢复该记录的 previous_leaf（它可在另一历史分支中，但必须仍存在于同一会话）。code 模式不移动对话 leaf。不同模式只恢复所选部分，不暗中恢复另一部分。
5. 以 custom:revert 记录 target、mode、previous_leaf、resulting_leaf、实际 paths，以及存在文件修改时的 file_snapshot={before,after,paths}。记录追加在操作后的分支上；快照路径只包含本次实际改动，避免撤销误覆盖未写路径。
6. 写到一半失败：停止写入，不执行后续对话切换；尝试拍实际 after 并记录成功部分，向调用方报 revert_partial 和可用的记录 ID。只有记录成功才能提示“可撤销”。如果拍快照或保存记录再次失败，明确报告已发生部分修改及记录失败，不承诺可一键恢复，不增加补偿事务。
7. 进程被杀或存储不可写时，不保证存在恢复记录；不能宣称对旧工具条目再次 revert 一定可恢复，因为冲突检查可能拒绝。保留已拍快照供诊断，用户可使用 git 手动恢复。

以上 target 边界、原 leaf 与失败语义必须先用临时仓库和真实会话存储覆盖，再替换 CLI/UI 旧入口。

### 6.2 删除

- `session/services/revert.py`、`session/domain/revert.py`、`session/domain/revert_state.py`；`AgentSession.preview_revert/revert/recover_revert/require_settled` 中只为 revert 的部分；所有 `pending_revert(...)` 检查和 `revert_recovery_required` 错误码。
- `snapshot/adapters/git_tree.py` 的 `preview/apply/restore/recover/restore_path`、`.partial` 暂存、逐文件 `hash-object` 复核、`restores/<id>.json`；`RestorePlan/RestoreFile/RestoreRecord`、`RevertPlan`。
- 记录类型 `revert_started/revert_completed/revert_recovery` 和 `custom:file_snapshot`（审计 2 C20）：加进 `upgrade_entry` 的 `REMOVED_CUSTOM_KINDS`（这些是 custom.kind），旧会话打开时丢弃（D4 保留 `upgrade_entry`，旧格式兼容只放这一处）。
- CLI `revert preview/apply/recover` 子命令和 `--compensate`；TUI `RevertPanel` 的 apply/discard/recover 按钮和"Revert recovery required"提示。

### 6.3 用户可见变化

用一次确认替代 preview/apply。可撤销最新 revert；非 leaf 的 revert 不允许直接撤销。中途失败只在记录成功时提供可撤销入口。保留完整历史树，不引入持久补偿事务。

### 6.4 测试

覆盖：普通 target 排除自身、root、非当前分支拒绝；冲突零写入；dialogue/code/both；custom 快照参与后续普通 revert；撤销 leaf revert 恢复文件及 previous_leaf；撤销后再撤销；非 leaf 拒绝；第二个文件注入 OSError；再次 capture 或记录保存失败；非 git 的 dialogue 成功、code 报错。

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

## 8. 已确定的 D3 与 B2

D3：恢复时直接用当前 provider 配置，删除 catalog_hash/provider_hash；找不到模型仍按现有 model_unavailable 处理。

B2：memory.background 只保留一层 except Exception + logger.debug，不发事件、不影响 run；collect 不再二次吞异常，取消任务按 asyncio 取消语义处理。手动 extract 失败正常传给调用方。后台输入绑定本次被采用的压缩范围，不能因异步延迟改成另一条最新压缩；同一时间最多一个任务，连续压缩的待处理范围不能静默遗漏。

## 9. 已有工作线与实施顺序

### 9.1 顺序

先同步设计和 plan → 确认旧工作流已停止或完成接管 → 保留 B/C/D 未提交成果 → 按新方案改各线 → 串行集成 → 审查修复 → 一次最终全量 → Pi 只读审计 → 合回 main（不 push）→ 清理工作分支。

现有五条线是代码归属，不要求重新开启并行 agent。公共阶段 f1460be 已完成，保留 kernel 平铺、ScopedBackend 和架构检查成果。Feature 的新增属于旧方案历史，需要在后续删除，不重新执行旧公共阶段。

### 9.2 公共收尾

共享数据与工具契约迁到 kernel/runtime.py、kernel/tools.py，更新包根导出。先迁调用方，再删 Feature；删除空实现测试，工具顺序与 gate 行为移到真实 ToolExecutor 测试。中间只跑受影响测试，不重复全量。

### 9.3 五条已有工作线

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
- **组之间没有依赖**：跨组的代码搬家按上面的"新包所在的组创建、原包所在的组删除"拆开；sandbox→hooks 的命令执行靠 kernel 的 `CommandRunner` 类型别名和组装注入；三个组对 `ScopedBackend` 的依赖靠公共阶段先做。保留各 worktree 的现有成果，按需要串行继续。

### 9.5 集成步骤（串行，本分支）

1. 依次合并：r32-c（models 在最底层）→ r32-d（context）→ r32-a（tools）→ r32-b（agent）→ r32-e（session、ui）。按"新包所在的组创建、原包所在的组删除"的规则，各组改的文件不重叠；如果合并时出现冲突，以本文为准解决。
2. 修跨包引用：核心包里还指向旧 API 的地方（例如 `context_meter` 接收 agent 给的尾部消息）。
3. 写 `app/composition.py`（第 5 节），删掉 `app/composition/`、`app/lifecycle/`、`app/bootstrap.py`；第 41 节 **B1、C18、E1、C5（launcher 侧）** 随这些文件一起消失或改写；**E2** 把会话事件流传给 `ModelControl`。
4. 第 33.2 节 **#7 AsyncExitStack**：会话和 run 各用一个 stack，删除 `_close_task`/shield 循环、`_close_all`、`RunAssembly`、`RuntimeContainer.aclose` 的三层 try/finally、`SessionCleanup`、`models/module.py` 的 `_finish_owned_close`。
5. 跨包的第 41 节条目：
   - **E3**：`AgentSession` 的 `durable_inputs/view/storage` 改必填；业务服务通过具体入口传给 AgentSession，不使用通用查询；测试改用真实对象或 fake。
   - **E4**：`history_store`、`projector`、`durable_inputs`、`eviction` 改必填；`app/api.py` 嵌入入口要求传 store；`tests/models/adapters/test_provider_wire.py` 等测试和 `scripts/verify_live_provider.py` 改用临时目录里的 `SqliteSessionStore`；删掉 agent/tools/models 里约 20 处判空。
   - **C7**：删除 `ToolSettlementState` 的 4 个 failure 字段、`Command` 里的 `**context.failure_guard` 和 `or request.state`；`FailureGuard` 改 `total=True` 并写全初始值。
   - **C10**：全仓库去掉对已冻结对象的 `clone_value`（tool_registry、model_catalog、summary_source + summary、mcp），注册或构造时 `freeze_value` 一次。
6. CLI：`revert` 子命令改成 `pycode revert <target> --mode`（#1）；删除 `skill create/update --revision`（33.2 最后一项）；ui 里调用功能模块命令的代码改为具体服务入口。
7. 第 34 节推迟的"错误码收敛"：grep 全仓库，删除只有抛出方、没有任何分支判断它的错误码（第 34 节第 10 条），对应测试一起删。
8. `scripts/check_architecture.py` 在真实代码上通过；删除 `docs/design/port-implementations.json`、`docs/design/feature-dependencies.json`。
9. 文档：`docs/architecture.md`（新结构、依赖方向、显式组装）、`docs/pi-map.md`（阅读顺序：kernel → agent（对照 Pi agent-loop）→ tools → 任选一个功能模块 → `app/composition.py`）、`docs/source-map.md`、`README.md`、`docs/design/subagents.md` 与 `tool-discovery.md` 里的路径；`docs/interview` 重新生成，通过 `docs/interview/tools/build.py --check` 和 `check_index.py`。
10. 跑受影响测试及 ruff、pyright、架构检查；最终全量放在所有代码和审查修复完成之后，不重复执行。

第 33.2 节 6 项、第 41 节全部范围、第 32 节的结构目标都在集成步骤结束前完成；之后才进入全量验证。

### 9.6 每一项归到哪一步

| 项 | 步骤 |
|---|---|
| 第 32 节：kernel 平铺保留、Feature 改为实际公共接入点 | 公共阶段成果 + 本轮公共收尾 |
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

## 11. 验收与合并

中间仅跑受影响测试，记录实际失败/未收集项目；不使用验证 shim 伪造通过。完成代码和最多两轮审查修复后，运行 tests + evals/tests、ruff check、ruff format --check、pyright、架构检查及 interview 文档三条命令。禁用 cacheprovider，因此失败按 node ID 重跑而非 --lf。报告基线对比，不用接口数判定架构是否成功。

DeepSeek 验收按第 7 节，缺少密钥询问用户，不能宣布已验证。之后 Pi 对齐只读审计，区分允许偏离和偏差。全部满足后 rebase 到 main，再 ff-only 合并，不 push；保留未提交文件，确认成果可达后才能删除 worktree/分支。

## 12. 最新确认决定

以根目录 11.1.md 为依据：取消统一 Feature，使用普通服务和显式组装；保留必要工具 gate/async 前后钩子；session 使用具体服务入口；不以 Protocol 数量或任意模块可拔插作为目标。原有业务能力、平铺、审计修复、ScopedBackend、输入去重、usage_metadata、model_change、记忆与 revert 改造继续执行。

记忆仅提取被压缩范围的 user 原文；摘要仅背景，不记录推断；手动 extract 使用最近 user 消息；没有值得记的则不修改。保留原字数/条数上限和明确偏好/约定/决定的指令。测试范围、角色过滤、连续压缩和“没有就不改”。

本文同步不代表代码已实现。当前 HEAD/未提交进度及任务状态以 11.1.md、plan 和实时 git 状态共同核实。
