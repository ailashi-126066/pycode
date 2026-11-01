import asyncio
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message, StorageError
from pycode.models import SQLiteRequestStore
from pycode.session import StorageLayout
from tests.context.test_summary_http import seed
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


@pytest.mark.parametrize("boundary", ["ready", "generating", "request_started"])
async def test_real_summary_process_death_uses_completed_object_or_regenerates_lost_job(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=20, memory_mode="off"
    )
    await seed(config, characters=100)
    initial = chat_completion()
    initial.body = (
        initial.body.decode()
        .replace('"prompt_tokens": 20', '"prompt_tokens": 17000')
        .replace('"completion_tokens": 5', '"completion_tokens": 300')
        .replace('"total_tokens": 25', '"total_tokens": 17300')
        .encode()
    )
    with protocol_server([initial, chat_completion(), chat_completion(), chat_completion()]) as (
        url,
        requests,
    ):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.summary_crash_worker",
            str(tmp_path),
            url,
            boundary,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
            assert child.returncode == 74, (stdout.decode(), stderr.decode())
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        selected = replace(provider(url), context_window=20000, max_output_tokens=1000)
        restored = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        try:
            try:
                await restored.resume()
            except StorageError as error:
                assert error.code == "no_pending_run"
            result = await restored.run(Message("arrived", "user", "new input " + "y" * 7000))
            # The split turn adds a summary-turn-prefix request.
            assert result.outcome == "completed" and len(requests) == 4
            assert not any(
                "old task 0" in str(message.get("content", ""))
                for message in requests[-1]["body"]["messages"]
            )
        finally:
            await restored.aclose()
        records = SQLiteRequestStore(StorageLayout(tmp_path, config.user_data).runtime_database)
        attempts = await records.list_requests("s1")
        assert not any(record.status == "started" for record in attempts)
        if boundary == "request_started":
            assert any(
                record.status == "interrupted" and record.purpose == "summary"
                for record in attempts
            )
        await records.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
