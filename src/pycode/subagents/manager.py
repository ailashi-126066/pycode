"""Start, run, resume, stop and isolate subagents (11.1 section 10).

Each subagent runs in its own AgentSession, so the framework (create_deep_agent with its
own history and checkpoint) runs the child; this manager owns what Deep Agents' `task`
tool lacks: background runs, SendMessage continuation, StopAgent and worktrees.
"""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from pycode.kernel import DomainError, Event, EventSink, Message, RunResult, ToolError, plain_data
from pycode.subagents.contracts import SubagentSession, SubagentSessions, Worktrees
from pycode.subagents.types import FINISHED, OUTCOME_TRIGGERS, Subagent, SubagentMode, Worktree


class SubagentManager:
    def __init__(
        self,
        sessions: SubagentSessions,
        worktrees: Worktrees,
        events: Callable[[Subagent], EventSink],
        *,
        workspace: str,
        max_agents: int,
        max_turns: int,
        can_write: Callable[[], bool],
    ):
        self._sessions_factory, self._worktrees, self._events = sessions, worktrees, events
        self.workspace, self.max_agents, self.max_turns = workspace, max_agents, max_turns
        self._can_write = can_write
        self.agents: dict[str, Subagent] = {}
        self._sessions: dict[str, SubagentSession] = {}
        self._streams: dict[str, EventSink] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._answers: dict[str, asyncio.Queue] = {}
        # TaskUpdate calls that complete a step, by call id, until their result settles.
        self._commit_steps: dict[str, str] = {}
        # Set by the composition: hands a background subagent's result to the main agent.
        self.notify: Callable[[Message], Awaitable[None]] | None = None

    def require(self, agent_id: str) -> Subagent:
        if agent_id not in self.agents:
            raise ToolError("unknown_agent", "Unknown subagent: " + agent_id)
        return self.agents[agent_id]

    def active(self) -> int:
        return sum(agent.status not in FINISHED for agent in self.agents.values())

    def require_capacity(self) -> None:
        if self.active() >= self.max_agents:
            raise ToolError("agent_limit_reached", "Wait for or stop a subagent first")

    async def start(
        self,
        description: str,
        prompt: str,
        mode: SubagentMode,
        *,
        background: bool,
        max_turns: int | None,
        parent_call_id: str,
        run_id: str,
    ) -> Subagent:
        self.require_capacity()
        if mode == "worktree" and not self._can_write():
            raise ToolError("worktree_unavailable", "This session cannot start a modifying agent")
        agent_id = uuid4().hex[:8]
        worktree = await self._worktrees.create(agent_id) if mode == "worktree" else None
        agent = Subagent(
            agent_id,
            "agent-" + uuid4().hex,
            description,
            mode,
            background,
            max_turns or self.max_turns,
            parent_call_id,
            run_id,
            worktree.path if worktree is not None else self.workspace,
            worktree,
        )
        self.agents[agent_id] = agent
        self._answers[agent_id] = asyncio.Queue()
        self._streams[agent_id] = self._events(agent)
        self._sessions[agent_id] = await self._sessions_factory.open(agent, _Relay(self, agent))
        self._launch(agent, self._sessions[agent_id].run(Message(uuid4().hex, "user", prompt)))
        await self.emit(agent)
        return agent

    async def send(self, agent_id: str, text: str, *, parent_call_id: str, run_id: str):
        """SendMessage: continue a finished or partial subagent in its own history."""
        agent = self.require(agent_id)
        if agent.status not in FINISHED:
            raise ToolError("agent_busy", "The subagent is still running")
        if agent.mode == "worktree" and agent.worktree is None:
            raise ToolError("worktree_removed", "The subagent's unchanged worktree was removed")
        self.require_capacity()
        agent.apply("send")
        agent.parent_call_id, agent.run_id = parent_call_id, run_id
        agent.final_text, agent.error_code = "", None
        self._streams[agent_id] = self._events(agent)
        self._launch(agent, self._sessions[agent_id].run(Message(uuid4().hex, "user", text)))
        await self.emit(agent)
        return agent

    async def wait(self, agent_id: str) -> Subagent:
        """A foreground call waits for the subagent; a background one is never awaited."""
        task = self._tasks[agent_id]
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            # 11.1 section 25: a foreground subagent stops with its cancelled or failed parent.
            await self.stop(agent_id)
            raise
        return self.agents[agent_id]

    async def stop(self, agent_id: str) -> Subagent:
        agent = self.require(agent_id)
        if agent.status in FINISHED:
            raise ToolError("agent_finished", "The subagent has already finished")
        task = self._tasks[agent_id]
        task.cancel()
        await asyncio.wait({task})
        return agent

    async def answer(self, agent_id: str, answer: dict) -> None:
        """The main session answers a forwarded approval."""
        agent = self.require(agent_id)
        if agent.status != "waiting":
            raise ToolError("agent_not_waiting", "The subagent is not waiting for approval")
        await self._answers[agent_id].put(answer)

    def _launch(self, agent: Subagent, run: Awaitable[RunResult]) -> None:
        self._tasks[agent.agent_id] = asyncio.create_task(self._drive(agent, run))

    async def _drive(self, agent: Subagent, run: Awaitable[RunResult]) -> None:
        session = self._sessions[agent.agent_id]
        try:
            result = await run
            # A blocked board ends with needs_input but nothing pending: it fails with its note.
            while result.pending:
                agent.pending = result.pending
                agent.apply("wait")
                await self.emit(agent)
                answer = await self._answers[agent.agent_id].get()
                agent.pending = ()
                agent.apply("resume")
                await self.emit(agent)
                result = await session.resume(answer)
            agent.final_text = result.final_text
            agent.apply(OUTCOME_TRIGGERS.get(result.outcome, "fail"))
            if agent.status == "failed":
                agent.error_code = result.outcome
        except asyncio.CancelledError:
            waiting = agent.status == "waiting"
            agent.pending = ()
            agent.apply("cancel")
            if waiting:
                # The child run is parked on its own approval; end it like a user cancel.
                await session.cancel()
            await self._finish(agent)
            raise
        except Exception as error:
            # A subagent failure is its own result, never the main run's.
            agent.final_text = str(error)
            agent.error_code = error.code if isinstance(error, DomainError) else "agent_error"
            agent.apply("fail")
        await self._finish(agent)

    async def _finish(self, agent: Subagent) -> None:
        worktree = agent.worktree
        # A partial agent may continue, so its worktree stays until it really ends.
        if (
            worktree is not None
            and agent.status != "partial"
            and not await self._worktrees.changed(worktree)
        ):
            await self._worktrees.remove(worktree)
            agent.worktree = None
        if agent.background and self.notify is not None:
            await self.notify(Message(uuid4().hex, "user", notification_text(agent)))
        await self.emit(agent)

    async def emit(self, agent: Subagent, event: Event | None = None) -> None:
        data: dict[str, object] = {"status": agent.status, "description": agent.description}
        if event is not None:
            data["event"] = {"kind": event.kind, "data": event.data}
        await self._streams[agent.agent_id].emit("subagent", data)

    async def forward(self, agent: Subagent, event: Event) -> None:
        """Relay one child event to the main session, wrapped as a subagent event."""
        if agent.worktree is not None:
            await self._commit_step(agent, agent.worktree, event)
        await self.emit(agent, event)

    async def _commit_step(self, agent: Subagent, worktree: Worktree, event: Event) -> None:
        # 11.1 section 9.8: each step completed with TaskUpdate is committed in the worktree.
        data = event.data
        if event.kind == "tool_execution_start" and data["tool_name"] == "TaskUpdate":
            if data["args"]["status"] == "completed" and data["args"]["target"] != "board":
                self._commit_steps[data["tool_call_id"]] = data["args"]["target"]
        elif event.kind == "tool_execution_end" and data["tool_call_id"] in self._commit_steps:
            step = self._commit_steps.pop(data["tool_call_id"])
            if not data["is_error"]:
                await self._worktrees.commit(
                    worktree, f"subagent {agent.agent_id}: complete step {step}"
                )

    def view(self) -> dict:
        agents = [plain_data(agent) for agent in self.agents.values()]
        return {
            "agents": agents,
            "running": self.active(),
            "limit": self.max_agents,
            "approvals": [agent for agent in agents if agent["status"] == "waiting"],
        }

    async def wait_background(self) -> None:
        """Headless runs wait for background subagents before the process exits."""
        if self._tasks:
            await asyncio.wait(set(self._tasks.values()))

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks.values()))
        for session in self._sessions.values():
            await session.aclose()


