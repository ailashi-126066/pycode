import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from pycode.hooks.actions import ConfiguredHookActions
from pycode.hooks.api import HookActionTool
from pycode.hooks.config import parse_hooks
from pycode.hooks.types import HookAction, HookContext
from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.tools import ToolContext
from pycode.sandbox.adapters.seatbelt import UnavailableSandbox
from pycode.sandbox.adapters.tools.bash import find_bash
from pycode.sandbox.adapters.tools.process_runner import run_process
from pycode.sandbox.domain.sandbox import SandboxSettings
from pycode.tools.adapters.output_store import TempOutputStore
from pycode.tools.domain.operations import operation_spec_hash

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


async def test_source_condition_vectors_use_owned_worker(tmp_path):
    runner = actions(tmp_path)
    for expression, fields, expected in VECTORS["conditions"]:
        hook = parse_hooks([{**VECTORS["config"], "if": expression}])[0]
        context = HookContext(
            "s1",
            "r1",
            str(tmp_path),
            "b",
            "pre_tool_use",
            fields.get("tool", ""),
            {key[5:]: value for key, value in fields.items() if key.startswith("args.")},
        )
        assert await runner.matches(hook, context) == expected


async def test_regex_condition_timeout_does_not_block_owner(tmp_path):
    runner = actions(tmp_path)
    hook = parse_hooks(
        [
            {
                **VECTORS["config"],
                "if": "args.command =~ /(a+)+$/",
                "action": {"type": "prompt", "message": "blocked", "timeout": 0.2},
            }
        ]
    )[0]
    context = HookContext(
        "s1", "r1", str(tmp_path), "b", "pre_tool_use", "Bash", {"command": "a" * 500 + "!"}
    )
    async with asyncio.timeout(5):
        with pytest.raises(ToolError) as error:
            await runner.matches(hook, context)
    assert error.value.code == "hook_condition_timeout"


async def test_command_variables_are_data_and_cannot_inject_shell_or_environment_expansion(
    tmp_path,
):
    runner = actions(tmp_path, {"SECRET": "must not leak"})
    value = "$(touch injected); $ENV.SECRET $EVENT"
    context = HookContext("s1", "r1", str(tmp_path), "b", "post_receive", message=value)
    tool = ToolContext("s1", "r1", "op", str(tmp_path), "call", "hook__test")
    result = await runner.execute(
        asdict(HookAction("command", command='printf "%s" "$MESSAGE"')), context, tool
    )
    assert not result.is_error and result.text == value
    assert not (tmp_path / "injected").exists()


async def test_command_receives_only_explicit_environment_references(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "inherited-secret")
    runner = actions(tmp_path, {"HOOK_API_KEY": "referenced-secret"})
    context = HookContext("s1", "r1", str(tmp_path), "b", "post_receive")
    tool = ToolContext("s1", "r1", "op", str(tmp_path), "call", "hook__test")
    command = 'printf "%s|%s|%s" "${FAKE_API_KEY:-}" "$ENV.HOOK_API_KEY" "${PATH:+path}"'
    result = await runner.execute(asdict(HookAction("command", command=command)), context, tool)
    assert not result.is_error and result.text == "|referenced-secret|path"


def test_hook_action_identity_binds_environment_grant_and_sandbox(tmp_path):
    hook = parse_hooks([VECTORS["config"]])[0]
    base = actions(tmp_path)
    expanded = ConfiguredHookActions(
        base._outputs,
        base._shell,
        base._sandbox,
        base._settings,
        {},
        passthrough=("HTTPS_PROXY",),
        process_runner=run_process,
    )
    enclosed = ConfiguredHookActions(
        base._outputs,
        base._shell,
        base._sandbox,
        SandboxSettings("on"),
        {},
        process_runner=run_process,
    )
    identities = {
        operation_spec_hash(HookActionTool(hook, runner).spec)
        for runner in (base, expanded, enclosed)
    }
    assert len(identities) == 3
