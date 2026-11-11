# 第八课 子 Agent

主 Agent 运行时,可以通过 `task` 工具把一个边界清楚的子任务交给另一个 Agent 去做,这个 Agent 就是子 Agent。这一课讲:子 Agent 从哪来,`task` 工具怎样派出它,它和主 Agent 哪些分开、哪些共用,几个子 Agent 怎样并行、结果怎样回来,以及超时、取消等保护。

主要代码在 `tools/builtins/task_tool.py`(`task` 工具)和 `subagents/executor.py`(执行器)。

示例数据沿用前面:小明的用户 ID 是 `7f3c...0001`,对话是 `a1b2c3`,任务是 `r-5566`,自定义 Agent 是 `my-researcher`。小明说"分析三个 CSV 文件,各画一张图"。这些值是编的。

## 本课涉及的数据结构

| 名字 | 是什么 | 谁创建 | 存在哪 |
| --- | --- | --- | --- |
| `SubagentConfig` | 一个子 Agent 的定义:名字、说明、提示词、工具、技能、模型、超时、轮数 | 内置写死在代码里;或由管理员配置 | 进程内存 |
| `subagents.custom_agents` | 管理员在配置文件里新增的子 Agent | 管理员 | `config.yaml` |
| 托管子 Agent | 管理员通过接口新增的子 Agent | 管理员调 `/api/subagents` | `{base}/managed-subagents/` 下每个一个 JSON 文件 |
| `config["metadata"]` | 主 Agent 组装时留下的限制,`task` 工具派子 Agent 时读 | `_assemble_lead_agent` | 内存里这次任务的 `config`;随检查点存进 SQLite |
| `SubagentExecutor` | 执行器:组装并运行一个子 Agent | `task` 工具 | 进程内存 |
| `SubagentResult` | 一个子 Agent 的运行情况:状态、消息、结果、用量、取消开关 | `execute_async` | 进程内存,`_background_tasks` 里 |
| `_background_tasks` | 所有子 Agent 的登记表,键是 `execution_id` | 模块级字典 | 进程内存,全进程一个 |
| `cancel_event` | 子 Agent 的取消开关,和主 Agent 的 `abort_event` 是两个 | 创建 `SubagentResult` 时 | `SubagentResult` 上 |
| `Runtime` / `runtime_ctx` | 交给工具和中间件的这次任务信息 | `run_agent` | 进程内存 |

管理员在 `config.yaml` 里新增一个子 Agent:

```yaml
subagents:
  custom_agents:
    analysis:
      description: "Data analysis specialist ..."   # 给主 Agent 看,决定什么时候派它
      system_prompt: |    # 子 Agent 自己的系统提示词
        You are a data analysis subagent. ...
      tools: [bash, read_file, write_file]           # 工具白名单,不写就继承全部
      skills: [data-analysis]         # 能用的技能
      model: inherit    # 用主 Agent 的模型
      max_turns: 80     # 最多走多少轮
      timeout_seconds: 600     # 最长运行多少秒
```

小明用 `my-researcher`、选了 ultra 模式时,主 Agent 的 `config["metadata"]`:

```python
{
    "deerflow_trace_id": "c47d...",
    "agent_name": "my-researcher",
    "model_name": "claude-y",
    "is_plan_mode": True,
    "subagent_enabled": True,
    "tool_groups": ["web", "file:read"],
    "mcp_plugins": None,
    "available_skills": ["deep-research"],
    "allowed_subagents": ["general-purpose"],
    "memory_enabled": True,
    # 还有 thinking_enabled、reasoning_effort 等
}
```

这份来自 `users/7f3c...0001/agents/my-researcher/config.yaml` 里的 `tool_groups: [web, file:read]`、`skills: [deep-research]`、`allowed_subagents: [general-purpose]`。用默认 Agent 时,`tool_groups`、`mcp_plugins`、`available_skills`、`allowed_subagents` 都是 `None`,表示不限制。第一课笔记里那份 `subagent_enabled` 是 `False`,因为那个例子是 pro 模式。

派出三个子 Agent 后的 `_background_tasks`:

| 键 `execution_id` | 值 `SubagentResult` |
| --- | --- |
| `e1a2...` | `status=RUNNING`,`external_task_id="call_A"`,`ai_messages=[2 条]`,`result=None` |
| `f3b4...` | `status=RUNNING`,`external_task_id="call_B"`,`ai_messages=[1 条]`,`result=None` |
| `9c8d...` | `status=COMPLETED`,`external_task_id="call_C"`,`result="orders.csv 共……"` |

`SubagentResult` 的主要字段:

