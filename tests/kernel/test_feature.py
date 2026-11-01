"""Feature contract: default no-ops, gate combination rule and list invariants."""

from pathlib import Path

import pytest

from pycode.kernel import (
    EventStream,
    Feature,
    Message,
    RunScope,
    SessionHeader,
    SessionScope,
    SessionSnapshot,
    ToolCall,
    ToolError,
    ToolResult,
    ToolSpec,
)


class Bare(Feature):
    name = "bare"


def scopes(tmp_path: Path) -> tuple[SessionScope, RunScope]:
    events = EventStream("s", "r", ())
    session = SessionScope("s", tmp_path, tmp_path / "data", None, events, False, "lead")
    return session, RunScope(session, "r", events)


SPEC = ToolSpec("read", "Read a file", {"type": "object"}, category="read")
CALL = ToolCall("c1", "read", {"path": "a"})


async def test_every_default_is_a_no_op(tmp_path):
    feature = Bare()
    session, run = scopes(tmp_path)
    messages = (Message("m1", "user", "hi"),)
    specs = (SPEC,)
    snapshot = SessionSnapshot(SessionHeader("s", str(tmp_path), "t"), (), None, 0, 0)

    assert await feature.open(session) is None
    assert await feature.prepare_run(run) is None
    assert feature.tools(run) == ()
    assert feature.mounts(run) == {}
    assert feature.gates(run) == ()
    assert feature.middleware(run) == ()
    assert feature.prompt_sections(session) == ()
    assert feature.contribute(snapshot, (), {}) is None
    assert feature.transform_context(run, messages) is messages
    assert feature.select_tools(run, specs) is specs
    assert await feature.before_tool_call(run, SPEC, CALL) is None
    assert await feature.after_tool_call(run, CALL, ToolResult("c1", "read", "ok")) is None
    assert await feature.on_event(await run.events.emit("agent_start", {})) is None
    assert await feature.aclose() is None
    assert Bare.checkpoint_keys == frozenset()


# The rule ToolExecutor applies (design 2.3): every gate runs in list order; any ToolError
# denies; otherwise one "allow" skips approval; all None falls through to the core approval gate.
async def combine(gates, run) -> str:
    allowed = False
    for gate in gates:
        allowed = await gate.authorize(run, SPEC, CALL) == "allow" or allowed
    return "allow" if allowed else "approval"


class Gate:
    def __init__(self, answer, calls):
        self.answer, self.calls = answer, calls

    async def authorize(self, run, spec, call):
        self.calls.append(self.answer)
        if self.answer == "deny":
            raise ToolError("permission_denied", "denied")
        return self.answer


@pytest.mark.parametrize(
    ("answers", "outcome"),
    [
        ((None, None), "approval"),
        ((None, "allow"), "allow"),
        (("allow", "deny"), "denied"),
        (("deny", "allow"), "denied"),
        ((), "approval"),
    ],
)
async def test_gate_combination(tmp_path, answers, outcome):
    calls = []
    _, run = scopes(tmp_path)
    try:
        result = await combine(tuple(Gate(answer, calls) for answer in answers), run)
    except ToolError as error:
        assert error.code == "permission_denied"
        result = "denied"
    assert result == outcome
    assert calls == list(answers[: answers.index("deny") + 1] if "deny" in answers else answers)


def check_invariants(features) -> None:
    names = [feature.name for feature in features]
    assert len(names) == len(set(names)), names
    owned: dict[str, str] = {}
    for feature in features:
        for key in feature.checkpoint_keys:
            assert key not in owned, (key, owned.get(key), feature.name)
            owned[key] = feature.name


class Tasks(Feature):
    name = "tasks"
    checkpoint_keys = frozenset({"task_board"})


class Plans(Feature):
    name = "plans"
    checkpoint_keys = frozenset({"plan"})


class Stealer(Feature):
    name = "stealer"
    checkpoint_keys = frozenset({"plan"})


def test_names_are_unique_and_checkpoint_keys_disjoint():
    check_invariants((Tasks(), Plans(), Bare()))
    with pytest.raises(AssertionError):
        check_invariants((Tasks(), Tasks()))
    with pytest.raises(AssertionError):
        check_invariants((Plans(), Stealer()))
