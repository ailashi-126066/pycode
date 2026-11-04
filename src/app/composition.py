# Pi 没有，pycode 新增
"""Explicit session and run assembly; AsyncExitStack owns each acquired lifetime."""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import httpx2
from deepagents.backends import CompositeBackend, StateBackend
from langchain_core.language_models import BaseChatModel

from ..agent import AgentMessagePublisher, AgentRuntime, RunContext, RuntimeBindings
from ..context import (
    ConfiguredSummaryGenerator,
    ContextProjector,
    EvictionBudget,
    FileSummaryStore,
    InputBudget,
    PromptService,
    SummaryService,
    SummarySettings,
    SummaryWorkflow,
    ToolEvictionService,
    instruction_files,
)
from ..hooks import HooksService
from ..kernel import (
    EntryDraft,
    EventStream,
    MemoryEventJournal,
    RunScope,
    SessionScope,
    StorageError,
    SystemTiming,
    ToolSpec,
    plain_data,
)
from ..loopguard import LoopGuard
from ..mcp import MCPAdapterConnections, MCPService, load_servers, mcp_projection
from ..memory import MemoryService
from ..models import (
    FileCatalogStore,
    MeteredChatModel,
    ModelCatalog,
    ModelControl,
    ModelRequestRunner,
    ProviderConfig,
    ProviderFailureClassifier,
    RetryPolicy,
    SDKModelDirectory,
    SQLiteRequestStore,
    UsageService,
    create_model,
    from_assistant,
    model_request_step,
    model_selection,
    stop_reason,
    to_framework,
    to_framework_messages,
)
from ..plans import (
    ExitPlanMode,
    FilePlanStore,
    InterruptPlanPrompt,
    PlanProjection,
    PlanService,
)
from ..sandbox import SandboxService, find_bash
from ..session import (
    AgentSession,
    ConversationHistoryBackend,
    DurableInputs,
    PendingInputs,
    SessionService,
    SessionView,
    SqliteSessionRepository,
    SqliteSessionStore,
    StorageLayout,
    assign_initial_title,
    pending_run_entry,
    recover_conversation,
)
from ..skills import SkillsSession
from ..snapshot import GitSnapshotStore, SnapshotService
from ..subagents import GitWorktrees, SubagentManager, child_prompt, subagent_tools
from ..tasks import TaskBoardMiddleware, TaskBoardService, plan_board, task_contribution
from ..tools import (
    AskUserQuestion,
    Edit,
    Find,
    Grep,
    InterruptQuestionPrompt,
    InterruptToolGate,
    JsonSchemaValidator,
    Ls,
    PycodeBackend,
    Read,
    ReadOnlyToolGate,
    TempOutputStore,
    ToolDiscovery,
    ToolExecutor,
    ToolRegistry,
    ToolSettlementMiddleware,
    Write,
    make_framework_tool,
)
from .config import credential_environment


def projector():
    return ContextProjector(
        (PlanProjection().contribute, task_contribution, mcp_projection),
        checkpoint_keys=frozenset({"plan", "task_board", "mcp"}),
    )


def layout(config):
    return StorageLayout(config.workspace, config.user_data)


def repository(config):
    return SqliteSessionRepository(layout(config).sessions, str(config.workspace), SystemTiming())


def model_catalog(config, providers):
    timing = SystemTiming()
    return ModelCatalog(
        providers,
        SDKModelDirectory(
            credential_environment(config, providers), ProviderFailureClassifier(timing)
        ),
        FileCatalogStore(config.user_data / "model-catalog.json"),
        timing,
    )


def request_records(config):
    return SQLiteRequestStore(config.request_database or layout(config).runtime_database)


@dataclass
class ModelOwner:
    model: BaseChatModel
    requests: ModelRequestRunner
    records: SQLiteRequestStore
    closing: AsyncExitStack

    async def aclose(self):
        await self.closing.aclose()


