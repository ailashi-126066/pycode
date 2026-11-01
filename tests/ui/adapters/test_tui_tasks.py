"""/tasks, /tasks graph, /tasks clear and /agents in the actual TUI."""

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.ui.adapters.test_tui_http import settled

STEPS = [
    {"id": "s1", "title": "locate", "depends_on": [], "acceptance": ["found the bug"]},
    {"id": "s2", "title": "fix", "depends_on": ["s1"]},
    {"id": "s3", "title": "docs", "depends_on": []},
]


def notice(app):
    return str(app.query_one("#notice").render())


async def test_tasks_show_evidence_graph_and_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    write = {"goal": "fix login", "done_when": ["tests pass"], "steps": STEPS}
    replies = [
        skill_reply("TaskWrite", write, "tw"),
        skill_reply("TaskUpdate", {"target": "s1", "status": "in_progress"}, "u1"),
        skill_reply("ls", {"path": "."}, "look"),
        skill_reply(
            "TaskUpdate", {"target": "s1", "status": "completed", "evidence_refs": ["look"]}, "u2"
        ),
        chat_completion(),
        chat_completion(),
    ]
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server(replies) as (url, _requests):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 50)):
            await app.ready.wait()
            await app.command("/tasks")
            assert notice(app) == "没有任务板"
            await app.send_text("fix login")
            await settled(app)
            await app.command("/tasks")
            text = notice(app)
            assert "- s1 [completed] locate" in text and "s1 evidence: look ls" in text
            await app.command("/tasks graph")
            text = notice(app)
            assert "1) s1, s3" in text and "2) s2" in text
            assert "Critical path: s1 -> s2" in text and "s2 <- s1" in text
            await app.command("/tasks clear")
            await settled(app)
            if app.last_error:
                raise app.last_error
            await app.command("/tasks")
            assert "[abandoned]" in notice(app)
            await app.command("/agents")
            assert app.query("#agents-panel")
