"""AGENTS.md reaches the system prompt through Deep Agents MemoryMiddleware, not history."""

import json

import pytest

from pycode.app import AppConfig, compose_application
from pycode.context import InstructionFileBackend, instruction_files
from pycode.kernel import Message, StorageError
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def instruction_paths(config):
    paths, _ = instruction_files(config.workspace, config.user_data, trusted=config.project_trusted)
    return paths


async def run_once(config):
    with protocol_server([chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            await session.run(Message("u1", "user", "hello"))
            state = await session.inspect()
        finally:
            await session.aclose()
    return json.dumps(requests[0]["body"]["messages"]), json.dumps(state["snapshot"])


async def test_user_and_trusted_project_agents_md_are_injected_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    data = tmp_path / "data"
    data.mkdir()
    (data / "AGENTS.md").write_text("USER_AGENTS_RULE", encoding="utf-8")
    (tmp_path / "agent.md").write_text("FIRST_PROJECT_RULE", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("PROJECT_AGENTS_RULE", encoding="utf-8")
    untrusted = AppConfig(tmp_path, data, memory_mode="off")
    sent, history = await run_once(untrusted)
    assert sent.count("USER_AGENTS_RULE") == 1 and "PROJECT_AGENTS_RULE" not in sent
    assert sent.count("FIRST_PROJECT_RULE") == 1
    assert "USER_AGENTS_RULE" not in history
    project = tmp_path / "trusted"
    project.mkdir()
    (project / "agent.md").write_text("FIRST_PROJECT_RULE", encoding="utf-8")
    (project / "AGENTS.md").write_text("PROJECT_AGENTS_RULE", encoding="utf-8")
    trusted = AppConfig(project, tmp_path / "data2", memory_mode="off", project_trusted=True)
    sent, history = await run_once(trusted)
    assert sent.count("PROJECT_AGENTS_RULE") == 1 and "PROJECT_AGENTS_RULE" not in history


def test_sources_skip_missing_files_and_enforce_project_trust(tmp_path):
    data = tmp_path / "data"
    config = AppConfig(tmp_path, data, project_trusted=True)
    assert instruction_paths(config) == ()
    (tmp_path / "AGENTS.md").write_text("project", encoding="utf-8")
    assert instruction_paths(config) == (str((tmp_path / "AGENTS.md").resolve()),)
    assert instruction_paths(AppConfig(tmp_path, data)) == ()
    data.mkdir()
    (data / "AGENTS.md").write_text("user", encoding="utf-8")
    assert instruction_paths(config) == (
        str((data / "AGENTS.md").resolve()),
        str((tmp_path / "AGENTS.md").resolve()),
    )
    (tmp_path / "agent.md").write_text("first", encoding="utf-8")
    assert instruction_paths(config)[-1] == str((tmp_path / "AGENTS.md").resolve())


def test_instruction_backend_is_read_only_and_allowlisted(tmp_path):
    allowed = tmp_path / "AGENTS.md"
    backend = InstructionFileBackend([str(allowed)])
    other = str(tmp_path / "secret.txt")
    missing, denied = backend.download_files([str(allowed), other])
    assert missing.error == "file_not_found" and denied.error == "permission_denied"
    allowed.write_text("rule", encoding="utf-8")
    assert backend.download_files([str(allowed)])[0].content == b"rule"
    allowed.write_bytes(b"x" * 65537)
    with pytest.raises(StorageError):
        backend.download_files([str(allowed)])
    assert not hasattr(backend, "upload_files") and not hasattr(backend, "write")


async def test_project_agents_md_alone_requires_trust_and_stays_out_of_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    (tmp_path / "AGENTS.md").write_text("PROJECT_ONLY_RULE", encoding="utf-8")
    sent, history = await run_once(
        AppConfig(tmp_path, tmp_path / "untrusted-data", memory_mode="off")
    )
    assert "PROJECT_ONLY_RULE" not in sent and "PROJECT_ONLY_RULE" not in history
    sent, history = await run_once(
        AppConfig(tmp_path, tmp_path / "trusted-data", memory_mode="off", project_trusted=True)
    )
    assert sent.count("PROJECT_ONLY_RULE") == 1 and "PROJECT_ONLY_RULE" not in history


def test_project_agents_md_cannot_escape_through_a_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to(outside)
    with pytest.raises(StorageError) as error:
        instruction_paths(AppConfig(workspace, tmp_path / "data", project_trusted=True))
    assert error.value.code == "instructions_outside_workspace"


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
