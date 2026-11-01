# pycode

本地终端编程 Agent：对话、读写代码、执行命令、保存会话，以及进程内多 Agent 协作。
提供非交互 CLI 和 Textual TUI。模型/工具循环使用 Deep Agents，长期记忆使用 LangMem。

## 源码与功能导读

当前结构见 [组件精简说明](docs/design/component-simplification.md)。
每次发给模型的内容、任务更新和工具预加载见 [上下文组成](docs/context-request.md)。
从各模块的 README.md 和 api.py 开始：Skills、MCP、Hooks、Team 已按职责平铺。
[原调用链手册](docs/interview/README.md)和离线网页是精简前的教学记录，部分节点已移除，
不作为当前接口或运行行为的依据。

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

无需开发和评测依赖时，也可以在虚拟环境中直接 `python -m pip install .`。
如果旧虚拟环境曾注册 `py` console script，请卸载该旧包；保留系统 Python Launcher。

## 配置模型并开始

复制 [Provider 示例](docs/provider-config.example.json) 为自己的配置文件，按服务实际能力
设置模型 ID、窗口和输出额度。`api_key_env` 是环境变量名，配置文件不保存 API Key。
非本机服务必须使用 HTTPS。

macOS / Linux：

```bash
cp docs/provider-config.example.json providers.json
export DEEPSEEK_API_KEY='替换为自己的密钥'
pycode --workspace /path/to/project --memory-mode off run '分析项目结构' --providers providers.json
pycode --workspace /path/to/project tui --providers providers.json
```

PowerShell 中用 `Copy-Item` 复制示例，用 `$env:DEEPSEEK_API_KEY = '自己的密钥'` 设置凭据，
再通过 `.venv\Scripts\pycode.exe` 执行同样的命令。
也支持从原工作区的本地 `.env` 加载凭据；凭据不会复制到评测或 worker 工作区。

`--trust-project` 启用项目 `AGENTS.md` 和受信任的项目 Skill 来源。
用户 `~/.pycode/AGENTS.md` 默认加载。可用 `--memory-mode off` 禁用长期笔记，
或保留默认 auto，由 LangMem 主动记忆并在对话结束/压缩时后台整理。

## 常用操作

| 场景 | 命令或操作 |
| --- | --- |
| 机器可读流 | `run '任务' --providers providers.json --output-format stream-json` |
| 会话列表/打开 | `session list`、`session open ID-or-name` |
| 进入计划 | TUI `/plan`，审阅后选择执行方式 |
| 查看/撤销记忆 | TUI `/memory`、`/memory undo` |
| 查看团队/取消成员 | TUI `/agents`、`/agents cancel AGENT_ID` |
| 压缩上下文 | TUI `/compact` |
| 文件与对话回退 | TUI `/revert ENTRY_ID both`，先预览后确认 |
| Skill | TUI `/skill create NAME DESCRIPTION`、`/skill read NAME`、`/skill install URL` |
| 存储清理预览 | `storage gc --dry-run` |

非交互 CLI 需要审批时输出 `needs_input` 并退出 2；重新打开后可通过 resume 回答保存的审批。
完整记录保存于 SQLite Store，执行位置保存于 checkpoint；重新打开不自动执行工具。

## 团队、命令与权限

主 Agent 可按需派出 readonly 或独立 Git worktree worker；`--max-agents 1` 到
`--max-agents 4` 控制同时运行人数。任务板、消息与状态保存到 SQLite Store，
子 Agent 的审批转发到主界面。取消主运行会收拢子任务；重启不会自动续跑。
worker 完成后主 Agent 先 review，逐次审批 merge，再按依赖顺序集成和清理。

macOS 的 `--sandbox on` 启用 Seatbelt，`--sandbox on-auto` 只自动批准只读白名单命令。
`--sandbox-network` 显式允许沙箱网络。worker 的命令必须使用可用的严格隔离后端；
不支持的系统仍可使用 readonly，worker 文件工具的权限由固定工作区边界约束。
私有目录、内部状态和 `.env*` 受保护，工作区 `.env.example` 是精确例外。

