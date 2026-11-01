"""Typed assembly inputs. These records borrow resources; they never create or close them."""

from dataclasses import dataclass

from pycode.context import EvictionBudget, SummaryService
from pycode.hooks import Hook
from pycode.kernel import AgentTool, EventConsumer, MemoryEventJournal, Purpose, ToolSpec
from pycode.mcp import MCPRun
from pycode.memory import MemoryService
from pycode.models import ProviderConfig, RetryPolicy
from pycode.plans import PlanService
from pycode.session import DurableInputs, SqliteSessionStore
from pycode.skills.module import SkillsRun
from pycode.subagents import SubagentManager
from pycode.tools import ToolOutputStore


@dataclass(frozen=True, kw_only=True)
class SessionResources:
    """Existing session capabilities shared by its runs, with explicit optional features."""

    session_id: str
    journal: SqliteSessionStore
    tools: tuple[AgentTool, ...] = ()
    consumers: tuple[EventConsumer, ...] = ()
    history_store: SqliteSessionStore | None = None
    event_journal: MemoryEventJournal | None = None
    configured_hooks: tuple[Hook, ...] = ()
    plans: PlanService | None = None
    plan_backend: object | None = None  # Opaque framework backend, only passed to assembly.
    durable_inputs: DurableInputs | None = None
    memory: MemoryService | None = None
    subagents: SubagentManager | None = None
    outputs: ToolOutputStore | None = None
    summary_service: SummaryService | None = None


@dataclass(frozen=True, kw_only=True)
class RunOptions:
    """Selection and prepared capabilities for exactly one graph execution.

    Working directory and read-only policy stay in AppConfig; there is no second
    copy here that could disagree with the session/worker policy.
    """

    run_id: str
    provider: ProviderConfig
    agent_id: str = "lead"
    purpose: Purpose = "agent"
    retry_policy: RetryPolicy | None = None
    eviction_budget: EvictionBudget | None = None
    checkpointer: object | None = None  # Opaque SDK handle, interpreted by the runtime.
    effective_tools: tuple[ToolSpec, ...] = ()
    skills_run: SkillsRun | None = None
    mcp_run: MCPRun | None = None
