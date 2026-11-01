import pytest

from pycode.app import AppConfig, compose_application
from tests.skills.test_skills import write_skill
from tests.skills.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.ui.test_tui_http import settled


async def test_tui_skill_arguments_reload_and_new_session_do_not_leak_activation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    root = config.user_data / "skills"
    await write_skill(root)
    replies = [
        skill_reply("read", {"path": "/skills/user/example/SKILL.md"}, "old-read"),
        chat_completion(),
        chat_completion(),
        skill_reply("read", {"path": "/skills/user/example/SKILL.md"}, "new-read"),
        chat_completion(),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 50)):
            await app.ready.wait()
            await app.command('/skill:example keep "quotes"')
            await settled(app)
            assert app.last_error is None
            assert any(
                message["tool_result"]
                and "Instructions $ARGUMENTS" in message["tool_result"]["text"]
                for message in app.state["context"]["messages"]
            )
            assert 'keep "quotes"' in str(requests[0]["body"]["messages"])
            await app.send_text("task")
            await settled(app)
            await write_skill(root, body="Updated $ARGUMENTS")
            await app.command("/reload")
            await settled(app)
            await app.command("/skill:example new")
            await settled(app)
            assert any(
                message["tool_result"] and "Updated $ARGUMENTS" in message["tool_result"]["text"]
                for message in app.state["context"]["messages"]
            )
            await app.command("/new")
            await app.send_text("independent")
            await settled(app)
            assert len(requests) == 6
            assert not any(
                "Updated $ARGUMENTS" in str(item["content"])
                for item in requests[-1]["body"]["messages"]
            )


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
