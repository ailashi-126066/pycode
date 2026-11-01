# Context
负责上下文投影、系统提示、预算、压缩摘要和大输出驱逐。系统提示默认文本在 `resources/`。
完整请求组成见 `docs/context-request.md`。

- `projector.py`：`ContextProjector(features)`。先放核心的系统提示投影，再按列表顺序调用每个
  `Feature.contribute(snapshot, path, checkpoint)`；功能模块只写自己 `checkpoint_keys` 里的键
  （契约规则，由测试守住，运行时不检查）。追加的工具结果 `details` 或 `custom` 记录碰到任一
  功能模块的 checkpoint 键时重算，否则走增量快路径。
- `prompts.py`：`PromptService(scope, features, *, suffix="")` 拼接默认提示、`agent.md`、后缀和
  各功能模块的 `prompt_sections(scope)`（列表顺序，空串跳过）；`instruction_files` 给出
  MemoryMiddleware 可读的 AGENTS.md 和只服务这些文件的 backend。
- `budget.py`：`InputBudget`、`estimate_messages`、`ContextMeter`。meter 不认识任何功能模块，
  功能模块每次请求追加的尾部消息由调用方经 `tail` 交给它一起估算。
- `summary.py`：摘要源的截取与校验、`FileSummaryStore`、`SummaryWorkflow`、
  `ConfiguredSummaryGenerator`、`SummaryService`。采用压缩后发出 `compaction` 事件
  （`status="applied"`），需要跟进的功能模块在 `on_event` 里处理。
- `compaction.py`：切点、保留范围、保留边界、文件操作记录和工具调用配对修复。
- `eviction.py`：选出旧工具结果并追加 `context_edit`。
- `types.py`：摘要相关的不可变数据。

组装由 `app/composition.py` 完成，例如：
```python
from pycode.context import ContextProjector, PromptService

projector = ContextProjector(features)
prompts = PromptService(scope, features, suffix=suffix)
```
摘要生成器每次拥有一个后台模型 scope，在成功、失败或取消后关闭；服务的 aclose 由会话所有者调用。
纯投影不打开连接。
