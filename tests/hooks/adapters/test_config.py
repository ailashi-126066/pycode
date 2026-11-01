import json
from pathlib import Path

import pytest

from pycode.hooks.config import parse_hooks
from pycode.hooks.types import HOOK_EVENTS, HookContext, parse_condition
from pycode.kernel.domain.errors import DomainError

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/hook-baselines.json").read_text(encoding="utf-8")
)


def test_fixed_event_and_condition_vectors_parse_before_execution():
    assert HOOK_EVENTS == set(VECTORS["events"])
    for expression, _, _ in VECTORS["conditions"]:
        assert parse_condition(expression).conditions
    for expression in VECTORS["invalid_conditions"]:
        with pytest.raises(DomainError):
            parse_condition(expression)
    assert parse_condition("") is None
    hook = parse_hooks([VECTORS["config"]])[0]
    assert hook.reject and hook.condition.conditions[0].field == "tool"
    context = HookContext(
        "s",
        "r",
        "/workspace",
        "post_tool_use",
        "Write",
        {"file_path": "src/main.py"},
        "src/main.py",
        "done",
    )
    assert (
        context.expand(VECTORS["template"])
        == "Event=post_tool_use Tool=Write File=src/main.py Msg=done Err= Arg=src/main.py"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"async": True},
        {"event": "invalid"},
        {"reject": "yes"},
        {"action": []},
        {"action": {"type": "http", "url": "https://hooks.example.test"}},
    ],
)
def test_invalid_configuration_is_a_domain_error(change):
    with pytest.raises(DomainError):
        parse_hooks([{**VECTORS["config"], **change}])


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("session_start", "run_start"),
        ("session_end", "run_end"),
        ("command_execute", 'pre_tool_use with "if": "tool == \\"bash\\""'),
    ],
)
def test_renamed_event_is_an_error_that_names_the_replacement(old, new):
    with pytest.raises(DomainError, match="renamed") as error:
        parse_hooks([{**VECTORS["config"], "event": old, "reject": False}])
    assert new in str(error.value)
