"""Agent model filtering, event publication and durable input boundaries."""

from uuid import uuid4

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.runnables import RunnableLambda

from pycode.context import board_from_data, task_board_text
from pycode.kernel import EntryDraft, Message, plain_data


async def prepare_model_boundary(request, bindings):
    for model_filter in bindings.model_filters:
        request = await model_filter(request)
    chain = (
        RunnableLambda(filter_model_tools)
        | RunnableLambda(bindings.disclose_model_tools)
        | RunnableLambda(bindings.calibrate_model_request)
    )
    return await chain.ainvoke(request)


def task_board_messages(context, projection) -> tuple[Message, ...]:
    """Only an active board is injected: its view each request, the full view once after
    a compaction. Without a board nothing is added. It follows the message projection
    (11.1 section 8.1 ③), so a new revision leaves the cached history prefix intact."""
    board = projection.checkpoint["task_board"]
    if board is None or board["status"] != "active":
        return ()
    text = task_board_text(board_from_data(board), full=context.board_full_view)
    context.board_full_view = False
    return (Message(uuid4().hex, "system", text),)


async def filter_model_tools(request):
    context = request.runtime.context
    planning = (
        context.plans is not None
        and context.plans.state is not None
        and (
            context.plans.state.mode == "planning"
            or context.plans.state.approved_run_id != context.run_id
            and context.plans.state.mode != "completed"
        )
    )
    # Only registered tools reach the model; Deep Agents' own tools (read_file) stay hidden.
    specs = {spec.name: spec for spec in context.registry.list_specs()}
    permitted = []
    for tool in request.tools:
        if isinstance(tool, dict):
            if not (context.read_only or planning):
                permitted.append(tool)
        elif tool.name in specs and (
            not (context.read_only or planning)
            or specs[tool.name].category == "read"
            or planning
            and not context.read_only
            and tool.name in {"write", "edit"}
        ):
            permitted.append(tool)
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
    entry_id = await context.journal.append_message(message)
    result = message.tool_result
    if (
        result is not None
        and result.details.get("file_snapshot")
        and context.history_store is not None
    ):
        await context.history_store.append_entry(
            EntryDraft(
                "custom",
                run_id=context.run_id,
                operation_id=result.operation_id,
                data={
                    "kind": "file_snapshot",
                    "message_entry_id": entry_id,
                    **result.details["file_snapshot"],
                },
            )
        )
    context.completed.append(message)
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


class AgentMessagePublisher:
    def __init__(self, context):
        self.context = context

    async def publish(self, message):
        return await save_message(self.context, message)
