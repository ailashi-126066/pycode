"""Native executable tool capability."""

from typing import Any, Protocol

from pycode.kernel.domain.messages import ToolResult
from pycode.kernel.domain.tools import ToolContext, ToolSpec


class ExecutableTool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...
    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult: ...


class ArgumentValidator(Protocol):
    def check_schema(self, schema: dict[str, Any]) -> None: ...
    def convert(self, schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]: ...
    def validate(self, schema: dict[str, Any], arguments: dict[str, Any]) -> None: ...
