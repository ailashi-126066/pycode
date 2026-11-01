"""Task facts follow the session branch and survive history compaction."""

from pycode.context.domain.projection import ProjectionContribution
from pycode.context.domain.tasks import apply_task_change, empty_tasks
from pycode.kernel import clone_value


class TaskProjection:
    name = "tasks"
    incremental_safe = True
    checkpoint_keys = frozenset({"task_state"})

    def contribute(self, snapshot, path, entries, checkpoint):
        state = empty_tasks()
        for entry in path:
            if entry.type == "compaction" and "task_state" in entry.data.get("checkpoint", {}):
                state = clone_value(entry.data["checkpoint"]["task_state"])
            if entry.type == "message" and entry.message is not None:
                change = entry.message.provider_metadata.get("pycode_task_change")
                if change is not None:
                    state = apply_task_change(state, change)
        return ProjectionContribution(checkpoint_updates={"task_state": state})
