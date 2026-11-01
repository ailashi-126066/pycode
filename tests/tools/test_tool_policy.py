from pycode.kernel import ToolSpec
from pycode.tools.settlement import batch_execution_mode


def test_tools_default_to_parallel_and_one_sequential_tool_forces_batch_barrier():
    read = ToolSpec(name="read", description="read", input_schema={})
    command = ToolSpec(name="mcp", description="mcp", input_schema={}, execution_mode="sequential")
    assert batch_execution_mode((read, command)) == "sequential"
    assert batch_execution_mode((read, read)) == "parallel"
