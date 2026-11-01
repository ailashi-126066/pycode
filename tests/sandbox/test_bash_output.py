import asyncio
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pycode.kernel import MAX_BYTES, EventStream, Message, SessionHeader, ToolCall, ToolContext
from pycode.sandbox import Bash, Sandbox, SandboxFeature, SandboxSettings, find_bash
from pycode.session import SqliteSessionStore
from pycode.tools.executor import ToolExecutor, ToolRegistry
from pycode.tools.output import TempOutputStore
from pycode.tools.schema import JsonSchemaValidator
from tests.support.requests import TestTiming
from tests.tools.test_tool_executor import run_scope


class Recorder:
    async def on_event(self, event):
        pass


class ApprovedGate:
    async def authorize(self, context, spec, call):
        return None


async def assemble(tmp_path, *, passthrough=()):
    sessions = tmp_path / "sessions"
    journal = SqliteSessionStore(
        sessions / "s1", SessionHeader("s1", str(tmp_path), "t"), TestTiming()
    )
    await journal.append_message(Message("u1", "user", "command task"))
    outputs = TempOutputStore("s1")
    validator = JsonSchemaValidator()
    registry = ToolRegistry(
        validator,
        (Bash(find_bash(), outputs, Sandbox(SandboxSettings()), passthrough=passthrough),),
    )
    executor = ToolExecutor(
        registry,
        validator,
        ApprovedGate(),
        run_scope(EventStream("s1", "r1", (Recorder(),))),
        (),
        outputs,
    )
    context = ToolContext("s1", "r1", "op1", str(tmp_path))
    return executor, context, journal, outputs


def python_command(code):
    return shlex.quote(sys.executable.replace("\\", "/")) + " -c " + shlex.quote(code)


async def test_real_command_output_is_complete_on_disk_and_bounded_in_model_result(tmp_path):
    executor, context, journal, outputs = await assemble(tmp_path)
    code = (
        "import sys; "
        "sys.stdout.buffer.write(''.join(f'line-{i:04d}\\n' for i in range(1,8001)).encode())"
    )
    result = await executor.execute(
        context, ToolCall("call-1", "bash", {"command": python_command(code)}), "tool_use"
    )
    assert not result.is_error
    assert result.exit_code == 0
    assert len(result.text.encode()) <= MAX_BYTES
    assert "line-8000" in result.text
    path = result.details["full_output_path"]
    assert result.text.endswith(f"[Showing lines 6001-8000 of 8000. Full output: {path}]")
    full = await asyncio.to_thread(Path(path).read_bytes)
    assert full.startswith(b"line-0001\n") and full.endswith(b"line-8000\n")
    assert full.count(b"\n") == 8000
    assert result.details["truncation"]["total_lines"] == 8000
    await journal.aclose()


async def test_command_environment_excludes_inherited_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "fake-secret-value")
    monkeypatch.setenv("PYCODE_UNLISTED_VALUE", "unlisted")
    executor, context, journal, outputs = await assemble(tmp_path)
    try:
        result = await executor.execute(
            context, ToolCall("call-1", "bash", {"command": "env"}), "tool_use"
        )
        assert not result.is_error and result.exit_code == 0
        variables = dict(line.split("=", 1) for line in result.text.splitlines() if "=" in line)
        assert "FAKE_API_KEY" not in variables and "fake-secret-value" not in result.text
        assert "PYCODE_UNLISTED_VALUE" not in variables
        assert variables["PATH"]
        assert variables["HOME"]
    finally:
        await journal.aclose()


async def test_explicit_proxy_and_agent_socket_passthrough_does_not_include_other_values(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.test:8443")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/explicit-agent.sock")
    monkeypatch.setenv("FAKE_API_KEY", "fake-secret-value")
    monkeypatch.setenv("PYCODE_UNLISTED_VALUE", "unlisted")
    executor, context, journal, _ = await assemble(
        tmp_path, passthrough=("HTTPS_PROXY", "SSH_AUTH_SOCK")
    )
    try:
        result = await executor.execute(
            context, ToolCall("call-1", "bash", {"command": "env"}), "tool_use"
        )
        assert not result.is_error
        variables = dict(line.split("=", 1) for line in result.text.splitlines() if "=" in line)
        assert variables["HTTPS_PROXY"] == "https://proxy.example.test:8443"
        assert variables["SSH_AUTH_SOCK"] == "/tmp/explicit-agent.sock"
        assert "FAKE_API_KEY" not in variables and "PYCODE_UNLISTED_VALUE" not in variables
    finally:
        await journal.aclose()


