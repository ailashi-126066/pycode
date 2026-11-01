> 当前存储与恢复行为以 [存储格式](storage-format.md) 为准：完整记录使用 SqliteStore，执行位置使用 AsyncSqliteSaver。

> 当前 TeamState、任务验收、无活动续期和交接以 [TeamState 设计](design/team-state-handoff.md) 为准。

# 当前工具发现流程

2026-10-22：模型工具选择器已删除，本地与 MCP 统一采用名称/一句话分类目录和 ToolSearch 按需加载。详见 [统一工具发现](design/tool-discovery.md)。

# Final architecture and ownership

> 2026-10-22：本文包含精简前的设计/教学记录；当前 Skills、MCP、Hooks、Team 以[组件精简说明](design/component-simplification.md)和组件 README 为准。

`app/bootstrap.py:compose_application` is the single public composition entry. It returns
an Application with explicitly selected `ApplicationModules`. CLI, TUI wiring, coding evaluations
and maintenance acceptance use it. `app/composition/` contains private application/session/run
connections; its package no longer exports a catalogue of constructors.

The app root contains only bootstrap and package documentation. `app/cli/` owns input/dispatch,
`app/configuration/` owns external settings and credentials, `app/composition/` connects modules,
and `app/lifecycle/` owns assembled session/run resources. Lifecycle receives explicit factories
and cannot import composition, bootstrap or CLI. Configuration has no reverse application edge.
Package initializers contain documentation only. The installed CLI entry point is
`pycode.app.cli.main:main`; module execution uses the same function. These boundaries and the
physical app layout are enforced by the architecture checker. See `src/pycode/app/README.md`.

Each feature owns `module.py`: configuration interpretation, concrete adapter construction,
local service products and resource lifetimes. Application wiring imports these named module
entries and native public contracts, never feature-internal adapters. Feature modules do not
import app, another feature's module entry, or another feature's adapters. Optional Skills/MCP
can be replaced or disabled without changing Agent core. Child scopes rebind the chosen
providers through their own scoped hooks, preserving implementation and credential selection.
No default module set is silently rebuilt for child operations.

Source is organized as kernel, agent, session, context, models, tools, sandbox, snapshot,
memory, skills, mcp, hooks, plans, team, ui and app. Each feature has domain, ports, services
and adapters. Kernel follows the same four layers: immutable exchange facts/algorithms,
native interfaces, event delivery, and explicit file/clock/journal implementations. Its root
API does not forward I/O adapters. Built-in Skill documents and the system prompt live inside
their owning feature resources. Old global layer packages and compatibility shells are absent.

The exact permitted DAG is in `docs/design/feature-dependencies.json`. Context owns no Skill,
MCP, Memory or Plan reconstruction: immutable contributors return only their owned checkpoint
keys and entries in a fixed order. Generic usage invalidation and system-fingerprint metadata
keep counting independent of feature event names. Unknown or unsafe contributors recompute on changed history. Incremental append is used only
when every contributor explicitly guarantees safety; unchanged revisions remain cached.
The checker enforces layer purity, explicit module entry ownership, public origins, module and
feature cycles, aliases/TYPE_CHECKING/dynamic imports, and real native-port implementation coverage.

`agent/adapters/agent.py` builds the sole `deepagents.create_deep_agent` graph. Framework state
is separate from the framework-free `agent/services/context.py` RunContext and its consumer
Protocols. History middleware records model responses before tools; tool settlement owns
only its private failure-state fields. Composition injects model converters, calibration,
request metering, Skills and tool middleware. A bound message publisher preserves session Store →
output binding → snapshot fact → completion-event order; tool lifecycle callbacks run after
durable publication. Revert orchestration and conversation transactions belong to session;
snapshot owns only Git/file versions and restores.

Skill and Plan backends delegate to explicitly injected filesystem operations, rather than
inheriting another feature's adapter. Skill files remain read-only and version-pinned. Plan
resolution restricts every operation to the active file, and its search scope makes rg scan
only that file. Skill scripts execute through Bash with the existing permission boundary;
worker write roots and credential/environment restrictions remain.

