"""The task board follows the session branch; compaction checkpoints keep a copy of it."""

from pycode.context.domain.tasks import branch_task_board
from pycode.kernel import ProjectionContribution


class TaskProjection:
    name = "tasks"
    checkpoint_keys = frozenset({"task_board"})

    def contribute(self, snapshot, path, entries, checkpoint):
        return ProjectionContribution(checkpoint_updates={"task_board": branch_task_board(path)})
