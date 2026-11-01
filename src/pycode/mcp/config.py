"""Mcp config: component-owned implementation."""

# Source: MewCode local config.py and mcp/manager.py; fixed inventory in docs/source-map.md.
# SHA-256 manager: c43972ec32c050d47ab665e17274795d20fd382af4f439ac8db352e0aa61cd0b
# Adaptation: explicit transports, no embedded credentials, bounded validated configuration.

import json
import re
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlsplit

from pycode.kernel import DomainError
from pycode.mcp.types import MCPServer


def parse_servers(raw, workspace):
    if not isinstance(raw, dict) or len(raw) > 32:
        raise DomainError("invalid_mcp_config", "MCP servers must be a bounded named object")
    servers = []
    allowed = {field.name for field in fields(MCPServer)} - {"name"}
    for name, data in raw.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name) or "__" in name:
            raise DomainError("invalid_mcp_config", "MCP server name is invalid")
        if not isinstance(data, dict) or not set(data) <= allowed:
            raise DomainError("invalid_mcp_config", "MCP server has unknown fields")
        transport = data.get("transport", "http" if data.get("url") else "stdio")
        if transport not in {"stdio", "http"}:
            raise DomainError("invalid_mcp_config", "Use stdio or http transport")
        for flag in ("enabled", "trusted"):
            if not isinstance(data.get(flag, flag == "enabled"), bool):
                raise DomainError("invalid_mcp_config", "MCP switches must be booleans")
        seconds = data.get("timeout", 30)
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 0 < seconds <= 600
        ):
            raise DomainError("invalid_mcp_config", "MCP timeout must be positive and bounded")
        args = data.get("args", [])
        if (
            not isinstance(args, list)
            or len(args) > 128
            or any(not isinstance(arg, str) or "\0" in arg for arg in args)
        ):
            raise DomainError("invalid_mcp_config", "MCP arguments must be text")
        command, url, cwd = (
            data.get("command", ""),
            data.get("url", ""),
            data.get("cwd", str(workspace)),
        )
        if any(not isinstance(value, str) or "\0" in value for value in (command, url, cwd)):
            raise DomainError("invalid_mcp_config", "MCP paths and URL must be text")
        directory = Path(cwd).expanduser()
        directory = (directory if directory.is_absolute() else workspace / directory).resolve()
        if not directory.is_dir():
            raise DomainError("invalid_mcp_config", "MCP working directory does not exist")
        if transport == "stdio" and (not command or url):
            raise DomainError("invalid_mcp_config", "stdio requires command without URL")
        if transport == "http":
            try:
                parsed = urlsplit(url)
                valid = parsed.port is None or 0 < parsed.port < 65536
            except ValueError as error:
                raise DomainError("invalid_mcp_config", "MCP URL is malformed") from error
            if (
                not valid
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or command
                or args
            ):
                raise DomainError(
                    "invalid_mcp_config", "HTTP MCP needs a credential-free HTTP(S) URL"
                )
        refs = {}
        for field_name in ("env", "headers"):
            values = data.get(field_name, {})
            if not isinstance(values, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in values.items()
            ):
                raise DomainError(
                    "invalid_mcp_config", "MCP environment/header declarations must be text"
                )
            for value in values.values():
                if not re.fullmatch(r"\$ENV\.[A-Z_][A-Z0-9_]*", value):
                    raise DomainError(
                        "invalid_mcp_config",
                        "MCP environment and headers require $ENV.NAME references",
                    )
            refs[field_name] = dict(values)
        disabled = data.get("disabled_tools", [])
        if (
            not isinstance(disabled, list)
            or any(not isinstance(name, str) or not name or len(name) > 128 for name in disabled)
            or len(disabled) != len(set(disabled))
        ):
            raise DomainError("invalid_mcp_config", "disabled_tools must contain unique tool names")
        servers.append(
            MCPServer(
                name,
                transport,
                command,
                tuple(args),
                str(directory),
                url,
                refs["env"],
                refs["headers"],
                seconds,
                data.get("enabled", True),
                data.get("trusted", False),
                tuple(disabled),
            )
        )
    return tuple(sorted(servers, key=lambda server: server.name))


def load_servers(roots, workspace):
    merged = {}
    for path in roots:
        if not path.exists():
            continue
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("MCP config exceeds limit")
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict) and "mcpServers" in raw:
                raw = raw["mcpServers"]
            for server in parse_servers(raw, workspace):
                merged[server.name] = server
        except (OSError, ValueError) as error:
            raise DomainError("invalid_mcp_config", "Cannot read MCP configuration") from error
    return tuple(merged[name] for name in sorted(merged))
