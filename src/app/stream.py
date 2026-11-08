"""agent.astream → plain events an interface can draw, each message reported once.

The framework stream repeats messages: approval re-emits the model's reply, compaction re-adds
the kept history. Messages already in the conversation, or already reported, are skipped.
"""

from dataclasses import dataclass

from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage


@dataclass(frozen=True)
class Text:  # a piece of the model's reply as it is generated
    text: str


@dataclass(frozen=True)
class Thinking:  # a piece of the model's reasoning, when the provider streams it
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    text: str
    error: bool


@dataclass(frozen=True)
class Usage:  # tokens of one finished model reply
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Notice:  # compaction progress, or the notice when the turn limit ends the run
    text: str


@dataclass(frozen=True)
class Approval:  # the run paused; answer with Command(resume={interrupt_id: {"decisions": [...]}})
    interrupt_id: str
    actions: list  # one {"name", "args", "description"} per tool call waiting for a decision


@dataclass(frozen=True)
class PlanReview:  # ExitPlanMode paused; answer with {"decision": ..., "feedback": ...}
    interrupt_id: str
    plan: str
    board: dict
    layers: list


def pause_event(item):
    """The event for one LangGraph interrupt: a plan to review, or tool calls to approve."""
    if item.value.get("kind") == "plan":
        return PlanReview(item.id, item.value["plan"], item.value["board"], item.value["layers"])
    return Approval(item.id, item.value["action_requests"])


async def events(agent, request, config, context):
    """Run the agent on `request` (new input or a resume Command) and yield events in order.

    The stream always runs to its end, even after an Approval: LangGraph saves the paused
    state only when the stream finishes."""
    state = await agent.aget_state(config)
    reported = {message.id for message in state.values.get("messages", [])}
    async for mode, chunk in agent.astream(
        request, config, context=context, stream_mode=["messages", "updates", "custom"]
    ):
        if mode == "messages":
            token, metadata = chunk
            if metadata.get("langgraph_node") != "model":
                continue  # e.g. a summary being generated inside the compaction middleware
            if reasoning := token.additional_kwargs.get("reasoning_content"):
                yield Thinking(reasoning)
            if token.text:
                yield Text(token.text)
        elif mode == "custom":
            if "compaction" in chunk:
                yield Notice(chunk["compaction"])
        else:  # "updates": what each graph node produced once it finished
            for node, update in chunk.items():
                if node == "__interrupt__":
                    for item in update:
                        yield pause_event(item)
                    continue
                # A node returns one dict, or a list of them when middleware added a Command
                # (compaction saving the compacted history with the reply); some return None.
                parts = update if isinstance(update, list) else [update]
                for part in parts:
                    for message in part.get("messages", []) if isinstance(part, dict) else []:
                        if isinstance(message, RemoveMessage) or message.id in reported:
                            continue
                        reported.add(message.id)
                        for event in message_events(node, message):
                            yield event


def message_events(node: str, message):
    if isinstance(message, AIMessage):
        if node != "model" and message.text:
            yield Notice(message.text)  # not streamed: written by middleware, e.g. the turn limit
        for call in message.tool_calls:
            yield ToolCall(call["id"], call["name"], call["args"])
        if message.usage_metadata:
            usage = message.usage_metadata
            yield Usage(usage["input_tokens"], usage["output_tokens"])
    elif isinstance(message, ToolMessage):
        yield ToolResult(
            message.tool_call_id, message.name, message.text, message.status == "error"
        )


async def settle_cancelled(agent, config) -> None:
    """After the user stops a run, answer tool calls that never got a result.

    A reply whose tool calls have no results would make the next model request invalid."""
    state = await agent.aget_state(config)
    if state.interrupts:
        return  # still waiting for approval; answering it settles those calls
    messages = state.values.get("messages", [])
    reply = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    missing = [call for call in (reply.tool_calls if reply else []) if call["id"] not in answered]
    if missing:
        results = [
            ToolMessage(
                "The user stopped the run before this tool finished; its effects are unknown.",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
            for call in missing
        ]
        await agent.aupdate_state(config, {"messages": results})
