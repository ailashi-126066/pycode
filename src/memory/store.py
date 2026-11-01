# Pi 没有，pycode 新增
"""One memory per Markdown file, read and written through a LangGraph BaseStore.

Frontmatter holds description and updated; the body is the memory. The store implements
only the get, put and search operations LangMem uses.
"""

import json
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from langgraph.store.base import BaseStore, GetOp, Item, PutOp, SearchItem, SearchOp

from ..kernel import (
    SECRET_PATTERNS,
    FileLease,
    StorageError,
    ToolError,
    atomic_write,
    owned_io,
)
from .types import MemoryNote, MemoryRecord

# Redaction patterns shared with tool failure text; they match a credential and its value.
# Write rejection is stricter: any credential-looking assignment keeps the entry out.
WRITE_SECRET_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{16,}",
    r"(?i)(api[_ -]?key|secret|token|password)[\"' ]*\s*[:=]",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"(?i)aws_secret_access_key",
    SECRET_PATTERNS[2],
)
MAX_ENTRY_CHARS = 2000
MAX_ENTRIES = 50
# The slug is the file name; same rule as a Skill name (LangMem's UUID ids satisfy it).
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SLUG_LENGTH = 64


def check_slug(slug):
    if not isinstance(slug, str) or len(slug) > MAX_SLUG_LENGTH or not SLUG.fullmatch(slug):
        raise ToolError("invalid_memory_id", "Memory ids use lowercase a-z, 0-9 and hyphens")


def note_fields(value):
    """LangMem manage tools write {content: note}; Store Manager writes {kind, content: note}."""
    note = value["content"]
    if not isinstance(note, dict) or set(note) != {"description", "content"}:
        raise ToolError("invalid_memory_content", "A memory needs a description and content")
    return note["description"], note["content"]


def check_memory_write(description, content, entry_count):
    """Policy for every new or changed entry; entry_count includes the written entry."""
    if not description.strip() or "\n" in description:
        raise ToolError("invalid_memory_content", "The description is one non-empty line")
    if len(description) + len(content) > MAX_ENTRY_CHARS:
        raise ToolError(
            "memory_entry_too_long", f"A memory entry is limited to {MAX_ENTRY_CHARS} characters"
        )
    if entry_count > MAX_ENTRIES:
        raise ToolError(
            "memory_limit_reached", f"A memory scope is limited to {MAX_ENTRIES} entries"
        )
    if any(
        re.search(pattern, text)
        for pattern in WRITE_SECRET_PATTERNS
        for text in (description, content)
    ):
        raise ToolError("memory_secret_rejected", "Secrets must not enter long-term memory")


def format_note(description, updated, content):
    meta = yaml.safe_dump(
        {"description": description, "updated": updated},
        allow_unicode=True,
        sort_keys=False,
        width=1_000_000,  # keep the description on one line
    )
    return f"---\n{meta}---\n{content}\n"


def parse_note(raw, path):
    lines = raw.splitlines(keepends=True)
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or closing is None:
        raise StorageError("memory_corrupt", f"Memory file needs frontmatter: {path}")
    try:
        meta = yaml.safe_load("".join(lines[1:closing]))
    except yaml.YAMLError as error:
        raise StorageError("memory_corrupt", f"Memory frontmatter is invalid: {path}") from error
    if not isinstance(meta, dict) or not isinstance(meta.get("description"), str):
        raise StorageError("memory_corrupt", f"Memory file needs a description: {path}")
    # YAML reads an unquoted timestamp as a date; "updated" may also be missing ("").
    updated = meta.get("updated", "")
    updated = updated.isoformat() if isinstance(updated, date) else str(updated)
    return meta["description"], updated, "".join(lines[closing + 1 :]).strip()


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
