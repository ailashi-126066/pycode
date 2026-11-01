"""Connect Hook engines to scoped tool execution and lifetime events."""

import os

from pycode.app.composition.factories import build_outputs
from pycode.kernel import EventStream


def build_hook_engine(
    hooks,
    history,
    actions,
    outputs,
    events,
    *,
    modules,
    snapshots=None,
    plans=None,
    read_only=False,
):
    def executor_factory(tools, *, plans=None, read_only=False):
        return modules.tools.configured_executor(
            tools, events, outputs, snapshots=snapshots, plans=plans, read_only=read_only
        )

    return modules.hooks.engine(
        hooks,
        history,
        actions,
        events,
        executor_factory=executor_factory,
        plans=plans,
        read_only=read_only,
    )


def build_lifecycle_hook_engine(launcher, store, snapshot, lifetime, *, modules):
    events = EventStream(
        snapshot.header.session_id, lifetime, launcher.consumers, journal=launcher.event_journal
    )
    outputs = build_outputs(modules, snapshot.header.session_id)
    return build_hook_engine(
        launcher.configured_hooks,
        store,
        modules.hooks.actions(
            outputs,
            modules.sandbox.shell(),
            modules.sandbox.backend(),
            launcher.config.sandbox,
            dict(os.environ),
            process_runner=modules.sandbox.process_runner,
            passthrough=launcher.config.subprocess_passthrough,
        ),
        outputs,
        events,
        modules=modules,
        snapshots=modules.snapshots.store(),
        plans=launcher.plans,
        read_only=launcher.config.read_only,
    )