| 字段 | 内容 |
| --- | --- |
| `status` | `PENDING` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED` / `TIMED_OUT` |
| `ai_messages` | 子 Agent 到目前为止产出的 AI 消息 |
| `result` | 最终结果文字 |
| `error` | 出错信息 |
| `token_usage_records` | 子 Agent 用了多少 token |
| `tool_receipts` | 子 Agent 每次工具调用的回执 |
| `cancel_event` | 取消开关 |

## 子 Agent 和自定义 Agent 不是一回事

| | 自定义 Agent(第一课的 `my-researcher`) | 子 Agent |
| --- | --- | --- |
| 是什么 | 主 Agent 的另一个版本:换了提示词、工具组、模型 | 主 Agent 运行时通过 `task` 工具派出去干一个子任务的 Agent |
| 谁跟它对话 | 用户直接选它聊天 | 用户不直接接触,只有主 Agent 派它 |
| 存在哪 | `users/{user_id}/agents/{name}/`,每个用户自己的 | 见下面三种来源,平台共用 |
| 谁创建 | 用户自己 | 内置,或管理员 |

普通用户不能创建子 Agent。用户能影响的只有两处:用自定义 Agent 配置里的 `allowed_subagents` 限制它能派哪些子 Agent;每次对话选择子 Agent 用哪个模型(只能选管理员允许的)。

| `allowed_subagents` | 含义 |
| --- | --- |
| 不写(`None`) | 所有启用的子 Agent 都能派 |
| `[]` | 一个都不能派 |
| `["general-purpose"]` | 只能派列出的这些 |

默认 Agent 没有 `AgentConfig`,能派全部子 Agent。

## 子 Agent 从哪来

`get_subagent_config(name)`(`subagents/registry.py`)按这个顺序找:

| 顺序 | 来源 | 有哪些 | 谁能改 |
| --- | --- | --- | --- |
| 1 | 内置 | `general-purpose`(通用,能探索和动手)、`bash`(专门执行命令) | 写死在代码里 |
| 2 | `config.yaml` 的 `subagents.custom_agents` | 模板里有一个注释掉的 `analysis` 例子 | 管理员改配置文件 |
| 3 | 托管子 Agent | 默认没有 | 管理员通过接口 `/api/subagents` 增删改,每个修改接口都检查 `require_admin_user` |

名字容易混:`custom_agents` 是"管理员自定义的子 Agent",和用户创建的"自定义 Agent"不是一回事。

找到之后,再叠加 `config.yaml` 里 `subagents.agents.<名字>` 下的覆盖项:超时、最大轮数、模型、技能、思考模式、提示词附加内容。

本地默认只有 `general-purpose` 和 `bash` 两个,而 `bash` 子 Agent 在本地沙箱没开 `allow_host_bash` 时不可用(第五课),所以实际能派的只有 `general-purpose`。

`analysis` 那段配置每一项的用处:

| 字段 | 意思 | 用在哪 |
| --- | --- | --- |
| `analysis` | 子 Agent 的名字 | 主 Agent 调 `task` 时用这个名字指定派谁 |
| `description` | 一句话说明它擅长什么 | 写进 `task` 工具的说明里,主 Agent 据此判断什么任务派给它 |
| `system_prompt` | 子 Agent 自己的系统提示词 | 子 Agent 的初始系统消息 |
| `tools` | 能用哪些工具 | 在主 Agent 的工具范围内再筛,见后面 |
| `skills` | 能用哪些技能 | 和主 Agent 的技能取交集 |
| `model` | 用哪个模型 | `inherit` 表示和主 Agent 一样 |
| `max_turns` | 最多走多少轮 | 防止一直跑下去 |
| `timeout_seconds` | 最长运行多少秒 | 超时强制结束 |

## 主 Agent 什么时候有 `task` 工具

有两个同名的 `subagent_enabled`,不要混:

| | 在哪 | 值 | 谁决定 |
| --- | --- | --- | --- |
| 主 Agent 的 | 主 Agent 组装时 | 看下面 | 前端请求 + `allowed_subagents` |
| `task` 工具里的 `subagent_enabled=False` | 给子 Agent 取工具时 | 永远是 `False` | 写死在代码里 |

主 Agent 的由请求带来。前端代码里是 `subagent_enabled: settings.mode === "ultra"`,只有界面上选了 ultra 模式才是 `true`。组装时:

```python
requested_subagent_enabled = cfg.get("subagent_enabled", False)
# 请求里要不要开子 Agent
# 结果:ultra 模式下是 True

subagent_enabled = bool(requested_subagent_enabled and allowed_subagents != [])
# 请求要开,并且自定义 Agent 没有禁止派任何子 Agent,才真的开
```

为 `True` 时,`get_available_tools(subagent_enabled=True)` 才给主 Agent 加上 `task` 工具(第七课)。所以默认模式下主 Agent 根本没有 `task`,不会派子 Agent。`allowed_subagents` 写成 `[]` 时,即使选了 ultra 模式也不给。

这个值和其他限制一起写进 `config["metadata"]`。这份"便条"由 `_assemble_lead_agent` 每次组装主 Agent 时写一次,`task` 工具运行时通过 `runtime` 读它,取 `tool_groups`、`mcp_plugins`、`available_skills`、`allowed_subagents` 来限制子 Agent。模型看不到它。

## task 工具怎样派出子 Agent

`task` 工具的参数:

| 参数 | 意思 |
| --- | --- |
| `subagent_type` | 派哪个子 Agent,如 `general-purpose` |
| `prompt` | 交给子 Agent 的任务说明,由主 Agent 写 |
| `description` | 一句话简述,给前端显示 |
| `context_mode` | `isolated`(默认,只给 `prompt`)或 `snapshot`(另外附上主 Agent 上下文的摘要) |
| `acceptance_criteria` | 可选的验收条件,如"某文件存在" |
| `model_name` | 可选,给子 Agent 换一个模型 |

主 Agent 可能发出:

```
task(subagent_type="general-purpose", description="分析 sales.csv",
     prompt="读取 /mnt/user-data/uploads/sales.csv,统计各月销售额,画折线图存到 /mnt/user-data/outputs/sales.png")
```

`task` 也是普通工具,同样经过第七课那一层层 `wrap_tool_call` 中间件,然后做下面这些。

**检查能不能派。**

```python
allowed_subagents = metadata.get("allowed_subagents")
# 从 config["metadata"] 读白名单
# 结果:默认 Agent 是 None,表示全部可派

available_subagent_names = get_available_subagent_names(allowed_subagents=allowed_subagents)
# 当前能派的:启用的、白名单允许的、运行条件满足的
# 结果:["general-purpose"]

config = get_subagent_config(subagent_type)
# 按三种来源的顺序查,再叠加覆盖项
```

`subagent_type` 不在名单里,或者 `context_mode` 写错了,工具不报异常,而是返回一条失败结果,里面列出可用的名字,主 Agent 可以改正。

**定模型。**

| 情况 | 子 Agent 用的模型 |
| --- | --- |
| 配置写 `model: inherit`(默认) | 和主 Agent 一样 |
| 配置写了具体模型 | 用配置的 |
| 调用时带了 `model_name` | 用它,但要先过 `authorize_selected_model`,必须是这个用户被允许使用的模型 |

最后一行是一道保护:模型写的参数不能让它绕过权限,换成一个用户本来用不了的贵模型。

**准备工具。** 分两道。第一道,`task` 工具调用第七课的 `get_available_tools`,范围按主 Agent 的来:

```python
parent_tool_groups = metadata.get("tool_groups")
# 读便条:主 Agent 能用哪几组工具
# 结果:["web", "file:read"]

tools = await run_assembly(
    get_available_tools,
    model_name=effective_model,     # 子 Agent 的模型,决定有没有 view_image 等
    groups=parent_tool_groups,      # 和主 Agent 一样的工具组
    subagent_enabled=False,  # 不给 task
    include_upload_tool=...,
)
```

便条上有 `mcp_plugins` 时也一起传进去,MCP 工具的范围也和主 Agent 一样。

第二道,执行器创建时按子 Agent 配置的白名单和黑名单再筛(`_filter_tools(tools, config.tools, config.disallowed_tools)`):

```python
if allowed is not None:
    filtered = [t for t in filtered if t.name in allowed_set]
