# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# Adaptation: native projected entries, UTF-16 ceil estimates and explicit immutable cut results;
# no recovery-omission suffix rule.
"""Pi retention selection; the caller validates pairing before applying a compaction."""

import math
from dataclasses import dataclass

from pycode.kernel import Message, ProjectedContext, SessionEntry, json_dumps
from pycode.models import model_tool_text


def utf16_units(text: str) -> int:
    return len(text) + sum(1 for character in text if ord(character) > 0xFFFF)


def estimate_compaction_message(message: Message) -> int:
    chars = utf16_units(message.text)
    for thinking in message.thinking:
        chars += utf16_units(thinking.text)
    for call in message.tool_calls:
        arguments = json_dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
        chars += utf16_units(call.name + arguments)
    if message.tool_result is not None:
        chars += utf16_units(model_tool_text(message.tool_result))
    chars += 4800 * len(message.attachments)
    return math.ceil(chars / 4)


@dataclass(frozen=True)
class CutPoint:
    first_kept_index: int
    turn_start_index: int
    split_turn: bool


# Pi: findProjectedCutPoint
def find_cut_point(
    entries: tuple[SessionEntry, ...],
    projection: ProjectedContext,
    start_index: int,
    keep_recent_tokens: int,
) -> CutPoint:
    messages = {item.entry_id: item.messages for item in projection.entries}
    valid = []
    turn_starts = []
    for index in range(start_index, len(entries)):
        entry = entries[index]
        if entry.type == "compaction":
            continue
        roles = {message.role for message in messages.get(entry.id, ())}
        if roles.intersection({"user", "assistant", "custom"}):
            valid.append(index)
        if roles.intersection({"user", "custom"}):
            turn_starts.append(index)
    if not valid:
        return CutPoint(start_index, -1, False)
    cut = valid[0]
    accumulated = 0
    for index in range(len(entries) - 1, start_index - 1, -1):
        tokens = sum(
            estimate_compaction_message(message) for message in messages.get(entries[index].id, ())
        )
        if tokens == 0:
            continue
        accumulated += tokens
        if accumulated >= keep_recent_tokens:
            cut = next((candidate for candidate in valid if candidate >= index), valid[-1])
            break
    while cut > start_index:
        previous = entries[cut - 1]
        if previous.type == "compaction" or messages.get(previous.id):
            break
        cut -= 1
    if cut in turn_starts:
        return CutPoint(cut, -1, False)
    start = next((index for index in reversed(turn_starts) if index <= cut), -1)
    return CutPoint(cut, start, start != -1)
