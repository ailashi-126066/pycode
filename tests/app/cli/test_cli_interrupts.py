import asyncio
import json
import threading

import pytest

from pycode.app.cli.commands import command
from pycode.app.cli.main import main
from pycode.app.cli.parser import build_parser
from pycode.app.config import AppConfig
from tests.app.cli.test_execution_cli import read_session, rows, setup_cli
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.tools.test_questions import question_response


def test_question_cli_retains_named_question_and_accepts_answer_file_after_restart(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    arguments = {
        "questions": [
            {"type": "text", "name": "language", "message": "Which language?"},
            {"type": "radio", "name": "style", "message": "Which style?", "options": ["A", "B"]},
        ]
    }
    with protocol_server([question_response(arguments), chat_completion()]) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        assert main([*common, "run", "ask", *execution, "--create", "--output-format", "json"]) == 2
        pending = rows(capsys)[0]["pending"][0]
        assert pending["kind"] == "ask_user"
        assert main([*common, "resume", *execution, "--output-format", "json"]) == 0
        again = rows(capsys)[0]
        assert again["pending"] and again["pending_run_id"] is not None
        assert len(requests) == 1
        answer_file = tmp_path / "answers.json"
        answer_file.write_text(
            json.dumps(
                {
                    "operation_id": pending["operation_id"],
                    "answers": {"language": "Python", "style": {"other": "custom"}},
                }
            ),
            encoding="utf-8",
        )
        assert (
            main(
                [
                    *common,
                    "resume",
                    *execution,
                    "--answers-file",
                    str(answer_file),
                    "--output-format",
                    "json",
                ]
            )
            == 0
        )
        final = rows(capsys)[0]
        assert final["outcome"] == "completed" and len(requests) == 2
        results = [
            entry.message.tool_result
            for entry in asyncio.run(read_session(tmp_path)).entries
            if entry.message and entry.message.tool_result
        ]
        assert len(results) == 1 and not results[0].is_error
        assert len(requests) == 2


async def test_cli_cancellation_collects_request_and_publishes_aborted_result_then_can_resume(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    first = chat_completion()
    started, release = threading.Event(), threading.Event()
    first.started, first.release = started, release
    with protocol_server([first, chat_completion()]) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        args = build_parser().parse_args(
            [*common, "run", "task", *execution, "--create", "--output-format", "stream-json"]
        )
        task = asyncio.create_task(
            command(args, AppConfig(tmp_path, tmp_path / "data", memory_mode="off"))
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            interrupted = rows(capsys)
            assert (
                interrupted[-1]["type"] == "result"
                and interrupted[-1]["data"]["outcome"] == "aborted"
            )
            assert interrupted[-1]["data"]["usage"]["last_request"]["status"] == "interrupted"
            release.set()
            resume = build_parser().parse_args(
                [
                    *common,
                    "run",
                    "Continue as a new task",
                    *execution,
                    "--output-format",
                    "stream-json",
                ]
            )
            assert (
                await command(resume, AppConfig(tmp_path, tmp_path / "data", memory_mode="off"))
                == 0
            )
            completed = rows(capsys)
            assert completed[-1]["data"]["outcome"] == "completed"
            assert completed[0]["sequence"] == 1
            assert completed[-1]["run_id"] != interrupted[-1]["run_id"]
            assert len(requests) == 2
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


pytestmark = pytest.mark.usefixtures("disclosed_tools")
