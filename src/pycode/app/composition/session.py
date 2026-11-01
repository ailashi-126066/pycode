"""Session scope connects feature products and transfers their owned lifetimes."""

from functools import partial

from pycode.app.composition.factories import build_memory_service, skill_settings
from pycode.app.composition.hooks import build_lifecycle_hook_engine
from pycode.app.composition.run import build_configured_run
from pycode.app.composition.subagents import build_subagents
from pycode.app.configuration.credentials import credential_environment
from pycode.app.lifecycle.session import ConfiguredRuntimeLauncher
from pycode.context import InputBudget, SummarySettings
from pycode.kernel import MemoryEventJournal, ToolSpec, plain_data
from pycode.mcp.module import MCPModule, MCPSettings
from pycode.models import UsageService
from pycode.session import (
    AgentSession,
    DurableInputs,
    RevertService,
    SessionCatalogWriteGuard,
    SessionRunOwnership,
    SessionView,
    pending_revert,
    pending_run_entry,
    recover_conversation,
)


async def open_agent_session(
    config,
    providers,
    session_id,
    *,
    create=False,
    tools=(),
    consumers=(),
    refresh_models=True,
    startup_hooks=True,
    connect_mcp=True,
    interactive=True,
    prompt_suffix="",
    modules,
) -> AgentSession:
    if not providers:
        raise ValueError("At least one configured provider is required")
    if not create:
        session_id = await modules.sessions.service().resolve(session_id)
    catalog = modules.models.catalog(providers)
    await catalog.initialize()
    if refresh_models:
        await catalog.refresh()
    store = await modules.sessions.repository().open(session_id, create=create)
    event_journal = None
    mcp_session = None
    view = None
    memory = None
    launcher = None
    try:
        await store.claim_writer()
        pending_execution = pending_run_entry(await store.snapshot())
        resumable = pending_execution is not None and await store.has_checkpoint(
            pending_execution.run_id
        )
        await recover_conversation(store, started_only=resumable, finish_runs=not resumable)
        plan_session = modules.plans.session(session_id, store)
        plans = plan_session.service
        await plans.initialize()
        skills_session = None
        current = await store.snapshot()
        if modules.skills is not None:
            skills_session = modules.skills.open_session(
                skill_settings(config), store, catalog_write_guard=SessionCatalogWriteGuard()
            )
            if pending_run_entry(current) is None and pending_revert(current) is None:
                await skills_session.initialize()
        event_journal = MemoryEventJournal()
        configured_hooks = await modules.hooks.configuration()
        if modules.mcp is not None and config.agent_id == "lead":
            mcp_session = await modules.mcp.open_session(
                MCPSettings(config.workspace, config.user_data, config.project_trusted),
                credential_environment(config, providers),
                connect=connect_mcp,
            )
        models = modules.models.control(catalog, modules.projector(), store)
        durable_inputs = DurableInputs(store)
        memory = build_memory_service(config, store, providers[0], consumers, modules=modules)
        subagents = (
            build_subagents(
                config,
                store,
                session_id,
                models,
                providers[0],
                consumers,
                event_journal,
                plans,
                interactive=interactive,
                session_opener=open_agent_session,
                modules=modules,
            )
            if config.allow_delegation
            else None
        )
        launcher = ConfiguredRuntimeLauncher(
            config,
            tools,
            consumers,
            event_journal,
            configured_hooks,
            resource_owners=(mcp_session,) if mcp_session else (),
            projector_factory=modules.projector,
            plans=plans,
            plan_backend=plan_session.files.backend,
            durable_inputs=durable_inputs,
            runtime_factory=partial(
                build_configured_run,
                modules=modules,
                skills_session=skills_session,
                mcp_session=mcp_session,
            ),
            model_resolver=catalog.resolve,
            hook_factory=partial(build_lifecycle_hook_engine, modules=modules),
            lifecycle_provider=providers[0],
            memory=memory,
            subagents=subagents,
            prompt_factory=lambda history: modules.context.prompt(
                history, config.workspace, suffix=prompt_suffix, sections=(memory.prompt_index,)
            ),
        )
        view = await build_session_view(
            config,
            store,
            session_id=session_id,
            consumers=consumers,
            event_journal=event_journal,
            tools=tools,
            modules=modules,
            skills_session=skills_session,
        )
        session = AgentSession(
            store,
            modules.projector(),
            models,
            launcher,
            providers[0],
            durable_inputs,
            RevertService(store, modules.snapshots.store()),
            view,
            skills=skills_session.service if skills_session else None,
            plans=plans,
            storage=modules.sessions.storage(owned_session_id=session_id),
            memory=memory,
            subagents=subagents,
        )
        if subagents is not None:
            subagents.notify = session.notify_subagent
    except BaseException as error:
        closers = []
        if memory is not None:
            closers.append(memory.aclose)
        if launcher is not None:
            closers.append(launcher.aclose)
        elif mcp_session is not None:
            closers.append(mcp_session.aclose)
        if view is not None:
            closers.append(view.aclose)
        elif event_journal is not None:
            closers.append(event_journal.aclose)
        closers.append(store.aclose)
        rollback = modules.sessions.cleanup()
        rollback.record(error)
        await rollback.close(tuple(closers))
        raise
    try:
        await session.initialize_memory()
        if startup_hooks:
            await launcher.startup(store)
    except BaseException:
        launcher.abort_startup()
        await session.aclose()
        raise
    return session


