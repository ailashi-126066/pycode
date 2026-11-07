"""Command line: run tasks, list and open sessions, revert and switch branches."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from ..agent import open_agent
from ..config import Settings
from ..context import Context
from ..sessions import branches
from ..sessions.catalog import (
    delete_session,
    get_session,
    list_sessions,
    open_storage,
    save_session,
)
from ..tools.files import Files


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="pycode")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Project directory")
    parser.add_argument("--providers", type=Path, help="Provider JSON (default: providers.json)")
    parser.add_argument("--provider", help="Provider name to use (default: the first one)")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run a task in a new session, or continue one")
    run.add_argument("prompt")
    run.add_argument("--session", help="Continue this session instead of starting a new one")
    run.add_argument(
        "--max-turns",
        type=int,
        default=100,
        help="Most model calls before the run stops; counting restarts after each approval",
    )
    resume = commands.add_parser("resume", help="Answer a pending approval without a terminal")
    resume.add_argument("session")
    resume.add_argument(
        "--answers", required=True, help='e.g. {"decisions": [{"type": "approve"}]}'
    )
    revert = commands.add_parser("revert", help="Go back to the end of turn N on a new branch")
    revert.add_argument("session")
    revert.add_argument("turn", type=int)

    session = commands.add_parser("session", help="List, open, branch and delete sessions")
    actions = session.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="Sessions of this workspace, newest first")
    for name, text in [
        ("open", "Show the conversation; answer a pending approval"),
        ("branches", "List branches"),
        ("delete", "Delete a session"),
    ]:
        actions.add_parser(name, help=text).add_argument("session")
    switch = actions.add_parser("switch", help="Make branch N (from `branches`) the current one")
    switch.add_argument("session")
    switch.add_argument("branch", type=int)
    return parser.parse_args(argv)


# ---- commands that only touch storage: no provider or API key needed ----


async def list_command(args, settings) -> int:
    async with open_storage(settings.database) as (_, store):
        for session in await list_sessions(store, settings.workspace):
            print(f"{session.id}  {session.updated_at:%Y-%m-%d %H:%M}  {session.title}")
    return 0


async def delete_command(args, settings) -> int:
    async with open_storage(settings.database) as (checkpointer, store):
        await require_session(store, args.session, settings)
        await delete_session(checkpointer, store, args.session)
    print(f"已删除会话 {args.session}")
    return 0


# ---- commands that run or inspect the agent ----
# Each receives the agent, the session store and this session's LangGraph config.


async def run_command(args, settings, agent, store, config) -> int:
    session_id = config["configurable"]["thread_id"]
    title = args.prompt
    if args.session:
        title = (await require_session(store, session_id, settings)).title
        if (await agent.aget_state(config)).interrupts:
            raise ValueError("This session is waiting for approval; use `session open` or `resume`")
    await save_session(store, session_id, settings.workspace, title)
    print(f"会话 {session_id}")
    request = {"messages": [{"role": "user", "content": args.prompt}]}
    return await drive(agent, request, config, settings)


async def resume_command(args, settings, agent, store, config) -> int:
    await require_session(store, args.session, settings)
    state = await agent.aget_state(config)
    if len(state.interrupts) != 1:
        raise ValueError("This session has no pending approval")
    pending = state.interrupts[0]
    answers = json.loads(args.answers)
    if len(answers.get("decisions", [])) != len(pending.value["action_requests"]):
        raise ValueError("--answers needs one decision per pending tool call")
    return await drive(agent, Command(resume={pending.id: answers}), config, settings)


async def revert_command(args, settings, agent, store, config) -> int:
    await require_session(store, args.session, settings)
    await branches.revert(agent, config, args.turn)
    print(f"已回到第 {args.turn} 轮结束时；之后的对话保留在另一个分支")
    return 0


async def open_command(args, settings, agent, store, config) -> int:
    await require_session(store, args.session, settings)
    state = await agent.aget_state(config)
    print_history(state.values.get("messages", []))
    if not state.interrupts:
        return 0
    answers = {item.id: ask_approval(item.value) for item in state.interrupts}
    return await drive(agent, Command(resume=answers), config, settings)


async def branches_command(args, settings, agent, store, config) -> int:
    await require_session(store, args.session, settings)
    head = (await agent.aget_state(config)).config["configurable"]["checkpoint_id"]
    for number, leaf in enumerate(await branches.branches(agent, config), 1):
        humans = [m for m in leaf.values["messages"] if isinstance(m, HumanMessage)]
        mark = "（当前）" if leaf.config["configurable"]["checkpoint_id"] == head else ""
        wait = "（等待审批）" if leaf.next else ""
        print(f"[{number}] {len(humans)} 轮  最后一问：{humans[-1].text[:40]}{mark}{wait}")
    return 0


async def switch_command(args, settings, agent, store, config) -> int:
    await require_session(store, args.session, settings)
    leaves = await branches.branches(agent, config)
    if not 1 <= args.branch <= len(leaves):
        raise ValueError(f"Branch {args.branch} does not exist; see `session branches`")
    await branches.switch(agent, config, leaves[args.branch - 1])
    print(f"已切换到分支 {args.branch}")
    return 0


AGENT_COMMANDS = {
    "run": run_command,
    "resume": resume_command,
    "revert": revert_command,
    "open": open_command,
    "branches": branches_command,
    "switch": switch_command,
}


async def dispatch(args, settings: Settings) -> int:
    name = args.action if args.command == "session" else args.command
    if name == "list":
        return await list_command(args, settings)
    if name == "delete":
        return await delete_command(args, settings)
    async with open_agent(settings) as (agent, store):
        # The session id is the LangGraph thread_id; `run` without --session starts a new one.
        config = {"configurable": {"thread_id": getattr(args, "session", None) or uuid4().hex[:12]}}
        return await AGENT_COMMANDS[name](args, settings, agent, store, config)


async def require_session(store, session_id, settings):
    session = await get_session(store, session_id)
    if session is None:
        raise ValueError(f"No session {session_id}")
    if session.workspace != str(settings.workspace):
        raise ValueError(f"Session {session_id} belongs to {session.workspace}; use --workspace")
    return session


# ---- streaming and approval ----


async def drive(agent, request, config, settings) -> int:
    """Stream the run to the end, asking at the terminal whenever it pauses for approval."""
    context = Context(files=Files(settings.workspace))
    while True:
        interrupts = await stream_until_pause(agent, request, config, context)
        if not interrupts:
            return 0
        if not sys.stdin.isatty():
            # Nobody can answer here: the run waits in storage for `pycode resume`.
            for item in interrupts:
                print(json.dumps({"needs_input": item.value}, ensure_ascii=False, default=str))
            session_id = config["configurable"]["thread_id"]
            print(f"需要审批。回答：pycode resume {session_id} --answers '<JSON>'", file=sys.stderr)
            return 2
        request = Command(resume={item.id: ask_approval(item.value) for item in interrupts})


async def stream_until_pause(agent, request, config, context) -> list:
    """Run until the agent finishes ([]) or pauses for approval (the pending interrupts)."""
    interrupts = []
    async for mode, chunk in agent.astream(
        request, config, context=context, stream_mode=["messages", "updates"]
    ):
        if mode == "messages":
            # Model text arrives token by token.
            token, metadata = chunk
            if metadata.get("langgraph_node") == "model" and token.text:
                print(token.text, end="", flush=True)
            continue
        # mode == "updates": what each graph node produced once it finished.
        for node, update in chunk.items():
            if node == "__interrupt__":
                # Keep reading: the stream must finish so LangGraph saves the paused state.
                interrupts = list(update)
                continue
            if not isinstance(update, dict):
                continue
            for message in update.get("messages", []):
                show(node, message)
    print()
    return interrupts


def print_history(messages) -> None:
    turn = 0
    for message in messages:
        if isinstance(message, HumanMessage):
            turn += 1
            print(f"\n[{turn}] 你：{message.text}")
        elif isinstance(message, AIMessage) and message.text:
            print(f"助手：{message.text}")
        show("model", message)


def show(node: str, message) -> None:
    if isinstance(message, AIMessage) and node == "model":
        # Its text was already streamed; show which tools it asked for.
        for call in message.tool_calls:
            print(f"\n→ {call['name']} {json.dumps(call['args'], ensure_ascii=False)}")
    elif isinstance(message, AIMessage) and node.startswith("ModelCallLimitMiddleware"):
        print(f"\n{message.text}")  # the run hit --max-turns
    elif isinstance(message, ToolMessage):  # from the tools node, or a rejection from approval
        mark = "✗" if message.status == "error" else "←"
        lines = message.text.splitlines() or [""]
        more = f" … (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
        print(f"{mark} {message.name}: {lines[0][:200]}{more}")


def ask_approval(request) -> dict:
    """Answer HumanInTheLoopMiddleware: one decision per pending tool call, in order."""
    decisions = []
    for action in request["action_requests"]:
        print(f"\n需要审批：{action['name']} {json.dumps(action['args'], ensure_ascii=False)}")
        try:
            approved = input("允许执行？[y/N] ").strip().lower() in {"y", "yes"}
        except EOFError:  # no terminal to ask: refuse
            approved = False
        decisions.append(
            {"type": "approve"} if approved else {"type": "reject", "message": "用户拒绝执行"}
        )
    return {"decisions": decisions}


def main(argv=None) -> None:
    args = parse_args(argv)
    workspace = args.workspace.resolve()
    settings = Settings(
        workspace=workspace,
        providers_file=args.providers or workspace / "providers.json",
        provider_name=args.provider,
        max_turns=getattr(args, "max_turns", 100),
    )
    try:
        sys.exit(asyncio.run(dispatch(args, settings)))
    except ValueError as error:  # bad input or configuration (JSONDecodeError is one too)
        print(f"pycode: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
