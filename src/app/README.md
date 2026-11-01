# Application

从 `composition.py` 读取应用的显式组装流程。功能使用普通服务，由 app 连接工具、gate、前后钩子、提示、投影与事件消费者。

- `api.py` 提供 Application、compose_application 和交互会话入口。
- `composition.py` 创建实际服务，分别用 AsyncExitStack 管理 session 和 run 资源；暂停时保留 run，结束后关闭，初始化失败时逆序释放已打开资源。
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