async def open_model(
    config,
    provider,
    consumers,
    session_id,
    run_id,
    purpose,
    *,
    events=None,
    retry_policy=None,
    agent_id="lead",
):
    async with AsyncExitStack() as stack:
        records = request_records(config)
        stack.push_async_callback(records.aclose)
        transport = httpx2 if provider.implementation == "anthropic" else httpx
        client = stack.enter_context(transport.Client())
        async_client = await stack.enter_async_context(transport.AsyncClient())
        model = create_model(
            provider,
            credential_environment(config, (provider,)),
            http_client=client,
            http_async_client=async_client,
        )
        if events is None:
            journal = MemoryEventJournal()
            stack.push_async_callback(journal.aclose)
            events = EventStream(session_id, run_id, consumers, journal=journal, agent_id=agent_id)
        timing = SystemTiming()
        requests = ModelRequestRunner(
            records,
            ProviderFailureClassifier(timing),
            timing,
            retry_policy or RetryPolicy(),
            events,
            model_selection(provider),
            session_id,
            run_id,
            purpose=purpose,
            agent_id=agent_id,
        )
        return ModelOwner(model, requests, records, stack.pop_all())


@asynccontextmanager
async def background_model(
    config,
    provider,
    consumers=(),
    *,
    session_id,
    run_id,
    purpose,
    logical_prefix,
    thinking=False,
    retry_policy=None,
    max_output_tokens=4096,
    input_limit=16000,
    max_calls=24,
    request_prefix=(),
):
    selected = replace(
        provider,
        max_output_tokens=max_output_tokens,
        thinking=provider.thinking if thinking else {},
        reasoning_effort=provider.reasoning_effort if thinking else None,
    )
    if request_prefix:
        # Full source prefixes can be as large as the conversation being compacted;
        # the ordinary small-component budget would reject them before transport.
        input_limit = InputBudget.create(
            selected.context_window, selected.max_output_tokens
        ).input_limit
    owner = await open_model(
        config, selected, consumers, session_id, run_id, purpose, retry_policy=retry_policy
    )
    try:
        yield MeteredChatModel(
            model=owner.model,
            requests=owner.requests,
            logical_prefix=logical_prefix,
            input_limit=input_limit,
            max_calls=max_calls,
            request_prefix=request_prefix,
        )
    finally:
        await owner.aclose()


def summary_generator(config, provider, consumers):
    async def model_factory(selected, session_id, run_id, purpose):
        return await open_model(config, selected, consumers, session_id, run_id, purpose)

    return ConfiguredSummaryGenerator(
        provider,
        model_factory,
        partial(request_records, config),
        partial(SummaryWorkflow, request_step=model_request_step, stop_reason=stop_reason),
    )


def build_runtime_bindings(*, run_hook, discovery=None):
    if discovery is None:
        discovery = ToolDiscovery(make_framework_tool)
    return RuntimeBindings(
        to_framework=to_framework,
        to_framework_messages=to_framework_messages,
        from_assistant=from_assistant,
        make_framework_tool=make_framework_tool,
        disclose_model_tools=discovery.disclose,
        model_request_step=model_request_step,
        run_hook=run_hook,
        settlement_middleware_factory=lambda context: ToolSettlementMiddleware(
            publish=AgentMessagePublisher(context).publish, converter=to_framework
        ),
    )


