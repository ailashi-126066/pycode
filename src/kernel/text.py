# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/truncate.ts
# Adaptation: UTF-8 bytes, Python dataclasses, whole-line head and partial tail semantics.
# 参照 https://github.com/anomalyco/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/core/src/util/token.ts
# Adaptation: explicit approximate token units; provider-reported usage is kept separately.
"""Pure line/byte truncation and conservative token estimates."""

import math
from dataclasses import dataclass
from typing import Literal

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
PreviewMode = Literal["head", "tail"]


@dataclass(frozen=True)
class Truncation:
    text: str
    truncated: bool
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    partial_line: bool = False
    first_line_exceeds_limit: bool = False
    truncated_by: Literal["lines", "bytes"] | None = None


# Pi: formatSize.
def format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def truncate(
    text: str, mode: PreviewMode, *, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES
) -> Truncation:
    if mode not in {"head", "tail"} or max_lines < 1 or max_bytes < 1:
        raise ValueError("Invalid preview limits or mode")
    total_bytes = len(text.encode("utf-8"))
    total_lines = line_count(text)
    if total_bytes <= max_bytes and total_lines <= max_lines:
        return Truncation(text, False, total_lines, total_bytes, total_lines, total_bytes)
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    if mode == "head" and len(lines[0].encode()) > max_bytes:
        return Truncation(
            "",
            True,
            total_lines,
            total_bytes,
            0,
            0,
            first_line_exceeds_limit=True,
            truncated_by="bytes",
        )
    candidates = lines if mode == "head" else reversed(lines)
    selected = []
    size = 0
    partial = False
    truncated_by: Literal["lines", "bytes"] = "lines"
    for line in candidates:
        if len(selected) == max_lines:
            break
        encoded = line.encode("utf-8")
        needed = len(encoded) + (1 if selected else 0)
        if size + needed > max_bytes:
            truncated_by = "bytes"
            if mode == "tail" and not selected:
                start = len(encoded) - max_bytes
                while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
                    start += 1
                selected.append(encoded[start:].decode())
                size = len(selected[0].encode())
                partial = True
            break
        selected.append(line)
        size += needed
    if mode == "tail":
        selected.reverse()
    preview = "\n".join(selected)
    return Truncation(
        preview,
        True,
        total_lines,
        total_bytes,
        len(selected),
        len(preview.encode()),
        partial,
        truncated_by=truncated_by,
    )


def estimate_text(text: str) -> int:
    """Fallback budget, not tokenizer counts: ASCII / 4, non-ASCII by UTF-8 bytes."""
    ascii_characters = sum(character.isascii() for character in text)
    non_ascii_bytes = len(text.encode("utf-8")) - ascii_characters
    return math.ceil(ascii_characters / 4) + non_ascii_bytes