FilesystemMiddleware uses PycodeBackend and explicit routes: `/workspace/`, `/plans/`,
`/skills/<source>/`, `/large_tool_results/`, `/conversation_history/`.
Workspace paths, hashes and locks are enforced by the Backend. rg supplies real regex/glob
search, ignoring internal state and default hidden paths; complete large results are preserved
before native display truncation. SkillsMiddleware owns loading/disclosure, including budgeted
catalogue reload through its public methods. LangChain MCPAdapter owns MCP connections/protocol.

Main-chat execution uses AsyncSqliteSaver in execution.sqlite. Full branch records live in
session.sqlite through SqliteStore. Explicit resume restores the graph; effects started without
a saved result receive an interrupted result. Saved business effects are not executed again. EventStream isolates failed consumers; closed CLI output requests
controlled cancellation. The session Store contains one original result; large bytes live in one scoped
object. Memory/summary/Plan metadata reference original facts rather than another conversation.

Settlement normally follows declared tool order. An interrupted call does not hold completed
later calls: their saved results settle before the native graph returns the waiting state.
Resume clears only transient interrupt barriers; confirmed results are not executed again.
The repeated-failure barrier applies only to the matching name/arguments, and yields when
that call interrupts. Each completion event and Hook retains its own operation identity.

Harness summarization is excluded through its public profile. The pycode 80% background
summary uses source/configuration hashes and two-phase safe adoption. Effective native/dynamic
tool definitions and system-prompt footprint are recorded for runtime/UI capacity consistency.
Project prompt snapshots refresh only at new tasks. Store entries are immutable, indexed once
by ID; active-path IDs and projection revision caches avoid deep-copying complete history.

AgentTeam owns asyncio child AgentSessions, a bounded atomic task board and durable inboxes.
A dedicated team JSONL contains atomic transitions, separate from the lead branch leaf;
child JSONL sessions link their lead and remain available after worktree removal.
HistoryMiddleware flushes inboxes before model calls and before closing input admission.
Wait delivery is acknowledged only after a successful ToolResult with message IDs is persisted;
idle wake inputs are confirmed from JSONL. Restart marks active children interrupted without reruns.
Readonly children expose read-only tools. Worker file tools target an owned worktree;
commands need an available strict sandbox and explicit approval, and unisolated MCP/Hook actions
are not loaded. Parent custom/Skill denials remain effective. Every merge has source-tree and
target-HEAD-bound approval outside Auto Plan, then guarded Git integration and cleanup.
Fork Skills and Hook delegates share this coordinator, preserving full/recent/no parent context.
Usage retains purpose/agent/session identity and sums children from durable team_agent links.
See [the reviewed design](design/inproc-team.md) for cancellation, conflict recovery and limits.

Plan is an exact writable document with run-bound approved hash; restart expires approvals.

Memory has two parts. AGENTS.md instructions (`~/.pycode/AGENTS.md`, plus the project's
`AGENTS.md` only with `--trust-project`) load through Deep Agents `MemoryMiddleware` with a
read-only allowlisted
backend. They are read once per run into private graph state and appended to every model call's
system message, so they never enter JSONL history and compaction/summaries cannot drop them;
the harness system footprint recorded for capacity includes them.

Long-term memory uses LangMem directly; pycode owns only storage location, trigger timing,
viewing/editing and undo. `memory/services/memory.py` (MemoryService) recalls entries before a task
(all of them up to 20, otherwise a metered selector picks up to 10), schedules one background
`create_memory_store_manager` pass when a run completes or after manual/automatic compaction, logs every
change and undoes the latest automatic pass. The model also gets LangMem
`manage_*_memory`/`search_*_memory` tools through ordinary settlement and MemoryToolGate.
Background memory models always run with thinking disabled (forced tool choice conflicts with
DeepSeek thinking); failures are logged, never retried and never reach the conversation.
Automatic passes write the user scope and, for a trusted project, the project scope. The write
filter (`memory/domain/memory.py`: length, per-scope count, credential patterns) applies to every write.
JsonMemoryStore keeps one atomic `memory.json` per scope with prefix and simple text search.

