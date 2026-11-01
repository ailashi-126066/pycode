# Source: local context/summary_cache.py, SHA-256:
# e291f27c5264240e6a535b4ec997e0958cec665f5be1e36f0f9f5f7b08621db0
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# Adaptation: extracted existing phase-5 estimator for identical runtime/UI accounting;
# model_change also invalidates provider usage.
"""One pure context estimator, with provenance rather than fabricated live counts."""

from pycode.context.domain.tasks import board_from_data, task_board_text
from pycode.context.domain.token_estimate import estimate_messages
from pycode.kernel import SessionTree, estimate_text, json_dumps, plain_data


class ContextMeter:
    def __init__(self, settings):
        self._settings = settings

    # Pi: estimateProjectedContextTokens — last valid usage plus an estimated tail.
    def report(self, snapshot, projection):
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
            # The injected board view is counted on its own, as the request adds it.
            board = projection.checkpoint["task_board"]
            if board is not None and board["status"] == "active":
                tokens += estimate_text(task_board_text(board_from_data(board))) + 4
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

    def estimate(self, snapshot, projection):
        return self.report(snapshot, projection)["tokens"]
