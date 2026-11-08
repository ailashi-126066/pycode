"""Subagents: the main agent hands a task to another agent that runs in the background.

The main agent has three tools: Agent starts a subagent, SendMessage continues a finished one,
StopAgent stops a running one. Agent returns at once; when the subagent finishes, `notify`
(set by the interface) hands its result to the main session, which wakes the main agent.

Each subagent is its own agent (built by `build_child`, see agent.py) on its own thread
`<session>-<name>`, in the same checkpointer: its history stays out of the main context, and
SendMessage continues that thread. Two modes:
- readonly: read, ls, grep, find, and bash for the allowed commands only (sandbox.py);
- worktree: all built-in tools, working in a git worktree of its own on branch
  pycode/agent/<session>-<name>. Its changes are committed when it finishes, and the main
  agent merges the branch; a worktree without changes is removed.

At most MAX_RUNNING subagents run at once; the others wait for a free place. A subagent's
approvals are asked through `ask` (also set by the interface). The records are kept in the
LangGraph store, so a later run of pycode can show and continue them; subagents still running
when pycode closes end as interrupted.
"""

import asyncio
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from ..context import Context
from ..sandbox import allowed_command
from ..sessions.branches import settle_cancelled
from ..tools.files import ToolError

MAX_RUNNING = 4
RESULT_LIMIT = 6000  # characters of a subagent's final reply passed to the main agent
FINISHED = {"completed", "failed", "stopped", "interrupted"}
STATUS = {
    "queued": "排队中",
    "running": "运行中",
    "waiting": "等待审批",
    "completed": "完成",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "已中断",
}
REJECTED = {"type": "reject", "message": "无人审批：这次调用没有执行"}


@dataclass
class Subagent:
    """One subagent; saved in the store under ("subagents", parent session)."""

    name: str
    parent: str  # thread id of the main session that started it
    description: str
    mode: str  # readonly or worktree
    max_turns: int | None
    status: str = "queued"
    result: str = ""  # its last reply, or why it failed
    worktree: str | None = None  # path of its git worktree
    workdir: str | None = None  # where it works: the worktree, or the main workspace
    branch: str | None = None
    base: str | None = None  # the commit the worktree started from

    @property
    def thread_id(self) -> str:
        return f"{self.parent}-{self.name}"


# ---- git worktrees ----


