"""Exit after persisted revert boundaries; use real JSONL and Git stores."""

import asyncio
import json
import os
import sys
from pathlib import Path

from pycode.kernel import SessionHeader
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.session.domain.revert_state import decode_plan
from pycode.session.services.revert import RevertService
from pycode.snapshot.adapters.git_tree import GitSnapshotStore
from tests.support.requests import TestTiming


class CrashHistory(SqliteSessionStore):
    def __init__(self, directory, header, stage):
        super().__init__(directory, header, TestTiming())
        self.stage = stage

    async def branch(self, leaf_id):
        if self.stage == "before-branch":
            os._exit(76)
        await super().branch(leaf_id)
        if self.stage == "after-branch":
            os._exit(76)

    async def append_entry(self, draft):
        entry = await super().append_entry(draft)
        if self.stage == "after-start" and draft.data.get("kind") == "revert_started":
            os._exit(76)
        if self.stage == "after-reminder" and draft.type == "custom_message":
            os._exit(76)
        return entry


async def main():
    root, snapshots, sessions, plan_path, stage = sys.argv[1:]
    raw = await asyncio.to_thread(Path(plan_path).read_text, encoding="utf-8")
    plan = decode_plan(json.loads(raw))
    recovering = stage.startswith("compensate-")
    history = CrashHistory(
        Path(sessions), SessionHeader("s1", root, "t"), stage.removeprefix("compensate-")
    )
    service = RevertService(history, GitSnapshotStore(Path(root), Path(snapshots)))
    if recovering:
        await service.recover(plan.transaction_id, compensate=True)
    else:
        await service.apply(plan)
    await history.aclose()


if __name__ == "__main__":
    asyncio.run(main())
