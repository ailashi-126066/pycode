# Pi 没有，pycode 新增
"""Adapt declared tools without constructing a second execution loop."""

from langchain_core.tools import StructuredTool

from ..kernel import ToolContext, plain_data


def make_framework_tool(spec, context, tool_context=None, domain_tool=None):
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
        args_schema=plain_data(spec.input_schema),
        coroutine=execute,
        response_format="content_and_artifact",
    )
