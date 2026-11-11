# Agent 组装与状态

第一课的笔记。第零课讲到 `start_run` 登记任务、把 Agent 放到后台去跑,当时把"后台跑的那个 Agent"当成现成的东西。本课讲它是什么时候、由谁、拿着什么材料拼出来的。

本课分 5 步,已全部写完:

| 步 | 内容 | 在哪一节 |
| --- | --- | --- |
| 1 | 组装在哪里被触发,拿到什么输入 | 二 |
| 2 | 读取运行选项,自定义 Agent 怎样"变身" | 三 |
| 3 | 确定模型,创建模型对象 | 四 |
| 4 | 准备工具、中间件、提示词,合成 Agent | 五 |
| 5 | `ThreadState` 和 reducer | 六 |

示例数据沿用第零课。示例里的 ID 和模型名是编的,字段和代码来自本地源码。

| 示例数据 | 值 |
| --- | --- |
| 用户 | 小明,用户 ID `7f3c...0001`,角色 `user` |
| 对话 | `thread_id = a1b2c3` |
| 任务 | `run_id = r-5566` |
| 请求 | 消息"帮我查一下明天的天气",模式 pro,想用的模型 `gpt-x` |
| 系统里配的模型 | `gpt-x`(默认)、`claude-y` |

## 一、本课涉及的数据结构

按"存在哪里"列出,每个都给出具体形态。

### 每次任务一份的(存在进程内存里,任务结束就不用了)

**`config`:这次任务的运行配置,一个字典。** 由 `start_run` 创建,后台任务再补充。下面是后台任务调用组装函数那一刻的完整内容,没有省略。每个键标了是谁写进去的:

```python
config = {

    "recursion_limit": 1000,
    # build_run_config 写入:最多执行多少步。请求要的是 1000,没超过服务端上限,采纳

    "run_name": "lead_agent",
    # 后台任务写入:这次运行的名字,追踪平台上显示用。自定义 Agent 时是它的名字

    "callbacks": [<RunJournal 对象>],
    # 后台任务写入:监听器列表。RunJournal 负责统计 token 用量

    "metadata": {
        "deerflow_trace_id": "9b1f...",
        # start_run 写入:追踪编号
        "deerflow_agent_name": "__default__",
        # 后台任务写入:产生这些检查点的是哪个 Agent。默认 Agent 用固定的 "__default__"
    },
    # 这个分区会落进每个检查点的 metadata 列

    "configurable": {
        "thread_id": "a1b2c3",
        # build_run_config 写入:对话 ID。LangGraph 靠它找检查点
        "model_name": "gpt-x",
        "mode": "pro",
        "thinking_enabled": True,
        "reasoning_effort": "medium",
        "is_plan_mode": True,
        "subagent_enabled": False,
        # 以上六项:merge_run_context_overrides 从请求体的 context 里按白名单抄过来的
        "__pregel_runtime": <Runtime 对象>,
        # 后台任务写入:LangGraph 约定的位置,执行时框架把它交给工具和中间件
        "__deerflow_checkpoint_channel_mode": "full",
        # assemble_lead_agent 写入:检查点用全量还是增量存储
    },

    "context": {
        "thread_id": "a1b2c3",
        "run_id": "r-5566",
        # 后台任务写入:对话 ID 和任务 ID
        "model_name": "gpt-x",
        "mode": "pro",
        "thinking_enabled": True,
        "reasoning_effort": "medium",
        "is_plan_mode": True,
        "subagent_enabled": False,
        # 以上六项:和 configurable 里那六项是同一批,两边各放一份
        "locale": "zh-CN",
        # merge_run_context_overrides 写入:界面语言
        "user_id": "7f3c...0001",
        "user_role": "user",
        "oauth_provider": None,
        "oauth_id": None,
        "is_internal": False,
        # 以上五项:inject_authenticated_user_context 根据登录身份写入,不是客户端传的
        "app_config": <AppConfig 对象>,
        # 后台任务写入:系统配置
        "deerflow_trace_id": "9b1f...",
        # 后台任务写入:追踪编号
        "thread_incarnation": <这段对话的代次标识>,
        # 后台任务写入:对话被删除重建后用来区分新旧
        "__run_journal": <RunJournal 对象>,
        # 后台任务写入:让中间件也能往任务日志里记事件
        "__run_input_control": <输入控制对象>,
        # 后台任务写入:插话功能用。只有有人在场的交互式任务才有
        "__deerflow_extension_agent_runs": <...>,
        "__deerflow_extension_snapshot": <...>,
        # 后台任务写入:扩展机制用的两项,本课用不到
    },
}
```

| 分区 | 放什么 | 特点 |
| --- | --- | --- |
| `configurable` | 运行选项的老位置,`thread_id` 必须在这里 | LangGraph 靠里面的 `thread_id` 找检查点 |
| `context` | 运行选项的新位置,就是第零课讲的 `runtime_ctx` | 只在运行期间存在 |
| `metadata` | 附带信息 | 写进每个检查点的 `metadata` 列 |
| `callbacks` | 监听器列表 | 模型调用开始和结束时框架会通知它们 |
| `recursion_limit` | 最多执行多少步 | 超过服务端上限会被压低 |

`context` 这个分区就是第零课讲的 `runtime_ctx`,两者是同一个字典。`__` 开头的键是后端内部用的对象,不是数据,只存在于内存,不会落进检查点。请求体的 `context` 里不在白名单的项(如 `client_timezone`)不会进入 `config`。

**组装过程中 `config` 还会被改两处。** 第 2 步定下子 Agent 开关后,`config["context"]["subagent_enabled"]` 被改成最终值;第 3 步定下模型后,`metadata` 分区加入一批最终选项。组装完成后 `metadata` 分区是:

```python
"metadata": {
    "deerflow_trace_id": "9b1f...",
    "deerflow_agent_name": "__default__",
    "agent_name": "default",
    "model_name": "gpt-x",
    "thinking_enabled": True,
    "reasoning_effort": "medium",
    "is_plan_mode": True,
    "subagent_enabled": False,      # 是否启用子 Agent
    "tool_groups": None,
    "mcp_plugins": None,
    "available_skills": None,
    "allowed_subagents": None,      # 允许调用哪些子 Agent;None 表示不限制
    "memory_enabled": True,
},
```

`metadata` 有两个用途:落进每个检查点,供事后查看;以及把组装时定下的限制带到运行时,供工具读取(例如 `task` 工具读其中的 `allowed_subagents`)。它相当于组装阶段留给运行阶段的一张便条。

**`cfg`:把 `configurable` 和 `context` 合并后的扁平字典。** 由 `_get_runtime_config(config)` 生成,组装代码读选项时用它。两处都有的键以 `context` 为准:

```python
cfg = {
    "thread_id": "a1b2c3", "run_id": "r-5566",
    "model_name": "gpt-x", "mode": "pro",
    "thinking_enabled": True, "is_plan_mode": True,
    "subagent_enabled": False, "reasoning_effort": "medium",
    "locale": "zh-CN",
    "user_id": "7f3c...0001", "user_role": "user", "is_internal": False,
    "app_config": <AppConfig 对象>,
    "deerflow_trace_id": "9b1f...",
    "__pregel_runtime": <Runtime 对象>,
}
```

**`RunContext`:这次任务要用到的对象打包成的"工具箱"。** 由 `get_run_context(request)` 在 `start_run` 开头创建,作为参数 `ctx` 传给后台任务:

```python
RunContext(
    checkpointer = <检查点读写对象>,
    store        = <键值存储>,
    event_store  = <任务事件存储>,
    thread_store = <对话记录读写对象>,
    app_config   = <AppConfig 对象>,
    ...
)
```

**`Runtime`:LangGraph 提供的包装对象。** 后台任务创建,交给工具和中间件的统一入口。工具里写的 `runtime.context.get("thread_id")`,读的就是它:

```python
Runtime(
    context = <runtime_ctx,也就是 config["context"] 那个字典>,
    store   = <键值存储>,
)
```

**`agent_factory`:一个变量,里面放的是一个函数。** `start_run` 里赋值,值是 `assemble_lead_agent` 这个函数本身(还没调用)。

**`LeadAgentAssembly`:组装函数的返回值,一个装着三样东西的包。**

```python
LeadAgentAssembly(
    graph    = <组装好的 Agent,能执行>,
    descriptor      = <一份记录:模型=gpt-x, 工具=[...], 中间件=[...], plan_mode=True, ...>,
    effective_model = "gpt-x",
)
```

**`AgentConfig`:一个自定义 Agent 的专属配置。** 用户在网页上创建自定义 Agent 时填的设置,保存下来就是它。按用户存放,每个自定义 Agent 一份。组装时由 `load_agent_config(名字, user_id)` 读出来。默认 Agent 没有这份配置,得到的是 `None`。

假设小明创建过一个叫 `my-researcher` 的自定义 Agent:

```python
AgentConfig(
    name       = "my-researcher",
    description= "专门做资料调研",
    model      = "claude-y",
    tool_groups= ["web"],    # 只给联网类的工具
    skills     = ["deep-research"],       # 只给这一个技能
    allowed_subagents = [],   # 不允许调用任何子 Agent
    memory_enabled    = True,
)
```

| 字段 | 含义 | 没填(`None`)时 |
| --- | --- | --- |
| `name` | 名字,也是它的标识 | 必填 |
| `description` | 描述 | 空 |
| `model` | 默认用哪个模型 | 用系统默认模型 |
| `tool_groups` | 可以用哪几组工具 | 全部工具 |
| `skills` | 可以用哪些技能 | 全部已启用的技能 |
| `allowed_subagents` | 可以调用哪些子 Agent | 全部 |
| `thinking_enabled` | 默认是否开启思考 | 不覆盖 |
| `reasoning_effort` | 默认思考强度 | 不覆盖 |
| `memory_enabled` | 是否启用记忆 | 默认 `True` |

列表字段有一个统一的约定:

| 值 | 含义 |
| --- | --- |
| `None`(没填) | 不限制,用全部 |
| `[]`(空列表) | 一个都不给 |
| `["a", "b"]` | 只给列出的这些 |

**组装函数里读出来的一组变量。** 第 2 步结束时,`_assemble_lead_agent` 里有这些局部变量(小明这次:默认 Agent、pro 模式):

| 变量 | 值 | 来自 | 后面哪里用 |
| --- | --- | --- | --- |
| `resolved_user_id` | `"7f3c...0001"` | `config["context"]` | 按用户加载技能、自定义 Agent,判断权限 |
| `requested_model_name` | `"gpt-x"` | `cfg` | 第 3 步确认后才算数 |
| `is_plan_mode` | `True` | `cfg` | 第 5 步决定装不装待办列表中间件 |
| `max_concurrent_subagents` | 系统容量内的一个数 | `cfg` 和系统配置 | 第 4、5 步 |
| `max_total_subagents` | 系统配置的值 | `cfg` 和系统配置 | 第 4、5 步 |
| `is_bootstrap` | `False` | `cfg` | 是否走引导模式分支 |
| `agent_name` | `None` | `cfg` | 记忆、提示词按 Agent 区分时用 |
| `agent_config` | `None` | 按 `agent_name` 加载 | 第 3 步取模型默认值,第 4 步取工具组 |
| `memory_enabled` | `True` | `agent_config` | 第 5 步决定装不装记忆中间件 |
| `allowed_subagents` | `None` | `agent_config` | 第 4 步决定能调用哪些子 Agent |
| `subagent_enabled` | `False` | 请求和 `agent_config` 共同决定 | 第 4 步决定有没有子 Agent 工具 |
| `available_skills` | `None` | `agent_config` | 第 5 步决定提示词里列哪些技能 |

**`create_agent` 的五样东西。** 组装函数的全部工作就是把它们准备好:

