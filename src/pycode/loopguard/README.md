# Loopguard

Optional module. `LoopGuard(threshold, asks_user)` watches settled tool calls through
`on_event` (`tool_execution_start` for the arguments, `tool_execution_end` for the result) and
`before_tool_call` (the tool's category). A detection is either the same call with the same result
`threshold` times (reads only when back to back) or the workspace changing back to a tree seen
`threshold` times (from `details.file_snapshot`, written by the snapshot module).

- First detection: `transform_context` appends one system reminder to the next model request.
- Second detection: its `middleware` stops in `abefore_model`. With `asks_user` it interrupts with
  an AskUserQuestion-shaped question; "continue" clears the counts, anything else ends the run with
  reason `loop_detected`. Without `asks_user` the run ends with `loop_detected` directly.
- Counts are cleared at `agent_start`, after a successful `TaskUpdate` to `in_progress`, and when a
  compaction is applied.

```python
from pycode.loopguard import LoopGuard

loopguard = LoopGuard(threshold=3, asks_user=True)
```
