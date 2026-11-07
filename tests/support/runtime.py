"""The real agent runtime with directly assembled model and tool callbacks."""

from pycode.agent import AgentRuntime


async def no_hook(*args, **kwargs):
    pass


def build_agent_runtime(model, **options):
    return AgentRuntime(model, run_hook=no_hook, **options)