# 有白名单:只留白名单里的

if disallowed is not None:
    filtered = [t for t in filtered if t.name not in disallowed_set]
# 有黑名单:去掉黑名单里的
```

| 子 Agent | 白名单 `tools` | 黑名单 `disallowed_tools` |
| --- | --- | --- |
| `general-purpose` | `None`,继承第一道给的全部 | `task`、`ask_clarification`、`present_files` |
| `bash` | `bash`、`ls`、`read_file`、`write_file`、`str_replace` | 同上 |

黑名单里的三个:`task` 防止子 Agent 再派子 Agent;`ask_clarification` 因为子 Agent 不能和用户对话;`present_files` 因为交付文件给用户是主 Agent 的事。`subagent_enabled=False` 和黑名单里的 `task` 两道一起,保证不会一层套一层。

所以:

```
子 Agent 的工具 = 主 Agent 能用的工具 ∩ 子 Agent 配置允许的工具 − 黑名单
```

| 情况 | 主 Agent 能用 | 子 Agent 配置 | 子 Agent 实际拿到 |
| --- | --- | --- | --- |
| 默认 Agent 派 `bash` 子 Agent | 全部工具 | 白名单五个 | 这五个 |
| `my-researcher` 派 `bash` 子 Agent | `web_search`、`web_fetch`、`ls`、`read_file` 等 | 同上 | 只有 `ls`、`read_file` |

子 Agent 的配置只能在主 Agent 范围内再缩小。用户给自定义 Agent 限制了工具,不能因为它派了一个子 Agent 就绕过限制。

**准备技能。** 也是两层取交集:

```python
parent_available_skills = metadata.get("available_skills")
# 读便条:主 Agent 能用哪些技能
# 结果:["deep-research"]

overrides["skills"] = _merge_skill_allowlists(list(parent_available_skills), config.skills)
# 和子 Agent 配置的 skills 取交集
```

| 主 Agent | 子 Agent 配置 | 结果 |
| --- | --- | --- |
| `None`(不限) | `None` | `None` |
| `None` | `["data-analysis"]` | `["data-analysis"]` |
| `["deep-research"]` | `None` | `["deep-research"]` |
| `["deep-research"]` | `["data-analysis", "deep-research"]` | `["deep-research"]` |

执行器准备初始消息时(`_load_skills`)按这个名单加载:读这个用户启用的技能,只留名单里的,再按权限过滤,最后把技能目录写进子 Agent 的系统消息。名单是 `[]` 时不加载任何技能。

| 子 Agent 的 | 第一层(主 Agent 的范围,来自便条) | 第二层(子 Agent 自己的配置) |
| --- | --- | --- |
| 工具 | `tool_groups`、`mcp_plugins`,且 `subagent_enabled=False` | 白名单 `tools`、黑名单 `disallowed_tools` |
| 技能 | `available_skills` | `skills`,取交集,再按权限过滤 |
| 能不能派 | `allowed_subagents` | 子 Agent 是否启用、运行条件是否满足 |

**准备初始消息。** `task` 工具创建 `SubagentExecutor`,执行器开始运行时组装子 Agent 的初始 state(`_build_initial_state`):

| 消息 | 内容 |
| --- | --- |
| 系统消息 | 子 Agent 配置里的 `system_prompt`,加上技能目录、延迟工具目录(第七课);最后套上提示词附加内容(第三课的 `prompt_overlay`) |
| 上下文摘要(只在 `snapshot` 模式) | 主 Agent 对话里相关内容的摘要 |
| 用户消息 | 主 Agent 写的 `prompt`,有验收条件时附在后面 |

子 Agent 看不到小明和主 Agent 之前的对话,只知道主 Agent 写给它的那段话,所以 `prompt` 必须写清楚。`snapshot` 模式用于相关要求散落在之前的对话里、一句话说不清的情况。

**组装和启动。**

```python
agent = create_agent(
    model=model,
    tools=bound_tools,
    middleware=middlewares,   # 子 Agent 自己的一套中间件
    system_prompt=None,       # 系统提示词已经放在初始消息里了
    state_schema=ThreadState,
    checkpointer=False,       # 不存检查点
)
```

和第一课主 Agent 的组装是同一个函数,区别是 `checkpointer=False`:子 Agent 的 state 不存进 SQLite,跑完就没了。它的成果靠写进对话目录的文件和最后返回给主 Agent 的结果留下来。

然后 `executor.execute_async(prompt, task_id=tool_call_id)` 生成一个 `execution_id`,在 `_background_tasks` 里登记一个 `SubagentResult`(状态 `PENDING`),把运行交给一个单独的事件循环在后台执行,自己立刻返回。键不用 `tool_call_id` 而另生成 `execution_id`,是因为模型服务商生成的调用 `id` 在不同任务之间可能重复,而这个字典是全进程共享的。

## 哪些分开、哪些共用

| | 子 Agent 自己的 | 和主 Agent 共用的 |
| --- | --- | --- |
| 消息 | 只有系统消息和主 Agent 写给它的 `prompt` | |
| state | 一份新的 state,不存检查点 | `sandbox`、`thread_data`、`uploaded_files` 三个字段从主 Agent 复制过来 |
| 文件 | | 同一个对话目录 |
| 沙箱 | | 同一个沙箱 |
| 身份 | | 同一个用户、同一段对话、同一次任务 `run_id`、同一个追踪编号 |
| 工具和技能 | 按两层规则算出的一份 | |
| 中间件 | 一套更少的中间件 | |

消息和 state 分开,是为了上下文隔离:子 Agent 读几十个文件、搜很多网页,中间过程全留在它自己的消息里,不会塞进主 Agent 的上下文,主 Agent 最后只收到一段总结。

文件和沙箱共用。`task` 工具从主 Agent 的 state 里取出三个字段交给执行器:

```python
sandbox_state = runtime.state.get("sandbox")
# 结果:{"sandbox_id": "local:a1b2c3"}

thread_data = runtime.state.get("thread_data")
# 结果:a1b2c3 的三个真实目录(第四课)

