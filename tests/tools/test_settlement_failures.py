"""Persistence failure stops a run; failed checks block their tool and return a paired result."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message, StorageError, ToolResult
from pycode.tools.executor import ToolExecutor
from tests.agent.test_runtime import assemble, tool_response
from tests.skills.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.tools.test_tool_executor import run_scope


async def test_result_write_failure_collects_other_parallel_tool_before_return():
    runtime, context, model, original, journal, _ = assemble(
        [tool_response(two=True), AIMessage(id="done", content="done")]
    )
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class Reader:
        spec = original.spec

        async def execute(self, current, arguments):
            if arguments["value"] == "first":
                await started.wait()
                return ToolResult("", "", "read completed")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    append = journal.append_message

    async def fail_result(message):
        if message.role == "tool_result":
            raise StorageError("forced_write_failure", "Result persistence failed")
        return await append(message)

    journal.append_message = fail_result
    context.registry.replace_source("builtin", (Reader(),))
    context.executor = ToolExecutor(
        context.registry,
        context.executor.validator,
        context.executor.approval,
        run_scope(context.events),
    )
    with pytest.raises(StorageError, match="persistence failed"):
        await asyncio.wait_for(runtime.run((Message("u", "user", "read both"),), context), 5)
    assert cleaned.is_set()
    assert model.call_index == 1
    assert not context.tool_tasks


async def test_real_failing_pretool_hook_blocks_tool_and_reports_to_next_model(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    (tmp_path / "code.py").write_text("original")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    config.user_data.mkdir()
    (config.user_data / "hooks.json").write_text(
        json.dumps(
            [
                {
                    "id": "failure",
                    "event": "pre_tool_use",
                    "action": {"type": "command", "command": "exit 7"},
                }
            ]
        ),
        encoding="utf-8",
    )
    with protocol_server(
        [skill_reply("read", {"path": "/workspace/code.py"}, "read"), chat_completion()]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s", create=True, refresh_models=False
        )
        try:
            result = await session.run(Message("u", "user", "read"))
            assert result.outcome == "completed" and len(requests) == 2
            state = await session.inspect()
            assert state["pending_run_id"] is None
            results = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] is not None and entry["message"]["tool_result"] is not None
            ]
            assert len(results) == 1 and results[0]["error_code"] == "hook_rejected"
            assert results[0]["call_id"] == "read"
            assert "original" not in results[0]["text"]
            assert (tmp_path / "code.py").read_text() == "original"
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
