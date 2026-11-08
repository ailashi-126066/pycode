"""Slash commands. Each one is a function; COMMANDS lists them for help and completion."""

from dataclasses import replace
from uuid import uuid4

from ...compaction.summarize import count_turns, is_user_turn
from ...middleware.skills import list_skills
from ...middleware.subagents import STATUS
from ...middleware.task_board import render
from ...models import load_providers
from ...sessions import branches
from ...sessions.catalog import delete_session, get_session, list_sessions
from ..stream import pause_event

COMMANDS = {
    "/new": "新建会话",
    "/sessions": "列出当前工作区的会话",
    "/open": "/open ID：打开会话，显示完整历史",
    "/revert": "/revert N：回到第 N 轮结束时，之后的对话留在另一个分支",
    "/branches": "列出当前会话的分支",
    "/switch": "/switch N：切换到 /branches 里的第 N 个分支",
    "/delete": "/delete ID：删除会话",
    "/compact": "立即压缩上下文",
    "/plan": "/plan 要求：只读规划，再由你审批计划；/plan show 看批准的计划；/plan off 退出",
    "/tasks": "显示任务板（F7 显示或隐藏输入框上方的任务板）",
    "/agents": "列出这个会话的子 agent 和它们的状态",
    "/skills": "列出可用的 Skill",
    "/skill": "/skill 名字 [要求]：按这个 Skill 的说明完成要求",
    "/mcp": "列出 MCP 服务器和它们的工具",
    "/memory": "列出长期记忆；/memory delete 名字：删除一条",
    "/model": "/model [名字]：列出或切换 providers.json 里的模型",
    "/theme": "切换深色和浅色主题",
    "/help": "列出所有命令",
    "/quit": "退出",
}
# Commands that change the conversation wait until the model stops.
WHILE_RUNNING = {
    "/sessions",
    "/branches",
    "/skills",
    "/mcp",
    "/memory",
    "/tasks",
    "/agents",
    "/theme",
    "/help",
    "/quit",
}


async def run(app, text: str) -> None:
    name, _, argument = text.partition(" ")
    handler = HANDLERS.get(name)
    if handler is None:
        app.show_notice(f"没有命令 {name}，输入 /help 查看", "error")
    elif app.running() and name not in WHILE_RUNNING:
        app.show_notice("模型还在运行，等它结束或按 Ctrl+C 停止", "warning")
    else:
        try:
            await handler(app, argument.strip())
        except ValueError as error:  # bad argument, unknown session, no such turn …
            app.show_notice(str(error), "error")


async def new(app, argument):
    await app.switch_session(uuid4().hex[:12])
    await app.clear_transcript()
    await app.refresh_status()
    app.show_notice("已新建会话")


async def sessions(app, argument):
    found = await list_sessions(app.opened.store, app.settings.workspace)
    lines = [f"{s.id}  {s.updated_at:%Y-%m-%d %H:%M}  {s.title}" for s in found]
    await app.write_info("\n".join(lines) or "这个工作区还没有会话")


async def open_session(app, argument):
    session = await get_session(app.opened.store, argument)
    if session is None:
        raise ValueError(f"没有会话 {argument}")
    if session.workspace != str(app.settings.workspace):
        raise ValueError(f"会话 {argument} 属于 {session.workspace}，请用 --workspace 打开")
    if argument != app.session_id:
        await app.switch_session(argument)
    await app.clear_transcript()
    await app.show_history()
    await app.refresh_status()
    state = await app.opened.agent.aget_state(app.config)
    if state.interrupts:  # it was waiting for approval when last closed
        app.answer_pending([pause_event(item) for item in state.interrupts])


async def revert(app, argument):
    if not argument.isdigit():
        raise ValueError("用法：/revert N")
    await branches.revert(app.opened.agent, app.config, int(argument))
    await app.clear_transcript()
    await app.show_history()
    await app.refresh_status()
    app.show_notice(f"已回到第 {argument} 轮结束时；之后的对话保留在另一个分支")


async def list_branches(app, argument):
    agent = app.opened.agent
    head = (await agent.aget_state(app.config)).config["configurable"].get("checkpoint_id")
    lines = []
    for number, leaf in enumerate(await branches.branches(agent, app.config), 1):
        messages = leaf.values.get("messages", [])
        asks = [m.text for m in messages if is_user_turn(m)]
        last = asks[-1][:40] if asks else "（已压缩）"
        mark = "（当前）" if leaf.config["configurable"]["checkpoint_id"] == head else ""
        wait = "（等待审批）" if leaf.next else ""
        lines.append(f"[{number}] {count_turns(messages)} 轮  最后一问：{last}{mark}{wait}")
    await app.write_info("\n".join(lines) or "这个会话还没有内容")


async def switch(app, argument):
    leaves = await branches.branches(app.opened.agent, app.config)
    if not argument.isdigit() or not 1 <= int(argument) <= len(leaves):
        raise ValueError("用法：/switch N，N 来自 /branches")
    await branches.switch(app.opened.agent, app.config, leaves[int(argument) - 1])
    await app.clear_transcript()
    await app.show_history()
    await app.refresh_status()
    app.show_notice(f"已切换到分支 {argument}")


