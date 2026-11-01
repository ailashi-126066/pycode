# ToolSearch 与延迟加载

`ToolSpec.exposure` 决定工具定义何时发给模型：

- `direct`：每次请求都带完整定义，并在系统提示的 `Available tools:` 里列出 `prompt_snippet`、在
  `Guidelines:` 里列出 `prompt_guidelines`。包括 Pi 的 7 个内置工具（read/bash/edit/write/grep/find/ls）、
  ToolSearch、TaskWrite/TaskUpdate、Agent/SendMessage/StopAgent。
- `deferred`（默认）：只在系统提示的目录里列出名字和一句话用途，按来源分组（`Local / <source>`、
  `MCP / <server>`）。模型先用 `ToolSearch` 按名字或关键词搜索，成功的搜索结果在
  `details.loaded_tools` 记下工具名，下一次请求起这些工具带上完整定义。

拼装在 `tools/adapters/discovery.py::ToolDiscovery.disclose`，每次模型请求前执行，读当前分支已完成的
ToolSearch 结果；失败的搜索、同一批里猜测的调用都不能加载工具。权限和 Skill 的 allowed-tools
范围先过滤再拼装。

执行时只检查一件事：调用名必须在本次请求的工具列表里（`settlement.declaration_error`），
否则得到 `tool_not_declared` 错误结果，工具不执行，也不进入审批。参数校验、Hook、审批、Plan
只读、MCP 执行前的远端 schema 核对照常走 `ToolExecutor`。

搜索不调用模型，不执行目标工具；没有结果时不会退回成全量工具。