parent_uploaded_files = runtime.state.get("uploaded_files")
# 结果:这次上传的文件清单
```

执行器把它们放进子 Agent 的初始 state。所以子 Agent 调 `read_file("/mnt/user-data/uploads/sales.csv")`,换出来的真实路径和主 Agent 一样;它把图存到 `/mnt/user-data/outputs/sales.png`,主 Agent 和小明都能看到。这是子 Agent 交回成果的主要方式之一:它返回的文字只需说"图已存到某路径",主 Agent 再用 `present_files` 交付给小明。

沙箱共用时要计数,这就是第五课提到的租约管理器(`sandbox/lease.py`)。子 Agent 运行时,运行时上下文里带一个 `sandbox_lease_owner_id`,值是 `subagent:{execution_id}`,主 Agent 有自己的另一个。租约管理器按这个 ID 记录谁正在用这个沙箱:

| 时刻 | 正在用沙箱的 | 能不能真的归还 |
| --- | --- | --- |
| 主 Agent 开始 | 主 Agent | |
| 派出两个子 Agent | 主 Agent、子 Agent 1、子 Agent 2 | |
| 子 Agent 1 跑完,调用归还 | 主 Agent、子 Agent 2 | 不能,还有人在用 |
| 主 Agent 任务结束 | 无 | 最后一个用完,这时才真正调用 `provider.release` |

模块开头的说明是"最后一个执行租约才能调用 `SandboxProvider.release`"。所以子 Agent 结束不会把主 Agent 还在用的容器放回预热池。取消时停掉命令也按这个 ID 区分,停子 Agent 1 的命令不会停掉子 Agent 2 的。子 Agent 的沙箱中间件以 `owns_agent_skill_projection=False` 创建,不重建沙箱里的技能目录,沿用主 Agent 布置好的那份。

身份共用。执行器给子 Agent 的运行时上下文带着主 Agent 的 `thread_id`、`user_id`、`run_id`、追踪编号、`user_role`、`authz_attributes` 等,另加 `is_subagent: True`。所以子 Agent 的日志能用同一个追踪编号查到,权限检查按小明的身份做,个人 MCP 工具核对主人时也能通过。传给子 Agent 的回调里去掉了主 Agent 的记录器 `RunJournal`(`_copy_isolated_subagent_context`),因为子 Agent 跑在另一个事件循环上,记录器只能在主 Agent 的事件循环里用;框架传流式片段的回调保留着,子 Agent 的输出还能流到前端。

中间件分开。子 Agent 的中间件由 `build_subagent_runtime_middlewares` 组装,用的是和主 Agent 同一个基础层函数 `_build_runtime_middlewares`(第二课),工具类中间件都有,但有几处不同:

| 参数 | 意思 |
| --- | --- |
| `include_uploads=False` | 没有上传中间件,子 Agent 的消息是主 Agent 写的,不需要再加上传清单 |
| `include_dangling_tool_call_patch=True` | 有悬空工具调用修复,和主 Agent 一样 |
| `receipts_render_mode="always"` | 总是给模型看工具回执,子 Agent 被要求在报告里引用回执编号 |
| `owns_agent_skill_projection=False` | 不重建技能视图 |

主 Agent 层的那些中间件(标题、记忆、澄清提问、子 Agent 数量限制等)子 Agent 都没有。

子 Agent 是一个"同一个人、同一个工作台、但只拿到一张任务单"的助手:共用身份、文件目录、沙箱;不共用消息;不存检查点,能留下来的只有写进目录的文件和返回的结果。

## 并行、进度和结果

小明说"分析三个 CSV 各画一张图",主 Agent 在一次回复里发出三个 `task` 调用:

```python
AIMessage(tool_calls=[
    {"name": "task", "args": {"subagent_type": "general-purpose", "prompt": "分析 sales.csv ..."}, "id": "call_A"},
    {"name": "task", "args": {"subagent_type": "general-purpose", "prompt": "分析 users.csv ..."}, "id": "call_B"},
    {"name": "task", "args": {"subagent_type": "general-purpose", "prompt": "分析 orders.csv ..."}, "id": "call_C"},
])
```

一条消息里的多个工具调用由工具节点并发执行(第七课),所以三个 `task` 同时运行,各自派出一个子 Agent。并行不需要特殊机制。数量有三道限制:

| 限制 | 在哪 | 默认 | 超出时 |
| --- | --- | --- | --- |
| 一次回复最多几个 `task` | 子 Agent 数量限制中间件(`SubagentLimitMiddleware`),`after_model` | 3 | 多出来的 `task` 调用从模型回复里删掉 |
| 一次任务总共最多派几个 | 同一个中间件 | 6(`subagents.max_total_per_run`) | 删掉多出的调用,回复里附上 "[SUBAGENT LIMIT REACHED] ...",让模型用已有结果收尾 |
| 整个进程同时跑几个子 Agent | `subagent_runtime` | 同时跑 3 个,最多排队 64 个,排队最多等 300 秒 | 排队等待;配置成 `reject` 时直接拒绝 |

前两道按这次任务算,删的是模型回复里的调用,写进 state。第三道是全进程共享的:小明和小红同时派子 Agent,一共也只有 3 个在跑。默认的 6 等于"两批满额的并行",防止主 Agent 反复派一批又一批。

`execute_async` 返回 `execution_id` 后,`task` 工具进入循环:

```python
while True:
    result = get_background_task_result(execution_id)
    # 从 _background_tasks 里取这个子 Agent 的 SubagentResult

    # 有新的 AI 消息:发一个 task_running 事件
    # 状态是 COMPLETED / FAILED / CANCELLED / TIMED_OUT:组装结果,返回

    await asyncio.sleep(5)
    # 每 5 秒查一次
