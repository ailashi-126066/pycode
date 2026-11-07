"""MCP servers from ~/.pycode/mcp.json; their tools are named mcp__<server>__<tool>.

The file uses the common "mcpServers" layout; pycode adds one key per server:

    {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_TOKEN": "..."}},
        "docs": {"url": "https://example.com/mcp", "approve": false}}}

"approve": false lets that server's tools run without asking; every other MCP call is approved
by the user like write, edit and bash. Servers connect once when the agent opens and stay
connected until it closes; one that fails is reported and the others still work.
"""

import asyncio
import json
import warnings
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StdioTransport
from fastmcp.mcp_config import MCPConfig
from langchain_core._api import LangChainBetaWarning
from langchain_core.tools import BaseTool

with warnings.catch_warnings():
    warnings.simplefilter("ignore", LangChainBetaWarning)
    from langchain.mcp import as_langchain_tool

CONNECT_SECONDS = 30
CALL_SECONDS = 120


@dataclass
class Server:
    name: str
    approve: bool  # whether its tool calls wait for the user's approval
    tools: list[BaseTool] = field(default_factory=list)
    error: str | None = None  # why it could not connect


async def decline(message, response_type, params, context):
    """A server asking the user for input mid-call is told no: pycode has no form for it."""
    return ElicitResult(action="decline")


def read_config(config_file: Path) -> dict:
    """Server name → options; no file means no servers."""
    if not config_file.exists():
        return {}
    return json.loads(config_file.read_text()).get("mcpServers", {})


@asynccontextmanager
async def connect(config_file: Path, log_file: Path):
    """Connect every configured server and yield them; disconnect on exit."""
    servers = []
    async with AsyncExitStack() as stack:
        for name, options in read_config(config_file).items():
            options = dict(options)
            server = Server(name, approve=options.pop("approve", True))
            servers.append(server)
            try:
                server.tools = await open_server(stack, name, options, log_file)
            except Exception as error:  # a wrong command, URL or token: report and go on
                server.error = f"{type(error).__name__}: {error}"
        yield servers


async def open_server(stack, name: str, options: dict, log_file: Path) -> list[BaseTool]:
    transport = MCPConfig.model_validate({"mcpServers": {name: options}}).mcpServers[name]
    transport = transport.to_transport()  # stdio for "command", HTTP for "url"
    if isinstance(transport, StdioTransport):
        transport.log_file = log_file  # its stderr would otherwise be drawn over the TUI
    client = Client(transport, elicitation_handler=decline, timeout=CALL_SECONDS)
    async with asyncio.timeout(CONNECT_SECONDS):
        await stack.enter_async_context(client)
        listed = await client.list_tools()
    tools = []
    for remote in listed:
        tool = await as_langchain_tool(remote, client)  # still calls the server's own name
        tool.name = f"mcp__{name}__{remote.name}"
        tools.append(tool)
    return tools
