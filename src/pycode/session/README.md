# Session

负责 SQLite Store 历史、会话列表/分支、恢复、回退编排和存储清理。
`domain/` 定义会话规则；`ports/` 接收执行、快照、模型等能力；`services/` 提供应用命令；
`adapters/storage/` 实现官方 SqliteStore、AsyncSqliteSaver 与文件对象布局。不会创建模型 SDK、Skill catalog 或 MCP 连接。

装配入口 `module.py:SessionsModule` 输入 workspace、user_data、Timing，输出 repository、
session service、header reader、history backend、storage GC 和本地 application status。

```python
from pathlib import Path
from pycode.kernel import SystemTiming
from pycode.session.module import SessionsModule

sessions = SessionsModule(Path("/project"), Path("/state"), SystemTiming())
repository = sessions.repository()
store = await repository.open("example", create=True)
try:
    snapshot = await store.snapshot()
finally:
    await store.aclose()
```

AgentSession 的功能产物由统一应用根连接。会话拥有 launcher、subagents、view、store；关闭会
清除所有权并等待各资源完成，前序失败/取消不跳过后续资源。重启不自动恢复执行或旧审批。
`scoped()` 保留所选实现及时钟，重新绑定工作区；子 Agent 不偷偷切回默认仓库。
