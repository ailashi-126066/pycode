import json
from pathlib import Path

import pytest

from pycode.hooks.actions import ConfiguredHookActions
from pycode.hooks.config import parse_hooks
from pycode.hooks.types import HookAction, HookContext
from pycode.kernel import DomainError, ToolContext
from pycode.sandbox.adapters.seatbelt import UnavailableSandbox
from pycode.sandbox.adapters.tools.bash import find_bash
from pycode.sandbox.adapters.tools.process_runner import run_process
from pycode.sandbox.domain.sandbox import SandboxSettings
from pycode.tools.adapters.output_store import TempOutputStore

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/hook-baselines.json").read_text(encoding="utf-8")
)


def actions(tmp_path, environment=None):
    return ConfiguredHookActions(
        TempOutputStore("s1"),
        find_bash(),
        UnavailableSandbox(),
        SandboxSettings(),
        environment or {},
        process_runner=run_process,
    )


def test_source_condition_vectors_evaluate_in_process():
    for expression, fields, expected in VECTORS["conditions"]:
        hook = parse_hooks([{**VECTORS["config"], "if": expression}])[0]
        assert hook.condition.matches(fields) == expected


def test_regex_conditions_compile_at_load_with_a_length_limit():
    for pattern in ("/" + "a" * 257 + "/", "/(unclosed/"):
        with pytest.raises(DomainError) as error:
            parse_hooks([{**VECTORS["config"], "if": "args.command =~ " + pattern}])
        assert error.value.code == "invalid_hook_condition"


async def test_command_variables_are_data_and_cannot_inject_shell_or_environment_expansion(
    tmp_path,
):
    runner = actions(tmp_path, {"SECRET": "must not leak"})
    value = "$(touch injected); $ENV.SECRET $EVENT"
    context = HookContext("s1", "r1", str(tmp_path), "post_receive", message=value)
    tool = ToolContext("s1", "r1", "op", str(tmp_path), "call", "hook__test")
    result = await runner.execute(
        HookAction("command", command='printf "%s" "$MESSAGE"'), context, tool
    )
    assert not result.is_error and result.text == value
    assert not (tmp_path / "injected").exists()


async def test_command_receives_only_explicit_environment_references(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "inherited-secret")
    runner = actions(tmp_path, {"HOOK_API_KEY": "referenced-secret"})
    context = HookContext("s1", "r1", str(tmp_path), "post_receive")
    tool = ToolContext("s1", "r1", "op", str(tmp_path), "call", "hook__test")
    command = 'printf "%s|%s|%s" "${FAKE_API_KEY:-}" "$ENV.HOOK_API_KEY" "${PATH:+path}"'
    result = await runner.execute(HookAction("command", command=command), context, tool)
    assert not result.is_error and result.text == "|referenced-secret|path"
