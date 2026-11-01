# Application

应用从 `bootstrap.py:compose_application` 开始读。这里负责把功能模块接成应用，不实现功能内部协议。

```text
app/
  bootstrap.py       唯一应用组装入口，返回 Application
  api.py             应用公开操作，内部模块集合不作为普通使用接口
  cli/               命令行入口、参数解析、命令分发
  configuration/     应用设置、Provider 配置校验、凭据加载
  composition/       连接模块产物，按 session/run/模型/Hook 等职责组织
  lifecycle/         接收已组装资源，拥有运行、等待、取消和关闭
```

- `cli/main.py` 接收用户输入，`parser.py` 声明参数，`commands.py` 调用应用能力。
  安装后的 `pycode` 与 `python -m pycode` 都进入这里。
- `configuration/settings.py` 定义 AppConfig 和路径默认值；`providers.py` 读取并校验外部
  Provider JSON；`credentials.py` 按显式引用加载环境或工作区 .env。配置不能反向依赖组装或 CLI。
- `composition/modules.py` 明确选择模块集合；session/run/runtime 分别连接会话、会话内单轮和
  低层运行能力；hooks/bindings/factories 是具名接线，不处理模块内部实现。
- `lifecycle/session.py` 的 ConfiguredRuntimeLauncher 拥有暂停/恢复及会话资源；`run.py` 的
  RuntimeContainer 拥有单轮资源，RunAssembly 在构造失败时回收。这里只消费公开契约和关闭端口，
  不调用模块装配入口，也不反向导入 composition。

依赖方向：CLI → bootstrap → composition → lifecycle/configuration/功能模块。
生命周期通过显式工厂接收组装动作；功能模块不导入 app，app 不直接导入框架或功能 adapters。
子 agent 在 `composition/subagents.py` 里各开一个完整的 AgentSession，由 SubagentsModule 管理。
架构检查同时强制真实目录布局、包入口只含说明和上述方向，禁止把零散文件重新堆回根目录。

```python
from dataclasses import replace
from pathlib import Path
from pycode.app.bootstrap import compose_application, default_modules
from pycode.app.configuration.settings import AppConfig

config = AppConfig(Path("/project"), Path("/state"), memory_mode="off")
selected = replace(default_modules(config), skills=None, mcp=None)
application = compose_application(config, modules=selected)
status = application.status()  # 不需要 Provider
```

`modules=None` 表示默认集合；集合中的 skills/mcp 为 None 表示禁用运行能力。
历史纯投影独立配置。子工作区调用所选模块的 scoped hook，保留实现和凭据源。
会话入口是 `await application.open_session(...)`；底层验收使用 `await application.runtime(...)`。
所有关闭路径都等待所拥有资源完成，不用无人等待的后台 task 代替关闭。

## 组装参数

`composition/inputs.py` 只定义两个不可变数据类，不创建、不查找、不关闭资源：

- `SessionResources`：会话标识、历史、工具、事件、记忆、计划等已有能力。
- `RunOptions`：本次 run ID、模型、重试策略、检查点及已准备的 Skills/MCP 运行绑定。

工作目录、只读权限等仍由 `AppConfig` 唯一决定，不在两个对象里重复配置。
组装层解包后只把需要的字段传给各组件，不把整包参数传入业务组件。
SDK 后端和检查点是透明传递的句柄，框架类型检查留在拥有它们的模块边界。

普通会话仍通过 `application.open_session()` 和 `session.run()` 使用。
底层验证/嵌入入口使用显式数据对象，不再接受长串运行关键字参数：

```python
from pycode.app.api import RunOptions, SessionResources

resources = SessionResources(session_id="session-1", journal=journal, tools=tools)
options = RunOptions(run_id="run-1", provider=provider)
container = await application.runtime(resources, options)
try:
    result = await container.runtime.run(messages, container.context)
finally:
    await container.aclose()
```

参数对象只是引用集合。原有的 runtime/session 所有权和关闭顺序不变；共享资源不会因为
参数对象离开作用域而被关闭，构建失败仍由 RunAssembly 清理已创建的资源。
