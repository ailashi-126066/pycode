# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/edit.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/edit-diff.ts
"""Exact, unique text replacements in one file, applied atomically."""

import difflib
import json
import sys

from deepagents.backends import CompositeBackend

from pycode.kernel import ToolContext, ToolError, ToolResult, ToolSpec
from pycode.tools.adapters.builtin.path_utils import backend_error, real_path, resolve_to_cwd
from pycode.tools.adapters.file_mutation_queue import with_file_mutation_queue


# Pi: prepareEditArguments. Some models send edits as a JSON string, one object,
# or the legacy top-level oldText/newText pair.
def prepare_edit_arguments(arguments: dict) -> dict:
    edits = arguments.get("edits")
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except json.JSONDecodeError:
            pass
    if isinstance(edits, dict):
        edits = [edits]
    if isinstance(arguments.get("oldText"), str) and isinstance(arguments.get("newText"), str):
        legacy = {"oldText": arguments.pop("oldText"), "newText": arguments.pop("newText")}
        edits = [*(edits if isinstance(edits, list) else ()), legacy]
    if edits is not None:
        arguments["edits"] = edits
    return arguments


def detect_line_ending(content: str) -> str:
    crlf = content.find("\r\n")
    return "\r\n" if crlf != -1 and crlf == content.find("\n") - 1 else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# Pi: applyEditsToNormalizedContent, exact matching only.
def apply_edits(content: str, edits: list[dict], path: str) -> str:
    many = len(edits) > 1
    matches = []
    for index, edit in enumerate(edits):
        name = f"edits[{index}]" if many else "the text"
        old = normalize_to_lf(edit["oldText"])
        if not old:
            raise ToolError("invalid_edit", f"{name}.oldText must not be empty in {path}.")
        count = content.count(old)
        if count == 0:
            raise ToolError(
                "edit_not_found",
                f"Could not find {name} in {path}. The old text must match exactly "
                "including all whitespace and newlines.",
            )
        if count > 1:
            raise ToolError(
                "edit_not_unique",
                f"Found {count} occurrences of {name} in {path}. The text must be unique. "
                "Please provide more context to make it unique.",
            )
        matches.append((content.index(old), len(old), normalize_to_lf(edit["newText"]), index))
    matches.sort()
    for previous, current in zip(matches, matches[1:], strict=False):
        if previous[0] + previous[1] > current[0]:
            raise ToolError(
                "edit_overlap",
                f"edits[{previous[3]}] and edits[{current[3]}] overlap in {path}. "
                "Merge them into one edit or target disjoint regions.",
            )
    replaced = content
    for start, length, new, _ in reversed(matches):
        replaced = replaced[:start] + new + replaced[start + length :]
    if replaced == content:
        raise ToolError(
            "edit_no_change",
            f"No changes made to {path}. The replacement produced identical content.",
        )
    return replaced


def first_changed_line(old: str, new: str) -> int:
    for number, (before, after) in enumerate(
        zip(old.split("\n"), new.split("\n"), strict=False), 1
    ):
        if before != after:
            return number
    return min(old.count("\n"), new.count("\n")) + 1


class Edit:
    spec = ToolSpec(
        name="edit",
        description=(
            "Edit a single file using exact text replacement. Every edits[].oldText must match "
            "a unique, non-overlapping region of the original file. If two changes affect the "
            "same block or nearby lines, merge them into one edit instead of emitting "
            "overlapping edits. Do not include large unchanged regions just to connect distant "
            "changes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit (relative or absolute)",
                },
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "One or more targeted replacements. Each edit is matched against the "
                        "original file, not incrementally. Do not include overlapping or nested "
                        "edits. If two changes touch the same block or nearby lines, merge them "
                        "into one edit instead."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {
                                "type": "string",
                                "description": (
                                    "Exact text for one targeted replacement. It must be unique "
                                    "in the original file and must not overlap with any other "
                                    "edits[].oldText in the same call."
                                ),
                            },
                            "newText": {
                                "type": "string",
                                "description": "Replacement text for this targeted edit.",
                            },
                        },
                        "required": ["oldText", "newText"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "edits"],
            "additionalProperties": False,
        },
        category="write",
        exposure="direct",
        prompt_snippet=(
            "Make precise file edits with exact text replacement, including multiple disjoint "
            "edits in one call"
        ),
        prompt_guidelines=(
            "Use edit for targeted changes. Batch changes to one file in a single call.",
            "Each edits[].oldText must exactly and uniquely match the original file. Keep "
            "matches short and non-overlapping; merge nearby changes without including large "
            "unchanged regions.",
        ),
        prepare_arguments=prepare_edit_arguments,
    )

    def __init__(self, backend: CompositeBackend) -> None:
        self.backend = backend

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        path = arguments["path"]
        target = resolve_to_cwd(self.backend, path)

        async def edit():
            page = await self.backend.aread(target, 0, sys.maxsize)
            if page.error:
                raise backend_error(page.error)
            if page.file_data is None:
                raise ToolError("not_a_file", f"{path} has no file content")
            raw = page.file_data["content"]
            # The model never includes an invisible BOM in oldText.
            bom = "\ufeff" if raw.startswith("\ufeff") else ""
            ending = detect_line_ending(raw)
            original = normalize_to_lf(raw.removeprefix(bom))
            changed = apply_edits(original, arguments["edits"], path)
            restored = changed.replace("\n", "\r\n") if ending == "\r\n" else changed
            result = await self.backend.awrite(target, bom + restored)
            if result.error:
                raise backend_error(result.error)
            patch = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    changed.splitlines(keepends=True),
                    path,
                    path,
                    n=4,
                )
            )
            return ToolResult(
                context.call_id,
                self.spec.name,
                f"Successfully replaced {len(arguments['edits'])} block(s) in {path}.",
                details={
                    "patch": patch,
                    "first_changed_line": first_changed_line(original, changed),
                },
            )

        return await with_file_mutation_queue(real_path(self.backend, target), edit)
