# Pi 没有，pycode 新增
"""Explicit session and run assembly; AsyncExitStack owns each acquired lifetime."""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import httpx
import httpx2
from deepagents.backends import CompositeBackend, StateBackend
from langchain_core.language_models import BaseChatModel

from ..agent import AgentRuntime, RunContext, RunLauncher, RunOptions, RuntimeContainer
from ..context import (
    ConfiguredSummaryGenerator,
    ContextProjector,
    FileSummaryStore,
    InputBudget,
    PromptService,
    SummaryService,
    SummarySettings,
    SummaryWorkflow,
    instruction_files,
)
from ..hooks import HooksService
from ..kernel import (
    EntryDraft,
    EventStream,
    MemoryEventJournal,
    RunScope,
    SessionScope,
    SystemTiming,
    ToolSpec,
    plain_data,
)
from ..loopguard import LoopGuard
from ..mcp import MCPAdapterConnections, MCPService, load_servers
from ..memory import MemoryService
from ..models import (
    FileCatalogStore,
    MeteredChatModel,
    ModelCatalog,
    ModelControl,
    ModelRequestRunner,
    ProviderFailureClassifier,
    RetryPolicy,
    SDKModelDirectory,
    SQLiteRequestStore,
    UsageService,
    create_model,
    model_request_step,
    model_selection,
    stop_reason,
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
    Write,
    make_framework_tool,
)
from .config import AppConfig, credential_environment


