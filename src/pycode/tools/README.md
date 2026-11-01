# Tools

`module.ToolsModule` owns JSON-schema validation, registry/executor construction, workspace and
artifact backends, composite routing, output storage, questions, execution gates, settlement and the
native framework tool adapter. It does not load AppConfig, select feature modules or create session
storage. Cross-session output ownership is checked by an explicitly supplied `SessionHeaderReader`.

```python
from pathlib import Path
from pycode.tools.module import ToolsModule

tools = ToolsModule()
workspace = tools.backend(Path.cwd(), read_only=True)
registry = tools.registry(())
```

`backend` is also the explicit backend factory supplied to Skills and Plans; the latter supplies its
own exact-path and pre-scan search restrictions. `composite(routes)` combines already selected module
routes with a state backend. `artifacts(outputs, history)` owns the large-result backend.
`default_tools` provides result reading and optional questions; Sandbox and Plans supply their own
tools. `output_store(..., header_reader=...)` owns output storage implementation selection.

`gate` preserves the policy sequence: ordinary approval, memory and Plan gates, optional custom gate,
then the injected feature scope. Worker mode always installs the mandatory worker boundary outside
the custom/scope constraints. Team merge approval remains a specialized Tools policy. The injected
scope is an independent `ToolGate`; it cannot replace its parent or enlarge worker permissions.

`executor` installs the official tool-error boundary. `configured_executor` is the constrained
executor factory building block for Hooks. `settlement` and `framework_tool` receive ordered
`tool_extensions` and `native_bindings`, leaving Skill and MCP lookup/normalization with their owners.

The module owns no global task lifetime. Streaming writers own open output handles; the Agent/run
owner cancels and settles in-flight tool tasks before closing shared runtime resources.

统一 ToolSearch 提供本地、框架和 MCP 工具分类简表，按名称/关键词加载完整定义。工具查找不调用模型，Bash、ReadToolResult 和常用文件工具预加载完整定义；其他工具在成功保存搜索结果后，于下一轮披露。所有定义先经过权限过滤。
