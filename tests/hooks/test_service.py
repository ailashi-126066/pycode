"""Explicit service wiring keeps hook rejection, lifecycle and command boundaries intact."""

import json
from types import SimpleNamespace

import pytest

from pycode.hooks import HooksService
from pycode.kernel import EventStream, RunScope, SessionScope, ToolCall, ToolResult, ToolSpec


class History:
    def __init__(self):
        self.entries = []

    async def snapshot(self):
        return SimpleNamespace(entries=self.entries)

    async def append_entry(self, entry):
        self.entries.append(entry)


async def opened(tmp_path, hooks, *, trusted=False, worker=False, read_only=False, runner=None):
    (tmp_path / "hooks.json").write_text(json.dumps(hooks))
    history = History()
    events = EventStream("s", "r", ())
    session = SessionScope("s", tmp_path / "project", tmp_path, history, events, read_only, "lead")

    async def command(argv, cwd, env, seconds):
        return ToolResult("command", "hook", "ok")

    service = HooksService(
        trusted=trusted,
        worker=worker,
        run_command=runner or command,
        shell="/bin/bash",
        environment={},
    )
    await service.open(session)
    return service, RunScope(session, "r", events), history


def prompt(identifier, event, **extra):
    return {
        "id": identifier,
        "event": event,
        "action": {"type": "prompt", "message": identifier},
        **extra,
    }


async def test_startup_shutdown_and_explicit_run_boundary(tmp_path):
    hooks = [prompt(name, name) for name in ("startup", "run_start", "pre_send", "shutdown")]
    service, run, history = await opened(tmp_path, hooks)
    await service.run_hook(run, "run_start")
    await service.run_hook(run, "pre_send")
    await service.aclose()
    assert [entry.data["hook_id"] for entry in history.entries] == [
        "startup",
        "run_start",
        "pre_send",
        "shutdown",
    ]


@pytest.mark.parametrize("failure", ["result", "exception"])
async def test_failed_pretool_command_blocks_and_persists_context(tmp_path, failure):
    async def command(*args):
        if failure == "exception":
            raise OSError("unavailable")
        return ToolResult("c", "hook", "check failed", is_error=True)

    hooks = [
        {"id": "check", "event": "pre_tool_use", "action": {"type": "command", "command": "check"}}
    ]
    service, run, history = await opened(tmp_path, hooks, runner=command)
    blocked = await service.before_tool_call(
        run, ToolSpec("write", "write", {}), ToolCall("c", "write", {})
    )
    assert blocked["block"] is True
    assert history.entries[0].data["hook_id"] == "check"


async def test_once_is_not_run_again_and_after_callback_preserves_file_change(tmp_path):
    service, run, history = await opened(
        tmp_path,
        [
            prompt("once", "pre_tool_use", once=True),
            prompt("post", "post_tool_use"),
            prompt("files", "file_change"),
        ],
    )
    call = ToolCall("c", "write", {"path": "a"})
    for _ in range(2):
        assert await service.before_tool_call(run, ToolSpec("write", "write", {}), call) is None
    result = ToolResult(
        "c",
        "write",
        "done",
        details={"file_snapshot": {"before": "a", "after": "b", "paths": ["a"]}},
    )
    assert await service.after_tool_call(run, call, result) is None
    assert [entry.data["hook_id"] for entry in history.entries] == ["once", "post", "files"]


@pytest.mark.parametrize("mode", ["worker", "read_only"])
async def test_worker_and_read_only_do_not_execute_command_hooks(tmp_path, mode):
    async def forbidden(*args):
        pytest.fail("command hooks must be filtered before execution")

    hooks = [
        prompt("prompt", "startup"),
        {"id": "command", "event": "startup", "action": {"type": "command", "command": "echo no"}},
    ]
    _, _, history = await opened(tmp_path, hooks, runner=forbidden, **{mode: True})
    assert [entry.data["hook_id"] for entry in history.entries] == ["prompt"]
