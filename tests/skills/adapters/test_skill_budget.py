"""An oversized catalogue discloses selected paths through native middleware only."""

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.skills.adapters.test_skills_http import skill_reply
from tests.skills.services.test_skills import write_skill
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_skill_search_bounds_native_catalogue_and_leaves_unselected_bodies_hidden(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off", skill_index_budget=1)
    await write_skill(config.user_data / "skills", "selected", body="SELECTED PRIVATE BODY")
    await write_skill(config.user_data / "skills", "unselected", body="UNSELECTED PRIVATE BODY")
    replies = [
        skill_reply("SkillSearch", {"query": "selected", "limit": 1}, "search"),
        skill_reply("read", {"path": "/skills/user/selected/SKILL.md"}, "read"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            done = await session.run(Message("u1", "user", "Find a relevant Skill"))
            assert done.outcome == "completed" and len(requests) == 3
            initial = str(requests[0]["body"]["messages"])
            assert (
                "selected: Example task" not in initial
                and "unselected: Example task" not in initial
            )
            second = str(requests[1]["body"]["messages"])
            assert "/skills/user/selected/SKILL.md" in second
            assert "SELECTED PRIVATE BODY" not in second
            assert "UNSELECTED PRIVATE BODY" not in str(requests)
            assert "SELECTED PRIVATE BODY" in str(requests[2]["body"]["messages"])
            state = await session.inspect()
            loads = [
                entry["message"]["tool_result"]["details"]["skill"]["name"]
                for entry in state["snapshot"]["entries"]
                if entry["message"]
                and entry["message"]["tool_result"]
                and "skill" in entry["message"]["tool_result"]["details"]
            ]
            assert loads == ["selected"]
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
