import pytest

from pycode.kernel.domain.messages import ToolResult
from pycode.kernel.domain.truncation import MAX_BYTES, MAX_LINES, line_count
from pycode.models.domain.tool_text import model_tool_text


@pytest.mark.parametrize(
    "text", ["x" * MAX_BYTES, "line\n" * MAX_LINES], ids=["byte-limit", "line-limit"]
)
def test_status_text_and_readback_hint_fit_the_model_preview_budget(text):
    result = ToolResult(
        "c1",
        "Bash",
        text,
        is_error=True,
        exit_code=7,
        details={"full_output_path": "/tmp/pycode/s/c1.log"},
        error_code="command_timeout",
    )
    visible = model_tool_text(result)
    assert len(visible.encode()) <= MAX_BYTES and line_count(visible) <= MAX_LINES
    assert "exit code 7" in visible and "command_timeout" in visible
    assert "[Full output: /tmp/pycode/s/c1.log]" in visible
    assert result.text == text
