# Plans

PlanService(history, files, prompt, *, parse_board) owns planning state and run-scoped approval.
FilePlanStore(root, *, backend_factory) uses ScopedBackend with one active Markdown path and a
restricted search scope. App supplies the task-board parser explicitly, avoiding imports between
business modules. initialize restores branch state; enter opens planning; review requests a
LangGraph approval; finish ends an approved run.

App registers ExitPlanMode, PlanService.authorize, select_tools, after_tool_call and on_event.
PlanProjection.contribute(snapshot, path, checkpoint) reconstructs the plan checkpoint and prompt.
There is no module factory, generic base class or fallback re-export.
