# Session

会话历史和执行入口平铺为 store、inputs、restart、sessions、storage_gc、view、agent_session、history_backend。
store 合并 SQLite 历史、codec、repository 和布局；旧格式只由 upgrade_entry 处理。

app 明确创建 StorageLayout、SqliteSessionRepository、SessionService、FileStorageObjects/StorageGC。
AgentSession 接收真实 store/projector/models/launcher/default/durable_inputs/view/storage；
资源关闭栈 closing 由 app 注册后转移，session 不再重建资源列表或吞关闭异常。

AgentSession 公开 history、launcher、models、view、storage、skills、plans、memory、subagents、tasks、snapshots。
业务命令直接调用服务，不存在 Feature 查询或 memory/skills/revert 转发层。
改变会话状态的 UI/CLI 操作使用 session.execution() 并检查 require_settled(snapshot)。
只有正在运行的 task 拥有执行权；输入排队不等待整个 run 完成。

输入身份和分支去重只在 Store。PendingInputs 只是 FIFO；DurableInputs 将输入提交给 Store，
完成边界与新输入使用同一 Store 锁保证不丢失输入。run 重试查询历史结果，但身份检查仍交给 Store。
Store snapshot 的 index 是当时的只读副本，后续写入不能改变已交给压缩/记忆的快照。

ConversationHistoryBackend 保留：它是会话树虚拟 JSON 的真实异步 backend，不能替换成对同步
文件 backend 的 ScopedBackend 包装；read-only、aread/als/agrep 等行为保持。
新版 revert 由 snapshot 服务实现，session 不再保存 pending_revert 或提供旧事务恢复入口。