async def build_session_view(
    config,
    store,
    *,
    session_id,
    consumers=(),
    owns_history=False,
    event_journal=None,
    tools=(),
    modules,
    skills_session,
):
    skill_tools = ()
    if skills_session is not None and modules.skills is not None:
        # Services expose immutable declarations through their module, without loading bodies.
        skill_tools = modules.skills.session_tools(skills_session, read_only=config.read_only)
    snapshots = (
        *tools,
        *skill_tools,
        *modules.tools.default_tools(modules.tools.question_prompt()),
    )
    specs = (
        *modules.tools.registry(snapshots).list_specs(),
        modules.sandbox.bash_spec(),
        modules.plans.exit_spec(),
        *modules.plans.task_specs(),
    )

    def settings(projection):
        selection = projection.selection
        if (
            selection is None
            or selection.capabilities.context_window is None
            or selection.capabilities.max_output_tokens is None
        ):
            return None
        effective = [
            ToolSpec(**plain_data(item)) for item in projection.checkpoint.get("model_tools", ())
        ]
        if not effective:
            effective.extend(specs)
            effective.extend(MCPModule.view_specs(projection, selection))
        effective.sort(key=lambda spec: spec.name)
        return SummarySettings(
            selection,
            tuple(effective) if selection.capabilities.supports_tools else (),
            InputBudget.create(
                selection.capabilities.context_window, selection.capabilities.max_output_tokens
            ),
            config.summary_keep_recent_tokens,
        )

    # Resolve factories that may fail before opening the request database.
    projector = modules.projector()
    events = event_journal or MemoryEventJournal()
    records = modules.models.records()
    try:
        return SessionView(
            store,
            projector,
            UsageService(store, records, ownership=SessionRunOwnership()),
            events,
            consumers,
            owns_history=owns_history,
            context_settings=settings,
        )
    except BaseException:
        try:
            await records.aclose()
        finally:
            if event_journal is None:
                await events.aclose()
        raise


async def open_session_view(config, session_id, *, modules):
    store = await modules.sessions.repository().open(session_id)
    try:
        skills = (
            modules.skills.open_session(
                skill_settings(config), store, catalog_write_guard=SessionCatalogWriteGuard()
            )
            if modules.skills is not None
            else None
        )
        return await build_session_view(
            config,
            store,
            session_id=session_id,
            owns_history=True,
            modules=modules,
            skills_session=skills,
        )
    except BaseException:
        await store.aclose()
        raise
