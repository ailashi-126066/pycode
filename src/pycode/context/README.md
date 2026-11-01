# Context

负责上下文投影、预算、压缩、安全采用和大输出驱逐。系统提示位于本模块 `resources/`。
也负责单 Agent 任务契约与 TaskProjection；任务更新随助手消息保存，图 State 使用派生视图。
完整请求组成见 `docs/context-request.md`。固定 JSON 契约在模型边界组装；动态任务块由 History 加入请求并单独计量。
`domain/` 是纯策略，`ports/` 描述摘要存储/生成与投影贡献，`services/` 编排事实，
`adapters/` 连接框架和文件。此模块不解析 Skill、MCP、Plan 或 Memory 的内部记录。

装配入口为 `module.py:ContextModule`。输入是模型调用/停止原因回调、摘要记录工厂及
有序 `ProjectionContributor`；输出是 projector、summary、eviction、prompt/workflow。
Projector 每实例缓存自己的会话版本；贡献器输入只读，检查名称和 checkpoint key 所有权。
自定义贡献可能读取普通消息；未声明 `incremental_safe` 或声明为 False 时重算贡献。
仅所有贡献器明确保证普通追加不影响既有贡献，才复用增量快路径；Skills 保守重算。

在应用装配层可以显式建立：

```python
from pycode.context.module import ContextModule
from pycode.models.module import ModelsModule

context = ContextModule(
    request_step=ModelsModule.model_request_step, stop_reason=ModelsModule.stop_reason
)
projector = context.projector(contributors=())
```

摘要生成器每次拥有一个后台模型 scope，并在成功、失败或取消后关闭；服务的 aclose 由
session/run 所有者调用。纯投影不打开连接。完整接线见 `app/bootstrap.py` 与
`app/composition/modules.py`；跨模块依赖只通过公开原生契约。
