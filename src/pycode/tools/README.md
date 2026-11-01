# Tools

Core package: validates, authorizes, executes and settles tool calls, and provides Pi's builtin
file tools. The application supplies concrete gates and async execution callbacks.

| File | Contents |
|---|---|
| `executor.py` | `ToolRegistry`, `ToolExecutor`, the tool error boundary and the repeated-failure guard |
| `settlement.py` | `ToolSettlementMiddleware`: ordered settlement inside the Deep Agents tool node |
| `permissions.py` | Core approval (`InterruptToolGate`, `ReadOnlyToolGate`) and resume-answer validation |
| `backend.py` | `PycodeBackend` (workspace files, receipts, ripgrep) and `ScopedBackend` |
| `rg.py`, `output.py`, `schema.py` | ripgrep parsing, large-output temp files, JSON schema |
| `call_state.py` | One call's state from issue to settlement |
| `framework_tool.py`, `discovery.py` | LangChain tool adapter and ToolSearch |
| `ask_user.py` | AskUserQuestion and its checkpoint-backed interrupt |
| `builtin/` | Pi's `read/write/edit/grep/find/ls`; `paths.py` maps paths onto mounts |

```python
executor = ToolExecutor(
    registry,
    validator,
    InterruptToolGate(),
    run,
    outputs,
    gates=gates,
    before_tool_call=(hooks.before_tool_call, snapshots.before_tool_call),
    after_tool_call=(snapshots.after_tool_call, skills.after_tool_call),
)
```

Authorization (design 2.3): the user's gate, then the concrete service gates in explicit order. Any
`ToolError` denies; one `"allow"` skips approval; otherwise the core approval gate decides. Then
each registered `before_tool_call` callback runs; `block=True` turns the call into a `hook_rejected` result.
Only executed calls reach `after_tool_call`, whose overrides chain in list order before settlement
persists the result.

Relative `path` values resolve under `/workspace/`; absolute ones must start with a mounted prefix.
Only `/workspace/` and `/plans/` accept writes, and `edit`/`write` run in a per-file mutation
queue. Past 2000 lines or 50KB a result keeps a preview (head for read, tail for bash) and the full
text goes to `$TMPDIR/pycode/<session_id>/<call_id>.log`, mounted read-only at its own path.

`ToolSpec.exposure == "direct"` tools always carry their full definition; deferred ones are found
with ToolSearch and disclosed on the next turn. Tools default to `execution_mode="parallel"`; one
sequential tool makes the whole batch sequential.
