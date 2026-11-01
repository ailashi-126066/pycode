"""The concrete planning service exposes gates and async result handling."""

from types import SimpleNamespace

import pytest

from pycode.kernel import SessionHeader, ToolCall, ToolError, ToolResult, ToolSpec
from pycode.plans import FilePlanStore, InterruptPlanPrompt, PlanService
from pycode.session import SqliteSessionStore
from pycode.tasks import plan_board
from pycode.tools.adapters.backend import PycodeBackend
from tests.support.requests import TestTiming


async def test_plan_file_is_the_only_write_target_and_result_updates_state(tmp_path):
    history = SqliteSessionStore(
        tmp_path / "s", SessionHeader("s", str(tmp_path), "t"), TestTiming()
    )
    files = FilePlanStore(tmp_path / "plans", backend_factory=PycodeBackend)
    plans = PlanService(history, files, InterruptPlanPrompt(), parse_board=plan_board)
    try:
        await plans.initialize()
        state = await plans.enter()
        run = SimpleNamespace(run_id="r")
        spec = ToolSpec("write", "write", {"type": "object"}, category="write")
        call = ToolCall("c", "write", {"path": state.virtual_path})
        assert await plans.authorize(run, spec, call) == "allow"
        assert plans.select_tools(
            run, (spec, ToolSpec("bash", "bash", {}, category="command"))
        ) == (spec,)
        result = files.backend.write("/" + state.plan_id + ".md", "A plan")
        assert result.error is None
        await plans.after_tool_call(run, call, ToolResult("c", "write", "ok"))
        assert plans.state.document_exists
        with pytest.raises(ToolError, match="current Plan"):
            files.backend.write("/other.md", "bad")
    finally:
        await history.aclose()