```

主 Agent 和子 Agent 之间不直接通信,而是通过 `_background_tasks` 里的同一个对象:

| 时刻 | 谁 | 做什么 |
| --- | --- | --- |
| 派出时 | `execute_async` | 新建 `SubagentResult`(`PENDING`),放进字典 |
| 运行中 | 子 Agent(在另一个事件循环) | 不断更新:状态改成 `RUNNING`,追加 AI 消息、token 用量、工具回执 |
| 结束时 | 子 Agent | 写最终状态和 `result` |
| 每 5 秒 | `task` 工具 | 取出对象,读状态和新消息 |
| 取消时 | `task` 工具 | 取出对象,打开它的 `cancel_event` |
| 收尾时 | `task` 工具 | `cleanup_background_task(execution_id)` 删掉 |

字典读写时加锁(`_background_tasks_lock`),因为主 Agent 和子 Agent 跑在不同的线程上。它和第六课的 `_runs` 对比:

| | `RunManager._runs` | `_background_tasks` |
| --- | --- | --- |
| 登记什么 | 主 Agent 的任务 | 子 Agent 的执行 |
| 值 | `RunRecord` | `SubagentResult` |
| 取消开关 | `abort_event` | `cancel_event` |
| 有没有数据库对应 | 有,`runs` 表 | 没有,只在内存 |

子 Agent 的状态不持久化。Gateway 重启后登记表就空了,正在跑的子 Agent 也随进程一起没了;主 Agent 的任务会被第六课的孤儿回收标成 `error`。

主 Agent 的 `task` 调用在这期间一直没有返回,主 Agent 的循环停在工具节点,等三个 `task` 都返回才回到模型节点。查询循环还有一个上限:最多查 `(超时秒数 + 60) / 5` 次,防止 `task` 工具一直等下去。

循环里每发现新情况,就往主 Agent 的事件流里发一个 `custom` 事件:

| 事件 | 什么时候发 | 内容 |
| --- | --- | --- |
| `task_started` | 刚派出 | `task_id`(就是 `call_A`)、描述 |
| `task_running` | 子 Agent 每产出一条新的 AI 消息 | 这条消息、第几条、token 用量、模型名 |
| `task_completed` | 完成 | 最终结果、token 用量 |
| `task_failed` | 失败 | 错误信息 |

前端用 `task_id` 把三个子 Agent 的进度分别显示在三张卡片上。这些事件经 Redis 发到前端,同时存进事件表,刷新页面后还能看到。它们不写进 state,模型看不到。

子 Agent 跑完时,执行器从它最后的 state 里取最后一条 AI 消息的文字作为 `result`(`_extract_final_result`)。就算是因为轮数用完被打断的,也取打断前最后一条,不丢弃已做的部分。`task` 工具看到 `COMPLETED` 后返回:

```python
Command(update={
    "messages": [
        ToolMessage(
            content="Task Succeeded. Result: sales.csv 共 12 个月数据,3 月最高……折线图已保存到 /mnt/user-data/outputs/sales.png",
            tool_call_id="call_A",
            name="task",
            additional_kwargs={...状态、模型、token 用量、工具回执...},
        )
    ]
})
```

| 状态 | `content` |
| --- | --- |
| 完成 | `Task Succeeded. Result: ...` |
| 完成但被上限截断 | `Task Succeeded (capped: ...). Result: ...` |
| 失败 | 错误信息 |
| 超时 | `Task timed out. ...` |
| 取消 | `Task cancelled by user.` |

失败不会让主 Agent 的任务失败,它只是一条报错的工具结果,主 Agent 可以自己做或者重新派。三个结果都回来后,主 Agent 下一次调用模型时看到:

| 序号 | 消息 |
| --- | --- |
| 1 | 用户:分析三个 CSV 各画一张图 |
| 2 | AI:三个 `task` 调用 |
| 3 | 工具结果 `call_A`:sales 的总结和图片路径 |
| 4 | 工具结果 `call_B`:users 的总结和图片路径 |
| 5 | 工具结果 `call_C`:orders 的总结和图片路径 |

主 Agent 汇总三段结果,用 `present_files` 交付三张图,写最终回答。`ToolMessage` 写进主 Agent 的 state,随检查点保存;子 Agent 的 token 用量记到主 Agent 这次任务的统计里。

子 Agent 的报告是自我报告,不是验证过的事实,它可能说"图已保存"但其实脚本报错了。两个办法帮主 Agent 判断:工具回执,子 Agent 被要求在报告里引用回执编号 `[rN]`,`task` 工具返回前核对引用(`verify_receipt_citations`),没有引用的操作声明标为 UNVERIFIED;验收条件,主 Agent 派任务时可以带 `acceptance_criteria`(如 `file:/mnt/user-data/outputs/sales.png exists`),子 Agent 完成后 `task` 工具用代码去检查,查不了的标为 UNVERIFIED,不会默认通过。回执引用只证明那次调用发生过,不证明结论正确,所以说明里还建议主 Agent 对关键结论自己抽查。核对逻辑没有细读。

## 子 Agent 的保护

| 保护 | 防的是 | 默认 | 触发后 |
| --- | --- | --- | --- |
| 超时 | 跑太久 | 内置子 Agent 1800 秒 | 状态 `TIMED_OUT` |
| 轮数上限 | 走太多步 | `general-purpose` 150 轮,`bash` 60 轮 | 停下,取停下前最后的结果 |
| token 上限 | 花太多 token | 200 万 | 删掉工具调用,正常收尾,标"被上限截断" |
| 循环检测 | 原地打转 | 和主 Agent 同一套 | 同第七课 |
| 并行和总数上限 | 主 Agent 派太多 | 一次 3 个,一次任务共 6 个 | 上一节 |
| 主 Agent 被取消 | 用户点了停止 | | 子 Agent 也停 |

前三项都可以在 `config.yaml` 的 `subagents` 下全局改或按名字单独改。超时默认值:`SubagentConfig` 里写的是 900 秒,配置注释写的全局默认是 1800 秒,内置子 Agent 用的是全局值。

超时。执行器在后台启动子 Agent 时:

```python
done, _ = await asyncio.wait({execution_task}, timeout=self.config.timeout_seconds)
if execution_task in done:
    return execution_task.result()
# 按时做完:返回结果