GC marks all durable references and
live/uncertain writers before 30-day quarantine/purge; memory directories are explicitly
excluded from managed objects. Worktree integration/cleanup stays separate.
Seatbelt profiles deny by default: reads cover the workspace, system directories, temp,
the running interpreter's prefixes/import roots and git global config; later rules deny
~/.ssh, ~/.aws, ~/.gnupg, ~/.config, ~/.pycode and every .env/.env.* except a workspace
.env.example. Writes stay in the workspace outside .git/.pycode and dotenv files (so they cannot
be renamed out of the read deny). Toolchains and caches under HOME are not readable.
`on-auto` approves only commands whose every segment passes the pure sandbox allow list
(`sandbox/domain/command_policy.auto_approvable`, quote-aware; unquoted globs in find/rg/git arguments are
rejected); others use the ordinary approval interrupt. Deny patterns are defence in depth ahead of both.
Evaluation runs the same harness in isolated declared-file workspaces and tests independently.

Build/tests operate here without initializing the project Git directory. Reference projects
remain read-only. Temporary Git/worktree operations are confined to tests-owned roots.
Evaluation lives in the separate `evals/` package (pycode never imports it) and keeps its
cache/work/results under `evals/` or `tests/.runtime-tests`.

Review fixes: serial tools wait for preceding durable settlement
or interrupt, rather than racing a lock and an outdated failure count. Live tool tasks are
owned and collected when persistence or a critical synchronous Hook fails.

MCPAdapter-produced BaseTools reach the graph directly. The connection gateway rechecks pinned
schemas/annotations; one boundary converts the native result to its canonical JSONL fact.
New runs refresh discovery without mutating a live run's declaration snapshot. No raw call-tool
dictionary round trip or random inner call ID remains.

rg uses its public NUL filename framing and sorted output. Python drains bounded chunks, retains
a bounded preview, and streams complete oversized matches into the existing output store. Bash
and Python progress uses the same process owner and bounded event chunks. Actual Textual render
errors are isolated in the message pump; immutable presentation views and incremental sidebar
nodes avoid repeatedly copying or rebuilding completed history.

Evaluation retains version-linked evidence before removing baseline/workspace pairs. Its optional
SDK uploader observes measured summaries after execution; submission status is distinct from
independently verified remote ingestion. Historical audit scripts and test-owned environments
are preserved outside normal collection and distributions; their confirmed cases are covered
by production regression tests.

## Ports and concrete implementations

Every Protocol declared in feature ports and shared contracts is listed below. Multiple
implementations may exist (for example, ToolGate and SandboxBackend); one concrete production
implementation is shown per port. Factory and callback rows identify the composition binding,
not the Protocol itself. `docs/design/port-implementations.json` records exact source symbols;
the architecture check verifies complete coverage and rejects missing or Protocol-only bindings.
Private adapter-local SDK Protocols are implemented by injected PycodeBackend/framework objects.

