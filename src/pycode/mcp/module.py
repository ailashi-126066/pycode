"""MCP-owned assembly, configuration validation and connection lifetime."""

from dataclasses import dataclass
from pathlib import Path

from pycode.kernel import ToolSpec, owned_io
from pycode.mcp.api import MCP
from pycode.mcp.config import load_servers
from pycode.mcp.connection import MCPAdapterConnections
from pycode.mcp.framework import MCPRun
from pycode.mcp.history import MCPProjection as MCPProjection
from pycode.mcp.types import catalog_specs


@dataclass(frozen=True)
class MCPSettings:
    workspace: Path
    user_data: Path
    project_trusted: bool = False


class MCPSession:
    def __init__(self, connections):
        self.connections = connections

    def prepare_run(self, history, pending=False) -> MCPRun | None:
        connections = self.connections
        if connections is None:
            return None
        return MCPRun(MCP(history, connections), connections, pending=pending)

    async def aclose(self):
        if self.connections is not None:
            await self.connections.aclose()


class MCPModule:
    def __init__(self, *, schema):
        self.schema = schema

    async def open_session(
        self,
        settings: MCPSettings,
        credentials,
        *,
        connect: bool = True,
    ) -> MCPSession:
        roots = [settings.user_data / "mcp.json"]
        if settings.project_trusted:
            roots.append(settings.workspace / ".pycode" / "mcp.json")
        servers = await owned_io(load_servers, tuple(roots), settings.workspace)
        connections = (
            MCPAdapterConnections(servers, credentials, validator=self.schema)
            if connect and any(server.enabled for server in servers)
            else None
        )
        try:
            if connections is not None:
                connections.start()
            return MCPSession(connections)
        except BaseException:
            if connections is not None:
                await connections.aclose()
            raise

    @staticmethod
    def view_specs(projection, selection) -> tuple[ToolSpec, ...]:
        catalog = projection.checkpoint.get("mcp")
        if catalog is None or not selection.capabilities.supports_tools:
            return ()
        declared = catalog_specs(catalog)
        return declared
