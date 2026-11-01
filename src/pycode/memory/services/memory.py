"""Memory lifecycle: the prompt index, consolidation after compaction and user edits."""

import asyncio
import contextvars
import logging
from uuid import uuid4

from pycode.context import retained_context_entries
from pycode.kernel import Message, SessionTree, StorageError, ToolError, plain_data
from pycode.memory.domain.memory import memory_index
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
            summary = entry.data["summary"][:SUMMARY_CHARS]
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
    def __init__(self, history, store, scopes, *, updater: MemoryUpdater | None = None, mode=None):
        # scopes: {namespace: "user" | "project"}; the project scope exists only when trusted.
        self.history, self.store, self.scopes = history, store, scopes
        self.updater = updater
        self.mode = mode
        self.user_namespace = next(ns for ns, scope in scopes.items() if scope == "user")
        self.task: asyncio.Task | None = None
        self.closed = False

    async def initialize(self):
        if self.mode is None:
            self.mode = await self.store.get_mode(self.user_namespace)

    async def set_mode(self, mode):
        if mode not in {"auto", "off"}:
            raise StorageError("invalid_memory_mode", "Choose auto or off")
        if mode == "off":
            await self.collect(cancel=True)
        await self.store.set_mode(self.user_namespace, mode)
        self.mode = mode

    async def records(self):
        return await self.store.arecords(self.scopes)

    async def list(self):
        return {"mode": self.mode, "records": plain_data(await self.records())}

    def prompt_index(self):
        """The system prompt section; empty when memory is off or nothing is saved."""
        if self.mode == "off":
            return ""
        return memory_index(self.store.records(self.scopes))

    async def compacted(self):
        """Consolidate in the background after a compaction; at most one runs at a time."""
        if self.closed or self.mode == "off" or self.updater is None:
            return
        if self.task is not None and not self.task.done():
            return
        self.task = asyncio.create_task(
            self.background("memory-compaction-" + uuid4().hex),
            name="pycode-memory-consolidate",
            context=contextvars.Context(),
        )

    async def background(self, run_id):
        try:
            await self.consolidate(run_id)
        except Exception as error:  # noqa: BLE001 - background work must not affect chat
            logger.warning(
                "Memory consolidation failed: %s", getattr(error, "code", type(error).__name__)
            )

    async def consolidate(self, run_id):
        """One LangMem Store Manager pass per scope; filtered entries are skipped."""
        if self.updater is None:
            raise StorageError("memory_unavailable", "No memory updater is configured")
        snapshot = await self.history.snapshot()
        summary, messages = consolidation_source(snapshot)
        if not summary and not messages:
            return
        view = self.store.lenient()
        for namespace in self.scopes:
            await self.updater.consolidate(
                view, namespace, summary, messages, snapshot.header.session_id, run_id
            )
        if view.rejected:
            logger.info("Memory consolidation skipped %d filtered entries", len(view.rejected))

    async def collect(self, *, cancel=False):
        task, self.task = self.task, None
        if task is None:
            return
        if cancel:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def aclose(self):
        self.closed = True
        await self.collect(cancel=True)

    async def target(self, memory_id, scope=None):
        found = [
            record
            for record in await self.records()
            if record.memory_id == memory_id and scope in {None, record.scope}
        ]
        if len(found) != 1:
            raise ToolError("memory_target_ambiguous", "Choose one existing scoped memory")
        return found[0]

    def namespace(self, scope):
        return next(ns for ns, bound in self.scopes.items() if bound == scope)

    async def edit(self, memory_id, content, *, scope=None):
        record = await self.target(memory_id, scope)
        value = {"content": {"description": record.description, "content": content}}
        await self.store.awrite(self.namespace(record.scope), memory_id, value)
        return memory_id

    async def remove(self, memory_id, *, scope=None):
        record = await self.target(memory_id, scope)
        await self.store.awrite(self.namespace(record.scope), memory_id, None)
        return memory_id

    async def clear(self):
        for record in await self.records():
            await self.store.awrite(self.namespace(record.scope), record.memory_id, None)
