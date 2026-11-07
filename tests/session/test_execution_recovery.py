"""Persistent execution resumes saved approvals; cancellation ends the old run."""

import asyncio
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy

import pytest

from pycode.app.api import compose_application
from pycode.app.config import AppConfig
from pycode.kernel import EntryDraft, Message, StorageError
from pycode.session.store import StorageLayout
from tests.support.execution import provider, session_at
from tests.support.http_provider import chat_completion, protocol_server
from tests.support.recovery_tools import WritingEcho


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_same_process_resumes_permission_without_reissuing_first_model(
    tmp_path, monkeypatch, decision
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(tool=True), chat_completion()]) as (url, received):
        tool = WritingEcho()
        session = await session_at(config, provider(url), tool, create=True)
        try:
            waiting = await session.run(Message("u1", "user", "write"))
            assert waiting.outcome == "needs_input" and tool.executed == 0
            result = await session.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": decision}
            )
            assert result.outcome == "completed"
            assert tool.executed == (1 if decision == "approve" else 0)
            assert len(received) == 2
            results = [
                entry["message"]["tool_result"]
                for entry in (await session.inspect())["snapshot"]["entries"]
                if entry["message"] and entry["message"]["tool_result"]
            ]
            assert len(results) == 1
            assert results[0]["is_error"] == (decision == "deny")
            assert results[0]["call_id"] == "call-1"
        finally:
            await session.aclose()
    database = StorageLayout(config.workspace, config.user_data).runtime_database
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {"model_requests"}


@pytest.mark.parametrize("legacy", [False, True])
async def test_reopen_resumes_saved_permission_without_reissuing_model(
    tmp_path, monkeypatch, legacy
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(tool=True), chat_completion()]) as (url, received):
        tool = WritingEcho()
        first = await session_at(config, provider(url), tool, create=True)
        waiting = await first.run(Message("u1", "user", "write"))
        if legacy:
            # Simulate a checkpoint saved before execution fields moved into graph state.
            saver = await first.history.checkpoint()
            saved = await saver.aget_tuple({"configurable": {"thread_id": f"s1:{waiting.run_id}"}})
            assert saved is not None
            checkpoint = deepcopy(saved.checkpoint)
            for name in ("phase", "reason", "ended_turn"):
                checkpoint["channel_values"].pop(name, None)
                checkpoint["channel_versions"].pop(name, None)
            await saver.aput(saved.parent_config or saved.config, checkpoint, saved.metadata, {})
        await first.aclose()
        restored = await session_at(config, provider(url), tool)
        try:
            state = await restored.inspect()
            assert state["pending_run_id"] == waiting.run_id
            assert state["pending"] and tool.executed == 0 and len(received) == 1
            result = await restored.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            assert result.outcome == "completed" and result.run_id == waiting.run_id
            assert tool.executed == 1 and len(received) == 2
        finally:
            await restored.aclose()


async def test_invalid_answer_keeps_live_interrupt_and_correct_answer_works(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(tool=True), chat_completion()]) as (url, received):
        tool = WritingEcho()
        session = await session_at(config, provider(url), tool, create=True)
        try:
            waiting = await session.run(Message("u1", "user", "write"))
            with pytest.raises(StorageError):
                await session.resume({"operation_id": "wrong-operation", "decision": "approve"})
            unchanged = await session.resume()
            assert unchanged.pending == waiting.pending and tool.executed == 0
            result = await session.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            assert result.outcome == "completed" and tool.executed == 1 and len(received) == 2
        finally:
            await session.aclose()


async def test_active_writer_cannot_be_mistaken_for_lost_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(tool=True), chat_completion()]) as (url, received):
        tool = WritingEcho()
        first = await session_at(config, provider(url), tool, create=True)
        try:
            waiting = await first.run(Message("u1", "user", "write"))
            with pytest.raises(StorageError) as error:
                await session_at(config, provider(url), tool)
            assert error.value.code == "session_busy"
            assert not any(
                entry["message"] and entry["message"]["tool_result"]
                for entry in (await first.inspect())["snapshot"]["entries"]
            )
            await first.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            assert tool.executed == 1 and len(received) == 2
        finally:
            await first.aclose()


async def test_changed_project_rules_do_not_change_live_approval_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    (tmp_path / "agent.md").write_text("APPROVED_TASK_RULE", encoding="utf-8")
    with protocol_server([chat_completion(tool=True), chat_completion()]) as (url, received):
        tool = WritingEcho()
        session = await session_at(config, provider(url), tool, create=True)
        try:
            waiting = await session.run(Message("u1", "user", "write"))
            (tmp_path / "agent.md").write_text("CHANGED_DISK_RULE", encoding="utf-8")
            await session.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            assert "APPROVED_TASK_RULE" in str(received[-1]["body"]["messages"])
            assert "CHANGED_DISK_RULE" not in str(received[-1]["body"]["messages"])
        finally:
            await session.aclose()


