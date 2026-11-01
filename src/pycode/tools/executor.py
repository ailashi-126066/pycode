# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
# Adaptation: framework middleware owns scheduling and session Store settlement.
"""Validate, authorize and execute one tool; no second execution ledger."""

import re
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, TypedDict, cast

from langchain.agents.middleware import ToolErrorMiddleware
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from pycode.kernel import (
    SECRET_PATTERNS,
    AgentTool,
    DomainError,
    Feature,
    RunScope,
    StopReason,
    ToolError,
    ToolGate,
    ToolResult,
    ToolSpec,
    clone_value,
    json_dumps,
    plain_data,
)
from pycode.tools.call_state import ToolCallState
from pycode.tools.output import TempOutputStore
from pycode.tools.permissions import InterruptToolGate, ReadOnlyToolGate
from pycode.tools.schema import JsonSchemaValidator


def tool_calls_may_execute(stop_reason: StopReason | None) -> bool:
    """Incomplete model output cannot authorize tool execution."""
    return stop_reason not in {"length", "error", "aborted"}


def tool_failure(error, call, *, executed=False, uncertain=False):
    """One stable, bounded mapping from any tool exception to a single error result."""
    if isinstance(error, DomainError):
        code = error.code
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            code = "tool_execution_error"
        text = str(error)
    elif isinstance(error, OSError):
        code = "tool_io_error"
        text = "Tool I/O operation failed"
    else:
        # Name only the type: arbitrary exception messages may carry internal detail.
        code = "tool_execution_error"
        text = f"Tool failed with {type(error).__name__}"
    for pattern in SECRET_PATTERNS:
        text = re.sub(pattern, "[redacted]", text)
    details = {"executed": executed}
    if uncertain:
        details["side_effects_uncertain"] = True
    return ToolResult(
        call.id, call.name, text[:1600], is_error=True, error_code=code, details=details
    )


def repeated_tool_failure(guard: Mapping[str, Any], name: str, arguments_hash: str) -> bool:
    """Reject a fourth execution of the same consecutively failing declaration."""
    return (
        guard.get("failure_count", 0) >= 3
        and guard.get("failure_tool") == name
        and guard.get("failure_arguments") == arguments_hash
    )


class FailureGuard(TypedDict, total=False):
    failure_tool: str
    failure_arguments: str
    failure_code: str
    failure_count: int


def failure_update(previous, call, result, arguments_hash) -> FailureGuard:
    count = 0
    if result.is_error:
        same = (
            previous.get("failure_tool") == call.name
            and previous.get("failure_arguments") == arguments_hash
            and previous.get("failure_code") == result.error_code
        )
        count = previous.get("failure_count", 0) + 1 if same else 1
    repeated = result.error_code == "repeated_tool_failure"
    return {
        "failure_tool": call.name if result.is_error else "",
        "failure_arguments": arguments_hash if result.is_error else "",
        "failure_code": previous.get("failure_code", "") if repeated else result.error_code or "",
        "failure_count": previous.get("failure_count", 3) if repeated else count,
    }


async def execute_tool_errors(call, spec, action):
    """The official error boundary wraps execution only, inside durable tool settlement.

    Every Exception from execution becomes one error result; LangGraph control flow
    (interrupts) still propagates, and so does cancellation (a BaseException).
    """
    request = ToolCallRequest(
        tool_call={"type": "tool_call", "id": call.id, "name": call.name, "args": call.arguments},
        tool=None,
        state={},
        runtime=ToolRuntime(
            state={},
            context=None,
            config={},
            stream_writer=lambda _: None,
            tool_call_id=call.id,
            store=None,
        ),
    )
    failure = None

    async def on_error(error, current):
        nonlocal failure
        failure = tool_failure(error, call, executed=True, uncertain=spec.category != "read")
        return json_dumps(
            {"error_code": failure.error_code, "message": failure.text}, ensure_ascii=False
        )

    async def invoke(current):
        result = await action()
        return ToolMessage(
            content=result.text,
            artifact=result,
            tool_call_id=call.id,
            name=call.name,
            status="error" if result.is_error else "success",
        )

    response = await ToolErrorMiddleware(aon_error=on_error).awrap_tool_call(request, invoke)
    if failure is not None:
        return failure
    # invoke always returns a ToolMessage; the middleware only replaces errors with text.
    return cast(ToolMessage, response).artifact


