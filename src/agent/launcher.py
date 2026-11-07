"""Own a run's lifetime; app supplies the concrete resource construction function."""

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from ..context import PromptService, SummaryService
from ..kernel import StorageError, ToolSpec
from ..models import ProviderConfig, RetryPolicy, SQLiteRequestStore
from .context import RunContext
from .runtime import AgentRuntime


@dataclass(frozen=True, kw_only=True)
class RunOptions:
    provider: ProviderConfig
    run_id: str
    agent_id: str = "lead"
    purpose: str = "agent"
    retry_policy: RetryPolicy | None = None
    checkpointer: object | None = None
    effective_tools: tuple[ToolSpec, ...] = ()


@dataclass
class RuntimeContainer:
    runtime: AgentRuntime
    context: RunContext
    request_store: SQLiteRequestStore
    closing: AsyncExitStack

    async def aclose(self):
        await self.closing.aclose()


class RunLauncher:
    """Start/resume a run, retain an interrupted run, and close completed resources."""

    def __init__(self, workspace: Path, prompt: PromptService, create_run, run_hook):
        self.workspace = workspace
        self.prompt = prompt
        self.create_run: Callable[[RunOptions], Awaitable[RuntimeContainer]] = create_run
        self.run_hook = run_hook
        self.summary: SummaryService | None = None
        self._active_container: RuntimeContainer | None = None

    async def execute(self, provider, run_id, *, inputs=(), history=(), resume=False, answers=None):
        if not self.workspace.is_dir():
            raise StorageError("workspace_missing", "Execution workspace no longer exists")
        container = self._active_container if resume else None
        restoring = resume and container is None
        if container is None:
            await self.prompt.refresh()
            container = await self.create_run(RunOptions(provider=provider, run_id=run_id))
            self.summary = container.context.summary
            self._active_container = container
        keep = False
        try:
            if restoring and answers is not None:
                await container.runtime.restore_task(container.context)
            if restoring and answers is None:
                result = await container.runtime.continue_task(container.context)
            elif resume:
                result = await container.runtime.resume(container.context, answers)
            else:
                result = await container.runtime.run(inputs, container.context, history=history)
            keep = bool(result.pending)
            return result
        except StorageError as error:
            # Invalid answers leave the existing question available for a corrected answer.
            if resume and error.code in {
                "invalid_resume",
                "invalid_approval",
                "invalid_question_answer",
                "invalid_answers",
                "invalid_plan_answer",
            }:
                keep = True
            raise
        finally:
            if not keep:
                self._active_container = None
                await container.aclose()

    async def cancel_wait(self, run_id):
        container = self._active_container
        if container is not None and container.context.run_id == run_id:
            self._active_container = None
            container.runtime.discard()
            await container.aclose()

    async def compact(self, provider):
        container = await self.create_run(RunOptions(provider=provider, run_id=uuid4().hex))
        self.summary = container.context.summary
        try:
            # Run construction always attaches the session's summary service.
            report = await cast(SummaryService, self.summary).prepare(manual=True)
            if report.applied_job_id is not None:
                await self.run_hook(container.context.scope, "compact")
            return report
        finally:
            await container.aclose()

    async def invalidate_summary(self, reason):
        if self.summary is not None:
            await self.summary.invalidate(reason)

    async def aclose(self):
        async with AsyncExitStack() as stack:
            if self.summary is not None:
                stack.push_async_callback(self.summary.aclose)
                self.summary = None
            if self._active_container is not None:
                stack.push_async_callback(self._active_container.aclose)
                self._active_container = None
