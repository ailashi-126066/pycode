"""The agent runtime with default bindings, for tests that drive the graph directly."""

from pycode.agent.module import AgentModule
from pycode.app.composition.bindings import build_runtime_bindings
from pycode.models.module import ModelsModule
from pycode.tools.module import ToolsModule


def build_agent_runtime(model, **options):
    return AgentModule().runtime(
        model,
        bindings=build_runtime_bindings(
            agent=AgentModule(), models=ModelsModule, tools=ToolsModule()
        ),
        **options,
    )
