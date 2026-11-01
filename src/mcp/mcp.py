# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/mcp-servers.ts
"""MCP catalog service and durable context projection; app owns their assembly."""

import asyncio
from collections.abc import Mapping
from typing import Any

from ..kernel import (
    EntryDraft,
    Message,
    ProjectedEntry,
    ProjectionContribution,
    SessionEntry,
    SessionSnapshot,
    SessionTree,
)
from .client import MCPAdapterConnections
from .tools import MCPTool
from .types import active_mcp, catalog_hash


class MCPService:
    def __init__(self, history, connections: MCPAdapterConnections):
        self._history = history
        self._connections = connections
        self._lock = asyncio.Lock()
        self._catalog = {"servers": [], "tools": []}

    async def initialize(self, *, pending=False):
        remote = await self._connections.catalog()
        async with self._lock:
            snapshot = await self._history.snapshot()
            path = SessionTree(snapshot.index).path(snapshot.leaf_id)
            previous = active_mcp(path)
            digest = catalog_hash(remote)
            if previous is not None and previous["hash"] != digest:
                changed = {"source": "mcp", "previous": previous["hash"], "current": digest}
                if not any(
                    entry.type == "tool_set_change" and entry.data == changed for entry in path
                ):
                    await self._history.append_entry(EntryDraft("tool_set_change", data=changed))
            if previous is None or (not pending and previous["hash"] != digest):
                text = "MCP servers (tools):\n" + "\n".join(
                    "- " + server["name"] + ": " + server["status"] for server in remote["servers"]
                )
                saved = await self._history.append_entry(
                    EntryDraft("mcp_catalog", data={**remote, "hash": digest, "text": text})
                )
                previous = saved.data
            self._catalog = previous

    async def prepare_run(self, *, pending=False):
        """Refresh discovery before a run; resumed runs retain their saved declarations."""
        await self.initialize(pending=pending)

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


MCP_CHECKPOINT_KEYS = frozenset({"mcp"})


def mcp_projection(
    snapshot: SessionSnapshot,
    path: tuple[SessionEntry, ...],
    checkpoint: Mapping[str, Any],
) -> ProjectionContribution:
    catalog = active_mcp(path)
    if catalog is None:
        return ProjectionContribution(checkpoint_removals=MCP_CHECKPOINT_KEYS)
    source = next((entry for entry in reversed(path) if entry.type == "mcp_catalog"), None)
    if source is None:
        source = next(entry for entry in reversed(path) if entry.type == "compaction")
    message = Message(
        "mcp:" + catalog["hash"],
        "system",
        catalog["text"],
        provider_metadata={"source_type": "mcp_catalog", "context_config_system_prompt": False},
    )
    return ProjectionContribution((ProjectedEntry(source.id, (message,)),), {"mcp": catalog})
