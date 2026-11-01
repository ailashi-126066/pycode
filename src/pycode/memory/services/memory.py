"""Memory lifecycle: recall, background consolidation, change log, undo and user edits."""

import asyncio
import contextvars
import logging
from uuid import uuid4

from pycode.context import ContextProjector, retained_context_entries
from pycode.kernel import (
    DomainError,
    EntryDraft,
    Message,
    SessionTree,
    StorageError,
    ToolError,
    plain_data,
)
from pycode.memory.domain.memory import RECALL_SELECTED_LIMIT, memory_record
from pycode.memory.ports.memory import MemoryUpdater

logger = logging.getLogger(__name__)

# Consolidation input: the latest compaction summary plus the most recent dialogue.
SUMMARY_CHARS = 8000
DIALOGUE_CHARS = 24000
DIALOGUE_MESSAGES = 40


def consolidation_source(snapshot):
    """Return (summary, messages) from the current branch, bounded in characters."""
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    # Reuse branch/fork and retained-tail semantics: compaction is appended after
    # the messages it keeps, so raw append order alone would lose that tail.
    path = retained_context_entries(path)
    summary = ""
    start = 0
    for index, entry in enumerate(path):
        if entry.type == "compaction":
            summary = str(entry.data.get("summary", ""))[:SUMMARY_CHARS]
            start = index + 1
    messages = []
    budget = DIALOGUE_CHARS
    for entry in reversed(path[start:]):
        message = entry.message
        if message is None or message.role not in {"user", "assistant"} or not message.text:
            continue
        if len(messages) >= DIALOGUE_MESSAGES or budget <= 0:
            break
        text = message.text[:budget]
        budget -= len(text)
        messages.append(Message(message.id, message.role, text))
    messages.reverse()
    return summary, tuple(messages)


