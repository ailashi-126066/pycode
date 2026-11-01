"""Actual application configuration and resource ownership, without module registries."""

import pytest
from tests.support.execution import provider

from pycode.app import AppConfig, compose_application
from pycode.app.composition import child_config
from pycode.kernel import Message
from pycode.subagents import Subagent
from pycode.tools import ReadOnlyToolGate


def configuration(tmp_path):
    return AppConfig(tmp_path, tmp_path / "state", memory_mode="off", allow_delegation=False)


async def test_providerless_root_does_not_open_configuration_and_mcp_can_be_disabled(tmp_path):
    config = configuration(tmp_path)
    config.user_data.mkdir()
    (config.user_data / "mcp.json").write_text("not valid JSON")
    application = compose_application(config)
    assert application.status().workspace.exists
    assert application.session_repository() is not None
    session = await compose_application(config, (provider("http://127.0.0.1:9"),)).open_session(
        "disabled", create=True, refresh_models=False, startup_hooks=False, connect_mcp=False
    )
    try:
        assert session.launcher.mcp is None
        assert session.subagents is None
        assert session.memory.mode == "off"
        assert session.skills is session.launcher.skills.service
    finally:
        await session.aclose()


def test_child_scope_keeps_credential_workspace_and_removes_delegation(tmp_path):
    config = configuration(tmp_path)
    agent = Subagent(
        "a1",
        "a1",
        "agent-1",
        "review",
        "readonly",
        False,
        5,
        "call",
        "run",
        str(tmp_path / "child"),
    )
    child = child_config(config, agent, interactive=False)
    assert child.read_only and not child.allow_delegation and child.max_turns == 5
    assert child.credential_workspace == tmp_path
    assert isinstance(child.tool_gate, ReadOnlyToolGate)


async def test_failed_initialization_releases_history_writer(tmp_path):
    config = configuration(tmp_path)
    config.user_data.mkdir()
    (config.user_data / "mcp.json").write_text("not valid JSON")
    application = compose_application(config, (provider("http://127.0.0.1:9"),))
    with pytest.raises(Exception, match="configuration"):
        await application.open_session("failed", create=True, refresh_models=False)
    store = await application.session_repository().open("failed", create=True)
    try:
        await store.claim_writer()
        await store.append_message(Message("u", "user", "writer available"))
    finally:
        await store.aclose()


async def test_close_attempts_remaining_resources_after_hook_failure(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    application = compose_application(config, (provider("http://127.0.0.1:9"),))
    closed = []
    from pycode.hooks import HooksService
    from pycode.memory import MemoryService

    original = MemoryService.aclose

    async def close_memory(self):
        closed.append("memory")
        await original(self)

    async def close_hooks(self):
        closed.append("hooks")
        raise RuntimeError("shutdown failed")

    monkeypatch.setattr(MemoryService, "aclose", close_memory)
    monkeypatch.setattr(HooksService, "aclose", close_hooks)
    session = await application.open_session("close", create=True, refresh_models=False)
    await session.history.append_message(Message("u", "user", "persist this session"))
    with pytest.raises(RuntimeError, match="shutdown failed"):
        await session.aclose()
    assert closed == ["hooks", "memory"]
    store = await application.session_repository().open("close")
    try:
        await store.claim_writer()
    finally:
        await store.aclose()
