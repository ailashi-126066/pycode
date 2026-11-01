"""Mcp history: component-owned implementation."""

from collections.abc import Mapping
from typing import Any

from pycode.kernel import (
    Message,
    ProjectedEntry,
    ProjectionContribution,
    SessionEntry,
    SessionSnapshot,
)
from pycode.mcp.types import active_mcp


class MCPProjection:
    name = "mcp"
    incremental_safe = True
    checkpoint_keys = frozenset({"mcp"})

    def contribute(
        self,
        snapshot: SessionSnapshot,
        path: tuple[SessionEntry, ...],
        entries: tuple[ProjectedEntry, ...],
        checkpoint: Mapping[str, Any],
    ) -> ProjectionContribution:
        catalog = active_mcp(path)
        if catalog is None:
            return ProjectionContribution(checkpoint_removals=self.checkpoint_keys)
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
