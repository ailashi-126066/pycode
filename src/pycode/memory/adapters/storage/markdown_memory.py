"""A LangGraph BaseStore over one Markdown file per memory, with get/put/search only."""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from langgraph.store.base import BaseStore, GetOp, Item, PutOp, SearchItem, SearchOp

from pycode.kernel import FileLease, StorageError, ToolError, atomic_write, owned_io
from pycode.memory.domain.memory import (
    MemoryNote,
    MemoryRecord,
    check_memory_write,
    check_slug,
    format_note,
    note_fields,
    parse_note,
)

MODES = frozenset({"auto", "off"})


@contextmanager
def memory_lease(root):
    path = root / "memory.lock"
    root.mkdir(parents=True, exist_ok=True)
    lease = FileLease(path)
    try:
        for attempt in range(50):
            try:
                lease.acquire()
                break
            except StorageError as error:
                if error.code != "session_busy" or attempt == 49:
                    raise
                time.sleep(0.01)
        yield
    finally:
        lease.close()


class MarkdownMemoryStore(BaseStore):
    """Only configuration supplies the scope roots; model namespaces never become paths."""

    supports_ttl = False

    def __init__(self, roots, *, rejected=None):
        self.roots = {namespace: Path(root) for namespace, root in roots.items()}
        # When set, filtered writes are skipped and listed here instead of raising.
        self.rejected = rejected

    def lenient(self):
        """A view for background consolidation: filtered entries are skipped, not fatal."""
        return MarkdownMemoryStore(self.roots, rejected=[])

    def root(self, namespace):
        if namespace not in self.roots:
            raise ToolError("memory_scope_denied", "Memory namespace is outside the allowed scopes")
        return self.roots[namespace]

    def read(self, namespace):
        """{slug: (description, updated, content)} for one scope."""
        root = self.root(namespace)
        if not root.is_dir():
            return {}
        notes = {}
        try:
            for path in sorted(root.glob("*.md")):
                description, updated, content = parse_note(path.read_text(encoding="utf-8"), path)
                # A hand-written file without "updated" sorts by its modification time.
                if not updated:
                    mtime = path.stat().st_mtime
                    updated = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                notes[path.stem] = (description, updated, content)
        except (OSError, UnicodeError) as error:
            raise StorageError("memory_io_error", "Memory files could not be read") from error
        return notes

    def records(self, scopes):
        return [
            MemoryRecord(scope, slug, description, content, updated)
            for namespace, scope in scopes.items()
            for slug, (description, updated, content) in self.read(namespace).items()
        ]

    def write(self, namespace, slug, value):
        """Create, replace or (value None) delete one memory file, holding the scope lease."""
        check_slug(slug)
        root = self.root(namespace)
        path = root / f"{slug}.md"
        try:
            with memory_lease(root):
                if value is None:
                    path.unlink(missing_ok=True)
                    return
                description, content = note_fields(value)
                count = len(self.read(namespace)) + (not path.exists())
                try:
                    check_memory_write(description, content, count)
                except ToolError as error:
                    if self.rejected is None:
                        raise
                    self.rejected.append({"key": slug, "reason": error.code})
                    return
                updated = datetime.now(timezone.utc).isoformat()
                atomic_write(path, format_note(description, updated, content).encode())
        except OSError as error:
            raise StorageError("memory_io_error", "Memory file could not be written") from error

    @staticmethod
    def item(namespace, slug, note, factory=Item):
        description, updated, content = note
        # Store Manager reads values back as {kind, content} of its schema.
        value = {
            "kind": MemoryNote.__name__,
            "content": {"description": description, "content": content},
        }
        return factory(
            namespace=namespace, key=slug, value=value, created_at=updated, updated_at=updated
        )

    def dispatch(self, op):
        if isinstance(op, GetOp):
            note = self.read(op.namespace).get(op.key)
            return self.item(op.namespace, op.key, note) if note is not None else None
        if isinstance(op, PutOp):
            self.write(op.namespace, op.key, op.value)
            return None
        if isinstance(op, SearchOp):
            # No keyword or vector matching: a scope holds at most 50 memories, newest first.
            found = [
                (namespace, slug, note)
                for namespace in self.roots
                if namespace[: len(op.namespace_prefix)] == op.namespace_prefix
                for slug, note in self.read(namespace).items()
            ]
            found.sort(key=lambda row: row[2][1], reverse=True)
            return [
                self.item(namespace, slug, note, SearchItem)
                for namespace, slug, note in found[op.offset : op.offset + op.limit]
            ]
        raise TypeError(f"Memory store supports get, put and search, not {type(op).__name__}")

    def batch(self, ops):
        return [self.dispatch(op) for op in ops]

    async def abatch(self, ops):
        return await owned_io(self.batch, list(ops))

    async def arecords(self, scopes):
        return await owned_io(self.records, scopes)

    async def awrite(self, namespace, slug, value):
        await owned_io(self.write, namespace, slug, value)

    async def get_mode(self, namespace):
        def read():
            path = self.root(namespace) / "settings.json"
            if not path.exists():
                return "auto"
            try:
                mode = json.loads(path.read_bytes())["mode"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise StorageError(
                    "invalid_memory_mode", "Memory settings cannot be read"
                ) from error
            if mode not in MODES:
                raise StorageError("invalid_memory_mode", "Memory settings cannot be read")
            return mode

        return await owned_io(read)

    async def set_mode(self, namespace, mode):
        def write():
            root = self.root(namespace)
            with memory_lease(root):
                atomic_write(root / "settings.json", json.dumps({"mode": mode}).encode())

        await owned_io(write)
