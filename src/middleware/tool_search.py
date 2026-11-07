"""ToolSearch: deferred tools are listed by name only until the model loads them.

Every request carries the built-in tools in full. Deferred tools (MCP for now) appear only as
"name: first sentence" in the system prompt; the model calls ToolSearch with the names it
needs, and from its next step on those tools are sent in full and can be called.

Loaded names live in the state field `loaded_tools`, so they are saved in checkpoints: revert
and branch switching bring back the tools that were loaded at that point, and compaction,
which only rewrites `messages`, leaves them alone.
"""

import re
from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from ..context import Context
from ..tools.files import ToolError


def add_names(old: list[str] | None, new: list[str] | None) -> list[str]:
    return sorted({*(old or []), *(new or [])})


class ToolSearchState(AgentState):
    loaded_tools: NotRequired[Annotated[list[str], add_names]]


def first_sentence(description: str) -> str:
    return re.split(r"(?<=[.!?。！？])\s|\n", description.strip(), maxsplit=1)[0][:180]


class ToolSearchMiddleware(AgentMiddleware):
    state_schema = ToolSearchState

    def __init__(self, deferred: list[BaseTool]):
        super().__init__()
        self.deferred = {tool.name: tool for tool in deferred}

        @tool("ToolSearch")
        def tool_search(names: list[str], runtime: ToolRuntime[Context]) -> Command:
            """Load deferred tools by their exact names from the catalog in the system prompt.
            Loaded tools can be called from your next step on.

            Args:
                names: Tool names copied from the deferred tool catalog.
            """
            return self.load(names, runtime.tool_call_id)

        self.tools = [tool_search]  # create_agent registers it with the tool node

    def load(self, names: list[str], call_id: str) -> Command:
        found = [name for name in names if name in self.deferred]
        if not found:
            raise ToolError(f"No deferred tool named {', '.join(names)}; copy a catalog name")
        text = f"Loaded: {', '.join(found)}. They can be called from your next step."
        if missing := [name for name in names if name not in self.deferred]:
            text += f" Not found: {', '.join(missing)}."
        result = ToolMessage(text, tool_call_id=call_id, name="ToolSearch")
        return Command(update={"loaded_tools": found, "messages": [result]})

    async def awrap_model_call(self, request, handler):
        """Send loaded deferred tools in full and the rest as a catalog in the system prompt."""
        if not self.deferred:  # nothing to load: hide ToolSearch as well
            tools = [t for t in request.tools if getattr(t, "name", None) != "ToolSearch"]
            return await handler(request.override(tools=tools))
        hidden = self.deferred.keys() - set(request.state.get("loaded_tools", []))
        tools = [t for t in request.tools if getattr(t, "name", None) not in hidden]
        # The whole catalog every time, loaded or not, so the prompt stays the same between steps.
        lines = [f"- {name}: {first_sentence(t.description)}" for name, t in self.deferred.items()]
        catalog = (
            "Deferred tools: listed by name only. To use one, first call ToolSearch with its "
            "name; it can be called from your next step on.\n" + "\n".join(sorted(lines))
        )
        system = request.system_message.text if request.system_message else ""
        return await handler(
            request.override(tools=tools, system_message=SystemMessage(f"{system}\n\n{catalog}"))
        )

    async def awrap_tool_call(self, request, handler):
        """Refuse a deferred tool that was not loaded: the model never saw its parameters."""
        name = request.tool_call["name"]
        if name in self.deferred and name not in request.state.get("loaded_tools", []):
            return ToolMessage(
                f"Error: {name} is not loaded. Call ToolSearch with its name first.",
                tool_call_id=request.tool_call["id"],
                name=name,
                status="error",
            )
        return await handler(request)