result.cancel_event.set()
result.try_set_terminal(SubagentStatus.TIMED_OUT, error=f"Execution timed out after {self.config.timeout_seconds} seconds", ...)
execution_task.cancel()
# 超时:打开子 Agent 的取消开关,状态记为超时,取消它的任务
```

和第六课主 Agent 的取消是同一个思路:一个开关加一次取消。子 Agent 运行循环里每拿到一个事件就看一眼开关。`task` 工具下一次查询时看到 `TIMED_OUT`,返回 `Task timed out. ...`。正常情况下执行器的超时一定先到;如果 `task` 工具查够次数子 Agent 还没结束,说明它卡住了,`task` 工具发取消请求,返回 `polling_timed_out`。

轮数上限。`max_turns` 是"最多想几次、做几次",一轮等于一次模型调用加上它调用的工具。LangGraph 的 `recursion_limit` 数的是图里的节点,而每个中间件钩子都是一个节点,两者不相等。执行器按子 Agent 实际装了多少中间件换算(`_resolve_recursion_limit`),中间件多了少了能走的轮数不变。换算公式在 `turn_budget.py`,没有读。到达上限时执行器接住 `GraphRecursionError`,取停下前最后一条 AI 消息作为结果:已经有可用结果时状态 `completed`,主 Agent 收到 `Task Succeeded (capped: ...)`;什么都没有时状态 `failed`。

token 上限。和第七课主 Agent 的 token 预算是同一个中间件,只是子 Agent 默认就有一个很宽的上限。到达上限时删掉工具调用,让子 Agent 自然结束,结果标为 `completed`,停止原因 `token_capped`。

## 主 Agent 被取消时子 Agent 怎么停

先分清两个"task":

| | 是什么 |
| --- | --- |
| `record.task` | asyncio 的任务对象,代表整个后台运行的 `run_agent`,由第六课 `start_run` 里的 `asyncio.create_task(worker)` 得到 |
| `task` 工具 | 主 Agent 用来派子 Agent 的工具 |

`record.task.cancel()` 在 `RunManager.cancel` 里执行。用户点停止,前端发 `POST /api/threads/a1b2c3/runs/r-5566/cancel`,路由函数检查权限后调用 `run_mgr.cancel`,里面同步执行:

```python
record = self._runs.get(run_id)
# 从内存字典取出 r-5566 的 RunRecord

record.abort_event.set()
# 打开开关

task_active = record.task is not None and not record.task.done()
if task_active and record.status == RunStatus.running:
    record.task.cancel()
# 取消整个后台运行
```

不经过任何队列或定时检查。`cancel()` 本身只是做标记,很快返回;异常真正抛出要等事件循环切到那个后台任务,因为它正在 `sleep`,基本立刻生效。多进程部署时取消请求可能落到另一个进程,那个进程没有这个 `record.task`,只能把"要取消"写进数据库,真正跑任务的进程在下一次心跳读到后才执行这两行。用户关网页且 `on_disconnect` 是 `cancel` 时、新请求的策略是 `interrupt` 或 `rollback` 时,也走这两行。

`task` 工具不是一个独立的东西,它是在 `record.task` 这个后台任务里面被执行的一段代码。主 Agent 派子 Agent 时,这个任务的调用栈是:

```
record.task(一个 asyncio 任务)
  └ run_agent
      └ async for item in agent.astream(...)
          └ LangGraph 的工具节点
              └ task 工具函数
                  └ await asyncio.sleep(5)    ← 此刻执行到这里
```

`task` 工具在返回之前,工具节点不会结束,图不会产生下一个事件,主 Agent 一直卡在 `async for` 等下一个 `item`,走不到 `if record.abort_event.is_set()` 那一行去看开关。如果取消时只打开开关,子 Agent 不知道主 Agent 被取消了,会继续跑到完,`task` 工具返回,主 Agent 才看到开关,用户点了停止要等很久才停。

asyncio 的规则是:对一个任务调用 `cancel()`,`CancelledError` 会在这个任务此刻正停着的那一行 `await` 抛出。`record.task` 此刻停在最里层的 `await asyncio.sleep(5)`,所以异常从这一行出来,一层层往外传:

```
await asyncio.sleep(5)       ← CancelledError 从这里抛出
  ↑ task 工具的 except asyncio.CancelledError 先接住
      通知子 Agent 停下,等它停下,然后 raise
  ↑ 工具节点、LangGraph 图(不处理,继续往外传)
  ↑ run_agent 循环的 finally:close_agent_stream 关闭生成器
  ↑ run_agent 的 except asyncio.CancelledError:写 interrupted
  ↑ run_agent 的外层 finally:发 end、同步标题、释放资源
```

如果这时主 Agent 不是在等子 Agent,而是在等模型回复,最里层就是"等模型返回的那一行";`task` 工具只是这一次碰巧在最里层。

子 Agent 不在 `record.task` 的调用栈里,它跑在另一个事件循环上,`record.task.cancel()` 碰不到它。主 Agent 的 `abort_event` 子 Agent 也不看。所以必须由 `task` 工具在 `except` 里专门打开子 Agent 自己的 `cancel_event`:

```python
except asyncio.CancelledError:
    try:
        request_cancel_background_task(execution_id)
    except Exception:
        logger.warning("Failed to request cancellation ...")
        # 通知本身出错了只记日志,不能让这个错误盖掉正在传递的取消
    await _finalize_interrupted_subagent(runtime, execution_id, trace_id, max_poll_count)
    raise
```

第一步,通知。`request_cancel_background_task`:

```python
result = _background_tasks.get(execution_id)
# 在登记表里找到这个子 Agent 的 SubagentResult

result.cancel_event.set()
# 打开子 Agent 的取消开关

future.cancel()
# 子 Agent 如果还在排队没开始跑,直接取消,不让它开始
```

第二步,等。`_finalize_interrupted_subagent` 里的核心是一个查询循环(`_await_subagent_terminal`):

```python
while True:
    result = _peek_subagent_result(execution_id)
    # 读登记表里子 Agent 的当前状态

    if _is_subagent_terminal(result):
        return result
    # 已经是 CANCELLED / COMPLETED / FAILED / TIMED_OUT 之一:停下了

    if polls >= max_polls - 1:
        return None
    # 查够次数还没停:不再等

    await asyncio.sleep(5)
    polls += 1
```

这个循环外面包了一层 `asyncio.shield(...)`:`task` 工具此刻已经在处理一次取消,如果又来一次取消(用户又点了一次停止),没有 `shield` 的话这次等待会被打断,后面的记账和清理就做不了。外层取消还是传进来时,函数用 `except asyncio.CancelledError: pass` 吞掉,继续做后面的事。等到结果后(没等到也读一次当前状态),`_report_subagent_usage` 把子 Agent 用掉的 token 记到主 Agent 这次任务的统计里,这是要等的主要原因。然后清理登记:子 Agent 已经停下就 `cleanup_background_task` 删掉;等够次数还没停就不删,交给子 Agent 所在的后台事件循环,等它真停下后再删(子 Agent 还在往 `SubagentResult` 里写,删了会出错)。整个函数外面有 `try ... except Exception`,任何错误只记日志不往外抛,因为此刻有一个 `CancelledError` 正在往外传,再抛别的会把它替换掉。

第三步,`raise` 重新抛出刚才接住的 `CancelledError`,继续往外传到 `run_agent`。如果不 `raise` 而是正常返回,工具节点会以为 `task` 工具正常结束了,主 Agent 会接着调用模型,取消就失效了。

子 Agent 那边,收到信号后:

```python
if result.cancel_event.is_set():
    logger.info(f"Subagent {self.config.name} cancelled by parent")
    cancelled_during_stream = True
    break
