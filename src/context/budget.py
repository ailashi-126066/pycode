# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# 参照 MewCode local context/summary_cache.py; SHA-256:
# e291f27c5264240e6a535b4ec997e0958cec665f5be1e36f0f9f5f7b08621db0
# Adaptation: separate input capacity and early summary trigger; one estimator for runtime and UI;
# model_change also invalidates provider usage.
"""Input budget and the context estimate, with provenance rather than fabricated live counts."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage

from ..kernel import Message, SessionTree, estimate_text, json_dumps, plain_data
from ..models import model_tool_text


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


def estimate_framework_messages(messages: Sequence[BaseMessage]) -> int:
    """Estimate the actual framework content and tool arguments prepared for a request."""
    total = 0
    for message in messages:
        total += 4
        if isinstance(message.content, str):
            total += estimate_text(message.content)
        else:
            for block in message.content:
                if isinstance(block, dict) and block.get("type") in {"image", "image_url", "file"}:
                    total += 1024
                else:
                    total += estimate_text(
                        block if isinstance(block, str) else json_dumps(block, ensure_ascii=False)
                    )
        if isinstance(message, AIMessage):
            reasoning = message.additional_kwargs.get("reasoning_content", "")
            total += estimate_text(reasoning)
            for call in message.tool_calls:
                total += estimate_text(
                    call["name"] + json_dumps(call["args"], ensure_ascii=False, sort_keys=True)
                )
    return total


class ContextMeter:
    def __init__(self, settings):
        self._settings = settings

    def report(self, snapshot, messages: Sequence[BaseMessage], *, checkpoint=None):
        """Measure prepared request messages; history supplies usage and invalidation metadata."""
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        invalidating = max(
            (
                index
                for index, entry in enumerate(path)
                if entry.type in {"context_edit", "compaction", "model_change", "system_prompt"}
                or (
                    entry.type == "tool_set_change"
                    and entry.data.get("source") == "effective_model"
                )
            ),
            default=-1,
        )
        originals = {
            entry.message.id: (index, entry.message)
            for index, entry in enumerate(path)
            if entry.type == "message" and entry.message is not None
        }
        tokens, source, anchor = None, "estimate", None
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, AIMessage) or message.id is None:
                continue
            original = originals.get(message.id)
            if original is None:
                continue
            position, saved_message = original
            usage = saved_message.usage
            if (
                position <= invalidating
                or usage is None
                or saved_message.stop_reason in {"error", "aborted"}
            ):
                continue
            reported = (
                usage.input_tokens + usage.output_tokens
                if usage.input_tokens is not None and usage.output_tokens is not None
                else usage.total_tokens
            )
            if reported is not None and reported > 0:
                added = messages[index + 1 :]
                tokens = reported + estimate_framework_messages(added)
                source = "provider_usage_and_estimated_tail" if added else "provider_usage"
                anchor = usage.request_id
                break
        if tokens is None:
            tokens = estimate_framework_messages(messages)
            tokens += estimate_text(
                json_dumps(
                    [
                        plain_data(spec)
                        for spec in sorted(self._settings.tools, key=lambda spec: spec.name)
                    ],
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
            tokens += (checkpoint or {}).get("harness_system_tokens", 0)
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

    def estimate(self, snapshot, messages: Sequence[BaseMessage], *, checkpoint=None):
        return self.report(snapshot, messages, checkpoint=checkpoint)["tokens"]
