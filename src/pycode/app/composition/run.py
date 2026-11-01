"""Prepare configured session capabilities before assembling one run."""

from pycode.app.composition.factories import build_outputs
from pycode.app.composition.inputs import RunOptions, SessionResources
from pycode.app.composition.runtime import build_runtime
from pycode.kernel import ToolSpec, plain_data


async def build_configured_run(
    launcher,
    provider,
    store,
    run_id,
    *,
    modules,
    skills_session=None,
    mcp_session=None,
    checkpointer=None,
):
    snapshot = await store.snapshot()
    outputs = build_outputs(modules, snapshot.header.session_id)
    skills_run = None
    if skills_session is not None:
        skills_run = await skills_session.prepare_run(read_only=launcher.config.read_only)
    mcp_run = (
        mcp_session.prepare_run(store)
        if mcp_session is not None and provider.supports_tools
        else None
    )
    projected = modules.projector().transform_context(snapshot)
    inherited = tuple(
        ToolSpec(**plain_data(item)) for item in projected.checkpoint.get("model_tools", ())
    )
    session = SessionResources(
        session_id=snapshot.header.session_id,
        journal=store,
        tools=launcher.tools,
        memory=launcher.memory,
        team_member=launcher.team_member,
        consumers=launcher.consumers,
        history_store=store,
        summary_service=launcher.summary,
        event_journal=launcher.event_journal,
        configured_hooks=launcher.configured_hooks,
        plans=launcher.plans,
        plan_backend=launcher.plan_backend,
        durable_inputs=launcher.durable_inputs,
        outputs=outputs,
    )
    options = RunOptions(
        provider=provider,
        run_id=run_id,
        checkpointer=checkpointer,
        effective_tools=inherited,
        skills_run=skills_run,
        mcp_run=mcp_run,
    )
    return await build_runtime(launcher.config, modules=modules, session=session, options=options)