class ToolRegistry:
    """Explicit registration; modules never self-register during import."""

    def __init__(self, validator: JsonSchemaValidator, tools: tuple[AgentTool, ...] = ()) -> None:
        self._validator = validator
        self._tools: dict[str, AgentTool] = {}
        self._specs: dict[str, ToolSpec] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        name = tool.spec.name
        if not name or name in self._tools:
            raise ToolError(
                "tool_name_conflict", f"Tool name is empty or already registered: {name}"
            )
        self._validator.check_schema(tool.spec.input_schema)
        self._tools[name] = tool
        # Specs are frozen dataclasses; copies are taken when specs leave the registry.
        self._specs[name] = tool.spec

    def get(self, name: str) -> AgentTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("unknown_tool", f"Unknown tool: {name}")
        return tool

    def replace_source(self, prefix, tools):
        if any(not tool.spec.source.startswith(prefix) for tool in tools):
            raise ToolError("invalid_tool_source", "Replacement tools have a different source")
        preserved = tuple(
            tool for tool in self.list_tools() if not tool.spec.source.startswith(prefix)
        )
        validated = ToolRegistry(self._validator, (*preserved, *tools))
        self._tools, self._specs = validated._tools, validated._specs

    def list_tools(self) -> tuple[AgentTool, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))

    def get_spec(self, name: str) -> ToolSpec:
        self.get(name)
        return clone_value(self._specs[name])

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(clone_value(self._specs[name]) for name in sorted(self._specs))

    def model_specs(self):
        return tuple(spec for spec in self.list_specs() if spec.model_visible)


