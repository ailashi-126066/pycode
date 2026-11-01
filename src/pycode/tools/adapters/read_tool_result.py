"""Read complete tool output by a scoped output ID, never an arbitrary path."""

from pycode.kernel import MAX_BYTES, MAX_LINES, ToolContext, ToolResult, ToolSpec, plain_data
from pycode.tools.ports.outputs import ToolOutputStore


class ReadToolResult:
    spec = ToolSpec(
        name="ReadToolResult",
        description="Read a bounded range of a saved tool result. Offsets are 1-based lines.",
        category="read",
        exposure="direct",
        input_schema={
            "type": "object",
            "properties": {
                "output_id": {"type": "string", "pattern": "^output_[a-f0-9]{32}$"},
                "offset": {"type": "integer", "minimum": 1, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LINES, "default": 100},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 4,
                    "maximum": MAX_BYTES,
                    "default": MAX_BYTES,
                },
                "byte_offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["output_id"],
            "additionalProperties": False,
        },
    )

    def __init__(self, outputs: ToolOutputStore) -> None:
        self._outputs = outputs

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        result = await self._outputs.read(
            arguments["output_id"],
            arguments.get("offset", 1),
            min(arguments.get("limit", 100), MAX_LINES - 1),
            min(arguments.get("max_bytes", MAX_BYTES), MAX_BYTES - 512),
            arguments.get("byte_offset", 0),
        )
        return ToolResult(
            context.call_id,
            self.spec.name,
            result.text
            + (
                f"\n[Lines {result.start_line}-{result.end_line}; more={result.has_more}; "
                f"next_offset={result.next_offset}; next_byte_offset={result.next_byte_offset}]"
            ),
            details={
                "range": {key: value for key, value in plain_data(result).items() if key != "text"}
            },
        )
