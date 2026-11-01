"""Public native contracts, policies and services for context."""

from pycode.context.domain.context_budget import InputBudget
from pycode.context.domain.context_meter import ContextMeter
from pycode.context.domain.history_boundaries import valid_kept_boundary
from pycode.context.domain.retention import retained_context_entries
from pycode.context.domain.summaries import SummaryReport, SummarySettings
from pycode.context.domain.tasks import (
    TaskOutput,
    apply_task_change,
    empty_tasks,
    task_context,
    task_context_text,
    task_evidence,
    validate_task_action,
)
from pycode.context.domain.token_estimate import estimate_messages
from pycode.context.domain.tool_eviction import EvictionBudget
from pycode.context.ports.summaries import SummaryGenerator, SummaryStore
from pycode.context.services.context_projector import ContextProjector
from pycode.context.services.prompts import PromptService
from pycode.context.services.summary import SummaryService
from pycode.context.services.tool_eviction import ToolEvictionService

__all__ = [
    "TaskOutput",
    "apply_task_change",
    "empty_tasks",
    "task_context",
    "task_context_text",
    "task_evidence",
    "validate_task_action",
    "ProjectionContribution",
    "ProjectionContributor",
    "ProjectedContext",
    "valid_kept_boundary",
    "estimate_messages",
    "ContextMeter",
    "ContextProjector",
    "EvictionBudget",
    "InputBudget",
    "PromptService",
    "SummaryGenerator",
    "SummaryService",
    "SummaryReport",
    "SummarySettings",
    "SummaryStore",
    "ToolEvictionService",
    "retained_context_entries",
]

from pycode.context.domain.projection import ProjectionContribution
from pycode.context.ports.projection import ProjectionContributor
from pycode.kernel import ProjectedContext
