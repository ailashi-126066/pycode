# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from pycode.context import ContextProjector
from pycode.kernel import (
    EntryDraft,
    Message,
    RunResult,
    SessionEntry,
    SessionTree,
    StorageError,
    plain_data,
)
from pycode.models import ModelControl, ProviderConfig
from pycode.session.inputs import DurableInputs, InputKind
from pycode.session.restart import pending_run_entry, recover_conversation
from pycode.session.sessions import name_session
from pycode.session.store import SqliteSessionStore
from pycode.session.view import SessionView

if TYPE_CHECKING:
    from pycode.memory import MemoryService
    from pycode.plans import PlanService
    from pycode.skills import Skills
    from pycode.snapshot import SnapshotService
    from pycode.subagents import SubagentManager
    from pycode.tasks import TaskBoardService


class AgentSession:
    def __init__(
        self,
        store: SqliteSessionStore,
        projector: ContextProjector,
        models: ModelControl,
        launcher,
        default: ProviderConfig,
        durable_inputs: DurableInputs,
        view: SessionView,
        storage,
        *,
        closing: AsyncExitStack,
        skills: Skills,
        plans: PlanService,
        memory: MemoryService,
        tasks: TaskBoardService,
        snapshots: SnapshotService,
        subagents: SubagentManager | None = None,
    ) -> None:
        self.history = store
        self._projector = projector
        self.models = models
        self.launcher = launcher
        self._default = default
        self._active_task: asyncio.Task | None = None
        self._durable_inputs = durable_inputs
        self.view = view
        self.skills = skills
        self.plans = plans
        self.storage = storage
        self.memory = memory
        self.subagents = subagents
        # Results of background subagents that finished while no run could take them.
        self._notifications: list[Message] = []
        self._closing = False
        self._cleanup = closing
        self.tasks = tasks
        self.snapshots = snapshots

    async def submit_input(self, message: Message, kind: InputKind) -> bool:
        snapshot = await self.history.snapshot()
        start = self.pending_run(snapshot)
        if start is None:
            # Retry ownership comes from the durable entry; identity checks live only in Store.
            start = next(
                (
                    entry
                    for entry in snapshot.entries
                    if entry.type == "input_queued" and entry.require_message().id == message.id
                ),
                None,
            )
        if start is None:
            raise StorageError("no_pending_run", "Start a task before queueing input")
        return await self._durable_inputs.submit(message, kind, self.require_run_id(start))

    @asynccontextmanager
    async def execution(self):
        if self._closing:
            raise StorageError("session_closed", "This session is closing or closed")
        if self._active_task is not None:
            raise StorageError("session_busy", "This session already has an active executor")
        # No await between checking and claiming ownership. Input admission uses Store's lock.
        self._active_task = asyncio.current_task()
        try:
            yield
        finally:
            self._active_task = None

    async def cancel(self) -> bool:
        running = self._active_task
        if running is None:
            async with self.execution():
                start = self.pending_run(await self.history.snapshot())
                if start is None:
                    return False
                await self.launcher.cancel_wait(self.require_run_id(start))
                await recover_conversation(self.history, reason="aborted")
                await self.plans.finish(self.require_run_id(start))
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
            if self.pending_run(await self.history.snapshot()) is not None:
                raise StorageError(
                    "session_needs_input", "Resolve the pending run before switching models"
                )
            return await self.models.select(provider, model_id)

    async def model_list(self, *, refresh=False):
        return await self.models.list_models(refresh=refresh)

    async def name(self, title):
        async with self.execution():
            return await name_session(self.history, title)

    async def branch(self, leaf_id: str | None) -> None:
        async with self.execution():
            if self.pending_run(await self.history.snapshot()) is not None:
                raise StorageError(
                    "session_needs_input", "Resolve the pending run before changing branches"
                )
            await self.history.branch(leaf_id)
            await self.plans.initialize()

    async def run(self, message: Message) -> RunResult:
        if message.role != "user" or not message.id:
            raise ValueError("A task starts with an identified user input")
        async with self.execution():
            return await self._run_locked(message)

    async def _run_locked(self, message):
        before = await self.history.snapshot()
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
            # Store is the single identity/branch validation boundary.
            await self.history.append_message(message)
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
                provider = await self.models.restore(self._default)
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
            await self.history.append_message(message)
            if await self.models.current() is None:
                await self.models.select(self._default.name, self._default.model_id)
        provider = await self.models.restore(self._default)
        start = await self.history.append_entry(
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
            result = await self.launcher.run(
                provider, self.history, self.require_run_id(start), (message,), history
            )
        except asyncio.CancelledError:
            await recover_conversation(self.history, reason="aborted")
            raise
        except Exception:
            # Settles a failure raised outside the runtime's own finish (G5).
            await recover_conversation(self.history, reason="error")
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
            start = self.pending_run(await self.history.snapshot())
            if start is None:
                raise StorageError("no_pending_run", "This branch has no pending execution")
            provider = await self.models.restore(self._default)
            return await self.resume_run(provider, start, answers)

    async def resume_run(self, provider, start, answers=None):
        # Take the durable writer lease before observing or resuming graph execution.
        await self.history.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "run_resume", "input_id": start.data["input_id"]},
                run_id=self.require_run_id(start),
            )
        )
        try:
            result = await self.launcher.resume(
                provider, self.history, self.require_run_id(start), answers
            )
        except asyncio.CancelledError:
            await recover_conversation(self.history, reason="aborted")
            raise
        return await self.settle_run(start, result)

    async def settle_run(self, start, result):
        return await self.record_run_result(start, result)

    async def record_run_result(self, start, result):
        result = replace(result, run_id=self.require_run_id(start))
        if result.pending:
            await self.history.append_entry(
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
        await self.plans.finish(self.require_run_id(start))
        return result

    async def aclose(self) -> None:
        if not self._closing and self._active_task is not None:
            raise StorageError("session_busy", "Cancel and collect the active run before closing")
        self._closing = True
        await self._cleanup.aclose()

    async def inspect(self, *, presentation=False):
        state = await self.view.inspect(presentation=presentation)
        state["plan"] = plain_data(self.plans.state) if self.plans.state is not None else None
        state["subagents"] = self.subagents.view() if self.subagents is not None else None
        return state

    async def notify_subagent(self, message: Message) -> None:
        """A background subagent finished: queue it as follow_up input, or wake the session."""
        start = self.pending_run(await self.history.snapshot())
        if start is not None:
            try:
                await self._durable_inputs.submit(message, "follow_up", self.require_run_id(start))
                return
            except StorageError as error:
                # The run is ending; the result starts the next run instead.
                if error.code != "input_admission_closed":
                    raise
        self._notifications.append(message)

    def wake_pending(self) -> bool:
        return bool(self._notifications)

    async def wait_subagents(self) -> None:
        if self.subagents is not None:
            await self.subagents.wait_background()

    async def wake(self):
        """Start a run with the results of background subagents that finished while idle."""
        if not self._notifications or self._active_task is not None:
            return None
        async with self.execution():
            if self.pending_run(await self.history.snapshot()) is not None:
                return None
            messages, self._notifications = self._notifications, []
            text = "\n\n".join(message.text for message in messages)
            return await self._run_locked(Message(uuid4().hex, "user", text))

    async def publish_result(self, result, *, collect_background=False):
        if result.run_id is None:
            raise StorageError(
                "result_unavailable", "This result has no durable execution identity"
            )
        if collect_background:
            await self.launcher.aclose()
        return await self.view.publish_result(result)

    async def compact(self):
        async with self.execution():
            self.require_settled(await self.history.snapshot())
            provider = await self.models.restore(self._default)
            await self.history.append_entry(EntryDraft("custom", data={"kind": "compact_request"}))
            return await self.launcher.compact(provider, self.history)

    def require_settled(self, snapshot):
        if self.pending_run(snapshot) is not None:
            raise StorageError(
                "session_needs_input", "Resolve the pending run before changing conversation state"
            )
