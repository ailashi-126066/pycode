# miniclaudecode（pycode）项目分析报告

日期：2026-11-01　范围：只读调查。没有修改源码，没有提交；只新建了本报告文件。没有读取 `.env` 的内容，也没有运行需要真实 API Key 的脚本。

---

## 0. 结论摘要

1. **项目是什么**：包名 `pycode`，一个本地终端编程 Agent，相当于 Python 版的迷你 Claude Code。它提供非交互 CLI（`pycode run`）和 Textual TUI，能力包括：对话、读写文件、受审批的 Bash 执行（macOS 可用 Seatbelt 沙箱）、带分支的会话持久化与回退、上下文压缩、LangMem 长期记忆、Hooks、MCP、Skills、Plan 模式，以及进程内多 Agent Team（每个 worker 用独立 git worktree）。模型/工具循环直接用 Deep Agents 0.7.32（LangChain 1.4.7 / LangGraph 1.2.18）实现。
2. **当前状态健康**：在当前工作区（含未提交改动）上，`pytest` 结果为 **1254 passed、1 skipped**（257 秒）。`ruff check`、`ruff format --check`、`scripts/check_architecture.py`、`pyright`（basic）全部通过，pyright 为 0 errors。
3. **架构**：共 16 个功能包，约 2.98 万行。每个功能包分 `domain/ports/services/adapters` 四层，各有一个 `module.py` 作装配入口。允许的功能间依赖关系是一个 DAG，由 AST 静态检查强制执行（`scripts/check_architecture.py`，`tests/architecture/`），工程纪律很强。
4. **开发状态**：`main` 与 `origin/main` 同步（`f3e60cf`，2026-10-28）。工作区还有一批**未提交的"单 Agent 任务状态 JSON 契约"功能**（62 个文件 +732/−261，另有 8 个未跟踪的新文件）。5 个 `.worktrees/` 分支的内容都已在 main 中有等价提交（`git cherry` 只剩共同的 baseline 快照一条），可以视为已完成、可清理的历史分支。
5. **主要风险**（细节见第 7 节）：
   - (a) 未提交的新功能要求主模型的每条非空文本都是严格 JSON。解析失败直接抛 `ModelError`，整次运行以 error 结束，这条模型响应也不保存。对不同 provider 和模型的稳定性要求很高。
   - (b) `docs/architecture.md` 的端口表还写着已不存在的 `JsonlSessionStore` 等类名，和代码不一致。
   - (c) 会话存储每次追加都遍历一遍活动路径，复杂度随会话长度线性增长。
   - (d) 多处用 `assert` 做运行时不变量检查。
   - (e) 75 个函数的圈复杂度超过 10，但 lint 没有启用 C901。

---

## 1. 项目定位与功能

依据：`README.md`、`docs/architecture.md`、`docs/source-map.md`、`pyproject.toml`

- `pyproject.toml` 的描述是 "a Python coding agent with persistent conversation branches"。唯一的 console 入口是 `pycode = "pycode.app.cli.main:main"`，`check_architecture.py:check_harness` 强制只能有这一个入口。
- README 列出的主要能力：
  - 非交互 CLI：支持 `--output-format stream-json`。遇到需要审批时输出 `needs_input` 并以退出码 2 结束，之后可 resume。
  - TUI：`/plan`、`/memory`、`/memory undo`、`/agents`、`/compact`、`/revert ENTRY_ID both`、`/skill ...`、`/model`。
  - 会话管理：`session list/open`，存储 GC（`storage gc --dry-run`）。
  - 团队：`--max-agents 1..4` 控制并发数。worker 有两种：readonly，或使用独立 Git worktree；merge 需要逐次审批。
  - 沙箱：`--sandbox on|on-auto`，网络需要 `--sandbox-network` 显式开启。环境变量默认按白名单继承，`--pass-env` 拒绝透传凭据形态的变量。
- 代码来源（`docs/source-map.md`）：
  - 借鉴 Pi（`earendil-works/pi@42a3497`，MIT）和 OpenCode（`anomalyco/opencode@055d95b`，MIT）。
  - 改造了作者本人的 MewCode 代码，授权记录在 `docs/licenses/local-author-permission.txt`。
  - 源文件头部的 `# Source:` 注释保留了出处，例如 `agent/adapters/agent.py:1`、`sandbox/domain/command_policy.py:1-7`。

## 2. 技术栈与依赖

