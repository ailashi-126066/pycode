# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
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
    deliver_input,
    prepare_model_boundary,
    save_message,
    task_board_messages,
)
from pycode.agent.adapters.state import PycodeState
from pycode.agent.domain.turn_policy import next_turn
from pycode.agent.services.context import RunContext
from pycode.context import board_from_data
from pycode.kernel import (
    EntryDraft,
    Message,
    ModelError,
    SnapshotError,
    StorageError,
    TextBlock,
    plain_data,
    work_graph,
)
from pycode.tools import ProgressGuard, enter_tree

BOARD_EXIT_REMINDER = (
    "The task board is still active. Before finishing, close it with "
    'TaskUpdate(target="board"): completed (evidence for every done_when item), '
    "failed (note what was tried), blocked (note the question for the user) or abandoned."
)


async def poll_steering(context):
    """Pi: getSteeringMessages. Polled at loop start, at each turn end, and after preparation."""
    if context.durable_inputs is not None:
        context.inputs = await context.durable_inputs.pending(context.run_id)
    return context.inputs.take("steer", one_at_a_time=context.one_at_a_time)


async def end_turn(context, bindings):
    """Pi: turn_end, derived from the RunState transition that leaves a model or tool turn."""
    turn = context.run_state.turn
    message = context.assistant
    if message is not None and message.provider_metadata["pycode_turn"] != turn:
        # The turn failed before its reply was saved.
        message = None
    results = [
        item.id
        for item in context.completed
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
    await bindings.run_hook(
        context, "turn_end", message=message.text if message is not None else ""
    )


def blocked_board_note(context):
    """The note of a board the last tool batch blocked; the run then ends to ask the user."""
    for message in reversed(context.completed):
        if message.role == "assistant":
            return None
        result = message.tool_result
        if result is not None and "task_board" in result.details:
            board = result.details["task_board"]
            return board["outcome"] if board["status"] == "blocked" else None
    return None


async def start_progress_guard(context):
    """The run's first workspace tree; later trees come from each call's file_snapshot."""
    context.progress_guard = ProgressGuard()
    if context.executor.snapshots is None:
        return
    try:
        snapshot = await context.executor.snapshots.capture()
    except SnapshotError:
        # No tree (e.g. an unmerged index): the guard waits for a call's file_snapshot.
        return
    if snapshot is not None:
        enter_tree(context.progress_guard, snapshot.tree_id)


def loop_reminder(context) -> tuple[Message, ...]:
    """The first detection is reminded once, in the next request only."""
    guard = context.progress_guard
    if guard.evidence is None or guard.reminded:
        return ()
    evidence, guard.evidence, guard.reminded = guard.evidence, None, True
    text = (
        f"Progress guard: {evidence}. This is not progress; change your approach. "
        "Another repeat ends the run with loop_detected."
    )
    return (Message(uuid4().hex, "system", text),)


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


# Pi's runLoop hooks map onto LangChain middleware: prepareNextTurn ↔ abefore_model,
# prepareRequest ↔ awrap_model_call, finishTurn ↔ aafter_model / aafter_agent.
class HistoryMiddleware(AgentMiddleware[PycodeState, RunContext]):
    state_schema = PycodeState

    def __init__(self, bindings):
        self.bindings = bindings

    async def current_projection(self, context):
        """One projection per model call, reused while the history leaf is unchanged."""

        if context.history_store is None or context.projector is None:
            raise StorageError("history_unavailable", "Projection needs history and a projector")
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
        await start_progress_guard(context)
        await context.events.emit("turn_start", {"turn": 1})
        for message in context.initial_inputs:
            await context.events.emit("message_start", {"message": plain_data(message)})
            await context.events.emit("message_end", {"message": plain_data(message)})
        # Pi polls steering at loop start: the user may have typed while waiting.
        context.steering = await poll_steering(context)

    # Pi: prepareNextTurn. LangGraph's jump_to "end" implements the turn budget stop.
    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        context = runtime.context
        run = context.run_state
        note = blocked_board_note(context)
        if note is not None:
            # A blocked board ends the run with its question; the user answers in a new run.
            run.reason = "needs_input"
            return {"jump_to": "end", "final_text": note}
        if run.turn >= context.max_turns:
            # The last turn has finished; end with turn_limit instead of starting another.
            run.reason = "turn_limit"
            return {"jump_to": "end"}
        if context.progress_guard.evidence is not None and context.progress_guard.reminded:
            # Detected again after the reminder: end the run instead of another request.
            run.reason = "loop_detected"
            await self.fail_board(context, context.progress_guard.evidence)
            return {"jump_to": "end"}
        ended = run.apply("model")
        if ended:
            await end_turn(context, self.bindings)
            context.steering = await poll_steering(context)
        run.turn += 1

        async def maintain_context(current):
            for callback in self.bindings.before_model:
                await callback()
            summary = None
            if current.summary is not None:
                current.summary.set_tools(current.model_tools or current.registry.model_specs())
                summary = await current.summary.prepare()
                if summary.applied_job_id is not None:
                    current.board_full_view = True
                    await self.bindings.run_hook(current, "compact")
            if current.eviction is not None:
                await current.eviction.apply(
                    protected_entry_ids=summary.protected_entry_ids if summary else ()
                )
            return current

        async def project_turn(pending):
            turn = run.turn
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
            elif pending:
                updates["messages"] = [
                    self.bindings.to_framework(message, context.selection) for message in pending
                ]
            return updates

        await maintain_context(context)
        if ended and not context.steering:
            # Compaction may run long: poll again, but only if the turn-end poll took nothing.
            context.steering = await poll_steering(context)
        pending, context.steering = context.steering, ()
        for message in pending:
            await deliver_input(context, message, "steer")
        return await project_turn(pending)

    # Pi: prepareRequest + streamAssistantResponse.
    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        state = cast(PycodeState, request.state)
        boundary = f"{context.run_id}:model-{state['turn']}"
        await self.bindings.run_hook(context, "pre_send")
        reminder = loop_reminder(context)
        if context.history_store is not None:
            projection = await self.current_projection(context)
            board = task_board_messages(context, projection)
            request = request.override(
                messages=self.bindings.to_framework_messages(
                    (*projection.messages, *board, *reminder), context.selection
                )
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
            # Calibration may append tool_set_change; record the leaf this request follows.
            await self.current_projection(context)
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
                context.board_full_view = True
                projection = await self.current_projection(context)
                board = task_board_messages(context, projection)
                request = request.override(
                    messages=self.bindings.to_framework_messages(
                        (*projection.messages, *board, *reminder), context.selection
                    )
                )
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
        assistant = model_response.result[-1]
        if not isinstance(assistant, AIMessage):
            raise ModelError("invalid_model_response", "Model response must end with an AI message")
        message = self.bindings.from_assistant(
            assistant, context.selection, context.session_id, context.run_id
        )
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

    # Pi: finishTurn for a reply. Deep Agents decides all tool routing.
    async def aafter_model(self, state, runtime):
        context = runtime.context
        message = context.assistant
        if message is None:
            raise StorageError("assistant_not_saved", "The model reply was not saved")
        if message.tool_calls:
            context.run_state.apply("tools")
        else:
            context.run_state.apply("verify")
            await end_turn(context, self.bindings)
            context.steering = await poll_steering(context)
        return {
            "final_text": message.text,
            "model_tools": plain_data(context.model_tools),
        }

    # Pi: the outer loop's follow-up check once the agent would stop (verifying).
    # AgentRuntime.invoke enters finished after the graph returns.
    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        context = runtime.context
        if context.summary is not None:
            report = await context.summary.prepare(wait_for_capacity=False)
            if report.applied_job_id is not None:
                context.board_full_view = True
        if context.run_state.reason is not None:
            return None
        kind, pending = "steer", context.steering
        context.steering = ()
        if not pending:
            if context.durable_inputs is not None:
                context.inputs = await context.durable_inputs.pending(context.run_id)
            kind, pending = take_queued(context)
        if not pending:
            reminder = await self.board_exit_reminder(context)
            if reminder is not None:
                return reminder
        if not pending and context.durable_inputs is not None:
            context.inputs = await context.durable_inputs.finish_boundary(context.run_id)
            kind, pending = take_queued(context)
        if not pending:
            return None
        if context.run_state.turn >= context.max_turns:
            # Queued input stays undelivered; the budget ends the run.
            context.run_state.reason = "turn_limit"
            return None
        framework = []
        for message in pending:
            await deliver_input(context, message, kind)
            framework.append(self.bindings.to_framework(message, context.selection))
        return {"messages": framework, "jump_to": "model"}

    async def fail_board(self, context, evidence):
        """11.1 section 9: loop_detected also fails an active board, as a custom record."""
        if context.history_store is None:
            return
        board = (await self.current_projection(context)).checkpoint["task_board"]
        if board is None or board["status"] != "active":
            return
        failed = work_graph.apply(
            board_from_data(board), "board", "fail", note=f"loop_detected: {evidence}"
        )
        await context.history_store.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "task_board", "task_board": plain_data(failed)},
                run_id=context.run_id,
            )
        )

    async def board_exit_reminder(self, context):
        """The end-of-run check: an active board must choose an exit, reminded once per run."""
        if (
            context.history_store is None
            or context.board_checked
            or context.run_state.turn >= context.max_turns
        ):
            return None
        board = (await self.current_projection(context)).checkpoint["task_board"]
        if board is None or board["status"] != "active":
            return None
        context.board_checked = True
        await context.history_store.append_entry(
            EntryDraft("custom_message", data={"text": BOARD_EXIT_REMINDER}, run_id=context.run_id)
        )
        return {"jump_to": "model"}
