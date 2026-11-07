# MCP

`MCPService(connections)` 在会话打开时连接服务器，把工具列表保存在内存里；app 通过 `load_servers` 和
`MCPAdapterConnections(servers, environment, validator=...)` 显式创建连接，调用 `initialize()` 连接并读取
工具列表，`tools()` 返回普通 AgentTool，关闭调用 `aclose()`。
不使用 MCPModule、MCPRun 或 NativeToolBinding。配置关闭连接时 app 不创建服务。

工具列表只在会话打开时读取一次，每次运行开始不再刷新，也没有目录哈希、`mcp_catalog` 记录或 MCP 专用的
上下文投影。MCP 工具按普通延迟加载工具处理：目录由 `ToolSearch` 的延迟工具目录给出，加载后才带完整定义。
服务器在会话期间的变化要重新打开会话才会生效。

FastMCP 处理协议与分页。调用前不重复拉取 schema 哈希；参数交给当前服务器校验。
MCPTool 直接经连接执行，统一工具流水线负责授权与结果持久化。媒体和 structured content 保留。
取消或断线可能产生效果未知错误，不自动重跑。不支持 resources、prompts、elicitation。
