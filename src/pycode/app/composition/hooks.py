"""Connect Hook engines to sandboxed actions and lifetime events."""

import os

from pycode.app.composition.factories import build_outputs
from pycode.kernel import EventStream


def build_hook_engine(hooks, history, actions, events, *, modules, read_only=False):
    # A read-only run keeps prompt Hooks only; commands could change the workspace.
    if read_only:
        hooks = tuple(hook for hook in hooks if hook.action.type == "prompt")
    return modules.hooks.engine(hooks, history, actions, events)


def build_lifecycle_hook_engine(launcher, store, snapshot, lifetime, *, modules):
    events = EventStream(
        snapshot.header.session_id, lifetime, launcher.consumers, journal=launcher.event_journal
    )
    return build_hook_engine(
        launcher.configured_hooks,
        store,
        modules.hooks.actions(
            build_outputs(modules, snapshot.header.session_id),
            modules.sandbox.shell(),
            modules.sandbox.backend(),
            launcher.config.sandbox,
            dict(os.environ),
            process_runner=modules.sandbox.process_runner,
            passthrough=launcher.config.subprocess_passthrough,
        ),
        events,
        modules=modules,
        read_only=launcher.config.read_only,
    )