| Consumer port | Production implementation |
| --- | --- |
| `agent.ports.runtime.InputQueue` | `session.services.input_queue.PendingInputs` |
| `agent.ports.runtime.DurableInputDelivery` | `session.services.durable_inputs.DurableInputs` |
| `agent.ports.runtime.HistoryProjector` | `context.services.context_projector.ContextProjector` |
| `agent.ports.runtime.RequestTracking` | `models.adapters.model_requests.ModelRequestRunner` |
| `agent.ports.runtime.TeamMailbox` | `team.services.team.TeamMember` |
| `agent.ports.runtime.TitleAssignment` | `app.composition.runtime.BoundTitleAssignment` |
| `agent.ports.runtime.WorkspaceReadback` | `tools.adapters.backend.PycodeBackend` |
| `context.ports.summaries.SummaryGenerator` | `context.adapters.background.ConfiguredSummaryGenerator` |
| `context.ports.summaries.SummaryStore` | `context.adapters.storage.summaries.FileSummaryStore` |
| `hooks.ports.hooks.HookActions` | `hooks.adapters.actions.ConfiguredHookActions` |
| `hooks.ports.lifecycle.LifecycleHooks` | `hooks.services.hooks.Hooks` |
| `hooks.ports.lifecycle.LifecycleContext` | `agent.services.context.RunContext` |
| `hooks.ports.runtime.HookExecution` | `tools.services.tool_executor.ToolExecutor` |
| `hooks.ports.runtime.HookPlanGuard` | `plans.services.plans.PlanService` |
| `hooks.ports.runtime.HookExecutorFactory` | `app.composition.hooks.build_hook_engine` (factory/callback) |
| `kernel.ports.history.MessageJournal` | `session.adapters.storage.session.JsonlSessionStore` |
| `kernel.ports.history.HistoryReader` | `session.adapters.storage.session.JsonlSessionStore` |
| `kernel.ports.history.HistoryJournal` | `session.adapters.storage.session.JsonlSessionStore` |
| `kernel.ports.tools.ExecutableTool` | `sandbox.adapters.tools.bash.Bash` |
| `kernel.ports.events.EventConsumer` | `ui.adapters.console.ConsoleOutput` |
| `kernel.ports.events.EventSink` | `kernel.services.events.EventStream` |
| `kernel.ports.events.EventJournal` | `kernel.adapters.event_journal.MemoryEventJournal` |
| `kernel.ports.timing.Timing` | `kernel.adapters.timing.SystemTiming` |
| `mcp.ports.mcp.MCPConnections` | `mcp.adapters.manager.MCPAdapterConnections` |
| `mcp.ports.schema.SchemaChecker` | `tools.adapters.schema.JsonSchemaValidator` |
| `memory.ports.memory.MemoryUpdater` | `memory.adapters.background.ConfiguredMemoryUpdater` |
| `models.ports.context.Projector` | `context.services.context_projector.ContextProjector` |
| `models.ports.context.RunOwnership` | `session.domain.guards.SessionRunOwnership` |
| `models.ports.context.RegistryLookup` | `tools.services.tool_registry.ToolRegistry` |
| `models.ports.context.CalibrationReport` | `context.domain.summaries.SummaryReport` |
| `models.ports.context.SummaryBudget` | `context.services.summary.SummaryService` |
| `models.ports.context.CalibrationContext` | `agent.services.context.RunContext` |
| `models.ports.models.ModelFailureClassifier` | `models.adapters.failures.ProviderFailureClassifier` |
| `models.ports.models.RequestStore` | `models.adapters.storage.requests.SQLiteRequestStore` |
| `models.ports.models.ModelObservation` | `models.adapters.observation.ProviderObservation` |
| `models.ports.models.ModelDirectory` | `models.adapters.directory.SDKModelDirectory` |
| `models.ports.models.CatalogStore` | `models.adapters.storage.catalog.FileCatalogStore` |
| `plans.ports.plans.PlanFiles` | `plans.adapters.storage.plans.FilePlanStore` |
| `plans.ports.plans.QuestionPrompt` | `plans.adapters.plans.InterruptPlanPrompt` |
| `sandbox.ports.output.StreamWriter` | `tools.adapters.output_store.StreamingOutputWriter` |
| `sandbox.ports.output.OutputSink` | `tools.adapters.output_store.FileToolOutputStore` |
| `sandbox.ports.process.ProcessRunner` | `sandbox.adapters.tools.process_runner.run_process` (factory/callback) |
| `sandbox.ports.sandbox.SandboxBackend` | `sandbox.adapters.seatbelt.RequiredSandbox` |
| `session.ports.history.SessionStore` | `session.adapters.storage.session.JsonlSessionStore` |
| `session.ports.runtime.RuntimeLauncher` | `app.lifecycle.session.ConfiguredRuntimeLauncher` |
| `session.ports.sessions.SessionRepository` | `session.adapters.storage.session_repository.JsonlSessionRepository` |
| `session.ports.storage.StorageObjects` | `session.adapters.storage.objects.FileStorageObjects` |
| `session.ports.workspace.WorkspaceInspector` | `session.adapters.storage.workspace.FileWorkspaceInspector` |
| `skills.ports.guard.CatalogWriteGuard` | `session.domain.guards.SessionCatalogWriteGuard` |
| `skills.ports.guard.SchemaChecker` | `tools.adapters.schema.JsonSchemaValidator` |
| `skills.ports.sandbox.SkillReadGrant` | `sandbox.module.skill_read_grant` (factory/callback) |
| `skills.ports.skills.SkillCatalog` | `skills.adapters.catalog.FileSkillCatalog` |
| `skills.ports.skills.SkillResourceToolFactory` | `skills.adapters.python_tool.PythonSkillToolFactory` (factory/callback) |
| `skills.ports.skills.SkillCatalogCommands` | `skills.services.skills.Skills` |
| `skills.ports.skills.SkillInstaller` | `skills.adapters.install.GitHubSkillInstaller` |
| `snapshot.ports.snapshots.SnapshotStore` | `snapshot.adapters.git_tree.GitSnapshotStore` |
| `team.ports.team.ChildSession` | `session.services.agent_session.AgentSession` |
| `team.ports.team.ChildSessions` | `app.composition.team.ConfiguredChildSessions` |
| `team.ports.team.Worktrees` | `team.adapters.worktrees.WorktreeManager` |
| `team.ports.team.WorktreeSnapshot` | `snapshot.adapters.git_tree.GitSnapshotStore` |
| `team.ports.team.WorktreeSnapshotFactory` | `snapshot.adapters.git_tree.GitSnapshotStore` (factory/callback) |
| `tools.ports.hooks.ToolHooks` | `hooks.services.tool_bridge.ToolHookBridge` |
| `tools.ports.outputs.OutputWriter` | `tools.adapters.output_store.StreamingOutputWriter` |
| `tools.ports.outputs.ToolOutputStore` | `tools.adapters.output_store.FileToolOutputStore` |
| `tools.ports.outputs.SessionHeaderReader` | `session.adapters.storage.header_reader.FileSessionHeaderReader` |
| `tools.ports.publication.MessagePublisher` | `agent.adapters.boundaries.AgentMessagePublisher` |
| `tools.ports.questions.QuestionPrompt` | `tools.adapters.questions.InterruptQuestionPrompt` |
| `tools.ports.tools.ArgumentValidator` | `tools.adapters.schema.JsonSchemaValidator` |
| `tools.ports.tools.ToolGate` | `tools.services.permissions.ReadOnlyToolGate` |
| `ui.ports.ui.InteractiveSession` | `session.services.agent_session.AgentSession` |
| `ui.ports.ui.InteractiveSessions` | `app.composition.ui.ConfiguredInteractiveSessions` |
| `ui.ports.ui.ModelChoice` | `models.domain.models.ModelDescriptor` |
| `ui.ports.ui.ModelChoices` | `models.domain.models.CatalogSnapshot` |
| `ui.ports.ui.SandboxAvailability` | `sandbox.domain.sandbox.SandboxStatus` |
| `context.ports.projection.ProjectionContributor` | `context.services.prompt_projection.SystemPromptProjection` |
| `tools.ports.extensions.ToolExtension` | `skills.adapters.extension.SkillsToolExtension` |
| `tools.ports.extensions.NativeToolBinding` | `mcp.adapters.native.MCPNativeBinding` |
| `agent.ports.runtime.Runtime` | `agent.adapters.agent.AgentRuntime` |