class MemoryService:
    def __init__(
        self,
        history,
        store,
        scopes,
        timing,
        changes,
        *,
        updater: MemoryUpdater | None = None,
        mode=None,
        auto_namespaces=None,
    ):
        self.history, self.store, self.scopes, self.timing = history, store, scopes, timing
        self.changes = changes
        self.projector = ContextProjector(())
        self.updater = updater
        self.mode = mode
        self.user_namespace = next(ns for ns, scope in scopes.items() if scope == "user")
        # Automatic consolidation writes the user scope and, only when trusted, the project.
        self.auto_namespaces = tuple(auto_namespaces or (self.user_namespace,))
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def initialize(self):
        if self.mode is None:
            self.mode = await self.store.get_mode(self.user_namespace)
        if self.mode not in {"auto", "off"}:
            raise StorageError("invalid_memory_mode", "Choose auto or off")

    async def set_mode(self, mode):
        if mode not in {"auto", "off"}:
            raise StorageError("invalid_memory_mode", "Choose auto or off")
        if mode == "off":
            await self.collect(cancel=True)
        await self.store.set_mode(self.user_namespace, mode)
        self.mode = mode
        if mode == "off":
            await self.history.append_entry(
                EntryDraft("memory_load", data={"records": [], "loaded_at": self.timing.now()})
            )

    async def records(self):
        found = []
        for namespace, scope in self.scopes.items():
            snapshot = await self.store.snapshot(namespace)
            for key, record in snapshot["records"].items():
                found.append(memory_record(namespace, key, record, scope))
        return found

    async def list(self):
        return {"mode": self.mode, "records": plain_data(await self.records())}

    async def publish_recall(self, records, *, task_id=None, input_id=None):
        await self.history.append_entry(
            EntryDraft(
                "memory_load",
                data={
                    "records": plain_data(records),
                    "task_id": task_id,
                    "input_id": input_id,
                    "loaded_at": self.timing.now(),
                },
            )
        )

    async def select(self, query, *, task_id=None, input_id=None):
        """Select relevant entries, including an empty selection; never fill with recent ones."""
        if self.mode == "off":
            return ()
        records = await self.records()
        selected = []
        if records and self.updater is not None:
            snapshot = await self.history.snapshot()
            try:
                chosen = await self.updater.select(
                    snapshot.header.session_id,
                    "memory-recall-" + uuid4().hex,
                    query,
                    records,
                    limit=RECALL_SELECTED_LIMIT,
                )
                candidates = {(r.scope, r.memory_id): r for r in records}
                identities = list(dict.fromkeys(chosen or ()))
                if not set(identities) <= candidates.keys():
                    raise ToolError("invalid_memory_selection", "Unknown memory ID in selection")
                selected = [candidates[key] for key in identities[:RECALL_SELECTED_LIMIT]]
            except DomainError as error:
                logger.warning("Memory recall selection failed: %s", error.code)
                await self.history.append_entry(
                    EntryDraft("memory_recall_error", data={"reason": error.code})
                )
        await self.publish_recall(selected, task_id=task_id, input_id=input_id)
        return tuple(selected)

    async def prepare_task(self, *, settled=False):
        """Keep one recall batch for the active task, across turns and process restarts."""
        snapshot = await self.history.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        state = self.projector.transform_context(snapshot).checkpoint["task_state"]
        task_id = state["active_task_id"]
        task = state["tasks"].get(task_id)
        loaded = None
        user = None
        user_position = change_position = -1
        action = None
        assistant = None
        for position, entry in enumerate(path):
            if entry.type == "memory_load":
                loaded = entry.data
            message = entry.message if entry.type == "message" else None
            if message is None:
                continue
            if message.role == "user" and not message.provider_metadata.get(
                "pycode_output_continuation"
            ):
                user, user_position = message, position
            elif message.role == "assistant":
                assistant = message
            if message.provider_metadata.get("pycode_task_change") is not None:
                change_position = position
                action = message.provider_metadata.get("pycode_task_action")
        terminal = task is not None and task["status"] in {"completed", "cancelled"}
        finished = (
            assistant is not None and not assistant.tool_calls and assistant.stop_reason == "stop"
        )
        release = (
            self.mode == "off"
            or terminal
            and (change_position > user_position or settled and finished)
            or task is None
            and settled
            and finished
        )
        input_id = user.id if user is not None else None
        if release:
            if loaded is not None and loaded["records"]:
                await self.publish_recall([], task_id=task_id, input_id=input_id)
            return
        if user is None:
            return
        owner = task_id if task is not None and not terminal else None
        if loaded is not None:
            if owner is not None and loaded.get("task_id") == owner:
                return
            if (
                owner is None
                and loaded.get("task_id") is None
                and loaded.get("input_id") == input_id
            ):
                return
            if (
                owner is not None
                and loaded.get("task_id") is None
                and loaded.get("input_id") == input_id
                and action is not None
                and action["relation"] == "new"
            ):
                await self.publish_recall(loaded["records"], task_id=owner, input_id=input_id)
                return
        query = user.text
        if owner is not None and task is not None:
            step = next(
                (item for item in task["steps"] if item["id"] == task["current_step"]), None
            )
            query = (
                f"Current user input: {user.text}\nTask goal: {task['goal']}\n"
                f"Current step: {step['title'] if step is not None else 'not planned'}\n"
                "Current user instructions take priority over the saved task."
            )
        await self.select(query, task_id=owner, input_id=input_id)

    async def settled(self, run_id, reason="session_end"):
        """Schedule one background consolidation; at most one runs at a time."""
        if self.closed or self.mode == "off" or self.updater is None:
            return
        if any(not task.done() for task in self.tasks.values()):
            return
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        self.tasks[run_id] = asyncio.create_task(
            self.consolidate(run_id, reason),
            name="pycode-memory-consolidate",
            context=contextvars.Context(),
        )

    async def compacted(self):
        await self.settled("memory-compaction-" + uuid4().hex, "compaction")

    async def consolidate(self, run_id, reason="manual"):
        """Run LangMem Store Manager once per automatic scope; failures are only logged."""
        try:
            return await self._consolidate(run_id, reason)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - even storage/log failures stay background
            logger.warning(
                "Memory consolidation failed (%s): %s",
                reason,
                getattr(error, "code", type(error).__name__),
            )
            return None

    async def _consolidate(self, run_id, reason):
        if self.updater is None:
            raise StorageError("memory_unavailable", "No memory updater is configured")
        async with self.lock:
            snapshot = await self.history.snapshot()
            summary, messages = consolidation_source(snapshot)
            if not summary and not messages:
                return None
            # Filtered entries (secrets, too long, scope full) are skipped, not fatal.
            view = self.store.recording(skip_rejected=True)
            error_code = None
            try:
                for namespace in self.auto_namespaces:
                    await self.updater.consolidate(
                        view, namespace, summary, messages, snapshot.header.session_id, run_id
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - background work must not affect chat
                error_code = getattr(error, "code", type(error).__name__)
                logger.warning("Memory consolidation failed (%s): %s", reason, error_code)
            finally:
                if view.rejected:
                    logger.info(
                        "Memory consolidation skipped %d filtered entries", len(view.rejected)
                    )
                # Partial writes from a failed run are still logged so they can be undone.
                change_id = await self.log(
                    view.changes,
                    "consolidate",
                    session_id=snapshot.header.session_id,
                    run_id=run_id,
                    reason=reason,
                    error_code=error_code,
                    rejected=view.rejected or None,
                )
            return change_id

    async def log(self, changes, origin, **details):
        if not changes:
            return None
        change_id = uuid4().hex
        # A manager may update/delete the same entry repeatedly. Undo needs its state
        # before this pass and its final state, rather than intermediate writes.
        merged = {}
        for change in changes:
            namespace = tuple(change["namespace"])
            identity = namespace, change["key"]
            if identity in merged:
                merged[identity]["after"] = change["after"]
            else:
                merged[identity] = {**change, "scope": self.scopes.get(namespace)}
        entries = [change for change in merged.values() if change["before"] != change["after"]]
        if not entries:
            return None
        record = {"id": change_id, "at": self.timing.now(), "origin": origin, "changes": entries}
        record.update({key: value for key, value in details.items() if value is not None})
        await self.changes.append(record)
        return change_id

    async def undo(self):
        """Restore the entries changed by the latest automatic consolidation in these scopes."""
        async with self.lock:
            history = await self.changes.read()
            undone = {item.get("undoes") for item in history if item.get("origin") == "undo"}
            target = next(
                (
                    item
                    for item in reversed(history)
                    if item.get("origin") == "consolidate"
                    and item.get("id") not in undone
                    and item.get("changes")
                    and all(tuple(c["namespace"]) in self.scopes for c in item["changes"])
                ),
                None,
            )
            if target is None:
                raise StorageError("memory_undo_empty", "No automatic memory update to undo")
            view = self.store.recording()
            try:
                await view.restore(target["changes"])
            finally:
                complete = len(view.changes) == len(target["changes"])
                await self.log(
                    view.changes,
                    "undo",
                    undoes=target["id"] if complete else None,
                    attempted_undo=target["id"] if not complete else None,
                )
            return {
                "undone": target["id"],
                "memory_ids": [change["key"] for change in target["changes"]],
            }

    async def collect(self, *, cancel=False):
        tasks = tuple(self.tasks.values())
        if cancel:
            for task in tasks:
                if not task.done():
                    task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def aclose(self):
        self.closed = True
        await self.collect(cancel=True)

    async def target(self, memory_id, scope=None):
        found = []
        for namespace, bound_scope in self.scopes.items():
            if scope is not None and scope != bound_scope:
                continue
            snapshot = await self.store.snapshot(namespace)
            if memory_id in snapshot["records"]:
                found.append((namespace, memory_id))
        if len(found) != 1:
            raise ToolError("memory_target_ambiguous", "Choose one existing scoped memory")
        return found[0]

    async def write(self, origin, namespace, changes):
        async with self.lock:
            view = self.store.recording()
            try:
                await view.apply(namespace, changes)
            finally:
                await self.log(view.changes, origin)

    async def edit(self, memory_id, content, *, scope=None):
        namespace, key = await self.target(memory_id, scope)
        await self.write(
            "edit", namespace, {key: {"kind": "Memory", "content": {"content": content}}}
        )
        return key

    async def remove(self, memory_id, *, scope=None):
        namespace, key = await self.target(memory_id, scope)
        await self.write("delete", namespace, {key: None})
        return key

    async def clear(self):
        for namespace in self.scopes:
            snapshot = await self.store.snapshot(namespace)
            if snapshot["records"]:
                await self.write("clear", namespace, {key: None for key in snapshot["records"]})

    async def tool_operation(self, context, namespace, operate):
        """Run a LangMem tool against a recording view and log what it changed."""
        if self.closed or self.mode == "off" or context.agent_id != "lead":
            raise ToolError("memory_disabled", "Memory tools are disabled for this execution")
        if namespace not in self.scopes:
            raise ToolError("memory_scope_denied", "Memory tool namespace is outside this session")
        async with self.lock:
            view = self.store.recording()
            try:
                result = await operate(view)
            finally:
                # Cancellation may arrive after an owned disk write finishes.
                await self.log(
                    view.changes, "tool", session_id=context.session_id, run_id=context.run_id
                )
            return result, view.changes
