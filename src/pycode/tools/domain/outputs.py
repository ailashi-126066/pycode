"""Stable output references and bounded readback results; no filesystem paths."""

from dataclasses import dataclass
from typing import Literal

from pycode.kernel import PreviewMode

# Control fields later stages read from details; they survive output externalization.
CONTROL_DETAILS = frozenset({"loaded_tools", "executed", "side_effects_uncertain", "task_board"})


@dataclass(frozen=True)
class OutputMetadata:
    output_id: str
    session_id: str
    call_id: str
    operation_id: str
    byte_count: int
    line_count: int
    sha256: str
    preview: str
    mode: PreviewMode
    storage: Literal["file", "inline"]
    status: Literal["completed", "interrupted"] = "completed"
    source_entry_id: str | None = None
    is_error: bool = False
    exit_code: int | None = None
    artifact: bool = False
    artifact_sha256: str | None = None
    preview_sha256: str = ""


@dataclass(frozen=True)
class OutputSlice:
    output_id: str
    text: str
    start_line: int
    end_line: int
    has_more: bool
    next_offset: int
    next_byte_offset: int
