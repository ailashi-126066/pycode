# Memory

长期记忆：一条记忆一个 Markdown 文件（frontmatter 的 `description`、`updated`，正文是记忆内容）。
用户级在 `~/.pycode/memory/<id>.md`，项目级在 `<项目>/.pycode/memory/<id>.md`（仅信任项目）；
同目录的 `settings.json` 存 mode（auto/off），`memory.lock` 只在写盘时持有。

`MemoryFeature(trusted, mode, model)` 实现的 Feature 成员：

- `open`：建 scope 根目录和 `MarkdownMemoryStore`，mode 未指定时读 `settings.json`。
- `prompt_sections`：系统提示里的索引（每条一行 `[scope] id — description`，最多 50 条，最近更新的在前）。
- `mounts`：只读挂载 `/memory/user/`、`/memory/project/`，模型用 `read` 读。没有自动召回。
- `tools`：`manage_user_memory` / `manage_project_memory`（LangMem manage tool，schema 为
  `MemoryNote(description, content)`）。过滤：单条 2000 字、每作用域 50 条、疑似密钥拒绝。
- `gates`：这两个工具免审批。
- `on_event`：`compaction` 事件 `status == "applied"` 时启动后台整理，同一时间最多一个；
  失败只记一行 debug 日志，不影响 run。
- `aclose`：取消并等待后台整理。

整理输入：后台整理取这次压缩被摘要掉的那段（上一次压缩的 `first_kept_entry_id` 到这次的
`first_kept_entry_id`）里的 user 消息，摘要只作背景；手动 `/memory extract`（`extract()`）取最近对话里的
user 消息。LangMem Store Manager 写同一套文件，指令要求只记用户明确说过的偏好、项目约定和决定，
没有就不改。`model` 由组装注入（`BackgroundModel`），每个 scope 打开一次，thinking 关闭、不重试。
