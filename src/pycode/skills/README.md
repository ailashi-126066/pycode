# Skills

本模块负责本地 Skill 的发现、读取、创建、更新、下载和安装。加载使用 Deep Agents 的
`SkillsMiddleware`；Skill 的脚本通过普通 Bash 工具执行，不自动注册 Python 函数。

阅读顺序：`api.py` → `catalog.py` / `install.py` → `framework.py`。`module.py` 只供应用组装。
照 Pi，Skill 由一次成功的 `read /skills/<source>/<name>/SKILL.md` 加载：结果的 `details.skill` 记下名字、路径、
content_hash 和 allowed-tools，`types.active_skills` 沿当前分支读这些结果。压缩摘要末尾写一行已加载的 skill。

- `Skills.list/search/read`：查找定义，`search` 返回用于更新的 revision。
- `Skills.load(name, args)`：只准备请求，不访问模型，也不启动任务。
- `Skills.create/update`：写入正文；update 必须提供当前 revision，不静默覆盖。
- `Skills.install`：下载 GitHub 定义和附属文件，校验路径/大小，原子安装并保留旧版本。
- `AgentSession.load_skill`：用户命令入口，准备请求后显式运行一次。
- `SaveSkill`：模型通过公共审批路径创建/更新定义；安装/创建不激活 Skill。

CLI：`skill create NAME --file SKILL.md`、`skill read NAME`、
`skill update NAME --file SKILL.md --revision SHA256`、`skill install URL`。
默认写入用户目录；`--scope project` 使用已信任项目的 `.pycode/skills`。
TUI：`/skill create NAME DESCRIPTION` 创建正文草稿，`/skill read NAME` 显示正文和 revision。

加载后的说明包含物理资源目录，Agent 通过 `Bash` 执行例如 `python /path/to/skill/scripts/check.py`。
读取资料仍使用 `/skills/...` 虚拟挂载。命令审批、环境过滤、进程取消、输出记录继续由公共设施负责。
OS 沙箱只增加受信任 Skill 目录的读取范围，不开放用户配置目录，也不扩大工作区写入范围。
`tool.json` 是普通文件，不会触发导入或工具注册。`mode: fork` 明确拒绝；内置 review 已改为 inline。

目录与目录索引属于会话，搜索选择和读取版本属于本次运行；本模块没有长期连接或后台任务。
