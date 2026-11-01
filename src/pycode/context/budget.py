# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# 参照 MewCode local context/summary_cache.py; SHA-256:
# e291f27c5264240e6a535b4ec997e0958cec665f5be1e36f0f9f5f7b08621db0
# Adaptation: separate input capacity and early summary trigger; one estimator for runtime and UI;
# model_change also invalidates provider usage.
"""Input budget and the context estimate, with provenance rather than fabricated live counts."""

import math
from dataclasses import dataclass

from pycode.kernel import Message, SessionTree, estimate_text, json_dumps, plain_data
from pycode.models import model_tool_text


@dataclass(frozen=True)
class InputBudget:
    window: int
    output_reserve: int
    safety: int

    def __post_init__(self):
        if self.window <= 0 or self.output_reserve < 1 or self.safety < 0:
            raise ValueError("Window, output reserve and safety are invalid")
        if self.output_reserve + self.safety >= self.window:
            raise ValueError("Output reserve and safety exhaust the input window")

    @classmethod
    def create(cls, window: int, output_reserve: int):
        return cls(window, output_reserve, max(64, math.ceil(window * 0.005)))

    @property
    def input_limit(self) -> int:
        return self.window - self.output_reserve - self.safety

    @property
    def cache_trigger(self) -> int:
        return min(
            math.ceil(self.window * 0.80),
            max(1, self.input_limit - max(64, math.ceil(self.window * 0.02))),
        )


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


class ContextMeter:
    def __init__(self, settings):
        self._settings = settings

    # Pi: estimateProjectedContextTokens — last valid usage plus an estimated tail.
    def report(self, snapshot, projection, tail: tuple[Message, ...] = ()):
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        positions = {entry.id: index for index, entry in enumerate(path)}
        # Usage from before a later edit, compaction or model switch no longer matches the context.
        invalidating = max(
            (
                index
                for index, entry in enumerate(path)
                if entry.type in {"context_edit", "compaction", "model_change"}
            ),
            default=-1,
        )
        by_message = {}
        for item in projection.entries:
            for message in item.messages:
                by_message[message.id] = item.entry_id
        tokens, source, anchor = None, "estimate", None
        for index in range(len(projection.messages) - 1, -1, -1):
            message = projection.messages[index]
            usage = message.usage
            if (
                message.role != "assistant"
                or usage is None
                or message.stop_reason in {"error", "aborted"}
            ):
                continue
            entry_id = by_message.get(message.id)
            position = positions.get(entry_id, -1) if entry_id is not None else -1
            if position <= invalidating:
                continue
            reported = (
                usage.input_tokens + usage.output_tokens
                if usage.input_tokens is not None and usage.output_tokens is not None
                else usage.total_tokens
            )
            if reported is not None and reported > 0:
                tail = projection.messages[index + 1 :]
                tokens = reported + estimate_messages(tail)
                source = "provider_usage_and_estimated_tail" if tail else "provider_usage"
                anchor = usage.request_id
                break
        if tokens is None:
            tokens = estimate_messages(projection.messages) + estimate_text(
                json_dumps(
                    [
                        plain_data(spec)
                        for spec in sorted(self._settings.tools, key=lambda spec: spec.name)
                    ],
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
            tokens += projection.checkpoint.get("harness_system_tokens", 0)
            # Messages that services append to each request (for example the task board view).
            tokens += estimate_messages(tail)
        budget = self._settings.budget
        return {
            "tokens": tokens,
            "window": budget.window,
            "input_limit": budget.input_limit,
            "output_reserve": budget.output_reserve,
            "summary_trigger": budget.cache_trigger,
            "fraction": tokens / budget.window,
            "source": source,
            "anchor_request_id": anchor,
        }

    def estimate(self, snapshot, projection, tail: tuple[Message, ...] = ()):
        return self.report(snapshot, projection, tail)["tokens"]
