# Application

从 `composition.py` 读取应用的显式组装流程。功能使用普通服务，由 app 连接工具、gate、前后钩子、提示、投影与事件消费者。

- `api.py` 提供 Application、compose_application 和交互会话入口。
- `composition.py` 只创建并连接具体服务，SessionServices 集中保存组装所需对象；build_run 返回 RuntimeContainer。
- `build_run` 按事件与文件资源 → 工具 → 模型与上下文服务 → RunContext → AgentRuntime 排列；`build_file_backend`、`build_tools` 是同文件的具体组装函数，不引入工厂层。
- 执行、暂停保留、恢复和关闭由 `agent/launcher.py` 的 RunLauncher 管理；app 通过 create_run 提供资源构造函数，不反向参与执行调度。
- AsyncExitStack 管理已获得的资源，初始化失败时逆序释放。
- `config.py` 定义 AppConfig、Provider 配置和凭据加载。
- `cli/` 负责参数解析及命令分发。`pycode` 与 `python -m pycode` 使用同一入口。

```python
from pathlib import Path
from pycode.app import AppConfig, compose_application

config = AppConfig(Path("/project"), Path("/state"), memory_mode="off")
application = compose_application(config)
status = application.status()
```

执行时传入 Provider 配置，调用 `await application.open_session(...)`，随后使用 `session.run()`、`session.resume()`，最终等待 `session.aclose()`。计划、技能、记忆等通过 session 的具体服务入口访问；历史、用量和请求记录从 `session.view` 读取。子 agent 各自打开完整会话，由 SubagentManager 管理。

低层嵌入入口 `application.runtime(resources, options)` 要求 SessionResources 显式提供真实 store；RunOptions 指定本次运行模型、ID 和检查点。资源构造和关闭仍由唯一组装入口负责，不提供 Feature 注册或查询层。
