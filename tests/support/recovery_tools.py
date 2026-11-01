import asyncio
from pathlib import Path

from pycode.kernel import ToolResult, ToolSpec


class WritingEcho:
    spec = ToolSpec(
        "echo",
        "Write a test result",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        category="write",
    )

    def __init__(self):
        self.executed = 0
        self.started = asyncio.Event()
        self.release = None

    async def execute(self, context, arguments):
        self.executed += 1
        await asyncio.to_thread(
            (Path(context.workspace) / "effect.txt").write_text, arguments["value"]
        )
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return ToolResult("", "", "saved " + arguments["value"])
