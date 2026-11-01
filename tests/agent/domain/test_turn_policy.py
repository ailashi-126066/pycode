import json
from pathlib import Path

import pytest

from pycode.agent.domain.turn_policy import next_turn
from pycode.tools.domain.turn_guard import tool_calls_may_execute

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures/pi-agent-loop.json").read_text())


@pytest.mark.parametrize(
    "case",
    [case for case in VECTORS["cases"] if "expected_execute" in case],
    ids=lambda case: case["name"],
)
def test_upstream_tool_execution_stop_reason_vectors(case):
    assert tool_calls_may_execute(case["stop_reason"]) == case["expected_execute"]


@pytest.mark.parametrize("reason", ["length", "error", "aborted"])
def test_failed_response_cannot_execute_its_tool_calls(reason):
    assert not tool_calls_may_execute(reason)


@pytest.mark.parametrize(
    "steer,follow_up,action,queue",
    [
        (False, False, "end", None),
        (True, False, "request", "steer"),
        (False, True, "request", "follow_up"),
        (True, True, "request", "steer"),
    ],
)
def test_finished_loop_selects_only_queued_inputs_with_steering_priority(
    steer, follow_up, action, queue
):
    decision = next_turn(steering_pending=steer, follow_up_pending=follow_up)
    assert (decision.action, decision.queue) == (action, queue)
