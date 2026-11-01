"""Own Plan files, approval prompt and control tools for one session."""

from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from pycode.kernel import ExecutableTool, HistoryJournal
from pycode.plans.adapters.plans import InterruptPlanPrompt
from pycode.plans.adapters.storage.plans import BackendOperations, FilePlanStore
from pycode.plans.adapters.tools.plans import ExitPlanMode
from pycode.plans.adapters.tools.task_board import TaskUpdate, TaskWrite
from pycode.plans.ports.plans import QuestionPrompt
from pycode.plans.services.plans import PlanService
from pycode.plans.services.projection import PlanProjection


@dataclass(frozen=True)
class PlanSession:
    service: PlanService
    files: FilePlanStore
    tools: tuple[ExecutableTool, ...]


class PlansModule:
    def scoped(self, directory):
        scoped = copy(self)
        scoped.directory = directory
        return scoped

    def __init__(
        self, directory: Path, *, backend_factory: Callable[..., BackendOperations]
    ) -> None:
        self.directory = directory
        self.backend_factory = backend_factory

    def files(self, session_id: str) -> FilePlanStore:
        return FilePlanStore(self.directory / session_id, backend_factory=self.backend_factory)

    @staticmethod
    def prompt() -> QuestionPrompt:
        return InterruptPlanPrompt()

    @staticmethod
    def exit_tool(service: PlanService) -> ExecutableTool:
        return ExitPlanMode(service)

    @staticmethod
    def projection() -> PlanProjection:
        return PlanProjection()

    @staticmethod
    def exit_spec():
        return ExitPlanMode.spec

    @staticmethod
    def task_tools(history: HistoryJournal) -> tuple[ExecutableTool, ...]:
        return (TaskWrite(history), TaskUpdate(history))

    @staticmethod
    def task_specs():
        return (TaskWrite.spec, TaskUpdate.spec)

    def session(
        self, session_id: str, history: HistoryJournal, *, prompt: QuestionPrompt | None = None
    ) -> PlanSession:
        files = self.files(session_id)
        service = PlanService(history, files, prompt or self.prompt())
        return PlanSession(service, files, (self.exit_tool(service),))