```

它把自己的状态写成 `CANCELLED`,错误信息 "Cancelled by user",然后停掉自己在沙箱里还在跑的命令、归还沙箱执行租约。

时间线(子 Agent A 正在运行 `python plot.py`):

| 时刻 | `task` 工具 | 子 Agent A |
| --- | --- | --- |
| 0 秒 | 收到 `CancelledError`,打开 A 的开关 | 正在等一个工具返回 |
| 0 秒 | 开始等,第一次查询:还在 `RUNNING` | |
| 2 秒 | | 工具返回,看到开关,停下,写 `CANCELLED`,停沙箱命令 |
| 5 秒 | 第二次查询:`CANCELLED`。记账,删登记 | 结束 |
| 5 秒 | `raise` | |

三个 `task` 工具同时走这个流程,各管各的子 Agent。

子 Agent 不能被强行杀死,只能通知它停。它只在拿到下一个事件时才看开关,如果正卡在一次很长的模型调用上,要等这次调用返回才会停。`request_cancel_background_task` 虽然对它调用了 `future.cancel()`,但代码注释写明这种跨线程的任务不能这样强行停掉。沙箱命令不受这个限制:主 Agent 的取消收尾会按执行租约停命令,子 Agent 自己的收尾也会再停一次,所以卡住的是一条 `bash` 命令时它会被停掉,子 Agent 随后就能看到开关。真正需要等的是卡在模型调用上的情况。

子 Agent 被取消、超时、失败后,它的消息和 state 没有存检查点,丢弃;写进对话目录的文件留着,不撤销;token 用量记进主 Agent 的统计;已经发出的进度事件留在事件表里。

## break 和 cancel:Agent 到底怎样停下

`async for` 里看开关后 `break`,只代表不再读这个流。真正让 Agent 停下的,是读不读和之后的关闭。

`agent.astream(...)` 返回的是一个异步生成器,特点是不读它就不往下跑。LangGraph 在生成器里跑 Agent 图,每产生一个事件就 `yield` 出来,停在那里等外面读下一个。`break` 之后外面不再读了,生成器就停在最后那个 `yield` 处,Agent 图不会再往下走。

停住的生成器还占着图的执行状态和可能正在等的模型连接,所以要关掉。这就是第六课 `run_agent` 里运行主 Agent 的那段循环(`runtime/runs/worker.py`):

```python
stream = agent.astream(input_payload, config=stream_config, stream_mode=lg_modes, subgraphs=stream_subgraphs)
# 启动 Agent,得到一个异步生成器。这时 Agent 还没开始跑

try:
    async for item in stream:
    # 每读一次,Agent 往前跑一段,产生一个事件

        if record.abort_event.is_set():
            broke_on_abort = True
            break
        # 取消开关开着:不再读

        mode, chunk, namespace = _unpack_stream_item(item, lg_modes, stream_subgraphs)
        await _publish_stream_item(bridge=bridge, run_id=run_id, mode=mode, chunk=chunk, ...)
        # 发到 Redis
finally:
    await close_agent_stream(stream)
    # 不管怎么出来的,都关闭生成器
```

`close_agent_stream` 调用生成器的 `aclose()`。Python 对异步生成器调用 `aclose()` 时,会在它停着的那个 `yield` 处抛出 `GeneratorExit`,让生成器里的清理代码执行,LangGraph 借此结束图的执行、释放模型连接等资源。

| | 什么时候用 | 怎么停 |
| --- | --- | --- |
| `break` + `aclose()` | Agent 在两个事件之间,外面能走到 `is_set()` 检查 | 不再读,生成器停在 `yield`;关闭时在 `yield` 处收到 `GeneratorExit` 做清理 |
| `task.cancel()` | Agent 卡在一个长时间的 `await` 上(等模型、等 `task` 工具) | 在那一行抛 `CancelledError`,往外冒;冒出 `async for` 时,`finally` 里同样调用 `close_agent_stream` |

所以开关只是一个标记,能不能让 Agent 停下,取决于外面能不能走到检查它的那一行。走得到就 `break`;走不到就靠 `task.cancel()` 从里面打断。两个都需要,这就是第六课说的"打开开关加取消任务一起做"。

子 Agent 是同样的结构:它的循环也是 `async for chunk in stream`,看到 `cancel_event` 就 `break`,`finally` 里也是 `close_agent_stream(stream)`,区别是它把消息写进 `SubagentResult` 而不是发到 Redis。因为跑在另一个事件循环上,`task.cancel()` 打不到它,只能靠 `break` 这一种方式停。

## run_agent 的结构和收尾

那段循环只是 `run_agent` 的中间一部分。整个函数:

```python
async def run_agent(...):
    try:
        # 准备:记录器、状态改 running、发 metadata、组装 Agent
        stream = agent.astream(...)
        try:
            async for item in stream:
                ...
        finally:
            await close_agent_stream(stream)     # 内层 finally
        # 循环结束后:按情况写最终状态(success / waiting_for_input / error)
    except asyncio.CancelledError:
        # 被取消:停沙箱命令,状态写 interrupted
    except Exception:
        # 出错:状态写 error,发 error 事件
    finally:
        # 外层 finally:不管哪种结束都做的收尾
