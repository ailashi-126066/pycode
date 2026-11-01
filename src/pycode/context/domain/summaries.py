"""Immutable summary jobs, provenance and settlement records; no framework types."""

from dataclasses import dataclass, field
from typing import Any, Literal

from pycode.context.domain.context_budget import InputBudget
from pycode.kernel import (
    Message,
    ModelSelection,
    ProjectedContext,
    SessionSnapshot,
    ToolSpec,
    UsageRecord,
)


@dataclass(frozen=True)
class SummarySource:
    job_id: str
    session_id: str
    source_leaf_id: str
    source_entry_ids: tuple[str, ...]
    first_kept_entry_id: str
    previous_compaction_id: str | None
    prefix_hash: str
    config_hash: str
    # Pi: CompactionPreparation — previousSummary, messagesToSummarize, turnPrefixMessages,
    # fileOps and tokensBefore.
    previous_summary: str | None
    messages: tuple[Message, ...]
    turn_prefix_messages: tuple[Message, ...]
    read_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    tokens_before: int
    selection: ModelSelection


@dataclass(frozen=True)
class SummaryCompletion:
    text: str
    usage: tuple[UsageRecord, ...] = ()


@dataclass(frozen=True)
class SummaryObject:
    job_id: str
    source_hash: str
    status: Literal["ready", "failed", "interrupted"]
    text: str = ""
    usage: tuple[UsageRecord, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class SummaryReport:
    context_tokens: int
    applied_job_id: str | None = None
    protected_entry_ids: tuple[str, ...] = ()
    status: str = "idle"
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SummarySettings:
    selection: ModelSelection
    tools: tuple[ToolSpec, ...]
    budget: InputBudget
    keep_recent_tokens: int = 20000

    def __post_init__(self):
        if self.keep_recent_tokens < 1:
            raise ValueError("Recent-message budget must be positive")


@dataclass(frozen=True)
class SummaryCheck:
    snapshot: SessionSnapshot
    projection: ProjectedContext
    config_hash: str
    tokens: int
    start_generation: bool
    apply_needed: bool
    manual: bool
    wait_for_capacity: bool
