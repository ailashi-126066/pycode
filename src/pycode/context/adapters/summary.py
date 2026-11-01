# Source: https://github.com/earendil-works/pi/blob/42a3497d03ad17e308a2299fa824727894f2c0ec/packages/coding-agent/src/core/compaction/compaction.ts
# Adaptation: one metered BaseChatModel request, without an extra Agent loop.
"""An independent framework-owned summary request cannot execute tools."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from langchain.agents.middleware import ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableBranch, RunnableLambda, RunnablePassthrough

from pycode.context.domain.summaries import SummaryCompletion, SummarySource
from pycode.kernel import ModelError, json_dumps
from pycode.models import RequestRecord, model_tool_text


class SummaryRequestRecords(Protocol):
    async def list_attempts(self) -> tuple[RequestRecord, ...]: ...


@dataclass(frozen=True)
class SummaryMessages:
    messages: list[BaseMessage]


SUMMARY_SYSTEM = (
    "You create context checkpoints for another coding agent. Summarize the supplied historical "
    "conversation rather than continuing it. Do not call tools. "
    "Preserve the user's goal, constraints, "
    "decisions, completed work, failures, unfinished tasks, file changes, and stable output IDs. "
    "Do not claim an uncertain tool succeeded. "
    "Use headings Goal, Constraints, Progress, Decisions, "
    "Next Steps, and Critical Context. A turn-prefix section describes the task being split; "
    "preserve its requirements so the retained assistant/tool messages can continue that task."
)


SUMMARY_PROMPT = ChatPromptTemplate.from_messages(
    [SystemMessage(content=SUMMARY_SYSTEM), ("human", "{conversation}")]
)


def serialize_source(source: SummarySource) -> str:
    sections = ["<conversation>"]
    for index, message in enumerate(source.messages):
        if source.turn_prefix_start == index:
            sections.append("</conversation>\n<turn-prefix>")
        sections.append(f"[{message.role} id={message.id}]")
        if message.text:
            sections.append(message.text)
        for thinking in message.thinking:
            sections.append("[thinking]\n" + thinking.text)
        for call in message.tool_calls:
            sections.append(
                f"[call {call.id} {call.name}] "
                + json_dumps(call.arguments, ensure_ascii=False, sort_keys=True)
            )
        if message.tool_result is not None:
            result = message.tool_result
            path = result.details.get("full_output_path")
            full = f" full_output={path}" if path else ""
            sections.append(f"[result {result.call_id}{full}]\n" + model_tool_text(result))
        for attachment in message.attachments:
            sections.append(
                f"[attachment {attachment.media_type} reference={attachment.reference}]"
            )
    sections.append("</turn-prefix>" if source.turn_prefix_start is not None else "</conversation>")
    sections.append("Create a concise structured checkpoint. Preserve the incomplete task prefix.")
    return "\n".join(sections)


def build_summary_request(text):
    return SummaryMessages(SUMMARY_PROMPT.invoke({"conversation": text}).to_messages())


def validate_summary_response(settled, stop_reason: Callable[[AIMessage], str]):
    response = settled.result[-1]
    if not isinstance(response, AIMessage) or response.tool_calls or response.invalid_tool_calls:
        raise ModelError("summary_tool_call", "Summary attempted a tool call; result rejected")
    if stop_reason(response) != "stop":
        raise ModelError("summary_incomplete", "Summary did not finish normally")
    return response


def clean_summary_text(text):
    text = text.strip()
    if not text:
        raise ModelError("empty_summary", "Summary returned no text")
    if "<summary>" in text and "</summary>" in text:
        text = text.split("<summary>", 1)[1].split("</summary>", 1)[0].strip()
    if not text or ("<analysis>" in text and "<summary>" not in text):
        raise ModelError("empty_summary", "Summary contained only analysis")
    return text


class SummaryWorkflow:
    def __init__(
        self, model: BaseChatModel, *, request_step, stop_reason: Callable[[AIMessage], str]
    ):
        self._model = model
        self._request_step = request_step
        self._stop_reason = stop_reason

    async def run(
        self, source: SummarySource, requests: SummaryRequestRecords
    ) -> SummaryCompletion:
        async def invoke(current):
            response = await self._model.ainvoke(current.messages, stream=True)
            return ModelResponse(result=[response])

        chain = (
            RunnableLambda(serialize_source)
            | RunnableLambda(build_summary_request)
            | self._request_step(requests, invoke, logical_id="summary")
            | RunnableLambda(
                lambda response: validate_summary_response(response, self._stop_reason)
            )
            | StrOutputParser()
            | RunnableLambda(clean_summary_text)
        )
        text = await chain.ainvoke(source)
        usage = tuple(
            record.usage for record in await requests.list_attempts() if record.usage is not None
        )
        return SummaryCompletion(text, usage)


async def prepare_summary(service, check):
    """Generation and adoption are consecutive stages under the service's existing lock."""

    async def generate(current):
        await service.start_generation(current)
        return current

    async def report(current):
        return service.preparation_report(current)

    async def apply(current):
        result = await service.apply_prepared(current)
        return result if result is not None else service.preparation_report(current)

    generation = RunnableBranch(
        (lambda current: current.start_generation, RunnableLambda(generate)),
        RunnablePassthrough(),
    )
    adoption = RunnableBranch(
        (lambda current: current.apply_needed, RunnableLambda(apply)),
        RunnableLambda(report),
    )
    return await (generation | adoption).ainvoke(check)