```

| | 管什么 | 什么时候执行 |
| --- | --- | --- |
| 内层 `finally` | 只关闭 Agent 的生成器 | 一离开 `async for` 就执行,不管是读完、`break` 还是异常冒出来 |
| 外层 `finally` | 整个任务的收尾 | `run_agent` 结束前最后执行 |

所以顺序总是先关生成器,再做任务收尾。`worker.py` 里关闭生成器有两处(1696 行和 1755 行),分别对应"只要一种事件"和"要多种事件"两种循环写法,网页用后一种。

外层 `finally` 的收尾(第六课):

| 收尾 | 作用 |
| --- | --- |
| 往 Redis 发 `end` 事件 | 前端收到后停止转圈、关闭连接 |
| 更新 `threads_meta` 表 | 写入对话标题;状态改回 `idle` 等 |
| 确认最终状态已写进 `runs` 表 | 补写只写了内存的部分 |
| 关闭记录器 | 把攒着的事件写进 SQLite 事件表 |
| 释放资源 | 见下面 |

释放资源分两类。一是沙箱执行租约:`await release_sandbox_execution_lease_async(runtime_ctx)`,把主 Agent 开始时登记的租约归还,如果它是最后一个在用的,租约管理器这时才真正调用 `provider.release`。沙箱中间件的 `after_agent` 也会归还(第五课),这里是兜底,任务被取消或出错时 `after_agent` 可能没跑到。二是大对象的引用:

```python
agent = None   # 组装出来的 Agent 图
accessor = None       # 读写检查点的对象
runtime = None
runtime_ctx = None    # 运行时上下文
journal = None        # 记录器
graph_input = {}      # 小明这次的输入消息
pre_run_workspace_snapshot = None   # 任务开始前工作区的文件快照
# 还有一批同类变量
```

再用 `_release_run_scoped_references` 从 `config` 字典里删掉几个内部键(如 `__run_journal`)、从回调列表里去掉记录器。Python 的对象只要还有人引用,内存就不会被回收;`RunRecord` 还要在 `_runs` 里留一阵,如果这些局部变量被它间接引用着,一个 Agent 图、一整份消息列表就会一直占着内存。手动断开引用防止内存越涨越高。这是防内存泄漏的工程细节。

最后安排两个延迟清理,作为独立的后台任务执行,`run_agent` 不等它们:

```python
_create_contextless_task(bridge.cleanup(run_id, delay=60))
# 60 秒后删掉 Redis 里这个任务的事件流

_create_contextless_task(run_manager.cleanup(run_id))
# 从内存字典 _runs 里删掉这个 RunRecord
```

## runtime 里有什么

`runtime` 是 `run_agent` 里建的 LangGraph `Runtime` 对象(第一课):

```python
runtime = Runtime(context=runtime_ctx, store=store)
config["configurable"]["__pregel_runtime"] = runtime
# 放进 config 的这个位置是 LangGraph 的约定,框架据此把它交给每个工具和中间件
```

| 字段 | 是什么 |
| --- | --- |
| `context` | `runtime_ctx` 这个字典,大部分内容在这里 |
| `store` | 一个键值存储对象,跨对话保存数据用 |

`runtime_ctx` 由 `_build_runtime_context` 生成:先放两个一定有的键,再合并请求里的 `context`,再加上后端自己的几个对象。小明这次任务大致是:

| 键 | 值 | 来自哪 |
| --- | --- | --- |
| `thread_id` | `a1b2c3` | 后端写入,不能被请求覆盖 |
| `run_id` | `r-5566` | 后端写入,不能被请求覆盖 |
| `user_id` | `7f3c...0001` | 认证后后端写入(第零课) |
| `model_name`、`thinking_enabled`、`is_plan_mode`、`subagent_enabled` 等 | 请求里的选项 | 前端请求的 `context` |
| `app_config` | `AppConfig` 对象 | 后端加的,工具读配置用 |
| 追踪编号 | `9b1f...` | 后端加的(第零课) |
| `sandbox_id` | `local:a1b2c3` | 第一次取得沙箱时写入(第五课) |
| `__run_journal` | 记录器对象 | 后端加的,内部用 |
| `__run_input_control` | 插话和排队的控制对象 | 后端加的,内部用(第零课) |

请求里如果有人想自己塞 `thread_id`、`run_id` 这类由服务器决定的键,合并时会被跳过(`_SERVER_OWNED_RUNTIME_CONTEXT_KEYS`),前端不能伪造它们。

工具和中间件都通过它拿这次任务的信息:

| 谁 | 读了什么 |
| --- | --- |
| 线程目录中间件(第四课) | `runtime.context["thread_id"]`、`user_id`,算出对话目录 |
| `read_file` 等工具(第四、五课) | `runtime.state["thread_data"]`、`runtime.state["sandbox"]` |
| `ensure_sandbox_initialized`(第五课) | 写 `runtime.context["sandbox_id"]` |
| `task` 工具 | `runtime.context` 里的身份和权限信息传给子 Agent;`runtime.config["metadata"]` 里的限制 |

`runtime.state` 和 `runtime.config` 不是 `run_agent` 建的这个对象上本来就有的。工具执行时,LangGraph 给每个工具传的是一个 `ToolRuntime`,它在这个 `Runtime` 的基础上加上了当前的 state、`config`、这次工具调用的 `id` 等,工具函数参数里写的 `runtime: Runtime` 拿到的就是这个。这部分是按代码里的用法推断的,本机没装 LangGraph。

## 小结

| 环节 | 内容 |
| --- | --- |
| 从哪来 | 内置(`general-purpose`、`bash`)、`custom_agents`、托管,都由管理员配置;用户的自定义 Agent 用 `allowed_subagents` 限制能派哪些 |
| 主 Agent 什么时候能派 | ultra 模式请求 `subagent_enabled`,且 `allowed_subagents` 不是 `[]`,才给 `task` 工具 |
| 怎样派出 | 检查名字、定模型(授权检查)、取工具和技能(主 Agent 范围 ∩ 子 Agent 配置)、准备初始消息、`create_agent(checkpointer=False)`、交给后台事件循环 |
| 分开和共用 | 共用身份、文件、沙箱(按执行租约计数);不共用消息,不存检查点 |
| 并行和结果 | 一次发多个 `task` 就是并行;`task` 工具每 5 秒查 `_background_tasks`,发 `task_*` 进度事件,最后返回一条 `ToolMessage` |
| 保护 | 超时、轮数上限、token 上限、并行和总数上限 |
| 取消 | `record.task.cancel()` 从 `task` 工具的 `await` 处打断;`task` 工具打开子 Agent 的 `cancel_event`、等它停下、记账、再 `raise`;子 Agent 在下一个事件处 `break` |

没有逐行核实的部分:`max_turns` 换算成 `recursion_limit` 的公式(`turn_budget.py`);回执引用和验收条件的核对逻辑;子 Agent 中间件的完整列表;`ToolRuntime` 怎样由 LangGraph 组装(本机未安装)。
