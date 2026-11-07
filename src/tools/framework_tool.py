# Pi 没有，pycode 新增
"""Declare framework tools; normal agent execution is intercepted by settlement."""

from langchain_core.tools import StructuredTool

from ..kernel import ToolContext, plain_data


def make_framework_tool(spec, context):
    async def execute(**arguments):
        actual_context = ToolContext(
            context.session_id,
            context.run_id,
            "",
            context.workspace,
            tool_name=spec.name,
            agent_id=context.agent_id,
        )
        tool = context.registry.get(spec.name)
        result = await tool.execute(actual_context, arguments)
        return result.text, result

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        args_schema=plain_data(spec.input_schema),
        coroutine=execute,
        response_format="content_and_artifact",
    )
