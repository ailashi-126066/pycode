# Memory

负责每作用域 JSON 笔记、LangMem 主动工具、一次后台整理、变更日志和撤销。
`domain/` 拒绝疑似凭据和超限内容；`ports/` 描述存储/整理；`services/` 控制时机；
`adapters/` 实现 LangMem、JSON 和模型边界。历史投影在 `services/projection.py`。

`module.py:MemoryModule` 接收 workspace、user_data、project_id、Timing、trust 和 mode，
输出 service、updater、tools、projection。模型 owner/facade 从外部显式注入；模块内部决定
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

召回统一按相关性选择，可为空，不再少量全选或失败补最近条目。prepare_task 在模型前后按 Store 中的任务状态管理当前批次：首次召回绑定任务，同任务和重启沿用，切换替换，完成/取消撤下。memory_load 保存 task_id/input_id；MemoryProjection 在任务变更时撤下过期注入，压缩保留 memory_task_id。长期笔记、后台整理与工具结果历史不删除。
