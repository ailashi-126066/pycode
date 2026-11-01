# Memory

负责每作用域 JSON 笔记、LangMem 主动工具、一次后台整理、变更日志和撤销。
`domain/` 拒绝疑似凭据和超限内容；`ports/` 描述存储/整理；`services/` 控制时机；
`adapters/` 实现 LangMem、JSON 和模型边界。

`module.py:MemoryModule` 接收 workspace、user_data、project_id、Timing、trust 和 mode，
输出 service、updater、tools。模型 owner/facade 从外部显式注入；模块内部决定
thinking 关闭、RetryPolicy(max_retries=0) 和整理额度，不依赖应用配置类。

```python
from pathlib import Path
from pycode.kernel.adapters.timing import SystemTiming
from pycode.memory.module import MemoryModule

memory = MemoryModule(
    Path("/project"), Path("/state"), "project-id", SystemTiming(), trusted=False, mode="off"
)
service = memory.service(history)  # history 是调用方提供的 HistoryJournal
```

service 属于会话，负责等待/取消自己的后台任务。updater 的每次调用拥有独立模型并关闭它；
失败仅记录且不重试。显式 model_factory 是 async callable，facade_factory 不拥有该模型。
`scoped()` 保留所选实现和时钟，仅重新绑定子工作区/命名空间。不会从 app 反向获取其他服务。

不做自动召回，也不和任务绑定；后台整理只在压缩采用时触发，另有手动 extract。