| 变量 | 是什么 | 小明这次 |
| --- | --- | --- |
| `chat_model` | 模型对象,一个配好连接方式的模型客户端 | `ChatOpenAI(model="gpt-x-2026", api_key="sk-...", 开启思考, 强度 medium)` |
| `final_tools` | 工具列表 | `[web_search, web_fetch, image_search, ls, read_file, glob, grep, write_file, str_replace, bash, present_files, ask_clarification, ...]`,没有 `task` |
| `bound_middlewares` | 中间件列表 | `[输入净化, 工具输出预算, 线程目录, 上传, 沙箱, ..., 动态上下文, 技能激活, 摘要, 待办列表, 标题, 记忆, 看图, 循环检测, ..., 澄清提问]` |
| `system_prompt` | 系统提示词 | 一大段文字:角色说明、行为规则、技能目录 |
| `ThreadState` | 状态结构 | 见下面的状态一节 |

`chat_model` 创建时不联网,只是把参数存起来,真正调用时才发请求。它里面没有对话内容,每次调用时消息由外面传给它。

### 整个进程一份的

**`AppConfig`:`config.yaml` 在内存里的对象。** 一个字段对应配置文件里的一块:

```python
AppConfig(
    models = [ModelConfig(name="gpt-x", ...), ModelConfig(name="claude-y", ...)],
    tools  = [ToolConfig(name="web_search", ...), ToolConfig(name="read_file", ...)],
    memory = MemoryConfig(enabled=True, ...),
    summarization = SummarizationConfig(...),
    subagents = ...,
    database = ...,
    ...
)
```

它存在模块里的一个缓存变量 `_app_config` 中,旁边还存着文件的指纹 `(修改时间, 大小, 内容哈希)`。

**`ModelConfig`:系统配置里一个模型的完整信息。** 它是 `AppConfig.models` 列表里的一项,对应 `config.yaml` 里 `models` 下的一个条目。请求里只有模型的名字,怎么连、支持什么都靠它:

```python
ModelConfig(
    name     = "gpt-x",    # 系统内部用的名字,请求里填的就是它
    display_name = "GPT-X",       # 界面上显示的名字
    use      = "langchain_openai:ChatOpenAI",    # 用哪个类来创建模型对象
    model    = "gpt-x-2026",      # 发给模型厂商的真实型号
    api_key  = "sk-abc...",       # 密钥(加载配置时已从环境变量换成真实值)
    supports_thinking = True,     # 支不支持思考
    supports_reasoning_effort = True,    # 支不支持调节思考强度
    supports_vision   = True,     # 支不支持看图
    when_thinking_enabled = {...},      # 开启思考时要额外传给模型的参数
    context_window    = 200000,   # 上下文窗口大小
)
```

`name` 是在这个系统里给它起的名字,`model` 是厂商的型号,两者不同。`ModelConfig` 是配置数据(图纸),`chat_model` 是照着它造出来、能调用的对象(成品);前者全进程共用,后者每次任务新建。

### 落到 SQLite 里的

| 内容 | 存在哪 | 什么时候写 |
| --- | --- | --- |
| 整个状态(`ThreadState`) | `checkpoints` 表的 `checkpoint` 列 | 每执行完一步 |
| 实际用的模型名 | `runs` 表的 `model_name` | 组装完成后 |
| 组装记录(`descriptor` 整理后的内容) | 这次任务的记录上 | 组装完成后 |
| `config["metadata"]` 的内容 | 每个检查点的 `metadata` 列 | 每存一个检查点 |

### 存在磁盘上的

**自定义 Agent 的两个文件。** 每个自定义 Agent 一个文件夹,按用户分目录:

```text
.deer-flow/users/7f3c...0001/agents/my-researcher/
├── config.yaml     ← 设置:模型、工具组、技能、子 Agent 等。读进来是 AgentConfig
└── SOUL.md  ← 人设:这个 Agent 的身份、性格、行为准则。拼进系统提示词
```

`config.yaml` 的内容(示意):

```yaml
name: my-researcher
description: 专门做资料调研
model: claude-y
tool_groups:
  - web
skills:
  - deep-research
allowed_subagents: []
memory_enabled: true
```

这里的 `config.yaml` 和项目根目录那个 `config.yaml` 是两个文件,只是同名:根目录的管整个系统,读进来是 `AppConfig`;Agent 文件夹里的只管这一个 Agent,读进来是 `AgentConfig`。

存储位置由系统配置的 `agent_storage.backend` 决定:`file`(默认)是上面的磁盘文件;`db` 是数据库的 `agents` 表,每个 Agent 一行,供多个 Gateway 进程共享。

### 状态(执行中在内存,每一步存进检查点)

**`ThreadState`:Agent 执行时框架一直拿着的字典的结构定义。** 小明让 Agent 写一份天气报告并展示,任务结束时状态是:

```python
{
    "messages": [HumanMessage("写一份天气报告"), AIMessage(调用 web_search), ToolMessage("明天晴"),
                 AIMessage(调用 write_file), ToolMessage("已写入"), AIMessage(调用 present_files),
                 ToolMessage("已展示"), AIMessage("报告已生成")],
    "title": "天气报告",
    "thread_data": {"workspace_path": "...", "uploads_path": "...", "outputs_path": "..."},
    "sandbox": {"sandbox_id": "local"},
    "artifacts": ["/mnt/user-data/outputs/report.md"],
    "todos": None,
}
```

**一份更新:工具或中间件交回的字典。** 它们不直接改状态,而是说"我想把哪些字段改成什么":

```python
Command(update={
    "artifacts": ["/mnt/user-data/outputs/report.md"],
    "messages": [ToolMessage("Successfully presented files")],
})
```

### 四个带"config"的名字

| 名字 | 是什么 | 多少份 |
| --- | --- | --- |
| `config.yaml` | 磁盘上的配置文件 | 一个文件 |
| `app_config` / `AppConfig` | `config.yaml` 读进内存后的对象 | 全进程一份 |
| `config` | 这次任务的运行配置字典 | 每次任务一个 |
| `cfg` | `config` 里两个分区合并后的字典 | 每次任务一个 |

前两个是一回事的两种形态(文件和对象),后两个是一回事的两种形态(分区的和合并的)。真正不同的只有两样:**系统的配置**和**这次任务的配置**。

## 二、组装在哪里被触发,拿到什么输入

### 全貌:这一步只有一件事

用两样材料(`config` 和 `app_config`),调用一个函数,得到一个 Agent。整个过程是三层函数,一层调用一层:

```text
① start_run     准备材料,决定用哪个函数来组装
    ↓ 调用
② run_agent     在后台执行。它调用那个组装函数,拿到 Agent,然后运行它
    ↓ 调用
③ assemble_lead_agent  组装函数。输入 config 和 app_config,输出 Agent
```

这三个函数里,`start_run` 是路由函数调用的那个;`run_agent` 就是第零课讲的"后台任务",里面有 `async for item in stream` 那个循环;`assemble_lead_agent` 是本课新出现的。

主线只有六句话:

1. 前端在请求体里发来 `assistant_id`,默认是 `"lead_agent"`。
2. `start_run` 准备好材料 `config`,并通过 `resolve_agent_factory` 确定组装函数是 `assemble_lead_agent`,存在变量 `agent_factory` 里。此时还没调用。
3. `start_run` 把 `config`、`agent_factory` 和装着 `app_config` 的工具箱 `ctx` 交给后台的 `run_agent`。
4. `run_agent` 往 `config` 里补上 `run_id` 等,然后调用 `agent_factory(config=..., app_config=...)`,也就是 `assemble_lead_agent(...)`。
5. `assemble_lead_agent` 返回一个包,里面有 Agent 本体、一份组装记录、模型名。
6. `run_agent` 从包里取出 Agent,之后用 `agent.astream(...)` 运行它。

旁支(不影响理解主线):`journal` 统计 token;组装记录存进数据库;组装放在另一个线程里执行。

### ① `start_run`:准备材料 `config`

`start_run`(`backend/app/gateway/services.py`)分四次往 `config` 里填东西。

**第一次:搭骨架。**

```python
config = build_run_config(thread_id, body.config, run_metadata, assistant_id=body.assistant_id)
```

`build_run_config` 先定执行步数上限,再放入对话 ID:

```python
default_recursion_limit, max_recursion_limit = _resolve_gateway_recursion_limits()
config: dict[str, Any] = {"recursion_limit": default_recursion_limit}
...
config["configurable"] = {"thread_id": thread_id}
```

小明请求体里的 `config` 是 `{"recursion_limit": 1000}`,不超过服务端上限,采纳。此时:

```python
config = {
    "recursion_limit": 1000,
    "configurable": {"thread_id": "a1b2c3"},
    "metadata": {"deerflow_trace_id": "9b1f..."},
}
```

保护:步数上限不直接信客户端。如果照单全收,客户端传一个极大的值,一次任务就能无限循环调用模型,费用失控。所以超过上限的会被压到上限以内。

**第二次:并入前端发来的运行选项。**

```python
merge_run_context_overrides(config, getattr(body, "context", None), internal=is_internal_caller)
```

函数只挑白名单里的键,而且两个分区各放一份:

```python
keys = _CONTEXT_CONFIGURABLE_KEYS | _CONTEXT_INTERNAL_CALLER_KEYS if internal else _CONTEXT_CONFIGURABLE_KEYS
for key in keys:
    if key in context:
        configurable.setdefault(key, context[key])
        runtime_context.setdefault(key, context[key])
```

放两份的原因:LangGraph 在版本演进中把运行选项的位置从 `configurable` 换到了 `context`,项目里新老代码各读各的,所以两处都写。

**第三次:删掉客户端不该设置的键。**

```python
if not is_internal_caller:
    strip_internal_context_keys(config)
```

`non_interactive`、`github_token` 这类键只有后端内部调用方能设。小明是普通网页用户,这些键即使出现也会被删。

**第四次:写入用户身份。**

```python
runtime_context["user_id"] = str(user_id)
runtime_context["user_role"] = getattr(user, "system_role", None)
```

这是 `inject_authenticated_user_context` 里的两行。用户 ID 和角色来自认证层查出的 `User` 对象,不是客户端传的。函数开头还会先把客户端自带的 `user_id` 删掉,所以伪造不了。

四次之后 `config` 里有了:步数上限、对话 ID、运行选项(两个分区各一份)、用户身份、追踪编号。

### ① `start_run`:确定用哪个函数来组装

**`assistant_id` 从哪来。** 它是请求体里的一个字段,前端发来的,含义是"这次用哪个 Agent":

```json
{
  "assistant_id": "lead_agent",
  "input": { "messages": ["..."] },
  "context": { "model_name": "gpt-x" }
}
```

后端的请求体模型里对应这一行(`run_models.py`):

```python
assistant_id: str | None = Field(default=None, description="Agent / assistant to use")
```

前端默认填 `"lead_agent"`。如果用户在用自己创建的自定义 Agent,填的是那个 Agent 的名字。

**用它来选函数**(`services.py` 第 2374 行):

```python
agent_factory = await run_assembly(resolve_agent_factory, body.assistant_id)
```

意思是调用 `resolve_agent_factory("lead_agent")`,把返回值存进变量 `agent_factory`。这个函数的全部内容是:

```python
def resolve_agent_factory(assistant_id: str | None):
    from deerflow.agents.lead_agent.agent import assemble_lead_agent
    return assemble_lead_agent
```

最后一行返回的是 `assemble_lead_agent`,后面**没有括号**。没有括号表示"把这个函数本身交出去",不是调用它。所以执行完之后:

```python
agent_factory = assemble_lead_agent      # agent_factory 现在是这个函数的另一个名字
```

此刻还没有组装任何东西。只是决定了"待会儿用这个函数来组装"。打个比方:要装修,先找好了施工队,记下了队长的电话。记电话不等于开工。`agent_factory` 就是那个电话号码。

不管 `assistant_id` 是什么,返回的都是同一个函数。自定义 Agent 的区别体现在 `config` 里多一个 `agent_name` 字段,这是 `build_run_config` 里写入的:

```python
normalized = custom_agent_name(assistant_id)
if normalized is not None:
    ...
    configurable["agent_name"] = effective_agent_name
    runtime_context["agent_name"] = effective_agent_name
```

