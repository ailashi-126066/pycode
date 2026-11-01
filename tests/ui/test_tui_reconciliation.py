import asyncio
import os
import subprocess
import sys

import pytest

from pycode.app import AppConfig, compose_application
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.support.recovery_tools import WritingEcho
from tests.ui.test_tui_http import settled


async def test_process_crash_has_unknown_fact_without_replaying_effect_or_old_approval(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion(tool=True), chat_completion(), chat_completion()]) as (
        url,
        requests,
    ):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.crash_worker",
            str(tmp_path),
            url,
            "effect_written",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            assert child.returncode == 73, (stdout.decode(), stderr.decode())
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert (tmp_path / "effect.txt").read_text(encoding="utf-8") == "wire"
        tool = WritingEcho()
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).tui(session_id="s1", tools=(tool,))
        async with app.run_test(size=(120, 50)):
            await app.ready.wait()
            await app.command("/resume")
            await settled(app)
            assert not app.query("#reconcile-inline") and tool.executed == 0 and len(requests) == 2
            outputs = [
                message["tool_result"]
                for message in app.state["context"]["messages"]
                if message["tool_result"]
            ]
            assert len(outputs) == 1 and outputs[0]["error_code"] == "tool_interrupted"
            await app.send_text(
                "I verified effect.txt contains wire; continue without rerunning it"
            )
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert app.state["pending_run_id"] is None and len(requests) == 3
            assert (
                tool.executed == 0
                and (tmp_path / "effect.txt").read_text(encoding="utf-8") == "wire"
            )


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
