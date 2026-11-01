"""Select parallel execution only when every declared tool permits it."""

from pycode.kernel import ExecutionMode, ToolSpec


def batch_execution_mode(tools: tuple[ToolSpec, ...]) -> ExecutionMode:
    if any(tool.execution_mode == "sequential" for tool in tools):
        return "sequential"
    return "parallel"
