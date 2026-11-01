import asyncio
from pathlib import Path

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.messages import Message
from tests.hooks.adapters.test_hooks import hook_file
from tests.mcp.adapters.test_mcp import mcp_config
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_large_mcp_result_is_complete_on_disk_and_readable_through_its_full_output_path(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config, mode="dispatch")
    with protocol_server(
        [
            skill_reply("mcp__srv__big", {"lines": 2400}, "big"),
            chat_completion(),
        ]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            assert (await session.run(Message("u1", "user", "big"))).outcome == "completed"
            state = await session.inspect()
            result = next(
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "tool_result"
            )
            path = result["details"]["full_output_path"]
            assert "中文行2399" not in result["text"] and result["text"].endswith(path + "]")
            # Pi: the complete output is an ordinary temp file the model can read.
            assert "中文行2399" in await asyncio.to_thread(Path(path).read_text, "utf-8")
            assert len(requests) == 2
        finally:
            await session.aclose()


async def test_dispatch_uses_target_name_for_hook_rejection_and_never_calls_server(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config, mode="dispatch")
    await hook_file(
        config,
        [
            {
                "id": "protect",
                "event": "pre_tool_use",
                "reject": True,
                "if": 'tool == "mcp__srv__mutate"',
                "action": {"type": "prompt", "message": "Rejected target"},
            }
        ],
    )
    with protocol_server(
        [
            skill_reply(
                "mcp__srv__mutate",
                {"value": "blocked"},
                "mutate",
            ),
            chat_completion(),
        ]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            assert (await session.run(Message("u1", "user", "change"))).outcome == "completed"
            assert not (tmp_path / "effects.txt").exists() and len(requests) == 2
            state = await session.inspect()
            result = next(
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "tool_result"
            )
            assert result["name"] == "mcp__srv__mutate" and result["error_code"] == "hook_rejected"
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
