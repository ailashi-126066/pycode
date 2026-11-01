"""The live panel lists memory files; edit and delete need no approval or model call."""

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.memory.domain.memory import format_note
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.ui.adapters.test_tui_http import settled

UPDATED = "2026-01-01T00:00:00+00:00"


async def test_tui_memory_panel_edit_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion()]) as (url, requests):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 65)):
            await app.ready.wait()
            await app.send_text("Keep functions readable.")
            await settled(app)
            root = config.user_data / "memory"
            root.mkdir(parents=True, exist_ok=True)
            (root / "style.md").write_text(format_note("Code style", UPDATED, "Readable."))
            await app.command("/memory")
            await settled(app)
            assert app.query("#memory-panel")
            await app.command("/memory edit style Keep functions readable and explicit.")
            await settled(app)
            if app.last_error:
                raise app.last_error
            records = (await app.session.memory_list())["records"]
            assert [(r["memory_id"], r["scope"], r["description"]) for r in records] == [
                ("style", "user", "Code style")
            ]
            assert records[0]["content"] == "Keep functions readable and explicit."
            await app.command("/memory delete style")
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert not (root / "style.md").exists()
            assert len(requests) == 1


async def test_tui_memory_edit_and_delete_can_disambiguate_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off", project_trusted=True)
    user, project = config.user_data / "memory", tmp_path / ".pycode" / "memory"
    for root, text in ((user, "user preference"), (project, "project preference")):
        root.mkdir(parents=True)
        (root / "same.md").write_text(format_note("Preference", UPDATED, text))
    with protocol_server([]) as (url, requests):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 65)):
            await app.ready.wait()
            await app.command("/memory edit same changed project preference --scope project")
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert (project / "same.md").read_text().endswith("changed project preference\n")
            assert (user / "same.md").read_text().endswith("user preference\n")
            await app.command("/memory delete same --scope project")
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert not (project / "same.md").exists() and (user / "same.md").exists()
        assert not requests


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