| `assistant_id` | `config` 里多了什么 | 结果 |
| --- | --- | --- |
| `lead_agent`(小明的情况) | 什么都不多 | 默认主 Agent |
| `my-researcher`(假设的自定义 Agent) | `agent_name = "my-researcher"` | 同一个组装函数,读到这个名字后去加载它的专属配置 |

所以只有一套组装代码,"是哪个 Agent"只是 `config` 里的一个字段。组装代码怎样读这个名字在第 2 步。

### ① `start_run`:把东西交给后台任务

```python
await run_agent(bridge, run_mgr, record, ctx=execution_context,
                agent_factory=agent_factory,     # 待会儿用来组装的函数
                graph_input=graph_input,  # 用户这条消息
                config=config,     # 材料
                ...)
```

`run_agent` 就是第零课讲的"后台任务"的主体。这一行在 `start_run` 内部定义的函数 `run_after_metadata` 里,而这个函数通过第零课见过的那一句被放到后台:

```python
worker = run_after_metadata(record)
record.task = asyncio.create_task(worker)
```

参数里 `bridge`、`run_mgr`、`record` 是第零课讲过的事件通道、任务管理器、任务记录。和本课有关的是另外四个:

| 参数 | 是什么 |
| --- | --- |
| `agent_factory` | 待会儿用来组装的函数 |
| `graph_input` | 用户这条消息 |
| `config` | 这次任务的运行配置 |
| `ctx` | 工具箱 `RunContext`,`app_config` 装在它里面 |

`ctx` 是 `start_run` 一开头创建的(`services.py` 第 2228 行):

```python
bridge = get_stream_bridge(request)
run_mgr = get_run_manager(request)
run_ctx = get_run_context(request)
```

`get_run_context`(`app/gateway/deps.py`)把 `app.state` 上的检查点、存储等对象取出来装进一个 `RunContext`,其中 `app_config` 这一项是取当前的应用配置:

```python
return RunContext(
    checkpointer=checkpointer,
    store=get_store(request),
    thread_store=get_thread_store(request),
    app_config=get_config(), # 取当前的应用配置
    ...
)
```

`run_ctx` 后面改名叫 `execution_context`,作为 `ctx` 传给 `run_agent`。

### ② `run_agent`:调用之前往 `config` 里补东西

进入 `run_agent`(`runtime/runs/worker.py`)。`start_run` 准备 `config` 时有些信息还没有(比如 `run_id` 是登记任务之后才有的),所以调用组装函数之前先补上。

**从工具箱里取出 `app_config`**(第 1359 行):

```python
agent_app_config = ctx.app_config
```

**建 `runtime_ctx`,写回 `config["context"]`:**

```python
runtime_ctx = _build_runtime_context(thread_id, run_id, config.get("context"), agent_app_config, ...)
...
_install_runtime_context(config, runtime_ctx)
```

`runtime_ctx` 第零课讲过。它以 `config["context"]` 为基础,加上 `thread_id`、`run_id`、`app_config`、追踪编号,然后写回 `config["context"]`。所以两者说的是同一批内容。

**建 `Runtime` 对象:**

```python
runtime = Runtime(context=cast(Any, runtime_ctx), store=store)
config.setdefault("configurable", {})["__pregel_runtime"] = runtime
```

把它放进 `config["configurable"]["__pregel_runtime"]` 是 LangGraph 的约定,执行时框架会把这个对象传给每个工具和中间件。

**旁支:挂上 `journal`。** 它和组装没有关系,只是恰好也在这个位置被放进 `config`:

```python
if journal is not None:
    config.setdefault("callbacks", []).append(journal)
```

它的用途:小明这次任务里模型被调用了两次,第一次用了 1200 个 token,第二次 800 个。任务结束后 `runs` 表里 `r-5566` 那一行的 `total_tokens` 是 2000。这个 2000 就是 `journal` 数出来的。LangChain 允许在 `config["callbacks"]` 里放"监听器",每次模型调用结束,框架会通知列表里的每个监听器并告诉它用了多少 token。`journal` 是 DeerFlow 写的一个监听器(`RunJournal`),收到通知就把数字加起来。它不影响 Agent 怎么组装。

### ② `run_agent`:调用组装函数

```python
agent_factory_kwargs: dict[str, Any] = {"config": initial_runnable_config}
if agent_app_config is not None and _agent_factory_supports_app_config(agent_factory):
    agent_factory_kwargs["app_config"] = agent_app_config
...
agent_result = await run_assembly(agent_factory, **agent_factory_kwargs)
```

这几行只是在准备参数、然后调用:

| 代码 | 意思 |
| --- | --- |
| `agent_factory_kwargs = {"config": initial_runnable_config}` | 准备一个参数字典,里面放 `config`(`initial_runnable_config` 就是 `config`,按 LangChain 规定的类型包了一层,内容不变) |
| `if ...: agent_factory_kwargs["app_config"] = agent_app_config` | 如果组装函数接受 `app_config` 参数,就把它也放进去 |
| `await run_assembly(agent_factory, **agent_factory_kwargs)` | 用这些参数调用 `agent_factory` |

`**agent_factory_kwargs` 是 Python 的语法:把字典展开成参数。字典是 `{"config": config, "app_config": app_config}`,展开后就是 `config=config, app_config=app_config`。

`run_assembly(函数, 参数)` 的意思是"在另一个线程里调用这个函数"。去掉这层,这一行等价于:

```python
agent_result = agent_factory(config=config, app_config=app_config)
```

而 `agent_factory` 就是 `assemble_lead_agent`,所以实际上是:

```python
agent_result = assemble_lead_agent(config=config, app_config=app_config)
```

带上括号、传了参数,这才是真正的调用。程序从这里跳进 `assemble_lead_agent` 函数内部去执行。等它执行完返回,结果存进 `agent_result`,程序回到这里继续往下。

```text
① start_run:   agent_factory = assemble_lead_agent        ← 只是记下用哪个函数(没括号)
            ↓ 作为参数传给 run_agent
② run_agent:   agent_result = agent_factory(config=..., app_config=...)    ← 真正调用(有括号)
            ↓ 等价于 assemble_lead_agent(config=..., app_config=...)
③ 进入 assemble_lead_agent 函数内部
```

保护:组装放到另一个线程里执行。组装过程会去取工具列表,其中 MCP 工具的初始化可能要等待。如果在事件循环里直接做,这段等待期间所有其他用户的请求都会被卡住。

### `config` 和 `app_config` 的区别

它们是两个完全不同的东西,只是名字里都有"config"。

| | `config` | `app_config` |
| --- | --- | --- |
| 类型 | 一个字典 | 一个 `AppConfig` 对象 |
| 来源 | 这次请求(前端发来的选项、用户身份、对话 ID) | `config.yaml` 文件 |
| 多少份 | 每次任务一个 | 整个应用一份,所有人共用 |
| 回答的问题 | **这一次**要怎么跑 | **这个系统**有什么、允许什么 |

组装时两个都要用。以"选模型"为例:

| 问题 | 去哪里查 | 得到 |
| --- | --- | --- |
| 小明这次想用哪个模型? | `config` | `"gpt-x"`,只是一个名字 |
| `gpt-x` 怎么连?用哪个类、地址、密钥是什么、支不支持思考? | `app_config.models` | 一个完整的 `ModelConfig` |

只有 `config`,知道名字但不知道怎么连;只有 `app_config`,知道系统里有哪些模型但不知道小明要哪个。打个比方:去餐厅吃饭,`config` 是你这桌的点菜单("要一份宫保鸡丁,少辣"),每桌一张;`app_config` 是餐厅的菜单和厨房配置,全店一份。厨师做菜两样都得看。

**`app_config` 也在 `config` 里有一份。** `config["context"]` 里有 `"app_config"` 这一项,所以单独再传一次看起来是重复的。组装函数是这样取它的(`agents/lead_agent/agent.py`):

```python
runtime_app_config = app_config or runtime_config.get("app_config")
if not isinstance(runtime_app_config, AppConfig):
    runtime_app_config = get_app_config()
```

| 顺序 | 来源 | 什么时候用到 |
| --- | --- | --- |
| 1 | 单独传进来的参数 `app_config` | Gateway 的后台任务调用时(现在的情况) |
| 2 | `config` 里带的那一份 | 调用方没单独传时 |
| 3 | 自己去取 | 前两个都没有时 |

留三条路是因为这个组装函数有不同的调用方。LangGraph 官方的开发工具按它的规定只会传一个 `config`:

```python
def make_lead_agent(config: RunnableConfig):
    return assemble_lead_agent(config).graph      # 只传了 config
```

后台任务这边调用前还检查了组装函数收不收 `app_config` 这个参数(`_agent_factory_supports_app_config`),收才传。这是为了兼容只接受 `config` 一个参数的自定义组装函数。

结论:单独传是首选的方式;`config` 里那一份是给工具和中间件在运行时读取用的,顺便也充当组装时的备用来源。默认情况下两处是同一个对象,理解成"组装函数拿到了 `config` 和 `app_config` 两样材料"就可以。

### `app_config` 是什么时候创建的,这次任务里怎么传过来的

**创建。** Gateway 进程启动时,在 `lifespan` 里第一次调用 `get_app_config()`,和创建事件通道、数据库、任务管理器是同一批,而且排在最前面,因为后面创建每个对象都要先查配置。它做的事是:读 `config.yaml`,校验后变成一个 `AppConfig` 对象,存进进程里的一个缓存变量。配置写错了,Gateway 启动失败并说明原因。

**指纹。** 读文件时顺便算出一个指纹:文件的修改时间、大小、内容哈希,和对象一起存进缓存。之后任何地方调用 `get_app_config()`,都会重新算一次指纹和缓存里的比:

| 指纹 | 结果 |
| --- | --- |
| 一样 | 返回缓存里的对象 |
| 不一样(文件被改过) | 重新读文件,创建新对象,替换缓存 |

**和其他在 `lifespan` 里创建的对象的区别:**

| | 事件通道、数据库、任务管理器 | 配置 |
| --- | --- | --- |
| 存在哪 | `app.state` 上 | 模块里的缓存变量 |
| 怎么取 | `get_xxx(request)`,只是取出来 | `get_app_config()`,不需要 `request`,取的时候检查文件有没有变 |
| 启动之后 | 不变,到进程退出都是同一个 | 改了 `config.yaml` 就换成新的 |
| 改配置后 | 要重启才生效 | 下一次任务就生效 |

**这次任务里它走过的路:**

| 顺序 | 位置 | 代码 | 发生了什么 |
| --- | --- | --- | --- |
| 1 | `start_run` 开头 | `run_ctx = get_run_context(request)` | 取到 `AppConfig`,装进工具箱 `RunContext` |
| 2 | `start_run` 末尾 | `run_agent(..., ctx=execution_context, ...)` | 工具箱整个传给后台任务 |
| 3 | `run_agent` 里 | `agent_app_config = ctx.app_config` | 从工具箱里取出来 |
| 4 | `run_agent` 里 | `_build_runtime_context(..., agent_app_config, ...)` | 放一份进 `runtime_ctx`,给工具和中间件用 |
| 5 | `run_agent` 里 | `agent_factory_kwargs["app_config"] = agent_app_config` | 作为参数交给组装函数 |

任务开始时拿到哪个对象,整个任务就用哪个。任务跑到一半时管理员改了配置,这次任务不受影响,新配置从下一次任务开始生效。

### `app_config` 在组装中什么时候参与

从头到尾。组装函数一进来就拿在手里,之后每组装一样东西都要查它:

| 环节 | `config` 说了什么 | 去 `app_config` 里查什么 |
| --- | --- | --- |
| 检查点模式 | (不能由请求决定) | `database.checkpoint_channel_mode`:全量还是增量 |
| 子 Agent 上限 | 请求希望的并发数和总数 | `subagents` 里配的系统上限,取较小的 |
| 技能 | 用户是谁 | `skills`:启用了哪些技能,技能目录在哪 |
| 模型 | 想用的模型名 | `models`:这个名字存不存在、怎么连、支持什么 |
| 工具 | 子 Agent 开关 | `tools`、`tool_groups`:系统配了哪些工具 |
| 中间件 | Plan 模式开关等 | `summarization`、`memory`、`loop_detection`、`token_usage` 等十多项功能的开关和参数 |
| 系统提示词 | | 技能目录路径等 |

两样材料的配合方式有四种:

| 关系 | 例子 | 谁说了算 |
| --- | --- | --- |
| 请求选,系统供 | 模型:请求给名字,系统给连接方式 | 请求在系统提供的范围内选 |
| 请求要,系统限 | 子 Agent 数量、执行步数上限 | 取较小的,系统是天花板 |
| 系统单方面定 | 摘要、循环检测、token 统计是否启用 | 只看 `app_config` |
| 请求单方面定 | Plan 模式、是否启用子 Agent | 只看 `config` |

以模型为例。系统里配了 `gpt-x`(默认)和 `claude-y`:

| 小明的 `config` 里 `model_name` 是 | 去 `app_config.models` 里查 | 结果 |
| --- | --- | --- |
| `"gpt-x"` | 找到了 | 用 `gpt-x` |
| `"不存在的模型"` | 找不到 | 记一条警告,退回默认的 `gpt-x`(列表第一个) |
| 没填 | | 用默认的 `gpt-x` |

这些环节的细节在第 2 到 6 步展开。

### ③ 组装函数返回什么,后台任务拿它做什么

`assemble_lead_agent` 内部怎么组装是第 2 到 6 步的内容。这里只看它返回什么:一个 `LeadAgentAssembly`,里面有三样东西(`agents/lead_agent/agent.py`):

```python
class LeadAgentAssembly:
    graph: Any         # 组装好的 Agent
    descriptor: Any    # 一份记录:这个 Agent 是用什么拼出来的
    effective_model: str | None = None   # 实际用的模型名
```

不直接返回 Agent 而要包成三样,是因为调用方除了要 Agent 本身,还想知道"它是怎么配出来的"。回到 `run_agent`,它拿到这个包之后做两件事。

**第一件:把 Agent 取出来。**

```python
agent = _agent_graph(agent_result)        # 相当于 agent = agent_result.graph
```

这个 `agent` 就是第零课里那一句用到的:

```python
stream = agent.astream(input_payload, config=stream_config, ...)
async for item in stream:
    ...
```

到这里和第零课接上了。第零课从 `agent.astream` 开始讲,这一步讲的是 `agent` 这个变量是怎么来的。

**第二件(旁支):把那份记录存进数据库。**

```python
if isinstance(agent_result, LeadAgentAssembly):
    evidence = project_assembly_evidence(agent_result.descriptor, thread_id=thread_id, run_id=run_id)
    if evidence is not None:
        await run_manager.record_assembly_evidence(run_id, evidence)
```

| 代码 | 意思 |
| --- | --- |
| `if isinstance(agent_result, LeadAgentAssembly):` | 如果返回的确实是那个三样东西的包 |
| `evidence = project_assembly_evidence(agent_result.descriptor, ...)` | 把包里的 `descriptor` 整理成适合存储的格式 |
| `await run_manager.record_assembly_evidence(run_id, evidence)` | 存到这次任务的记录上 |

为什么要存:一周后小明反馈"上周二那次回答质量很差"。你想知道当时 Agent 是什么配置,但这一周里管理员可能改过 `config.yaml`,现在的配置已经不是当时的了。有了这份记录,能查到"`r-5566` 这次任务当时用的是哪个模型、有哪些工具、开了哪些功能"。它和第零课讲的追踪编号是同一类东西:为事后排查留底。

### 进入组装函数后先做的两件事

`assemble_lead_agent`(`agents/lead_agent/agent.py`)在真正组装之前做两件准备。

**把两个分区合成一个字典 `cfg`。** 后面每一步都要用这个小函数:

```python
def _get_runtime_config(config: RunnableConfig) -> dict:
    """Merge legacy configurable options with LangGraph runtime context."""
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)
    return cfg
```

先拿 `configurable`,再用 `context` 覆盖。后面讲组装时反复出现的 `cfg.get("is_plan_mode")`、`cfg.get("subagent_enabled")`,读的就是它。有了这个函数,组装代码不用关心一个选项到底放在哪个分区。

**确定检查点的存储模式,然后进入真正的组装:**

```python
mode = freeze_checkpoint_channel_mode(requested_mode)
...
return _assemble_lead_agent(config, app_config=runtime_app_config)
```

`mode` 是第零课讲过的 `full` 或 `delta`。"freeze"的意思是进程里第一次确定之后就固定下来,之后不允许变。它决定了状态的存储结构,同一个进程里时而全量、时而增量,检查点会读不出来。客户端塞进来的同名键会被忽略,不能借此改变进程的模式。

`_assemble_lead_agent` 是真正干活的函数,约 450 行,第 2 到 6 步讲它。

### 为什么每次任务都重新组装

小明每发一条消息,上面的流程走一遍,组装出一个新的 Agent。不复用上一次的,是因为决定 Agent 长什么样的因素每次都可能不同:

| 因素 | 来自哪 | 例子 |
| --- | --- | --- |
| 运行选项 | 这次请求的 `context` | 上一条用 flash 模式,这一条用 ultra,工具和中间件都不一样 |
| 用户身份 | 认证层 | 不同用户有不同的权限和自定义技能 |
| 应用配置 | `config.yaml` | 管理员改了配置,下一次任务就生效,不用重启 |
| 哪个 Agent | `agent_name` | 默认 Agent 还是某个自定义 Agent |

组装出来的 Agent 只是"怎么执行"的规则(模型、工具、提示词、中间件),不含对话内容。对话内容在检查点里,靠 `config["configurable"]["thread_id"]` 找到。所以每次重新组装不会丢失上下文。

### 需要记住的

1. 组装需要两样材料:`config`(这次任务的选项)和 `app_config`(系统配置)。前者每次任务一个,后者全进程一份。
2. `assistant_id` 是请求体里的字段。不管它是什么,组装函数都是 `assemble_lead_agent`;自定义 Agent 只是 `config` 里多一个 `agent_name`。
3. `start_run` 里 `agent_factory = assemble_lead_agent` 只是记下用哪个函数;真正的调用发生在后台的 `run_agent` 里:`agent_factory(config=..., app_config=...)`。
4. `config` 由 `start_run` 填四次(骨架、运行选项、删敏感键、用户身份),后台任务再补上 `run_id`、`app_config` 等。运行选项在 `configurable` 和 `context` 里各有一份,组装代码用 `_get_runtime_config` 合成 `cfg` 来读。
5. `app_config` 在 Gateway 启动时创建,改了 `config.yaml` 会自动更新。它随工具箱 `RunContext` 传到后台任务,再交给组装函数,在组装的每个环节都被用到。
6. 组装函数返回 `LeadAgentAssembly`:Agent 本体、一份组装记录、模型名。后台任务取出 Agent,用 `agent.astream(...)` 运行。
7. 每次任务都重新组装。组装出来的只是执行规则,对话内容在检查点里。

## 三、读取运行选项,自定义 Agent 怎样"变身"

上一步停在:`assemble_lead_agent` 调用了 `_assemble_lead_agent(config, app_config=...)`。

这一步是 `_assemble_lead_agent` 开头约 40 行。只做一件事:把后面组装要用的选项从字典里读出来、算好、定下来,存成变量。还没有创建任何东西。

函数进来时手里是两样材料:

| 参数 | 是什么 | 小明这次的内容 |
| --- | --- | --- |
| `config` | 这次任务的运行配置字典 | 含 `thread_id`、小明选的模式和模型名、用户 ID |
| `app_config` | 系统配置对象 | 含系统里配的模型 `gpt-x` 和 `claude-y`、各功能开关 |

### `config` 不是用户发的请求

读选项之前先分清一件事:用户发的是**请求体**,`config` 是**后端根据请求体造出来的另一个东西**。两者不是同一个对象。

用户发的请求体(一段 JSON,从浏览器传过来):

```json
{
  "assistant_id": "lead_agent",
  "input":   { "messages": ["..."] },
  "context": { "model_name": "gpt-x", "is_plan_mode": true, "subagent_enabled": false },
  "config":  { "recursion_limit": 1000 }
}
```

后端造出来的 `config`(一个 Python 字典,只存在于后端内存里):

```python
config = {
    "recursion_limit": 1000,
    "configurable": {"thread_id": "a1b2c3", "model_name": "gpt-x", ...},
    "context": {
        "model_name": "gpt-x", "is_plan_mode": True, "subagent_enabled": False,
        "user_id": "7f3c...0001",   # 请求体里没有,后端加的
        "run_id": "r-5566",  # 请求体里没有,后端加的
        "app_config": <AppConfig 对象>,   # 请求体里没有,后端加的
        ...
    },
    "metadata": {...},
}
```

| | 请求体 | 后端的 `config` |
| --- | --- | --- |
| 谁创建的 | 前端 | 后端的 `start_run` |
| 在哪 | 从浏览器传到后端的一段 JSON | 后端内存里的一个字典 |
| 里面有 `user_id` 吗 | 没有(有也会被删) | 有,后端根据登录身份加的 |
| 里面有 `run_id`、`app_config` 吗 | 没有 | 有,后端加的 |
| 用户发的内容全都进去了吗 | | 没有,只有白名单里的键被采纳 |

容易混的原因是名字重了:请求体里恰好有字段叫 `config` 和 `context`,后端的 `config` 字典里也有一个分区叫 `context`。

| 名字 | 是什么 |
| --- | --- |
| 请求体的 `config` 字段 | 用户发来的一小块,通常只有 `recursion_limit` |
| 请求体的 `context` 字段 | 用户发来的运行选项 |
| 后端的 `config` 字典 | 后端造的,把上面两块里**允许的部分**吸收进来,再加上后端自己的信息 |

所以请求体是原料之一,`config` 是后端加工后的成品。第 1 步讲的"分四次填"就是这个加工过程。

打个比方:去医院填了一张挂号单(请求体),上面有你写的症状和想挂的科室。护士拿着它另开了一份病历(`config`):把挂号单上的有效信息抄过来,再加上你的就诊号、医保身份这些你自己填不了的内容。后面医生看的是病历,不是那张挂号单。

### 前半:从 `cfg` 里读出这次任务的选项

```python
cfg = _get_runtime_config(config) # 把 config 的两个分区合并成一个字典 cfg,后面都从它读选项
resolved_app_config = app_config  # 给系统配置起个别名,后面都叫 resolved_app_config

resolved_user_id = resolve_config_user_id(config)    # 确定这次任务的用户是谁

requested_model_name = cfg.get("model_name") or cfg.get("model")     # 请求想用的模型名
is_plan_mode = cfg.get("is_plan_mode", False)  # 是否开启 Plan 模式,没填算 False
requested_subagent_enabled = cfg.get("subagent_enabled", False)      # 请求是否想启用子 Agent,没填算 False

subagent_execution_capacity = configured_subagent_max_running()      # 取进程启动时定下的"同时最多跑几个子 Agent"
max_concurrent_subagents = effective_subagent_concurrency(    # 算出:同一时刻最多几个子 Agent 在跑
    cfg.get("max_concurrent_subagents"),     #   请求里想要的数(可能没填)
    resolved_app_config,   #   系统配置
    execution_capacity=subagent_execution_capacity,   #   上面取到的进程容量,作为天花板
)
max_total_subagents = effective_total_subagents_per_run(      # 算出:这次任务总共最多派几个子 Agent
    cfg.get("max_total_subagents"),   #   请求里想要的数(可能没填)
    resolved_app_config,   #   系统配置
)

is_bootstrap = cfg.get("is_bootstrap", False)        # 是不是"引导模式"(创建自定义 Agent 时用),平时是 False
```

里面调用的几个小函数的作用:

| 函数 | 作用 |
| --- | --- |
| `_get_runtime_config(config)` | 把 `config["configurable"]` 和 `config["context"]` 合成一个字典返回 |
| `resolve_config_user_id(config)` | 返回这次任务的用户 ID。取的是 `start_run` 根据登录身份写进 `config` 的那个值 |
| `configured_subagent_max_running()` | 返回进程启动时定下的子 Agent 并发容量 |
| `effective_subagent_concurrency(请求值, 系统配置, 容量)` | 返回最终的并发上限:请求填了就用请求的,没填用容量;结果不会超过容量 |
| `effective_total_subagents_per_run(请求值, 系统配置)` | 返回最终的总数上限:请求填了就用请求的,没填用系统配置里的;结果会被限制在允许的最小值和最大值之间 |

后两个函数是同一个规律:请求可以要得少,但突破不了系统的范围。

小明这次读出来的结果:

| 变量 | 值 | 是不是最终值 |
| --- | --- | --- |
| `resolved_user_id` | `"7f3c...0001"` | 是 |
| `requested_model_name` | `"gpt-x"` | 否,第 3 步确认 |
| `is_plan_mode` | `True` | 是 |
| `requested_subagent_enabled` | `False` | 否,后半确认 |
| `max_concurrent_subagents` | 系统容量内的一个数 | 是 |
| `max_total_subagents` | 系统配置的值 | 是 |
| `is_bootstrap` | `False` | 是 |

这些值的来历:小明在界面上选了 pro 模式,前端把它翻译成 `is_plan_mode: true`、`subagent_enabled: false` 放进请求体,`start_run` 并入 `config`,现在在这里读出来。

带 `requested_` 前缀的两个变量是"请求想要的",还要经过系统检查才算数。`is_plan_mode` 没有这个前缀,因为它由请求单方面决定,读出来就是最终值。

为什么组装要先确定用户是谁:后面有几样东西是按用户分的,比如每个用户可以有自己的自定义技能和自定义 Agent,权限也按用户判断。所以先把用户定下来,后面都用这一个值。

引导模式是用户在网页上创建自定义 Agent 时用的一个特殊 Agent。后面代码里多处出现 `if is_bootstrap:`,对小明来说都走"不是"的那一边,本课不展开。

这一段中间还夹着几行和定时任务有关的代码(交互策略、调度能力),和小明的情况无关,跳过。

### 后半:读出 `agent_name`,加载自定义 Agent 的配置

紧接着的 7 行。做两件事:看这次用的是不是自定义 Agent,是的话把它的专属配置读出来;然后把子 Agent 开关最终定下来。

```python
agent_name = validate_agent_name(cfg.get("agent_name"))
# 读出自定义 Agent 的名字;默认 Agent 时是 None

agent_config = load_agent_config(agent_name, user_id=resolved_user_id) if not is_bootstrap else None
# 按名字加载这个 Agent 的专属配置;名字是 None 就得到 None

memory_enabled = getattr(agent_config, "memory_enabled", True) is not False
# 这个 Agent 是否启用记忆;没有专属配置时默认启用

allowed_subagents = getattr(agent_config, "allowed_subagents", None) if agent_config is not None else None
# 这个 Agent 允许调用哪些子 Agent;没有专属配置时是 None(不限制)

subagent_enabled = bool(requested_subagent_enabled and allowed_subagents != [])
# 最终是否启用子 Agent:请求想启用,并且这个 Agent 没有被禁止

config.setdefault("context", {})["subagent_enabled"] = subagent_enabled
# 把最终结果写回 config,让后面的工具和中间件读到的是最终值

available_skills = _available_skill_names(agent_config, is_bootstrap)
# 这个 Agent 可以用哪些技能;没有专属配置时是 None(不限制)
```

| 函数 | 作用 |
| --- | --- |
| `validate_agent_name(名字)` | 检查名字格式是否合法(只允许字母、数字、连字符),合法就原样返回;传入 `None` 返回 `None` |
| `load_agent_config(名字, user_id)` | 按名字和用户找到这个自定义 Agent 保存的配置,返回一个 `AgentConfig` 对象;名字是 `None` 时返回 `None` |
| `_available_skill_names(agent_config, is_bootstrap)` | 从 `AgentConfig` 里取出技能名单;没有配置或没限定技能时返回 `None` |

`AgentConfig` 的字段和例子见第一节。

### 两种情况对比

**情况一:小明用的是默认 Agent(这次的实际情况)。** `assistant_id` 是 `"lead_agent"` 时,`config` 里没有 `agent_name`:

| 变量 | 值 | 含义 |
| --- | --- | --- |
| `agent_name` | `None` | 不是自定义 Agent |
| `agent_config` | `None` | 没有专属配置 |
| `memory_enabled` | `True` | 默认启用记忆 |
| `allowed_subagents` | `None` | 不限制 |
| `subagent_enabled` | `False` | 请求没开(pro 模式) |
| `available_skills` | `None` | 不限制技能 |

默认 Agent 没有 `AgentConfig`,所以各项都是"不限制",完全按请求和系统配置来。

**情况二:假设小明用的是 `my-researcher`,并且选了 ultra 模式。** 这时 `config` 里有 `agent_name = "my-researcher"`,而 ultra 模式下请求里 `subagent_enabled` 是 `True`:

| 变量 | 值 | 含义 |
| --- | --- | --- |
| `agent_name` | `"my-researcher"` | 自定义 Agent |
| `agent_config` | 第一节里那个 `AgentConfig` | 读到了专属配置 |
| `memory_enabled` | `True` | 配置里写的 |
| `allowed_subagents` | `[]` | 一个都不允许 |
| `requested_subagent_enabled` | `True` | 请求想启用 |
| `subagent_enabled` | `False` | 被这个 Agent 的配置否决了 |
| `available_skills` | `{"deep-research"}` | 只能用这一个技能 |

这就是"变身":同一个组装函数,因为读到了不同的 `AgentConfig`,后面拿到的模型、工具、技能都不一样。第 1 步说的"自定义 Agent 只是 `config` 里多一个 `agent_name`",落实到代码就是 `load_agent_config` 这一行。

### 子 Agent 开关是怎么定下来的

```python
subagent_enabled = bool(requested_subagent_enabled and allowed_subagents != [])
```

两个条件同时成立才启用:请求想启用,并且这个 Agent 的 `allowed_subagents` 不是空列表。

| 请求想启用吗 | Agent 的 `allowed_subagents` | 最终 `subagent_enabled` |
| --- | --- | --- |
| 否 | 任意 | `False` |
| 是 | `None`(不限制) | `True` |
| 是 | `["general-purpose"]`(只允许这一种) | `True` |
| 是 | `[]`(一个都不允许) | `False` |

请求里的开关可以**关掉**子 Agent,但不能**放宽** Agent 自身的限制。创建者把 `allowed_subagents` 设成空列表,就是明确禁止,使用者切换到 ultra 模式也绕不过去。这解释了为什么前半的变量名叫 `requested_subagent_enabled`:它只是请求的意愿,到这里才得到最终的 `subagent_enabled`。

### "把结果写回 `config`"是什么意思

```python
config.setdefault("context", {})["subagent_enabled"] = subagent_enabled
```

意思是:后端修改自己造的那个字典里的一个值。不是改用户的请求,用户那边不受任何影响,也看不到这个改动。

为什么要改。用情况二(小明用 `my-researcher`,选了 ultra 模式)来看这个值的变化:

| 时刻 | `config["context"]["subagent_enabled"]` | 说明 |
| --- | --- | --- |
| `start_run` 并入请求选项后 | `True` | 照抄请求体里的值,这是小明**想要**的 |
| 组装函数读出 `AgentConfig` | 还是 `True` | 但已经发现这个 Agent 的 `allowed_subagents` 是 `[]`,不允许 |
| 算出 `subagent_enabled = False` | 还是 `True` | 最终决定是不启用,但字典里还留着旧值 |
| 执行"写回"这一行 | `False` | 把字典里的值改成最终决定 |

如果不执行最后一步,就会出现不一致:组装函数自己知道最终结果是 `False`,所以不给 Agent 装子 Agent 工具;但 `config["context"]` 里还写着 `True`。

而 `config["context"]` 就是 `runtime_ctx`,任务运行期间工具和中间件通过 `runtime.context` 读它。比如某个中间件要判断"这次任务有没有启用子 Agent",读到 `True`,就会按"启用了"来处理,和实际情况矛盾。

"写回"就是为了让这个字典里记的是**最终决定**,而不是用户**最初的意愿**。任务运行期间不管谁来读,读到的都和组装时的决定一致。

小明这次的实际情况(默认 Agent、pro 模式):请求里是 `False`,算出来的最终值也是 `False`,写回前后没有变化。只有在请求的意愿被否决时这一行才起作用。

### 自定义 Agent 的配置:创建时写,使用时读

`AgentConfig` 是内存里的对象,它的内容来自磁盘上该 Agent 文件夹里的 `config.yaml`(文件的位置和内容见第一节)。流程是三步:读 yml、创建 `AgentConfig` 对象、读它的字段。前两步合在 `load_agent_config` 里。

yml 是用户**创建自定义 Agent** 时写出来的,之后每次使用只是读。写和读是两个时刻,走两条路:

| | 创建 Agent | 使用 Agent |
| --- | --- | --- |
| 什么时候 | 用户在网页上创建或修改它 | 用它发每一条消息 |
| 哪个接口 | `POST /api/agents`(修改是 `PUT /api/agents/{name}`) | `POST /api/threads/{id}/runs/stream` |
| 对 yml 做什么 | 写 | 读 |
| 发生几次 | 创建一次,之后偶尔修改 | 每条消息一次 |
| 会启动 Agent 吗 | 不会,只是存文件 | 会 |

创建接口(`app/gateway/routers/agents.py`)做的事:

```python
_validate_agent_name(body.name)
# 检查名字格式是否合法

_validate_model_exists(body.model)
# 检查填的模型在系统配置里是否存在

user_id = get_effective_user_id()
# 取当前登录的用户

return await _persist_new_agent(body, normalized_name, user_id)
# 保存:在这个用户的目录下建文件夹,写入 config.yaml 和 SOUL.md
```

| 时间 | 小明做了什么 | yml |
| --- | --- | --- |
| 周一 | 在网页上创建 `my-researcher` | 写出 `config.yaml` |
| 周二 | 用它发了 3 条消息 | 读 3 次 |
| 周三 | 把它的模型从 `claude-y` 改成 `gpt-x` | 改写 `config.yaml` |
| 周三 | 又发 1 条消息 | 读 1 次,读到的是新模型 |

每次任务都重新读一次,所以中途改了配置,下一条消息就生效。用默认 Agent 时不读,`load_agent_config` 直接返回 `None`。读的是这个用户自己目录下的 yml,所以小明读不到小红的 Agent。

"创建 agent"在这个项目里有两种意思:用户**创建自定义 Agent** 是调用 `POST /api/agents` 保存一份设置,结果是磁盘上多了两个文件;代码里的 **`create_agent(...)`** 是组装函数最后调用的那个函数,把模型、工具等拼成能运行的 Agent,结果是内存里多了一个 Agent 对象,任务结束就没了。前者是"存一份配方",后者是"照着配方做一道菜"。

还有一种写 yml 的途径是对话式创建:用户和引导 Agent 聊天描述需求,最后由 `setup_agent` 工具把两个文件写出来。`is_bootstrap`(引导模式)就是为这个流程准备的。

### `AgentConfig` 在组装函数里的完整轨迹

`AgentConfig` 只用在组装函数 `_assemble_lead_agent` 里,读完就不再用了。下面沿函数从上往下,只看和它有关的地方。例子:小明用 `my-researcher`,选了 ultra 模式,请求里没指定模型。

