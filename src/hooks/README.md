# Hooks

HooksService 是普通服务，不继承 Feature。command 执行显式配置的命令，prompt 追加提示。
app 注入 sandbox.run_command、shell 与环境引用；本包不 import sandbox，不自行管理子进程。

阅读顺序：hooks.py（服务、执行和结果记录）→ config.py（外部配置）→ types.py（值与条件）。

构造：`HooksService(trusted=, worker=, run_command=, shell=, environment=, enabled=True)`。
`open(SessionScope)` 加载配置，项目 hooks 仅 trusted 生效，worker/read_only 只保留 prompt，执行 startup。
`aclose()` 执行 shutdown。`run_hook(RunScope, event, message="", error="")` 由 app/agent 明确接入
run_start/run_end、turn_start/turn_end、pre_send/post_receive、compact 和运行错误边界。
`before_tool_call(run, spec, call)` 做 pre_tool_use，失败或 reject 返回 block；不能批准父工具。
`after_tool_call(run, call, result)` 做 post_tool_use，实际快照变化时做 file_change，工具错误做 error。
`permission_request(run, call)` 在审批边界调用。没有通用事件扫描或 lifecycle Protocol。

配置中 once 的已执行结果仍记录在历史中。command 变量通过独立环境变量替换，防止插入 shell 代码；
ENV 引用只显式传给命令，不允许进入 prompt。命令超时/取消/环境白名单由注入的沙箱命令边界负责。
普通 hook 失败被记录供模型阅读，pre_tool_use 保持 fail-closed；取消不吞掉。
