"""Turn old messages into one summary message.

Prompt templates are Pi's (https://github.com/earendil-works/pi, MIT, see licenses/pi-MIT.txt).
"""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the following summary:\n"
    "<summary>\n"
)
SUMMARY_SUFFIX = "\n</summary>"
TOOL_RESULT_MAX_CHARS = 2000

SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""  # noqa: E501

FIRST_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""  # noqa: E501

UPDATE_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""  # noqa: E501

# pycode: the cut fell inside a turn, so the summary has to carry that turn's request forward.
ONGOING_NOTE = (
    "The last user request above is still in progress: the messages after this summary "
    "continue working on it. Record exactly what was asked and what has been done so far, "
    "under In Progress."
)


def is_summary(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage) and "pycode_summary" in message.additional_kwargs


def summary_text(message: HumanMessage) -> str:
    return message.text.removeprefix(SUMMARY_PREFIX).removesuffix(SUMMARY_SUFFIX)


def count_turns(messages: list[BaseMessage]) -> int:
    """User turns so far; a summary counts the turns it replaced, so numbering never shifts."""
    return sum(
        m.additional_kwargs["pycode_summary"]["turns"] if is_summary(m) else 1
        for m in messages
        if isinstance(m, HumanMessage)
    )


def serialize(messages: list[BaseMessage]) -> str:
    parts = []
    for message in messages:
        if isinstance(message, HumanMessage) and message.text:
            parts.append(f"[User]: {message.text}")
        elif isinstance(message, AIMessage):
            if message.text:
                parts.append(f"[Assistant]: {message.text}")
            calls = [
                f"{call['name']}({json.dumps(call['args'], ensure_ascii=False)})"
                for call in message.tool_calls
            ]
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif isinstance(message, ToolMessage) and message.text:
            text = message.text
            if len(text) > TOOL_RESULT_MAX_CHARS:
                cut = len(text) - TOOL_RESULT_MAX_CHARS
                text = f"{text[:TOOL_RESULT_MAX_CHARS]}\n\n[... {cut} more characters truncated]"
            parts.append(f"[Tool result]: {text}")
    return "\n\n".join(parts)


def file_lists(messages: list[BaseMessage], previous: dict) -> tuple[list[str], list[str]]:
    """(files only read, files written or edited), including those the previous summary listed."""
    read = set(previous.get("read_files", []))
    modified = set(previous.get("modified_files", []))
    for message in messages:
        for call in message.tool_calls if isinstance(message, AIMessage) else []:
            path = call["args"].get("path")
            if call["name"] == "read" and isinstance(path, str):
                read.add(path)
            elif call["name"] in ("write", "edit") and isinstance(path, str):
                modified.add(path)
    return sorted(read - modified), sorted(modified)


async def summarize(model, messages: list[BaseMessage], cut: int, ongoing: bool, max_tokens: int):
    """One summary message replacing messages[:cut]; an earlier summary at the start is updated."""
    previous = messages[0] if messages and is_summary(messages[0]) else None
    source = messages[1 if previous else 0 : cut]
    prompt = f"<conversation>\n{serialize(source)}\n</conversation>\n\n"
    if previous is not None:
        prompt += f"<previous-summary>\n{summary_text(previous)}\n</previous-summary>\n\n"
    prompt += UPDATE_PROMPT if previous is not None else FIRST_PROMPT
    if ongoing:
        prompt += "\n\n" + ONGOING_NOTE

    reply = await model.ainvoke(
        [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)], max_tokens=max_tokens
    )
    text = reply.text.strip()
    if "<summary>" in text and "</summary>" in text:
        text = text.split("<summary>", 1)[1].split("</summary>", 1)[0].strip()
    if not text:
        raise ValueError("The summary model returned no text")

    read, modified = file_lists(
        source, previous.additional_kwargs["pycode_summary"] if previous else {}
    )
    if read:
        text += "\n\n<read-files>\n" + "\n".join(read) + "\n</read-files>"
    if modified:
        text += "\n\n<modified-files>\n" + "\n".join(modified) + "\n</modified-files>"
    info = {"turns": count_turns(messages[:cut]), "read_files": read, "modified_files": modified}
    return HumanMessage(
        SUMMARY_PREFIX + text + SUMMARY_SUFFIX, additional_kwargs={"pycode_summary": info}
    )
