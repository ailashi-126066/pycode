import json

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.snapshot.test_git_tree import git, repository
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def reply(name, arguments):
    response = chat_completion(tool=True)
    response.body = (
        response.body.decode()
        .replace('"name": "echo"', json.dumps("name") + ": " + json.dumps(name))
        .replace(
            json.dumps('{"value":"wire"}'),
            json.dumps(json.dumps(arguments)),
        )
        .encode()
    )
    return response


async def test_read_receipt_survives_approval_restart_and_exact_edit_gets_snapshot_boundary(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    root = repository(tmp_path)
    (root / "file.txt").write_text("before target after", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    config = AppConfig(root, tmp_path / "data", memory_mode="off")
    responses = [
        reply("read", {"path": "/workspace/file.txt"}),
        reply(
            "edit",
            {
                "path": "/workspace/file.txt",
                "edits": [{"oldText": "target", "newText": "changed"}],
            },
        ),
        chat_completion(),
    ]
    with protocol_server(responses) as (url, requests):
        selected = provider(url)
        session = await compose_application(config, (selected,)).open_session(
            "s1", create=True, refresh_models=False
        )
        pending = await session.run(Message("u1", "user", "edit the file"))
        assert pending.outcome == "needs_input" and pending.pending[0]["call"]["name"] == "edit"
        restored = session
        assert (
            await restored.resume(
                {"operation_id": pending.pending[0]["operation_id"], "decision": "approve"}
            )
        ).outcome == "completed"
        assert len(requests) == 3 and (root / "file.txt").read_text() == "before changed after"
        await restored.aclose()
        store = await compose_application(config).session_repository().open("s1")
        snapshot = await store.snapshot()
        boundaries = [
            entry.message.tool_result.details["file_snapshot"]
            for entry in snapshot.entries
            if entry.message is not None
            and entry.message.tool_result is not None
            and "file_snapshot" in entry.message.tool_result.details
        ]
        assert len(boundaries) == 1 and boundaries[0]["paths"] == ("file.txt",)
        assert boundaries[0]["before"] != boundaries[0]["after"]
        await store.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