def projector():
    return ContextProjector(
        (PlanProjection().contribute, task_contribution),
        checkpoint_keys=frozenset({"plan", "task_board"}),
        cache=False,
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


@dataclass(frozen=True, kw_only=True)
class SessionResources:
    session_id: str
    history_store: SqliteSessionStore
    tools: tuple = ()
    consumers: tuple = ()


@dataclass
class SessionServices:
    """Concrete services shared by run construction; no execution or lifecycle dispatch."""

    config: AppConfig
    scope: SessionScope
    consumers: tuple
    event_journal: MemoryEventJournal
    projector: ContextProjector
    tools: tuple
    sandbox: SandboxService
    snapshots: SnapshotService
    skills: SkillsSession
    plans: PlanService
    memory: MemoryService
    tasks: TaskBoardService
    loopguard: LoopGuard
    hooks: HooksService
    mcp: MCPService | None
    subagents: SubagentManager | None
    summary: SummaryService | None = None


def build_file_backend(services: SessionServices, run: RunScope, outputs: TempOutputStore):
    """Connect workspace, plan, history, memory and skill file mounts."""
    config, store = services.config, services.scope.history
    workspace_files = PycodeBackend(
        config.workspace,
        read_only=config.read_only,
        excluded_roots=(config.user_data,),
        write_roots=config.sandbox.write_roots,
    )
    routes = {
        "/workspace/": workspace_files,
        "/plans/": services.plans.files.backend,
        "/conversation_history/": ConversationHistoryBackend(store),
        str(outputs.directory) + "/": PycodeBackend(outputs.directory, read_only=True),
    }
    routes.update(services.memory.mounts(run))
    routes.update(services.skills.mounts(run))
    return CompositeBackend(StateBackend(), routes), workspace_files


def build_tools(services: SessionServices, provider, run: RunScope, backend, outputs):
    """Register tools and connect the executor's explicit gates and callbacks."""
    config = services.config
    question = InterruptQuestionPrompt()
    discovery = ToolDiscovery(make_framework_tool)
    enabled = (
        (
            *services.tools,
            *services.memory.tools(run),
            *services.skills.tools(run),
            *services.tasks.tools(),
            ExitPlanMode(services.plans),
            *((*subagent_tools(services.subagents),) if services.subagents is not None else ()),
            *(services.mcp.tools() if services.mcp is not None else ()),
            Read(backend),
            Write(backend),
            Edit(backend),
            Grep(backend),
            Find(backend),
            Ls(backend),
            AskUserQuestion(question),
            discovery,
            *services.sandbox.tools(run),
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
        *services.sandbox.gates(run),
        services.plans,
        *services.skills.gates(run),
        *services.memory.gates(run),
    )
    executor = ToolExecutor(
        registry,
        validator,
        ReadOnlyToolGate()
        if config.read_only
        else InterruptToolGate(on_request=partial(services.hooks.permission_request, run)),
        run,
        outputs,
        gates=gates,
        before_tool_call=(
            services.hooks.before_tool_call,
            services.snapshots.before_tool_call,
            services.skills.before_tool_call,
            services.loopguard.before_tool_call,
        ),
        after_tool_call=(
            services.snapshots.after_tool_call,
            services.skills.after_tool_call,
            services.plans.after_tool_call,
            services.hooks.after_tool_call,
        ),
    )
    return registry, executor, discovery


async def build_run(services: SessionServices, options: RunOptions) -> RuntimeContainer:
    """Events/files → tools → model/context services → RunContext → AgentRuntime."""
    config, provider, store = services.config, options.provider, services.scope.history
    async with AsyncExitStack() as stack:
        consumers = (
            services.loopguard,
            services.tasks,
            services.plans,
            services.skills,
            services.hooks,
            *((services.subagents,) if services.subagents is not None else ()),
            *services.consumers,
        )
        events = EventStream(
            services.scope.session_id,
            options.run_id,
            consumers,
            journal=services.event_journal,
            agent_id=config.agent_id,
        )
        run = RunScope(services.scope, options.run_id, events)
        await services.skills.prepare_run(run)
        outputs = TempOutputStore(services.scope.session_id)
        backend, workspace_files = build_file_backend(services, run, outputs)
        registry, executor, discovery = build_tools(services, provider, run, backend, outputs)
        owner = await open_model(
            config,
            provider,
            consumers,
            services.scope.session_id,
            options.run_id,
            options.purpose,
            events=events,
            retry_policy=options.retry_policy,
            agent_id=config.agent_id,
        )
        stack.push_async_callback(owner.aclose)
        budget = InputBudget.create(provider.context_window, provider.max_output_tokens)
        settings = SummarySettings(
            owner.requests.selection,
            options.effective_tools or registry.list_specs(),
            budget,
            config.summary_keep_recent_tokens,
        )
        generator = summary_generator(config, provider, consumers)
        if services.summary is None:
            services.summary = SummaryService(
                store,
                services.projector,
                FileSummaryStore(layout(config).project / "summaries"),
                generator,
                settings,
                events,
                on_compacted_source=services.memory.compacted_source,
            )
        else:
            services.summary.configure(settings, generator, events)

        context = RunContext(
            session_id=services.scope.session_id,
            run_id=options.run_id,
            workspace=str(config.workspace),
            selection=owner.requests.selection,
            events=events,
            history=store,
            executor=executor,
            registry=registry,
            inputs=PendingInputs(),
            requests=owner.requests,
            max_turns=config.max_turns,
            scope=run,
            projector=services.projector,
            durable_inputs=DurableInputs(store),
            agent_id=config.agent_id,
            workspace_files=workspace_files,
            model_tools=options.effective_tools,
            read_only=config.read_only,
            summary=services.summary,
        )
        instructions, instruction_backend = instruction_files(
            config.workspace, config.user_data, trusted=config.project_trusted
        )
        runtime = AgentRuntime(
            owner.model,
            run_hook=services.hooks.run_hook,
            discovery=discovery,
            middleware=(
                *services.loopguard.middleware(run),
                TaskBoardMiddleware(),
                *services.skills.middleware(run),
            ),
            context_transforms=(
                services.tasks.transform_context,
                *(
                    (services.subagents.transform_context,)
                    if services.subagents is not None
                    else ()
                ),
                services.loopguard.transform_context,
            ),
            tool_filters=(services.plans.select_tools, services.skills.select_tools),
            before_finish=services.tasks.before_finish,
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
) -> AgentSession:
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
            mcp = MCPService(connections)
            stack.push_async_callback(mcp.aclose)
            await mcp.initialize()
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
        services = SessionServices(
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
        launcher = RunLauncher(
            config.workspace,
            PromptService(scope, (memory.prompt_sections,), suffix=prompt_suffix),
            partial(build_run, services),
            hooks.run_hook,
        )
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
) -> AgentSession:
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
        container = await session.launcher.create_run(options)
        session.launcher.summary = container.context.summary
    except BaseException:
        await session.aclose()
        raise
    # The borrowed store remains open; app-created service lifetimes follow the container.
    container.closing.push_async_callback(session.aclose)
    return container
