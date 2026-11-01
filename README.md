# pycode

本地终端编程 Agent：对话、读写代码、执行命令、保存会话，可以派出子 agent 并行工作。
提供非交互 CLI 和 Textual TUI。底层照 Pi 翻译成 Python，用 Deep Agents / LangGraph / LangChain 实现，
长期记忆用 LangMem。

## 源码导读

- [架构](docs/architecture.md)：包结构、一次运行的流程、"转移 → 记录 → 事件 → Hook"对照。
- [Pi 对照](docs/pi-map.md)：Pi 概念 → pycode 文件 → 框架机制，以及建议的阅读顺序。
- [上下文组成](docs/context-request.md)：每次发给模型的内容、四道转换边界、任务板注入、压缩。
- [存储格式](docs/storage-format.md)：会话记录类型、工具调用、运行与任务板记录。
- [工具发现](docs/design/tool-discovery.md)、[Subagents](docs/design/subagents.md)、[源码来源](docs/source-map.md)。
- 各包的 README.md；组装从 `src/pycode/app/bootstrap.py` 开始。

## 安装

需要 Python 3.11+（开发验证使用 3.12），以及 PATH 中的 `rg`、`git`、`bash`。
macOS 可用 `brew install ripgrep`，Debian/Ubuntu 可用 `sudo apt-get install ripgrep`。
Windows 需要 Git Bash；文件搜索仍需要单独安装 ripgrep。

