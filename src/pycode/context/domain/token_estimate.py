"""Approximate native message input size for context budgets."""

from pycode.kernel import Message, estimate_text, json_dumps
from pycode.models import model_tool_text


def estimate_messages(messages: tuple[Message, ...]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_text(message.text)
        for thinking in message.thinking:
            total += estimate_text(thinking.text)
        for call in message.tool_calls:
            total += estimate_text(
                call.name + json_dumps(call.arguments, ensure_ascii=False, sort_keys=True)
            )
        if message.tool_result is not None:
            total += estimate_text(model_tool_text(message.tool_result))
        total += 1024 * len(message.attachments)
    return total
