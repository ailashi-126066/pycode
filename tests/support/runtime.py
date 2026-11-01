"""The real agent runtime with directly assembled model and tool callbacks."""

from pycode.agent import AgentRuntime
from pycode.app.composition import build_runtime_bindings


async def no_hook(*args, **kwargs):
    pass


def build_agent_runtime(model, **options):
    return AgentRuntime(model, bindings=build_runtime_bindings(run_hook=no_hook), **options)