async def delete(app, argument):
    if await get_session(app.opened.store, argument) is None:
        raise ValueError(f"没有会话 {argument}")
    checkpointer = app.opened.agent.checkpointer
    await delete_session(checkpointer, app.opened.store, argument)
    if argument == app.session_id:
        await new(app, "")
    app.show_notice(f"已删除会话 {argument}")


async def compact(app, argument):
    def write(update):
        app.show_notice(update["compaction"])

    await app.opened.compaction.compact_now(app.opened.agent, app.config, write)
    await app.clear_transcript()
    await app.show_history()
    await app.refresh_status()


async def skills(app, argument):
    found = await list_skills(app.opened.skills)
    lines = [f"{s['name']}  {s['description']}\n    {s['path']}" for s in found]
    await app.write_info("\n".join(lines) or "没有找到 Skill")


async def skill(app, argument):
    name, _, request = argument.partition(" ")
    if not name:
        raise ValueError("用法：/skill 名字 [要求]")
    if name not in {s["name"] for s in await list_skills(app.opened.skills)}:
        raise ValueError(f"没有 Skill {name}，输入 /skills 查看")
    app.run_turn(request.strip() or f"按 {name} Skill 的说明执行", skill=name)


async def mcp_servers(app, argument):
    lines = []
    for server in app.opened.servers:
        approval = "需要审批" if server.approve else "免审批"
        if server.error:
            lines.append(f"{server.name}  连接失败：{server.error}")
        else:
            lines.append(f"{server.name}  {len(server.tools)} 个工具，{approval}")
            lines += [f"    {tool.name}" for tool in server.tools]
    await app.write_info("\n".join(lines) or f"没有配置 MCP 服务器（{app.settings.mcp_file}）")


async def plan(app, argument):
    state = await app.opened.agent.aget_state(app.config)
    if argument == "show":
        await app.write_info(state.values.get("plan") or "这个会话还没有批准过计划")
    elif argument == "off":
        await app.opened.agent.aupdate_state(app.config, {"plan_mode": False})
        await app.refresh_status()
        app.show_notice("已退出 Plan 模式")
    elif argument:
        app.run_turn(argument, plan=True)
    else:
        raise ValueError("用法：/plan 要求 | /plan show | /plan off")


async def tasks(app, argument):
    board = (await app.opened.agent.aget_state(app.config)).values.get("task_board")
    await app.write_info(render(board) if board else "没有任务板；/plan 的计划批准后才有")


async def agents(app, argument):
    lines = []
    for agent in await app.opened.subagents.list(app.session_id):
        line = f"{agent.name}  {STATUS[agent.status]}  {agent.mode}  {agent.description}"
        lines.append(line + (f"\n    分支 {agent.branch}" if agent.branch else ""))
    await app.write_info("\n".join(lines) or "这个会话还没有子 agent")


async def memory(app, argument):
    action, _, name = argument.partition(" ")
    if action == "delete":
        if not app.opened.memory.delete(name.strip()):
            raise ValueError(f"没有叫 {name.strip()} 的记忆")
        app.show_notice(f"已删除记忆 {name.strip()}")
        return
    if action:
        raise ValueError("用法：/memory | /memory delete 名字")
    entries = app.opened.memory.entries()
    lines = [f"{e.name}  {e.type}  {e.modified}  {e.description}" for e in entries]
    where = f"\n\n文件在 {app.settings.memory_dir}"
    await app.write_info("\n".join(lines) + where if lines else "还没有长期记忆")


async def model(app, argument):
    providers = load_providers(app.settings.providers_file)
    if not argument:
        current = app.opened.provider.name
        lines = [f"{'*' if p.name == current else ' '} {p.name}  {p.model_id}" for p in providers]
        await app.write_info("\n".join(lines))
        return
    if argument not in {p.name for p in providers}:
        raise ValueError(f"providers.json 里没有 {argument}")
    app.settings = replace(app.settings, provider_name=argument)
    await app.reopen_agent()
    await app.refresh_status()
    app.show_notice(f"已切换到 {argument}")


async def theme(app, argument):
    app.action_toggle_theme()


async def help_command(app, argument):
    await app.write_info("\n".join(f"{name}  {text}" for name, text in COMMANDS.items()))


async def quit_command(app, argument):
    app.exit()


HANDLERS = {
    "/new": new,
    "/sessions": sessions,
    "/open": open_session,
    "/revert": revert,
    "/branches": list_branches,
    "/switch": switch,
    "/delete": delete,
    "/compact": compact,
    "/plan": plan,
    "/tasks": tasks,
    "/agents": agents,
    "/skills": skills,
    "/skill": skill,
    "/mcp": mcp_servers,
    "/memory": memory,
    "/model": model,
    "/theme": theme,
    "/help": help_command,
    "/quit": quit_command,
}
