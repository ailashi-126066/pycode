"""Own active runtime, hook and summary lifetimes through explicit factories."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

from pycode.app.configuration.settings import AppConfig
from pycode.context import ContextProjector, SummaryService
from pycode.hooks import HookContext
from pycode.kernel import (
    EventConsumer,
    EventJournal,
    Message,
    SessionTree,
    StorageError,
    ToolSpec,
    plain_data,
)
from pycode.kernel import ExecutableTool as Tool
from pycode.models import ProviderConfig
from pycode.session import SessionStore


class ConfiguredRuntimeLauncher:
    def __init__(
        self,
        config: AppConfig,
        tools: tuple[Tool, ...],
        consumers: tuple[EventConsumer, ...],
        event_journal: EventJournal,
        configured_hooks=(),
        resource_owners=(),
        projector_factory: Callable[[], ContextProjector] = ContextProjector,
        plans=None,
        plan_backend=None,
        durable_inputs=None,
        runtime_factory=None,
        model_resolver=None,
        hook_factory=None,
        prompt_factory=None,
        lifecycle_provider=None,
        memory=None,
        subagents=None,
    ) -> None:
        self.config = config
        self.model_resolver = model_resolver
        self._runtime_factory = runtime_factory
        self._hook_factory = hook_factory
        self._prompt_factory = prompt_factory
        self.lifecycle_provider = lifecycle_provider
        self.memory = memory
        self.subagents = subagents
        self.tools = tools
        self.consumers = consumers
        self.summary: SummaryService | None = None
        self.event_journal = event_journal
        self.configured_hooks = configured_hooks
        self.resource_owners = tuple(resource_owners)
        self.projector_factory = projector_factory
        self.plans = plans
        self.plan_backend = plan_backend
        self.durable_inputs = durable_inputs
        self._lifecycle_engine = None
        self._lifecycle_context = None
        self._active_container = None
        self._close_task: asyncio.Task | None = None

    async def startup(self, store):
        if not self.configured_hooks:
            return
        snapshot = await store.snapshot()
        lifetime = uuid4().hex
        assert self._hook_factory is not None
        engine = self._hook_factory(self, store, snapshot, lifetime)
        self._lifecycle_engine = engine
        self._lifecycle_context = HookContext(
            snapshot.header.session_id, lifetime, str(self.config.workspace), "startup"
        )
        await engine.fire(self._lifecycle_context)

    def abort_startup(self):
        self._lifecycle_engine, self._lifecycle_context = None, None

    async def run(
        self,
        provider: ProviderConfig,
        store: SessionStore,
        inputs: tuple[Message, ...],
        history: tuple[Message, ...],
    ):
        snapshot = await store.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        start = next(
            (
                entry
                for entry in reversed(path)
                if entry.type == "custom"
                and entry.data.get("kind") == "run_start"
                and entry.data.get("input_id") == inputs[0].id
            ),
            None,
        )
        run_id = start.run_id if start is not None else uuid4().hex
        return await self.execute(provider, store, run_id, inputs=inputs, history=history)

    async def resume(self, provider, store, run_id, answers=None):
        return await self.execute(provider, store, run_id, resume=True, answers=answers)

    async def cancel_wait(self, run_id):
        container = self._active_container
        if container is None or container.context.run_id != run_id:
            return
        self._active_container = None
        try:
            if container.context.summary is not None:
                await container.context.summary.cancel_background()
        finally:
            container.runtime.discard()
            await container.aclose()

    async def execute(
        self, provider, store, run_id, *, inputs=(), history=(), resume=False, answers=None
    ):
        if not self.config.workspace.is_dir():
            raise StorageError("workspace_missing", "Execution workspace no longer exists")
        container = self._active_container if resume else None
        restoring = resume and container is None
        if container is None:
            assert self._prompt_factory is not None and self._runtime_factory is not None
            await self._prompt_factory(store).refresh()
            container = await self._runtime_factory(self, provider, store, run_id)
            self._active_container = container
            self.summary = container.context.summary
            container.owns_summary = False
            if resume and answers is not None:
                await container.runtime.restore_task(container.context)
        keep_waiting = False
        try:
            if restoring and answers is None:
                result = await container.runtime.continue_task(container.context)
            elif resume:
                result = await container.runtime.resume(container.context, answers)
            else:
                result = await container.runtime.run(inputs, container.context, history=history)
            keep_waiting = bool(result.pending)
            return result
        except StorageError as error:
            if resume and error.code in {
                "invalid_resume",
                "invalid_approval",
                "invalid_question_answer",
                "invalid_answers",
                "invalid_plan_answer",
            }:
                keep_waiting = True
            raise
        finally:
            if not keep_waiting:
                self._active_container = None
                await container.aclose()

    async def compact(self, provider, store):
        snapshot = await store.snapshot()
        assert self._runtime_factory is not None
        container = await self._runtime_factory(self, provider, store, uuid4().hex)
        self.summary = container.context.summary
        container.owns_summary = False
        try:
            if self.summary is None:
                raise StorageError("summary_unavailable", "This runtime has no summary service")
            await container.prepare_tools()
            self.summary.set_tools(container.context.registry.model_specs())
            current = self.projector_factory().transform_context(await store.snapshot())
            effective = []
            for declaration in current.checkpoint.get("model_tools", ()):
                effective.append(ToolSpec(**plain_data(declaration)))
            if effective:
                self.summary.set_tools(tuple(effective))
            report = await self.summary.prepare(manual=True)
            if container.context.hooks is not None and report.applied_job_id is not None:
                await container.context.hooks.fire(
                    HookContext(
                        snapshot.header.session_id,
                        container.context.run_id,
                        str(self.config.workspace),
                        "compact",
                    )
                )
            return report
        finally:
            await container.aclose()

    async def aclose(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_all())
        interrupted = None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as error:
                interrupted = error
            except BaseException:
                break
        try:
            self._close_task.result()
        finally:
            if interrupted is not None:
                raise interrupted

    async def _close_all(self):
        active, self._active_container = self._active_container, None
        summary, self.summary = self.summary, None
        owners, self.resource_owners = self.resource_owners, ()
        lifecycle, self._lifecycle_engine = self._lifecycle_engine, None
        lifecycle_context, self._lifecycle_context = self._lifecycle_context, None
        errors = []

        async def attempt(operation):
            try:
                await operation
            except BaseException as error:
                errors.append(error)

        if active is not None:
            await attempt(active.aclose())
        if lifecycle is not None and lifecycle_context is not None:
            await attempt(lifecycle.fire(replace(lifecycle_context, event_name="shutdown")))
        if summary is not None:
            await attempt(summary.aclose())
        for owner in reversed(owners):
            await attempt(owner.aclose())
        if errors:
            raise errors[0]

    async def invalidate_summary(self, reason):
        if self.summary is not None:
            await self.summary.invalidate(reason)