@pytest.mark.parametrize(
    "boundary",
    [
        "model_saved",
        "output_ready",
        "history_saved",
        "effect_written",
        "model_stream",
        "output_published",
        "stream_output_published",
        "model_request_settled",
    ],
)
async def test_real_process_death_preserves_facts_and_never_replays_old_effects(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion(tool=True), chat_completion()]) as (url, received):
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
        config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
        tool = WritingEcho()
        restored = await session_at(config, provider(url), tool)
        try:
            assert tool.executed == 0 and len(received) == 1
            # Reopening never executes work. Explicit cancellation closes a resumable run.
            await restored.cancel()
            state = await restored.inspect()
            assert state["pending"] == () and state["pending_run_id"] is None
            results = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["tool_result"]
            ]
            if boundary in {"model_stream", "model_request_settled"}:
                assert results == []
            elif boundary in {"output_ready", "history_saved"}:
                assert len(results) == 1 and not results[0]["is_error"]
            elif boundary == "model_saved":
                # No tool_started record: the call never ran.
                assert len(results) == 1 and results[0]["error_code"] == "tool_abandoned"
            else:
                assert len(results) == 1 and results[0]["error_code"] == "tool_interrupted"
            with pytest.raises(StorageError):
                await restored.resume()
            assert tool.executed == 0 and len(received) == 1
            await restored.run(Message("u2", "user", "inspect before further changes"))
            assert tool.executed == 0 and len(received) == 2
        finally:
            await restored.aclose()
        reopened = await session_at(config, provider(url), tool)
        try:
            results_again = [
                entry["message"]["tool_result"]
                for entry in (await reopened.inspect())["snapshot"]["entries"]
                if entry["message"] and entry["message"]["tool_result"]
            ]
            assert len(results_again) == len(results)
            assert len(received) == 2 and tool.executed == 0
        finally:
            await reopened.aclose()


async def test_saved_input_without_first_checkpoint_is_not_automatically_scheduled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    store = await compose_application(config).session_repository().open("s1", create=True)
    await store.append_message(Message("u1", "user", "unstarted task"))
    await store.append_entry(
        EntryDraft(
            "custom",
            data={"kind": "run_start", "input_id": "u1", "execution_session_id": "s1"},
            run_id="unstarted-run",
        )
    )
    await store.aclose()
    with protocol_server([chat_completion()]) as (url, received):
        restored = await session_at(config, provider(url), WritingEcho())
        try:
            assert not received
            assert any(
                entry["message"] and entry["message"]["id"] == "u1"
                for entry in (await restored.inspect())["snapshot"]["entries"]
            )
            with pytest.raises(StorageError):
                await restored.resume()
            assert not received
            assert (
                await restored.run(Message("u2", "user", "continue explicitly"))
            ).outcome == "completed"
            assert len(received) == 1
        finally:
            await restored.aclose()


async def test_cancellation_collects_tool_and_marks_unknown_effect_without_repeating(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(tool=True)]) as (url, received):
        tool = WritingEcho()
        tool.release = asyncio.Event()
        session = await session_at(config, provider(url), tool, create=True)
        try:
            waiting = await session.run(Message("u1", "user", "write"))
            running = asyncio.create_task(
                session.resume(
                    {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
                )
            )
            await asyncio.wait_for(tool.started.wait(), 5)
            assert await session.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert not await session.cancel()
            assert tool.executed == 1 and len(received) == 1
            state = await session.inspect()
            assert state["pending"] == () and state["pending_run_id"] is None
            result = next(
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["tool_result"]
            )
            assert result["error_code"] == "tool_interrupted"
            assert (tmp_path / "effect.txt").read_text() == "wire"
        finally:
            await session.aclose()


async def test_independent_sessions_can_execute_concurrently_with_shared_request_database(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(), chat_completion()]) as (url, received):
        sessions = []
        for identifier in ("s1", "s2"):
            sessions.append(
                await compose_application(config, (provider(url),)).open_session(
                    identifier, create=True, refresh_models=False
                )
            )
        try:
            results = await asyncio.gather(
                *(session.run(Message("u1", "user", "task")) for session in sessions)
            )
            assert all(result.outcome == "completed" for result in results)
            assert len(received) == 2
        finally:
            for session in sessions:
                await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")


async def test_process_death_then_resume_does_not_repeat_completed_external_effect(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion(tool=True), chat_completion()]) as (url, received):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.crash_worker",
            str(tmp_path),
            url,
            "effect_written",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            assert child.returncode == 73, (stdout, stderr)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        tool = WritingEcho()
        config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
        restored = await session_at(config, provider(url), tool)
        try:
            before = (tmp_path / "effect.txt").read_text()
            result = await restored.resume()
            assert result.outcome == "completed"
            assert tool.executed == 0 and (tmp_path / "effect.txt").read_text() == before
            assert len(received) == 2
        finally:
            await restored.aclose()
