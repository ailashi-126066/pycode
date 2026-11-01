"""MCP discovery exposes tool controls only, even if the server has other capabilities."""

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.mcp.adapters.test_mcp import mcp_config
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_mcp_exposes_tools_without_resource_or_prompt_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config, mode="dispatch")
    with protocol_server([chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            assert (await session.run(Message("u1", "user", "inspect"))).outcome == "completed"
            names = {item["function"]["name"] for item in requests[0]["body"]["tools"]}
            assert {"ToolSearch", "bash", "read"} <= names
            assert not any(name.startswith("mcp__") for name in names)
            assert not names & {"MCPResources", "MCPReadResource", "MCPPrompts", "MCPGetPrompt"}
            assert "Resource content stays on demand" not in str(requests[0]["body"]["messages"])
        finally:
            await session.aclose()
