import pytest

from pycode.kernel import DomainError, StateMachine


def positive(value):
    return value > 0


MACHINE = StateMachine({("idle", "go"): ("busy", positive), ("busy", "stop"): ("idle", None)})


def test_transition_returns_target():
    assert MACHINE.apply("idle", "go", value=1) == "busy"
    assert MACHINE.apply("busy", "stop") == "idle"


@pytest.mark.parametrize(
    ("state", "trigger", "facts"),
    [("idle", "stop", {}), ("idle", "go", {"value": 0})],
)
def test_missing_entry_or_failed_guard_is_invalid_transition(state, trigger, facts):
    with pytest.raises(DomainError) as error:
        MACHINE.apply(state, trigger, **facts)
    assert error.value.code == "invalid_transition"


def test_idle_transition_is_rejected_at_construction():
    with pytest.raises(DomainError) as error:
        StateMachine({("idle", "noop"): ("idle", None)})
    assert error.value.code == "invalid_transition"
