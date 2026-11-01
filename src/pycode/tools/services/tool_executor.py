# Source: Pi agent-loop.ts at 42a3497d03ad17e308a2299fa824727894f2c0ec.
# Adaptation: framework middleware owns scheduling and session Store settlement.
"""Validate, authorize and execute one tool; no second execution ledger."""

import time
from dataclasses import replace

from pycode.kernel import SnapshotError, StorageError, ToolError, clone_value, plain_data
from pycode.tools.domain.tool_call_state import ToolCallState
from pycode.tools.domain.tool_failures import tool_failure
from pycode.tools.domain.turn_guard import tool_calls_may_execute
from pycode.tools.ports.hooks import ToolHooks
from pycode.tools.ports.tools import ToolGate


class ToolExecutor:
    def __init__(
        self,
        registry,
        validator,
        gate: ToolGate,
        events,
        outputs=None,
        *,
        error_boundary,
        snapshots=None,
        hooks: ToolHooks | None = None,
    ):
        self.error_boundary = error_boundary
        self.registry = registry
        self.validator = validator
        self.gate = gate
        self.events = events
        self.outputs = outputs
        self.snapshots = snapshots
        self.hooks = hooks
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
        before = None
        result_name = call.name
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
            hook_call = replace(call, arguments=arguments)
            # A resumed approval already passed pre_tool_use before it was interrupted.
            if self.hooks is not None and state.status == "validating":
                rejected = await self.hooks.fire("pre_tool_use", context, hook_call, spec)
                if rejected is not None:
                    raise ToolError("hook_rejected", rejected)
            # A permission interrupt leaves the call awaiting_approval (set by settlement,
            # which also fires permission_request); the resumed authorize returns only
            # when the user approved.
            await self.gate.authorize(context, spec, hook_call)
            if state.status == "awaiting_approval":
                state.apply("approve")
            elif state.status == "validating":
                state.apply("run")
            if (
                self.snapshots is not None
                and spec.source != "memory"
                and spec.category in {"write", "command", "external"}
            ):
                before = await self.snapshots.capture()
        except ToolError as error:
            # Validation, authorization and synchronous Hooks precede execution.
            result = tool_failure(error, replace(call, name=result_name))
        if result is None:
            if spec is None:
                raise StorageError("unknown_tool_spec", "No spec for " + call.name)

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

            result = await self.error_boundary(replace(call, name=result_name), spec, action)
        result = replace(result, call_id=call.id, name=result_name)
        result = replace(
            result,
            session_id=context.session_id,
            run_id=context.run_id,
            operation_id=context.operation_id,
        )
        if self.outputs is not None:
            mode = "tail" if call.name.lower() in {"bash", "command"} else "head"
            result = await self.outputs.prepare(context, result, mode)
        if before is not None:
            if self.snapshots is None:
                raise SnapshotError("snapshot_unavailable", "No snapshot service for this call")
            try:
                after = await self.snapshots.capture()
                paths = []
                for change in await self.snapshots.diff(before.tree_id, after.tree_id):
                    paths.append(change.path)
                result = replace(
                    result,
                    details={
                        **result.details,
                        "file_snapshot": {
                            "before": before.tree_id,
                            "after": after.tree_id,
                            "paths": paths,
                        },
                    },
                )
            except SnapshotError as error:
                raise StorageError(
                    "snapshot_settlement_failed", "Tool executed but final snapshot failed"
                ) from error
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
        if self.hooks is None:
            return
        await self.hooks.fire("post_tool_use", context, call, result=result)
        changed_file = not result.is_error and call.name in {"write", "edit"}
        if changed_file or result.details.get("file_snapshot", {}).get("paths"):
            await self.hooks.fire("file_change", context, call, result=result)
        if result.is_error:
            await self.hooks.fire("error", context, call, result=result)
