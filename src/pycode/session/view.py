# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
from __future__ import annotations

from contextlib import AsyncExitStack

from pycode.context import ContextMeter, ContextProjector
from pycode.kernel import (
    EventConsumer,
    EventStream,
    MemoryEventJournal,
    SessionTree,
    freeze_value,
    plain_data,
)
from pycode.models import UsageService
from pycode.session.restart import pending_run_entry
from pycode.session.sessions import session_title
from pycode.session.store import SqliteSessionStore


class SessionView:
    def __init__(
        self,
        history: SqliteSessionStore,
        projector: ContextProjector,
        usage: UsageService,
        events: MemoryEventJournal,
        consumers: tuple[EventConsumer, ...] = (),
        *,
        owns_history=False,
        context_settings=None,
    ):
        self._history = history
        self._projector = projector
        self._usage = usage
        self._events = events
        self._consumers = consumers
        self._owns_history = owns_history
        self._closing = AsyncExitStack()
        if owns_history:
            self._closing.push_async_callback(history.aclose)
        self._closing.push_async_callback(events.aclose)
        self._closing.push_async_callback(usage.aclose)
        self._context_settings = context_settings
        self._entry_views = {}
        self._message_views = {}

    def message_view(self, message):
        previous = self._message_views.get(message.id)
        if previous is not None and previous[0] is message:
            return previous[1]
        viewed = freeze_value(plain_data(message))
        self._message_views[message.id] = (message, viewed)
        return viewed

    def presentation(self, snapshot, projected):
        entries = []
        for entry in snapshot.entries:
            viewed = self._entry_views.get(entry.id)
            if viewed is None:
                data = {
                    "id": entry.id,
                    "parent_id": entry.parent_id,
                    "type": entry.type,
                    "timestamp": entry.timestamp,
                    "data": entry.data,
                    "selection": freeze_value(plain_data(entry.selection)),
                    "run_id": entry.run_id,
                    "operation_id": entry.operation_id,
                    "message": self.message_view(entry.message)
                    if entry.message is not None
                    else None,
                }
                viewed = freeze_value(data)
                self._entry_views[entry.id] = viewed
            entries.append(viewed)
        snapshot_view = {
            "header": freeze_value(plain_data(snapshot.header)),
            "entries": tuple(entries),
            "leaf_id": snapshot.leaf_id,
            "revision": snapshot.revision,
            "record_count": snapshot.record_count,
            "issues": snapshot.issues,
            "active_path_ids": snapshot.active_path_ids,
        }
        contributions = []
        for entry in projected.entries:
            contributions.append(
                {
                    "entry_id": entry.entry_id,
                    "messages": tuple(self.message_view(message) for message in entry.messages),
                }
            )
        context_view = {
            "leaf_id": projected.leaf_id,
            "entries": tuple(contributions),
            "messages": tuple(self.message_view(message) for message in projected.messages),
            "selection": freeze_value(plain_data(projected.selection)),
            "thinking": projected.thinking,
            "checkpoint": projected.checkpoint,
        }
        return snapshot_view, context_view

    def context_report(self, snapshot, projection):
        settings = (
            self._context_settings(projection) if self._context_settings is not None else None
        )
        if settings is None:
            return {"tokens": None, "window": None, "source": "unavailable"}
        return ContextMeter(settings).report(snapshot, projection)

    async def inspect(self, *, presentation=False):
        snapshot = await self._history.snapshot()
        projected = self._projector.transform_context(snapshot)
        run = pending_run_entry(snapshot)
        waiting = ()
        if run is not None:
            for entry in reversed(SessionTree(snapshot.index).path(snapshot.leaf_id)):
                if entry.run_id == run.run_id and entry.data.get("kind") == "run_wait":
                    waiting = entry.data["pending"]
                    break
        history = []
        for entry in SessionTree(snapshot.index).path(snapshot.leaf_id):
            if entry.type == "message" and entry.require_message().role != "system":
                if presentation:
                    cached = self._entry_views.get(entry.id)
                    shown_message = (
                        cached["message"]
                        if cached is not None
                        else self.message_view(entry.message)
                    )
                else:
                    shown_message = plain_data(entry.message)
                history.append({"entry_id": entry.id, "kind": "message", "message": shown_message})
            elif entry.type == "compaction":
                history.append(
                    {"entry_id": entry.id, "kind": "compaction", "text": "这里进行了上下文压缩"}
                )
        if presentation:
            snapshot_view, context_view = self.presentation(snapshot, projected)
        else:
            snapshot_view, context_view = plain_data(snapshot), plain_data(projected)
        return {
            "title": session_title(snapshot),
            "snapshot": snapshot_view,
            "context": context_view,
            "pending_run_id": run.run_id if run else None,
            "pending": tuple(waiting),
            "history": tuple(history),
            "context_usage": self.context_report(snapshot, projected),
        }

    async def usage(self, run_id=None):
        report = await self._usage.report(run_id)
        snapshot = await self._history.snapshot()
        report["context"] = self.context_report(
            snapshot, self._projector.transform_context(snapshot)
        )
        return report

    async def request_records(self):
        return await self._usage.request_records()

    async def publish_result(self, result):
        snapshot = await self._history.snapshot()
        return await EventStream(
            snapshot.header.session_id, result.run_id, self._consumers, journal=self._events
        ).emit(
            "result",
            {
                "outcome": result.outcome,
                "final_text": result.final_text,
                "pending": list(result.pending),
                "checkpoint_id": result.checkpoint_id,
                "leaf_id": snapshot.leaf_id,
                "usage": await self.usage(result.run_id),
            },
        )

    async def aclose(self):
        await self._closing.aclose()
