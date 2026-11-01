# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/utils.ts
# Adaptation: metered BaseChatModel requests without an extra Agent loop; tool results name
# their full_output path; summaries reject empty or analysis-only text.
"""An independent framework-owned summary request cannot execute tools."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from langchain.agents.middleware import ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

from pycode.context.domain.file_ops import format_file_operations, format_loaded_skills
from pycode.context.domain.summaries import SummaryCompletion, SummarySource
from pycode.kernel import Message, ModelError, convert_to_llm, json_dumps
from pycode.models import RequestRecord, model_tool_text


class SummaryRequestRecords(Protocol):
    async def list_attempts(self) -> tuple[RequestRecord, ...]: ...


@dataclass(frozen=True)
class SummaryMessages:
    messages: list[BaseMessage]


# Pi's templates, verbatim (E501 is ignored so the text stays byte-identical).
SUMMARIZATION_SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""  # noqa: E501
SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""  # noqa: E501
UPDATE_SUMMARIZATION_INSTRUCTIONS = """Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""  # noqa: E501
UPDATE_SUMMARIZATION_PROMPT = (
    "The messages above are NEW conversation messages to incorporate into the existing summary "
    "provided in <previous-summary> tags.\n\n" + UPDATE_SUMMARIZATION_INSTRUCTIONS
)
TURN_PREFIX_SUMMARIZATION_PROMPT = """The messages above are earlier context from an ongoing conversation. Later messages are stored separately and do not need to be reconstructed.

Create a concise checkpoint of the user's request and the progress shown above. This checkpoint will be placed before the later messages so the conversation can continue with the necessary context.

## Original Request
[What did the user ask for?]

## Progress So Far
- [Key decisions and work completed in these messages]

## Context Needed to Continue
- [Information from these messages needed to understand the later work]

Only summarize information explicitly present above. Do not infer or recreate later messages."""  # noqa: E501
SUMMARY_PROMPT = ChatPromptTemplate.from_messages(
    [SystemMessage(content=SUMMARIZATION_SYSTEM_PROMPT), ("human", "{prompt}")]
)
TOOL_RESULT_MAX_CHARS = 2000


# Pi: truncateForSummary
def truncate_for_summary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[... {len(text) - max_chars} more characters truncated]"


# Pi: serializeConversation (after convertToLlm); thinking and attachments are omitted.
def serialize_conversation(messages: tuple[Message, ...]) -> str:
    parts = []
    for message in convert_to_llm(messages):
        if message.role == "user":
            if message.text:
                parts.append(f"[User]: {message.text}")
        elif message.role == "assistant":
            calls = [
                call.name
                + "("
                + ", ".join(
                    f"{key}={json_dumps(value, ensure_ascii=False, separators=(',', ':'))}"
                    for key, value in call.arguments.items()
                )
                + ")"
                for call in message.tool_calls
            ]
            if message.text:
                parts.append(f"[Assistant]: {message.text}")
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif message.role == "tool_result":
            result = message.require_tool_result()
            content = model_tool_text(result)
            if content:
                # The full_output path is the only way back to a truncated result.
                path = result.details.get("full_output_path")
                label = f"[Tool result full_output={path}]" if path else "[Tool result]"
                parts.append(f"{label}: {truncate_for_summary(content, TOOL_RESULT_MAX_CHARS)}")
    return "\n\n".join(parts)


# Pi: generateSummaryWithUsage prompt text
def history_prompt(source: SummarySource) -> str:
    prompt = f"<conversation>\n{serialize_conversation(source.messages)}\n</conversation>\n\n"
    if source.previous_summary is not None:
        prompt += f"<previous-summary>\n{source.previous_summary}\n</previous-summary>\n\n"
        return prompt + UPDATE_SUMMARIZATION_PROMPT
    return prompt + SUMMARIZATION_PROMPT


# Pi: generateTurnPrefixSummary prompt text
def turn_prefix_prompt(source: SummarySource) -> str:
    conversation = serialize_conversation(source.turn_prefix_messages)
    return f"# Conversation\n{conversation}\n\n# Instructions\n{TURN_PREFIX_SUMMARIZATION_PROMPT}"


def build_summary_request(prompt):
    return SummaryMessages(SUMMARY_PROMPT.invoke({"prompt": prompt}).to_messages())


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

    async def summarize(self, prompt: str, requests, logical_id: str) -> str:
        async def invoke(current):
            response = await self._model.ainvoke(current.messages, stream=True)
            return ModelResponse(result=[response])

        chain = (
            RunnableLambda(build_summary_request)
            | self._request_step(requests, invoke, logical_id=logical_id)
            | RunnableLambda(
                lambda response: validate_summary_response(response, self._stop_reason)
            )
            | StrOutputParser()
            | RunnableLambda(clean_summary_text)
        )
        return await chain.ainvoke(prompt)

    # Pi: compact
    async def run(
        self, source: SummarySource, requests: SummaryRequestRecords
    ) -> SummaryCompletion:
        if source.turn_prefix_messages:
            history = (
                source.previous_summary
                if source.previous_summary is not None
                else "No prior history."
            )
            if source.messages:
                history = await self.summarize(history_prompt(source), requests, "summary")
            prefix = await self.summarize(
                turn_prefix_prompt(source), requests, "summary-turn-prefix"
            )
            summary = f"{history}\n\n---\n\n**Turn Context (split turn):**\n\n{prefix}"
        else:
            summary = await self.summarize(history_prompt(source), requests, "summary")
        summary += format_file_operations(source.read_files, source.modified_files)
        summary += format_loaded_skills(source.read_files)
        usage = tuple(
            record.usage for record in await requests.list_attempts() if record.usage is not None
        )
        return SummaryCompletion(summary, usage)


async def prepare_summary(service, check):
    """Generation and adoption are consecutive stages under the service's existing lock."""
    if check.start_generation:
        await service.start_generation(check)
    if check.apply_needed:
        result = await service.apply_prepared(check)
        if result is not None:
            return result
    return service.preparation_report(check)