@dataclass(frozen=True, kw_only=True)
class RunOptions:
    provider: ProviderConfig
    run_id: str
    agent_id: str = "lead"
    purpose: str = "agent"
    retry_policy: RetryPolicy | None = None
    eviction_budget: EvictionBudget | None = None
    checkpointer: object | None = None
    effective_tools: tuple[ToolSpec, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SessionResources:
    session_id: str
    history_store: SqliteSessionStore
    tools: tuple = ()
    consumers: tuple = ()


@dataclass
class RuntimeContainer:
    runtime: AgentRuntime
    context: RunContext
    request_store: SQLiteRequestStore
    closing: AsyncExitStack

    async def aclose(self):
        await self.closing.aclose()


class SessionLauncher:
    def __init__(
        self,
        config,
        scope,
        consumers,
        journal,
        projection,
        *,
        tools,
        sandbox,
        snapshots,
        skills,
        plans,
        memory,
        tasks,
        loopguard,
        hooks,
        mcp,
        subagents,
    ):
        self.config, self.scope, self.consumers = config, scope, consumers
        self.event_journal, self.projector = journal, projection
        self.tools, self.sandbox, self.snapshots = tools, sandbox, snapshots
        self.skills, self.plans, self.memory, self.tasks = skills, plans, memory, tasks
        self.loopguard, self.hooks, self.mcp, self.subagents = loopguard, hooks, mcp, subagents
        self.summary: SummaryService | None = None
        self.prompt_suffix = ""
        self._active_container = None

    async def run(self, provider, store, run_id, inputs, history):
        return await self.execute(provider, store, run_id, inputs=inputs, history=history)

    async def resume(self, provider, store, run_id, answers=None):
        return await self.execute(provider, store, run_id, resume=True, answers=answers)

    async def execute(
        self, provider, store, run_id, *, inputs=(), history=(), resume=False, answers=None
    ):
        if not self.config.workspace.is_dir():
            raise StorageError("workspace_missing", "Execution workspace no longer exists")
        container = self._active_container if resume else None
        restoring = resume and container is None
        if container is None:
            await PromptService(
                self.scope, (self.memory.prompt_sections,), suffix=self.prompt_suffix
            ).refresh()
            container = await build_run(self, RunOptions(provider=provider, run_id=run_id))
            self._active_container = container
            if restoring and answers is not None:
                await container.runtime.restore_task(container.context)
        keep = False
        try:
            if restoring and answers is None:
                result = await container.runtime.continue_task(container.context)
            elif resume:
                result = await container.runtime.resume(container.context, answers)
            else:
                result = await container.runtime.run(inputs, container.context, history=history)
            keep = bool(result.pending)
            return result
        except StorageError as error:
            if resume and error.code in {
                "invalid_resume",
                "invalid_approval",
                "invalid_question_answer",
                "invalid_answers",
                "invalid_plan_answer",
            }:
                keep = True
            raise
        finally:
            if not keep:
                self._active_container = None
                await container.aclose()

    async def cancel_wait(self, run_id):
        container = self._active_container
        if container is not None and container.context.run_id == run_id:
            self._active_container = None
            container.runtime.discard()
            await container.aclose()

    async def compact(self, provider, store):
        container = await build_run(self, RunOptions(provider=provider, run_id=uuid4().hex))
        try:
            report = await cast(SummaryService, container.context.summary).prepare(manual=True)
            if report.applied_job_id is not None:
                await self.hooks.run_hook(container.context.scope, "compact")
            return report
        finally:
            await container.aclose()

    async def invalidate_summary(self, reason):
        if self.summary is not None:
            await self.summary.invalidate(reason)

    async def aclose(self):
        async with AsyncExitStack() as stack:
            if self.summary is not None:
                stack.push_async_callback(self.summary.aclose)
                self.summary = None
            if self._active_container is not None:
                stack.push_async_callback(self._active_container.aclose)
                self._active_container = None


async def build_run(launcher, options):
    config, provider, store = launcher.config, options.provider, launcher.scope.history
    async with AsyncExitStack() as stack:
        consumers = (
            launcher.loopguard,
            launcher.tasks,
            launcher.plans,
            launcher.skills,
            launcher.hooks,
            *((launcher.subagents,) if launcher.subagents is not None else ()),
            *launcher.consumers,
        )
        events = EventStream(
            launcher.scope.session_id,
            options.run_id,
            consumers,
            journal=launcher.event_journal,
            agent_id=config.agent_id,
        )
        run = RunScope(launcher.scope, options.run_id, events)
        await launcher.skills.prepare_run(run)
        if launcher.mcp is not None and provider.supports_tools:
            await launcher.mcp.prepare_run(
                pending=pending_run_entry(await store.snapshot()) is not None
            )
        outputs = TempOutputStore(launcher.scope.session_id)
        workspace_files = PycodeBackend(
            config.workspace,
            read_only=config.read_only,
            excluded_roots=(config.user_data,),
            write_roots=config.sandbox.write_roots,
        )
        routes = {
            "/workspace/": workspace_files,
            "/plans/": launcher.plans.files.backend,
            "/conversation_history/": ConversationHistoryBackend(store),
            str(outputs.directory) + "/": PycodeBackend(outputs.directory, read_only=True),
        }
        routes.update(launcher.memory.mounts(run))
        routes.update(launcher.skills.mounts(run))
        backend = CompositeBackend(StateBackend(), routes)
        question = InterruptQuestionPrompt()
        discovery = ToolDiscovery(make_framework_tool)
        enabled = (
            (
                *launcher.tools,
                *launcher.memory.tools(run),
                *launcher.skills.tools(run),
                *launcher.tasks.tools(),
                ExitPlanMode(launcher.plans),
                *((*subagent_tools(launcher.subagents),) if launcher.subagents is not None else ()),
                *(launcher.mcp.tools() if launcher.mcp is not None else ()),
                Read(backend),
                Write(backend),
                Edit(backend),
                Grep(backend),
                Find(backend),
                Ls(backend),
                AskUserQuestion(question),
                discovery,
                *launcher.sandbox.tools(run),
            )
            if provider.supports_tools
            else ()
        )
        if config.read_only:
            enabled = tuple(tool for tool in enabled if tool.spec.category == "read")
        validator = JsonSchemaValidator()
        registry = ToolRegistry(validator, enabled)
        gates = (
            *((config.tool_gate,) if config.tool_gate is not None else ()),
            *launcher.sandbox.gates(run),
            launcher.plans,
            *launcher.skills.gates(run),
            *launcher.memory.gates(run),
        )
        executor = ToolExecutor(
            registry,
            validator,
            ReadOnlyToolGate()
            if config.read_only
            else InterruptToolGate(on_request=partial(launcher.hooks.permission_request, run)),
            run,
            outputs,
            gates=gates,
            before_tool_call=(
                launcher.hooks.before_tool_call,
                launcher.snapshots.before_tool_call,
                launcher.skills.before_tool_call,
                launcher.loopguard.before_tool_call,
            ),
            after_tool_call=(
                launcher.snapshots.after_tool_call,
                launcher.skills.after_tool_call,
                launcher.plans.after_tool_call,
                launcher.hooks.after_tool_call,
            ),
        )
        owner = await open_model(
            config,
            provider,
            consumers,
            launcher.scope.session_id,
            options.run_id,
            options.purpose,
            events=events,
            retry_policy=options.retry_policy,
            agent_id=config.agent_id,
        )
        stack.push_async_callback(owner.aclose)
        budget = InputBudget.create(provider.context_window, provider.max_output_tokens)
        eviction = ToolEvictionService(
            store,
            launcher.projector,
            options.eviction_budget or EvictionBudget(budget.input_limit),
            events,
        )
        context = RunContext(
            session_id=launcher.scope.session_id,
            run_id=options.run_id,
            workspace=str(config.workspace),
            selection=owner.requests.selection,
            events=events,
            journal=store,
            executor=executor,
            registry=registry,
            inputs=PendingInputs(),
            requests=owner.requests,
            max_turns=config.max_turns,
            scope=run,
            history_store=store,
            projector=launcher.projector,
            eviction=eviction,
            durable_inputs=DurableInputs(store),
            agent_id=config.agent_id,
            workspace_files=workspace_files,
            model_tools=options.effective_tools,
            read_only=config.read_only,
        )
        settings = SummarySettings(
            context.selection,
            options.effective_tools or registry.list_specs(),
            budget,
            config.summary_keep_recent_tokens,
        )
        generator = summary_generator(config, provider, consumers)
        if launcher.summary is None:
            launcher.summary = SummaryService(
                store,
                launcher.projector,
                FileSummaryStore(layout(config).project / "summaries"),
                generator,
                settings,
                events,
                on_compacted_source=launcher.memory.compacted_source,
            )
        else:
            launcher.summary.configure(settings, generator, events)
        context.summary = launcher.summary
        instructions, instruction_backend = instruction_files(
            config.workspace, config.user_data, trusted=config.project_trusted
        )
        runtime = AgentRuntime(
            owner.model,
            bindings=build_runtime_bindings(run_hook=launcher.hooks.run_hook, discovery=discovery),
            middleware=(
                *launcher.loopguard.middleware(run),
                TaskBoardMiddleware(),
                *launcher.skills.middleware(run),
            ),
            context_transforms=(
                launcher.tasks.transform_context,
                *(
                    (launcher.subagents.transform_context,)
                    if launcher.subagents is not None
                    else ()
                ),
                launcher.loopguard.transform_context,
            ),
            tool_filters=(launcher.plans.select_tools, launcher.skills.select_tools),
            before_finish=launcher.tasks.before_finish,
            assign_title=partial(assign_initial_title, store),
            checkpointer=options.checkpointer
            if options.checkpointer is not None
            else await store.checkpoint(),
            backend=backend,
            instructions=instructions,
            instruction_backend=instruction_backend,
        )
        return RuntimeContainer(runtime, context, owner.records, stack.pop_all())


def child_config(config, agent, *, interactive):
    return replace(
        config,
        workspace=Path(agent.workspace),
        read_only=agent.mode == "readonly",
        isolated_worker=agent.mode == "worktree",
        allow_delegation=False,
        agent_id="agent:" + agent.agent_id,
        memory_mode="off",
        max_turns=agent.max_turns,
        sandbox=replace(config.sandbox, mode="on") if agent.mode == "worktree" else config.sandbox,
        tool_gate=config.tool_gate if interactive else ReadOnlyToolGate(),
        loop_asks_user=interactive,
        credential_workspace=config.credential_workspace or config.workspace,
    )


async def assemble_session(
    config,
    providers,
    store,
    consumers,
    *,
    tools=(),
    interactive=True,
    startup_hooks=True,
    connect_mcp=True,
    prompt_suffix="",
    refresh_models=True,
    owns_history=True,
):
    if not providers:
        raise ValueError("At least one configured provider is required")
    async with AsyncExitStack() as stack:
        if owns_history:
            stack.push_async_callback(store.aclose)
        await store.claim_writer()
        pending = pending_run_entry(await store.snapshot())
        resumable = pending is not None and await store.has_checkpoint(pending.run_id)
        await recover_conversation(store, started_only=resumable, finish_runs=not resumable)
        session_id = (await store.snapshot()).header.session_id
        catalog = model_catalog(config, providers)
        await catalog.initialize()
        if refresh_models:
            await catalog.refresh()
        journal = MemoryEventJournal()
        # View owns journal and usage; registered as soon as these resources exist.
        projection = projector()
        view = build_session_view(config, store, consumers=consumers, event_journal=journal)
        stack.push_async_callback(view.aclose)
        events = EventStream(
            session_id, "session", consumers, journal=journal, agent_id=config.agent_id
        )
        scope = SessionScope(
            session_id,
            config.workspace,
            config.user_data,
            store,
            events,
            config.read_only,
            config.agent_id,
        )
        models = ModelControl(catalog, projection, store, events)
        sandbox = SandboxService(
            config.sandbox,
            required=config.isolated_worker,
            passthrough=config.subprocess_passthrough,
            path_prefix=config.bash_path_prefix,
        )
        await sandbox.open(scope)
        snapshots = SnapshotService(
            store,
            GitSnapshotStore(
                config.workspace,
                layout(config).project / "snapshots",
                excluded_roots=(config.user_data,),
            ),
        )
        plans = PlanService(
            store,
            FilePlanStore(
                config.workspace / ".pycode/plans" / session_id, backend_factory=PycodeBackend
            ),
            InterruptPlanPrompt(),
            parse_board=plan_board,
        )
        await plans.initialize()
        skills = SkillsSession(
            trusted=config.project_trusted, index_budget=config.skill_index_budget
        )
        await skills.open(scope)
        if pending_run_entry(await store.snapshot()) is None:
            await skills.service.refresh()
        sandbox.read_roots = lambda run: skills.script_roots
        memory = MemoryService(
            trusted=config.project_trusted,
            mode=config.memory_mode,
            model=partial(background_model, config, providers[0], consumers),
        )
        await memory.open(scope)
        stack.push_async_callback(memory.aclose)
        loopguard = LoopGuard(config.loop_repeat_threshold, config.loop_asks_user)
        tasks = TaskBoardService(store, events, on_step_started=loopguard.step_started)
        await tasks.initialize()
        hooks = HooksService(
            trusted=config.project_trusted,
            worker=config.agent_id != "lead",
            run_command=sandbox.run_command,
            shell=find_bash(),
            environment=credential_environment(config, providers),
            enabled=startup_hooks,
        )
        await hooks.open(scope)
        stack.push_async_callback(hooks.aclose)
        mcp = None
        if connect_mcp and config.agent_id == "lead":
            roots = [config.user_data / "mcp.json"]
            if config.project_trusted:
                roots.append(config.workspace / ".pycode/mcp.json")
            connections = MCPAdapterConnections(
                load_servers(roots, config.workspace),
                credential_environment(config, providers),
                validator=JsonSchemaValidator(),
            )
            mcp = MCPService(store, connections)
            stack.push_async_callback(mcp.aclose)
            await mcp.initialize(pending=resumable)
        subagents = None
        if config.allow_delegation:

            async def open_child(agent, consumer):
                child = child_config(
                    replace(
                        config,
                        request_database=config.request_database or layout(config).runtime_database,
                    ),
                    agent,
                    interactive=interactive,
                )
                provider = await models.restore(providers[0])
                session = await open_agent_session(
                    child,
                    (provider,),
                    agent.session_id,
                    create=True,
                    consumers=(consumer,),
                    refresh_models=False,
                    startup_hooks=False,
                    connect_mcp=False,
                    prompt_suffix=child_prompt(agent),
                )
                await store.append_entry(
                    EntryDraft(
                        "custom",
                        data={
                            "kind": "subagent_start",
                            "agent_id": agent.agent_id,
                            "name": agent.name,
                            "session_id": agent.session_id,
                            "mode": agent.mode,
                            "description": agent.description,
                        },
                        run_id=agent.run_id,
                    )
                )
                return session

            def child_events(agent):
                return EventStream(
                    session_id,
                    agent.run_id,
                    consumers,
                    journal=journal,
                    agent_id=agent.agent_id,
                    parent_call_id=agent.parent_call_id,
                )

            subagents = SubagentManager(
                open_child,
                GitWorktrees(config.workspace, layout(config).project / "worktrees"),
                child_events,
                workspace=str(config.workspace),
                max_agents=config.max_agents,
                max_turns=config.max_turns,
                can_write=lambda: (
                    not config.read_only and (plans.state is None or plans.state.mode != "planning")
                ),
            )
            stack.push_async_callback(subagents.aclose)
        launcher = SessionLauncher(
            config,
            scope,
            consumers,
            journal,
            projection,
            tools=tools,
            sandbox=sandbox,
            snapshots=snapshots,
            skills=skills,
            plans=plans,
            memory=memory,
            tasks=tasks,
            loopguard=loopguard,
            hooks=hooks,
            mcp=mcp,
            subagents=subagents,
        )
        launcher.prompt_suffix = prompt_suffix
        stack.push_async_callback(launcher.aclose)
        from ..session import FileStorageObjects, StorageGC

        storage = StorageGC(
            FileStorageObjects(config.user_data, config.workspace, owned_session_id=session_id)
        )
        session = AgentSession(
            store,
            projection,
            models,
            launcher,
            providers[0],
            DurableInputs(store),
            view,
            storage,
            closing=stack.pop_all(),
            skills=skills.service,
            plans=plans,
            memory=memory,
            tasks=tasks,
            snapshots=snapshots,
            subagents=subagents,
        )
        if subagents is not None:
            subagents.notify = session.notify_subagent
        return session


async def open_agent_session(
    config, providers, session_id, *, create=False, consumers=(), tools=(), **options
):
    repo = repository(config)
    if not create:
        session_id = await SessionService(repo).resolve(session_id)
    store = await repo.open(session_id, create=create)
    return await assemble_session(config, providers, store, consumers, tools=tools, **options)


def build_session_view(config, store, *, consumers=(), event_journal=None, owns_history=False):
    def settings(projection):
        selection = projection.selection
        if (
            selection is None
            or selection.capabilities.context_window is None
            or selection.capabilities.max_output_tokens is None
        ):
            return None
        tools = tuple(
            ToolSpec(**plain_data(item)) for item in projection.checkpoint.get("model_tools", ())
        )
        return SummarySettings(
            selection,
            tools,
            InputBudget.create(
                selection.capabilities.context_window, selection.capabilities.max_output_tokens
            ),
            config.summary_keep_recent_tokens,
        )

    return SessionView(
        store,
        projector(),
        UsageService(store, request_records(config)),
        event_journal if event_journal is not None else MemoryEventJournal(),
        consumers,
        owns_history=owns_history,
        context_settings=settings,
    )


async def open_session_view(config, session_id):
    store = await repository(config).open(session_id)
    try:
        return build_session_view(config, store, owns_history=True)
    except BaseException:
        await store.aclose()
        raise


async def embedded_runtime(config, resources, options):
    session = await assemble_session(
        config,
        (options.provider,),
        resources.history_store,
        resources.consumers,
        tools=resources.tools,
        refresh_models=False,
        owns_history=False,
    )
    try:
        container = await build_run(session.launcher, options)
    except BaseException:
        await session.aclose()
        raise
    # The borrowed store remains open; app-created service lifetimes follow the container.
    container.closing.push_async_callback(session.aclose)
    return container