class ToolExecutor:
    """One per run. gates come first (the user's AppConfig.tool_gate), then each feature's."""

    def __init__(
        self,
        registry: ToolRegistry,
        validator: JsonSchemaValidator,
        approval: InterruptToolGate | ReadOnlyToolGate,
        run: RunScope,
        features: tuple[Feature, ...] = (),
        outputs: TempOutputStore | None = None,
        *,
        gates: tuple[ToolGate, ...] = (),
    ):
        self.registry = registry
        self.validator = validator
        self.approval = approval
        self.run = run
        self.events = run.events
        self.features = features
        self.gates = (*gates, *(gate for feature in features for gate in feature.gates(run)))
        self.outputs = outputs
        # Keyed by operation_id; one executor lives as long as one run container.
        self.tool_calls: dict[str, ToolCallState] = {}

    async def begin(self, context, call) -> ToolCallState:
        """Announce a call once; re-entry after an approval interrupt keeps its state."""
        state = self.tool_calls.setdefault(context.operation_id, ToolCallState())
        if state.status == "issued":
            await self.events.emit(
                "tool_execution_start",
                {"tool_call_id": call.id, "tool_name": call.name, "args": call.arguments},
                operation_id=context.operation_id,
            )
            state.apply("validate")
        return state

    async def authorize(self, context, spec, call) -> None:
        """Design 2.3: every gate runs and a ToolError denies; one "allow" skips approval."""
        allowed = False
        for gate in self.gates:
            allowed = await gate.authorize(self.run, spec, call) == "allow" or allowed
        if not allowed:
            await self.approval.authorize(context, spec, call)

    async def execute(self, context, call, stop_reason, *, spec=None, invoke=None):
        # Pi: acceptingUpdates. Updates that arrive after the tool returned are dropped.
        accepting_updates = True

        async def progress(text):
            if not accepting_updates:
                return
            await self.events.emit(
                "tool_execution_update",
                {"tool_call_id": call.id, "tool_name": call.name, "delta": text},
                operation_id=context.operation_id,
            )

        context = replace(context, call_id=call.id, tool_name=call.name, progress=progress)
        state = await self.begin(context, call)
        result = None
        try:
            if not tool_calls_may_execute(stop_reason):
                raise ToolError(
                    "incomplete_model_output",
                    "Tool call cannot execute: model stopped at output token limit or failed",
                )
            if call.parse_error:
                raise ToolError("invalid_arguments", call.parse_error)
            if spec is None:
                spec = self.registry.get_spec(call.name)
            if invoke is None:
                tool = self.registry.get(call.name)
            # Pi: prepareToolCall — prepareArguments, then Value.Convert, then schema check.
            arguments = clone_value(call.arguments)
            if spec.prepare_arguments is not None:
                arguments = spec.prepare_arguments(arguments)
            arguments = self.validator.convert(spec.input_schema, arguments)
            self.validator.validate(spec.input_schema, arguments)
            prepared = replace(call, arguments=arguments)
            # A permission interrupt leaves the call awaiting_approval (set by settlement);
            # the resumed authorize returns only when the user approved.
            await self.authorize(context, spec, prepared)
            # Pi: beforeToolCall, after validation and before execution.
            for feature in self.features:
                before = await feature.before_tool_call(self.run, spec, prepared)
                if before is not None and before.get("block"):
                    raise ToolError(
                        "hook_rejected", before.get("reason") or "Tool execution was blocked"
                    )
            if state.status == "awaiting_approval":
                state.apply("approve")
            elif state.status == "validating":
                state.apply("run")
        except ToolError as error:
            # Validation, authorization and before_tool_call precede execution.
            result = tool_failure(error, call)
        executed = result is None
        if result is None:

            async def action():
                nonlocal accepting_updates
                await self.events.emit(
                    "tool_execution_update",
                    {
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "state": "running",
                    },
                    operation_id=context.operation_id,
                )
                try:
                    if invoke is None:
                        return await tool.execute(context, arguments)
                    return await invoke(context, call, spec)
                finally:
                    accepting_updates = False

            result = await execute_tool_errors(call, spec, action)
        result = replace(
            result,
            call_id=call.id,
            name=call.name,
            session_id=context.session_id,
            run_id=context.run_id,
            operation_id=context.operation_id,
        )
        if self.outputs is not None:
            mode = "tail" if call.name == "bash" else "head"
            result = await self.outputs.prepare(context, result, mode)
        if executed:
            result = await self.after_tool_call(call, result)
        return result

    # Pi: finalizeExecutedToolCall. Features override fields of an executed call's result
    # in list order before it is persisted; a replaced text drops its structured content.
    async def after_tool_call(self, call, result):
        for feature in self.features:
            override = await feature.after_tool_call(self.run, call, result)
            if override is not None:
                if "text" in override:
                    result = replace(result, structured_content=None)
                result = replace(result, **override)
        return result

    # Pi: emitToolExecutionEnd. Only called after the result has been persisted.
    async def emit_tool_execution_end(self, context, call, result):
        state = self.tool_calls[context.operation_id]
        state.settle(result)
        await self.events.emit(
            "tool_execution_end",
            {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "result": plain_data(result),
                "is_error": result.is_error,
                "status": state.status,
                "outcome": state.outcome,
                "duration_ms": round((time.monotonic() - state.began) * 1000),
            },
            operation_id=context.operation_id,
        )
        # 11.1 section 3.1: stream-json sees each saved board snapshot (TaskWrite, TaskUpdate,
        # ExitPlanMode).
        if "task_board" in result.details:
            await self.events.emit(
                "task", {"board": result.details["task_board"]}, operation_id=context.operation_id
            )
