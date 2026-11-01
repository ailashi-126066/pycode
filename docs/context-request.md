# 模型请求的上下文组成

一次主 Agent 请求由固定规则、完整工具目录、已加载资料、任务状态和消息组成；完整工具参数放在模型 API 的 tools 字段。没有额外的工具选择模型或任务提取模型。

```text
System / Developer 职责的说明
  角色、权限、工作方式、项目指令、任务维护规则、JSON 输出契约
工具目录
  当前权限范围内所有本地 / MCP 工具的分类、名称和一句话用途，以及 Skill 索引
参考信息
  当前任务召回的相关记忆；文件和 MCP 正文不在这里重复复制
任务状态与执行观察
  当前任务详情、其他未完成任务索引、最新用户输入引用、最近工具结果元数据
Messages
  历史摘要、最近对话、工具调用与结果、最新用户输入
Tools
  常用工具完整定义 + ToolSearch 已加载的其他定义
```

这是职责划分，不会把每一项变成独立存储，也不会强行改写工具调用与结果的消息顺序。模型适配器使用已有 system 消息表达固定规则；文件和 MCP 返回的正文保留在对应工具消息中，不复制进资料区。最新用户输入只作为一条用户消息发送。任务块中的输入 ID 只是证据引用。

## 工具目录与预加载

ToolSearch 始终可用。常用工具预加载名单为 Bash、ReadToolResult、ls、read_file、write_file、edit_file、glob、grep。只有实际存在、通过权限和 Skill 范围过滤的工具才会加载；预加载不代表操作获批。

其余本地与 MCP 工具先发送名称、分类和一句话用途。ToolSearch 搜索成功且结果保存后，后续请求加载完整参数；目录名称本身不授予调用权限。已加载定义沿用现有版本校验与当前运行恢复规则。

MCP 工具属于目录和定义；MCP 调用的返回内容属于工具结果。Skill 提供说明和附属文件，脚本仍由 Agent 通过 Bash 执行。

## 单 Agent 的任务状态

首次请求的 current_task 为 null。模型在开始实际工作时提出 new，程序生成 task_id；简单问答可以不建任务。任务字段为 goal、status、constraints、acceptance、current_step、steps。约束和验收项带 source=user/agent，区分用户要求与 Agent 拟定的计划。

每个步骤包含 id、title、status、conclusion、evidence_refs。完成步骤必须有结论和当前分支上存在的证据引用；整个任务完成时，步骤全部完成并清空 current_step。引用可以是记录 ID、消息 ID、已保存工具结果的 call_id 或 output_id。校验引用存在不等于程序证明了结论正确，语义判断仍由 Agent 根据实际结果负责。

continue 更新当前任务；switch 恢复另一个已知任务；new 创建新目标，并把原来的未完成任务保留为 paused。同一目标的补充约束继续原任务，不能借 update 替换原目标。其他未完成任务只展示简要索引，切换后再展开详情。

switch 本身使用 update=null，且不夹带工具调用。保存切换后，图通过原生 jump_to="model" 立即再请求主模型，提供恢复任务的完整约束、步骤与证据，再继续处理当前用户要求；不需要用户再发一条“继续”。

任务状态与 Team 的共享任务板分开：这里管理一个 Agent 的工作进度，不能替代 Team 负责人、依赖、提交和验收规则。

## 模型输出和应用顺序

主模型的非空文字遵守 JSON 契约，继续使用原生 tool_calls：

```json
{
  "task_action": {
    "relation": "continue",
    "task_id": "task_001",
    "update": {"current_step": "s2"}
  },
  "message": "已定位问题，接下来修改连接池配置。"
}
```

没有任务变化时 task_action=null；只调用工具的响应可以没有文字。update 为补丁，省略的字段保留原值；steps 如提供则为完整步骤列表。JSON Schema 随固定说明发送，Pydantic 和任务规则在收到完整响应后校验；不是通过另一个工具或模型提取状态，也没有依赖只能处理最终回答的 response_format。

```text
组装当前请求 → 主模型返回 JSON 文字及原生工具调用
→ 校验输出、任务归属、步骤状态和证据
→ 同一助手消息保存回复、原始 action 和已校验的 task_change
→ 框架执行原生工具 → 保存结果 → 重新组装
```

无效输出报 invalid_task_output，不执行该响应的工具调用，不猜测转换成另一种格式，也不额外请求一个修复模型。被输出长度截断的文字仅显示已收到的 message 部分，不应用不完整任务更新，续写时要求完整的新 JSON 对象，message 只补齐后半段，未应用的任务更新需完整重述。

流式界面只显示 message 字段，隐藏 JSON 和任务字段。模型正在输出任务字段时也算真实输出活动，继续更新子 Agent 的无活动期限。

## 保存、恢复和预算

唯一的任务变更记录位于 session.sqlite 当前分支的助手消息 metadata：pycode_task_change。回复与任务更新随同一条消息提交，避免分开写入。TaskProjection 重放这些变更，在 ProjectedContext.checkpoint.task_state 中生成当前视图；压缩快照也保留这个结构。task_state 进入图 State 供执行和 checkpoint 使用，但不是第二个独立写入的任务账本。

分支回退读取对应路径；关闭重开从 Store 恢复；执行中断仍使用原有 execution.sqlite checkpoint。不会新建任务 JSON 文件或另一套数据库。原生简单 todos 已从项目组装中移除，write_todos 不再披露。

prepare_model_boundary 在权限过滤、工具披露和校准这条现有链中补充固定输出契约。History 随后用 attach_task_context 加入动态任务数据消息，避免最近输入 ID 的变化使固定系统配置失效。calibrate_model_request 记录固定系统提示和工具定义；ContextMeter 单独计算动态任务块及历史 JSON envelope 的开销，使用真实用量作起点时只追加动态部分的估算差值。任务变更使投影重新计算，普通无关追加保留原有增量路径。

后台摘要预生成（何时压缩）与旧工具输出清理算法不变；摘要写法照 Pi：首次、增量更新（`<previous-summary>`）和拆开 turn 的前半段三套模板，工具结果截到 2000 字符并写明 full_output 路径，读过和改过的文件跨压缩累加，写进 compaction 的 `details` 并附在正文末尾的 `<read-files>`/`<modified-files>`，发给模型时用 `<summary>` 包装。执行观察只展示工具名、错误状态、退出码和结果引用，不重复贴结果正文。长结果继续用 output_id 和 ReadToolResult 回读。

## 参考信息的任务范围

长期记忆不自动召回，也不和任务绑定。有已保存记忆时，系统提示里放一份索引（每条一行 `[scope] id — description`，最多 50 条），模型需要时用 read 读 `/memory/<scope>/<id>.md`。读到的内容是普通工具结果，由旧工具结果驱逐和压缩照常清理。

文件、Skill 正文和 MCP 查询结果按需进入工具消息。未知位置先搜索再读片段；已知路径可直接读，不强制 grep。不把这份正文再复制到参考区。项目 AGENTS.md 等工作指令仍按原规则加载。

## 子 Agent 的过程隔离

子 Agent 的完整对话、搜索、文件读取和工具结果保存在子会话，不自动并入主 Agent 上下文。完成时回传最终答复，结合任务验收记录和产物引用供主 Agent 核查。邮箱中的进度、阻塞、审批与交付消息仍可进入主上下文；因此过程隔离不表示只能在任务结束后通信。完整过程不删除，需要时按子会话或产物引用查询。