async def test_evaluation_path_prefix_survives_final_child_environment_filter(
    tmp_path, monkeypatch
):
    _, context, journal, outputs = await assemble(tmp_path)
    context = replace(context, call_id="path-prefix", tool_name="bash")
    prefix = tmp_path / "evaluation-python-bin"
    feature = SandboxFeature(SandboxSettings(), path_prefix=(str(prefix),))
    (bash,) = feature.tools(run_scope(EventStream("s1", "r1", ())))
    captured = {}

    async def capture_process(argv, workspace, writer, result, **options):
        captured.update(options["environment"])
        return await writer.finish(result)

    monkeypatch.setattr("pycode.sandbox.tools.run_process", capture_process)
    try:
        await bash.execute(context, {"command": "true"})
        assert captured["PATH"].split(os.pathsep)[0] == str(prefix)
    finally:
        await journal.aclose()


async def test_cancelled_command_cannot_write_after_owner_returns(tmp_path):
    executor, context, journal, outputs = await assemble(tmp_path)
    code = (
        "from pathlib import Path; import time; Path('ready').write_text('yes'); "
        "time.sleep(1); Path('late').write_text('bad')"
    )
    running = asyncio.create_task(
        executor.execute(
            context, ToolCall("call-1", "bash", {"command": python_command(code)}), "tool_use"
        )
    )
    try:
        async with asyncio.timeout(5):
            ready = tmp_path / "ready"
            while not await asyncio.to_thread(ready.exists):  # noqa: ASYNC110 - external child readiness
                await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(1.1)
        assert not (tmp_path / "late").exists()
        assert (await journal.snapshot()).entries[-1].message.role == "user"
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await journal.aclose()


async def test_timeout_stops_owned_command_and_preserves_known_output(tmp_path):
    executor, context, journal, outputs = await assemble(tmp_path)
    code = (
        "import sys,time; from pathlib import Path; "
        "sys.stdout.buffer.write(b'HEAD\\n'); sys.stdout.flush(); "
        "time.sleep(1); Path('late').write_text('bad')"
    )
    result = await executor.execute(
        context,
        ToolCall(
            "call-1",
            "bash",
            {
                "command": python_command(code),
                "timeout": 0.3,
            },
        ),
        "tool_use",
    )
    assert result.is_error and result.error_code == "command_timeout"
    assert "HEAD" in result.text
    await asyncio.sleep(1.1)
    assert not (tmp_path / "late").exists()
    await journal.aclose()


async def test_timeout_kills_descendant_even_after_original_process_exits(tmp_path):
    executor, context, journal, outputs = await assemble(tmp_path)
    child = "import time; from pathlib import Path; time.sleep(1); Path('late').write_text('bad')"
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "sys.stdout.buffer.write(b'PARENT\\n'); sys.stdout.flush()"
    )
    result = await executor.execute(
        context,
        ToolCall(
            "call-1",
            "bash",
            {
                "command": python_command(parent),
                "timeout": 0.4,
            },
        ),
        "tool_use",
    )
    assert result.is_error and result.error_code == "command_timeout"
    await asyncio.sleep(1.1)
    assert not (tmp_path / "late").exists()
    await journal.aclose()


async def test_descendant_output_after_parent_exit_is_not_cut_off_by_fixed_grace(tmp_path):
    executor, context, journal, outputs = await assemble(tmp_path)
    child = (
        "import sys,time; "
        "[(time.sleep(0.05),sys.stdout.buffer.write(('TICK'+str(i)+chr(10)).encode()),"
        "sys.stdout.flush()) "
        "for i in range(6)]"
    )
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "sys.stdout.buffer.write(b'HEAD\\n'); sys.stdout.flush()"
    )
    result = await executor.execute(
        context,
        ToolCall(
            "call-1",
            "bash",
            {
                "command": python_command(parent),
                "timeout": 3,
            },
        ),
        "tool_use",
    )
    assert not result.is_error
    assert "HEAD" in result.text and "TICK5" in result.text
    await journal.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")


async def test_run_command_adds_env_to_the_allowlist_and_reports_hook_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_API_KEY", "fake-secret-value")
    feature = SandboxFeature(SandboxSettings())
    session = run_scope(EventStream("s1", "r1", ())).session
    await feature.open(session)
    shell = find_bash()
    result = await feature.run_command(
        (shell, "-c", 'echo "$HOOK_VALUE:${FAKE_API_KEY:-none}"'),
        str(tmp_path),
        {"HOOK_VALUE": "hi"},
        5,
    )
    assert (result.text.strip(), result.is_error) == ("hi:none", False)
    failed = await feature.run_command((shell, "-c", "exit 4"), str(tmp_path), None, 5)
    assert (failed.error_code, failed.exit_code) == ("hook_command_failed", 4)
