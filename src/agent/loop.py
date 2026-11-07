# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
"""Pi's runLoop as LangChain middleware: one owner for history, turns and queued input."""

import asyncio
import time
from dataclasses import replace
from typing import cast
from uuid import uuid4

from langchain.agents.middleware import (
    AgentMiddleware,
    ExtendedModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from ..kernel import (
    Message,
    ModelError,
    StorageError,
    TextBlock,
    plain_data,
)
from ..models import from_assistant, to_framework, to_framework_messages
from ..tools import ToolDiscovery, make_framework_tool
from .boundaries import deliver_input, prepare_model_boundary, save_message
from .calibration import calibrate_model_request
from .context import PycodeState, RunContext, next_turn, transition_phase


async def poll_steering(context):
    """Pi: getSteeringMessages. Polled at loop start, at each turn end, and after preparation."""
    context.inputs = await context.durable_inputs.pending(context.run_id)
    return context.inputs.take("steer", one_at_a_time=context.one_at_a_time)


async def end_turn(context, run_hook, turn):
    """Publish the completed graph turn using its persisted turn number."""
    message = await context.last_assistant()
    if message is not None and message.provider_metadata["pycode_turn"] != turn:
        # The turn failed before its reply was saved.
        message = None
    results = [
        item.id
        for item in await context.run_messages()
        if message is not None
        and item.tool_result is not None
        and item.tool_result.details["source_assistant_id"] == message.id
    ]
    await context.events.emit(
        "turn_end",
        {
            "turn": turn,
            "message_id": message.id if message is not None else None,
            "tool_result_ids": results,
        },
    )

    await run_hook(context.scope, "turn_end", message=message.text if message is not None else "")


def take_queued(context):
    # The agent loop has already settled with no tool calls. Queued input is delivered after
    # any completed loop, so no stop reason is applied here.
    decision = next_turn(
        steering_pending=context.inputs.has("steer"),
        follow_up_pending=context.inputs.has("follow_up"),
    )
    if decision.queue is None:
        return None, ()
    return decision.queue, context.inputs.take(decision.queue, one_at_a_time=context.one_at_a_time)


def transform_context(context, transforms, messages) -> tuple[Message, ...]:
    """Pi: transformContext. Transform projected messages in supplied order."""
    for transform in transforms:
        messages = transform(context.scope, messages)
    return messages


# Pi's runLoop hooks map onto LangChain middleware: prepareNextTurn ↔ abefore_model,
# prepareRequest ↔ awrap_model_call, finishTurn ↔ aafter_model / aafter_agent.
class HistoryMiddleware(AgentMiddleware[PycodeState, RunContext]):
    state_schema = PycodeState

    def __init__(
        self,
        run_hook,
        *,
        discovery=None,
        context_transforms=(),
        tool_filters=(),
        before_finish=None,
    ):
        self.run_hook = run_hook
        self.discovery = discovery if discovery is not None else ToolDiscovery(make_framework_tool)
        self.context_transforms = context_transforms
        self.tool_filters = tool_filters
        self.before_finish = before_finish

    async def current_projection(self, context):
        """Build transient domain messages from the durable branch for this preparation."""

        snapshot = await context.history.snapshot()
        projection = context.projector.transform_context(snapshot)
        context.model_source_leaf_id = snapshot.leaf_id
        return projection

    async def model_messages(self, context):
        projection = await self.current_projection(context)
        return to_framework_messages(
            transform_context(context, self.context_transforms, projection.messages),
            context.selection,
        )

    async def abefore_agent(self, state, runtime):
        context = runtime.context
        await self.run_hook(context.scope, "run_start")
        await context.events.emit("agent_start", {})
        await context.events.emit("turn_start", {"turn": 1})
        for message, entry_id in zip(
            context.initial_inputs, context.initial_entry_ids, strict=True
        ):
            await context.events.emit("message_start", {"message": plain_data(message)})
            await context.events.emit(
                "message_end", {"message": plain_data(message), "entry_id": entry_id}
            )
        # Pi polls steering at loop start: the user may have typed while waiting.
        context.steering = await poll_steering(context)

    # Pi: prepareNextTurn. LangGraph's jump_to "end" implements the turn budget stop.
    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        context = runtime.context
        turn = state.get("turn", 0)
        if turn >= context.max_turns:
            return {"reason": "turn_limit", "jump_to": "end"}
        phase = transition_phase(state, "model")
        ended_turn = state.get("ended_turn", 0)
        if turn > ended_turn:
            await end_turn(context, self.run_hook, turn)
            ended_turn = turn
            context.steering = await poll_steering(context)
        return {"turn": turn + 1, "phase": phase, "ended_turn": ended_turn}

    # The framework entry point keeps one visible sequence: prepare → request → save.
    async def awrap_model_call(self, request, handler):
        request = await self.prepare_request(request)
        started = time.monotonic()
        response, request = await self.request_model(request, handler, started)
        response = await self.save_response(request, response, started)
        model_response = (
            response.model_response if isinstance(response, ExtendedModelResponse) else response
        )
        # Commit exactly the prepared input plus the reply, replacing stale graph history.
        updated = replace(
            model_response,
            result=[
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *request.messages,
                *model_response.result,
            ],
        )
        if isinstance(response, ExtendedModelResponse):
            return replace(response, model_response=updated)
        return updated

    async def prepare_request(self, request):
        """Prepare tools and budget, maintain history, admit input, then build final messages."""
        context = request.runtime.context

        # 1. Decide the actual tools and system footprint before calculating input capacity.
        request = await prepare_model_boundary(request, self.discovery, self.tool_filters)
        await calibrate_model_request(request)

        # 2. Protect summary sources before evicting outputs or delivering new steering.
        report = None
        if context.summary is not None:
            context.summary.set_tools(context.model_tools)
            projection = await self.current_projection(context)
            transformed = transform_context(context, self.context_transforms, projection.messages)
            request = request.override(
                messages=to_framework_messages(transformed, context.selection)
            )
            report = await context.summary.prepare(
                messages=request.messages,
                tail=transformed[len(projection.messages) :],
            )
            if report.applied_job_id is not None:
                await self.run_hook(context.scope, "compact")

        # 3. Maintenance may take time. Admit steering only after it succeeds.
        if request.state["turn"] > 1 and not context.steering:
            context.steering = await poll_steering(context)
        pending, context.steering = context.steering, ()
        for message in pending:
            await deliver_input(context, message, "steer")
        await self.run_hook(context.scope, "turn_start")
        if request.state["turn"] > 1:
            await context.events.emit("turn_start", {"turn": request.state["turn"]})
        await self.run_hook(context.scope, "pre_send")

        # 4. Build final messages after all history edits; retain feature transformations.
        return request.override(messages=await self.model_messages(context))

    async def request_model(self, request, handler, started):
        context = request.runtime.context
        state = cast(PycodeState, request.state)
        boundary = f"{context.run_id}:model-{state['turn']}"

        async def record_failure(error):
            partial = context.streamed_text
            await save_message(
                context,
                Message(
                    uuid4().hex,
                    "assistant",
                    (TextBlock(partial),) if partial else (),
                    stop_reason="error" if isinstance(error, ModelError) else "aborted",
                    error=error.code if isinstance(error, ModelError) else "model_interrupted",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    model_selection=context.selection,
                    provider_metadata={
                        "pycode_run_id": context.run_id,
                        "pycode_turn": state["turn"],
                    },
                ),
            )

        async def record_partial_retry(error):
            if context.streamed_text is not None:
                await record_failure(error)

        try:
            try:
                response = await context.requests.invoke(
                    request, handler, logical_id=boundary, on_retry=record_partial_retry
                )
            except ModelError as error:
                if error.code != "context_overflow" or context.summary is None:
                    raise
                report = await context.summary.prepare(force=True)
                if report.applied_job_id is None:
                    raise
                await self.run_hook(context.scope, "compact")
                request = request.override(messages=await self.model_messages(context))
                response = await context.requests.invoke(
                    request,
                    handler,
                    logical_id=boundary,
                    attempt_offset=context.requests.active_attempt + 1,
                    on_retry=record_partial_retry,
                )
        except (ModelError, asyncio.CancelledError) as error:
            await record_failure(error)
            raise
        return response, request

    async def save_response(self, request, response, started):
        context = request.runtime.context
        state = cast(PycodeState, request.state)
        model_response = (
            response.model_response if isinstance(response, ExtendedModelResponse) else response
        )
        assistant = model_response.result[-1]
        # ModelRequestRunner validates the response once at the model boundary.
        assistant = cast(AIMessage, assistant)
        message = from_assistant(assistant, context.selection, context.session_id, context.run_id)
        message = replace(
            message,
            duration_ms=round((time.monotonic() - started) * 1000),
            provider_metadata={
                **message.provider_metadata,
                "pycode_run_id": context.run_id,
                "pycode_turn": state["turn"],
                "pycode_source_leaf_id": context.model_source_leaf_id,
            },
        )
        await save_message(context, message)
        context.dispatched_calls.clear()
        context.interrupted_calls.clear()
        context.ready_results.clear()
        context.settled_calls.clear()
        await self.run_hook(context.scope, "post_receive", message=message.text)
        if assistant.invalid_tool_calls:
            calls = list(assistant.tool_calls)
            for call in message.tool_calls:
                if call.parse_error:
                    calls.append(
                        {"type": "tool_call", "id": call.id, "name": call.name, "args": {}}
                    )
            normalized = assistant.model_copy(
                update={"tool_calls": calls, "invalid_tool_calls": []}
            )
            updated = replace(model_response, result=[*model_response.result[:-1], normalized])
            if isinstance(response, ExtendedModelResponse):
                return replace(response, model_response=updated)
            return updated
        return response

    # Pi: finishTurn for a reply. Deep Agents decides all tool routing.
    async def aafter_model(self, state, runtime):
        context = runtime.context
        message = await context.last_assistant()
        if message is None:
            raise StorageError("assistant_not_saved", "The model reply was not saved")
        trigger = "tools" if message.tool_calls else "verify"
        phase = transition_phase(state, trigger)
        ended_turn = state.get("ended_turn", 0)
        if not message.tool_calls:
            await end_turn(context, self.run_hook, state["turn"])
            ended_turn = state["turn"]
            context.steering = await poll_steering(context)
        return {
            "phase": phase,
            "ended_turn": ended_turn,
            "final_text": message.text,
            "model_tools": plain_data(context.model_tools),
        }

    # Pi: the outer loop's follow-up check once the agent would stop (verifying).
    # AgentRuntime.invoke enters finished after the graph returns.
    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        context = runtime.context
        if context.summary is not None:
            await context.summary.prepare(wait_for_capacity=False)
        if state.get("reason") is not None:
            return None
        kind, pending = "steer", context.steering
        context.steering = ()
        if not pending:
            context.inputs = await context.durable_inputs.pending(context.run_id)
            kind, pending = take_queued(context)
        if not pending and self.before_finish is not None:
            if await self.before_finish(context, turn=state["turn"]):
                return {"jump_to": "model"}
        if not pending:
            context.inputs = await context.durable_inputs.finish_boundary(context.run_id)
            kind, pending = take_queued(context)
        if not pending:
            return {"phase": transition_phase(state, "finish"), "reason": "completed"}
        if state["turn"] >= context.max_turns:
            return {"phase": transition_phase(state, "finish"), "reason": "turn_limit"}
        framework = []
        for message in pending:
            await deliver_input(context, message, kind)
            framework.append(to_framework(message, context.selection))
        return {"messages": framework, "jump_to": "model"}
