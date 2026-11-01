"""Acceptance uses saved tool results, code versions and declared task criteria."""

from types import SimpleNamespace

import pytest

from pycode.kernel import Message, ToolError, ToolResult
from tests.team.services.test_handoff import eventually
from tests.team.services.test_team import close, spawn, team_at


async def test_claimed_pass_must_match_actual_result_and_lead_accepts(tmp_path):
    team = team_at(tmp_path)
    try:
        task = await team.board.create_task("check", acceptance=["tests"])
        a = await spawn(team, task_id=task["task_id"])
        child = team.sessions[a["agent_id"]]
        result = ToolResult("call", "Bash", "failed", is_error=True, exit_code=1)

        async def evidence():
            return SimpleNamespace(
                entries=[
                    SimpleNamespace(message=Message("result", "tool_result", tool_result=result))
                ]
            )

        child.task_evidence = evidence
        check = {"name": "tests", "status": "passed", "call_id": "call"}
        with pytest.raises(ToolError, match="saved execution result"):
            await team.board.update_task(
                task["task_id"],
                actor=a["agent_id"],
                generation=1,
                status="submitted",
                result="all passed",
                checks=[check],
            )
        result = ToolResult("call", "Bash", "passed", exit_code=0)
        await team.board.update_task(
            task["task_id"],
            actor=a["agent_id"],
            generation=1,
            status="submitted",
            result="tests passed",
            checks=[check],
        )
        child.release.set()
        await eventually(lambda: team.records[a["agent_id"]]["status"] == "completed")
        await team.board.update_task(task["task_id"], actor="lead", status="done")
        assert team.board.records[task["task_id"]]["done_by"] == a["agent_id"]
    finally:
        await close(team)
