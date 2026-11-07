"""Slash commands. Each one is a function; COMMANDS lists them for help and completion."""

from dataclasses import replace
from uuid import uuid4

from langchain_core.messages import HumanMessage

from ...compaction.summarize import count_turns, is_summary
from ...models import load_providers
from ...sessions import branches
from ...sessions.catalog import delete_session, get_session, list_sessions
from ..stream import Approval

COMMANDS = {
    "/new": "新建会话",
    "/sessions": "列出当前工作区的会话",
    "/open": "/open ID：打开会话，显示完整历史",
    "/revert": "/revert N：回到第 N 轮结束时，之后的对话留在另一个分支",
    "/branches": "列出当前会话的分支",
    "/switch": "/switch N：切换到 /branches 里的第 N 个分支",
    "/delete": "/delete ID：删除会话",
    "/compact": "立即压缩上下文",
    "/model": "/model [名字]：列出或切换 providers.json 里的模型",
    "/theme": "切换深色和浅色主题",
    "/help": "列出所有命令",
    "/quit": "退出",
}
# Commands that change the conversation wait until the model stops.
WHILE_RUNNING = {"/sessions", "/branches", "/theme", "/help", "/quit"}


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
    app.session_id = uuid4().hex[:12]
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
    app.session_id = argument
    await app.clear_transcript()
    await app.show_history()
    await app.refresh_status()
    state = await app.opened.agent.aget_state(app.config)
    if state.interrupts:  # it was waiting for approval when last closed
        app.answer_pending([Approval(i.id, i.value["action_requests"]) for i in state.interrupts])


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
        asks = [m.text for m in messages if isinstance(m, HumanMessage) and not is_summary(m)]
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
    "/model": model,
    "/theme": theme,
    "/help": help_command,
    "/quit": quit_command,
}
