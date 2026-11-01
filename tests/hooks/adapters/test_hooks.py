import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.hooks.types import HOOK_EVENTS
from pycode.kernel.domain.errors import StorageError
from pycode.kernel.domain.messages import Message
from tests.context.adapters.test_summary_http import seed
from tests.session.adapters.test_revert_http import command_reply
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def hook_file(config, hooks):
    def write():
        config.user_data.mkdir(parents=True, exist_ok=True)
        (config.user_data / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    await asyncio.to_thread(write)


async def open_session(config, url, *, create=False):
    return await compose_application(config, (provider(url),)).open_session(
        "s1", create=create, refresh_models=False
    )


async def test_prompt_action_appends_context_and_notification_before_actual_request(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "rule",
                "event": "pre_send",
                "action": {"type": "prompt", "message": "Preserve the Hook constraint"},
            }
        ],
    )
    with protocol_server([chat_completion()]) as (url, requests):
        session = await open_session(config, url, create=True)
        try:
            assert (await session.run(Message("u1", "user", "task"))).outcome == "completed"
            assert any(
                "Preserve the Hook constraint" in str(item["content"])
                for item in requests[0]["body"]["messages"]
            )
            entries = (await session.inspect())["snapshot"]["entries"]
            assert len([entry for entry in entries if entry["type"] == "hook_context"]) == 1
            assert any(entry["type"] == "hook_result" for entry in entries)
        finally:
            await session.aclose()


async def test_pre_tool_rejection_is_a_paired_error_without_approval_or_effect(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "protect",
                "event": "pre_tool_use",
                "if": 'tool == "bash"',
                "reject": True,
                "action": {"type": "prompt", "message": "Blocked command"},
            }
        ],
    )
    with protocol_server([command_reply("printf bad > effect.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        session = await open_session(config, url, create=True)
        try:
            result = await session.run(Message("u1", "user", "run command"))
            assert result.outcome == "completed" and len(requests) == 2
            assert not (tmp_path / "effect.txt").exists()
            state = await session.inspect()
            results = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "tool_result"
            ]
            assert len(results) == 1 and results[0]["error_code"] == "hook_rejected"
        finally:
            await session.aclose()


async def test_command_once_persists_and_cannot_approve_parent_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "mark",
                "event": "pre_tool_use",
                "once": True,
                "action": {"type": "command", "command": "printf hook >> hook.txt"},
            }
        ],
    )
    with protocol_server([command_reply("printf parent > parent.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        session = await open_session(config, url, create=True)
        waiting = await session.run(Message("u1", "user", "command"))
        assert waiting.outcome == "needs_input" and (tmp_path / "hook.txt").read_text() == "hook"
        assert not (tmp_path / "parent.txt").exists() and len(requests) == 1
        await session.aclose()
        session = await open_session(config, url)
        try:
            result = await session.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            assert result.outcome == "completed"
            assert (tmp_path / "hook.txt").read_text() == "hook"
            assert (tmp_path / "parent.txt").read_text() == "parent" and len(requests) == 2
        finally:
            await session.aclose()


async def test_hook_configuration_change_cannot_bypass_pending_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    original = [
        {"id": "mark", "event": "pre_tool_use", "action": {"type": "prompt", "message": "original"}}
    ]
    await hook_file(config, original)
    with protocol_server([command_reply("printf parent > parent.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        session = await open_session(config, url, create=True)
        await session.run(Message("u1", "user", "command"))
        await session.aclose()
        await hook_file(
            config, [{**original[0], "action": {"type": "prompt", "message": "changed"}}]
        )
        with pytest.raises(StorageError, match="Hook configuration changed"):
            await open_session(config, url)
        assert len(requests) == 1 and not (tmp_path / "parent.txt").exists()


async def test_startup_and_shutdown_run_at_open_close_without_user_input(tmp_path):
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "boot",
                "event": "startup",
                "action": {"type": "command", "command": "printf start >> life.txt"},
            },
            {
                "id": "close",
                "event": "shutdown",
                "action": {"type": "command", "command": "printf end >> life.txt"},
            },
        ],
    )
    session = await open_session(config, "http://127.0.0.1:1", create=True)
    assert (tmp_path / "life.txt").read_text() == "start"
    await session.aclose()
    assert (tmp_path / "life.txt").read_text() == "startend"
    session = await open_session(config, "http://127.0.0.1:1", create=True)
    await session.aclose()
    assert (tmp_path / "life.txt").read_text() == "startendstartend"


async def test_startup_once_result_survives_close_before_first_user(tmp_path):
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "boot",
                "event": "startup",
                "once": True,
                "action": {"type": "command", "command": "printf once >> boot.txt"},
            }
        ],
    )
    for _ in range(2):
        session = await open_session(config, "http://127.0.0.1:1", create=True)
        await session.aclose()
    assert (tmp_path / "boot.txt").read_text() == "once"


