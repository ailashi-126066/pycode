"""Mcp connection: component-owned implementation."""

# Source: MewCode local mcp/manager.py and tool_wrapper.py; fixed SHA in docs/source-map.md.
# SHA-256 manager: c43972ec32c050d47ab665e17274795d20fd382af4f439ac8db352e0aa61cd0b
# SHA-256 wrapper: aeb2cfda21b0b9c03a490762ac64ec3208ed1cea4fafb637220e820aec55840a
# Adaptation: concurrent owned connections, native catalogs, schema hashes and no blind call retry.

import asyncio
import contextvars
from contextlib import asynccontextmanager
from typing import cast

from fastmcp.client import Client
from fastmcp.client.transports import ClientTransport
from fastmcp.exceptions import McpError as MCPError
from langchain.mcp import MCPAdapter
from langchain.mcp.tools import as_langchain_tool

from pycode.kernel import DomainError, StorageError, ToolError, data_hash, plain_data
from pycode.mcp.types import result_from_mcp, tool_name
from pycode.tools import JsonSchemaValidator


async def reject_elicitation(*args, **kwargs):
    raise ToolError(
        "mcp_elicitation_unsupported", "MCP elicitation is not supported by this client"
    )


class MCPAdapterConnection:
    def __init__(self, config, environment):
        self.config = config
        self.environment = environment
        self.adapter: MCPAdapter | None = None
        self.tools = {}
        self.info = {}
        self.closed = False
        self.connect_lock = asyncio.Lock()

    def resolve(self, references):
        values = {}
        for key, reference in references.items():
            name = reference.removeprefix("$ENV.")
            if name not in self.environment:
                raise ToolError(
                    "mcp_environment_missing", "MCP environment reference is unavailable"
                )
            values[key] = self.environment[name]
        return values

    async def connect(self):
        async with self.connect_lock:
            await self.connect_locked()

    async def connect_locked(self):
        if self.closed:
            raise ToolError("mcp_connection_closed", "MCP adapter has been closed")
        if self.adapter is not None:
            return
        if self.config.transport == "stdio":
            server = {
                "command": self.config.command,
                "args": list(self.config.args),
                "cwd": self.config.cwd,
                "env": self.resolve(self.config.env),
            }
        else:
            server = {
                "url": self.config.url,
                "transport": "http",
                "headers": self.resolve(self.config.headers),
            }
        client = Client(
            {"mcpServers": {self.config.name: server}},
            elicitation_handler=reject_elicitation,
        )
        adapter = MCPAdapter(client)
        async with asyncio.timeout(self.config.timeout):
            await adapter.__aenter__()
            try:
                client = self.single_client(adapter)
                declared = await client.list_tools(cache_mode="refresh")
                self.remote_tools = [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in declared
                ]
                self.tools = {tool.name: await as_langchain_tool(tool, client) for tool in declared}
            except BaseException:
                await adapter.__aexit__(None, None, None)
                raise
        self.adapter = adapter
        client = self.single_client(adapter)
        server_info = client.server_info
        capabilities = client.server_capabilities
        self.info = {
            "serverInfo": server_info.model_dump(mode="json", exclude_none=True)
            if server_info is not None
            else {},
            "capabilities": capabilities.model_dump(mode="json", exclude_none=True)
            if capabilities is not None
            else {},
            "instructions": client.instructions,
        }

    @staticmethod
    def single_client(adapter: MCPAdapter) -> Client[ClientTransport]:
        # The adapter is constructed from one server config, never a ClientGroup.
        return cast(Client[ClientTransport], adapter.client)

    def connected_adapter(self) -> MCPAdapter:
        adapter = self.adapter
        if adapter is None:
            raise ToolError("mcp_connection_closed", "MCP adapter has been closed")
        return adapter

    async def list_tools(self, *, refresh=False):
        await self.connect()
        if refresh:
            adapter = self.connected_adapter()
            client = self.single_client(adapter)
            try:
                async with adapter:
                    async with asyncio.timeout(self.config.timeout):
                        declared = await client.list_tools(cache_mode="refresh")
                        self.remote_tools = [
                            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                            for tool in declared
                        ]
                        self.tools = {
                            tool.name: await as_langchain_tool(tool, client) for tool in declared
                        }
            except (MCPError, OSError, TimeoutError) as error:
                await client.close()
                raise ToolError("mcp_request_failed", "MCP tool discovery failed") from error
        if len(self.remote_tools) > 2048:
            raise ToolError("mcp_catalog_invalid", "MCP catalog exceeds limits")
        return self.remote_tools

    def framework_tool(self, name):
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError("mcp_tool_missing", "MCP tool is no longer available")
        return tool

    async def invoke(self, name, arguments, *, call_id):
        async with self.tool_call():
            tool = self.framework_tool(name)
            return await tool.ainvoke(
                {"type": "tool_call", "id": call_id, "name": name, "args": arguments}
            )

    @asynccontextmanager
    async def tool_call(self):
        await self.connect()
        adapter = self.connected_adapter()
        try:
            async with adapter:
                async with asyncio.timeout(self.config.timeout):
                    yield
        except (MCPError, OSError, TimeoutError) as error:
            await self.single_client(adapter).close()
            if str(error) == "MCP elicitation is not supported by this client":
                raise StorageError(
                    "mcp_elicitation_unsupported",
                    "MCP tool requested unsupported elicitation; no answer was sent. "
                    "The interrupted tool may already have produced effects.",
                ) from error
            raise StorageError(
                "mcp_effects_uncertain", "MCP call interrupted; effects may have occurred"
            ) from error

    async def aclose(self):
        self.closed = True
        async with self.connect_lock:
            if self.adapter is not None:
                adapter, self.adapter = self.adapter, None
                await adapter.__aexit__(None, None, None)
        self.tools.clear()


