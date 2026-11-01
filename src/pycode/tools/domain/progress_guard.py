"""Progress guard: the same call with the same result again and again, or a workspace
changed back to an earlier tree.

pycode's own feature (Pi has none). 11.1 section 40: a fingerprint is the action (tool
name, arguments hash) plus the result (error, error code, exit code and a hash of the
output with digit runs replaced by #), so polling that sees new output is not a repeat.
A read-only call only counts while the same fingerprint repeats back to back. A step
start, an adopted compaction or the user's "continue" clears the repeat and tree counts.
Identical failures stay with repeated_tool_failure (section 11), so they are not counted.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from pycode.kernel import ToolResult, data_hash

Fingerprint = tuple[str, str, bool, str | None, int | None, str]


@dataclass
class ProgressGuard:
    # The current workspace tree; None until a snapshot exists.
    tree_id: str | None = None
    actions: Counter[Fingerprint] = field(default_factory=Counter)
    trees: Counter[str] = field(default_factory=Counter)
    # The last read-only fingerprint and how often it was seen back to back.
    last_read: Fingerprint | None = None
    read_streak: int = 0
    # The detection not yet acted on: the first is a reminder, a later one asks the user.
    evidence: str | None = None
    reminded: bool = False


def enter_tree(guard: ProgressGuard, tree_id: str) -> int:
    """Make tree_id current; returns how many times the workspace has arrived at it."""
    if tree_id != guard.tree_id:
        guard.tree_id = tree_id
        guard.trees[tree_id] += 1
    return guard.trees[tree_id]


def fingerprint(name: str, arguments_hash: str, result: ToolResult) -> Fingerprint:
    output = data_hash(re.sub(r"\d+", "#", result.text))
    return (name, arguments_hash, result.is_error, result.error_code, result.exit_code, output)


def reset_repeats(guard: ProgressGuard) -> None:
    guard.actions.clear()
    # The current tree counts as just arrived at, the same as at run start.
    guard.trees = Counter() if guard.tree_id is None else Counter([guard.tree_id])
    guard.last_read, guard.read_streak = None, 0
    guard.evidence = None


def observe_call(
    guard: ProgressGuard,
    threshold: int,
    name: str,
    arguments_hash: str,
    result: ToolResult,
    *,
    read_only: bool,
) -> None:
    """Record one settled call; read_only marks a successful call of a read-category tool."""
    seen = fingerprint(name, arguments_hash, result)
    if read_only:
        if seen == guard.last_read:
            guard.read_streak += 1
        else:
            guard.last_read, guard.read_streak = seen, 1
        if guard.read_streak >= threshold:
            guard.evidence = f"`{name}` returned the same result {guard.read_streak} times in a row"
        return
    # Any other call ends a run of identical reads.
    guard.last_read, guard.read_streak = None, 0
    if not result.is_error:
        guard.actions[seen] += 1
        if guard.actions[seen] >= threshold:
            guard.evidence = (
                f"`{name}` ran {guard.actions[seen]} times with the same arguments and result"
            )
    file_snapshot = result.details.get("file_snapshot")
    if file_snapshot is None:
        return
    enter_tree(guard, file_snapshot["before"])
    before, after = file_snapshot["before"], file_snapshot["after"]
    if after != before and enter_tree(guard, after) >= threshold:
        guard.evidence = (
            f"`{name}` changed the workspace back to tree {after}, seen before (from tree {before})"
        )
