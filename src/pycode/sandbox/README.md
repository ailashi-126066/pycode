# Sandbox

Optional module. `SandboxService(settings, *, required, passthrough, path_prefix, read_roots)`:

- `tools(run)`: the `bash` tool. Its output streams to `$TMPDIR/pycode/<session>/`; child
  processes get an allowlisted environment without credentials, plus the `passthrough` names.
  `path_prefix` is prepended to `PATH`; `read_roots(run)` adds read-only Seatbelt roots (Python
  Skill script directories).
- `gates(run)`: for builtin `bash`, the source deny list (`command_denied`) and, in `on-auto` mode
  with a usable sandbox, `"allow"` for allow-listed read-only commands. Every other call is left to
  the other gates and the core approval step.
- `run_command(argv, cwd, env, seconds)`: kernel `CommandRunner`, used by hooks.
- `status()`: what the UI shows.

`Sandbox` is Seatbelt on macOS after a real probe; elsewhere it is unavailable and commands run
behind ordinary approval. `required=True` (isolated workers) refuses to run a command without an
enabled sandbox.

```python
from pycode.sandbox import SandboxService, SandboxSettings

sandbox = SandboxService(SandboxSettings(mode="on-auto"), passthrough=("LANG",))
```
