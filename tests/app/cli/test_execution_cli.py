# Vectors: local tests/test_stream_json.py, source inventory in docs/source-map.md.
# Adaptation: real SDK HTTP and persistent interrupt instead of source monkeypatch.
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from tests.context.test_summary_http import seed
from tests.snapshot.test_git_tree import git, repository
from tests.support.execution import provider
from tests.support.http_provider import (
    HTTPResponse,
    chat_completion,
    command_reply,
    protocol_server,
)

from pycode.app.api import compose_application
from pycode.app.cli.main import main
from pycode.app.config import AppConfig
from pycode.kernel import Message

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/cli-baselines.json").read_text(encoding="utf-8")
)


def setup_cli(tmp_path, configured, workspace=None):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps([asdict(configured)]), encoding="utf-8")
    common = ["--workspace", str(workspace or tmp_path), "--data-dir", str(tmp_path / "data")]
    common.extend(["--memory-mode", "off"])
    execution = ["--providers", str(path), "--cached", "--session", "s1"]
    return common, execution


async def read_session(tmp_path, session_id="s1", workspace=None):
    store = (
        await compose_application(
            AppConfig(workspace or tmp_path, tmp_path / "data", memory_mode="off")
        )
        .session_repository()
        .open(session_id)
    )
    try:
        return await store.snapshot()
    finally:
        await store.aclose()


def rows(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_prompt_streams_one_terminal_result_after_history_and_checkpoint_with_all_usage(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion()]) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        assert (
            main(
                [
                    *common,
                    *execution,
                    "--create",
                    "--input-id",
                    "u1",
                    "-p",
                    "你好",
                    "--output-format",
                    "stream-json",
                ]
            )
            == 0
        )
        events = rows(capsys)
        assert (
            events[-1]["type"] == "result"
            and sum(row["type"] == "result" for row in events)
            == VECTORS["cases"][0]["expected_result_events"]
        )
        assert events[-1]["data"]["final_text"] == "done"
        assert events[-1]["data"]["usage"]["main_turn"]["tokens"]["input_tokens"] == 20
        assert events[-1]["data"]["usage"]["main_turn"]["tokens"]["output_tokens"] == 5
        assert {row["type"] for row in events} >= {"agent_start", "turn_start", "message_update"}
        reply = next(
            row["data"]["message"]
            for row in events
            if row["type"] == "message_end" and row["data"]["message"]["role"] == "assistant"
        )
        assert reply["usage"]["output_tokens"] == 5 and "duration_ms" in reply
        run_id = events[-1]["run_id"]
        run_events = [event for event in events if event["run_id"] == run_id]
        assert [event["sequence"] for event in run_events] == list(range(1, len(run_events) + 1))
        assert any(event["type"] == "model_change" for event in events)
        snapshot = asyncio.run(read_session(tmp_path))
        assert snapshot.entries[-1].data["kind"] == "run_end"
        assert events[-1]["data"]["leaf_id"] == snapshot.leaf_id
        assert events[-1]["data"]["checkpoint_id"] == snapshot.entries[-1].data["checkpoint_id"]
        assert (
            main(
                [*common, "run", "你好", *execution, "--input-id", "u1", "--output-format", "json"]
            )
            == 0
        )
        retried = rows(capsys)[0]
        assert retried["run_id"] == run_id and retried["usage"]["session"]["request_count"] == 1
        assert len(requests) == 1
        assert (
            main(
                [
                    *common,
                    "session",
                    "show",
                    "s1",
                ]
            )
            == 0
        )
        replay = rows(capsys)[0]
        assert replay["snapshot"]["leaf_id"] == snapshot.leaf_id
        assert len(requests) == 1
        assert main([*common, "usage", "--session", "s1", "--json"]) == 0
        usage = rows(capsys)[0]
        assert usage["session"]["tokens"]["cache_read_tokens"] == 12
        assert usage["session"]["tokens"]["cache_miss_tokens"] is None
        assert not (tmp_path / ".git").exists()


