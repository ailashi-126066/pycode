"""Progress guard: a repeated action on an unchanged workspace, or a workspace changed back.

pycode's own feature (Pi has none). Workspace state is the snapshot tree id, so a call
repeated after a file changed is progress, and a tree id seen before is an oscillation.
"""

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class ProgressGuard:
    # The current workspace tree; None until a snapshot exists, and then nothing is checked.
    tree_id: str | None = None
    actions: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    trees: Counter[str] = field(default_factory=Counter)
    # The detection not yet acted on: the first is a reminder, a later one ends the run.
    evidence: str | None = None
    reminded: bool = False


def enter_tree(guard: ProgressGuard, tree_id: str) -> int:
    """Make tree_id current; returns how many times the workspace has arrived at it."""
    if tree_id != guard.tree_id:
        guard.tree_id = tree_id
        guard.trees[tree_id] += 1
    return guard.trees[tree_id]


def observe_call(
    guard: ProgressGuard,
    threshold: int,
    name: str,
    arguments_hash: str,
    *,
    failed: bool,
    file_snapshot: dict | None,
) -> None:
    """Record one settled call; repeated failures stay with repeated_tool_failure."""
    if file_snapshot is not None:
        enter_tree(guard, file_snapshot["before"])
    tree_id = guard.tree_id
    if tree_id is None:
        return
    if not failed:
        action = (name, arguments_hash, tree_id)
        guard.actions[action] += 1
        if guard.actions[action] >= threshold:
            guard.evidence = (
                f"`{name}` ran {guard.actions[action]} times with the same arguments"
                f" on unchanged workspace tree {tree_id}"
            )
    if file_snapshot is not None and file_snapshot["after"] != tree_id:
        after = file_snapshot["after"]
        if enter_tree(guard, after) >= threshold:
            guard.evidence = (
                f"`{name}` changed the workspace back to tree {after}, seen before"
                f" (from tree {tree_id})"
            )
