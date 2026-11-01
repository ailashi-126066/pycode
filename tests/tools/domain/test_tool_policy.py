from pycode.kernel.domain.tools import ToolSpec
from pycode.tools.domain.execution import batch_execution_mode


def test_one_sequential_tool_forces_batch_barrier():
    read = ToolSpec(name="Read", description="read", input_schema={}, execution_mode="parallel")
    command = ToolSpec(name="Bash", description="command", input_schema={})
    assert batch_execution_mode((read, command)) == "sequential"
    assert batch_execution_mode((read, read)) == "parallel"
