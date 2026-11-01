# Source: MewCode local mewcode/conversation_pairing.py; SHA-256:
# 533aa1f53b8241e3ac6db13c11c87e1d58856ee60c73406371d5ff761e0cb0cb
# Adaptation: separate tool-message role, immutable tuples and deterministic synthetic result IDs.
"""Repair only a model send copy; missing results are unknown, never successful."""

from pycode.kernel import Message, ToolResult, freeze_value

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