```python
agent_name = validate_agent_name(cfg.get("agent_name"))
# 从 cfg 里读出 Agent 的名字
# 结果:agent_name = "my-researcher"

agent_config = load_agent_config(agent_name, user_id=resolved_user_id) if not is_bootstrap else None
# 按名字和用户,从磁盘读出这个 Agent 的 config.yaml,变成 AgentConfig 对象
# 结果:agent_config = AgentConfig(name="my-researcher", model="claude-y", ...)

memory_enabled = getattr(agent_config, "memory_enabled", True) is not False
# 读 memory_enabled 字段:这个 Agent 是否启用记忆
# 结果:memory_enabled = True

allowed_subagents = getattr(agent_config, "allowed_subagents", None) if agent_config is not None else None
# 读 allowed_subagents 字段:允许调用哪些子 Agent
# 结果:allowed_subagents = []

subagent_enabled = bool(requested_subagent_enabled and allowed_subagents != [])
# 请求想启用(ultra 模式),但这个 Agent 的名单是空列表,所以不启用
# 结果:subagent_enabled = False

available_skills = _available_skill_names(agent_config, is_bootstrap)
# 读 skills 字段:这个 Agent 可以用哪些技能
# 结果:available_skills = {"deep-research"}

agent_model_name = agent_config.model if agent_config and agent_config.model else None
# 读 model 字段:这个 Agent 默认用哪个模型
# 结果:agent_model_name = "claude-y"

model_name = _resolve_model_name(requested_model_name or agent_model_name, app_config=resolved_app_config)
# 请求没指定模型,所以用 Agent 设定的
# 结果:model_name = "claude-y"

config["metadata"].update({"tool_groups": agent_config.tool_groups if agent_config else None, ...})
# 把从 AgentConfig 得到的几项抄进 config["metadata"]
# 结果:metadata 里有 "agent_name": "my-researcher", "tool_groups": ["web"],
#      "available_skills": ["deep-research"], "allowed_subagents": [], "memory_enabled": True

raw_tools = get_available_tools(groups=agent_config.tool_groups if agent_config else None, ...)
# 读 tool_groups 字段:只取这个 Agent 限定的工具组。这是最后一次直接读 agent_config
# 结果:只有 web 组的工具,没有文件读写和命令执行
```

再往下,代码不再出现 `agent_config`,用的是前面取出来的变量:`build_middlewares` 用 `memory_enabled`、`available_skills`;`apply_prompt_template` 用 `agent_name`(去读 `SOUL.md`)、`allowed_subagents`、`memory_enabled`。

放进 `metadata` 的是 `AgentConfig` 里几个字段的值,不是这个对象本身。存进去的东西最终要写进数据库,必须是字符串、数字、布尔、列表这类简单值;`description`、`SOUL.md` 的人设文字这些大段内容不放。

同一个函数,两种结果。差别全部来自 `load_agent_config` 读到的是 `None` 还是一个 `AgentConfig`:

| | 默认 Agent(小明实际的情况) | `my-researcher` |
| --- | --- | --- |
| `agent_config` | `None` | `AgentConfig(...)` |
| 模型 | 请求指定的 `gpt-x` | Agent 设定的 `claude-y` |
| 工具 | 系统配置的全部工具 | 只有 web 组 |
| 技能 | 全部已启用的 | 只有 `deep-research` |
| 子 Agent | 按请求(pro 模式没开) | 被禁止,ultra 模式也不行 |
| 人设 | 默认的 | 它自己的 `SOUL.md` |

### 组装之后不再用 `AgentConfig`

`create_agent` 的参数里没有它,任务运行中也没有代码回头去读它。它规定的东西已经体现在 Agent 的结构上:

| 想限制的事 | 靠什么生效 |
| --- | --- |
| 不让用文件工具 | 工具列表里根本没有文件工具,模型看不到也调不了 |
| 不让派子 Agent | 没有 `task` 工具 |
| 只让用一个技能 | 提示词里只列了这一个 |
| 用 `claude-y` | 模型对象就是 `claude-y` |

限制不是靠运行时每次检查,而是靠组装时就不给。模型手里没有那个工具,也就无从越权。

**运行时需要的限制,读的是 `metadata`。** 派子 Agent 是通过 `task` 工具完成的。模型调用它时,工具(`tools/builtins/task_tool.py`)这样取允许名单:

```python
metadata: dict = runtime.config.get("metadata", {}) if runtime is not None else {}
# 取出这次任务的 config["metadata"]

allowed_subagents = metadata.get("allowed_subagents")
# 从里面读出允许名单
# 结果(假设这个 Agent 允许 general-purpose):["general-purpose"]

available_subagent_names = get_available_subagent_names(app_config=runtime_app_config, allowed_subagents=allowed_subagents)
# 系统里启用的子 Agent 种类,再用允许名单过滤一遍
# 结果:["general-purpose"]

if config is None or subagent_type not in available_subagent_names:
    return result_command(tool_call_id=tool_call_id, status="failed", error=error)
# 模型要派的种类不在名单里:不执行,返回一条错误给模型
```

这里读的是 `runtime.config["metadata"]`,没有去加载 `AgentConfig`。`allowed_subagents` 这个值走过的路:

| 顺序 | 什么时候 | 在哪 |
| --- | --- | --- |
| 1 | 用户创建 Agent 时 | 磁盘上该 Agent 的 `config.yaml` |
| 2 | 组装时 | `load_agent_config` 读出的 `AgentConfig.allowed_subagents` |
| 3 | 组装时 | 取出来存成局部变量 `allowed_subagents` |
| 4 | 组装时 | 写进 `config["metadata"]` |
| 5 | 运行时,模型调用 `task` 工具 | 工具从 `runtime.config["metadata"]` 读,检查要派的种类是否允许 |

不在运行时重新读 `AgentConfig` 有两个原因:省掉每次读磁盘;更重要的是一致性,任务跑到一半用户改了 Agent 的配置,整个任务用的仍是开始时那一份。

所以子 Agent 的限制有两道:组装时,`subagent_enabled` 是 `False` 就根本不给 `task` 工具(管"能不能派");运行时,`task` 工具检查要派的种类在不在 `allowed_subagents` 里(管"能派哪几种")。

组装之外还有几处会加载 `AgentConfig`,都和运行 Agent 无关:Agent 管理接口(用户查看、修改自定义 Agent)、`update_agent` 工具(用户让 Agent 修改自己的配置)、IM 渠道(消息进来时确定用哪个 Agent)。

第八课会出现 `SubagentConfig`,它描述的是一种子 Agent(名字、提示词、可用工具、最大轮数、超时),和 `AgentConfig` 是两个东西。两者的联系只有一处:`AgentConfig.allowed_subagents` 里填的名字就是 `SubagentConfig.name`。

### 这一步的保护

- **用户身份不从普通选项里取。** `resolved_user_id` 取的是服务端根据登录身份写入的值,写入前还删掉了客户端自带的同名键。
- **数量上限有天花板。** 请求里要的子 Agent 数量会被压到系统允许的范围内。
- **带 `requested_` 前缀的值不直接用。** 它们只是请求的意愿,要经过后面的检查。
- **Agent 名字要校验。** `agent_name` 之后会用来找文件目录,所以先检查格式,防止像 `../` 这样的内容混进来。和第零课讲的 `thread_id` 格式校验是同一个道理。
- **配置按用户加载。** `load_agent_config` 带着 `user_id`,小明只能加载自己的自定义 Agent,加载不到小红的。
- **请求不能放宽 Agent 的限制。** 子 Agent 开关的规则。

### 需要记住的

1. 用户发的是请求体;`config` 是后端根据请求体造的字典,还加了很多请求体里没有的东西。两者名字有重叠,不是一回事。
2. `_assemble_lead_agent` 开头先把 `config` 合成 `cfg`,之后用 `cfg.get(...)` 读选项。这一步只是读和算,没有创建任何东西。
3. 读出的内容:用户 ID、模型名、Plan 模式、子 Agent 开关、两个子 Agent 上限、是否引导模式、Agent 名字。
4. `agent_name` 是 `None` 表示默认 Agent,没有专属配置,各项都不限制。自定义 Agent 有一份 `AgentConfig`,组装函数读到它之后,后面各环节按它来,这就是"变身"。
5. 列表字段的约定:`None` 是不限制,`[]` 是一个都不给,有内容是白名单。
6. 带 `requested_` 前缀的值是请求想要的。子 Agent 最终是否启用 = 请求想启用,并且这个 Agent 没有被禁止。请求只能收紧,不能放宽。
7. "写回 `config`"是后端把自己字典里"用户想要的"换成"最终决定的",保证后面读到的是同一个结论。

## 四、确定模型,创建模型对象

上一步停在:读完了运行选项和 `AgentConfig`。手里有 `requested_model_name = "gpt-x"`(请求想用的)。

这一步把"用哪个模型"最终定下来,然后创建模型对象。这部分是配置处理,用"代码、注释、结果"的块讲。小明这次:默认 Agent,pro 模式。

### 确定模型名和思考选项

```python
agent_model_name = agent_config.model if agent_config and agent_config.model else None
# 自定义 Agent 自己设定的默认模型;默认 Agent 或没设时是 None
# 结果:None

agent_thinking = getattr(agent_config, "thinking_enabled", None) if agent_config else None
agent_reasoning = getattr(agent_config, "reasoning_effort", None) if agent_config else None
# 自定义 Agent 设定的"默认是否思考"和"默认思考强度";没有就是 None
# 结果:都是 None

thinking_enabled = bool(_resolve_runtime_option(cfg, "thinking_enabled", agent_thinking, True))
# 最终是否开启思考:请求填了用请求的,否则用 Agent 设定的,都没有就默认开启
# 结果:True(请求里填了 true)

reasoning_effort = _resolve_runtime_option(cfg, "reasoning_effort", agent_reasoning, None)
# 最终的思考强度:同样的优先顺序,都没有就是 None
# 结果:"medium"

model_name = _resolve_model_name(requested_model_name or agent_model_name, app_config=resolved_app_config)
# 确定模型名:请求指定的优先,其次是 Agent 设定的;到系统配置里核对,不存在就退回系统默认模型
# 结果:"gpt-x"

model_name = _authorize_model_name(model_name, context=cfg, app_config=resolved_app_config)
# 检查这个用户有没有权限用这个模型;没有就换成他有权用的第一个模型
# 结果:"gpt-x"(有权限)

model_config = resolved_app_config.get_model_config(model_name)
# 按模型名从系统配置里取出这个模型的完整配置
# 结果:gpt-x 的 ModelConfig(内容见第一节)

if model_config is None:
    raise ValueError("No chat model could be resolved. ...")
# 一个可用的模型都找不到,报错
```

| 函数 | 作用 |
| --- | --- |
| `_resolve_runtime_option(cfg, 键, Agent 的值, 默认值)` | 按"请求 > 自定义 Agent > 默认值"的顺序取一个选项 |
| `_resolve_model_name(想用的名字, app_config)` | 到系统配置的模型列表里找这个名字。找到就返回它;找不到或没给名字,返回系统默认模型(列表第一个) |
| `_authorize_model_name(模型名, context, app_config)` | 权限检查。有权用就原样返回;没权限就返回这个用户能用的第一个模型 |
| `app_config.get_model_config(模型名)` | 按名字返回对应的 `ModelConfig`,找不到返回 `None` |

模型名、是否思考、思考强度都按同一个三级顺序取:请求、自定义 Agent 的配置、系统默认。以模型名为例,系统里配了 `gpt-x`(默认)和 `claude-y`:

| 请求里的 `model_name` | 自定义 Agent 的 `model` | 最终 `model_name` | 走的哪一级 |
| --- | --- | --- | --- |
| `gpt-x` | (默认 Agent,无) | `gpt-x` | 请求。这是小明的情况 |
| 没填 | `claude-y` | `claude-y` | 自定义 Agent |
| `gpt-x` | `claude-y` | `gpt-x` | 请求优先于 Agent |
| 没填 | (无) | `gpt-x` | 系统默认 |
| `不存在的模型` | (无) | `gpt-x` | 找不到,退回系统默认,并记一条警告 |

