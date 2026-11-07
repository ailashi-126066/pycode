from pathlib import Path
from types import SimpleNamespace

from pycode.kernel import (
    Event,
    EventStream,
    Message,
    RunScope,
    SessionScope,
    ToolCall,
    ToolResult,
    ToolSpec,
    plain_data,
)
from pycode.loopguard import LoopGuard
from pycode.loopguard.loopguard import (
    ProgressGuard,
    enter_tree,
    observe_call,
    reset_repeats,
)

THRESHOLD = 3


def guard_at(tree_id):
    guard = ProgressGuard()
    enter_tree(guard, tree_id)
    return guard


def call(guard, name, digest, text="ok", *, read_only=False, file_snapshot=None, exit_code=None):
    details = {} if file_snapshot is None else {"file_snapshot": file_snapshot}
    result = ToolResult("c", name, text, exit_code=exit_code, details=details)
    observe_call(guard, THRESHOLD, name, digest, plain_data(result), read_only=read_only)


def test_the_same_call_with_the_same_result_three_times_is_detected():
    guard = guard_at("a")
    call(guard, "bash", "h1", "pending (attempt 1)")
    call(guard, "grep", "h2", read_only=True)
    call(guard, "bash", "h1", "pending (attempt 2)")
    assert guard.evidence is None
    # Digits are ignored, so a counter or a timestamp in the output is the same result.
    call(guard, "bash", "h1", "pending (attempt 3)")
    assert "`bash` ran 3 times with the same arguments and result" in guard.evidence


def test_a_changed_result_is_not_a_repeat():
    guard = guard_at("a")
    call(guard, "bash", "h1", "waiting")
    call(guard, "bash", "h1", "waiting")
    call(guard, "bash", "h1", "ready")
    call(guard, "bash", "h1", "waiting", exit_code=1)
    assert guard.evidence is None


def test_a_read_only_call_counts_only_when_repeated_back_to_back():
    guard = guard_at("a")
    for _ in range(2):
        call(guard, "read", "h1", read_only=True)
        call(guard, "read", "h1", read_only=True)
        call(guard, "ls", "h2", read_only=True)
    assert guard.evidence is None
    for _ in range(3):
        call(guard, "read", "h1", read_only=True)
    assert "`read` returned the same result 3 times in a row" in guard.evidence


def test_a_new_step_compaction_or_continue_clears_the_repeat_counts():
    guard = guard_at("a")
    call(guard, "bash", "h1")
    call(guard, "bash", "h1")
    reset_repeats(guard)
    call(guard, "bash", "h1")
    call(guard, "bash", "h1")
    assert guard.evidence is None


def test_changing_a_file_back_to_old_content_is_an_oscillation():
    guard = guard_at("a")
    call(guard, "edit", "h1", file_snapshot={"before": "a", "after": "b"})
    call(guard, "edit", "h2", file_snapshot={"before": "b", "after": "a"})
    call(guard, "edit", "h1", file_snapshot={"before": "a", "after": "b"})
    assert guard.evidence is None
    call(guard, "edit", "h2", file_snapshot={"before": "b", "after": "a"})
    assert "back to tree a" in guard.evidence


def test_a_reset_also_clears_the_tree_counts():
    guard = guard_at("a")
    for _ in range(2):
        call(guard, "edit", "h1", file_snapshot={"before": "a", "after": "b"})
        call(guard, "edit", "h2", file_snapshot={"before": "b", "after": "a"})
    reset_repeats(guard)
    call(guard, "edit", "h1", file_snapshot={"before": "a", "after": "b"})
    call(guard, "edit", "h2", file_snapshot={"before": "b", "after": "a"})
    assert guard.evidence is None and guard.tree_id == "a"


def run():
    events = EventStream("s", "r", ())
    session = SessionScope("s", Path("/w"), Path("/d"), None, events, False, "lead")
    return RunScope(session, "r", events)


async def settle(feature, call_id, name, arguments, text="ok", *, category="command"):
    start = Event(0, "s", "r", "tool_execution_start", {"tool_call_id": call_id, "args": arguments})
    await feature.on_event(start)
    spec = ToolSpec(name, name, {"type": "object"}, category=category)
    await feature.before_tool_call(run(), spec, ToolCall(call_id, name, arguments))
    result = plain_data(ToolResult(call_id, name, text))
    data = {"tool_call_id": call_id, "tool_name": name, "result": result, "is_error": False}
    await feature.on_event(Event(0, "s", "r", "tool_execution_end", data))


def runtime(turn=2):
    return SimpleNamespace(context=SimpleNamespace())


async def test_guard_reminds_once_then_its_middleware_ends_the_run():
    feature = LoopGuard(threshold=2, asks_user=False)
    (middleware,) = feature.middleware(run())
    await feature.on_event(Event(0, "s", "r", "agent_start", {}))
    for index in range(2):
        await settle(feature, f"c{index}", "bash", {"command": "make"})
    messages = (Message("u", "user", "go"),)
    reminded = feature.transform_context(run(), messages)
    assert reminded[:1] == messages and "Progress guard" in reminded[1].content
    # Budget estimation may inspect the same reminder before a model request.
    assert "Progress guard" in feature.transform_context(run(), messages)[-1].text
    await feature.on_event(Event(0, "s", "r", "message_start", {"message": {"role": "assistant"}}))
    assert feature.transform_context(run(), messages) == messages
    assert await middleware.abefore_model({}, runtime()) is None
    await settle(feature, "c2", "bash", {"command": "make"})
    current = runtime()
    assert await middleware.abefore_model({"turn": 2}, current) == {
        "jump_to": "end",
        "reason": "loop_detected",
    }


async def test_continue_answer_clears_the_counts(monkeypatch):
    feature = LoopGuard(threshold=2, asks_user=True)
    (middleware,) = feature.middleware(run())
    questions = []

    def answer(question):
        questions.append(question)
        return {"operation_id": question["operation_id"], "answers": {"loop": "continue"}}

    monkeypatch.setattr("pycode.loopguard.loopguard.interrupt", answer)
    for index in range(3):
        await settle(feature, f"c{index}", "bash", {"command": "make"})
        feature.transform_context(run(), ())
        await feature.on_event(
            Event(0, "s", "r", "message_start", {"message": {"role": "assistant"}})
        )
    assert await middleware.abefore_model({"turn": 4}, runtime()) is None
    assert questions[0]["operation_id"] == "r:loop-4" and feature.guard.evidence is None


async def test_reads_count_by_category_and_compaction_or_new_step_resets():
    feature = LoopGuard(threshold=2, asks_user=False)
    await settle(feature, "r1", "read", {"path": "a"}, category="read")
    await feature.on_event(Event(0, "s", "r", "compaction", {"job_id": "j", "status": "applied"}))
    await settle(feature, "r2", "read", {"path": "a"}, category="read")
    assert feature.guard.evidence is None
    await settle(feature, "r3", "read", {"path": "a"}, category="read")
    assert "returned the same result 2 times" in feature.guard.evidence
    feature.step_started()
    assert feature.guard.evidence is None