class _Relay:
    def __init__(self, manager: SubagentManager, agent: Subagent):
        self.manager, self.agent = manager, agent

    async def consume(self, event: Event) -> None:
        await self.manager.forward(self.agent, event)


def worktree_note(agent: Subagent) -> str:
    if agent.worktree is None:
        return ""
    return (
        f"\nChanges are kept in worktree {agent.worktree.path} on branch "
        f"{agent.worktree.branch}; merge them with `git merge {agent.worktree.branch}`. "
        "On conflicts, resolve them and commit; if you cannot, run `git merge --abort` "
        "and tell the user which files conflict."
    )


def result_text(agent: Subagent) -> str:
    """What the main agent sees: only the subagent's final result, never its context."""
    if agent.status == "running":
        return f"Subagent {agent.agent_id} is running in the background; you will be notified."
    lines = [f"Subagent {agent.agent_id} {agent.status}."]
    if agent.status == "partial":
        lines.append("It reached max_turns; continue it with SendMessage.")
    if agent.error_code is not None:
        lines.append("Error: " + agent.error_code)
    return "\n".join(lines) + "\n" + agent.final_text + worktree_note(agent)


def notification_text(agent: Subagent) -> str:
    return f"[subagent {agent.agent_id}: {agent.description}]\n" + result_text(agent)
