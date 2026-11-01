# 统一 ToolSearch 与组装精简

本地、框架文件工具和 MCP 工具先提供按来源分类的名称与一句话用途。主模型预加载 ToolSearch、Bash、ReadToolResult、ls、read_file、write_file、edit_file、glob、grep 的完整定义；其余工具按名称或关键词搜索后，在下一次模型请求中加载。预加载也经过权限与 Skill 范围过滤。搜索不调用第二个模型，不执行目标工具，失败或无结果不会回退成全量工具。

预加载名单以外，只有成功保存的搜索结果能加载工具；同批猜测调用、定义变化、权限过滤移除的工具不能据此执行。参数校验、Skill 范围、Plan、审批、只读和 MCP 执行前 schema/权限核对保持有效。搜索状态属于当前运行，重开新运行重新加载。

模型工具选择器、独立选择器 Provider 配置和用途记录已删除。旧 tool_selector 配置明确拒绝。MCP 不再提供专用 ToolSearch 或 MCPCall；启用的远端工具直接加入统一目录，disabled_tools 明确禁用指定工具；旧 loading/tool_modes 字段拒绝读取，不做自动转换。

内部组装函数必须接收模块。默认模块仅由 compose_application 选择，独立 Agent 入口明确构造最小依赖。Skill 由会话/独立应用入口准备，运行时不补建；目录挂载共用。删除动态 Skill lookup 及执行层兜底、重复 calibrator 参数与空模型转发函数。

Session、Summary、工具输出、模型记录、快照、Skill 和 MCP 配置读取共用 owned_io，仍各自保留锁、错误转换和取消后的等待。MCP 同次发现结果同时生成目录与 SDK 工具，不重复 list；实际执行前的远端校验仍保留。

Agent 调度、文件工具入口、Skill 加载、MCP 协议与转换、同模型重试、长期记忆继续复用既有框架。自定义摘要、Team 邮箱与 worktree、当前格式历史及 GC 未在本次变更中删除。

## 验证分层

统一发现测试和 MCP/评测集成使用真实加载流程。已有审批、输出、Hook、恢复等执行回归显式使用 disclosed_tools 测试前提，隔离测试已披露工具的执行行为；该测试 fixture 不进入产品。专门测试同时覆盖未搜索调用拒绝、加载后执行、MCP 审批和分类简表。