async def test_synchronous_hook_blocks_and_cancellation_stops_process(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "later",
                "event": "post_receive",
                "action": {
                    "type": "command",
                    "command": "printf started > started.txt; sleep 30; printf bad > late.txt",
                },
            }
        ],
    )
    with protocol_server([chat_completion()]) as (url, requests):
        session = await open_session(config, url, create=True)
        running = asyncio.create_task(session.run(Message("u1", "user", "first")))
        try:
            async with asyncio.timeout(5):
                while not await asyncio.to_thread((tmp_path / "started.txt").exists):  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
                assert not running.done()
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            assert len(requests) == 1 and not (tmp_path / "late.txt").exists()
            state = await session.inspect()
            assert any(entry["type"] == "hook_call" for entry in state["snapshot"]["entries"])
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await session.aclose()
        assert not (tmp_path / "late.txt").exists()


async def test_real_exit_after_hook_command_reuses_confirmed_effect_then_asks_parent_approval(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "mark",
                "event": "pre_tool_use",
                "action": {"type": "command", "command": "printf hook >> hook.txt"},
            }
        ],
    )
    with protocol_server([command_reply("printf parent > parent.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        request = {
            "workspace": str(tmp_path),
            "data": str(config.user_data),
            "provider": asdict(provider(url)),
        }
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "tests.support.hook_crash_worker"],
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=30,
            env=dict(os.environ),
        )
        assert child.returncode == 81, child.stderr.decode(errors="replace")
        assert (tmp_path / "hook.txt").read_text() == "hook" and not (
            tmp_path / "parent.txt"
        ).exists()
        session = await open_session(config, url)
        try:
            await session.cancel()
            assert (await session.inspect())["pending"] == ()
            with pytest.raises(StorageError):
                await session.resume()
            assert (tmp_path / "hook.txt").read_text() == "hook" and len(requests) == 1
            assert not (tmp_path / "parent.txt").exists()
        finally:
            await session.aclose()


async def test_project_hooks_require_trust_and_same_id_overrides_in_config_order(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [{"id": "rule", "event": "pre_send", "action": {"type": "prompt", "message": "USER_RULE"}}],
    )

    def project():
        directory = config.workspace / ".pycode"
        directory.mkdir()
        (directory / "hooks.json").write_text(
            json.dumps(
                [
                    {
                        "id": "rule",
                        "event": "pre_send",
                        "action": {"type": "prompt", "message": "PROJECT_RULE"},
                    }
                ]
            ),
            encoding="utf-8",
        )

    await asyncio.to_thread(project)
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        for index, trusted in enumerate((False, True)):
            session = await compose_application(
                replace(config, project_trusted=trusted), (provider(url),)
            ).open_session(f"s{index}", create=True, refresh_models=False)
            try:
                await session.run(Message("user", "user", "task"))
            finally:
                await session.aclose()
        assert "USER_RULE" in str(requests[0]["body"]["messages"]) and "PROJECT_RULE" not in str(
            requests[0]["body"]["messages"]
        )
        assert "PROJECT_RULE" in str(requests[1]["body"]["messages"]) and "USER_RULE" not in str(
            requests[1]["body"]["messages"]
        )


