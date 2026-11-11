"""Run naming helpers for LangChain/LangSmith tracing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resolve_root_run_name(config: Mapping[str, Any], assistant_id: str | None) -> str:
    context = config.get("context")
    if isinstance(context, Mapping):
        agent_name = context.get("agent_name")
        if isinstance(agent_name, str) and agent_name.strip():
            return agent_name
    return assistant_id or "lead_agent"
