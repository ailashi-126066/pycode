"""Incomplete model output cannot authorize tool execution."""

from pycode.kernel import StopReason


def tool_calls_may_execute(stop_reason: StopReason | None) -> bool:
    return stop_reason not in {"length", "error", "aborted"}
