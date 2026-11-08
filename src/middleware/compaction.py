"""Background compaction: start a summary at 80% of the context window, adopt it at 90%.

Adopting replaces the old messages in graph state with one summary message. The originals
stay in earlier checkpoints, so reverting to before the compaction brings them back.
"""

import asyncio
import contextvars
import json
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware, ExtendedModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import RemoveMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from ..compaction.budget import choose_cut, context_tokens, estimate_text, thresholds
from ..compaction.summarize import summarize


@dataclass
class Job:
    ids: list[str]  # the messages the summary replaces, oldest first
    task: asyncio.Task  # produces the summary message


class CompactionMiddleware(AgentMiddleware):
    def __init__(self, model, *, context_window: int, max_output_tokens: int):
        super().__init__()
        self.model = model  # the agent's own model, called here without tools
        self.start_at, self.adopt_at = thresholds(context_window, max_output_tokens)
        self.max_tokens = min(8192, max_output_tokens)
        # Set by agent.py: runs the pre_compact hooks before a summary is made.
        self.before_summary = None  # async () -> None
        # Estimated system prompt plus tool definitions, refreshed on every model request.
        self.overhead = 0
        # Lives only in this process; a summary lost on exit is generated again when needed.
        self.job: Job | None = None

    async def awrap_model_call(self, request, handler):
        """Around every model request: compact first when needed, then call the model."""
        # The request carries the system prompt and tool definitions; state does not.
        system = request.system_message.text if request.system_message else ""
        tools = json.dumps([convert_to_openai_tool(tool) for tool in request.tools], default=str)
        self.overhead = estimate_text(system + tools)
        messages, writer = list(request.messages), request.runtime.stream_writer

        # 1. Normal case: start a background summary at 80%, adopt it at 90%.
        kept = None
        tokens = context_tokens(messages, self.overhead)
        if tokens >= self.start_at and not self.job_covers(messages):
            self.start_job(messages, writer)
        if tokens >= self.adopt_at and self.job is not None:
            kept = await self.adopt(messages, writer)

        try:
            response = await handler(request.override(messages=kept) if kept else request)
        except ContextOverflowError:
            # 2. The model accepts less than the configured window: compact below the size
            # that just failed and retry once.
            current = kept or messages
            limit = context_tokens(current, self.overhead)
            self.start_job(current, writer, limit)
            kept = await self.adopt(current, writer, limit) if self.job else None
            if kept is None:
                raise
            response = await handler(request.override(messages=kept))

        if kept is None:
            return response
        # Save the compacted history together with the reply: clear state, then add both.
        update = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *kept, *response.result]
        return ExtendedModelResponse(
            model_response=response, command=Command(update={"messages": update})
        )

    def job_covers(self, messages) -> bool:
        """The running summary still describes how this conversation begins (no branch switch)."""
        if self.job is None:
            return False
        return [m.id for m in messages[: len(self.job.ids)]] == self.job.ids

    def start_job(self, messages, writer, limit: int | None = None) -> None:
        if self.job is not None:
            self.job.task.cancel()
            self.job = None
        cut = choose_cut(messages, self.overhead, limit or self.adopt_at)
        if cut is None:
            return
        # An empty context detaches the summary call from the run's streaming callbacks.
        task = asyncio.create_task(self.make_summary(messages, cut), context=contextvars.Context())
        self.job = Job([m.id for m in messages[: cut.index]], task)
        writer({"compaction": "正在生成摘要"})

    async def make_summary(self, messages, cut):
        if self.before_summary is not None:
            await self.before_summary()
        return await summarize(self.model, messages, cut.index, cut.ongoing, self.max_tokens)

    async def adopt(self, messages, writer, limit: int | None = None):
        """Summary plus the kept messages, or None when they still would not fit under limit."""
        job, self.job = self.job, None
        try:
            summary = await job.task
        except Exception as error:
            writer({"compaction": f"摘要生成失败（{type(error).__name__}），保留原历史"})
            return None
        tail = messages[len(job.ids) :]
        info = summary.additional_kwargs["pycode_summary"]
        # Model replies after this id report the compacted context; earlier ones do not.
        info["last_kept_id"] = tail[-1].id if tail else None
        kept = [summary, *tail]
        if context_tokens(kept, self.overhead) >= (limit or self.adopt_at):
            writer({"compaction": "摘要加上保留的消息仍然超限，保留原历史"})
            return None
        writer({"compaction": f"已把前 {info['turns']} 轮对话压缩成摘要"})
        return kept

    async def compact_now(self, agent, config, writer) -> None:
        """Manual compaction (/compact), whatever the current size."""
        messages = (await agent.aget_state(config)).values.get("messages", [])
        if not self.job_covers(messages):
            self.start_job(messages, writer)
        if self.job is None:
            writer({"compaction": "没有可以压缩的内容"})
            return
        kept = await self.adopt(messages, writer)
        if kept is not None:
            await agent.aupdate_state(
                config, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *kept]}
            )
