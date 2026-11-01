import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel.domain.errors import SnapshotError, StorageError
from pycode.kernel.domain.messages import Message
from pycode.session.adapters.storage.layout import StorageLayout
from tests.snapshot.adapters.test_git_tree import git, repository
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def command_reply(command):
    reply = chat_completion(tool=True)
    reply.body = (
        reply.body.decode()
        .replace('"name": "echo"', '"name": "bash"')
        .replace(
            json.dumps('{"value":"wire"}'),
            json.dumps(json.dumps({"command": command})),
        )
        .encode()
    )
    return reply


@pytest.mark.parametrize("mode", ["dialogue", "code", "both"])
async def test_actual_bash_snapshots_and_revert_modes_continue_through_shared_application(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    workspace = repository(tmp_path)
    (workspace / "tracked.txt").write_text("original")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "initial")
    index = (workspace / ".git/index").read_bytes()
    config = AppConfig(workspace, tmp_path / "data", memory_mode="off")
    with protocol_server(
        [
            command_reply("printf changed > tracked.txt; printf new > created.txt"),
            chat_completion(),
            chat_completion(),
        ]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        pending = await session.run(Message("u1", "user", "change files"))
        assert (
            await session.resume(
                {"operation_id": pending.pending[0]["operation_id"], "decision": "approve"}
            )
        ).outcome == "completed"
        reader = await compose_application(config).session_repository().open("s1")
        snapshot = await reader.snapshot()
        user = next(
            entry.id
            for entry in snapshot.entries
            if entry.type == "message" and entry.message.id == "u1"
        )
        boundary = next(
            entry for entry in snapshot.entries if entry.data.get("kind") == "file_snapshot"
        )
        assert set(boundary.data["paths"]) == {"tracked.txt", "created.txt"}
        assert boundary.run_id is not None and boundary.operation_id is not None
        assert snapshot.entries[-1].data["checkpoint_id"] is not None
        await reader.aclose()
        plan = await session.preview_revert(user, mode)
        if mode != "dialogue":
            assert plan.files is not None and not plan.files.conflicts
        await session.revert(plan)
        if mode == "dialogue":
            assert (workspace / "tracked.txt").read_text() == "changed" and (
                workspace / "created.txt"
            ).exists()
        else:
            assert (workspace / "tracked.txt").read_text() == "original" and not (
                workspace / "created.txt"
            ).exists()
        assert (workspace / ".git/index").read_bytes() == index
        assert (
            await session.run(Message("u2", "user", "continue after revert"))
        ).outcome == "completed"
        assert len(requests) == 3
        await session.aclose()
        reader = await compose_application(config).session_repository().open("s1")
        after = await reader.snapshot()
        projection = ContextProjector().transform_context(after)
        assert any(entry.message and entry.message.role == "tool_result" for entry in after.entries)
        assert any(message.role == "tool_result" for message in projection.messages) == (
            mode == "code"
        )
        await reader.aclose()


async def test_external_change_is_visible_in_preview_and_rejected_without_file_loss(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    root = repository(tmp_path)
    (root / "file.txt").write_text("original")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    config = AppConfig(root, tmp_path / "data", memory_mode="off")
    with protocol_server([command_reply("printf changed > file.txt"), chat_completion()]) as (
        url,
        _,
    ):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        pending = await session.run(Message("u1", "user", "change"))
        await session.resume(
            {"operation_id": pending.pending[0]["operation_id"], "decision": "approve"}
        )
        reader = await compose_application(config).session_repository().open("s1")
        leaf = next(
            entry.id
            for entry in (await reader.snapshot()).entries
            if entry.message and entry.message.id == "u1"
        )
        await reader.aclose()
        (root / "file.txt").write_text("external")
        plan = await session.preview_revert(leaf, "both")
        assert plan.files.conflicts == ("file.txt",)
        with pytest.raises(SnapshotError) as error:
            await session.revert(plan)
        assert (
            error.value.code == "restore_conflict" and (root / "file.txt").read_text() == "external"
        )
        await session.aclose()


@pytest.mark.parametrize(
    "mode,stage",
    [
        ("dialogue", "after-branch"),
        ("code", "after-reminder"),
        ("both", "after-branch"),
    ],
)
async def test_public_session_recovers_crashed_revert_and_continues_real_http_execution(
    tmp_path,
    monkeypatch,
    mode,
    stage,
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    root = repository(tmp_path)
    (root / "tracked.txt").write_text("original", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    config = AppConfig(root, tmp_path / "data", memory_mode="off")
    layout = StorageLayout(root, config.user_data)
    with protocol_server(
        [
            command_reply("printf changed > tracked.txt"),
            chat_completion(),
            chat_completion(),
        ]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        pending = await session.run(Message("u1", "user", "change files"))
        await session.resume(
            {
                "operation_id": pending.pending[0]["operation_id"],
                "decision": "approve",
            }
        )
        reader = await compose_application(config).session_repository().open("s1")
        try:
            target = next(
                entry.id
                for entry in (await reader.snapshot()).entries
                if entry.message and entry.message.id == "u1"
            )
        finally:
            await reader.aclose()
        plan = await session.preview_revert(target, mode)
        await session.aclose()
        plan_path = tmp_path / "revert.json"
        plan_path.write_text(json.dumps(asdict(plan)), encoding="utf-8")
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.revert_crash_worker",
            str(root),
            str(layout.project / "snapshots"),
            str(layout.sessions / "s1"),
            str(plan_path),
            stage,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            assert child.returncode == 76, (stdout.decode(), stderr.decode())
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", refresh_models=False
        )
        try:
            with pytest.raises(StorageError) as error:
                await session.run(Message("u2", "user", "continue"))
            assert error.value.code == "revert_recovery_required"
            assert len(requests) == 2
            await session.recover_revert(plan.transaction_id)
            assert (await session.run(Message("u2", "user", "continue"))).outcome == "completed"
            await session.recover_revert(plan.transaction_id)
            assert len(requests) == 3
            assert (root / "tracked.txt").read_text(encoding="utf-8") == (
                "changed" if mode == "dialogue" else "original"
            )
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