Stage-five type boundaries add the UI's read-only ModelChoice/ModelChoices and
SandboxAvailability views; their implementations are the existing native ModelDescriptor,
CatalogSnapshot and SandboxStatus values. The private `_DataclassValue` typing helper is not
a business port. Explicit subprocess passthrough accepts only exact ordinary environment
names and still rejects credential-shaped names (including bare API_KEY/TOKEN/CREDENTIAL).
Bash, Hook and Python Skill policy hashes bind sandbox settings and the validated name list;
environment values and provider secrets never enter those policy hashes. The configured
process/grant callbacks and strict worker sandbox remain injected through the selected module entries.

## Team 框架复用边界

成员图负责 TimeoutPolicy、有效活动 heartbeat、审批等待和交接路由；engine 保留实际子执行的停止确认、成员资源、邮箱与 worktree。公共 TeamState 更新直接使用 reducer。子图 checkpoint 保存执行位置，JSONL 提供中断边界已持久化的工具事实；不复制旧 RunContext 的整套执行字段。后台摘要和工具输出清理保持原实现。

Single-agent task updates use the main model task_action/message JSON envelope. History
validates changes before publishing the assistant or executing its native tool calls.
TaskProjection replays assistant metadata from the active Store branch; compaction retains
task_state, and the graph uses a derived plain-data view. Common tools are preloaded; other
local/MCP schemas load through ToolSearch. See docs/context-request.md for request assembly.
