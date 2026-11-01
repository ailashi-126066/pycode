import json

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_nonzero_command_exit_is_visible_in_real_openai_tool_content(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    response = chat_completion(tool=True)
    response.body = (
        response.body.decode()
        .replace('"name": "echo"', '"name": "bash"')
        .replace(
            json.dumps('{"value":"wire"}'),
            json.dumps(json.dumps({"command": "printf 'ordinary output'; exit 7"})),
        )
        .encode()
    )
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([response, chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        pending = await session.run(Message("u1", "user", "test command"))
        result = await session.resume(
            {
                "operation_id": pending.pending[0]["operation_id"],
                "decision": "approve",
            }
        )
        assert result.outcome == "completed"
        content = next(
            message["content"]
            for message in requests[1]["body"]["messages"]
            if message["role"] == "tool"
        )
        assert "ordinary output" in content and "exit code 7" in content and "Tool error" in content
        await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