- **Python**：`requires-python >=3.11`。开发环境 `.venv` 实测为 Python 3.12.14。
- **核心依赖**（`pyproject.toml`，大多精确锁定版本）：
  - Agent 框架：`deepagents==0.7.32`、`langchain[mcp]==1.4.7`、`langchain-core==1.7.0`、`langgraph==1.2.18`
  - 持久化：`langgraph-checkpoint==4.3.1`、`langgraph-checkpoint-sqlite==3.1.2`
  - MCP 与记忆：`fastmcp==4.1.4`、`langmem==0.0.30`
  - UI 与工具库：`textual>=8.2.5,<9`、`httpx`、`pyyaml`、`jsonschema`、`pydantic>=2.7`
  - 模型 SDK：`langchain-deepseek`、`langchain-openai`、`langchain-anthropic`
- **可选依赖**：
  - eval：`langfuse==4.17.0`、`openevals`、`agentevals`
  - dev：`pytest 9`、`pytest-asyncio`、`ruff`、`pyright==1.1.414`、`build`、`hatchling`
  - 完整锁定版本见 `requirements-dev.lock`，由 `scripts/lock_dependencies.py` 生成。
- **构建**：hatchling。wheel 会把 `docs/licenses` 打包进 `pycode/licenses`。
- **外部命令**：`rg`、`git`、`bash`；macOS 还需要 `/usr/bin/sandbox-exec`。`pycode doctor` 会检测这些命令（`app/cli/main.py:external_tools`）。
- **模型 Provider**：
  - 支持 `implementation ∈ {deepseek, openai, anthropic}`（`models/adapters/factory.py:80,83,95`）。
  - OpenAI 还有 `openai_api` 子选项，例如 `chat_completions`（`app/configuration/providers.py:102-104,175`）。
  - 非本机 URL 必须用 HTTPS（`providers.py:75`）。
  - 示例配置 `docs/provider-config.example.json`：DeepSeek `deepseek-flash`，`api_key_env: DEEPSEEK_API_KEY`，`context_window: 1000000`，`max_output_tokens: 8192`，开启 thinking。
  - `.env.example` 只有一个变量名 `DEEPSEEK_API_KEY=`。`.env` 已被 `.gitignore` 排除，本次没有读取。
- **CI**：README 明确写了"本项目不配置 GitHub Actions/CI"。

## 3. 架构

### 3.1 模块划分（`src/pycode`，行数为实测）

| 包 | 文件/行数 | 职责 |
|---|---|---|
| kernel | 28 / 1072 | 共享的原生事实：Message、ToolSpec、SessionEntry、错误、哈希、脱敏；事件流 `EventStream`；文件锁和时钟适配器 |
| agent | 15 / 1259 | 唯一的 `create_deep_agent` 图（`agent/adapters/agent.py`）、HistoryMiddleware、RunContext、任务输出解码 |
| session | 38 / 3628 | SqliteStore 会话树、AsyncSqliteSaver checkpoint、AgentSession、分支/fork/回退、存储 GC |
| context | 31 / 2246 | 上下文投影（ContextProjector）、预算、80% 后台摘要、大输出驱逐、系统提示、TaskProjection |
| models | 28 / 2627 | Provider 配置、SDK 工厂、请求计量与记录（runtime.sqlite）、失败分类、同模型有界重试、消息转换 |
| tools | 39 / 3712 | 工具注册表、执行器、权限门链、settlement 中间件、PycodeBackend（虚拟路由 + rg）、输出存储、ToolSearch 发现 |
| sandbox | 16 / 1135 | Bash 工具、进程执行、Seatbelt profile、命令 deny/allow 策略 |
| snapshot | 8 / 729 | 基于 Git tree 的文件快照与恢复 |
| memory | 17 / 1437 | LangMem 主动工具、每个作用域一个 JSON 文件、后台整理、变更日志、undo |
| skills | 10 / 1627 | Skill 发现/读取/创建/更新/从 GitHub 安装（平铺结构） |
| mcp | 10 / 851 | LangChain MCPAdapter 连接、工具目录、schema 复核（平铺结构） |
| hooks | 8 / 848 | 同步 command/prompt Hook（平铺结构） |
| plans | 15 / 588 | Plan 文档、审批 hash、Plan 门 |
| team | 13 / 3170 | 进程内 AgentTeam：任务板、邮箱、worktree、merge（平铺结构） |
| ui | 20 / 2087 | ConsoleOutput 与 Textual TUI（`ui/adapters/tui.py` 947 行） |
| app | 25 / 2740 | `bootstrap.py` 装配入口，`cli/`、`configuration/`、`composition/`、`lifecycle/` 四个区 |

skills、mcp、hooks、team 是"平铺"结构，checker 用 `FLAT_FEATURES` 单独处理（`scripts/check_architecture.py:64`）。

### 3.2 依赖方向

允许的功能间依赖（`scripts/check_architecture.py:10-27`，与 `docs/design/feature-dependencies.json` 必须一致，见 checker 第 598-602 行）：

