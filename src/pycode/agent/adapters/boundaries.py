"""Agent model filtering, event publication and durable input boundaries."""

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableLambda

from pycode.agent.adapters.task_output import TASK_OUTPUT_PROMPT, visible_partial
from pycode.context import empty_tasks, task_context_text
from pycode.kernel import EntryDraft, estimate_text, plain_data


async def prepare_model_boundary(request, bindings):
    system = request.system_message.content if request.system_message is not None else ""
    content = (
        system + "\n\n" + TASK_OUTPUT_PROMPT
        if isinstance(system, str)
        else [*system, {"type": "text", "text": TASK_OUTPUT_PROMPT}]
    )
    request = request.override(system_message=SystemMessage(content=content))
    for model_filter in bindings.model_filters:
        request = await model_filter(request)
    chain = (
        RunnableLambda(filter_model_tools)
        | RunnableLambda(bindings.disclose_model_tools)
        | RunnableLambda(bindings.calibrate_model_request)
    )
    return await chain.ainvoke(request)


def attach_task_context(request, projection):
    """Dynamic data is counted separately from the stable system configuration."""
    context = request.runtime.context
    state = (
        projection.checkpoint["task_state"]
        if projection is not None
        else request.state.get("task_state", empty_tasks())
    )
    messages = projection.messages if projection is not None else context.completed
    text = task_context_text(state, messages)
    context.task_context_tokens = estimate_text(text) + 4
    return request.override(messages=[SystemMessage(content=text), *request.messages])


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
        self.text_streams = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if "pycode:internal" in kwargs.get("tags", ()):
            return
        self.text_streams[run_id] = ("", "")
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
                    "role": "assistant",
                    "request_id": requests.active_request_id,
                    "attempt": requests.active_attempt,
                    "model_selection": plain_data(requests.selection),
                    "provider_run_id": str(kwargs["run_id"]),
                },
            )
        if token:
            raw, visible = self.text_streams[kwargs["run_id"]]
            raw += token
            updated = visible_partial(raw)
            self.text_streams[kwargs["run_id"]] = (raw, updated)
            context.streamed_text = updated
            await context.events.emit(
                "message_delta",
                {
                    "text": updated[len(visible) :],
                    "output_activity": True,
                    "request_id": requests.active_request_id,
                },
            )
        if chunk is None:
            return
        reasoning = chunk.message.additional_kwargs.get("reasoning_content")
        if reasoning:
            await context.events.emit(
                "thinking_delta",
                {"text": reasoning, "request_id": requests.active_request_id},
            )
        if isinstance(chunk.message.content, list):
            for block in chunk.message.content:
                if isinstance(block, dict) and block.get("type") in {"thinking", "reasoning"}:
                    text = block.get("thinking") or block.get("reasoning")
                    if text:
                        await context.events.emit(
                            "thinking_delta",
                            {"text": text, "request_id": requests.active_request_id},
                        )

    async def on_llm_end(self, response, *, run_id, **kwargs):
        self.text_streams.pop(run_id, None)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.text_streams.pop(run_id, None)


async def save_message(context, message):
    entry_id = await context.journal.append_message(message)
    result = message.tool_result
    if result is not None and result.output_id and context.outputs is not None:
        await context.outputs.bind(result.output_id, entry_id)
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
        await context.events.emit("message_start", plain_data(message))
    context.streamed_text = None
    await context.events.emit("message_end", {**plain_data(message), "entry_id": entry_id})
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
