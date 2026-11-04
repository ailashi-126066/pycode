# Memory

长期记忆：一条记忆一个 Markdown 文件（frontmatter 的 `description`、`updated`，正文是记忆内容）。
用户级在 `~/.pycode/memory/<id>.md`，项目级在 `<项目>/.pycode/memory/<id>.md`（仅信任项目）；
同目录的 `settings.json` 存 mode（auto/off），`memory.lock` 只在写盘时持有。

`MemoryService(trusted, mode, model)` 由 app 显式连接的服务方法：

- `open`：建 scope 根目录和 `MarkdownMemoryStore`，mode 未指定时读 `settings.json`。
- `prompt_sections`：系统提示里的索引（每条一行 `[scope] id — description`，最多 50 条，最近更新的在前）。
- `mounts`：只读挂载 `/memory/user/`、`/memory/project/`，模型用 `read` 读。没有自动召回。
- `tools`：`manage_user_memory` / `manage_project_memory`（LangMem manage tool，schema 为
  `MemoryNote(description, content)`）。过滤：单条 2000 字、每作用域 50 条、疑似密钥拒绝。
- `gates`：这两个工具免审批。
- `compacted_source(snapshot, source)`：每次压缩采用后传入确定的会话快照和该次摘要任务捕获的
  `SummarySource`，直接复用压缩前的原始消息，不在后台重新读取不确定的最新历史。
  `compacted(snapshot)` 保留为快照输入的兼容入口。
- `completed(snapshot, run_id)`：每完成 5 次真实用户请求，提交一次提前提取。审批暂停、失败、取消、
  模型与工具内部轮次以及子 agent 通知引起的合成请求不计数；同一 run 不重复计数。
- `aclose(force=False)`：正常关闭最多等待 10 秒，然后取消剩余工作；`force=True` 立即取消并收集任务。
  关闭记忆功能时也取消当前任务并清空待处理工作，已经写盘的内容保留。

两种自动触发共用一个后台 worker：所有压缩任务依次保留，等待中的提前提取仅保留最新快照；
手动 `/memory extract` 与后台整理也通过同一个提取锁串行执行。提前提取的进程内游标只在成功后推进，
失败时保留旧位置，供后续触发重试；分支改变会重置计数和增量位置。压缩任务失败只记 debug，继续后续项，
不影响主 run。压缩整理每次都执行，不因同一段用户消息已被提前提取而跳过。

整理输入保留原始对话前缀作为背景，LangMem 的提取输入只选择用户原文；只允许保存用户明确表达的偏好、
项目约定和决定。助手回复、工具结果和摘要不能作为新增事实来源。提前提取通过游标限定新增用户消息，
不会为此裁掉公共请求前缀。手动 `/memory extract` 使用当前保留上下文中的用户消息。

压缩和记忆在模型调用边界复用公共的原始对话前缀构建方式，便于 Provider 复用缓存；
后续指令和 LangMem 工具声明仍有差别，不保证完整请求共享 KV cache，也不保证每次命中。

LangMem Store Manager 负责结合已有记忆生成新增、更新或删除，`MarkdownMemoryStore` 执行实际文件保存，
继续使用原有格式、动态索引和 `manage_*_memory` 工具；没有新增独立 `MEMORY.md` 或额外召回模型。
`model` 由组装注入（`BackgroundModel`），每个 scope 打开一次，thinking 关闭、不重试，
`max_steps=1` 控制一次提取内部的处理步数，与每 5 次用户请求的触发间隔无关。

TUI 的 `Ctrl+Q` 或 `/quit` 正常关闭；`Ctrl+Shift+Q`、`/quit force`、`/quit --force`
（以及 `/exit` 的对应形式）立即取消后台记忆。CLI 取消执行时也采用强制关闭。
