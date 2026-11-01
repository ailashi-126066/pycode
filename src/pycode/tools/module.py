"""Own tool adapters, backend composition and mandatory execution policy ordering."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import BaseMessage

from pycode.kernel import (
    ArgumentValidator,
    EventSink,
    ExecutableTool,
    Message,
    ModelSelection,
    ToolCall,
    ToolContext,
    ToolSpec,
)
from pycode.plans import PlanService
from pycode.sandbox import SandboxBackend, SandboxSettings
from pycode.snapshot import SnapshotStore
from pycode.tools.adapters.ask_user import AskUserQuestion
from pycode.tools.adapters.backend import PycodeBackend
from pycode.tools.adapters.builtin.edit import Edit
from pycode.tools.adapters.builtin.find import Find
from pycode.tools.adapters.builtin.grep import Grep
from pycode.tools.adapters.builtin.ls import Ls
from pycode.tools.adapters.builtin.read import Read
from pycode.tools.adapters.builtin.write import Write
from pycode.tools.adapters.discovery import ToolDiscovery
from pycode.tools.adapters.framework_tool import make_framework_tool
from pycode.tools.adapters.output_store import TempOutputStore
from pycode.tools.adapters.permissions import InterruptToolGate
from pycode.tools.adapters.questions import InterruptQuestionPrompt
from pycode.tools.adapters.schema import JsonSchemaValidator
from pycode.tools.adapters.settlement import ToolSettlementMiddleware
from pycode.tools.adapters.tool_errors import execute_tool_errors
from pycode.tools.ports.extensions import NativeToolBinding, ToolExtension
from pycode.tools.ports.hooks import ToolHooks
from pycode.tools.ports.outputs import ToolOutputStore
from pycode.tools.ports.publication import MessagePublisher
from pycode.tools.ports.questions import QuestionPrompt
from pycode.tools.ports.tools import ToolGate
from pycode.tools.services.permissions import MemoryToolGate, ReadOnlyToolGate
from pycode.tools.services.plan_gate import PlanToolGate
from pycode.tools.services.tool_executor import ToolExecutor
from pycode.tools.services.tool_registry import ToolRegistry


@dataclass(frozen=True)
class _ScopedGate:
    scope: ToolGate
    parent: ToolGate

    async def authorize(self, context: ToolContext, spec: ToolSpec, call: ToolCall) -> None:
        await self.scope.authorize(context, spec, call)
        await self.parent.authorize(context, spec, call)


class ToolsModule:
    discovery = staticmethod(ToolDiscovery)
    framework_tool = staticmethod(make_framework_tool)
    execute_tool_errors = staticmethod(execute_tool_errors)

    def __init__(self) -> None:
        self._validator = JsonSchemaValidator()

    def validator(self) -> ArgumentValidator:
        return self._validator

    def registry(self, tools: tuple[ExecutableTool, ...]) -> ToolRegistry:
        return ToolRegistry(self._validator, tools)

    @staticmethod
    def output_store(session_id: str) -> TempOutputStore:
        return TempOutputStore(session_id)

    @staticmethod
    def backend(
        workspace: Path,
        *,
        read_only: bool = False,
        write_roots: tuple[str, ...] | None = None,
        excluded_roots: tuple[Path, ...] = (),
        path_guard: Callable[[str], None] | None = None,
        search_scope: Callable[[str], tuple[str, ...]] | None = None,
        empty_missing_root: bool = False,
    ) -> PycodeBackend:
        return PycodeBackend(
            workspace,
            read_only=read_only,
            excluded_roots=excluded_roots,
            write_roots=write_roots,
            path_guard=path_guard,
            search_scope=search_scope,
            empty_missing_root=empty_missing_root,
        )

    @staticmethod
    def read_only_mount(root: Path) -> PycodeBackend:
        """A directory the builtin tools can read under a virtual prefix (/skills/, /memory/).

        Mounts outside path_utils.WRITABLE never accept write/edit, and reads run in this
        process, so the sandbox does not need access to the directory.
        """
        return PycodeBackend(root, read_only=True, empty_missing_root=True)

    @staticmethod
    def composite(routes: Mapping[str, object]) -> CompositeBackend:
        # Assembly carries opaque SDK handles. Validate them here, where the
        # framework types belong, rather than importing the SDK into app inputs.
        backends: dict[str, BackendProtocol] = {}
        for prefix, backend in routes.items():
            if not isinstance(backend, BackendProtocol):
                raise TypeError(f"Invalid framework backend for route {prefix}")
            backends[prefix] = backend
        return CompositeBackend(default=StateBackend(), routes=backends)

    @staticmethod
    def builtin_tools(backend: CompositeBackend) -> tuple[ExecutableTool, ...]:
        """Pi's file tools; bash is owned by the sandbox feature."""
        return (
            Read(backend),
            Write(backend),
            Edit(backend),
            Grep(backend),
            Find(backend),
            Ls(backend),
        )

    @staticmethod
    def question_prompt() -> QuestionPrompt:
        return InterruptQuestionPrompt()

    @staticmethod
    def default_tools(question_prompt: QuestionPrompt | None = None) -> tuple[ExecutableTool, ...]:
        return (AskUserQuestion(question_prompt),) if question_prompt is not None else ()

    @staticmethod
    def gate(
        sandbox: SandboxBackend,
        settings: SandboxSettings,
        *,
        interruptible: bool = True,
        read_only: bool = False,
        custom: ToolGate | None = None,
        memory_specs: tuple[ToolSpec, ...] = (),
        plans: PlanService | None = None,
        scope: ToolGate | None = None,
    ) -> ToolGate:
        gate: ToolGate = (
            InterruptToolGate(sandbox, settings)
            if interruptible and not read_only
            else ReadOnlyToolGate()
        )
        if memory_specs:
            gate = MemoryToolGate(gate, memory_specs)
        if plans is not None:
            gate = PlanToolGate(plans, gate)
        if custom is not None:
            gate = custom
        if scope is not None:
            gate = _ScopedGate(scope, gate)
        return gate

    def executor(
        self,
        registry: ToolRegistry,
        gate: ToolGate,
        events: EventSink,
        outputs: ToolOutputStore | None = None,
        *,
        snapshots: SnapshotStore | None = None,
        hooks: ToolHooks | None = None,
    ) -> ToolExecutor:
        return ToolExecutor(
            registry,
            self._validator,
            gate,
            events,
            outputs,
            snapshots=snapshots,
            hooks=hooks,
            error_boundary=execute_tool_errors,
        )

    @staticmethod
    def settlement(
        *,
        publisher: MessagePublisher,
        converter: Callable[[Message, ModelSelection], BaseMessage],
        tool_extensions: tuple[ToolExtension, ...] = (),
        native_bindings: tuple[NativeToolBinding, ...] = (),
    ) -> ToolSettlementMiddleware:
        return ToolSettlementMiddleware(
            publisher=publisher,
            converter=converter,
            tool_extensions=tool_extensions,
            native_bindings=native_bindings,
        )