def git(arguments: list[str], cwd: Path) -> str:
    """git without the repository's hooks or fsmonitor, which could run arbitrary programs."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
    command += ["-c", "user.name=pycode", "-c", "user.email=pycode@localhost"]
    done = subprocess.run(
        [*command, *arguments], cwd=cwd, capture_output=True, text=True, env=environment, timeout=60
    )
    if done.returncode != 0:
        raise ToolError(f"git {arguments[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def create_worktree(agent: Subagent, workspace: Path, root: Path) -> None:
    """A new worktree at the current commit; the subagent works at the same place inside it."""
    try:
        top = Path(git(["rev-parse", "--show-toplevel"], workspace))
    except ToolError as error:
        raise ToolError("worktree mode needs the workspace to be a git repository") from error
    path = root / agent.parent / agent.name
    agent.base = git(["rev-parse", "HEAD"], top)
    agent.branch = f"pycode/agent/{agent.thread_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", agent.branch, str(path), agent.base], top)
    agent.worktree = str(path)
    agent.workdir = str(path / workspace.resolve().relative_to(top))


def finish_worktree(agent: Subagent, workspace: Path) -> None:
    """Commit what the subagent changed; remove the worktree when nothing changed at all."""
    path = Path(agent.worktree)
    if git(["status", "--porcelain"], path):
        git(["add", "-A"], path)
        git(["commit", "-q", "-m", f"subagent {agent.name}: {agent.description}"], path)
    if git(["rev-parse", "HEAD"], path) == agent.base:
        git(["worktree", "remove", "--force", str(path)], workspace)
        git(["branch", "-D", agent.branch], workspace)
        agent.worktree = agent.workdir = agent.branch = None


# ---- what the main agent reads ----


def notification(agent: Subagent) -> str:
    """The message that hands a finished subagent to the main agent."""
    lines = [f"子 agent {agent.name}（{agent.description}）{STATUS[agent.status]}。"]
    result = agent.result[:RESULT_LIMIT]
    if len(agent.result) > RESULT_LIMIT:
        result += f"\n…（还有 {len(agent.result) - RESULT_LIMIT} 个字符）"
    lines.append(f"它的结果：\n{result or '（没有回复）'}")
    if agent.branch:
        lines.append(
            f"改动已提交在分支 {agent.branch}（worktree：{agent.worktree}）。用 git merge "
            f"{agent.branch} 合并，再运行相关测试；有冲突时自己解决，或用 SendMessage 让 "
            f"{agent.name} 修改后再合并。"
        )
    elif agent.mode == "worktree":
        lines.append("它没有改动任何文件，worktree 已删除。")
    if agent.status != "completed":
        lines.append(f"可以用 SendMessage 让 {agent.name} 接着做。")
    return "\n\n".join(lines)


# ---- the manager, which is also the main agent's middleware ----


class Subagents(AgentMiddleware):
    def __init__(self, build_child, store, workspace: Path, worktree_root: Path, max_turns: int):
        super().__init__()
        self.build_child = build_child  # (Subagent) -> (compiled agent, Context)
        self.store = store
        self.workspace = workspace.resolve()
        self.worktree_root = worktree_root
        self.max_turns = max_turns
        self.limit = asyncio.Semaphore(MAX_RUNNING)
        self.running: dict[str, asyncio.Task] = {}  # thread id -> task
        self.closing = False
        # Held from picking a name to saving the record: parallel Agent calls get different names.
        self.naming = asyncio.Lock()
        # Set by the interface. Without them results are only saved, and approvals rejected.
        self.notify = None  # async (Subagent) -> None
        self.ask = None  # async (Subagent, interrupt value) -> resume value
        self.tools = self.make_tools()

    # ---- records ----

    async def save(self, agent: Subagent) -> None:
        await self.store.aput(("subagents", agent.parent), agent.name, asdict(agent))

    async def list(self, parent: str) -> list[Subagent]:
        items = await self.store.asearch(("subagents", parent), limit=100)
        agents = [Subagent(**item.value) for item in items]
        for agent in agents:  # left running by an earlier pycode that closed
            if agent.status not in FINISHED and agent.thread_id not in self.running:
                agent.status = "interrupted"
        return sorted(agents, key=lambda agent: agent.name)

    async def find(self, parent: str, name: str) -> Subagent:
        agent = next((a for a in await self.list(parent) if a.name == name), None)
        if agent is None:
            raise ToolError(f"No subagent named {name}")
        return agent

    # ---- running ----

    def launch(self, agent: Subagent, message: str) -> None:
        task = asyncio.create_task(self.run(agent, message))
        self.running[agent.thread_id] = task
        task.add_done_callback(lambda _: self.running.pop(agent.thread_id, None))

    async def run(self, agent: Subagent, message: str) -> None:
        child, context = self.build_child(agent)
        config = {"configurable": {"thread_id": agent.thread_id}}
        try:
            async with self.limit:  # waits here while MAX_RUNNING others run
                agent.status = "running"
                await self.save(agent)
                if (await child.aget_state(config)).interrupts:
                    # It was interrupted while waiting for approval: answer that first.
                    await self.answer_pending(agent, child, config, context, reject=True)
                await child.ainvoke(
                    {"messages": [{"role": "user", "content": message}]}, config, context=context
                )
                await self.answer_pending(agent, child, config, context)
            messages = (await child.aget_state(config)).values.get("messages", [])
            reply = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
            agent.status, agent.result = "completed", reply.text if reply else ""
        except asyncio.CancelledError:
            agent.status = "interrupted" if self.closing else "stopped"
            await settle_cancelled(child, config)
            await self.end(agent)
            raise
        except Exception as error:  # a failing subagent is its own result, not the main run's
            agent.status, agent.result = "failed", f"{type(error).__name__}: {error}"
        await self.end(agent)

    async def answer_pending(self, agent, child, config, context, *, reject=False) -> None:
        """Ask the user about each pause and resume, until the run ends."""
        while interrupts := (await child.aget_state(config)).interrupts:
            agent.status = "waiting"
            await self.save(agent)
            answers = {}
            for item in interrupts:
                count = len(item.value.get("action_requests", []))
                if reject or self.ask is None:
                    answers[item.id] = {"decisions": [REJECTED] * count}
                else:
                    answers[item.id] = await self.ask(agent, item.value)
            agent.status = "running"
            await self.save(agent)
            await child.ainvoke(Command(resume=answers), config, context=context)

    async def end(self, agent: Subagent) -> None:
        if agent.worktree:
            try:
                await asyncio.to_thread(finish_worktree, agent, self.workspace)
            except ToolError as error:
                agent.result += f"\n\n（整理 worktree 时出错：{error}）"
        await self.save(agent)
        if self.notify is not None and not self.closing:
            await self.notify(agent)

    async def aclose(self) -> None:
        """pycode is closing: stop every subagent; they end as interrupted."""
        self.closing = True
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---- the main agent's tools ----

    def make_tools(self):
        manager = self

        @tool
        async def Agent(  # noqa: N802 - the tool name the model sees
            description: str,
            prompt: str,
            mode: Literal["readonly", "worktree"],
            runtime: ToolRuntime[Context],
            name: str | None = None,
            max_turns: int | None = None,
        ) -> str:
            """Start a subagent in the background with its own context; this returns at once.

            You are told when it finishes, with its final reply; do not wait or poll for it.
            readonly: it may only read and search. worktree: it edits in its own git worktree
            started from the last commit (uncommitted changes are not there); its changes are
            committed on a branch you then merge with git merge. Give subagents that run at
            the same time different files. At most 4 run at once; more wait their turn.

            Args:
                description: What it does, in a few words.
                prompt: The whole task. It knows nothing of this conversation: say what to
                    do, where, and what its final reply must contain.
                mode: readonly or worktree.
                name: Lowercase letters, digits and hyphens; a1, a2, ... when left out.
                max_turns: Fewer model calls than the default, for a small task.
            """
            parent = runtime.config["configurable"]["thread_id"]
            async with manager.naming:
                used = {agent.name for agent in await manager.list(parent)}
                if name is None:
                    name = next(f"a{n}" for n in range(1, 1000) if f"a{n}" not in used)
                elif not re.fullmatch(r"[a-z0-9-]{1,32}", name) or name in used:
                    raise ToolError(f"Name {name} is taken, or not made of a-z, 0-9 and -")
                agent = Subagent(name, parent, description, mode, max_turns)
                agent.workdir = str(manager.workspace)
                await manager.save(agent)
            if mode == "worktree":
                await asyncio.to_thread(
                    create_worktree, agent, manager.workspace, manager.worktree_root
                )
            await manager.save(agent)
            manager.launch(agent, prompt)
            return f"Subagent {name} started in the background. You will be told when it finishes."

        @tool
        async def SendMessage(agent: str, message: str, runtime: ToolRuntime[Context]) -> str:  # noqa: N802
            """Continue a finished subagent in its own history, in the background.

            Args:
                agent: The subagent's name.
                message: What it should do next.
            """
            found = await manager.find(runtime.config["configurable"]["thread_id"], agent)
            if found.status not in FINISHED:
                raise ToolError(f"Subagent {agent} is still {found.status}")
            if found.mode == "worktree" and not found.worktree:
                raise ToolError(f"Subagent {agent} changed nothing and its worktree was removed")
            found.status, found.result = "queued", ""
            await manager.save(found)
            manager.launch(found, message)
            return (
                f"Subagent {agent} continues in the background. You will be told when it finishes."
            )

        @tool
        async def StopAgent(agent: str, runtime: ToolRuntime[Context]) -> str:  # noqa: N802
            """Stop a running subagent; you are told when it has stopped.

            Args:
                agent: The subagent's name.
            """
            found = await manager.find(runtime.config["configurable"]["thread_id"], agent)
            task = manager.running.get(found.thread_id)
            if task is None:
                raise ToolError(f"Subagent {agent} is not running")
            task.cancel()
            return f"Stopping subagent {agent}."

        return [Agent, SendMessage, StopAgent]


class ReadOnlyBash(AgentMiddleware):
    """For readonly subagents: bash runs only the allowed commands (sandbox.py)."""

    def __init__(self, allowed_commands: list[str]):
        super().__init__()
        self.allowed_commands = allowed_commands

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        command = call["args"].get("command", "")
        if call["name"] != "bash" or allowed_command(command, self.allowed_commands):
            return await handler(request)
        return ToolMessage(
            "Error: a readonly subagent's bash only runs: " + ", ".join(self.allowed_commands),
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )
