import json

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def search_response(relative=False):
    response = chat_completion(tool=True)
    rows = []
    for line in response.body.decode().splitlines():
        if line.startswith("data: {"):
            data = json.loads(line[6:])
            for choice in data.get("choices", []):
                if "tool_calls" in choice["delta"]:
                    choice["delta"]["tool_calls"] = [
                        {
                            "index": 0,
                            "id": "find-first",
                            "type": "function",
                            "function": {
                                "name": "find",
                                "arguments": json.dumps(
                                    {"pattern": "*.py"}
                                    if relative
                                    else {"pattern": "*.py", "path": "/workspace/"}
                                ),
                            },
                        },
                        {
                            "index": 1,
                            "id": "grep-second",
                            "type": "function",
                            "function": {
                                "name": "grep",
                                "arguments": json.dumps(
                                    {
                                        "pattern": "needle",
                                        "glob": "*.py",
                                        "path": "." if relative else "/workspace/",
                                    }
                                ),
                            },
                        },
                    ]
            rows.append("data: " + json.dumps(data) + "\n\n")
        elif line == "data: [DONE]":
            rows.append("data: [DONE]\n\n")
    response.body = "".join(rows).encode()
    return response


@pytest.mark.requires_rg
@pytest.mark.parametrize("relative", [False, True])
async def test_public_agent_executes_parallel_read_searches_without_approval_and_pairs_outputs(
    tmp_path, monkeypatch, relative
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    (tmp_path / "one.py").write_text("needle one\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/你好.py").write_text("needle two\n", encoding="utf-8")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([search_response(relative), chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            result = await session.run(Message("u1", "user", "search project"))
            assert result.outcome == "completed" and len(requests) == 2
            names = {item["function"]["name"] for item in requests[0]["body"]["tools"]}
            assert {"read", "write", "edit", "bash", "grep", "find", "ls"} <= names
            outputs = {
                message.tool_result.call_id: message.tool_result
                for message in result.messages
                if message.tool_result
            }
            assert set(outputs) == {"find-first", "grep-second"}
            # Pi: paths relative to the search directory.
            assert outputs["find-first"].text == "nested/你好.py\none.py"
            assert (
                outputs["grep-second"].text == "nested/你好.py:1: needle two\none.py:1: needle one"
            )
            assert all(not item.is_error and item.operation_id for item in outputs.values())
            assert (
                await session.run(Message("u1", "user", "search project"))
            ).outcome == "completed"
            assert len(requests) == 2
        finally:
            await session.aclose()
    reader = await compose_application(config).session_repository().open("s1")
    try:
        snapshot = await reader.snapshot()
        assert (
            sum(
                entry.message is not None and entry.message.role == "tool_result"
                for entry in snapshot.entries
            )
            == 2
        )
    finally:
        await reader.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
