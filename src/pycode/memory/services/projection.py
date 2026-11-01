"""Reconstruct this feature's historical model context without opening resources."""

from pycode.context import ProjectionContribution
from pycode.kernel import Message, ProjectedEntry, clone_value


class MemoryProjection:
    name = "memory"
    incremental_safe = True
    checkpoint_keys = frozenset({"memories", "memory_task_id"})

    def contribute(self, snapshot, path, entries, checkpoint):
        pinned = []
        updates = {}
        memory_records = ()
        memory_source = None
        memory_task_id = None
        for entry in path:
            if entry.type == "compaction" and "memories" in entry.data.get("checkpoint", {}):
                memory_records = entry.data["checkpoint"]["memories"]
                memory_source = entry
                memory_task_id = entry.data["checkpoint"].get("memory_task_id")
            elif entry.type == "memory_load":
                memory_records = entry.data["records"]
                memory_source = entry
                memory_task_id = entry.data.get("task_id")
            elif entry.type == "message" and entry.message is not None:
                change = entry.message.provider_metadata.get("pycode_task_change")
                if change is not None and (
                    change["task"]["status"] in {"completed", "cancelled"}
                    or memory_task_id is not None
                    and memory_task_id != change["task_id"]
                ):
                    memory_records = ()
        updates.pop("memories", None)
        if memory_records:
            assert memory_source is not None
            updates["memories"] = clone_value(memory_records)
            if memory_task_id is not None:
                updates["memory_task_id"] = memory_task_id
            lines = [
                "Saved memory references. Current user instructions and permission boundaries "
                "take priority. "
                "Verify project facts against current code; historical cases are references:"
            ]
            for record in memory_records:
                lines.append(
                    f"[{record['scope']} {record['memory_id']}; saved={record['updated_at']}] "
                    + record["content"]
                )
            pinned.append(
                ProjectedEntry(
                    memory_source.id,
                    (
                        Message(
                            "memory:" + memory_source.id,
                            "system",
                            "\n".join(lines),
                            provider_metadata={"source_type": "memory_load"},
                        ),
                    ),
                )
            )

        return ProjectionContribution(
            tuple(pinned), updates, frozenset({"memories", "memory_task_id"})
        )
