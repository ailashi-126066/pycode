# Models

Provider 配置、模型目录、模型选择、SDK 模型、请求记录和计量。底层 HTTP 和模型协议由官方 LangChain 集成执行。

- `types.py`：`ProviderConfig`、`ModelDescriptor`、`RequestRecord`、`RetryPolicy`。
- `catalog.py`：`ModelCatalog`（SDK 模型列表 + 目录缓存文件 + 解析选择）。
- `control.py`：`ModelControl`，模型选择写成会话分支上的 `model_change` 记录，切换时发 `model_change` 事件；恢复时按当前配置解析。
- `factory.py`：`create_model` 选 ChatOpenAI / ChatAnthropic / ChatDeepSeek；`ReasoningDeepSeek` 把每轮 assistant 的 `reasoning_content` 写回请求。
- `messages.py`：kernel 消息和 LangChain 消息互转；用量只读 `AIMessage.usage_metadata`。
- `requests.py`：SDK 异常分类、同模型最多 3 次重试、每次尝试一条 SQLite 记录；中断的请求 `usage=None`。
- `metered.py`：给内部组件（摘要、记忆整理）用的 `MeteredChatModel`，经同一套请求记录。
- `usage.py`：`/usage` 报告。
