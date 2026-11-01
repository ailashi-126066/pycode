# Source: https://github.com/earendil-works/pi/blob/42a3497d03ad17e308a2299fa824727894f2c0ec/packages/coding-agent/src/core/agent-session.ts
# Adaptation: explicit storage/projector/model/runtime dependencies and linear task settlement.
"""Application commands share the same canonical history and execution owner."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TypeVar
from uuid import uuid4

from pycode.context import ContextProjector, task_board
from pycode.kernel import (
    EntryDraft,
    Message,
    RunResult,
    SessionEntry,
    SessionTree,
    StorageError,
    plain_data,
    work_graph,
)
from pycode.models import ModelControl, ProviderConfig
from pycode.session.domain.execution_recovery import pending_run_entry
from pycode.session.domain.inputs import InputKind
from pycode.session.domain.revert_state import pending_revert
from pycode.session.ports.history import SessionStore
from pycode.session.ports.runtime import RuntimeLauncher
from pycode.session.services.durable_inputs import DurableInputs
from pycode.session.services.ownership import SessionCleanup
from pycode.session.services.restart import recover_conversation
from pycode.session.services.revert import RevertService
from pycode.session.services.session_view import SessionView
from pycode.session.services.sessions import name_session
from pycode.skills import Skills
from pycode.subagents import SubagentManager

T = TypeVar("T")


def require(service: T | None, name: str) -> T:
    """The UI only offers a command when its module is installed."""
    if service is None:
        raise StorageError(f"{name}_unavailable", f"This session has no {name} service")
    return service


class AgentSession:
    def __init__(
        self,
        store: SessionStore,
        projector: ContextProjector,
        models: ModelControl,
        launcher: RuntimeLauncher,
        default: ProviderConfig,
        durable_inputs: DurableInputs | None = None,
        revert: RevertService | None = None,
        view: SessionView | None = None,
        skills: Skills | None = None,
        plans=None,
        storage=None,
        memory=None,
        subagents: SubagentManager | None = None,
    ) -> None:
        self._store = store
        self._projector = projector
        self._models = models
        self._launcher = launcher
        self._default = default
        self._execution = asyncio.Lock()
        self._active_task: asyncio.Task | None = None
        self._durable_inputs = durable_inputs
        self._submission = asyncio.Lock()
        self._revert = revert
        self._view = view
        self._skills = skills
        self._plans = plans
        self._storage = storage
        self._memory = memory
        self._subagents = subagents
        # Results of background subagents that finished while no run could take them.
        self._notifications: list[Message] = []
        self._closing = False
        self._cleanup = SessionCleanup()

    async def submit_input(self, message: Message, kind: InputKind) -> bool:
        async with self._submission:
            snapshot = await self._store.snapshot()
            if pending_revert(snapshot) is not None:
                raise StorageError(
                    "revert_recovery_required", "Recover the pending revert before queueing input"
                )
            start = self.pending_run(snapshot)
            if start is None:
                for entry in snapshot.entries:
                    if entry.type == "input_queued" and entry.require_message().id == message.id:
                        return await require(self._durable_inputs, "queue").submit(
                            message, kind, self.require_run_id(entry)
                        )
                raise StorageError("no_pending_run", "Start a task before queueing input")
            return await require(self._durable_inputs, "queue").submit(
                message, kind, self.require_run_id(start)
            )

    @asynccontextmanager
    async def execution(self, *, allow_revert_recovery=False):
        if self._closing:
            raise StorageError("session_closed", "This session is closing or closed")
        if self._execution.locked():
            raise StorageError("session_busy", "This session already has an active executor")
        async with self._execution:
            self._active_task = asyncio.current_task()
            try:
                if (
                    not allow_revert_recovery
                    and pending_revert(await self._store.snapshot()) is not None
                ):
                    raise StorageError(
                        "revert_recovery_required",
                        "Recover the pending revert before executing commands",
                    )
                yield
            finally:
                self._active_task = None

    async def cancel(self) -> bool:
        running = self._active_task
        if running is None:
            async with self.execution():
                start = self.pending_run(await self._store.snapshot())
                if start is None:
                    return False
                await self._launcher.cancel_wait(self.require_run_id(start))
                await recover_conversation(self._store, reason="aborted")
                if self._plans is not None:
                    await self._plans.finish(self.require_run_id(start))
                return True
        if running is asyncio.current_task():
            raise StorageError("invalid_cancel_owner", "Cancellation must come from another task")
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        return True

    async def select_model(self, provider: str, model_id: str):
        async with self.execution():
            if self.pending_run(await self._store.snapshot()) is not None:
                raise StorageError(
                    "session_needs_input", "Resolve the pending run before switching models"
                )
            return await self._models.select(provider, model_id)

    async def model_list(self, *, refresh=False):
        return await self._models.list_models(refresh=refresh)

    async def name(self, title):
        async with self.execution():
            return await name_session(self._store, title)

    async def initialize_memory(self):
        if self._memory is not None:
            await self._memory.initialize()

    async def load_skill(self, name, args=""):
        selected = await require(self._skills, "skills").load(name, args)
        prompt = (
            f"Use the {selected['name']} Skill. Read {selected['path']} for its instructions.\n"
            f"Task arguments: {selected['arguments']}"
        )
        return await self.run(
            Message(uuid4().hex, "user", prompt, provider_metadata={"skill_request": selected})
        )

    async def reload_skills(self):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            return await require(self._skills, "skills").refresh()

    async def skill_search(self, query="", limit=10):
        return await require(self._skills, "skills").search(query, limit)

    async def install_skill(self, url, ref=None, subpath=None):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            return await require(self._skills, "skills").install(url, ref=ref, subpath=subpath)

    async def read_skill(self, name):
        return await require(self._skills, "skills").read(name)

    async def save_skill(self, name, content, *, scope="user", expected_revision=None):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            skills = require(self._skills, "skills")
            if expected_revision is None:
                return await skills.create(name, content, scope=scope)
            return await skills.update(
                name, content, scope=scope, expected_revision=expected_revision
            )

    async def branch(self, leaf_id: str | None) -> None:
        async with self.execution():
            if self.pending_run(await self._store.snapshot()) is not None:
                raise StorageError(
                    "session_needs_input", "Resolve the pending run before changing branches"
                )
            await self._store.branch(leaf_id)
            if self._plans is not None:
                await self._plans.initialize()

    async def enter_plan(self, task=""):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            state = await require(self._plans, "plan").enter()
            if not task:
                await self._store.append_message(Message(uuid4().hex, "user", "/plan"))
                return state
        return await self.run(Message(uuid4().hex, "user", "Create a Plan for: " + task))

    async def storage_status(self):
        return await require(self._storage, "storage").status()

    async def storage_gc(self, *, dry_run=False):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            return await require(self._storage, "storage").collect(dry_run=dry_run)

    async def run(self, message: Message) -> RunResult:
        if message.role != "user" or not message.id:
            raise ValueError("A task starts with an identified user input")
        async with self.execution():
            return await self._run_locked(message)

    async def _run_locked(self, message):
        before = await self._store.snapshot()
        path = SessionTree(before.index).path(before.leaf_id)
        pending = self.pending_run(before)
        original = next(
            (
                entry
                for entry in before.entries
                if entry.type == "message" and entry.require_message().id == message.id
            ),
            None,
        )
        if original is not None:
            if original.message != message:
                raise StorageError("input_id_conflict", "Input ID already has different content")
            if original.id not in {entry.id for entry in path}:
                raise StorageError("input_branch_conflict", "Input ID belongs to another branch")
            for entry in reversed(path):
                if (
                    entry.type == "custom"
                    and entry.data.get("kind") == "run_end"
                    and (
                        entry.data.get("input_id") == message.id
                        or message.id in entry.data.get("input_ids", ())
                    )
                ):
                    final_id = entry.data.get("final_entry_id")
                    final_text = (
                        SessionTree(before.index).get(final_id).require_message().text
                        if final_id
                        else ""
                    )
                    return RunResult(
                        entry.data["reason"],
                        final_text,
                        (),
                        checkpoint_id=entry.data.get("checkpoint_id"),
                        run_id=entry.run_id,
                    )
            following = path[path.index(original) + 1 :]
            if pending is not None and pending.data["input_id"] == message.id:
                provider = await self._models.restore(self._default)
                return await self.resume_run(provider, pending)
            if any(
                entry.type == "message"
                and entry.require_message().role in {"assistant", "tool_result"}
                for entry in following
            ):
                raise StorageError(
                    "execution_recovery_required",
                    "Unsettled execution must be reconciled before continuing",
                )
            history = tuple(
                item
                for item in self._projector.transform_context(before).messages
                if item.id != message.id
            )
        else:
            if pending is not None:
                raise StorageError(
                    "session_needs_input",
                    "Resume or resolve the pending task before adding input",
                )
            history = self._projector.transform_context(before).messages
            await self._store.append_message(message)
            if await self._models.current() is None:
                await self._models.select(self._default.name, self._default.model_id)
        if self._skills is not None:
            await self._skills.index()
        provider = await self._models.restore(self._default)
        start = await self._store.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "run_start",
                    "input_id": message.id,
                    "execution_session_id": before.header.session_id,
                },
                run_id=uuid4().hex,
            )
        )
        try:
            result = await self._launcher.run(
                provider, self._store, self.require_run_id(start), (message,), history
            )
        except asyncio.CancelledError:
            await recover_conversation(self._store, reason="aborted")
            raise
        except Exception:
            # Settles a failure raised outside the runtime's own finish (G5).
            await recover_conversation(self._store, reason="error")
            raise
        return await self.settle_run(start, result)

    @staticmethod
    def require_run_id(entry: SessionEntry) -> str:
        if entry.run_id is None:
            raise StorageError("invalid_run_entry", "Execution entry requires a run ID")
        return entry.run_id

    def pending_run(self, snapshot):
        return pending_run_entry(snapshot)

    async def resume(self, answers: dict | None = None) -> RunResult:
        async with self.execution():
            start = self.pending_run(await self._store.snapshot())
            if start is None:
                raise StorageError("no_pending_run", "This branch has no pending execution")
            provider = await self._models.restore(self._default)
            return await self.resume_run(provider, start, answers)

    async def resume_run(self, provider, start, answers=None):
        # Take the durable writer lease before observing or resuming graph execution.
        await self._store.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "run_resume", "input_id": start.data["input_id"]},
                run_id=self.require_run_id(start),
            )
        )
        try:
            result = await self._launcher.resume(
                provider, self._store, self.require_run_id(start), answers
            )
        except asyncio.CancelledError:
            await recover_conversation(self._store, reason="aborted")
            raise
        return await self.settle_run(start, result)

    async def settle_run(self, start, result):
        async with self._submission:
            return await self.record_run_result(start, result)

    async def record_run_result(self, start, result):
        result = replace(result, run_id=self.require_run_id(start))
        if result.pending:
            await self._store.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": "run_wait",
                        "pending": list(result.pending),
                        "checkpoint_id": result.checkpoint_id,
                    },
                    run_id=self.require_run_id(start),
                )
            )
            return result
        # The runtime already wrote run_end when the run entered finished.
        if self._plans is not None:
            await self._plans.finish(self.require_run_id(start))
        return result

    async def aclose(self) -> None:
        if not self._closing and self._execution.locked():
            raise StorageError("session_busy", "Cancel and collect the active run before closing")
        self._closing = True
        closers = []
        memory, self._memory = self._memory, None
        if memory is not None:
            closers.append(memory.aclose)
        subagents, self._subagents = self._subagents, None
        if subagents is not None:
            closers.append(subagents.aclose)
        closers.append(self._launcher.aclose)
        view, self._view = self._view, None
        if view is not None:
            closers.append(view.aclose)
        closers.append(self._store.aclose)
        await self._cleanup.close(tuple(closers))

    async def inspect(self, *, presentation=False):
        state = await require(self._view, "view").inspect(presentation=presentation)
        state["plan"] = (
            plain_data(self._plans.state)
            if self._plans is not None and self._plans.state is not None
            else None
        )
        state["subagents"] = self._subagents.view() if self._subagents is not None else None
        return state

    async def task_board(self):
        """/tasks: the current branch's board, or None."""
        snapshot = await self._store.snapshot()
        return task_board(SessionTree(snapshot.index).path(snapshot.leaf_id))

    async def clear_task_board(self):
        """/tasks clear: abandon the open board; the branch keeps the snapshot as a record."""
        async with self.execution():
            snapshot = await self._store.snapshot()
            self.require_settled(snapshot)
            board = task_board(SessionTree(snapshot.index).path(snapshot.leaf_id))
            if board is None:
                raise StorageError("no_task_board", "This branch has no task board")
            abandoned = work_graph.apply(board, "board", "abandon")
            await self._store.append_entry(
                EntryDraft(
                    "custom", data={"kind": "task_board", "task_board": plain_data(abandoned)}
                )
            )

    def require_subagents(self) -> SubagentManager:
        return require(self._subagents, "subagents")

    async def answer_agent(self, agent_id, answer):
        await self.require_subagents().answer(agent_id, answer)

    async def cancel_agent(self, agent_id):
        return await self.require_subagents().stop(agent_id)

    async def notify_subagent(self, message: Message) -> None:
        """A background subagent finished: queue it as follow_up input, or wake the session."""
        async with self._submission:
            start = self.pending_run(await self._store.snapshot())
            if start is not None and self._durable_inputs is not None:
                try:
                    await self._durable_inputs.submit(
                        message, "follow_up", self.require_run_id(start)
                    )
                    return
                except StorageError as error:
                    # The run is ending; the result starts the next run instead.
                    if error.code != "input_admission_closed":
                        raise
            self._notifications.append(message)

    def wake_pending(self) -> bool:
        return bool(self._notifications)

    async def wait_subagents(self) -> None:
        if self._subagents is not None:
            await self._subagents.wait_background()

    async def wake(self):
        """Start a run with the results of background subagents that finished while idle."""
        if not self._notifications or self._execution.locked():
            return None
        async with self.execution():
            if self.pending_run(await self._store.snapshot()) is not None:
                return None
            messages, self._notifications = self._notifications, []
            text = "\n\n".join(message.text for message in messages)
            return await self._run_locked(Message(uuid4().hex, "user", text))

    async def usage(self, run_id=None):
        return await require(self._view, "view").usage(run_id)

    async def request_records(self):
        return await require(self._view, "view").request_records()

    async def memory_list(self):
        return await require(self._memory, "memory").list()

    async def collect_memory(self):
        if self._memory is not None:
            await self._memory.collect()

    async def memory_mode(self, mode):
        await require(self._memory, "memory").set_mode(mode)

    async def memory_remove(self, memory_id, *, scope=None):
        async with self.execution():
            await require(self._memory, "memory").remove(memory_id, scope=scope)

    async def memory_edit(self, memory_id, content, *, scope=None):
        async with self.execution():
            return await require(self._memory, "memory").edit(memory_id, content, scope=scope)

    async def memory_clear(self):
        async with self.execution():
            await require(self._memory, "memory").clear()

    async def memory_extract(self):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            if self._memory is None or self._memory.updater is None:
                raise StorageError("memory_unavailable", "No memory updater is configured")
            await self._memory.consolidate("manual-memory-" + uuid4().hex)
            return {"extracted": True}

    async def publish_result(self, result, *, collect_background=False):
        if self._view is None or result.run_id is None:
            raise StorageError(
                "result_unavailable", "This result has no durable execution identity"
            )
        if collect_background:
            await self._launcher.aclose()
        return await self._view.publish_result(result)

    async def compact(self):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            provider = await self._models.restore(self._default)
            await self._store.append_entry(EntryDraft("custom", data={"kind": "compact_request"}))
            return await self._launcher.compact(provider, self._store)

    async def preview_revert(self, target_leaf_id, mode="dialogue"):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            return await require(self._revert, "revert").preview(target_leaf_id, mode)

    async def revert(self, plan):
        async with self.execution():
            self.require_settled(await self._store.snapshot())
            result = await require(self._revert, "revert").apply(plan)
            await self._launcher.invalidate_summary("reverted")
            if self._plans is not None:
                await self._plans.initialize()
            return result

    def require_settled(self, snapshot):
        if self.pending_run(snapshot) is not None:
            raise StorageError(
                "session_needs_input", "Resolve the pending run before changing conversation state"
            )

    async def recover_revert(self, restore_id, *, compensate=False):
        async with self.execution(allow_revert_recovery=True):
            result = await require(self._revert, "revert").recover(
                restore_id, compensate=compensate
            )
            await self._launcher.invalidate_summary("reverted")
            if self._plans is not None:
                await self._plans.initialize()
            return result