def test_command_cli_keeps_approval_persistent_and_resume_does_not_repeat_first_request(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([command_reply("printf approved > effect.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        common, execution = setup_cli(tmp_path, provider(url))
        assert (
            main(
                [
                    *common,
                    "run",
                    "execute",
                    *execution,
                    "--create",
                    "--output-format",
                    "stream-json",
                ]
            )
            == 2
        )
        events = rows(capsys)
        first = events[-1]
        pending = first["data"]["pending"][0]
        assert first["data"]["outcome"] == "needs_input"
        assert any(row["type"] == "needs_input" for row in events)
        assert not (tmp_path / "effect.txt").exists()
        answers = json.dumps({"operation_id": pending["operation_id"], "decision": "approve"})
        assert (
            main(
                [
                    *common,
                    "resume",
                    *execution,
                    "--answers",
                    answers,
                    "--output-format",
                    "stream-json",
                ]
            )
            == 0
        )
        resumed = rows(capsys)[-1]
        assert resumed["data"]["outcome"] == "completed"
        assert resumed["run_id"] == first["run_id"]
        assert (tmp_path / "effect.txt").read_text() == "approved"
        assert len(requests) == 2


def test_session_cli_forks_and_dialogue_navigation_preserves_native_history(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        assert main([*common, "run", "one", *execution, "--create", "--output-format", "json"]) == 0
        rows(capsys)
        assert main([*common, "session", "fork", "s1", "child"]) == 0
        fork = rows(capsys)[0]
        assert fork["header"]["parent_session_id"] == "s1"
        assert main([*common, "session", "list"]) == 0
        assert {item["session_id"] for item in rows(capsys)[0]["sessions"]} == {"s1", "child"}
        original = asyncio.run(read_session(tmp_path))
        user_leaf = next(
            entry.id for entry in original.entries if entry.message and entry.message.role == "user"
        )
        assert main([*common, "branch", user_leaf, *execution]) == 0
        state = rows(capsys)[0]
        assert state["snapshot"]["leaf_id"] == user_leaf
        assert len(state["snapshot"]["entries"]) == len(original.entries)
        assert main([*common, "run", "continue", *execution, "--output-format", "json"]) == 0
        assert rows(capsys)[0]["outcome"] == "completed" and len(requests) == 2


def test_failed_model_request_has_error_result_and_resume_retains_unknown_usage(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    failure = HTTPResponse(
        b'{"error":{"message":"invalid request","type":"invalid_request_error"}}',
        status=400,
        content_type="application/json",
    )
    with protocol_server([failure, chat_completion()]) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        assert (
            main([*common, "run", "task", *execution, "--create", "--output-format", "stream-json"])
            == 1
        )
        first = rows(capsys)
        assert first[-1]["type"] == "result" and first[-1]["data"]["outcome"] == "model_error"
        assert first[-1]["data"]["usage"]["main_turn"]["tokens"]["input_tokens"] is None
        assert (
            main([*common, "run", "Continue in a new task", *execution, "--output-format", "json"])
            == 0
        )
        resumed = rows(capsys)[0]
        assert resumed["usage"]["session"]["request_count"] == 2
        assert resumed["usage"]["session"]["tokens"]["input_tokens"] is None
        assert resumed["usage"]["session"]["known_tokens"]["input_tokens"] == 20
        assert resumed["usage"]["main_turn"]["request_count"] == 1
        assert len(requests) == 2


def test_compact_cli_restarts_from_native_summary_and_preserves_all_original_records(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    asyncio.run(seed(AppConfig(tmp_path, tmp_path / "data", memory_mode="off")))
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        assert main([*common, "compact", *execution, "--keep-recent-tokens", "1500"]) == 0
        report = rows(capsys)[0]
        assert report["applied_job_id"] is not None
        assert main([*common, "run", "continue", *execution, "--output-format", "json"]) == 0
        final = rows(capsys)[0]
        assert final["usage"]["main_turn"]["request_count"] == 1
        assert final["usage"]["session"]["request_count"] == 2
        assert final["usage"]["by_purpose"]["summary"]["request_count"] == 1
        assert not any(
            "old task 0" in str(message.get("content", ""))
            for message in requests[1]["body"]["messages"]
        )
        assert any(
            "old task 4" in str(message.get("content", ""))
            for message in requests[1]["body"]["messages"]
        )
        assert any(
            entry.message and entry.message.id == "u0"
            for entry in asyncio.run(read_session(tmp_path)).entries
        )


@pytest.mark.parametrize("mode", ["dialogue", "code", "both"])
def test_revert_cli_applies_explicit_mode_then_continues(tmp_path, monkeypatch, capsys, mode):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    root = repository(tmp_path)
    (root / "file.txt").write_text("old", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    with protocol_server(
        [command_reply("printf new > file.txt"), chat_completion(), chat_completion()]
    ) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url), root)

        async def create_effect():
            session = await compose_application(
                AppConfig(root, tmp_path / "data", memory_mode="off"), (provider(url),)
            ).open_session("s1", create=True, refresh_models=False)
            try:
                waiting = await session.run(Message("first", "user", "change"))
                result = await session.resume(
                    {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
                )
                assert result.outcome == "completed"
            finally:
                await session.aclose()

        asyncio.run(create_effect())
        snapshot = asyncio.run(read_session(tmp_path, workspace=root))
        target = next(
            entry.id for entry in snapshot.entries if entry.message and entry.message.role == "user"
        )
        assert main([*common, "revert", *execution, target, "--mode", mode]) == 0
        rows(capsys)
        assert (root / "file.txt").read_text(encoding="utf-8") == (
            "new" if mode == "dialogue" else "old"
        )
        assert main([*common, "run", "continue", *execution, "--output-format", "json"]) == 0
        assert rows(capsys)[0]["outcome"] == "completed" and len(requests) == 3


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
