from dataclasses import replace

import pytest

from pycode.kernel import Board, DomainError, Step, work_graph


def board(*steps, done_when=("tests pass",)):
    return Board("b1", "ship it", done_when, steps)


def error_code(call):
    with pytest.raises(DomainError) as error:
        call()
    return error.value.code


LINE = board(Step("a", "A", ()), Step("b", "B", ("a",)), Step("c", "C", ("b",)))


def test_cycle_dangling_duplicate_and_decisions_are_invalid():
    assert (
        error_code(
            lambda: work_graph.validate(board(Step("a", "A", ("b",)), Step("b", "B", ("a",))))
        )
        == "invalid_board"
    )
    assert error_code(lambda: work_graph.validate(board(Step("a", "A", ("x",))))) == "invalid_board"
    assert (
        error_code(lambda: work_graph.validate(board(Step("a", "A", ()), Step("a", "A", ()))))
        == "invalid_board"
    )
    too_many = replace(LINE, decisions=tuple(str(i) for i in range(11)))
    assert error_code(lambda: work_graph.validate(too_many)) == "invalid_board"


def test_step_cannot_start_before_dependencies_complete():
    assert error_code(lambda: work_graph.apply(LINE, "b", "start")) == "invalid_transition"
    started = work_graph.apply(LINE, "a", "start")
    assert started.steps[0].status == "in_progress"
    assert started.revision == 1


def test_only_one_step_in_progress():
    parallel = board(Step("a", "A", ()), Step("b", "B", ()))
    started = work_graph.apply(parallel, "a", "start")
    assert error_code(lambda: work_graph.apply(started, "b", "start")) == "invalid_board"


def test_completion_needs_evidence_and_cannot_move_back():
    checked = board(Step("a", "A", (), acceptance=("unit tests",)))
    started = work_graph.apply(checked, "a", "start")
    assert error_code(lambda: work_graph.apply(started, "a", "complete")) == "invalid_transition"
    done = work_graph.apply(started, "a", "complete", evidence=("msg-1",))
    assert done.steps[0].evidence == ("msg-1",)
    assert error_code(lambda: work_graph.apply(done, "a", "retry")) == "invalid_transition"
    assert error_code(lambda: work_graph.apply(done, "a", "start")) == "invalid_transition"


def test_failed_step_needs_note_and_can_retry():
    started = work_graph.apply(LINE, "a", "start")
    assert error_code(lambda: work_graph.apply(started, "a", "fail")) == "invalid_transition"
    failed = work_graph.apply(started, "a", "fail", note="flaky build")
    assert work_graph.apply(failed, "a", "retry").steps[0].status == "pending"


def test_blocked_or_failed_upstream_propagates_downstream():
    assert work_graph.derived(LINE) == {"a": "ready", "b": "waiting", "c": "waiting"}
    blocked = work_graph.apply(LINE, "a", "block", note="need API key")
    assert work_graph.derived(blocked) == {"b": "dep_blocked", "c": "dep_blocked"}


def test_board_exits():
    one = board(Step("a", "A", ()))
    assert (
        error_code(lambda: work_graph.apply(one, "board", "complete", evidence=("ok",)))
        == "invalid_transition"
    )
    finished = work_graph.apply(work_graph.apply(one, "a", "start"), "a", "complete")
    assert (
        error_code(lambda: work_graph.apply(finished, "board", "complete")) == "invalid_transition"
    )
    completed = work_graph.apply(finished, "board", "complete", evidence=("pytest passed",))
    assert completed.status == "completed"
    assert error_code(lambda: work_graph.apply(one, "board", "fail")) == "invalid_transition"
    assert work_graph.apply(one, "board", "fail", note="tried A and B").outcome == "tried A and B"
    assert error_code(lambda: work_graph.apply(one, "board", "block")) == "invalid_transition"
    blocked = work_graph.apply(one, "board", "block", note="which database?")
    assert work_graph.apply(blocked, "board", "resume").status == "active"
    assert work_graph.apply(one, "board", "abandon").status == "abandoned"


def test_layers_and_critical_path():
    graph = board(
        Step("a", "A", ()),
        Step("b", "B", ()),
        Step("c", "C", ("a",)),
        Step("d", "D", ("c", "b")),
    )
    assert work_graph.layers(graph) == (("a", "b"), ("c",), ("d",))
    assert work_graph.critical_path(graph) == ("a", "c", "d")


def test_views():
    decided = replace(LINE, decisions=("use sqlite",))
    assert "- a [ready] A" in work_graph.view(decided)
    assert "use sqlite" not in work_graph.view(decided)
    full = work_graph.full_view(decided)
    assert "use sqlite" in full
    assert "Critical path: a -> b -> c" in full