```
kernel ← sandbox, snapshot, models
context → kernel, models          mcp/skills/plans → context, kernel(, sandbox)
hooks → kernel, sandbox           memory → context, kernel, models
tools → kernel, sandbox, snapshot, plans
agent → kernel, models, context, tools, plans, hooks
team → kernel, snapshot, tools, plans
session → kernel, context, models, skills, snapshot, tools, memory, team
ui → kernel, tools, plans, team, mcp
app → 全部
```

层内顺序为 domain(0) < ports(1) < services(2) < adapters(3) < module(4)（`RANK`，第 63 行）。纯层禁止 I/O 模块（`CORE_IO`）和框架（`FRAMEWORKS`）。`pycode` 禁止 import `evals`、`scripts`、`archive`。

### 3.3 架构约束怎么执行

`scripts/check_architecture.py`（810 行）基于 AST 做以下检查：

- **`check_dependencies`**：
  - 跨功能 import 必须在 DAG 内。
  - 能追踪别名、`TYPE_CHECKING` 和延迟 import（`delayed_imports`）。
  - 用 `symbol_origin` 解析再导出的真实来源。
  - 包根必须有字面量 `__all__`，并且不能再导出 adapter、module 或框架符号。
  - 检测模块级和功能级环（`dependency_cycles`）。
  - kernel 必须是四层结构。
  - app 四个区之间有独立的依赖规则（`APP_DEPENDENCIES`）。
- **`check_port_implementations`**：每个 `ports`/`contracts` 里的 Protocol，都必须在 `docs/design/port-implementations.json` 中登记一个存在的、非 Protocol 的实现。
- **`check_harness`**：
  - 必须锁定三个框架版本。
  - 禁止直接依赖 `mcp` 和 `langchain-mcp-adapters`。
  - 禁止 `langchain.agents.create_agent`。
  - 禁止旧的 `execute_batch` 和旧表。
- **`check_branding`**：禁止旧品牌名。
- **`tests/architecture/test_dependencies.py`**：把 `check_dependencies() == []` 作为 pytest 用例，并用 27 个参数化反例确认违规能被检出，例如 `"pycode.memory.domain.policy", "reader = open\nreader('x')"`。
- 本次实测：`python scripts/check_architecture.py` 输出 "Architecture and branding checks passed"。

### 3.4 一次请求的完整链路

代码主干根据实际读过的文件整理，装配细节参考 `docs/architecture.md`。

1. **入口**：`app/cli/main.py:main` 解析参数，`load_config` 加上 CLI 覆盖项，`load_providers`，再调用 `compose_application`（`app/bootstrap.py`）。`compose_application` 返回 `Application`，它持有 `ApplicationModules`（`app/composition/modules.py`，显式列出 14 个功能模块；Skills 和 MCP 可以设为 `None`）。
2. **会话**：`app/cli/commands.py:command` 通过 composition 打开 `AgentSession`（`session/services/agent_session.py`）。`ConfiguredRuntimeLauncher`（`app/lifecycle/session.py`）为每次运行调用 runtime factory（`app/composition/runtime.py`）。factory 负责组装：
   - 工具门链（`tools/module.py:gate`：InterruptToolGate → MemoryToolGate → PlanToolGate → 可选 custom/scope → Worker/Team 门）
   - ToolExecutor、模型 owner、`AgentRuntime`
3. **建图**：`AgentRuntime.run`（`agent/adapters/agent.py:139`）先把用户输入写入 journal（SqliteStore），再 `build()`。`build()` 注册 harness profile，关闭 Deep Agents 自带的 SummarizationMiddleware 和 `write_todos`，然后组装中间件：
   - `FilesystemMiddleware`：ls/read_file/glob/grep，非只读时加 write_file/edit_file，后端是 `PycodeBackend`
   - `MemoryMiddleware`：每次调用都把 AGENTS.md 追加到系统提示
   - 功能扩展：Skills、ToolSearch
   - `HistoryMiddleware`
   - `ToolSettlementMiddleware`

   最后用 `create_deep_agent(..., checkpointer=AsyncSqliteSaver)` 生成图。
4. **模型调用前**（`agent/adapters/history.py:abefore_model`）：
   - 摘要 `prepare()`（80% 阈值触发后台压缩，安全时才采用）和大输出驱逐
   - 冲刷 Team 收件箱和持久化输入队列（steer）
   - 用 `ContextProjector.project(snapshot)` 生成投影消息，替换图状态中的 messages
5. **模型调用**（`awrap_model_call`）：
   - 合并工具定义
   - `prepare_model_boundary` 做权限过滤、工具披露和校准，并加入 JSON 输出契约
   - `attach_task_context` 加入动态任务块
   - 通过 `model_request_step` 发请求（`models/adapters/model_requests.py` 负责计量和重试）
   - 遇到 `context_overflow` 时强制压缩后重试一次（第 186-199 行）
   - `decode_task_output` 校验 JSON，然后 `save_message` 持久化助手消息
