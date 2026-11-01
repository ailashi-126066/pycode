"""Consumer-owned projection and execution attribution capabilities."""

from typing import Protocol

from pycode.kernel import (
    HistoryJournal,
    ModelSelection,
    ProjectedContext,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    ToolSpec,
)


class Projector(Protocol):
    def transform_context(self, snapshot: SessionSnapshot) -> ProjectedContext: ...


class RunOwnership(Protocol):
    def owns(self, header: SessionHeader, entry: SessionEntry) -> bool: ...


class RegistryLookup(Protocol):
    def get_spec(self, name: str) -> ToolSpec: ...


class CalibrationReport(Protocol):
    @property
    def applied_job_id(self) -> str | None: ...


class SummaryBudget(Protocol):
    def set_tools(self, tools: tuple[ToolSpec, ...]) -> None: ...
    async def prepare(self) -> CalibrationReport: ...


class CalibrationContext(Protocol):
    """Native footprint state used by calibration, independent of AgentRuntime."""

    model_tools: tuple[ToolSpec, ...]
    disclosed_tool_names: tuple[str, ...] | None
    system_hash: str
    model_source_leaf_id: str | None

    @property
    def run_id(self) -> str: ...

    @property
    def registry(self) -> RegistryLookup: ...

    @property
    def history_store(self) -> HistoryJournal | None: ...

    @property
    def summary(self) -> SummaryBudget | None: ...

    @property
    def projector(self) -> Projector | None: ...

    @property
    def selection(self) -> ModelSelection: ...
