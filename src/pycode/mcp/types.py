"""Mcp types: component-owned implementation."""

# Source: MewCode local mcp/loading_strategy.py; SHA-256:
# d34db2524575d47b93c7c68fe8b93340bc84d4cc23a21477e86018059763092b
# Adaptation: enabled tool declarations and immutable server/tool identities.
from dataclasses import dataclass, field
from typing import Literal

from pycode.kernel import StorageError, ToolResult, ToolSpec, data_hash


@dataclass(frozen=True)
class MCPServer:
    name: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str = ""
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30
    enabled: bool = True
    trusted: bool = False
    disabled_tools: tuple[str, ...] = ()


def tool_name(server, original):
    return "mcp__" + server + "__" + original


def mcp_spec(tool, *, visible=True):
    readonly = tool["trusted"] and tool.get("annotations", {}).get("readOnlyHint") is True
    return ToolSpec(
        tool["name"],
        tool["description"],
        tool["input_schema"],
        category="read" if readonly else "external",
        execution_mode="parallel" if readonly else "sequential",
        source="mcp:" + tool["server"],
        model_visible=visible,
    )


def catalog_specs(catalog):
    tools = catalog.get("tools", ())
    result = []
    for tool in tools:
        if tool["enabled"]:
            result.append(mcp_spec(tool))
    return tuple(sorted(result, key=lambda spec: spec.name))


def active_mcp(path):
    catalog = None
    for entry in path:
        if entry.type == "compaction" and "mcp" in entry.data.get("checkpoint", {}):
            catalog = entry.data["checkpoint"]["mcp"]
        elif entry.type == "mcp_catalog":
            catalog = entry.data
    if catalog is not None and any(
        type(tool.get("enabled")) is not bool or "loading" in tool for tool in catalog["tools"]
    ):
        raise StorageError(
            "invalid_mcp_catalog", "MCP catalog requires current enabled tool declarations"
        )
    return catalog


def catalog_hash(catalog):
    return data_hash({key: catalog[key] for key in ("servers", "tools")})


def result_from_mcp(content, status, artifact, *, name, call_id="", declaration=None):
    """Normalize one native MCP result at the durable message boundary."""
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    text = []
    media = []
    for block in blocks:
        if isinstance(block, str):
            text.append(block)
            continue
        kind = block.get("type", "unknown")
        resource = block.get("resource")
        if kind == "text":
            text.append(block["text"])
        elif kind == "resource" and isinstance(resource, dict):
            text.append(resource.get("text", "[binary resource]"))
        else:
            text.append("[" + kind + ": " + block.get("mime_type", "") + "]")
            media.append(block)
    details = {"mcp_content_blocks": media}
    if declaration is not None:
        for key in ("server", "original_name"):
            details[key] = declaration[key]
    return ToolResult(
        call_id,
        name,
        "\n".join(text),
        is_error=status == "error",
        structured_content=artifact.get("structured_content")
        if isinstance(artifact, dict)
        else None,
        details=details,
    )
