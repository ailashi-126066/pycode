"""Bind native feature products at the framework runtime boundary."""

from functools import partial

from pycode.agent import RunContext
from pycode.agent.module import AgentModule
from pycode.hooks import run_hook
from pycode.models.module import ModelsModule
from pycode.tools.module import ToolsModule


def build_runtime_bindings(
    *,
    agent,
    models,
    tools,
    framework_extensions=(),
    model_filters=(),
    before_model=(),
    after_model=(),
    tool_extensions=(),
    native_bindings=(),
):
    converter = partial(tools.framework_tool, native_bindings=native_bindings)
    discovery = tools.discovery(converter)

    def settlement_factory(bound: RunContext):
        async def lifecycle(context, event, *, message="", error=""):
            await run_hook(bound, event, message=message, error=error)

        return tools.settlement(
            publisher=agent.publisher(bound),
            lifecycle=lifecycle,
            converter=models.to_framework,
            tool_extensions=tool_extensions,
            native_bindings=native_bindings,
        )

    return agent.bindings(
        to_framework=models.to_framework,
        to_framework_messages=models.to_framework_messages,
        from_assistant=models.from_assistant,
        make_framework_tool=converter,
        disclose_model_tools=discovery.disclose,
        calibrate_model_request=models.calibrate_model_request,
        model_request_step=models.model_request_step,
        settlement_middleware_factory=settlement_factory,
        run_hook=run_hook,
        framework_extensions=framework_extensions,
        model_filters=model_filters,
        before_model=before_model,
        after_model=after_model,
    )


def build_agent_runtime(model, **options):
    return AgentModule().runtime(
        model,
        bindings=build_runtime_bindings(
            agent=AgentModule(), models=ModelsModule, tools=ToolsModule()
        ),
        **options,
    )