async def test_all_fifteen_events_fire_through_real_lifecycle_tools_error_and_compaction(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path,
        tmp_path / "data",
        summary_keep_recent_tokens=1500,
        memory_mode="off",
    )
    await seed(config)
    await hook_file(
        config,
        [
            {"id": name, "event": name, "action": {"type": "prompt", "message": "event:" + name}}
            for name in sorted(HOOK_EVENTS)
        ],
    )
    replies = [
        skill_reply("write", {"path": "/workspace/created.txt", "content": "created"}, "write"),
        command_reply("printf okay"),
        skill_reply("unknown", {}, "unknown"),
        chat_completion(),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await open_session(config, url)
        try:
            waiting = await session.run(Message("new", "user", "write"))
            assert waiting.outcome == "needs_input"
            result = await session.resume(
                {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
            )
            assert result.outcome == "needs_input"
            result = await session.resume(
                {"operation_id": result.pending[0]["operation_id"], "decision": "approve"}
            )
            assert (
                result.outcome == "completed"
                and (tmp_path / "created.txt").read_text() == "created"
            )
            assert (await session.compact()).applied_job_id
        finally:
            await session.aclose()
        view = await compose_application(config).open_view("s1")
        try:
            entries = (await view.inspect())["snapshot"]["entries"]
            observed = {
                entry["data"]["event"] for entry in entries if entry["type"] == "hook_result"
            }
            assert observed == HOOK_EVENTS
        finally:
            await view.aclose()
        assert len(requests) == 5


async def test_failed_check_is_recorded_and_reported_without_aborting_model(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "check",
                "event": "pre_send",
                "once": True,
                "action": {
                    "type": "command",
                    "command": "printf x >> attempts.txt; printf CHECK_FAILED; exit 2",
                },
            }
        ],
    )
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        session = await open_session(config, url, create=True)
        try:
            assert (await session.run(Message("u1", "user", "inspect"))).outcome == "completed"
            assert "CHECK_FAILED" in str(requests[0]["body"]["messages"])
            assert (await session.run(Message("u2", "user", "continue"))).outcome == "completed"
            assert (tmp_path / "attempts.txt").read_text() == "x"
            results = [
                entry
                for entry in (await session.inspect())["snapshot"]["entries"]
                if entry["type"] == "hook_result"
            ]
            assert len(results) == 1 and results[0]["data"]["result"]["is_error"]
        finally:
            await session.aclose()


async def test_failed_pre_tool_check_blocks_only_the_requested_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "check",
                "event": "pre_tool_use",
                "action": {"type": "command", "command": "printf CHECK_FAILED; exit 1"},
            }
        ],
    )
    with protocol_server([command_reply("printf bad > effect.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        session = await open_session(config, url, create=True)
        try:
            assert (await session.run(Message("u", "user", "run"))).outcome == "completed"
            assert not (tmp_path / "effect.txt").exists()
            state = await session.inspect()
            assert not state["pending"] and len(requests) == 2
            results = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "tool_result"
            ]
            assert results[0]["error_code"] == "hook_rejected"
        finally:
            await session.aclose()


async def test_interrupted_once_check_never_replays_or_blocks_reopened_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "check",
                "event": "post_receive",
                "once": True,
                "action": {"type": "command", "command": "printf x >> attempts.txt; sleep 30"},
            }
        ],
    )
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        session = await open_session(config, url, create=True)
        running = asyncio.create_task(session.run(Message("u1", "user", "inspect")))
        try:
            async with asyncio.timeout(5):
                while not await asyncio.to_thread((tmp_path / "attempts.txt").exists):  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await session.aclose()
        session = await open_session(config, url)
        try:
            assert (await session.run(Message("u2", "user", "continue"))).outcome == "completed"
            assert len(requests) == 2 and (tmp_path / "attempts.txt").read_text() == "x"
            state = await session.inspect()
            assert not state["pending"]
            records = [e for e in state["snapshot"]["entries"] if e["type"] == "hook_result"]
            assert len(records) == 1
            assert records[0]["data"]["result"]["error_code"] == "hook_interrupted"
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
