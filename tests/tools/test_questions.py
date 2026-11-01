"""Questions resume only in this process; restart preserves answers and expires waits."""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message, StorageError
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def question_response(arguments):
    response = chat_completion(tool=True)
    response.body = (
        response.body.decode()
        .replace('"name": "echo"', '"name": "AskUserQuestion"')
        .replace(json.dumps('{"value":"wire"}'), json.dumps(json.dumps(arguments)))
        .encode()
    )
    return response


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_question_resumes_in_process_and_returns_one_paired_result(
    tmp_path, monkeypatch, cancel
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    arguments = {
        "questions": [
            {"type": "text", "name": "language", "title": "Language", "message": "Which language?"},
            {"type": "radio", "name": "style", "message": "Style?", "options": ["A", "B"]},
            {
                "type": "checkbox",
                "name": "features",
                "message": "Features?",
                "options": ["tests", "docs"],
            },
        ]
    }
    with protocol_server([question_response(arguments), chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            waiting = await session.run(Message("u1", "user", "ask questions"))
            assert waiting.outcome == "needs_input" and waiting.pending[0]["kind"] == "ask_user"
            with pytest.raises(StorageError):
                await session.resume(
                    {"operation_id": waiting.pending[0]["operation_id"], "answers": {}}
                )
            assert (await session.resume()).pending == waiting.pending
            answer = (
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "cancel"}
                if cancel
                else {
                    "operation_id": waiting.pending[0]["operation_id"],
                    "answers": {
                        "language": "Python",
                        "style": {"other": "custom style"},
                        "features": ["tests", "docs"],
                    },
                }
            )
            result = await session.resume(answer)
            assert result.outcome == "completed" and len(requests) == 2
            assert (
                await session.run(Message("u1", "user", "ask questions"))
            ).outcome == "completed"
            assert len(requests) == 2
        finally:
            await session.aclose()
        store = await compose_application(config).session_repository().open("s1")
        try:
            results = [
                entry.message.tool_result
                for entry in (await store.snapshot()).entries
                if entry.message and entry.message.tool_result
            ]
            assert len(results) == 1 and results[0].call_id == "call-1"
            assert results[0].is_error == cancel
            assert (results[0].error_code == "question_cancelled") == cancel
            if not cancel:
                assert (
                    "language: Python" in results[0].text
                    and "style: custom style" in results[0].text
                )
                assert "features: tests, docs" in results[0].text
        finally:
            await store.aclose()


@pytest.mark.parametrize("boundary", ["question_wait", "question_answered"])
async def test_actual_process_death_preserves_answer_and_explicit_cancel_ends_wait(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    arguments = {"questions": [{"type": "text", "name": "language", "message": "Which language?"}]}
    with protocol_server([question_response(arguments)]) as (url, requests):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "tests.support.crash_worker",
            str(tmp_path),
            url,
            boundary,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            assert child.returncode == 73, (stdout.decode(), stderr.decode())
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        restored = await compose_application(config, (provider(url),)).open_session(
            "s1", refresh_models=False
        )
        try:
            assert len(requests) == 1
            await restored.cancel()
            state = await restored.inspect()
            assert state["pending"] == () and state["pending_run_id"] is None
            with pytest.raises(StorageError):
                await restored.resume()
            assert len(requests) == 1
            results = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["tool_result"]
            ]
            assert len(results) == 1
            if boundary == "question_wait":
                assert results[0]["error_code"] == "tool_interrupted"
            else:
                assert not results[0]["is_error"] and "language: Python" in results[0]["text"]
        finally:
            await restored.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
