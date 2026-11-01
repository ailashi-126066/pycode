# Source: MewCode local mewcode/context/summary_cache.py; SHA-256:
# e291f27c5264240e6a535b4ec997e0958cec665f5be1e36f0f9f5f7b08621db0
# Source: https://github.com/earendil-works/pi/blob/42a3497d03ad17e308a2299fa824727894f2c0ec/packages/coding-agent/src/core/compaction/compaction.ts
# Adaptation: injected I/O, owned task, immutable objects and append-only safe adoption.
"""Background summary production never mutates the active history or graph."""

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from pycode.context.domain.context_meter import ContextMeter
from pycode.context.domain.summaries import (
    SummaryCheck,
    SummaryObject,
    SummaryReport,
    SummarySettings,
)
from pycode.context.domain.summary_source import capture_source, valid_source
from pycode.context.ports.summaries import SummaryGenerator, SummaryStore
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import (
    DomainError,
    EntryDraft,
    EventStream,
    ModelError,
    SessionEntry,
    SessionTree,
    StorageError,
    clone_value,
    data_hash,
    plain_data,
)

if TYPE_CHECKING:
    from pycode.session import SqliteSessionStore


class SummaryService:
    def __init__(
        self,
        history: "SqliteSessionStore",
        projector: ContextProjector,
        objects: SummaryStore,
        generator: SummaryGenerator,
        settings: SummarySettings,
        events: EventStream,
        *,
        preparation: Callable[["SummaryService", SummaryCheck], Awaitable[SummaryReport]],
        on_compacted: Callable[[], Awaitable[None]] | None = None,
    ):
        self._preparation = preparation
        self._on_compacted = on_compacted
        self._history = history
        self._projector = projector
        self._objects = objects
        self._generator = generator
        self._settings = settings
        self._meter = ContextMeter(settings)
        self._events = events
        self._source = None
        self._result = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def configure(
        self, settings: SummarySettings, generator: SummaryGenerator, events: EventStream
    ):
        if self._closed:
            raise RuntimeError("Cannot reconfigure a closed summary service")
        self._settings = settings
        self._meter = ContextMeter(settings)
        self._generator = generator
        self._events = events

    def checkpoint(self, projection):
        checkpoint = clone_value(projection.checkpoint)
        system = [
            message.text
            for message in projection.messages
            if message.role == "system"
            and message.provider_metadata.get("context_config_system_prompt", True)
        ]
        checkpoint.update(
            system_prompt="\n\n".join(system) if system else None,
            tools=[
                plain_data(spec)
                for spec in sorted(self._settings.tools, key=lambda spec: spec.name)
            ],
            selection=plain_data(self._settings.selection),
        )
        return checkpoint

    def set_tools(self, tools):
        self._settings = replace(self._settings, tools=tools)
        self._meter = ContextMeter(self._settings)

    def config_hash(self):
        # Only the numbers that shape the cut and its budget (11.1 section 35): switching to a
        # model with the same window keeps a prepared summary. Checkpoint state (task_board,
        # plan, mcp, skills, prompt, tools) is taken again at adoption, so it is not hashed.
        return data_hash(
            {
                "context_window": self._settings.budget.window,
                "max_output_tokens": self._settings.budget.output_reserve,
                "keep_recent_tokens": self._settings.keep_recent_tokens,
            }
        )

    def estimate(self, snapshot, projection):
        return self._meter.estimate(snapshot, projection)

    async def restore(self, snapshot, projection, fingerprint):
        if self._source is not None:
            return
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        for entry in reversed(path):
            if entry.type != "custom" or entry.data.get("kind") != "summary_job":
                continue
            try:
                loaded = await self._objects.load(entry.data["job_id"])
            except StorageError as error:
                await self._objects.invalidate(entry.data["job_id"], "corrupt")
                await self._events.emit(
                    "compaction",
                    {"job_id": entry.data["job_id"], "status": "invalid", "error_code": error.code},
                )
                continue
            if loaded is None:
                continue
            source, result = loaded
            if result is None:
                await self._generator.reconcile_lost(source)
                await self._objects.invalidate(source.job_id, "lost_executor")
                continue
            if result.status == "ready" and valid_source(source, snapshot, projection, fingerprint):
                self._source, self._result = source, result
                return
            if result.status == "ready":
                await self._objects.invalidate(source.job_id, "source_or_configuration_changed")

    async def invalidate(self, reason: str):
        source = self._source
        if source is not None:
            await self._objects.invalidate(source.job_id, reason)
        task = self._task
        if task is not None:
            await self.collect(cancel=True)
        self._source = None
        self._result = None
        self._task = None

    async def collect(self, *, cancel: bool):
        if self._task is None:
            return
        if cancel and not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            if not cancel:
                raise

    async def produce(self, source, generator):
        source_hash = data_hash(plain_data(source))
        try:
            completion = await generator.generate(clone_value(source))
            if not completion.text.strip():
                raise ModelError("empty_summary", "Summary model returned empty text")
            result = SummaryObject(
                source.job_id, source_hash, "ready", completion.text, completion.usage
            )
        except asyncio.CancelledError:
            result = SummaryObject(
                source.job_id, source_hash, "interrupted", error_code="summary_cancelled"
            )
            await self._objects.save_result(result)
            raise
        except DomainError as error:
            result = SummaryObject(source.job_id, source_hash, "failed", error_code=error.code)
        await self._objects.save_result(result)
        if self._source is not None and self._source.job_id == source.job_id:
            self._result = result
        await self._events.emit(
            "compaction",
            {"job_id": source.job_id, "status": result.status, "error_code": result.error_code},
        )

    async def prepare(self, *, manual=False, force=False, wait_for_capacity=True, allow_apply=True):
        async with self._lock:
            check = await self.inspect_preparation(manual, force, wait_for_capacity, allow_apply)
            return await self._preparation(self, check)

    async def inspect_preparation(self, manual, force, wait_for_capacity, allow_apply):
        if self._closed:
            raise RuntimeError("Summary service is closed")
        snapshot = await self._history.snapshot()
        projection = self._projector.transform_context(snapshot)
        fingerprint = self.config_hash()
        tokens = self.estimate(snapshot, projection)
        full = tokens >= self._settings.budget.input_limit or force
        if self._source is not None and not valid_source(
            self._source, snapshot, projection, fingerprint
        ):
            await self._objects.invalidate(self._source.job_id, "source_or_configuration_changed")
            await self.collect(cancel=True)
            self._source = self._result = self._task = None
        await self.restore(snapshot, projection, fingerprint)
        if (
            manual
            and self._source is not None
            and self._result is not None
            and self._result.status == "failed"
        ):
            await self._objects.invalidate(self._source.job_id, "manual_retry")
            self._source = self._result = self._task = None
        trigger = manual or force or tokens >= self._settings.budget.cache_trigger
        return SummaryCheck(
            snapshot,
            projection,
            fingerprint,
            tokens,
            self._source is None and trigger,
            allow_apply and (manual or full),
            manual,
            wait_for_capacity,
        )

    async def start_generation(self, check):
        snapshot = check.snapshot
        projection = check.projection
        fingerprint = check.config_hash
        source = capture_source(
            snapshot,
            projection,
            job_id=uuid4().hex,
            selection=self._settings.selection,
            config_hash=fingerprint,
            keep_recent_tokens=self._settings.keep_recent_tokens,
            tokens_before=check.tokens,
        )
        if source is not None:
            await self._objects.save_source(source)
            await self._history.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": "summary_job",
                        "job_id": source.job_id,
                        "prefix_hash": source.prefix_hash,
                        "config_hash": source.config_hash,
                    },
                )
            )
            self._source = source
            self._task = asyncio.create_task(
                self.produce(source, self._generator),
                name="pycode-summary-" + source.job_id,
                context=contextvars.Context(),
            )
            await self._events.emit("compaction", {"job_id": source.job_id, "status": "generating"})

    async def apply_prepared(self, check):
        if self._task is not None and (check.manual or check.wait_for_capacity):
            await asyncio.shield(self._task)
        if (
            self._result is None
            and self._source is not None
            and self._task is not None
            and self._task.done()
        ):
            loaded = await self._objects.load(self._source.job_id)
            if loaded is not None:
                self._result = loaded[1]
        if self._result is not None and self._result.status == "ready":
            return await self.adopt()
        if check.manual or check.wait_for_capacity:
            code = self._result.error_code if self._result is not None else "no_summary_boundary"
            raise ModelError(
                code or "summary_failed", "Context capacity insufficient; history preserved"
            )
        return None

    def preparation_report(self, check):
        if self._task is not None and self._task.done() and not self._task.cancelled():
            self._task.result()
        status = (
            self._result.status
            if self._result is not None
            else "generating"
            if self._task is not None
            else "idle"
        )
        protected = (
            self._source.source_entry_ids
            if self._source is not None and status in {"ready", "generating"}
            else ()
        )
        return SummaryReport(check.tokens, protected_entry_ids=protected, status=status)

    async def adopt(self):
        snapshot = await self._history.snapshot()
        projection = self._projector.transform_context(snapshot)
        source, result = self._source, self._result
        if source is None or result is None:
            raise ModelError("summary_not_ready", "No completed summary is available to adopt")
        identifier = "compaction_" + source.job_id
        data = {
            "summary": result.text,
            "first_kept_entry_id": source.first_kept_entry_id,
            # Taken at adoption: changes made while the summary ran are already in this state.
            "checkpoint": self.checkpoint(projection),
            "summary_job_id": source.job_id,
            "prefix_hash": source.prefix_hash,
            "config_hash": source.config_hash,
            "usage": [plain_data(item) for item in result.usage],
            # Pi: CompactionEntry.tokensBefore and details{readFiles, modifiedFiles}
            "tokens_before": source.tokens_before,
            "details": {
                "read_files": list(source.read_files),
                "modified_files": list(source.modified_files),
            },
        }
        entry = SessionEntry(identifier, snapshot.leaf_id, "compaction", "summary", data=data)
        candidate = replace(snapshot, entries=(*snapshot.entries, entry), leaf_id=identifier)
        after = self._projector.transform_context(candidate)
        tokens = self.estimate(candidate, after)
        if tokens >= self._settings.budget.input_limit:
            await self._objects.invalidate(source.job_id, "tail_outgrew_retention")
            self._source = self._result = self._task = None
            raise ModelError(
                "compaction_capacity",
                "Summary and recent tail still exceed capacity; history preserved",
            )
        await self._history.append_entry(EntryDraft("compaction", id=identifier, data=data))
        await self._events.emit(
            "compaction", {"job_id": source.job_id, "status": "applied", "context_tokens": tokens}
        )
        await self._objects.invalidate(source.job_id, "adopted")
        self._source = self._result = self._task = None
        if self._on_compacted is not None:
            await self._on_compacted()
        return SummaryReport(tokens, source.job_id, status="applied")

    async def aclose(self):
        if self._closed:
            return
        await self.collect(cancel=True)
        self._closed = True

    async def cancel_background(self):
        await self.collect(cancel=True)
        if self._source is not None:
            loaded = await self._objects.load(self._source.job_id)
            if loaded is not None and loaded[1] is not None and loaded[1].status == "ready":
                self._result = loaded[1]
                self._task = None
                return
        self._source = self._result = self._task = None
