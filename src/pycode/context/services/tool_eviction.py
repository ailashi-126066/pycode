"""Apply budget decisions only through branch-local append-only context edits."""

from dataclasses import dataclass

from pycode.context.domain.token_estimate import estimate_messages
from pycode.context.domain.tool_eviction import EvictionBudget, select_old_results
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import EntryDraft, EventSink
from pycode.kernel import HistoryJournal as SessionStore


@dataclass(frozen=True)
class EvictionReport:
    entry_ids: tuple[str, ...]
    reclaimed_tokens: int
    context_tokens: int


class ToolEvictionService:
    def __init__(
        self,
        store: SessionStore,
        projector: ContextProjector,
        budget: EvictionBudget,
        events: EventSink | None = None,
    ) -> None:
        self._store = store
        self._projector = projector
        self._budget = budget
        self._events = events

    async def apply(self, *, protected_entry_ids: tuple[str, ...] = ()) -> EvictionReport:
        snapshot = await self._store.snapshot()
        projection = self._projector.transform_context(snapshot)
        selection = select_old_results(
            snapshot, projection, self._budget, protected_entry_ids=protected_entry_ids
        )
        by_id = {entry.id: entry for entry in snapshot.entries}
        applied = []
        for identifier in selection.entry_ids:
            result = by_id[identifier].require_message().require_tool_result()
            state = f"; exit code {result.exit_code}" if result.exit_code is not None else ""
            error = "; error result" if result.is_error else ""
            path = result.details.get("full_output_path")
            where = f"; full output: {path}. Read it with read" if path else ""
            replacement = f"Old tool output omitted{state}{error}{where}."
            await self._store.append_entry(
                EntryDraft(
                    "context_edit",
                    data={
                        "target_id": identifier,
                        "replacement": replacement,
                        "kind": "tool_eviction",
                    },
                )
            )
            applied.append(identifier)
        after = self._projector.transform_context(await self._store.snapshot())
        before_tokens = estimate_messages(projection.messages)
        after_tokens = estimate_messages(after.messages)
        recovered = max(0, before_tokens - after_tokens)
        if applied:
            await self._store.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": "tool_eviction_audit",
                        "targets": applied,
                        "reclaimed_tokens": recovered,
                        "context_tokens_before": before_tokens,
                        "context_tokens_after": after_tokens,
                        "source_leaf_id": snapshot.leaf_id,
                        "source_revision": snapshot.revision,
                    },
                )
            )
            if self._events is not None:
                await self._events.emit(
                    "notification",
                    {"kind": "tool_eviction", "targets": applied, "reclaimed_tokens": recovered},
                )
            after = self._projector.transform_context(await self._store.snapshot())
        return EvictionReport(tuple(applied), recovered, after_tokens)
