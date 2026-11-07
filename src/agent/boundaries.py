# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
"""Model tool filtering, streamed message events and durable message/input boundaries."""

from langchain_core.callbacks import AsyncCallbackHandler

from ..kernel import plain_data
from ..tools import make_framework_tool
from .context import RunContext


async def prepare_model_boundary(request, discovery, tool_filters=()):
    """Supply, filter and disclose tools without changing conversation history."""
    context = request.runtime.context
    tools = list(request.tools)
    names = {tool.name for tool in tools if not isinstance(tool, dict)}
    tools.extend(
        make_framework_tool(spec, context)
        for spec in context.registry.model_specs()
        if spec.name not in names
    )
    request = request.override(
        tools=tools, model_settings={**request.model_settings, "stream": True}
    )
    request = filter_model_tools(request, tool_filters)
    return await discovery.disclose(request)


def filter_model_tools(request, tool_filters=()):
    """Read-only keeps read tools; then explicit filters narrow the list."""
    context: RunContext = request.runtime.context
    # Only registered tools reach the model; Deep Agents' own tools (read_file) stay hidden.
    registered = {spec.name: spec for spec in context.registry.list_specs()}
    specs = tuple(
        registered[tool.name]
        for tool in request.tools
        if not isinstance(tool, dict)
        and tool.name in registered
        and (not context.read_only or registered[tool.name].category == "read")
    )
    for select in tool_filters:
        specs = select(context.scope, specs)
    selected = {spec.name for spec in specs}
    permitted = [
        tool
        for tool in request.tools
        if (not context.read_only if isinstance(tool, dict) else tool.name in selected)
    ]
    return request.override(tools=permitted)


# This uses LangChain streaming callbacks to implement Pi's message_start/update events.
class ModelEventCallbacks(AsyncCallbackHandler):
    def __init__(self, context):
        self.context = context

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if "pycode:internal" in kwargs.get("tags", ()):
            return
        self.context.streamed_text = None

    async def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        if "pycode:internal" in kwargs.get("tags", ()):
            return
        context = self.context
        requests = context.requests
        if context.streamed_text is None:
            # Pi: message_start is emitted with the first chunk; a retry starts again.
            context.streamed_text = ""
            await context.events.emit(
                "message_start",
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "model_selection": plain_data(requests.selection),
                    },
                    "request_id": requests.active_request_id,
                    "attempt": requests.active_attempt,
                },
            )
        if token:
            context.streamed_text += token
            await self.update(context, "text", token)
        if chunk is None:
            return
        reasoning = chunk.message.additional_kwargs.get("reasoning_content")
        if reasoning:
            await self.update(context, "thinking", reasoning)
        if isinstance(chunk.message.content, list):
            for block in chunk.message.content:
                if isinstance(block, dict) and block.get("type") in {"thinking", "reasoning"}:
                    text = block.get("thinking") or block.get("reasoning")
                    if text:
                        await self.update(context, "thinking", text)

    @staticmethod
    async def update(context, kind, delta):
        await context.events.emit(
            "message_update",
            {"request_id": context.requests.active_request_id, "kind": kind, "delta": delta},
        )


async def save_message(context, message):
    entry_id = await context.history.append_message(message)
    # A streamed assistant reply already sent message_start with its first chunk.
    if message.role != "assistant" or context.streamed_text is None:
        await context.events.emit("message_start", {"message": plain_data(message)})
    context.streamed_text = None
    await context.events.emit("message_end", {"message": plain_data(message), "entry_id": entry_id})
    return entry_id


async def deliver_input(context, message, kind):
    entry_id = await save_message(context, message)
    await context.events.emit(
        "notification",
        {
            "kind": "input_delivered",
            "input_id": message.id,
            "queue_kind": kind,
            "entry_id": entry_id,
        },
    )
