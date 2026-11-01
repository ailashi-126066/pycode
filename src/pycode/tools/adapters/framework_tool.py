"""Adapt declared tools without constructing a second execution loop."""

from langchain_core.tools import StructuredTool

from pycode.kernel import ToolContext


def make_framework_tool(spec, context, tool_context=None, domain_tool=None, *, native_bindings=()):
    for binding in native_bindings:
        native = binding.framework_tool(spec.name)
        if native is not None:
            return native

    async def execute(**arguments):
        actual_context = tool_context or ToolContext(
            context.session_id,
            context.run_id,
            "",
            context.workspace,
            tool_name=spec.name,
            agent_id=context.agent_id,
        )
        tool = domain_tool or context.registry.get(spec.name)
        result = await tool.execute(actual_context, arguments)
        return result.text, result

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.input_schema,
        coroutine=execute,
        response_format="content_and_artifact",
    )
