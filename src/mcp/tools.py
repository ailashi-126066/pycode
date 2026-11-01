# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/extensions/mcp/tools.ts
# 参照 MewCode local mcp/tool_wrapper.py; SHA-256:
# aeb2cfda21b0b9c03a490762ac64ec3208ed1cea4fafb637220e820aec55840a
# Adaptation: an ordinary AgentTool; common validation and authorization run first.
"""One MCP tool declaration as an AgentTool that calls its server connection."""

from .types import mcp_spec


class MCPTool:
    def __init__(self, declaration, connections):
        self.spec = mcp_spec(declaration)
        self._declaration = declaration
        self._connections = connections

    async def execute(self, context, arguments):
        return await self._connections.call(self._declaration, arguments, call_id=context.call_id)
