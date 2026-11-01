# Pi 没有，pycode 新增
# 对应 Pi 扩展系统 pi.on() 与 AgentLoopConfig 的回调（packages/agent/src/types.ts）
"""The Feature contract: optional modules plug into the core only through these hooks."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, TypedDict

from pycode.kernel.events import Event, EventStream
from pycode.kernel.history import ProjectionContribution, SessionEntry, SessionSnapshot
from pycode.kernel.messages import Message, ToolCall, ToolResult
from pycode.kernel.tools import AgentTool, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path

    from pycode.session import SqliteSessionStore


@dataclass(frozen=True)
class SessionScope:
    """Session-wide dependencies handed to Feature.open."""

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


class BeforeToolCallResult(TypedDict, total=False):
    """Pi: BeforeToolCallResult."""

    block: bool
    reason: str


class AfterToolCallResult(TypedDict, total=False):
    """Pi: AfterToolCallResult; set fields replace the result's fields."""

    text: str
    details: dict[str, Any]
    is_error: bool


class ToolGate(Protocol):
    async def authorize(
        self, run: RunScope, spec: ToolSpec, call: ToolCall
    ) -> Literal["allow"] | None:
        """None: no opinion; "allow": skip approval; raise ToolError to deny."""
        ...


# Runs one sandboxed command: (argv, cwd, env, timeout seconds).
CommandRunner = Callable[
    [tuple[str, ...], str, Mapping[str, str] | None, float], Awaitable[ToolResult]
]


class Feature:
    """Every hook defaults to a no-op; a module overrides only what it uses."""

    name: ClassVar[str]
    checkpoint_keys: ClassVar[frozenset[str]] = frozenset()

    async def open(self, session: SessionScope) -> None:
        pass

    async def prepare_run(self, run: RunScope) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def tools(self, run: RunScope) -> tuple[AgentTool, ...]:
        return ()

    def mounts(self, run: RunScope) -> Mapping[str, object]:
        """Path prefix -> Deep Agents backend."""
        return {}

    def gates(self, run: RunScope) -> tuple[ToolGate, ...]:
        return ()

    def middleware(self, run: RunScope) -> tuple[object, ...]:
        """LangChain AgentMiddleware instances; kernel does not import the framework."""
        return ()

    def prompt_sections(self, session: SessionScope) -> tuple[str, ...]:
        return ()

    def contribute(
        self,
        snapshot: SessionSnapshot,
        path: tuple[SessionEntry, ...],
        checkpoint: Mapping[str, Any],
    ) -> ProjectionContribution | None:
        return None

    def transform_context(
        self, run: RunScope, messages: tuple[Message, ...]
    ) -> tuple[Message, ...]:
        return messages

    def select_tools(self, run: RunScope, specs: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
        return specs

    async def before_tool_call(
        self, run: RunScope, spec: ToolSpec, call: ToolCall
    ) -> BeforeToolCallResult | None:
        return None

    def after_tool_call(
        self, run: RunScope, call: ToolCall, result: ToolResult
    ) -> AfterToolCallResult | None:
        return None

    async def on_event(self, event: Event) -> None:
        pass
