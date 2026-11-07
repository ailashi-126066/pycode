# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
"""Receive framework tool calls, execute registered tools, and persist ordered results."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from ..kernel import (
    EntryDraft,
    ExecutionMode,
    Message,
    StorageError,
    ToolCall,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    data_hash,
    plain_data,
)
from .call_state import abandoned_result, interrupted_result
from .executor import failure_update, repeated_tool_failure


def batch_execution_mode(tools: tuple[ToolSpec, ...]) -> ExecutionMode:
    """Parallel only when every declared tool permits it."""
    if any(tool.execution_mode == "sequential" for tool in tools):
        return "sequential"
    return "parallel"


async def issued_call(context, raw) -> ToolCall:
    """The persisted call; the framework request only carries its id and arguments."""
    assistant = await context.last_assistant()
    if assistant is None or raw["id"] not in {call.id for call in assistant.tool_calls}:
        raise StorageError(
            "model_not_persisted", "Tool call has no persisted completed model message"
        )
    return next(call for call in assistant.tool_calls if call.id == raw["id"])


async def operation_context(context, call) -> ToolContext:
    assistant = await context.last_assistant()
    return ToolContext(
        context.session_id,
        context.run_id,
        f"{context.run_id}:{assistant.id}:{call.id}",
        context.workspace,
        call.id,
        call.name,
        agent_id=context.agent_id,
    )


async def persisted_result(context, tool_context) -> ToolResult | None:
    """A result saved before a restart or interrupt is reused, never executed again."""
    for message in await context.run_messages():
        if (
            message.tool_result is not None
            and message.tool_result.operation_id == tool_context.operation_id
        ):
            return message.tool_result
    return None


class ToolSettlementMiddleware(AgentMiddleware):
    # request.runtime.context is the agent's RunContext; tools cannot import agent.
    def __init__(self, *, publish: Callable[[Message], Awaitable[str]], converter):
        self.publish = publish
        self.converter = converter

    async def awrap_tool_call(self, request, handler):
        """Wait → reuse/check → execute → save → return the tool message to the graph."""
        context = cast(Any, request.runtime.context)
        call = await issued_call(context, request.tool_call)
        assistant = await context.last_assistant()
        tool_context = await operation_context(context, call)
        owner = cast(asyncio.Task, asyncio.current_task())
        context.tool_tasks.add(owner)
        try:
            await self.wait_for_turn(context, call)
            result = await persisted_result(context, tool_context)
            if result is None:
                state = await context.executor.begin(tool_context, call)
                resumed = state.status == "awaiting_approval"
                spec, result = await self.check_tool(context, call)
                if result is None:
                    result = await context.executor.execute(
                        tool_context,
                        call,
                        assistant.stop_reason,
                        spec=spec,
                        before_execute=partial(self.record_tool_start, context, resumed),
                    )
                    if resumed and result.error_code == "permission_denied":
                        await self.record_approval(context, tool_context, spec, "deny")
            message = await self.save_result(context, call, tool_context, result)
            return Command(update={"messages": [message]})
        except asyncio.CancelledError:
            await self.settle_cancelled(context, call, tool_context)
            raise
        except GraphInterrupt:
            # wait_for_turn already marked calls blocked by an earlier approval.
            if call.id not in context.interrupted_calls:
                await self.pause_call(context, call, tool_context)
            raise
        finally:
            context.tool_tasks.discard(owner)

    async def record_tool_start(self, context, resumed, tool_context, call, spec):
        """Executor callback for lifecycle records only; it never invokes the tool."""
        if resumed:
            await self.record_approval(context, tool_context, spec, "approve")
        await self.mark_prepared(context, call)
        await context.history.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "tool_started", "replay": spec.replay},
                run_id=context.run_id,
                operation_id=tool_context.operation_id,
            )
        )

    async def pause_call(self, context, call, tool_context):
        state = context.executor.tool_calls[tool_context.operation_id]
        if state.status == "validating":
            state.apply("request_approval")
        async with context.settlement:
            context.interrupted_calls.add(call.id)
            await self.settle_ready(context)
            async with context.duplicate_calls:
                context.dispatched_calls.add(call.id)
                context.duplicate_calls.notify_all()
            context.settlement.notify_all()

    async def settle_cancelled(self, context, call, tool_context):
        """Preserve completed results; distinguish interrupted effects from unstarted calls."""
        state = await context.executor.begin(tool_context, call)
        outcome = (
            interrupted_result(call, "the run was cancelled while this call was running")
            if state.status == "running"
            else abandoned_result(call)
        )
        await self.save_result(context, call, tool_context, outcome, stopping=True)

    # Pi: executeToolCalls. One sequential tool makes the whole batch sequential.
    async def wait_for_turn(self, context, call):
        assistant = await context.last_assistant()
        call_order = [item.id for item in assistant.tool_calls]
        names = {spec.name for spec in context.registry.list_specs()}
        specifications = tuple(
            context.registry.get_spec(call.name)
            for call in assistant.tool_calls
            if call.name in names
        )
        serial = batch_execution_mode(specifications) == "sequential"
        if call.id in call_order:
            position = call_order.index(call.id)
            preceding = set(call_order[:position])
            async with context.duplicate_calls:
                if serial:
                    await context.duplicate_calls.wait_for(
                        lambda: preceding <= context.dispatched_calls
                    )
                else:
                    # Pi: executeToolCallsParallel prepares calls one by one (one approval
                    # at a time) and only then lets them execute concurrently.
                    await context.duplicate_calls.wait_for(
                        lambda: preceding <= context.prepared_calls | context.dispatched_calls
                    )
                    if preceding & context.interrupted_calls:
                        # An earlier call awaits an answer: stop without a result or event;
                        # the resumed tools node runs this call again.
                        context.interrupted_calls.add(call.id)
                        context.dispatched_calls.add(call.id)
                        context.duplicate_calls.notify_all()
                        raise GraphInterrupt(())

    async def mark_prepared(self, context, call):
        async with context.duplicate_calls:
            context.prepared_calls.add(call.id)
            context.duplicate_calls.notify_all()

    async def save_result(self, context, call, tool_context, result, *, stopping=False):
        """Attach operation details, persist in call order, then return the framework message."""
        # Already-persisted results keep their original metadata and read receipts.
        if await persisted_result(context, tool_context) is None:
            assistant = await context.last_assistant()
            details = {**result.details, "source_assistant_id": assistant.id}
            if context.workspace_files is not None:
                details["read_receipts"] = dict(context.workspace_files.receipts)
            result = replace(
                result,
                session_id=context.session_id,
                run_id=context.run_id,
                operation_id=tool_context.operation_id,
                details=details,
            )
        if not stopping:
            await self.mark_prepared(context, call)
        async with context.settlement:
            if not stopping or (
                call.id not in context.ready_results and call.id not in context.settled_calls
            ):
                context.ready_results[call.id] = (call, result)
            if stopping:
                await self.settle_ready(context, stopping=True)
                context.settlement.notify_all()
                return None
            while call.id not in context.settled_calls:
                await self.settle_ready(context)
                if call.id not in context.settled_calls:
                    await context.settlement.wait()
            context.settlement.notify_all()
        return self.converter(
            Message("result:" + tool_context.operation_id, "tool_result", tool_result=result),
            context.selection,
        )

    async def check_tool(self, context, call):
        result = await self.repeated_failure(context, call)
        if result is not None:
            return None, result
        try:
            spec = context.registry.get_spec(call.name)
        except ToolError:
            return None, ToolResult(
                call.id,
                call.name,
                "Unknown tool: " + call.name,
                is_error=True,
                error_code="unknown_tool",
                details={"executed": False},
            )
        if (
            context.disclosed_tool_names is not None
            and call.name not in context.disclosed_tool_names
        ):
            return None, ToolResult(
                call.id,
                call.name,
                "Tool was not declared for this model request",
                is_error=True,
                error_code="tool_not_declared",
                details={"executed": False},
            )
        return spec, None

    async def record_approval(self, context, tool_context, spec, decision):
        await context.history.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "tool_approval",
                    "decision": decision,
                    "spec_hash": data_hash(plain_data(spec)),
                },
                run_id=context.run_id,
                operation_id=tool_context.operation_id,
            )
        )

    async def repeated_failure(self, context, call):
        assistant = await context.last_assistant()
        digest = data_hash(call.arguments)
        if (
            context.failure_guard["failure_count"] > 0
            and context.failure_guard["failure_tool"] == call.name
            and context.failure_guard["failure_arguments"] == digest
        ):
            async with context.duplicate_calls:
                preceding = set()
                for candidate in assistant.tool_calls:
                    if candidate.id == call.id:
                        break
                    if candidate.name == call.name and data_hash(candidate.arguments) == digest:
                        preceding.add(candidate.id)
                await context.duplicate_calls.wait_for(
                    lambda: preceding <= context.dispatched_calls
                )
        guard = context.failure_guard
        if repeated_tool_failure(guard, call.name, digest):
            return ToolResult(
                call.id,
                call.name,
                "repeated_tool_failure: read the current state or choose another approach",
                is_error=True,
                error_code="repeated_tool_failure",
                details={"executed": False},
            )
        return None

    async def settle_ready(self, context, *, stopping=False):
        """Settle ready results in call order; stopping skips calls that never finished."""
        assistant = await context.last_assistant()
        for identifier in (call.id for call in assistant.tool_calls):
            if identifier in context.settled_calls or identifier in context.interrupted_calls:
                continue
            ready = context.ready_results.get(identifier)
            if ready is None:
                if stopping:
                    continue
                break
            call, result = ready
            digest = data_hash(call.arguments)
            context.failure_guard = failure_update(context.failure_guard, call, result, digest)
            operation_id = cast(str, result.operation_id)
            message = Message("result:" + operation_id, "tool_result", tool_result=result)
            await self.publish(message)
            context.settled_calls.add(call.id)
            tool_context = await operation_context(context, call)
            await context.executor.emit_tool_execution_end(tool_context, call, result)
            async with context.duplicate_calls:
                context.dispatched_calls.add(call.id)
                context.duplicate_calls.notify_all()
