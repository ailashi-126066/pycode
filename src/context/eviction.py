# Pi 没有，pycode 新增；参照 https://github.com/anomalyco/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/compaction.ts
# Adaptation: whole entries, proportional budgets, >= threshold and append-only context edits.
"""Old completed tool results are replaced through branch-local append-only context edits."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..kernel import EntryDraft, EventStream, ProjectedContext, SessionSnapshot, estimate_text
from ..models import model_tool_text
from .budget import estimate_messages
from .projector import ContextProjector

if TYPE_CHECKING:
    from ..session import SqliteSessionStore


@dataclass(frozen=True)
class EvictionBudget:
    input_limit: int
    protect_tokens: int | None = None
    minimum_tokens: int | None = None

    def limits(self) -> tuple[int, int]:
        if self.input_limit <= 0:
            raise ValueError("Input window must be positive")
        protect = (
            min(self.input_limit // 4, 40000)
            if self.protect_tokens is None
            else self.protect_tokens
        )
        minimum = (
            min(self.input_limit // 8, 20000)
            if self.minimum_tokens is None
            else self.minimum_tokens
        )
        if protect < 0 or minimum < 0:
            raise ValueError("Eviction budgets must not be negative")
        return protect, minimum


@dataclass(frozen=True)
class EvictionSelection:
    entry_ids: tuple[str, ...]
    tokens: int


def select_old_results(
    snapshot: SessionSnapshot,
    projection: ProjectedContext,
    budget: EvictionBudget,
    *,
    protected_entry_ids: tuple[str, ...] = (),
) -> EvictionSelection:
    protect, minimum = budget.limits()
    active_ids = {item.entry_id for item in projection.entries}
    prior = {
        entry.data["target_id"]
        for entry in snapshot.entries
        if entry.type == "context_edit"
        and entry.data.get("kind") == "tool_eviction"
        and entry.id in active_ids
    }
    total = 0
    recoverable = 0
    users = 0
    selected = []
    visible_results = {
        message.id for message in projection.messages if message.tool_result is not None
    }
    for contribution in reversed(projection.entries):
        for message in reversed(contribution.messages):
            if message.role == "user":
                users += 1
            if users < 2:
                continue
            if message.provider_metadata.get("source_type") == "compaction":
                return EvictionSelection(
                    tuple(selected) if recoverable >= minimum else (), recoverable
                )
            result = message.tool_result
            if result is None or message.id not in visible_results:
                continue
            if contribution.entry_id in protected_entry_ids:
                continue
            if contribution.entry_id in prior:
                return EvictionSelection(
                    tuple(selected) if recoverable >= minimum else (), recoverable
                )
            if result.name.lower() in {"skill", "loadskill"} or result.details.get(
                "protected_content"
            ):
                continue
            if result.details.get("status") in {"running", "pending", "unknown"}:
                continue
            tokens = estimate_text(model_tool_text(result))
            total += tokens
            if total <= protect:
                continue
            selected.append(contribution.entry_id)
            recoverable += tokens
    return EvictionSelection(tuple(selected) if recoverable >= minimum else (), recoverable)


@dataclass(frozen=True)
class EvictionReport:
    entry_ids: tuple[str, ...]
    reclaimed_tokens: int
    context_tokens: int


class ToolEvictionService:
    def __init__(
        self,
        store: "SqliteSessionStore",
        projector: ContextProjector,
        budget: EvictionBudget,
        events: EventStream | None = None,
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
