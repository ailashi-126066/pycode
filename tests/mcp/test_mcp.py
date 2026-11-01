import asyncio
import json
import sys
from pathlib import Path

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message
from tests.skills.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server

SERVER = Path(__file__).parents[1] / "support/mcp_server.py"


async def mcp_config(config, *, mode="direct", trusted=True):
    def write():
        config.user_data.mkdir(parents=True, exist_ok=True)
        (config.user_data / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": sys.executable,
                            "args": [str(SERVER)],
                            "trusted": trusted,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    await asyncio.to_thread(write)


@pytest.mark.parametrize("mode", ["direct", "dispatch"])
async def test_real_mcp_tool_through_native_sdk_model_and_shared_executor(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config, mode=mode)
    name = "mcp__srv__echo"
    args = {"value": "wire"}
    with protocol_server(
        [
            skill_reply("ToolSearch", {"query": name}, "search"),
            skill_reply(name, args, "call"),
            chat_completion(),
        ]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            result = await session.run(Message("u1", "user", "call MCP"))
            assert result.outcome == "completed" and len(requests) == 3
            specs = {item["function"]["name"] for item in requests[0]["body"]["tools"]}
            assert {"ToolSearch", "bash", "read"} <= specs
            assert name not in specs
            assert "mcp__srv__echo" in {t["function"]["name"] for t in requests[1]["body"]["tools"]}
            state = await session.inspect()
            outputs = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "tool_result"
            ]
            assert len(outputs) == 2 and outputs[-1]["text"] == "echo:wire"
            assert outputs[-1]["name"] == name
            report = state["context_usage"]
            assert report["source"] == "provider_usage" and report["tokens"] == 25
        finally:
            await session.aclose()
        view = await compose_application(config).open_view("s1")
        try:
            assert (await view.inspect())["context_usage"] == report
        finally:
            await view.aclose()


async def test_dispatch_default_side_effect_approval_reopens_and_executes_once(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config, mode="dispatch")
    reply = skill_reply("mcp__srv__mutate", {"value": "once"}, "call")
    with protocol_server(
        [
            skill_reply("ToolSearch", {"query": "mcp__srv__mutate"}, "search"),
            reply,
            chat_completion(),
        ]
    ) as (url, requests):
        configured = provider(url)
        session = await compose_application(config, (configured,)).open_session(
            "s1", create=True, refresh_models=False
        )
        waiting = await session.run(Message("u1", "user", "change"))
        assert waiting.outcome == "needs_input" and not (tmp_path / "effects.txt").exists()
        assert waiting.pending[0]["call"]["name"] == "mcp__srv__mutate"
        try:
            assert (await session.resume()).pending == waiting.pending
            done = await session.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            assert done.outcome == "completed" and len(requests) == 3
            assert (tmp_path / "effects.txt").read_text().splitlines() == ["once"]
        finally:
            await session.aclose()
