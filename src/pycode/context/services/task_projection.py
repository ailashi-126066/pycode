"""The task board follows the session branch; compaction checkpoints keep a copy of it."""

from pycode.context.domain.projection import ProjectionContribution
from pycode.context.domain.tasks import branch_task_board


class TaskProjection:
    name = "tasks"
    incremental_safe = True
    checkpoint_keys = frozenset({"task_board"})

    def contribute(self, snapshot, path, entries, checkpoint):
        return ProjectionContribution(checkpoint_updates={"task_board": branch_task_board(path)})
