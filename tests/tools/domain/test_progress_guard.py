from pycode.kernel import ToolResult
from pycode.tools.domain.progress_guard import (
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
    observe_call(guard, THRESHOLD, name, digest, result, read_only=read_only)


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
