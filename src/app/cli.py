"""Command line: `pycode run "task"` streams one conversation and asks before risky tools run."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from ..agent import build_agent
from ..context import Context
from ..models import create_model, load_providers
from ..tools.files import Files


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="pycode")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Project directory")
    parser.add_argument("--providers", type=Path, help="Provider JSON (default: providers.json)")
    parser.add_argument("--provider", help="Provider name to use (default: the first one)")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one task")
    run.add_argument("prompt")
    run.add_argument(
        "--max-turns",
        type=int,
        default=100,
        help="Most model calls before the run stops; counting restarts after each approval",
    )
    return parser.parse_args(argv)


async def run_task(args) -> None:
    workspace = args.workspace.resolve()
    providers = load_providers(args.providers or workspace / "providers.json")
    provider = next((p for p in providers if p.name == args.provider), None)
    if provider is None and args.provider:
        raise ValueError(f"No provider named {args.provider}")
    provider = provider or providers[0]
    agent = build_agent(create_model(provider, workspace), workspace, max_turns=args.max_turns)

    config = {"configurable": {"thread_id": uuid4().hex}}
    context = Context(files=Files(workspace))
    request = {"messages": [{"role": "user", "content": args.prompt}]}
    # Each pass streams until the run ends (None) or pauses for approval (a resume Command).
    while request is not None:
        request = await stream_until_pause(agent, request, config, context)


async def stream_until_pause(agent, request, config, context) -> Command | None:
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
                return Command(resume={item.id: ask_approval(item.value) for item in update})
            if not isinstance(update, dict):
                continue
            for message in update.get("messages", []):
                show(node, message)
    print()
    return None


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
    try:
        asyncio.run(run_task(args))
    except ValueError as error:  # configuration problems: providers file, missing API key
        print(f"pycode: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