6. **工具执行**：Deep Agents 负责路由 tool_calls，然后由 settlement 中间件调用 `ToolExecutor.execute`（`tools/services/tool_executor.py:36`），步骤依次是：
   - 截断检查，jsonschema 校验参数
   - `pre_tool_use`/`permission_request` Hook
   - `gate.authorize`，可能触发 LangGraph `interrupt` 等待用户审批（`tools/adapters/permissions.py:11-23,67`）
   - 写/命令类工具执行前做 Git 快照
   - 在沙箱中执行
   - 大输出写入 output store，执行后再做快照 diff

   结果持久化以后，才触发 `post_tool_use`、`file_change`、`error` Hook（`completed()`）。
7. **循环与结束**：`aafter_model` 决定 outcome，处理任务 switch 时的 `jump_to="model"`。`aafter_agent` 依次处理：
   - 被截断的输出最多续写 3 次
   - 有排队输入就继续运行
   - 触发 `session_end` Hook
   - 由 MemoryService 安排一次后台 LangMem 整理
8. **中断与恢复**：如果有 interrupt，返回 `RunResult("needs_input", ..., checkpoint_id)`（`agent.py:288-298`）。`resume()` 会校验 answers 和 interrupt id（第 223-259 行）。进程重启后由 `restore_task` 从 Store 重建，已保存结果的工具不会重复执行（第 172-217 行）。

## 4. 关键子系统

### 4.1 工具、权限与沙箱

