# Memory
长期记忆：一条记忆一个 Markdown 文件（frontmatter 的 `description`、`updated`，正文是记忆内容）。
用户级在 `~/.pycode/memory/<id>.md`，项目级在 `<项目>/.pycode/memory/<id>.md`（仅信任项目）；
同目录的 `settings.json` 存 mode（auto/off），`memory.lock` 只在写盘时持有。

- 召回：系统提示放索引（每条一行 `[scope] id — description`，最多 50 条，最近更新的在前），
  模型用 `read` 读只读挂载 `/memory/user/<id>.md`、`/memory/project/<id>.md`。没有自动召回。
- 写入：`manage_user_memory` / `manage_project_memory`（LangMem manage tool，schema 为
  `MemoryNote(description, content)`），不需要审批。过滤：单条 2000 字、每作用域 50 条、疑似密钥拒绝。
- 后台整理：只在压缩采用时触发，另有手动 `/memory extract`；LangMem Store Manager 写同一套文件，
  整理时关闭 thinking。底层 `MarkdownMemoryStore` 只实现 LangMem 用到的 get / put / search。

`module.py:MemoryModule(workspace, user_data, trusted, mode)` 输出 service、updater、tools、mounts。
模型 owner/facade 从外部显式注入；模块内部决定 thinking 关闭和 RetryPolicy(max_retries=0)。
