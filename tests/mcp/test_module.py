"""Configuration-only opens and native call scopes do not bypass lifecycle policy."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from pycode.mcp.framework import MCPRun
from pycode.mcp.module import MCPModule, MCPSettings


@pytest.mark.asyncio
async def test_config_only_open_builds_no_connections(tmp_path, monkeypatch):
    server = SimpleNamespace(name="fixture")
    monkeypatch.setattr("pycode.mcp.module.load_servers", lambda roots, workspace: (server,))

    def forbidden(*args, **kwargs):
        raise AssertionError("config-only must not construct connections")

    monkeypatch.setattr("pycode.mcp.module.MCPAdapterConnections", forbidden)
    module = MCPModule(schema=None)
    settings = MCPSettings(tmp_path, tmp_path)
    session = await module.open_session(settings, {}, connect=False)
    assert session.connections is None
    assert session.prepare_run(None) is None
    await session.aclose()


@pytest.mark.asyncio
async def test_native_lookup_has_no_effect_until_authorized_scope():
    events = []

    @asynccontextmanager
    async def scope(name):
        events.append(("enter", name))
        yield
        events.append(("exit", name))

    binding = MCPRun(
        SimpleNamespace(declaration=lambda name: name),
        SimpleNamespace(framework_tool=lambda name: "native", tool_call=scope),
    )
    assert binding.framework_tool("tool") == "native"
    assert events == []
    async with binding.call_scope("tool"):
        assert events == [("enter", "tool")]
    assert events[-1] == ("exit", "tool")
