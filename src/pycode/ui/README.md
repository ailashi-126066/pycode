# UI

Textual presentation is flat in this package. `PyApp` calls concrete session services for memory, skills, tasks, plans, subagents and snapshots. Actual disabled services hide their commands. Event rendering failures propagate to the UI error boundary rather than disabling rendering and continuing silently.

`RevertPanel` shows a target, mode and affected paths with Confirm/Cancel. Confirmation executes the new snapshot service once; there are no restore recovery or compensation controls.
