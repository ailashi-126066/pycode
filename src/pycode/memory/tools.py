# Pi 没有，pycode 新增
"""LangMem's manage tool per scope; the model reads memories with read under /memory/."""

from langmem import create_manage_memory_tool
from pydantic import BaseModel

from pycode.kernel import ToolError, ToolResult, ToolSpec
from pycode.memory.types import MemoryNote

INSTRUCTIONS = (
    "Create, update or delete one short durable memory: lasting user preferences, "
    "corrections of your behavior or stable project facts. Skip temporary requests. "
    "Never store secrets, tokens, passwords or keys."
)


class ManageMemoryTool:
    def __init__(self, memory, namespace, scope):
        self.memory = memory
        self.native = create_manage_memory_tool(
            namespace,
            store=memory.store,
            name=f"manage_{scope}_memory",
            schema=MemoryNote,
            instructions=INSTRUCTIONS,
        )
        schema = self.native.get_input_schema()
        self.spec = ToolSpec(
            self.native.name,
            self.native.description,
            schema.model_json_schema() if issubclass(schema, BaseModel) else schema.schema(),
            category="write",
            execution_mode="sequential",
            source="memory",
            approval_details={"memory_namespace": list(namespace)},
        )

    async def execute(self, context, arguments):
        if self.memory.mode == "off" or context.agent_id != "lead":
            raise ToolError("memory_disabled", "Memory tools are disabled for this execution")
        try:
            response = await self.native.ainvoke(arguments)
        except ValueError as error:
            # LangMem rejects invalid action/id combinations with ValueError.
            raise ToolError("invalid_memory_arguments", str(error)[:300]) from error
        return ToolResult("", "", str(response))
