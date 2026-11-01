from dataclasses import replace

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import StorageError
from pycode.ui.tui import content_text
from tests.context.test_summary_http import seed
from tests.snapshot.test_git_tree import git, repository
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, command_reply, protocol_server
from tests.ui.test_tui_http import settled


@pytest.mark.parametrize("mode", ["dialogue", "code", "both"])
async def test_tui_revert_modes_display_preview_then_require_explicit_apply_and_continue(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    root = repository(tmp_path)
    (root / "file").write_text("old", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    config = AppConfig(root, tmp_path / "data", memory_mode="off")
    with protocol_server(
        [command_reply("printf new > file"), chat_completion(), chat_completion()]
    ) as (url, requests):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 50)) as pilot:
            await app.ready.wait()
            await app.send_text("change")
            await settled(app)
            await pilot.press("enter")
            await settled(app)
            target = next(
                entry["id"]
                for entry in app.state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "user"
            )
            await app.command(f"/revert {target} {mode}")
            await settled(app)
            assert (
                app.query("#revert-inline") and (root / "file").read_text(encoding="utf-8") == "new"
            )
            await pilot.click("#revert-confirm")
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert not app.query("#revert-inline")
            assert (root / "file").read_text(encoding="utf-8") == (
                "new" if mode == "dialogue" else "old"
            )
            await app.send_text("continue")
            await settled(app)
            assert app.state["pending_run_id"] is None and len(requests) == 3


async def test_tui_model_command_fork_and_invalid_open_preserve_selected_native_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        first = provider(url)
        second = replace(first, name="second", model_id="second-model")
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (first, second)
        ).tui()
        async with app.run_test(size=(120, 50)):
            await app.ready.wait()
            await app.command("/model second second-model")
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert app.state["context"]["selection"]["provider"] == "second"
            await app.send_text("first")
            await settled(app)
            original = app.session_id
            with pytest.raises(StorageError):
                await app.command("/session open missing")
            assert (
                app.session_id == original
                and content_text((await app.session.inspect())["context"]["messages"][-1], "text")
                == "done"
            )
            await app.command("/session fork child")
            assert (
                app.session_id == "child"
                and app.state["snapshot"]["header"]["parent_session_id"] == original
            )
            await app.send_text("second")
            await settled(app)
            assert len(requests) == 2
            assert requests[-1]["body"]["model"] == "second-model"


async def test_tui_manual_compaction_and_next_input_use_preserved_native_source(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=1500, memory_mode="off"
    )
    await seed(config)
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        app = compose_application(config, (provider(url),)).tui(session_id="s1")
        async with app.run_test(size=(120, 50)):
            await app.ready.wait()
            await app.command("/compact")
            await settled(app)
            if app.last_error:
                raise app.last_error
            assert any(entry["type"] == "compaction" for entry in app.state["snapshot"]["entries"])
            await app.send_text("continue")
            await settled(app)
            assert len(requests) == 2
            assert not any(
                "old task 0" in str(message.get("content", ""))
                for message in requests[-1]["body"]["messages"]
            )
            assert any(
                entry["message"] and entry["message"]["id"] == "u0"
                for entry in app.state["snapshot"]["entries"]
            )


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
