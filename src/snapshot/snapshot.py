# Pi 没有，pycode 新增
"""Tool file snapshots and one-step, explicitly reversible history navigation."""

from typing import TYPE_CHECKING, cast

from ..kernel import (
    AfterToolCallResult,
    EntryDraft,
    SessionEntry,
    SessionTree,
    SnapshotError,
    StorageError,
)
from .store import GitSnapshotStore
from .types import Snapshot

if TYPE_CHECKING:
    from ..session import SqliteSessionStore


def revert_targets(snapshot, target, mode):
    """Resolve a target without I/O; ordinary targets exclude their own changes."""
    target = None if target in {None, "root"} else target
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    positions = {entry.id: index for index, entry in enumerate(path)}
    if target is not None and target not in positions:
        raise StorageError("revert_branch_conflict", "Target must be on the current branch")
    entry = path[positions[target]] if target is not None else None
    undo = entry is not None and entry.type == "custom" and entry.data.get("kind") == "revert"
    if undo and target != snapshot.leaf_id:
        raise StorageError("revert_branch_conflict", "Only the latest revert can be undone")
    leaf = cast(SessionEntry, entry).data["previous_leaf"] if undo else target
    if leaf is not None and leaf not in {item.id for item in snapshot.entries}:
        raise StorageError("revert_branch_conflict", "The original dialogue branch is unavailable")
    records = (
        (cast(SessionEntry, entry),)
        if undo
        else path[positions[target] + 1 if target is not None else 0 :]
    )
    targets, expected = {}, {}
    if mode in {"code", "both"}:
        for record in records:
            saved = None
            if record.message is not None and record.message.tool_result is not None:
                saved = record.message.tool_result.details.get("file_snapshot")
            elif record.type == "custom" and record.data.get("kind") == "revert":
                saved = record.data.get("file_snapshot")
            if saved is not None:
                for name in saved["paths"]:
                    targets.setdefault(name, saved["before"])
                    expected[name] = saved["after"]
    return leaf, targets, expected


class SnapshotService:
    def __init__(self, history: "SqliteSessionStore", store: GitSnapshotStore):
        self.history = history
        self.store = store
        self._before = {}

    async def before_tool_call(self, run, spec, call):
        if spec.category in {"write", "command"}:
            self._before[call.id] = await self.store.capture()

    async def after_tool_call(self, run, call, result) -> AfterToolCallResult | None:
        before = self._before.pop(call.id, None)
        if before is None:
            return None
        after = await self.store.capture()
        if after is None:
            raise SnapshotError("snapshot_unavailable", "Snapshot repository disappeared")
        paths = [change.path for change in await self.store.diff(before.tree_id, after.tree_id)]
        return {
            "details": {
                **result.details,
                "file_snapshot": {
                    "before": before.tree_id,
                    "after": after.tree_id,
                    "paths": paths,
                },
            }
        }

    async def changed_paths(self, target, mode):
        _, targets, _ = revert_targets(await self.history.snapshot(), target, mode)
        return tuple(sorted(targets))

    async def record(self, target, mode, previous, resulting, before, after, paths):
        data = {
            "kind": "revert",
            "target": target,
            "mode": mode,
            "previous_leaf": previous,
            "resulting_leaf": resulting,
            "paths": paths,
        }
        if before is not None:
            data["file_snapshot"] = {
                "before": before.tree_id,
                "after": after.tree_id,
                "paths": paths,
            }
        return await self.history.append_entry(EntryDraft("custom", data=data))

    async def revert(self, target, mode="dialogue"):
        snapshot = await self.history.snapshot()
        leaf, targets, expected = revert_targets(snapshot, target, mode)
        before = after = None
        if mode in {"code", "both"}:
            before = await self.store.capture()
            if before is None:
                raise SnapshotError("snapshot_unavailable", "File snapshots require a Git project")
            conflicts = []
            for name, tree_id in expected.items():
                version = await self.store.get(tree_id)
                if before.files.get(name) != version.files.get(name):
                    conflicts.append(name)
            if conflicts:
                raise SnapshotError("revert_conflict", "External changes: " + ", ".join(conflicts))
        try:
            for name, tree_id in sorted(targets.items()):
                await self.store.write_path(name, tree_id)
        except (OSError, SnapshotError) as error:
            try:
                after = await self.store.capture()
                if after is None:
                    raise SnapshotError("snapshot_unavailable", "Snapshot repository disappeared")
                changed = [
                    name
                    for name in targets
                    if cast(Snapshot, before).files.get(name)
                    != cast(Snapshot, after).files.get(name)
                ]
                record = await self.record(
                    target, mode, snapshot.leaf_id, snapshot.leaf_id, before, after, changed
                )
            except Exception as record_error:
                raise SnapshotError(
                    "revert_partial",
                    "Files may be partially restored; recovery record could not be saved",
                ) from record_error
            raise SnapshotError(
                "revert_partial",
                f"Restored {len(changed)}/{len(targets)} files; undo revert {record.id}",
            ) from error
        if before is not None:
            try:
                after = await self.store.capture()
            except (OSError, SnapshotError) as error:
                raise SnapshotError(
                    "revert_partial", "Files restored but final snapshot could not be saved"
                ) from error
            if after is None:
                raise SnapshotError(
                    "revert_partial", "Files restored but final snapshot unavailable"
                )
        resulting = leaf if mode in {"dialogue", "both"} else snapshot.leaf_id
        changed = [
            name
            for name in targets
            if cast(Snapshot, before).files.get(name) != cast(Snapshot, after).files.get(name)
        ]
        try:
            if mode in {"dialogue", "both"}:
                await self.history.branch(leaf)
            return await self.record(
                target, mode, snapshot.leaf_id, resulting, before, after, changed
            )
        except Exception as error:
            raise SnapshotError(
                "revert_partial", "Revert state changed but recovery record could not be saved"
            ) from error
