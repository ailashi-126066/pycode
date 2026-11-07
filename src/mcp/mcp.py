# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/mcp-servers.ts
"""MCP tool service: connect once when the session opens and keep the tools in memory."""

from .client import MCPAdapterConnections
from .tools import MCPTool


class MCPService:
    def __init__(self, connections: MCPAdapterConnections):
        self._connections = connections
        self._catalog = {"servers": [], "tools": []}

    async def initialize(self):
        """Connect to every configured server and keep its tool list for this session."""
        self._catalog = await self._connections.catalog()

    def tools(self):
        return tuple(
            MCPTool(tool, self._connections) for tool in self._catalog["tools"] if tool["enabled"]
        )

    async def aclose(self):
        await self._connections.aclose()

    def declaration(self, name):
        for tool in self._catalog["tools"]:
            if tool["name"] == name and tool["enabled"]:
                return tool
        return None
