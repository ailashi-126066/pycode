"""How full the context is, when to start and adopt a summary, and where to cut."""

import json
import math
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from .summarize import is_summary

# The newest messages stay as they are; only older ones go into the summary.
KEEP_RECENT_TOKENS = 20_000
# Room for the summary itself when checking that the kept messages will fit.
SUMMARY_RESERVE = 8_192


def thresholds(window: int, max_output: int) -> tuple[int, int]:
    """(start a background summary, adopt it). Adopting happens at 90% of the window, but never
    later than the input the model can still accept after its output reserve."""
    accepted = window - max_output - max(64, math.ceil(window * 0.005))
    adopt = min(math.ceil(window * 0.9), accepted)
    start = min(math.ceil(window * 0.8), adopt - max(64, math.ceil(window * 0.02)))
    return start, adopt


def estimate_text(text: str) -> int:
    """About 4 characters per token for English; one per Chinese (or other non-ASCII) character,
    which errs on the high side so compaction starts early rather than late."""
    wide = sum(1 for ch in text if ord(ch) > 127)
    return wide + math.ceil((len(text) - wide) / 4)


def estimate_message(message: BaseMessage) -> int:
    size = estimate_text(message.text) + 4
    if isinstance(message, AIMessage):
        for call in message.tool_calls:
            size += estimate_text(call["name"] + json.dumps(call["args"], ensure_ascii=False))
    # DeepSeek's reasoning is sent back with every request (see models.ReasoningDeepSeek).
    if reasoning := message.additional_kwargs.get("reasoning_content"):
        size += estimate_text(reasoning)
    return size


def context_tokens(messages: list[BaseMessage], overhead: int) -> int:
    """Tokens the next model request will take.

    The newest reply's reported input_tokens already count the system prompt, the tool
    definitions and every earlier message, so only what came after it is estimated. A reply
    reported before the latest summary was adopted describes a context that no longer exists,
    so only replies after the last message kept at adoption count. `overhead` is the estimated
    system prompt plus tool definitions, used when no report can be trusted.
    """
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        if is_summary(messages[index]):
            last_kept = messages[index].additional_kwargs["pycode_summary"].get("last_kept_id")
            ids = [m.id for m in messages]
            start = ids.index(last_kept) + 1 if last_kept in ids else len(messages)
            break
    for index in range(len(messages) - 1, start - 1, -1):
        usage = getattr(messages[index], "usage_metadata", None)
        if isinstance(messages[index], AIMessage) and usage:
            later = sum(estimate_message(m) for m in messages[index + 1 :])
            return usage["input_tokens"] + usage["output_tokens"] + later
    return overhead + sum(estimate_message(m) for m in messages)


def is_turn_start(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage) and not is_summary(message)


@dataclass(frozen=True)
class Cut:
    index: int  # messages[:index] become the summary
    ongoing: bool  # the cut falls inside a turn that is still being worked on


def choose_cut(messages: list[BaseMessage], overhead: int, limit: int) -> Cut | None:
    first = 1 if messages and is_summary(messages[0]) else 0
    # Keep roughly the newest KEEP_RECENT_TOKENS; a small window keeps at most a quarter of it.
    keep, index, kept = min(KEEP_RECENT_TOKENS, limit // 4), len(messages), 0
    while index > first and kept < keep:
        index -= 1
        kept += estimate_message(messages[index])
    # A tool result must stay right after the call that produced it.
    while index > first and isinstance(messages[index], ToolMessage):
        index -= 1

    # Prefer keeping the whole turn the cut falls in...
    turn = next((i for i in range(index, first - 1, -1) if is_turn_start(messages[i])), None)
    if turn is not None and turn > first:
        whole = overhead + SUMMARY_RESERVE + sum(estimate_message(m) for m in messages[turn:])
        if whole < limit:
            return Cut(turn, ongoing=False)
    # ...unless that leaves nothing to summarize or still does not fit: then cut inside it.
    if index <= first:
        return None
    return Cut(index, ongoing=not is_turn_start(messages[index]))
