# Context

`ContextProjector(contributors=(), *, checkpoint_keys=frozenset())` 重建当前分支上下文。
contributors 是 `(snapshot, path, checkpoint) -> ProjectionContribution | None` 函数序列，
系统提示先投影，之后按显式顺序调用业务投影。checkpoint_keys 告诉增量缓存哪些状态变化需要重算。

`PromptService(scope, sections=(), *, suffix="")` 拼接默认提示、项目 agent.md、后缀及实际提示读取函数。
每个 section 接收 SessionScope、返回文本 tuple；app 只注册确实提供提示的服务。

`SummaryService(..., on_compacted=None)` 在压缩记录成功采用后将该时刻的完整 SessionSnapshot
同步交给回调，再发 applied 事件。app 将 memory.compacted 接到这个入口，不让后台任务再读取不确定的最新状态。
配置更新不会丢掉已连接的回调。压缩失败或尚未采用时不会调用。

阅读顺序：types → projector/prompts → budget/compaction → summary。
本包不认识 Feature 基类，也不通过通用服务列表寻找能力。
