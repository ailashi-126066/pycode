# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/utils.ts
# 参照 MewCode local mewcode/conversation_pairing.py; SHA-256:
# 533aa1f53b8241e3ac6db13c11c87e1d58856ee60c73406371d5ff761e0cb0cb
# Adaptation: native projected entries, UTF-16 ceil estimates and explicit immutable cut results;
# retained boundaries never split a call from its result; missing results are unknown.
"""Compaction cut points, retention, kept boundaries, file operations and tool pairing."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..kernel import (
    Message,
    ProjectedContext,
    SessionEntry,
    ToolResult,
    freeze_value,
    json_dumps,
)
from ..models import model_tool_text


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


def retained_context_entries(path: tuple[SessionEntry, ...]) -> tuple[SessionEntry, ...]:
    for index in range(len(path) - 1, -1, -1):
        entry = path[index]
        if entry.type == "fork_context":
            allowed = set(entry.data["source_entry_ids"])
            selected = []
            for previous in path[:index]:
                if previous.id in allowed:
                    selected.append(previous)
            selected.extend(path[index:])
            path = tuple(selected)
            break
    latest = None
    for index, entry in enumerate(path):
        if entry.type == "compaction":
            latest = index
    if latest is None:
        return path
    compaction = path[latest]
    kept_id = compaction.data["first_kept_entry_id"]
    selected = [compaction]
    keeping = False
    for entry in path[:latest]:
        if entry.id == kept_id:
            keeping = True
        if keeping and not (entry.type == "message" and entry.require_message().role == "system"):
            selected.append(entry)
    selected.extend(path[latest + 1 :])
    return tuple(selected)


def valid_kept_boundary(path: tuple[SessionEntry, ...], first_kept: str | None) -> bool:
    boundary = (
        len(path)
        if first_kept is None
        else next((index for index, entry in enumerate(path) if entry.id == first_kept), -1)
    )
    if boundary < 0:
        return False
    issued = {}
    for index, entry in enumerate(path):
        message = entry.message
        if message is None:
            continue
        for call in message.tool_calls:
            issued[call.id] = index
        if message.tool_result is not None:
            start = issued.pop(message.tool_result.call_id, None)
            if start is not None and start < boundary <= index:
                return False
    return not any(index < boundary for index in issued.values())


@dataclass
class FileOperations:
    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


# Pi: extractFileOpsFromMessage
def extract_file_ops_from_message(message: Message, file_ops: FileOperations) -> None:
    for call in message.tool_calls:
        path = call.arguments.get("path")
        if not isinstance(path, str) or not path:
            continue
        if call.name == "read":
            file_ops.read.add(path)
        elif call.name == "write":
            file_ops.written.add(path)
        elif call.name == "edit":
            file_ops.edited.add(path)


# Pi: computeFileLists
def compute_file_lists(file_ops: FileOperations) -> tuple[list[str], list[str]]:
    modified = file_ops.edited | file_ops.written
    return sorted(file_ops.read - modified), sorted(modified)


# Pi: formatFileOperations
def format_file_operations(read_files: Sequence[str], modified_files: Sequence[str]) -> str:
    sections = []
    if read_files:
        sections.append("<read-files>\n" + "\n".join(read_files) + "\n</read-files>")
    if modified_files:
        sections.append("<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>")
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def format_loaded_skills(read_files: Sequence[str]) -> str:
    """11.1 section 33.1 #8: one line naming the skills read so far (/skills/<source>/<name>/)."""
    skills = [
        f"{path.split('/')[-2]} ({path})"
        for path in read_files
        if path.startswith("/skills/") and path.endswith("/SKILL.md")
    ]
    return "\n\nLoaded skills: " + ", ".join(skills) if skills else ""


UNKNOWN_RESULT = (
    "Tool execution was interrupted. The result is unknown: the tool may or may "
    "not have completed. Verify its effects before relying on them."
)


def repair_tool_pairing(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    copied = tuple(freeze_value(message) for message in messages)
    issued = {}
    resolved = {}
    for index, message in enumerate(copied):
        for call in message.tool_calls:
            issued[call.id] = index
        if message.tool_result is not None:
            result = message.tool_result
            owner = issued.get(result.call_id)
            if owner is not None:
                resolved.setdefault((owner, result.call_id), message)
    output = []
    for index, message in enumerate(copied):
        if message.role == "tool_result":
            continue
        output.append(message)
        for call in message.tool_calls:
            result = resolved.get((index, call.id))
            if result is None:
                result = freeze_value(
                    Message(
                        f"unknown:{message.id}:{call.id}",
                        "tool_result",
                        tool_result=ToolResult(
                            call.id,
                            call.name,
                            UNKNOWN_RESULT,
                            is_error=True,
                            error_code="execution_unknown",
                            details={"status": "unknown"},
                        ),
                    )
                )
            output.append(result)
    return tuple(output)
