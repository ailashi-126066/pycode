"""Memory scopes, namespaces, stored entry structure and the write-filter policy."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pycode.kernel import SECRET_PATTERNS, ToolError, json_dumps

Scope = Literal["user", "project"]
SCOPES = ("user", "project")

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
MAX_ENTRIES = 200
Identity = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]


def scope_namespace(scope, identity):
    return ("memories", scope, identity)


class StoredMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def check(self):
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Memory times require a timezone")
        if not isinstance(self.value.get("kind"), str) or not isinstance(
            self.value.get("content"), dict
        ):
            raise ValueError("Memory value requires kind and content")
        return self


class MemorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    namespace: tuple[Identity, ...]
    revision: Annotated[int, Field(ge=0)]
    records: dict[Identity, StoredMemory]


def normalize_value(value):
    """LangMem manage tools write {content: text}; Store Manager writes {kind, content}."""
    if isinstance(value, dict) and set(value) == {"content"}:
        content = value["content"]
        if isinstance(content, str):
            return {"kind": "Memory", "content": {"content": content}}
        if isinstance(content, dict):
            if isinstance(content.get("kind"), str) and isinstance(content.get("content"), dict):
                return {"kind": content["kind"], "content": content["content"]}
            return {"kind": "Memory", "content": content}
    if (
        isinstance(value, dict)
        and isinstance(value.get("kind"), str)
        and isinstance(value.get("content"), dict)
    ):
        return {"kind": value["kind"], "content": value["content"]}
    raise ToolError("invalid_memory_content", "Memory content must be text or {kind, content}")


def memory_text(value):
    """Display current text memories or structured content without legacy conversions."""
    content = value["content"]
    if isinstance(content.get("content"), str):
        return content["content"]
    return json_dumps(content, ensure_ascii=False, sort_keys=True)


def content_strings(value):
    """Inspect strings before JSON escaping, including non-display metadata fields."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from content_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from content_strings(child)


def check_memory_write(value, entry_count):
    """Policy for every new or changed entry; entry_count includes the written entry."""
    serialized = json_dumps(value, ensure_ascii=False)
    wrapper_chars = len(json_dumps({"kind": "Memory", "content": {"content": ""}}))
    if (
        len(memory_text(value)) > MAX_ENTRY_CHARS
        or len(serialized) > MAX_ENTRY_CHARS + wrapper_chars
    ):
        raise ToolError(
            "memory_entry_too_long", f"A memory entry is limited to {MAX_ENTRY_CHARS} characters"
        )
    if entry_count > MAX_ENTRIES:
        raise ToolError(
            "memory_limit_reached", f"A memory scope is limited to {MAX_ENTRIES} entries"
        )
    candidates = (serialized, *content_strings(value))
    if any(re.search(pattern, text) for pattern in WRITE_SECRET_PATTERNS for text in candidates):
        raise ToolError("memory_secret_rejected", "Secrets must not enter long-term memory")


@dataclass(frozen=True)
class MemoryRecord:
    """One stored entry as shown to users and injected into a task."""

    namespace: tuple[str, ...]
    memory_id: str
    scope: Scope
    kind: str
    content: str
    created_at: str
    updated_at: str


def memory_record(namespace, key, record, scope):
    return MemoryRecord(
        tuple(namespace),
        key,
        scope,
        record["value"]["kind"],
        memory_text(record["value"]),
        record["created_at"],
        record["updated_at"],
    )