- **门链**：`tools/services/permissions.py` 和 `tools/adapters/permissions.py`。
  - 默认 `InterruptToolGate`：非 read 类工具都要走审批 interrupt。
  - Bash 先过 `denied_command` 黑名单（`sandbox/domain/command_policy.py:16-38`）。
  - 只有 `on-auto` 模式，且沙箱可用，且命令通过 `auto_approvable` 时才自动放行。`auto_approvable` 是基于引号解析的白名单：17 个只读命令；`find`、`rg`、`git` 有参数限制；拒绝 `` `$<>\ `` 和换行，拒绝未加引号的 glob。
  - 源码注释明确说黑名单只是纵深防御（第 9-12 行）。这个设计是对的。
- **Worker 门**：`WorkerBoundaryGate`（`tools/services/permissions.py`）对子 Agent 禁用 MCP 和 hook 来源的工具。Bash 和 Skill 要求沙箱可用，并且必须经过审批。
- **Seatbelt profile**：`sandbox/adapters/seatbelt.py:build_profile`。
  - 默认 deny。读权限放开系统目录、临时目录和解释器路径。
  - 之后再 deny `~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.config`、`~/.pycode`，以及所有 `.env*`。工作区内的 `.env.example` 是唯一例外。
  - 写权限只给工作区，并排除 `.git`、`.pycode` 和 dotenv 文件。
- **`scripts/verify_seatbelt.py`**：在 `tests/.runtime-tests/seatbelt-*` 下做真实物理检查，覆盖工作区内外写入、`on-auto`、读取私有文件、`.env` 重命名绕过、网络开关（本地 HTTP 探针）。不需要 API Key。本次只读了代码，没有运行。

### 4.2 上下文管理与压缩（`docs/context-request.md`）

- 请求由五部分组成：固定系统说明（含 JSON 契约）、工具目录（名称、分类、一句话用途）、召回的记忆、任务状态与执行观察、投影后的消息。
- 常用工具始终预加载：Bash、ReadToolResult、ls、read_file、write_file、edit_file、glob、grep。其余工具通过 ToolSearch 按需加载完整定义（`docs/design/tool-discovery.md`）。
- 压缩：80% 阈值触发后台摘要，用源/配置 hash 判断，分两阶段安全采用；也可以手动 `/compact`。`compaction` 记录不能把工具调用和它的结果拆开（`session/adapters/storage/session.py:288-301` 的 `valid_kept_boundary`）。
- 新增（未提交）功能：单 Agent 任务状态。模型输出 `{"task_action":…, "message":…}`，校验后的任务变更存进助手消息 metadata 的 `pycode_task_change`，再由 `TaskProjection` 重放出当前状态（`agent/adapters/task_output.py`、`context/domain/tasks.py`、`context/services/task_projection.py`）。

### 4.3 记忆

- 第一类是指令：`~/.pycode/AGENTS.md`，以及加 `--trust-project` 时项目里的 `AGENTS.md`。由 `MemoryMiddleware` 追加到系统提示，不进入历史记录。
- 第二类是长期记忆：用 LangMem 的 `manage_*/search_*_memory` 工具，并在运行结束或压缩后做一次后台 `create_memory_store_manager` 整理。存储是每个作用域一个 `memory.json`，加上 `changes.jsonl` 变更日志，支持撤销最近一次自动整理。
- 写入过滤（`memory/domain/memory.py:18-27,102-118`）：单条不超过 2000 字符，每个作用域不超过 200 条，并用凭据正则拒绝疑似密钥。
- 后台记忆请求强制关闭 thinking。原因是 DeepSeek 开启 thinking 时与强制 `tool_choice` 冲突，会返回 400，排查过程见 `archive/遇到的问题-排查记录.md`。

### 4.4 Hooks / MCP / Skills

- **Hooks**（`hooks/README.md`）：只有同步的 command（执行命令）和 prompt（添加提示文本，不调用模型）两种。只有 `pre_tool_use` 能阻止工具，Hook 不能批准工具。被中断的 Hook 记为"结果未知"，不会重跑。agent 动作和 async 模式已经删除，旧配置会明确报错。
- **MCP**：支持 stdio 和 HTTP 工具。通过 LangChain `MCPAdapter` 连接，`check_harness` 禁止直接依赖 `mcp` 包。执行前会复核 schema 和权限标记。resources、prompts、elicitation 都已移除，测试时会出现 `LangChainBetaWarning: langchain.mcp is in beta`。
- **Skills**：加载用 Deep Agents 的 `SkillsMiddleware`，通过 `/skills/<source>/` 虚拟挂载只读访问，脚本通过 Bash 执行，走同样的审批。从 GitHub 安装时校验路径和大小，原子替换并保留旧版本（`skills/install.py:297-301`）。内置 Skill 有 commit、review、test 三个（`skills/resources/`）。

### 4.5 存储格式（`docs/storage-format.md`）

- 每个会话一个 `session.sqlite`，使用 LangGraph `SqliteStore`：`entries` namespace 存不可变的 SessionEntry，`session/position` 存 header、leaf_id 和 revision。每次写入用一次 `batch` 事务提交（`session.py:330-339`）。
- 每个会话还有一个 `execution.sqlite`（`AsyncSqliteSaver`），thread_id 为 `session_id:run_id`，图以 `durability="sync"` 运行。
- 其他存储：模型请求与计费用 `runtime.sqlite`；记忆用 JSON 文件；大输出、摘要和 Git 快照是独立对象，Store 中只保存引用。
- 不再读取旧的 `entries.jsonl` 和 `position.json`，也不做自动转换。
- 懒持久化：第一条用户消息或 `hook_call` 出现之前，记录只在内存里（`session.py:240-246`）。

### 4.6 TUI

`ui/adapters/tui.py`（947 行）基于 Textual。只依赖 `ui/ports/ui.py` 的 InteractiveSession 端口，可以注入内存端口单独测试。渲染异常会被隔离（第 250-266 行）。最近的提交主要在打磨 TUI，例如 `5f15cf1` 模型选择器、`833ad10` 折叠工具输出、`2954eae` 审批留在终端记录里。`tests/ui` 下有 17 个测试文件。

## 5. 测试与质量

- **目录**：
  - `tests/` 下按功能分子目录，共 178 个 `test_*.py`（含 `evals/tests`）。数量较多的有 tools 23、context 22、models 21、session 21、ui 17、mcp 12。
  - `tests/adapters`、`tests/core`、`tests/services`、`tests/workflows` 里只剩 `__pycache__`，是旧布局的残留目录。
  - pytest 配置：`testpaths = ["tests","evals/tests"]`，`asyncio_mode=auto`，`--import-mode=importlib`。
- **本次实测**：

| 检查 | 命令 | 结果 |
|---|---|---|
| 单元/集成测试 | `.venv/bin/python -m pytest -q -p no:cacheprovider` | **1254 passed, 1 skipped, 2 warnings，257.15 s**（跳过的是 `tests/sandbox/adapters/test_sandbox_policy.py:73` 的 Windows/Linux 专用向量） |
| Lint | `ruff check src tests scripts evals` | All checks passed |
| 格式 | `ruff format --check ...` | 624 files already formatted |
| 架构 | `python scripts/check_architecture.py` | passed |
| 类型 | `python -m pyright --pythonpath .venv/bin/python` | 0 errors, 0 warnings |
| 复杂度（未启用的规则） | `ruff check src --select C901` | **75 个函数复杂度 > 10**，最高为 `ui/adapters/tui.py:267 render_runtime_event` 24、`tools/adapters/output_store.py:317 read` 20、`tools/services/tool_executor.py:36 execute` 19、`tools/adapters/settlement.py:173 settle_call` 18、`team/tasks.py:112 update_task` 18 |

  上次记录的基线是 1192 passed（`docs/refactor-audit.md`），说明之后的改动补了测试。
- **Ruff 配置**：`select = ["E","F","I","B","ASYNC"]`，行宽 100，目标 py311。
- **评测体系 `evals/`**（独立的包，pycode 不能 import 它）：
  - `evals/coding`：在隔离工作区里运行同一个 harness 解编程题（`solver.py` 注入 `EvaluationToolGate`，只允许写声明过的解答文件），由独立的 pytest 判分。
  - `evals/data/manifest.json` 冻结题目清单。根据 `docs/refactor-audit.md`，两轮真实 DeepSeek 评测都是 12/12 通过（每轮 233 项独立测试）。
  - `evals/quality/langfuse.py` 和 `evals/scripts/run_output_quality.py`、`run_trajectory_quality.py` 用 openevals/agentevals 加 Langfuse 评估输出和轨迹质量。
  - `evals/memory_rules`：已归档的记忆规则对比实验，被 pytest 和 ruff 排除（`norecursedirs`、ruff `exclude`）。

## 6. 开发历史与状态

- **提交历史**：main 共 90 个提交，从 2026-07-26 到 2026-10-28，其中 2026-10-15 一天有 34 个。
  - 首个提交 `a4035bf` 为 "Baseline: recovered sources compiling..."。
  - 早期提交里有 RAG 和 bwrap 等后来已删除的能力。
  - 近期主题依次是：模块化重构（`29d382c`、`8baa5cc`）、app 分区（`1aafcaa`）、组件精简与统一 ToolSearch（`bd2392a`）、TeamState 交接（`5d949be`），最后改为 SQLite 存储（`f3e60cf`）。
- **未提交工作**（`git status`）：62 个已跟踪文件被修改（src 25 个文件 +326/−68，docs 28 个文件），另有 8 个新文件未跟踪：
  - `docs/context-request.md`
  - `agent/adapters/task_output.py`
  - `context/domain/tasks.py`
  - `context/services/task_projection.py`
  - 4 个对应的测试文件

  这些就是"单 Agent 任务状态 + JSON 输出契约"功能，测试已全部通过，但还**没有提交**。
- **Worktree**（`git worktree list` 与 `git cherry main <branch>`）：

| worktree | 分支 | 最后提交 | 尚未进入 main 的提交 |
|---|---|---|---|
| github-main | delivery/github-main | df051d2，10-15 | 0（15 个都有等价提交） |
| s2-memory | s2/memory | 3f80ef1 | 只有 `35e2e58 baseline snapshot` |
| s3-team | s3/inproc-team | f21db32 | 同上；工作区里有一个未跟踪的 `.agents/` |
| s4-modules | s4/modules | d740c16 | 同上 |
| s5-quality | s5/quality | 584237f | 同上 |

  这些分支分别对应《五阶段改造计划》的阶段 2-5（记忆、进程内团队、模块化、质量）。历史改写后，内容已经以树等价的提交接到 main 上。`docs/git-history-map.json` 记录 `all_replayed_trees_identical: true`，baseline `35e2e58` 对应 `33f2d60`。**这些分支都可以清理**，但删除前需要你确认。
- **`docs/refactor-audit.md`** 记录了阶段验收的结果：
  - 首次全量测试 17 个失败，修复后全部通过。
  - 真实模型两轮 12/12。
  - 397 项旧产物清理全部成功。
  - 已快进推送 GitHub main（`1e9cd36`）。
- **`archive/`**：保存历史验证日志、上游基线清单、旧脚本，以及 DeepSeek thinking 与 `tool_choice` 冲突的排查记录。这些内容不维护、不测试、不打包。

## 7. 代码质量评估

### 优点

- **边界强制到位**：AST 检查覆盖别名、延迟 import、再导出来源和端口实现覆盖率，并有反例测试（`tests/architecture/test_dependencies.py`）。这在 Python 项目中少见。
- **资源生命周期严谨**：构造失败时会按顺序关闭已打开的资源（`models/module.py:166`、`mcp/module.py:65`、`app/composition/session.py:156,193,269,296`）。取消时会收拢子任务（`agent.py:collect_tools`）。
- **持久化语义清楚**：结果先保存，再触发 Hook 和事件（`tool_executor.py:completed`）。中断的操作标记为 interrupted，不会重放。审批 interrupt 用 `response_schema` 绑定 `operation_id`。
- **安全设计成熟**：白名单优先、黑名单只做纵深防御；Seatbelt 默认 deny；环境变量白名单；远程 provider 强制 HTTPS；凭据形态的变量不能透传。
- **依赖版本精确锁定**，有可复现的锁文件。pyright、ruff、架构检查全部为零告警。

### 风险、缺陷与技术债

按影响从高到低排列：

1. **【高】JSON 输出契约太脆弱**（未提交的新功能）
   - `agent/adapters/task_output.py:48-69`：只要非空文本不符合 `TaskOutput`，例如模型加了 Markdown 代码围栏、多输出了一段文字，或者 schema 不符，就抛 `ModelError("invalid_task_output")`。
   - `history.py:215` 的这个异常发生在 `save_message`（`history.py:235`）之前，所以已经付费的响应不会被保存。
   - `AgentRuntime.invoke` 在 `agent.py:274-279` 捕获 `ModelError`，直接以 `RunResult("error")` 结束整个运行，中间没有重试或修复。
   - `TASK_OUTPUT_PROMPT`（第 10-34 行）指令很长，还内嵌了完整的 JSON Schema。弱一些的模型或非 DeepSeek 的 provider 违约概率会明显上升。
   - 空文本且没有工具调用、`stop_reason=="stop"` 时也会报错（第 50-52 行）。
2. **【中】文档与代码不一致**
   - `docs/architecture.md:189-191,217,219` 的端口表仍写 `session.adapters.storage.session.JsonlSessionStore`、`JsonlSessionRepository`，第 178、184、225 行写 `team.services.team.TeamMember`、`hooks.services.hooks.Hooks`、`skills.adapters.catalog.FileSkillCatalog`。这些类和模块在源码中都已不存在：实际是 `SqliteSessionStore`、`team/engine.py:TeamMember`、平铺后的 skills/hooks。
   - 架构检查只校验 `docs/design/port-implementations.json`（那里已经是 `SqliteSessionStore`），不校验 Markdown 表格，所以这种漂移检测不到。
   - `docs/architecture.md` 中还有 11 处、`docs/design/inproc-team.md` 中有 12 处在描述 JSONL 会话。README 仍写"当前格式的半行写入恢复"，这是 JSONL 时代的语义。
3. **【中】会话追加的复杂度随长度增长**
   - `session/adapters/storage/session.py:258-260` 的 `validate_branch_entry` 在每次 `append_entry` 时都调用 `SessionTree(self._entries_by_id).path(self._leaf)`，即使类里已经维护了 `_active_path_ids`。
   - 处理 `input_queued` 时还会扫描全部记录（第 271-276 行）。
   - 消息去重分支同样重新计算路径（第 198、217 行）。
   - 所以整个会话的总开销是 O(n²)。对长期运行、带 Team 和长对话的会话不友好。
4. **【中】用 `assert` 做运行时检查**
   - 生产代码中至少有 20 处，例如：
     - `session/adapters/storage/session.py:52,72`：`assert saved is not None`。数据库缺少 position 时抛 AssertionError，而不是 `StorageError`。
     - `agent/adapters/agent.py:173,176`
     - `app/lifecycle/session.py:77,135,191,220`
     - `tools/adapters/settlement.py:127,290,601`
   - 用 `python -O` 运行时这些检查会被移除。`scripts/verify_seatbelt.py:46-104` 的安全验收也全靠 `assert`，在 `-O` 下会"静默通过"。
   - `agent.py:179` 的 `next(...)` 没有默认值。找不到 `run_start` 时抛 StopIteration，在协程里会变成 RuntimeError。第 209 行的 `calls[(...)]` 也可能抛 KeyError。这两处都没有转换成领域错误。
5. **【低-中】复杂度债**：75 个函数 C901 > 10（见第 5 节表格）。`docs/refactor-audit.md` 只承诺"五个指定函数 C901≤15"，现在最高到 24，且 lint 没有启用 C901，复杂度还可能继续上升。`team/engine.py` 有 1190 行，`ui/adapters/tui.py` 有 947 行，是最大的两个文件。
6. **【低】分页依赖未定义的排序**
   - `read_records`（`session.py:55-61`）用 `store.search(limit=1000, offset=...)` 分页。LangGraph SqliteStore 的 search 按 `ORDER BY updated_at DESC` 排序（venv 中 `langgraph/store/sqlite/base.py:587`），而同一个 batch 写入的行 `updated_at` 完全相同。
   - 我在 `/tmp` 做了实验：2500 行全部只有 1 个不同的 `updated_at`。这次分页结果完整，但这依赖 SQLite 的临时 B-tree 排序恰好稳定，SQL 并不保证。
   - 测试里没有超过 1000 条记录的用例（grep 没找到 `range(1001)` 之类）。
7. **【低】全局状态与重复装配**
   - `AgentRuntime.build` 每次运行都调用 `register_harness_profile(provider, ...)`（`agent.py:71`），这是进程级全局注册，以 provider 名为键。如果以后不同运行需要不同 profile，会互相覆盖。
   - `app/cli/main.py:92,94` 的 doctor 路径把 `compose_application(config)` 构造了两次。
8. **【低】`ModelError` 分支没有收尾**：`agent.py:274-279` 捕获 `ModelError`/`GraphRecursionError` 后没有调用 `collect_tools`，也没有 `summary.cancel_background()`。另外两个异常分支（第 268、280 行）都做了。如果后台摘要任务还在跑，会留到 session 关闭时才收拢。
9. **【低】custom 门会替换整条链**：`tools/module.py:163-164` 的 `if custom is not None: gate = custom` 会替换掉 Interrupt、Memory 和 Plan 门。代码注释（`app/configuration/settings.py:29-30`）写明这是嵌入方的有意设计，目前只有 `evals/coding/solver.py:31` 使用。但这是个误用陷阱：嵌入方传入任何 gate，都会丢掉审批和 Plan 限制。
10. **【低】on-auto 白名单的剩余面**：`git diff`、`git log`、`git show` 在仓库配置了 `diff.external` 或 textconv 时会执行外部程序。`command_policy.py:155-164` 只拦截 `--output` 和全局选项。外层的 Seatbelt 能限制后果，但白名单本身没有拒绝 `--ext-diff`、`--textconv`。
11. **【低】仓库卫生**：
    - `tests/adapters`、`tests/core`、`tests/services`、`tests/workflows` 只剩 `__pycache__`。
    - 5 个已合并的 worktree 还在。
    - 根目录有 `.DS_Store`。
    - 部分源码头部带推广性署名注释（`sandbox/domain/command_policy.py:4-7`），有授权记录，但会出现在发行包里。

## 8. 改进建议（按优先级）

1. **P0：先给 JSON 任务契约加容错，再提交这批改动。**
   - 解析失败时，先保存原始响应（标记为 invalid），再给模型发一条修复提示重试 1-2 次，类似已有的截断续写机制 `MAX_OUTPUT_CONTINUATIONS`。
   - 至少去掉外层 Markdown 围栏后再解析。
   - 考虑按 provider 开关这个功能。
   - 补一组用例：带围栏、多余文字、schema 不符、空文本。
   - 理由：这是唯一可能直接让真实运行失败、还浪费 token 的问题，而且目前未提交，改起来成本最低。
2. **P1：修正文档漂移，并加检查。**
   - 从 `port-implementations.json` 自动生成 `docs/architecture.md` 的端口表，或者在 checker 里比对两者。
   - 统一清理架构文档、inproc-team 设计文档和 README 中关于 JSONL 的描述。
   - 理由：项目把文档当作架构契约，而且 README 引导读者从这些文档入手。
3. **P1：把运行时的 `assert` 换成显式检查。**
   - 运行时不变量改为 `if ...: raise StorageError/ModelError(...)`。
   - `agent.py:179` 的 `next()` 加默认值并抛 `StorageError("run_start_missing")`。
   - `verify_seatbelt.py` 改用显式判断并以非零退出码结束。
   - 理由：保证在 `-O` 下也正确，并且错误码可预期，CLI 会把 `DomainError` 转成 JSON 错误。
4. **P2：消除会话追加的 O(n²)。**
   - `validate_branch_entry` 和去重分支直接使用 `_active_path_ids`，或者维护一个集合。
   - 为 run_start/run_end 维护索引。
   - 分页读取改为在 Python 侧一次读完，或者在 key 中嵌入 sequence，不再依赖 `updated_at` 的排序。
   - 补一个超过 1000 条记录的重开测试。
5. **P2：控制复杂度。** 在 ruff 中启用 `C90`，并设 `max-complexity = 15`，与审计承诺一致。现有违规先用 per-file 例外或基线锁住，再逐步拆分 `render_runtime_event`、`output_store.read`、`ToolExecutor.execute` 和 `settle_call`。
6. **P3：小修补。**
   - `invoke` 的 `ModelError` 分支补上取消后台摘要。
   - doctor 只构造一次 Application。
   - harness profile 只在第一次注册，或者用 provider 加配置作键。
   - on-auto 的 git 白名单拒绝 `--ext-diff`、`--textconv`。
   - custom 门改成包裹默认链，而不是替换；或者把参数改名为 `replace_tool_gate`，让替换行为显而易见。
7. **P3：清理仓库（需要你确认后再做）。**
   - 删除 5 个已合并的 worktree 及分支：`git worktree remove`，然后 `git branch -d`。由于历史被改写过，`-d` 可能会拒绝删除，需要先确认以后再用 `-D`。
   - 删除空的测试目录。
   - 把 `.DS_Store` 加入 `.gitignore`。

---

### 附：本次执行的命令

- `git -C <repo> log/status/worktree list/cherry/rev-list`
- `.venv/bin/python -m pytest -q -p no:cacheprovider`，结果 1254 passed / 1 skipped
- `.venv/bin/python -m ruff check|format --check src tests scripts evals --no-cache`
- `.venv/bin/python scripts/check_architecture.py`
- `.venv/bin/python -m pyright --pythonpath .venv/bin/python`
- `.venv/bin/python -m ruff check src --select C901`
- 在 `/tmp` 用 SqliteStore 做的分页排序实验，实验文件已删除
