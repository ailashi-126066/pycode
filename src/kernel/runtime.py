# Pi 没有，pycode 新增
"""Immutable session and run data shared by execution callbacks."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .events import EventStream

if TYPE_CHECKING:
    from pathlib import Path

    from ..session import SqliteSessionStore


@dataclass(frozen=True)
class SessionScope:
    """Session-wide execution data; no service registry."""

    session_id: str
    workspace: "Path"
    user_data: "Path"
    history: "SqliteSessionStore"
    events: EventStream
    read_only: bool
    agent_id: str


@dataclass(frozen=True)
class RunScope:
    session: SessionScope
    run_id: str
    events: EventStream
