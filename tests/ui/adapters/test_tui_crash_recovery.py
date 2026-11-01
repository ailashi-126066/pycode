import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from pycode.session.adapters.storage.layout import StorageLayout
from tests.session.adapters.test_revert_http import command_reply
from tests.snapshot.adapters.test_git_tree import git, repository
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.ui.adapters.test_tui_http import settled


@pytest.mark.parametrize("compensate", [False, True], ids=["continue", "compensate"])
async def test_actual_process_exit_after_revert_branch_is_recovered_by_tui_controls(
    tmp_path, monkeypatch, compensate
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    root = repository(tmp_path)
    (root / "file.txt").write_text("old", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    config = AppConfig(root, tmp_path / "data", memory_mode="off")
    layout = StorageLayout(root, config.user_data)
    with protocol_server(
        [command_reply("printf new > file.txt"), chat_completion(), chat_completion()]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            waiting = await session.run(Message("u1", "user", "change"))
            await session.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            state = await session.inspect()
            target = next(
                entry["id"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["id"] == "u1"
            )
            plan = await session.preview_revert(target, "both")
        finally:
            await session.aclose()
        plan_path = tmp_path / "revert.json"
        plan_path.write_text(json.dumps(asdict(plan)), encoding="utf-8")
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.revert_crash_worker",
            str(root),
            str(layout.project / "snapshots"),
            str(layout.sessions / "s1"),
            str(plan_path),
            "after-branch",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            assert child.returncode == 76, (stdout.decode(), stderr.decode())
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        app = compose_application(config, (provider(url),)).tui(session_id="s1")
        async with app.run_test(size=(120, 50)) as pilot:
            await app.ready.wait()
            assert app.query("#revert-inline") and len(requests) == 2
            button = "#restore-compensate" if compensate else "#restore-continue"
            await pilot.pause()
            await pilot.click(button)
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert app.state["pending_revert_id"] is None
            assert (root / "file.txt").read_text(encoding="utf-8") == (
                "new" if compensate else "old"
            )
            await app.send_text("continue after recovery")
            await settled(app)
            assert app.state["pending_run_id"] is None and len(requests) == 3


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
