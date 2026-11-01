"""Project instructions reach the real public runtime once per fresh task."""

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_rules_change_only_at_new_tasks_and_snapshots_are_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    (tmp_path / "agent.md").write_text("FIRST_PROJECT_RULE", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("LOWER_PRIORITY_RULE", encoding="utf-8")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(), chat_completion(), chat_completion()]) as (
        url,
        requests,
    ):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            await session.run(Message("u1", "user", "first"))
            await session.run(Message("u2", "user", "second"))
            state = await session.inspect()
            snapshots = [
                entry for entry in state["snapshot"]["entries"] if entry["type"] == "system_prompt"
            ]
            assert len(snapshots) == 1
            for request in requests:
                content = str(request["body"]["messages"])
                assert content.count("FIRST_PROJECT_RULE") == 1
                assert "LOWER_PRIORITY_RULE" not in content
            (tmp_path / "agent.md").write_text("NEW_PROJECT_RULE", encoding="utf-8")
            await session.run(Message("u3", "user", "third"))
            content = str(requests[-1]["body"]["messages"])
            assert content.count("NEW_PROJECT_RULE") == 1
            assert "FIRST_PROJECT_RULE" not in content
            state = await session.inspect()
            assert (
                len(
                    [
                        entry
                        for entry in state["snapshot"]["entries"]
                        if entry["type"] == "system_prompt"
                    ]
                )
                == 2
            )
            assert all(
                item["message"]["role"] != "system"
                for item in state["history"]
                if item["kind"] == "message"
            )
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
