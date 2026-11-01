# Tools

`module.ToolsModule` owns JSON-schema validation, registry/executor construction, workspace and
artifact backends, composite routing, output storage, questions, execution gates, settlement and the
native framework tool adapter. It does not load AppConfig, select feature modules or create session
storage.

```python
from pathlib import Path
from pycode.tools.module import ToolsModule

tools = ToolsModule()
workspace = tools.backend(Path.cwd(), read_only=True)
registry = tools.registry(())
```

`backend` is also the explicit backend factory supplied to Skills and Plans; the latter supplies its
own exact-path and pre-scan search restrictions. `composite(routes)` combines already selected module
routes with a state backend.
`builtin_tools(backend)` provides Pi's `read/write/edit/grep/find/ls` over the composite routes:
relative `path` values resolve under `/workspace/`, absolute ones must start with a mounted prefix,
and `edit`/`write` run inside the per-file mutation queue. Any feature can add a read-only mount
with `read_only_mount(root)` under its own prefix (Skills use `/skills/<source>/`); only
`/workspace/` and `/plans/` accept writes. `default_tools` provides optional
questions; Sandbox (`bash`) and Plans supply their own tools. `output_store(session_id)` follows Pi for large outputs: past 2000 lines or 50KB the result keeps a
preview (head for read, tail for bash) and a notice, the full text goes to
`$TMPDIR/pycode/<session_id>/<call_id>.log`, and `details` gains `truncation` and
`full_output_path`. The directory is mounted read-only at its own path, so the model reads it with
`read`; Seatbelt already allows reading `$TMPDIR`.

`gate` preserves the policy sequence: ordinary approval, memory and Plan gates, optional custom gate,
then the injected feature scope. Worker mode always installs the mandatory worker boundary outside
the custom/scope constraints. Team merge approval remains a specialized Tools policy. The injected
scope is an independent `ToolGate`; it cannot replace its parent or enlarge worker permissions.

`executor` installs the official tool-error boundary. `settlement` and `framework_tool` receive ordered
`tool_extensions` and `native_bindings`, leaving Skill and MCP lookup/normalization with their owners.

The module owns no global task lifetime. Streaming writers own open output handles; the Agent/run
owner cancels and settles in-flight tool tasks before closing shared runtime resources.

`ToolSpec.exposure == "direct"` 的工具（7 个内置工具和 ToolSearch）每次请求都带完整定义，系统提示照 Pi 列出它们的 `prompt_snippet` 和 `prompt_guidelines`。其他工具是 deferred：ToolSearch 提供分类简表，按名称/关键词加载完整定义，工具查找不调用模型，成功保存搜索结果后于下一轮披露。所有定义先经过权限过滤。工具默认 `execution_mode="parallel"`，批次里有一个 sequential 就整批串行；并行时准备阶段逐个进行，执行阶段并发。
