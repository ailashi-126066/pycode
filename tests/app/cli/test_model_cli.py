import asyncio
import json
from dataclasses import asdict

import pytest
from tests.models.adapters.test_factory import configuration

from pycode.app.bootstrap import compose_application
from pycode.app.cli.main import main
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.history import EntryDraft
from pycode.kernel.domain.messages import Message


def test_model_command_lists_all_providers_and_selects_existing_session(tmp_path, capsys):
    providers = (configuration("openai"), configuration("deepseek"))
    path = tmp_path / "providers.json"
    path.write_text(json.dumps([asdict(provider) for provider in providers]), encoding="utf-8")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")

    async def create():
        store = await compose_application(config).session_repository().open("s1", create=True)
        await store.append_message(Message("u1", "user", "hello"))
        await store.aclose()

    asyncio.run(create())
    common = [
        "--workspace",
        str(tmp_path),
        "--data-dir",
        str(config.user_data),
        "/model",
        "--providers",
        str(path),
        "--cached",
        "--json",
        "--session",
        "s1",
    ]
    assert main([*common, "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["provider"] for item in listed["catalog"]["models"]] == [
        "deepseek-instance",
        "openai-instance",
    ]
    assert main([*common, "select", providers[0].name, providers[0].model_id]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["selection"]["provider"] == providers[0].name
    assert main([*common, "list"]) == 0
    assert json.loads(capsys.readouterr().out)["selection"]["provider"] == providers[0].name


def test_model_selection_without_session_reports_explicit_error(tmp_path, capsys):
    path = tmp_path / "providers.json"
    provider = configuration("openai")
    path.write_text(json.dumps([asdict(provider)]), encoding="utf-8")
    result = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "model",
            "--providers",
            str(path),
            "--cached",
            "select",
            provider.name,
            provider.model_id,
        ]
    )
    assert result == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "session_required"


@pytest.mark.parametrize(
    "pending_kind,error_code",
    [
        ("revert_started", "revert_recovery_required"),
        ("run_start", "session_needs_input"),
    ],
)
def test_cli_model_selection_cannot_bypass_pending_application_state(
    tmp_path,
    capsys,
    pending_kind,
    error_code,
):
    provider = configuration("openai")
    path = tmp_path / "providers.json"
    path.write_text(json.dumps([asdict(provider)]), encoding="utf-8")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")

    async def create():
        store = await compose_application(config).session_repository().open("s1", create=True)
        try:
            await store.append_message(Message("u1", "user", "hello"))
            await store.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": pending_kind,
                        "session_id": "s1",
                        "transaction_id": "tx",
                        "execution_session_id": "s1",
                        "input_id": "u1",
                    },
                    run_id="run",
                )
            )
            return await store.snapshot()
        finally:
            await store.aclose()

    before = asyncio.run(create())
    result = main(
        [
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(config.user_data),
            "model",
            "--providers",
            str(path),
            "--cached",
            "--session",
            "s1",
            "select",
            provider.name,
            provider.model_id,
        ]
    )
    if pending_kind == "run_start":
        assert result == 0
        assert json.loads(capsys.readouterr().out)["selection"]["model_id"] == provider.model_id
    else:
        assert result == 1
        assert json.loads(capsys.readouterr().out)["error"]["code"] == error_code

    async def read():
        store = await compose_application(config).session_repository().open("s1")
        try:
            return await store.snapshot()
        finally:
            await store.aclose()

    after = asyncio.run(read())
    if pending_kind == "run_start":
        assert any(
            entry.data.get("kind") == "run_end" and entry.data.get("reason") == "interrupted"
            for entry in after.entries
        )
    else:
        assert after == before


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
