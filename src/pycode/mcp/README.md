# MCP

`MCPService(history, connections)` 管理当前分支目录；app 通过 `load_servers` 和
`MCPAdapterConnections(servers, environment, validator=...)` 显式创建连接，调用服务
`prepare_run(pending=...)` 刷新目录，`tools()` 返回普通 AgentTool，关闭调用 `aclose()`。
不使用 MCPModule、MCPRun 或 NativeToolBinding。配置关闭连接时 app 不创建服务。

`mcp_projection(snapshot, path, checkpoint)` 重建 MCP 提示与 checkpoint，归属键为
`MCP_CHECKPOINT_KEYS`。待恢复的 run 保留已保存目录，后续新 run 才采用远端目录变化。

FastMCP 处理协议与分页。调用前不重复拉取 schema 哈希；参数交给当前服务器校验。
MCPTool 直接经连接执行，统一工具流水线负责授权与结果持久化。媒体和 structured content 保留。
取消或断线可能产生效果未知错误，不自动重跑。不支持 resources、prompts、elicitation。