子进程默认只继承白名单环境。需要 ssh-agent 或代理时，可显式传入普通变量：

```bash
pycode --pass-env SSH_AUTH_SOCK --pass-env HTTPS_PROXY tui --providers providers.json
```

API Key、token、密码和 AWS 凭据形态的变量仍拒绝透传。
透传变量不改变沙箱文件和网络权限；命令 Hook 的显式 `$ENV.NAME` 引用遵循自身配置。

## 开发与评测

```bash
python -m ruff check src tests scripts evals
python -m ruff format --check src tests scripts evals
python scripts/check_architecture.py
python -m pyright --pythonpath .venv/bin/python
python -m pytest -q
```

评测是独立包：`python -m evals coding --help`；真实模型评测会产生 API 请求，
请先检查 Provider 和输出额度。macOS 沙箱实测使用 `scripts/verify_seatbelt.py`，
发行安装验收使用 `scripts/verify_release.py`。本项目不配置 GitHub Actions/CI。

设计见 [第二个计划.md](计划/第二个计划.md)，数据格式见 [存储格式](docs/storage-format.md)，
源码边界与端口实现见 [架构](docs/architecture.md)，阶段进度及验收见
[五阶段改造计划.md](计划/五阶段改造计划.md)。历史日志保存在 `archive/`，不作为运行时或验证依据。


## 从代码入口阅读

统一组装从 `src/pycode/app/bootstrap.py` 开始，具名模块集合在
`app/composition/modules.py`。每个功能的 `module.py` 负责自己内部的组装，局部 README
说明其输入、产物与资源寿命；`skills` 和 `mcp` 可分别替换或设为 `None` 禁用。
`kernel` 只保留 `domain / ports / services / adapters` 四层，不混放功能实现。
完整装配约定见 [模块装配设计](docs/design/module-assembly.md)。

## 精简后的扩展

Skill 正文和附属文件由框架读取，脚本通过 Bash 执行；不再自动注册 Python 函数。
CLI 支持 `skill create NAME --file SKILL.md`、`skill read NAME` 和
`skill update NAME --file SKILL.md --revision SHA256`。下载仍使用 `skill install URL`。
Skill fork、agent/background Hook 及 MCP resources/prompts/elicitation 已移除。
Hooks 保留同步 command/prompt；MCP 保留 stdio/HTTP 工具，本地与远端统一使用 ToolSearch，删除 MCPCall。
Team 邮箱、worktree 和合并不变。旧配置使用已移除模式时明确报错。

## 当前执行与数据策略

主模型失败后只按既有策略重试同一模型，不自动切换备用模型；`fallback_models` 配置已移除，
保留 `/model` 等手动选择。Hooks 只保留同步提醒、检查和拦截，`hooks`/`/hooks` 核对命令已删除。
检查失败会记录并返回给 Agent；前置检查失败阻止对应工具。中断的旧操作不会自动重放。

只读取当前定义的存储格式：旧会话 v0、旧记忆迁移字段/旧模式和旧请求候选模型字段会明确报错，
不自动转换，也不删除或重写原文件。当前格式的半行写入恢复、分支恢复和执行记录保留。

工具发现：本地与 MCP 统一通过 ToolSearch。模型先看分类名称和一句话用途，再按需加载完整定义；无额外模型工具选择器。参见 [工具发现设计](docs/design/tool-discovery.md)。

Team 任务状态、无活动超时与进程内 A→B 交接见 [TeamState 设计](docs/design/team-state-handoff.md)。完整记录使用官方 SqliteStore，执行图使用本地 AsyncSqliteSaver。

框架基线：Deep Agents 0.7.32、LangChain 1.4.7 / Core 1.7.0、LangGraph 1.2.18 / Checkpoint 4.3.1、FastMCP 4.1.4。具体开发依赖见 `requirements-dev.lock`。Team 继续使用进程内执行与邮箱，不引入 Agent Server；会话持久化已迁移为 SqliteStore + AsyncSqliteSaver。