class MCPAdapterConnections:
    def __init__(self, servers, environment, *, validator: JsonSchemaValidator):
        self._validator = validator
        self._configs = {server.name: server for server in servers if server.enabled}
        self._clients = {}
        self._environment = environment
        self._catalog = {"servers": [], "tools": []}
        self._startup = None
        self._closed = False

    def start(self):
        if self._closed:
            raise ToolError("mcp_connection_closed", "MCP connections have been closed")
        if self._startup is None:
            self._startup = asyncio.create_task(
                self.discover(), name="pycode-mcp-startup", context=contextvars.Context()
            )

    async def client(self, name):
        if self._closed or name not in self._configs:
            raise ToolError("mcp_server_unavailable", "MCP server is unavailable")
        if name not in self._clients:
            self._clients[name] = MCPAdapterConnection(self._configs[name], self._environment)
        connection = self._clients[name]
        await connection.connect()
        return connection

    async def describe(self, name):
        config = self._configs[name]
        connected = name in self._clients and self._clients[name].adapter is not None
        client = await self.client(name)
        capabilities = client.info.get("capabilities", {})
        tools = await client.list_tools(refresh=connected) if "tools" in capabilities else []
        validator = self._validator
        declared, names = [], set()
        config_hash = data_hash(plain_data(config))
        for tool in sorted(tools, key=lambda tool: tool.get("name", "")):
            original = tool.get("name")
            if (
                not isinstance(original, str)
                or not original
                or len(original) > 128
                or original in names
            ):
                raise ToolError("mcp_catalog_invalid", "MCP tool names must be unique bounded text")
            names.add(original)
            schema = tool.get("inputSchema")
            if not isinstance(schema, dict):
                raise ToolError("mcp_catalog_invalid", "MCP input schema is missing")
            validator.check_schema(schema)
            annotations = tool.get("annotations", {})
            declared.append(
                {
                    "name": tool_name(name, original),
                    "server": name,
                    "original_name": original,
                    "description": tool.get("description") or original,
                    "input_schema": schema,
                    "schema_hash": data_hash(schema),
                    "config_hash": config_hash,
                    "annotations": annotations,
                    "trusted": config.trusted,
                    "enabled": original not in config.disabled_tools,
                }
            )
        information = {
            "name": name,
            "info": client.info,
            "config_hash": config_hash,
            "status": "connected",
        }
        return information, declared

    async def discover(self):
        names = sorted(self._configs)
        results = await asyncio.gather(
            *(self.describe(name) for name in names), return_exceptions=True
        )
        servers, tools = [], []
        for name, result in zip(names, results, strict=True):
            if isinstance(result, DomainError):
                servers.append({"name": name, "status": "failed", "error_code": result.code})
            elif isinstance(result, BaseException):
                raise result
            else:
                info, declared = result
                servers.append(info)
                tools.extend(declared)
        self._catalog = {"servers": servers, "tools": sorted(tools, key=lambda item: item["name"])}
        return self._catalog

    async def catalog(self):
        self.start()
        owned = self._startup
        if owned is None:
            raise StorageError("mcp_not_started", "MCP startup did not begin")
        result = await asyncio.shield(owned)
        if self._startup is owned:
            self._startup = None
        return result

    def framework_tool(self, declaration):
        connection = self._clients[declaration["server"]]
        return connection.framework_tool(declaration["original_name"]).model_copy(
            update={"name": declaration["name"]}
        )

    async def validate_call(self, declaration):
        client = await self.client(declaration["server"])
        # Validate against the catalog captured at connect time; a reconnect
        # re-discovers, so its cache is always fresh. Tool-set changes only take
        # effect at turn boundaries, matching the documented reload semantics.
        current = await client.list_tools()
        remote = next(
            (tool for tool in current if tool["name"] == declaration["original_name"]), None
        )
        if remote is None or data_hash(remote["inputSchema"]) != declaration["schema_hash"]:
            raise ToolError(
                "mcp_schema_changed",
                "MCP schema changed or tool disappeared; reload after settling execution",
            )
        expected = declaration.get("annotations", {})
        observed = remote.get("annotations", {})
        if any(
            expected.get(key) != observed.get(key)
            for key in (
                "readOnlyHint",
                "destructiveHint",
                "idempotentHint",
                "openWorldHint",
            )
        ):
            raise ToolError(
                "mcp_policy_changed", "MCP tool policy annotations changed before execution"
            )
        return client

    @asynccontextmanager
    async def tool_call(self, declaration):
        if declaration is None:
            raise ToolError("mcp_tool_unavailable", "MCP tool is outside the current catalog")
        client = await self.validate_call(declaration)
        async with client.tool_call():
            yield

    async def call(self, declaration, arguments, *, call_id="mcp-call"):
        """The explicit adapter entry uses the same native tool and policy boundary."""
        client = await self.validate_call(declaration)
        result = await client.invoke(declaration["original_name"], arguments, call_id=call_id)
        return result_from_mcp(
            result.content,
            result.status,
            result.artifact,
            name=declaration["name"],
            call_id=call_id,
            declaration=declaration,
        )

    async def aclose(self):
        self._closed = True
        if self._startup is not None and not self._startup.done():
            self._startup.cancel()
        if self._startup is not None:
            await asyncio.gather(self._startup, return_exceptions=True)
        await asyncio.gather(*(client.aclose() for client in self._clients.values()))
        self._clients.clear()
