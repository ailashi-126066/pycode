import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message, StorageError
from tests.context.test_summary_http import seed
from tests.mcp.test_mcp import mcp_config
from tests.skills.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_changed_server_schema_is_recorded_but_pending_call_keeps_old_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config, mode="dispatch")
    with protocol_server(
        [
            skill_reply(
                "mcp__srv__mutate",
                {"value": "old"},
                "mutate",
            ),
            chat_completion(),
        ]
    ) as (url, requests):
        configured = provider(url)
        session = await compose_application(config, (configured,)).open_session(
            "s1", create=True, refresh_models=False
        )
        waiting = await session.run(Message("u1", "user", "change"))
        assert waiting.outcome == "needs_input"
        await session.aclose()
        await asyncio.to_thread((tmp_path / "schema.variant").write_text, "new", encoding="utf-8")
        session = await compose_application(config, (configured,)).open_session(
            "s1", refresh_models=False
        )
        try:
            await session.cancel()
            with pytest.raises(StorageError) as error:
                await session.resume(
                    {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
                )
            assert error.value.code == "no_pending_run" and len(requests) == 1
            done = await session.run(Message("new-task", "user", "Inspect the new MCP catalog"))
            assert done.outcome == "completed" and not (tmp_path / "effects.txt").exists()
            state = await session.inspect()
            assert any(
                entry["type"] == "tool_set_change" and entry["data"].get("source") == "mcp"
                for entry in state["snapshot"]["entries"]
            )
            results = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "tool_result"
            ]
            assert (
                results[-1]["error_code"] == "tool_abandoned"
                and results[-1]["name"] == "mcp__srv__mutate"
            )
            assert len(requests) == 2
        finally:
            await session.aclose()


async def test_compaction_and_restart_preserve_mcp_catalog_schema_and_loading_mode(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=1500, memory_mode="off"
    )
    await mcp_config(config, mode="direct")
    await seed(config)
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        configured = provider(url)
        session = await compose_application(config, (configured,)).open_session(
            "s1", refresh_models=False
        )
        try:
            assert (await session.compact()).applied_job_id
            state = await session.inspect()
            checkpoint = next(
                entry["data"]["checkpoint"]
                for entry in state["snapshot"]["entries"]
                if entry["type"] == "compaction"
            )
            assert checkpoint["mcp"]["tools"] and any(
                spec["name"] == "mcp__srv__echo" for spec in checkpoint["tools"]
            )
            assert "MCP servers" not in checkpoint["system_prompt"]
        finally:
            await session.aclose()
        session = await compose_application(config, (configured,)).open_session(
            "s1", refresh_models=False
        )
        try:
            assert (await session.run(Message("new", "user", "continue"))).outcome == "completed"
            assert any(
                tool["function"]["name"] == "mcp__srv__echo"
                for tool in requests[-1]["body"]["tools"]
            )
            assert (
                sum(
                    "MCP servers" in str(message["content"])
                    for message in requests[-1]["body"]["messages"]
                )
                == 1
            )
        finally:
            await session.aclose()


async def test_actual_parent_exit_reuses_confirmed_mcp_result_without_second_server_call(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config, mode="dispatch")
    reply = skill_reply("mcp__srv__mutate", {"value": "once"}, "mutate")
    with protocol_server([reply, chat_completion()]) as (url, requests):
        configured = provider(url)
        request = {
            "workspace": str(tmp_path),
            "data": str(config.user_data),
            "provider": asdict(configured),
        }
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "tests.support.mcp_crash_worker"],
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=30,
            env=dict(os.environ),
        )
        assert child.returncode == 85, child.stderr.decode(errors="replace")
        assert (tmp_path / "effects.txt").read_text().splitlines() == ["once"]
        session = await compose_application(config, (configured,)).open_session(
            "s1", refresh_models=False
        )
        try:
            await session.cancel()
            with pytest.raises(StorageError):
                await session.resume()
            assert len(requests) == 1
            assert (
                await session.run(
                    Message("new-task", "user", "Continue without repeating the MCP call")
                )
            ).outcome == "completed"
            assert len(requests) == 2
            assert (tmp_path / "effects.txt").read_text().splitlines() == ["once"]
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