判断"请求有没有填"时,看的是 `cfg` 里有没有这个键,而不是值的真假。"请求明确填了 `thinking_enabled: false`"表示用户要关闭思考,应该照办;"请求根本没填"才去看 Agent 的默认值。

拿到 `model_config` 之后还要拿思考选项对照这个模型的实际能力,不符合就调整:请求要开思考但模型不支持,关掉并记警告;模型规定必须开思考但请求要关,重新打开;思考强度这个模型不认识,换成它支持的档位。小明这次 `gpt-x` 支持思考,不用调整。

### 记日志,写 `metadata`,创建模型对象

```python
logger.info("Create Agent(%s) -> thinking_enabled: %s, reasoning_effort: %s, model_name: %s, ...", ...)
# 把前面定下来的选项打印成一行日志
# 结果:日志里出现 "Create Agent(default) -> thinking_enabled: True, reasoning_effort: medium,
#      model_name: gpt-x, is_plan_mode: True, subagent_enabled: False, ..."

config["metadata"].update({
    "agent_name": agent_name or "default",
    "model_name": model_name or "default",
    "thinking_enabled": thinking_enabled,
    "reasoning_effort": reasoning_effort,
    "is_plan_mode": is_plan_mode,
    "subagent_enabled": subagent_enabled,
    "tool_groups": agent_config.tool_groups if agent_config else None,
    "mcp_plugins": getattr(agent_config, "mcp_plugins", None),
    "available_skills": sorted(available_skills) if available_skills is not None else None,
    "allowed_subagents": list(allowed_subagents) if allowed_subagents is not None else None,
    "memory_enabled": memory_enabled,
})
# 把最终选项写进 config 的 metadata 分区;这个分区的内容会落进每个检查点,运行时工具也会读
# 结果:见第一节"组装完成后 metadata 分区"

chat_model = create_chat_model(
    name=model_name,
    thinking_enabled=thinking_enabled,
    reasoning_effort=reasoning_effort,
    app_config=resolved_app_config,
    attach_tracing=False,
    model_overrides=agent_model_overrides,
)
# 创建模型对象:按模型名从系统配置找到 ModelConfig,根据其中的 use 字段找到对应的类,配好参数后创建
# 结果:chat_model = ChatOpenAI(model="gpt-x-2026", api_key="sk-...", 开启思考, 强度 medium)
#      这是 create_agent 五个参数里的第一个
```

以后想知道某次任务实际用了什么配置,在日志里搜 `Create Agent` 就能看到。

`create_chat_model` 是组装过程中第一次真正创建东西,前面都在读选项。`use` 字段的格式是"模块路径:类名",程序在运行时按这个字符串找到类。要接入一个新的模型厂商,只需要在 `config.yaml` 里写上它的类路径,不用改代码。

每次任务都新建模型对象,因为是否思考、思考强度这些参数是创建时定进去的,不同的选项需要不同的对象。

### 这一步的保护

- **不存在的模型不会让任务失败。** 退回系统默认模型并记警告。
- **模型有权限控制。** 没权限时换成用户有权用的模型,而不是直接报错。
- **思考选项按模型能力校正。** 避免把模型不支持的参数发给厂商导致调用失败。
- **一个模型都没有才报错。**

### 需要记住的

1. 模型名、是否思考、思考强度都按"请求 > 自定义 Agent > 系统默认"的顺序取。
2. 确定模型名分三关:找名字(不存在就退回默认)、查权限(没权限就换一个)、取出完整配置。
3. `ModelConfig` 是配置数据,`chat_model` 是照着它造出来、能调用的对象。
4. 最终选项写进 `config["metadata"]`,并打印一行 `Create Agent(...)` 日志。

## 五、准备工具、中间件、提示词,合成 Agent

上一步停在:创建了模型对象 `chat_model`。`create_agent` 的五样东西有了第一样。

这一步把另外四样准备好,然后调用 `create_agent`。这部分是拼装,用"代码、注释、结果"的块讲。工具、中间件、提示词各自内部怎么工作,分别是第七课、第二课、第三课的内容,这里只看它们在组装时怎么被拿到。

### 工具

```python
raw_tools = get_available_tools(
    model_name=model_name,
    groups=agent_config.tool_groups if agent_config else None,
    mcp_plugins=getattr(agent_config, "mcp_plugins", None),
    subagent_enabled=subagent_enabled,
    app_config=resolved_app_config,
    chat_model=chat_model,
    ...
)
# 汇总这次任务可用的工具。groups 是自定义 Agent 限定的工具组,None 表示不限制
# 结果:raw_tools = [web_search, web_fetch, image_search, ls, read_file, glob, grep,
#     write_file, str_replace, bash, present_files, ask_clarification, ...]
#      没有 task(派子 Agent 的工具),因为 subagent_enabled 是 False

configured_tools = raw_tools + extra_tools
# 加上额外的工具。extra_tools 只有自定义 Agent 才有(让 Agent 修改自己配置的 update_agent)
# 结果:小明用默认 Agent,extra_tools 是空的

authorized_tools, _authz_provider = apply_tool_authorization(authorization_candidates, context=cfg, app_config=resolved_app_config)
# 按这个用户的权限过滤工具,没权限用的工具直接从列表里去掉
# 结果:默认没开启细粒度权限,一个都不去掉

final_tools, setup = assemble_deferred_tools(configured_tools, enabled=resolved_app_config.tool_search.enabled)
# 工具很多时,把一部分设为"延迟加载":先不给模型看,需要时再搜出来。setup 记录了哪些被延迟
# 结果:final_tools 是最终交给 Agent 的工具列表。这是 create_agent 的第二个参数
```

`get_available_tools` 把四个来源的工具合成一个列表并去重:

| 来源 | 例子 | 由什么决定 |
| --- | --- | --- |
| 系统配置里的工具 | `web_search`、`read_file`、`write_file`、`bash` | `config.yaml` 的 `tools`,每个工具属于一个工具组 |
| 内置工具 | `present_files`(展示文件)、`ask_clarification`(向用户提问) | 总是有 |
| 子 Agent 工具 | `task` | `subagent_enabled` 为真才有 |
| MCP 工具 | 用户接入的外部工具 | `extensions_config.json` |

| 工具组 | 里面的工具 |
| --- | --- |
| `web` | `web_search`、`web_fetch`、`image_search` |
| `file:read` | `ls`、`read_file`、`glob`、`grep` |
| `file:write` | `write_file`、`str_replace` |
| `bash` | `bash` |

`my-researcher` 的 `tool_groups = ["web"]` 意味着它只拿到第一行的三个工具(加上内置工具)。

### 中间件

```python
middlewares = build_middlewares(
    config,
    model_name=model_name,
    agent_name=agent_name,
    available_skills=available_skills,
    memory_enabled=memory_enabled,
    app_config=resolved_app_config,
    deferred_setup=setup,
    user_id=resolved_user_id,
    ...
)
# 按配置和这次的选项,构建中间件列表
# 结果:middlewares = [输入净化, 工具输出预算, 线程目录, 上传, 沙箱, ..., 动态上下文, 技能激活,
#摘要, 待办列表, 标题, 记忆, 看图, 循环检测, ..., 澄清提问]
#      有"待办列表"是因为 is_plan_mode 为 True;没有"子 Agent 并发限制"是因为 subagent_enabled 为 False

bound_middlewares = normalize_middleware_state_schemas(middlewares, mode)
# 让中间件的状态结构和检查点模式(全量或增量)保持一致
# 结果:mode 是 "full",原样返回。这是 create_agent 的第三个参数
```

| 选项 | 影响 |
| --- | --- |
| `is_plan_mode` | 为真才装待办列表中间件 |
| `subagent_enabled` | 为真才装子 Agent 并发限制中间件 |
| `memory_enabled` | 为真才装记忆中间件 |
| 模型的 `supports_vision` | 为真才装看图中间件 |
| 系统配置的各项开关 | 摘要、循环检测、token 统计等装不装 |

### 系统提示词

```python
system_prompt = apply_prompt_template(
    subagent_enabled=subagent_enabled,
    max_concurrent_subagents=max_concurrent_subagents,
    max_total_subagents=max_total_subagents,
    agent_name=agent_name,
    available_skills=available_skills,
    app_config=resolved_app_config,
    user_id=resolved_user_id,
    allowed_subagents=allowed_subagents,
    memory_enabled=memory_enabled,
    ...
)
# 用模板拼出系统提示词:角色说明、行为规则、可用的技能目录;启用了子 Agent 才加入怎样派子任务的说明
# 结果:system_prompt = 一大段文字。这是 create_agent 的第四个参数
```

第 2 步算出来的两个子 Agent 上限在这里用上:启用子 Agent 时,提示词里会告诉模型"同时最多派几个、总共最多派几个"。自定义 Agent 的 `SOUL.md` 也是在这个函数里按 `agent_name` 读出来、拼进去的。

### 合成与返回

```python
graph = create_agent(
    model=chat_model,     # 第 3 步创建的模型对象
    tools=final_tools,    # 上面汇总、过滤后的工具列表
    middleware=bound_middlewares,  # 上面构建的中间件列表
    system_prompt=system_prompt,   # 上面拼出的系统提示词
    state_schema=get_thread_state_schema(mode),  # 状态结构:ThreadState
    context_schema=dict,   # 运行上下文的类型:普通字典(就是 runtime_ctx)
)
# 把五样东西拼成一个能执行的 Agent
# 结果:graph = 组装好的 Agent

return _complete_assembly(
    config=config,
    graph=graph,
    agent_name=agent_name or "lead-agent",
    effective_model=model_name,
    tools=final_tools,
    middlewares=middlewares,
    enabled_skills=enabled_skills,
    effective_policies={"plan_mode": is_plan_mode, "subagents": {...}, ...},
    ...
)
# 把组装好的 Agent 和"它是用什么拼出来的"打包返回
# 结果:LeadAgentAssembly(graph=<Agent>, descriptor=<组装记录>, effective_model="gpt-x")
```

`create_agent` 是 LangChain 提供的函数,不是 DeerFlow 写的。DeerFlow 负责准备好五样东西交给它,它负责把它们连成"调模型、执行工具、再调模型"的循环。

| 参数 | 是什么 | 在哪一步准备的 | 由什么决定 |
| --- | --- | --- | --- |
| `model` | 模型对象 | 第 3 步 | 请求的模型名、自定义 Agent、系统配置 |
| `tools` | 工具列表 | 这一步 | 系统配置的工具、工具组限定、子 Agent 开关、权限 |
| `middleware` | 中间件列表 | 这一步 | Plan 模式、子 Agent、记忆等开关,系统配置 |
| `system_prompt` | 系统提示词 | 这一步 | 技能、子 Agent、自定义 Agent 的人设 |
| `state_schema` | 状态结构 | 这一步 | 固定是 `ThreadState` |

返回之后就回到了第 1 步:后台任务拿到 `LeadAgentAssembly`,取出 `graph`,用 `agent.astream(...)` 运行它。

### 整个组装函数的全貌

```text
_assemble_lead_agent(config, app_config)
    读选项  用户、模式、子 Agent 开关和上限、是否引导模式 (第 2 步)
    读 AgentConfig  自定义 Agent 的专属配置,默认 Agent 是 None  (第 2 步)
    定模型  模型名 → 权限 → ModelConfig → 核对思考选项    (第 3 步)
    写 metadata    把最终选项记进 config["metadata"]     (第 3 步)
    创建模型对象    chat_model      (第 3 步)
    汇总工具       final_tools     (第 4 步)
    构建中间件      bound_middlewares      (第 4 步)
    拼提示词       system_prompt   (第 4 步)
    create_agent   五样东西合成 graph     (第 4 步)
    返回    LeadAgentAssembly(graph, descriptor, effective_model)
```

### 这一步的保护

- **没权限的工具不出现。** 工具按用户权限过滤后才交给 Agent。
- **没启用的能力不给工具。** 子 Agent 没启用就没有 `task` 工具。
- **自定义 Agent 不能在不可信的入口修改自己。** `update_agent` 工具只在可信的入口(网页、接口)提供;通过 GitHub 评论触发的任务不给,因为任何能在仓库里评论的人都能触发它。

