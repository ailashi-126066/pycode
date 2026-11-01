"""Textual Pilot exercises the actual Plan interrupt and automatic execution."""

import pytest
from textual.widgets import Static

from pycode.app import AppConfig, compose_application
from tests.plans.test_plans import fix_plan_path, write_plan_reply
from tests.skills.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.ui.test_tui_http import settled


async def test_tui_plan_inline_review_drives_same_live_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    write = write_plan_reply()
    replies = [
        write,
        skill_reply("ExitPlanMode", {}, "exit"),
        skill_reply("bash", {"command": "printf implemented > done.txt"}, "run"),
        chat_completion(),
        # The approved board is still active: the one end check asks again.
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).tui()
        async with app.run_test(size=(120, 50)) as pilot:
            await app.ready.wait()
            plan = await app.session.plans.enter()
            fix_plan_path(write, plan.plan_id)
            await app.send_text("create plan")
            await settled(app)
            await pilot.pause()
            assert app.query("#plan-inline") and len(requests) == 2
            assert str(app.query_one("#plan-layers", Static).render()) == "分层：1) a, c  2) b"
            assert str(app.query_one("#plan-critical-path", Static).render()) == "关键路径：a → b"
            assert not (tmp_path / "done.txt").exists()
            await pilot.click("#plan-auto")
            await settled(app)
            assert (tmp_path / "done.txt").read_text() == "implemented"
            assert len(requests) == 5 and not app.query("#plan-inline")
            assert app.state["plan"]["mode"] == "completed"


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
