# Tasks

TaskBoardService(history, events, *, on_step_started) owns the current board view and run-end check.
TaskWrite and TaskUpdate persist board snapshots through ordinary tool results; task_contribution
reconstructs the task_board checkpoint from branch history. App registers tools, the task event
consumer, transform_context and before_finish, plus TaskBoardMiddleware to end a newly blocked run.
The on_step_started callback connects loopguard explicitly after a settled task event.

Task parsing and evidence functions live in types.py; plans receives plan_board as a function.
current and clear serve the UI. No framework-wide service base class is required.