### 需要记住的

1. `create_agent` 收五样东西:模型、工具、中间件、系统提示词、状态结构。组装函数的全部工作就是把这五样准备好。
2. 工具来自四个来源(系统配置、内置、子 Agent、MCP),经过工具组限定和权限过滤。
3. 中间件和提示词都由前面读出的选项决定:Plan 模式、子 Agent、记忆、技能。
4. 组装结果打包成 `LeadAgentAssembly` 返回。
5. 工具、中间件、提示词内部怎么工作,分别在第七课、第二课、第三课讲。

## 六、`ThreadState` 和 reducer

上一步停在:`create_agent(...)` 的第五个参数 `state_schema=get_thread_state_schema(mode)`。

这一步讲这个状态是什么、里面有什么、谁在读写它、同时写同一个字段时怎么办。中间件和工具做的事,归根到底都是在读写这个状态。

### 状态是什么

Agent 执行时,框架手里一直拿着一个字典,记着"这段对话到目前为止的全部情况"。这个字典就是状态(state)。

| 谁 | 和状态的关系 |
| --- | --- |
| 模型 | 每次被调用时,看到的是状态里的 `messages` |
| 工具、中间件 | 执行时读状态,执行完交回一份"更新" |
| 检查点 | 每执行完一步,把整个状态存一份 |

`ThreadState` 是这个字典的结构定义:规定有哪些字段、每个字段是什么类型。`get_thread_state_schema(mode)` 在默认的全量模式下返回的就是 `ThreadState` 这个类。

### 有哪些字段

定义在 `agents/thread_state.py`(节选):

```python
class ThreadState(AgentState):
    sandbox: SandboxStateField
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: Annotated[list | None, merge_todos]
    goal: Annotated[GoalState | None, merge_goal]
    uploaded_files: NotRequired[list[dict] | None]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    delegations: Annotated[list[DelegationEntry], merge_delegations]
    skill_context: Annotated[list[SkillEntry], merge_skill_context]
    summary_text: NotRequired[str | None]
    ...
```

它继承自 LangChain 的 `AgentState`。`AgentState` 里只有一个字段 `messages`(消息列表),DeerFlow 在它的基础上加了自己的字段。

| 字段 | 存什么 | 谁写 | 细讲 |
| --- | --- | --- | --- |
| `messages` | 消息列表:用户、AI、工具结果 | 框架、工具、中间件 | 贯穿全部 |
| `title` | 对话标题 | 标题中间件 | 第二课 |
| `thread_data` | 这段对话的三个目录路径 | 线程目录中间件 | 第四课 |
| `sandbox` | 用的是哪个沙箱 | 沙箱中间件、沙箱工具 | 第五课 |
| `artifacts` | 展示给用户的文件路径 | `present_files` 工具 | 本节 |
| `todos` | 待办列表 | 待办工具 | 第十一课 |
| `uploaded_files` | 用户上传的文件信息 | 上传中间件 | 按需 |
| `viewed_images` | 看过的图片的信息(不含图片本身) | 看图工具 | 按需 |
| `delegations` | 派出去的子任务的账本 | 子 Agent 相关 | 第八课 |
| `skill_context` | 已经加载的技能 | 技能相关 | 第九课 |
| `summary_text` | 历史消息的摘要 | 摘要中间件 | 第十一课 |

### 状态在一次任务里怎么变

小明让 Agent 写一份天气报告,Agent 把报告存成文件并展示给他。每一行是一步执行完之后的状态(只列出有内容的字段):

| 步 | 发生了什么 | `messages` | 其他字段 |
| --- | --- | --- | --- |
| 开始 | 收到用户消息 | [用户:写一份天气报告] | |
| 1 | 线程目录中间件算出目录 | 同上 | `thread_data = {workspace_path, uploads_path, outputs_path}` |
| 2 | 模型决定搜索 | 加上 [AI:调用 web_search] | |
| 3 | 搜索工具执行完 | 加上 [工具结果:明天晴] | |
| 4 | 模型决定写文件 | 加上 [AI:调用 write_file] | |
| 5 | 写文件工具执行完 | 加上 [工具结果:已写入] | |
| 6 | 模型决定展示文件 | 加上 [AI:调用 present_files] | |
| 7 | 展示文件工具执行完 | 加上 [工具结果:已展示] | `artifacts = ["/mnt/user-data/outputs/report.md"]` |
| 8 | 模型给出最终回答 | 加上 [AI:报告已生成] | |
| 9 | 标题中间件生成标题 | 同上 | `title = "天气报告"` |

`messages` 一直在变长,其他字段是各个中间件和工具在自己负责的时刻写进去的。

### 状态是怎么被修改的:交回一份"更新"

工具和中间件不直接改状态,而是返回一个字典,说"我想把哪些字段改成什么"。框架负责把这份更新并进状态。`present_files` 工具(`tools/builtins/present_file_tool.py`)的返回:

```python
return Command(
    update={
        "artifacts": normalized_paths,
        "messages": [ToolMessage("Successfully presented files", tool_call_id=tool_call_id)],
    },
)
```

`Command(update={...})` 就是一份更新:`artifacts` 字段加上这些文件路径,`messages` 字段加上一条工具结果消息。中间件也是同样的方式,标题中间件(`title_middleware.py`)返回一个只含 `title` 的字典:

```python
return {"title": attachment_title}
```

没提到的字段表示"我不动它"。

### 更新怎么并进状态:覆盖还是合并

框架把更新并进现有状态时,对每个字段有两种做法:覆盖(新值直接替换旧值),或合并(按某种规则把新值和旧值结合起来)。哪个字段用哪种,写在 `ThreadState` 的定义里:

```python
title: NotRequired[str | None]      # 没有合并函数
artifacts: Annotated[list[str], merge_artifacts]   # 有合并函数 merge_artifacts
```

`title` 只写了类型,更新时直接覆盖。`artifacts` 用 `Annotated[类型, 函数]` 的写法多带了一个函数,更新时框架调用 `merge_artifacts(旧值, 新值)`,用它的返回值作为新状态。这个附带的合并函数就叫 **reducer**。

### 为什么需要 reducer

假设 `artifacts` 没有 reducer,是直接覆盖的。小明让 Agent 生成两份文件:

| 步 | 工具交回的更新 | 覆盖方式下的 `artifacts` |
| --- | --- | --- |
| 展示第一份 | `{"artifacts": ["report.md"]}` | `["report.md"]` |
| 展示第二份 | `{"artifacts": ["chart.png"]}` | `["chart.png"]` |

第一份文件被冲掉了。

**并行的情况。** 模型可以在一次回复里同时调用多个工具,框架会并行执行它们。假设模型一次调用了两个 `present_files`:

| | 工具甲 | 工具乙 |
| --- | --- | --- |
| 开始时读到的 `artifacts` | `[]` | `[]` |
| 各自交回的更新 | `["report.md"]` | `["chart.png"]` |

两个工具同时开始,读到的旧值都是空的,谁也不知道对方的存在。两份更新同时交回给框架:没有 reducer,框架不知道该听谁的,直接报错(同一步里一个字段收到了多个值);有 reducer,框架依次调用它,把两份都并进去。`merge_artifacts` 的作用是把新旧两个列表接起来并去重,保持先后顺序:

| 调用 | 旧值 | 新值 | 返回 |
| --- | --- | --- | --- |
| 第一次 | `[]` | `["report.md"]` | `["report.md"]` |
| 第二次 | `["report.md"]` | `["chart.png"]` | `["report.md", "chart.png"]` |

有了 reducer,工具只需要交回"我这次新增的",不用关心现有状态是什么,也不用关心有没有别的工具同时在改。`present_files` 的说明文字里就写着:This tool can be safely called in parallel with other tools. State updates are handled by a reducer to prevent conflicts.

### 不同字段的合并规则不同

| 字段 | reducer | 规则 | 为什么这样定 |
| --- | --- | --- | --- |
| `messages` | 框架自带的 | 追加;消息 ID 相同的替换旧的 | 对话是不断增长的 |
| `artifacts` | `merge_artifacts` | 接起来并去重 | 展示过的文件要累积 |
| `todos` | `merge_todos` | 新值不是 `None` 就整个替换;是 `None` 就保留旧的 | 待办列表每次是整份重写的 |
| `viewed_images` | `merge_viewed_images` | 合并两个字典;新值是空字典表示清空 | 需要一种"清空"的表达方式 |
| `sandbox` | `merge_sandbox` | 两个值相同就保留;不同就报错 | 见下 |

`sandbox` 的规则是"只接受相同的值":

| 旧值 | 新值 | 结果 |
| --- | --- | --- |
| 无 | `{sandbox_id: "sb-1"}` | 采用新值 |
| `{sandbox_id: "sb-1"}` | `{sandbox_id: "sb-1"}` | 保留,不变 |
| `{sandbox_id: "sb-1"}` | `{sandbox_id: "sb-2"}` | 报错 |

多个沙箱工具并行执行时,每个都可能报告自己用的沙箱 ID。正常情况下它们用的是同一个沙箱,合并后不变。如果出现了两个不同的 ID,说明一段对话用上了两个沙箱,这是隔离出了问题,所以直接报错而不是悄悄选一个。reducer 不只是合并数据,也可以用来发现不该出现的情况。

### 哪些字段没有 reducer

`title`、`thread_data`、`summary_text`、`uploaded_files` 直接覆盖。它们各自只有一个写入者,不会在同一步里被并行写:

| 字段 | 唯一的写入者 |
| --- | --- |
| `title` | 标题中间件 |
| `thread_data` | 线程目录中间件 |
| `summary_text` | 摘要中间件 |
| `uploaded_files` | 上传中间件 |

判断一个字段要不要 reducer,就看它会不会在同一步里被多方同时写,以及新旧值之间是不是累积关系。

### 状态和几样东西的关系

| | 和状态的关系 |
| --- | --- |
| `config`、`runtime_ctx` | 不是状态。它们是这次任务的配置和运行信息,不存进检查点的 `checkpoint` 列 |
| 检查点 | 每一步之后把整个状态序列化存一份 |
| 请求体的 `input` | 是对状态的第一份更新:`{"messages": [用户消息]}`,并进上一次任务留下的状态里 |

第三行解释了为什么前端每次只发新消息、不发历史:新消息作为一份更新交给框架,框架从检查点取出这段对话上次留下的状态,用 `messages` 的 reducer 把新消息追加进去。

| | `runtime_ctx`(运行上下文) | 状态 |
| --- | --- | --- |
| 内容 | 这次任务的配置:对话 ID、模型名、开关 | 对话的内容:消息、标题、文件、待办 |
| 任务期间会变吗 | 基本不变 | 每一步都在变 |
| 跨任务保留吗 | 不保留,每次任务新建 | 保留,存在检查点里,下次任务接着用 |
| 工具怎么读 | `runtime.context` | `runtime.state` |

### 这一步的保护

- **并行写不丢数据。** 有 reducer 的字段,多个工具同时交回更新时全部并入。
- **异常状态被拦下。** `sandbox` 的 reducer 发现两个不同的沙箱 ID 就报错。
- **工具不能随意改写历史。** 工具只能交回更新,由框架按规则并入。

### 需要记住的

1. 状态是 Agent 执行时框架一直拿着的一个字典,记着对话的全部情况。`ThreadState` 是它的结构定义,在 `messages` 之外加了标题、文件、待办等字段。
2. 工具和中间件不直接改状态,而是返回一份更新(`Command(update={...})` 或一个字典),框架负责并进状态。
3. 并入时,没有 reducer 的字段直接覆盖;有 reducer 的字段调用它来合并。reducer 是写在字段定义里的合并函数。
4. reducer 解决两个问题:累积型的数据不被覆盖;多个工具并行写同一个字段时不冲突。
5. 状态存在检查点里,跨任务保留;`runtime_ctx` 是每次任务的配置,不保留。
