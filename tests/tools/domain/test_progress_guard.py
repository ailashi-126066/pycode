from pycode.tools.domain.progress_guard import ProgressGuard, enter_tree, observe_call


def guard_at(tree_id):
    guard = ProgressGuard()
    enter_tree(guard, tree_id)
    return guard


def test_same_action_on_unchanged_workspace_is_detected_on_the_repeat():
    guard = guard_at("a")
    observe_call(guard, 2, "read", "h1", failed=False, file_snapshot=None)
    assert guard.evidence is None
    observe_call(guard, 2, "read", "h1", failed=False, file_snapshot=None)
    assert "`read` ran 2 times" in guard.evidence and "tree a" in guard.evidence


def test_same_action_after_a_file_change_is_not_a_repeat():
    guard = guard_at("a")
    observe_call(guard, 2, "bash", "h1", failed=False, file_snapshot=None)
    observe_call(guard, 2, "edit", "h2", failed=False, file_snapshot={"before": "a", "after": "b"})
    observe_call(guard, 2, "bash", "h1", failed=False, file_snapshot=None)
    assert guard.evidence is None


def test_changing_a_file_back_to_old_content_is_an_oscillation():
    guard = guard_at("a")
    observe_call(guard, 2, "edit", "h1", failed=False, file_snapshot={"before": "a", "after": "b"})
    assert guard.evidence is None
    observe_call(guard, 2, "edit", "h2", failed=False, file_snapshot={"before": "b", "after": "a"})
    assert "back to tree a" in guard.evidence
