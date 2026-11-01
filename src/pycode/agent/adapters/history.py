"""Deep Agents boundary extracted for sequential, readable ownership."""

import asyncio
import time
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

from langchain.agents.middleware import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from pycode.agent.adapters.boundaries import (
    attach_task_context,
    deliver_input,
    prepare_model_boundary,
    save_message,
)
from pycode.agent.adapters.state import PycodeState
from pycode.agent.adapters.task_output import decode_task_output
from pycode.agent.domain.turn_policy import next_turn
from pycode.agent.services.context import RunContext
from pycode.context import apply_task_change, empty_tasks, task_evidence
from pycode.kernel import Message, ModelError, SessionTree, TextBlock, plain_data

MAX_OUTPUT_CONTINUATIONS = 3
OUTPUT_CONTINUATION_PROMPT = (
    "Your previous JSON response was cut off by the output limit. Return a new complete JSON "
    "object following the task_action/message schema, not a trailing JSON fragment. The previous "
    "task_action was not applied: restate its intended update if needed. Continue only the "
    "message text after the already visible reply; do not repeat earlier reply text, summarize "
    "it, or mention this instruction."
)


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


class HistoryMiddleware(AgentMiddleware[PycodeState, RunContext]):
    state_schema = PycodeState

    def __init__(self, bindings):
        self.bindings = bindings

    async def continue_truncated_output(self, context, state):
        """Resume a length-truncated response instead of ending the turn cut off."""

        index = state.get("output_continuations", 0)
        message = Message(
            f"{context.run_id}:continue-{index}",
            "user",
            OUTPUT_CONTINUATION_PROMPT,
            provider_metadata={"pycode_output_continuation": True},
        )
        await deliver_input(context, message, "continuation")
        return {
            "messages": [self.bindings.to_framework(message, context.selection)],
            "outcome": "running",
            "jump_to": "model",
            "output_continuations": index + 1,
        }

    async def current_projection(self, context):
        """One projection per model call, reused while the history leaf is unchanged."""

        assert context.history_store is not None and context.projector is not None
        snapshot = await context.history_store.snapshot()
        cached = context.model_projection
        if cached is None or cached.leaf_id != snapshot.leaf_id:
            cached = context.projector.transform_context(snapshot)
            context.model_projection = cached
        context.model_source_leaf_id = snapshot.leaf_id
        return cached

    async def abefore_agent(self, state, runtime):
        context = runtime.context
        await self.bindings.run_hook(context, "run_start")
        await context.events.emit("agent_start", {})
        await context.events.emit("turn_start", {"turn": 1})
        for message in context.initial_inputs:
            await context.events.emit("message_start", {"message": plain_data(message)})
            await context.events.emit("message_end", {"message": plain_data(message)})

    async def abefore_model(self, state, runtime):
        context = runtime.context

        async def maintain_context(current):
            for callback in self.bindings.before_model:
                await callback()
            summary = None
            if current.summary is not None:
                current.summary.set_tools(current.model_tools or current.registry.model_specs())
                summary = await current.summary.prepare()
                if summary.applied_job_id is not None:
                    await self.bindings.run_hook(current, "compact")
            if current.eviction is not None:
                await current.eviction.apply(
                    protected_entry_ids=summary.protected_entry_ids if summary else ()
                )
            return current

        async def deliver_steering(current):
            if current.team is not None:
                await current.team.flush_inbox(current)
            if current.durable_inputs is not None:
                current.inputs = await current.durable_inputs.pending(current.run_id)
            pending = current.inputs.take("steer", one_at_a_time=current.one_at_a_time)
            for message in pending:
                await deliver_input(current, message, "steer")
            return pending

        async def project_turn(pending):
            turn = state.get("turn", 0) + 1
            await self.bindings.run_hook(context, "turn_start")
            if turn > 1:
                await context.events.emit("turn_start", {"turn": turn})
            updates: dict[str, Any] = {"turn": turn}
            if context.history_store is not None:
                projection = await self.current_projection(context)
                messages: list[BaseMessage] = [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *self.bindings.to_framework_messages(projection.messages, context.selection),
                ]
                updates.update(messages=messages, branch_leaf_id=projection.leaf_id)
                updates["task_state"] = plain_data(projection.checkpoint["task_state"])
            elif pending:
                updates["messages"] = [
                    self.bindings.to_framework(message, context.selection) for message in pending
                ]
            return updates

        await maintain_context(context)
        pending = await deliver_steering(context)
        return await project_turn(pending)

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        state = cast(PycodeState, request.state)
        boundary = f"{context.run_id}:model-{state['turn']}"
        await self.bindings.run_hook(context, "pre_send")
        projection = None
        if context.history_store is not None:
            projection = await self.current_projection(context)
            request = request.override(
                messages=self.bindings.to_framework_messages(projection.messages, context.selection)
            )
        tools = list(request.tools)
        names = set()
        for tool in tools:
            if not isinstance(tool, dict):
                names.add(tool.name)
        for spec in context.registry.model_specs():
            if spec.name not in names:
                tools.append(self.bindings.make_framework_tool(spec, context))
        request = request.override(
            tools=tools, model_settings={**request.model_settings, "stream": True}
        )
        if not any(
            extension.prepares_model_boundary for extension in self.bindings.framework_extensions
        ):
            request = await prepare_model_boundary(request, self.bindings)
        if context.history_store is not None:
            projection = await self.current_projection(context)
        request = attach_task_context(request, projection)
        model_step = self.bindings.model_request_step(
            context.requests, handler, logical_id=boundary
        )
        started = time.monotonic()
        try:
            try:
                response = await model_step.ainvoke(request)
            except ModelError as error:
                if error.code != "context_overflow" or context.summary is None:
                    raise
                report = await context.summary.prepare(force=True)
                if report.applied_job_id is None:
                    raise
                projection = await self.current_projection(context)
                request = request.override(
                    messages=self.bindings.to_framework_messages(
                        projection.messages, context.selection
                    )
                )
                request = attach_task_context(request, projection)
                response = await model_step.ainvoke(request)
        except (ModelError, asyncio.CancelledError) as error:
            # Pi keeps a failed reply (stopReason error|aborted) and transform_messages skips it.
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
            raise
        model_response = (
            response.model_response if isinstance(response, ExtendedModelResponse) else response
        )
        if isinstance(model_response, AIMessage):
            model_response = ModelResponse(result=[model_response])
        if context.history_store is not None:
            projection = await self.current_projection(context)
        assistant = model_response.result[-1]
        if not isinstance(assistant, AIMessage):
            raise ModelError("invalid_model_response", "Model response must end with an AI message")
        message = self.bindings.from_assistant(
            assistant, context.selection, context.session_id, context.run_id
        )
        path = ()
        if context.history_store is not None:
            snapshot = await context.history_store.snapshot()
            path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        message = decode_task_output(
            message,
            projection.checkpoint["task_state"]
            if projection is not None
            else state.get("task_state", empty_tasks()),
            task_evidence(path, messages=context.completed),
        )
        message = replace(
            message,
            duration_ms=round((time.monotonic() - started) * 1000),
            provider_metadata={
                **message.provider_metadata,
                "pycode_run_id": context.run_id,
                "pycode_turn": state["turn"],
                "pycode_source_leaf_id": context.model_source_leaf_id,
                "pycode_task_context_tokens": context.task_context_tokens,
                "pycode_summary_config_hash": context.summary.config_hash(projection)
                if context.summary is not None and projection is not None
                else None,
            },
        )
        await save_message(context, message)
        context.dispatched_calls.clear()
        context.interrupted_calls.clear()
        context.ready_results.clear()
        context.settled_calls.clear()
        context.turn_end_sent = False
        await self.bindings.run_hook(context, "post_receive", message=message.text)
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

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        # This hook records a boundary only. Deep Agents decides all tool routing.
        context = runtime.context
        message = context.assistant
        assert message is not None
        for callback in self.bindings.after_model:
            await callback()
        outcome = "running" if message.tool_calls else "completed"
        if message.stop_reason in {"error", "aborted"}:
            outcome = message.stop_reason
        elif message.stop_reason == "length" and not message.tool_calls:
            outcome = "output_limit"
        if not message.tool_calls:
            await context.events.emit("turn_end", {"turn": state["turn"], "message_id": message.id})
            await self.bindings.run_hook(context, "turn_end", message=message.text)
        updates = {
            "outcome": outcome,
            "final_text": message.text,
            "model_tools": plain_data(context.model_tools),
            "disclosed_tool_names": plain_data(context.disclosed_tool_names),
            "task_state": apply_task_change(
                state.get("task_state", empty_tasks()),
                message.provider_metadata["pycode_task_change"],
            )
            if message.provider_metadata.get("pycode_task_change") is not None
            else state.get("task_state", empty_tasks()),
        }
        action = message.provider_metadata.get("pycode_task_action")
        if (
            action is not None
            and action["relation"] == "switch"
            and action["task_id"] != state.get("task_state", empty_tasks())["active_task_id"]
        ):
            updates.update(outcome="running", jump_to="model")
        return updates

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        context = runtime.context
        if context.team is not None:
            await context.team.flush_inbox(context)
        if context.summary is not None:
            await context.summary.prepare(wait_for_capacity=False)
        if (
            state.get("outcome") == "output_limit"
            and state.get("output_continuations", 0) < MAX_OUTPUT_CONTINUATIONS
        ):
            return await self.continue_truncated_output(context, state)
        if context.durable_inputs is not None:
            context.inputs = await context.durable_inputs.pending(context.run_id)
        kind, pending = take_queued(context)
        if not pending:
            await self.bindings.run_hook(context, "run_end", message=state.get("final_text", ""))
            if context.durable_inputs is not None:
                context.inputs = await context.durable_inputs.finish_boundary(context.run_id)
                kind, pending = take_queued(context)
        if pending:
            framework = []
            for message in pending:
                await deliver_input(context, message, kind)
                framework.append(self.bindings.to_framework(message, context.selection))
            return {"messages": framework, "outcome": "running", "jump_to": "model"}
        await context.events.emit("agent_end", {"reason": state.get("outcome", "completed")})
