# Skills
本模块负责本地 Skill 的发现、读取、创建、更新、下载和安装。加载使用 Deep Agents 的
`SkillsMiddleware`；Skill 的脚本通过普通 Bash 工具执行，不自动注册 Python 函数。
阅读顺序：`skills.py`（`SkillsSession`、`Skills` 服务）→ `store.py` / `install.py` → `tools.py`。
照 Pi，Skill 由一次成功的 `read /skills/<source>/<name>/SKILL.md` 加载：结果的 `details.skill` 记下名字、路径、
content_hash 和 allowed-tools，`types.active_skills` 沿当前分支读这些结果。压缩摘要末尾写一行已加载的 skill。

`SkillsSession(trusted=, index_budget=)` 提供的具体方法（由 app 按需连接）：
- `open`：按信任状态确定根目录，建 `Skills` 服务。
- `prepare_run`：扫描目录，目录有变化时在分支上记一条 `skill_catalog`，建本次运行的选择和 `/skills/<source>/` 挂载。
- `tools`：`SkillSearch`，非只读时加 `InstallSkill`、`SaveSkill`；`mounts`：只读的 `/skills/<source>/`。
- `middleware`：`SkillsMiddleware` 子类，SkillSearch 改变选择后重新载入列表。
- `gates`、`select_tools`：活跃 Skill 的 allowed-tools 收窄工具列表并拒绝范围外的调用（读取 `/skills/` 下的说明除外）。
- `before_tool_call`/`after_tool_call`：读取 SKILL.md 成功时在结果 `details` 写 `skill` 和 `protected_content`。
- `on_event`：SkillSearch 结束后按结果更新选择（目录过大、只列选中项时）。

- `Skills.list/search/read`：查找定义。
- `Skills.load(name, args)`：只准备请求，不访问模型，也不启动任务。
- `Skills.create/update`：create 要求 Skill 不存在；update 要求已存在，直接覆盖。
- `Skills.install`：下载 GitHub 定义和附属文件，校验路径/大小，原子安装并保留旧版本。
- `SaveSkill`：模型通过公共审批路径创建定义，`replace=true` 时覆盖已有定义；安装/创建不激活 Skill。
CLI：`skill create NAME --file SKILL.md`、`skill read NAME`、`skill update NAME --file SKILL.md`、
`skill install URL`。默认写入用户目录；`--scope project` 使用已信任项目的 `.pycode/skills`。
加载后的说明包含物理资源目录，Agent 通过 `Bash` 执行例如 `python /path/to/skill/scripts/check.py`。
读取资料仍使用 `/skills/...` 虚拟挂载。命令审批、环境过滤、进程取消、输出记录继续由公共设施负责。
OS 沙箱只增加受信任 Skill 目录（`SkillsSession.script_roots`）的读取范围，不开放用户配置目录，也不扩大工作区写入范围。
`tool.json` 是普通文件，不会触发导入或工具注册。`mode: fork` 明确拒绝；内置 review 已改为 inline。
目录索引属于会话，搜索选择和读取版本属于本次运行；本模块没有长期连接或后台任务。