macOS / Linux，从仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e '.[dev,eval]'
pycode doctor --json
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e '.[dev,eval]'
.venv\Scripts\pycode.exe doctor --json
```

不需要开发和评测依赖时，也可以在虚拟环境中直接 `python -m pip install .`。

## 配置模型并开始

复制 [Provider 示例](docs/provider-config.example.json) 为自己的配置文件，按服务实际能力
设置模型 ID、窗口和输出额度。`api_key_env` 是环境变量名，配置文件不保存 API Key。
非本机服务必须使用 HTTPS。

```bash
cp docs/provider-config.example.json providers.json
export DEEPSEEK_API_KEY='替换为自己的密钥'
pycode --workspace /path/to/project --memory-mode off run '分析项目结构' --providers providers.json
pycode --workspace /path/to/project tui --providers providers.json
```

PowerShell 中用 `Copy-Item` 复制示例，用 `$env:DEEPSEEK_API_KEY = '自己的密钥'` 设置凭据。
也支持从工作区的本地 `.env` 加载凭据；凭据不会复制到子 agent 的 worktree。

`--trust-project` 启用项目 `AGENTS.md` 和项目 Skill 来源；用户 `~/.pycode/AGENTS.md` 默认加载。
长期记忆默认 auto：模型用 `manage_user_memory` / `manage_project_memory` 主动记，压缩被采用后后台整理一次；
`--memory-mode off` 关闭。`--max-turns`（默认 100）限制一次运行的轮数。

## 工具

内置工具照 Pi：`read`、`bash`、`edit`、`write`、`grep`、`find`、`ls`，默认并行，edit/write 同一文件排队。
另有 `ToolSearch`（按名字或关键词加载延迟工具）、`TaskWrite` / `TaskUpdate`（任务板）、`ExitPlanMode`、
`AskUserQuestion`、`Agent` / `SendMessage` / `StopAgent`（子 agent），以及 Skill、MCP 和记忆工具。
MCP 工具和其他 deferred 工具先只在目录里给名字，`ToolSearch` 加载后才能调用。

## 任务板与 Plan

多步骤、要改文件或要验证的工作，模型用 `TaskWrite` 建任务板：目标、`done_when`、步骤（`depends_on` 有先后约束时
才写）。`TaskUpdate` 推进步骤，完成要有证据：验收项可以带 `check` 命令，系统在步骤开始后找完全相同且成功的
bash 调用。任务板以 completed、failed、blocked（把问题交给用户）或 abandoned 结束。`/plan` 进入 Plan 模式：
只读规划，计划文档末尾的 `pycode-tasks` 代码块经审批后变成执行任务板。

## 常用操作

| 场景 | 命令或操作 |
| --- | --- |
| 机器可读流 | `run '任务' --providers providers.json --output-format stream-json` |
| 会话列表/打开 | `session list`、`session open ID-or-name` |
| 进入计划 | TUI `/plan`，审阅后选择执行方式 |
| 任务板 | TUI `/tasks`（可并行时附分层和关键路径）、`/tasks clear` |
| 子 agent | TUI `/agents`、`/agents cancel AGENT_ID` |
| 查看/编辑记忆 | TUI `/memory`（edit/delete/clear/extract/mode） |
| 压缩上下文 | TUI `/compact` |
| 文件与对话回退 | TUI `/revert ENTRY_ID both`，先预览后确认 |
| Skill | TUI `/skill create NAME DESCRIPTION`、`/skill read NAME`、`/skill install URL`、`/skill:NAME` |
| 存储清理 | `storage gc --dry-run` 预览，`storage gc` 直接删除未引用对象 |
| 删除会话 | `storage delete-session ID`（确认后直接删除，不能恢复） |

非交互 CLI 需要审批时输出 `needs_input` 并退出 2；重新打开后可以 resume 回答保存的审批。
完整记录保存在 SQLite Store，执行位置保存在 checkpoint；重新打开不自动执行工具。

## 子 agent、命令与权限

主 agent 用 `Agent` 派出 readonly 或 worktree 子 agent，前台等结果或 `background=true` 后台运行；
`--max-agents 1` 到 `4` 控制同时运行数。子 agent 的审批转到主界面；非交互模式下子 agent 不能审批，需要审批的
调用直接失败。worktree 子 agent 有改动时保留分支，由主 agent 用 bash `git merge`（照常审批）。详见
[Subagents](docs/design/subagents.md)。

macOS 的 `--sandbox on` 启用 Seatbelt，`--sandbox on-auto` 只自动批准白名单里的只读命令，
`--sandbox-network` 显式允许沙箱网络。worktree 子 agent 的命令必须在可用的系统沙箱里运行。
私有目录、内部状态和 `.env*` 受保护，工作区 `.env.example` 是精确例外。

子进程默认只继承白名单环境。需要 ssh-agent 或代理时，可显式传入普通变量：

```bash
pycode --pass-env SSH_AUTH_SOCK --pass-env HTTPS_PROXY tui --providers providers.json
```

API Key、token、密码和 AWS 凭据形态的变量仍拒绝透传。

## 事件与 Hook

事件照 Pi 共 18 种（`agent_start/end`、`turn_start/end`、`message_start/update/end`、
`tool_execution_start/update/end`、`needs_input`、`result`、`retry`、`compaction`、`model_change`、`subagent`、
`task`、`notification`），`--output-format stream-json` 原样输出。Hook 共 14 种：`startup`、`shutdown`、
`run_start`、`run_end`、`turn_start`、`turn_end`、`pre_send`、`post_receive`、`pre_tool_use`、`post_tool_use`、
`compact`、`permission_request`、`file_change`、`error`；配置里用旧名字会报错并提示新名字。`pre_tool_use`
出错时按拦截处理。

## 开发与评测

```bash
python -m ruff check src tests scripts evals
python -m ruff format --check src tests scripts evals
python scripts/check_architecture.py
python -m pyright --pythonpath .venv/bin/python
python -m pytest -q
```

评测是独立包：`python -m evals coding --help`；真实模型评测会产生 API 请求，请先检查 Provider 和输出额度。
macOS 沙箱实测用 `scripts/verify_seatbelt.py`，发行安装验收用 `scripts/verify_release.py`。本项目不配置 CI。

框架基线：Deep Agents 0.7.32、LangChain 1.4.7、LangGraph 1.2.18、FastMCP 4.1.4，具体版本见
`requirements-dev.lock`。
