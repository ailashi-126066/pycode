import pytest
from textual.widgets import Button, Checkbox, Input, Select, TabbedContent

from pycode.app import AppConfig, compose_application
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.tools.test_questions import question_response
from tests.ui.test_tui_http import settled


async def test_actual_tui_named_question_tabs_other_checkbox_review_and_submit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    questions = {
        "questions": [
            {"type": "text", "name": "language", "title": "Language", "message": "Which language?"},
            {"type": "radio", "name": "style", "message": "Style?", "options": ["A", "B"]},
            {
                "type": "select",
                "name": "target",
                "message": "Target?",
                "options": ["local", "remote"],
            },
            {
                "type": "checkbox",
                "name": "features",
                "message": "Features?",
                "options": [
                    {"value": "tests", "label": "Tests", "description": "Run tests"},
                    "docs",
                ],
            },
        ]
    }
    with protocol_server([question_response(questions), chat_completion()]) as (url, requests):
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).tui()
        async with app.run_test(size=(120, 50)) as pilot:
            await app.ready.wait()
            await app.send_text("ask")
            await settled(app)
            await pilot.pause()
            assert app.query("#askuser-inline")
            assert app.query_one("#question-submit", Button).disabled and len(requests) == 1
            app.query_one("#answer-0", Input).value = "Python"
            app.query_one("#other-1", Input).value = "custom style"
            app.query_one("#answer-2", Select).value = "local"
            app.query_one("#choice-3-0", Checkbox).value = True
            app.query_one("#choice-3-1", Checkbox).value = True
            app.query_one("#question-tabs", TabbedContent).active = "question-3"
            await pilot.pause()
            await pilot.click("#question-check")
            await pilot.pause()
            assert not app.query_one("#question-submit", Button).disabled
            assert len(requests) == 1
            await pilot.click("#question-submit")
            await settled(app)
            if app.last_error is not None:
                raise app.last_error
            assert len(requests) == 2 and not app.query("#askuser-inline")
            results = [
                item["tool_result"]
                for item in app.state["context"]["messages"]
                if item["tool_result"]
            ]
            assert len(results) == 1
            assert (
                "language: Python" in results[0]["text"]
                and "features: tests, docs" in results[0]["text"]
            )
            assert "style: custom style" in results[0]["text"]


async def test_tui_question_reopen_has_one_interrupted_pair_and_cannot_answer_old_wait(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    question = {"questions": [{"type": "text", "name": "choice", "message": "Your choice?"}]}
    with protocol_server([question_response(question), chat_completion()]) as (url, requests):
        first = compose_application(config, (provider(url),)).tui()
        async with first.run_test(size=(120, 45)):
            await first.ready.wait()
            await first.send_text("ask")
            await settled(first)
            session_id = first.session_id
            assert first.state["pending"][0]["interrupt_id"]
        app = compose_application(config, (provider(url),)).tui(session_id=session_id)
        async with app.run_test(size=(120, 45)):
            await app.ready.wait()
            assert app.state["pending"] == () and len(requests) == 1
            assert not app.query("#question-inline")
            await app.send_text("Continue")
            await settled(app)
            assert len(requests) == 2
            outputs = [
                item["tool_result"]
                for item in app.state["context"]["messages"]
                if item["tool_result"]
            ]
            assert len(outputs) == 1 and outputs[0]["error_code"] == "tool_interrupted"


async def test_tui_answers_a_subagent_progress_guard_question_in_that_subagent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    question = {
        "kind": "ask_user",
        "operation_id": "child-run:loop-4",
        "questions": [
            {
                "type": "radio",
                "name": "loop",
                "title": "Progress guard",
                "message": "`bash` ran 3 times. Is the agent waiting on purpose?",
                "options": [
                    {"value": "continue", "label": "Continue", "description": ""},
                    {"value": "stop", "label": "Stop (loop_detected)", "description": ""},
                ],
            }
        ],
    }
    child = {"agent_id": "agent-1", "name": "a1", "description": "poll", "pending": [question]}
    with protocol_server([]) as (url, requests):
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).tui()
        async with app.run_test(size=(120, 45)) as pilot:
            await app.ready.wait()
            session = app.active_session
            inspect = session.inspect
            answered = []

            async def waiting_child(**options):
                state = dict(await inspect(**options))
                state["subagents"] = {"approvals": [child]}
                return state

            async def answer_agent(agent_id, answer):
                answered.append((agent_id, answer))
                child["pending"] = []

            monkeypatch.setattr(session, "inspect", waiting_child)
            monkeypatch.setattr(session.subagents, "answer", answer_agent)
            await app.refresh_state()
            await pilot.pause()
            app.query_one("#answer-0", Select).value = "continue"
            await pilot.pause()
            await pilot.click("#question-check")
            await pilot.pause()
            await pilot.click("#question-submit")
            await settled(app)
            assert answered == [
                ("agent-1", {"operation_id": "child-run:loop-4", "answers": {"loop": "continue"}})
            ]
            assert app.last_error is None and requests == []


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
