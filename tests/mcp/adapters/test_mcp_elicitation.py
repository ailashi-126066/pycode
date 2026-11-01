"""An unsupported MCP question cannot leave an unanswerable graph interrupt."""

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.mcp.adapters.test_mcp import mcp_config
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_mcp_elicitation_is_an_error_not_an_input_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await mcp_config(config)
    with protocol_server([skill_reply("mcp__srv__confirm", {}, "c1"), chat_completion()]) as (
        url,
        requests,
    ):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            # Like every tool exception, the failure becomes one error result for the model.
            result = await session.run(Message("u1", "user", "Ask MCP"))
            assert result.outcome == "completed" and len(requests) == 2
            state = await session.inspect()
            assert not state["pending"]
            entries = state["snapshot"]["entries"]
            results = [e["message"]["tool_result"] for e in entries if e["message"]]
            results = [item for item in results if item]
            assert [item["error_code"] for item in results] == ["mcp_elicitation_unsupported"]
            assert not any(entry["type"] == "mcp_elicitation" for entry in entries)
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
