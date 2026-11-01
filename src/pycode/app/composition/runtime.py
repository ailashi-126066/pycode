"""Connect feature-owned products for one run; do not construct their adapters here."""

from pycode.agent import RunContext
from pycode.app.composition.bindings import build_runtime_bindings
from pycode.app.composition.factories import build_outputs, build_summary_generator
from pycode.app.composition.hooks import build_hook_engine
from pycode.app.composition.inputs import RunOptions, SessionResources
from pycode.app.composition.modules import ApplicationModules
from pycode.app.configuration.credentials import credential_environment
from pycode.app.configuration.settings import AppConfig
from pycode.app.lifecycle.run import RunAssembly, RuntimeContainer
from pycode.context import EvictionBudget, InputBudget, SummarySettings
from pycode.hooks import ToolHookBridge
from pycode.kernel import EventStream, MemoryEventJournal
from pycode.session import (
    DurableInputs,
    PendingInputs,
    assign_initial_title,
)


async def build_runtime(
    config: AppConfig,
    *,
    modules: ApplicationModules,
    session: SessionResources,
    options: RunOptions,
) -> RuntimeContainer:
    provider = options.provider
    skills_run = options.skills_run
    summary_service = session.summary_service
    async with RunAssembly() as assembling:
        checkpointer = (
            options.checkpointer
            if options.checkpointer is not None
            else await session.history_store.checkpoint()
            if session.history_store is not None
            else modules.agent.checkpoint()
        )
        outputs = session.outputs or build_outputs(modules, session.session_id)
        memory_tools = modules.memory.tools(session.memory, read_only=config.read_only)
        subagent_tools = (
            modules.subagents.tools(session.subagents) if session.subagents is not None else ()
        )
        supplied = (*session.tools, *memory_tools, *subagent_tools)
        workspace_files = modules.tools.backend(
            config.workspace,
            read_only=config.read_only,
            excluded_roots=(config.user_data,),
            write_roots=config.sandbox.write_roots,
        )
        routes: dict[str, object] = {"/workspace/": workspace_files}
        if session.plan_backend is not None:
            routes["/plans/"] = session.plan_backend
        # Pi writes truncated outputs to a temp file the model reads back with read; the
        # directory is mounted at its own absolute path, so full_output_path works as is.
        routes[str(outputs.directory) + "/"] = modules.tools.read_only_mount(outputs.directory)
        if session.history_store is not None:
            routes["/conversation_history/"] = modules.sessions.history_backend(
                session.history_store
            )
        # Feature mounts are read-only for the builtin tools unless listed in path_utils.WRITABLE.
        if session.memory is not None:
            for prefix, root in modules.memory.mounts().items():
                routes[prefix] = modules.tools.read_only_mount(root)
        if skills_run:
            routes.update(skills_run.routes)
        # One backend serves the builtin file tools and the framework's SkillsMiddleware.
        backend = modules.tools.composite(routes)
        enabled = (
            (
                *supplied,
                *(skills_run.tools if skills_run else ()),
                *modules.tools.builtin_tools(backend),
                *modules.tools.default_tools(
                    modules.tools.question_prompt() if config.agent_id == "lead" else None
                ),
                modules.sandbox.bash(
                    outputs, skill_roots=skills_run.script_roots if skills_run else ()
                ),
                *((modules.plans.exit_tool(session.plans),) if session.plans is not None else ()),
                *(
                    modules.plans.task_tools(session.history_store)
                    if session.history_store is not None
                    else ()
                ),
            )
            if provider.supports_tools
            else ()
        )
        if config.read_only:
            enabled = tuple(tool for tool in enabled if tool.spec.category == "read")
        registry = modules.tools.registry(enabled)
        event_store = MemoryEventJournal() if session.event_journal is None else None
        assembling.events = event_store.aclose if event_store else None
        events = EventStream(
            session.session_id,
            options.run_id,
            session.consumers,
            journal=session.event_journal or event_store,
            agent_id=config.agent_id if options.agent_id == "lead" else options.agent_id,
        )
        snapshots = modules.snapshots.store()
        gate = modules.tools.gate(
            modules.sandbox.backend(),
            config.sandbox,
            interruptible=checkpointer is not None,
            read_only=config.read_only,
            custom=config.tool_gate,
            memory_specs=tuple(tool.spec for tool in memory_tools),
            plans=session.plans,
            scope=skills_run.permission_constraint if skills_run else None,
        )
        hooks = None
        if session.configured_hooks and session.history_store is not None:
            actions = modules.hooks.actions(
                outputs,
                modules.sandbox.shell(),
                modules.sandbox.backend(),
                config.sandbox,
                credential_environment(config, (provider,)),
                process_runner=modules.sandbox.process_runner,
                passthrough=config.subprocess_passthrough,
            )
            hooks = build_hook_engine(
                session.configured_hooks,
                session.history_store,
                actions,
                events,
                modules=modules,
                read_only=config.read_only,
            )
        executor = modules.tools.executor(
            registry,
            gate,
            events,
            outputs,
            snapshots=snapshots,
            hooks=ToolHookBridge(hooks) if hooks is not None else None,
        )
        budget = InputBudget.create(provider.context_window, provider.max_output_tokens)
        eviction = (
            modules.context.eviction(
                session.history_store,
                modules.projector(),
                options.eviction_budget or EvictionBudget(budget.input_limit),
                events,
            )
            if session.history_store is not None
            else None
        )
        owner = await modules.models.owner(
            provider,
            session.consumers,
            session.session_id,
            options.run_id,
            "subagent"
            if config.agent_id != "lead" and options.purpose == "agent"
            else options.purpose,
            retry_policy=options.retry_policy,
            agent_id=config.agent_id if options.agent_id == "lead" else options.agent_id,
            events=events,
        )
        assembling.model = owner.aclose
        context = RunContext(
            session_id=session.session_id,
            run_id=options.run_id,
            workspace=str(config.workspace),
            selection=owner.requests.selection,
            events=events,
            journal=session.journal,
            executor=executor,
            registry=registry,
            inputs=PendingInputs(),
            requests=owner.requests,
            history_store=session.history_store,
            projector=modules.projector() if session.history_store else None,
            outputs=outputs,
            eviction=eviction,
            durable_inputs=session.durable_inputs
            or (DurableInputs(session.history_store) if session.history_store else None),
            agent_id=config.agent_id if options.agent_id == "lead" else options.agent_id,
            hooks=hooks,
            max_turns=config.max_turns,
            loop_repeat_threshold=config.loop_repeat_threshold,
            loop_asks_user=config.loop_asks_user,
            plans=session.plans,
            subagent_view=session.subagents.active_view if session.subagents is not None else None,
            workspace_files=workspace_files,
            model_tools=options.effective_tools,
            read_only=config.read_only,
        )
        if session.history_store is not None:
            settings = SummarySettings(
                context.selection,
                options.effective_tools or registry.list_specs(),
                budget,
                config.summary_keep_recent_tokens,
            )
            generator = build_summary_generator(
                config, provider, session.consumers, modules=modules
            )
            if summary_service is None:
                summary_service = modules.context.summary(
                    session.history_store,
                    modules.projector(),
                    modules.sessions.layout.project / "summaries",
                    generator,
                    settings,
                    events,
                    on_compacted=session.memory.compacted if session.memory is not None else None,
                )
                assembling.summary = summary_service.aclose
            else:
                summary_service.configure(settings, generator, events)
            context.summary = summary_service
        native_bindings = (options.mcp_run,) if options.mcp_run else ()
        tool_extensions = (
            (skills_run.tool_extension,) if skills_run and skills_run.tool_extension else ()
        )
        extension = skills_run.extension if skills_run else None
        instructions, instruction_backend = modules.context.instructions(
            config.workspace, config.user_data, trusted=config.project_trusted
        )

        async def prepare_tools():
            if options.mcp_run is not None:
                await options.mcp_run.refresh(registry)

        preparations = (prepare_tools,) if options.mcp_run else ()
        bindings = build_runtime_bindings(
            agent=modules.agent,
            models=modules.models,
            tools=modules.tools,
            framework_extensions=(extension,) if extension else (),
            model_filters=(extension.filter_request,) if extension else (),
            before_model=preparations,
            tool_extensions=tool_extensions,
            native_bindings=native_bindings,
        )
        runtime = modules.agent.runtime(
            owner.model,
            bindings=bindings,
            title_assignment=BoundTitleAssignment(session.history_store)
            if session.history_store
            else None,
            checkpointer=checkpointer,
            backend=backend,
            instructions=instructions,
            instruction_backend=instruction_backend,
        )
        return RuntimeContainer(
            runtime,
            context,
            owner.records,
            owner,
            event_store=event_store,
            preparations=preparations,
        )


class BoundTitleAssignment:
    def __init__(self, history):
        self.history = history

    async def assign(self, message):
        await assign_initial_title(self.history, message)
