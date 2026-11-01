# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/utils.ts
# 参照 MewCode local mewcode/context/summary_cache.py; SHA-256:
# e291f27c5264240e6a535b4ec997e0958cec665f5be1e36f0f9f5f7b08621db0
# Adaptation: a fixed summary prefix captured apart from the appended tail; immutable checked
# summary objects; metered BaseChatModel requests without an extra Agent loop; an owned
# background task; append-only safe adoption.
"""Background summary production never mutates the active history or graph."""

import asyncio
import contextvars
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain.agents.middleware import ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from pydantic import TypeAdapter

from ..kernel import (
    DomainError,
    EntryDraft,
    EventStream,
    Message,
    ModelError,
    ModelSelection,
    ProjectedContext,
    SessionEntry,
    SessionSnapshot,
    SessionTree,
    StorageError,
    atomic_write,
    clone_value,
    convert_to_llm,
    data_hash,
    json_dumps,
    owned_io,
    plain_data,
)
from ..models import model_tool_text
from .budget import ContextMeter
from .compaction import (
    FileOperations,
    compute_file_lists,
    extract_file_ops_from_message,
    find_cut_point,
    format_file_operations,
    format_loaded_skills,
    valid_kept_boundary,
)
from .projector import ContextProjector
from .types import (
    SummaryCheck,
    SummaryCompletion,
    SummaryObject,
    SummaryReport,
    SummarySettings,
    SummarySource,
)

if TYPE_CHECKING:
    from ..session import SqliteSessionStore


# Pi: prepareCompaction
def capture_source(
    snapshot: SessionSnapshot,
    projection: ProjectedContext,
    *,
    job_id: str,
    selection: ModelSelection,
    config_hash: str,
    keep_recent_tokens: int,
    tokens_before: int,
) -> SummarySource | None:
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    by_id = {entry.id: entry for entry in path}
    entries = tuple(by_id[item.entry_id] for item in projection.entries)
    previous_index = next(
        (
            index
            for index, item in enumerate(projection.entries)
            if entries[index].type == "compaction" and item.messages
        ),
        -1,
    )
    start = previous_index + 1
    cut = find_cut_point(entries, projection, start, keep_recent_tokens)
    index = cut.first_kept_index
    while index > start and not valid_kept_boundary(path, entries[index].id):
        index -= 1
    if index <= start:
        return None
    split = cut.split_turn and start <= cut.turn_start_index < index
    history_end = cut.turn_start_index if split else index

    def summarized(begin, end):
        return tuple(
            message
            for item, entry in zip(projection.entries[begin:end], entries[begin:end], strict=True)
            if entry.type != "compaction"
            for message in item.messages
            if message.role != "system"
        )

    messages = summarized(start, history_end)
    turn_prefix = summarized(history_end, index) if split else ()
    if not messages and not turn_prefix:
        return None
    file_ops = FileOperations()
    previous_summary = None
    if previous_index >= 0:
        previous = entries[previous_index]
        previous_summary = previous.data["summary"]
        details = previous.data["details"]
        file_ops.read.update(details["read_files"])
        file_ops.edited.update(details["modified_files"])
    for message in (*messages, *turn_prefix):
        extract_file_ops_from_message(message, file_ops)
    read_files, modified_files = compute_file_lists(file_ops)
    included = projection.entries[:index]
    visible = tuple(item for item in included if any(m.role != "system" for m in item.messages))
    return SummarySource(
        job_id,
        snapshot.header.session_id,
        path[-1].id,
        tuple(item.entry_id for item in visible),
        entries[index].id,
        entries[previous_index].id if previous_index >= 0 else None,
        data_hash([plain_data(item) for item in visible]),
        config_hash,
        previous_summary,
        clone_value(messages),
        clone_value(turn_prefix),
        tuple(read_files),
        tuple(modified_files),
        tokens_before,
        clone_value(selection),
    )


def valid_source(
    source: SummarySource, snapshot: SessionSnapshot, projection: ProjectedContext, config_hash: str
) -> bool:
    if source.session_id != snapshot.header.session_id or source.config_hash != config_hash:
        return False
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    previous = next((entry.id for entry in reversed(path) if entry.type == "compaction"), None)
    if previous != source.previous_compaction_id or source.first_kept_entry_id not in {
        e.id for e in path
    }:
        return False
    visible = tuple(
        item for item in projection.entries if any(m.role != "system" for m in item.messages)
    )
    prefix = visible[: len(source.source_entry_ids)]
    if tuple(item.entry_id for item in prefix) != source.source_entry_ids:
        return False
    return data_hash([plain_data(item) for item in prefix]) == source.prefix_hash


class FileSummaryStore:
    def __init__(self, directory: Path):
        self._directory = directory
        self._lock = asyncio.Lock()

    def path(self, job_id: str, suffix: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise StorageError("invalid_summary_id", "Invalid summary object ID")
        return self._directory / f"{job_id}.{suffix}.json"

    async def run_io(self, action):
        async with self._lock:
            try:
                return await owned_io(action)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise StorageError(
                    "summary_storage_error", "Summary object storage failed"
                ) from error

    def publish(self, path: Path, value: dict):
        envelope = {"value": value, "sha256": data_hash(value)}
        encoded = json_dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if path.exists():
            if path.read_text(encoding="utf-8") != encoded:
                raise StorageError("summary_object_conflict", "Immutable summary object changed")
            return
        atomic_write(path, encoded.encode("utf-8"))

    def read(self, path: Path, expected_type):
        envelope = json.loads(path.read_bytes())
        if envelope["sha256"] != data_hash(envelope["value"]):
            raise StorageError("summary_corrupt", "Summary content checksum verification failed")
        return TypeAdapter(expected_type).validate_json(json_dumps(envelope["value"]), strict=True)

    async def save_source(self, source: SummarySource) -> None:
        await self.run_io(
            lambda: self.publish(self.path(source.job_id, "source"), plain_data(source))
        )

    async def save_result(self, result: SummaryObject) -> None:
        def save():
            source = self.read(self.path(result.job_id, "source"), SummarySource)
            if data_hash(plain_data(source)) != result.source_hash:
                raise StorageError(
                    "summary_origin_conflict", "Result has a different source snapshot"
                )
            self.publish(self.path(result.job_id, "result"), plain_data(result))

        await self.run_io(save)

    async def load(self, job_id: str):
        def load():
            if self.path(job_id, "invalidated").exists():
                return None
            path = self.path(job_id, "source")
            if not path.exists():
                return None
            source = self.read(path, SummarySource)
            result_path = self.path(job_id, "result")
            result = self.read(result_path, SummaryObject) if result_path.exists() else None
            if source.job_id != job_id or (
                result is not None
                and (result.job_id != job_id or result.source_hash != data_hash(plain_data(source)))
            ):
                raise StorageError(
                    "summary_origin_conflict", "Summary identity verification failed"
                )
            return source, result

        return await self.run_io(load)

    async def invalidate(self, job_id: str, reason: str) -> None:
        def invalidate():
            path = self.path(job_id, "invalidated")
            if not path.exists():
                self.publish(path, {"job_id": job_id, "reason": reason})

        await self.run_io(invalidate)


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
    async def run(self, source: SummarySource, requests) -> SummaryCompletion:
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


class ConfiguredSummaryGenerator:
    """Owns one background model per summary job; reconciles requests a lost job left open."""

    def __init__(self, provider, model_factory, records_factory, workflow_factory):
        self.provider = replace(
            provider, supports_tools=False, max_output_tokens=min(provider.max_output_tokens, 8192)
        )
        self.model_factory = model_factory
        self.records_factory = records_factory
        self.workflow_factory = workflow_factory

    async def reconcile_lost(self, source):
        records = self.records_factory()
        try:
            await records.interrupt_unsettled(source.session_id, source.job_id)
        finally:
            await records.aclose()

    async def generate(self, source):
        owner = await self.model_factory(self.provider, source.session_id, source.job_id, "summary")
        try:
            return await self.workflow_factory(owner.model).run(source, owner.requests)
        finally:
            await owner.aclose()


class SummaryService:
    def __init__(
        self,
        history: "SqliteSessionStore",
        projector: ContextProjector,
        objects: FileSummaryStore,
        generator: ConfiguredSummaryGenerator,
        settings: SummarySettings,
        events: EventStream,
        *,
        on_compacted: Callable[[SessionSnapshot], None] | None = None,
    ):
        self._history = history
        self._projector = projector
        self._objects = objects
        self._generator = generator
        self._settings = settings
        self._meter = ContextMeter(settings)
        self._events = events
        self._on_compacted = on_compacted
        self._source = None
        self._result = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def configure(
        self, settings: SummarySettings, generator: ConfiguredSummaryGenerator, events: EventStream
    ):
        if self._closed:
            raise RuntimeError("Cannot reconfigure a closed summary service")
        self._settings = settings
        self._meter = ContextMeter(settings)
        self._generator = generator
        self._events = events

    def checkpoint(self, projection):
        checkpoint = clone_value(projection.checkpoint)
        system = [
            message.text
            for message in projection.messages
            if message.role == "system"
            and message.provider_metadata.get("context_config_system_prompt", True)
        ]
        checkpoint.update(
            system_prompt="\n\n".join(system) if system else None,
            tools=[
                plain_data(spec)
                for spec in sorted(self._settings.tools, key=lambda spec: spec.name)
            ],
            selection=plain_data(self._settings.selection),
        )
        return checkpoint

    def set_tools(self, tools):
        self._settings = replace(self._settings, tools=tools)
        self._meter = ContextMeter(self._settings)

    def config_hash(self):
        # Only the numbers that shape the cut and its budget (11.1 section 35): switching to a
        # model with the same window keeps a prepared summary. Checkpoint state (task_board,
        # plan, mcp, skills, prompt, tools) is taken again at adoption, so it is not hashed.
        return data_hash(
            {
                "context_window": self._settings.budget.window,
                "max_output_tokens": self._settings.budget.output_reserve,
                "keep_recent_tokens": self._settings.keep_recent_tokens,
            }
        )

    def estimate(self, snapshot, projection, tail=()):
        return self._meter.estimate(snapshot, projection, tail)

    async def restore(self, snapshot, projection, fingerprint):
        if self._source is not None:
            return
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        for entry in reversed(path):
            if entry.type != "custom" or entry.data.get("kind") != "summary_job":
                continue
            try:
                loaded = await self._objects.load(entry.data["job_id"])
            except StorageError as error:
                await self._objects.invalidate(entry.data["job_id"], "corrupt")
                await self._events.emit(
                    "compaction",
                    {"job_id": entry.data["job_id"], "status": "invalid", "error_code": error.code},
                )
                continue
            if loaded is None:
                continue
            source, result = loaded
            if result is None:
                await self._generator.reconcile_lost(source)
                await self._objects.invalidate(source.job_id, "lost_executor")
                continue
            if result.status == "ready" and valid_source(source, snapshot, projection, fingerprint):
                self._source, self._result = source, result
                return
            if result.status == "ready":
                await self._objects.invalidate(source.job_id, "source_or_configuration_changed")

    async def invalidate(self, reason: str):
        source = self._source
        if source is not None:
            await self._objects.invalidate(source.job_id, reason)
        task = self._task
        if task is not None:
            await self.collect(cancel=True)
        self._source = None
        self._result = None
        self._task = None

    async def collect(self, *, cancel: bool):
        if self._task is None:
            return
        if cancel and not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            if not cancel:
                raise

    async def produce(self, source, generator):
        source_hash = data_hash(plain_data(source))
        try:
            completion = await generator.generate(clone_value(source))
            if not completion.text.strip():
                raise ModelError("empty_summary", "Summary model returned empty text")
            result = SummaryObject(
                source.job_id, source_hash, "ready", completion.text, completion.usage
            )
        except asyncio.CancelledError:
            result = SummaryObject(
                source.job_id, source_hash, "interrupted", error_code="summary_cancelled"
            )
            await self._objects.save_result(result)
            raise
        except DomainError as error:
            result = SummaryObject(source.job_id, source_hash, "failed", error_code=error.code)
        await self._objects.save_result(result)
        if self._source is not None and self._source.job_id == source.job_id:
            self._result = result
        await self._events.emit(
            "compaction",
            {"job_id": source.job_id, "status": result.status, "error_code": result.error_code},
        )

    async def prepare(
        self, *, manual=False, force=False, wait_for_capacity=True, allow_apply=True, tail=()
    ):
        """Generation and adoption are consecutive stages under one lock.

        ``tail`` holds the messages services append to each request, so the estimate counts them.
        """
        async with self._lock:
            check = await self.inspect_preparation(
                manual, force, wait_for_capacity, allow_apply, tail
            )
            if check.start_generation:
                await self.start_generation(check)
            if check.apply_needed:
                result = await self.apply_prepared(check)
                if result is not None:
                    return result
            return self.preparation_report(check)

    async def inspect_preparation(self, manual, force, wait_for_capacity, allow_apply, tail):
        if self._closed:
            raise RuntimeError("Summary service is closed")
        snapshot = await self._history.snapshot()
        projection = self._projector.transform_context(snapshot)
        fingerprint = self.config_hash()
        tokens = self.estimate(snapshot, projection, tail)
        full = tokens >= self._settings.budget.input_limit or force
        if self._source is not None and not valid_source(
            self._source, snapshot, projection, fingerprint
        ):
            await self._objects.invalidate(self._source.job_id, "source_or_configuration_changed")
            await self.collect(cancel=True)
            self._source = self._result = self._task = None
        await self.restore(snapshot, projection, fingerprint)
        if (
            manual
            and self._source is not None
            and self._result is not None
            and self._result.status == "failed"
        ):
            await self._objects.invalidate(self._source.job_id, "manual_retry")
            self._source = self._result = self._task = None
        trigger = manual or force or tokens >= self._settings.budget.cache_trigger
        return SummaryCheck(
            snapshot,
            projection,
            fingerprint,
            tokens,
            self._source is None and trigger,
            allow_apply and (manual or full),
            manual,
            wait_for_capacity,
        )

    async def start_generation(self, check):
        snapshot = check.snapshot
        projection = check.projection
        fingerprint = check.config_hash
        source = capture_source(
            snapshot,
            projection,
            job_id=uuid4().hex,
            selection=self._settings.selection,
            config_hash=fingerprint,
            keep_recent_tokens=self._settings.keep_recent_tokens,
            tokens_before=check.tokens,
        )
        if source is not None:
            await self._objects.save_source(source)
            await self._history.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": "summary_job",
                        "job_id": source.job_id,
                        "prefix_hash": source.prefix_hash,
                        "config_hash": source.config_hash,
                    },
                )
            )
            self._source = source
            self._task = asyncio.create_task(
                self.produce(source, self._generator),
                name="pycode-summary-" + source.job_id,
                context=contextvars.Context(),
            )
            await self._events.emit("compaction", {"job_id": source.job_id, "status": "generating"})

    async def apply_prepared(self, check):
        if self._task is not None and (check.manual or check.wait_for_capacity):
            await asyncio.shield(self._task)
        if (
            self._result is None
            and self._source is not None
            and self._task is not None
            and self._task.done()
        ):
            loaded = await self._objects.load(self._source.job_id)
            if loaded is not None:
                self._result = loaded[1]
        if self._result is not None and self._result.status == "ready":
            return await self.adopt()
        if check.manual or check.wait_for_capacity:
            code = self._result.error_code if self._result is not None else "no_summary_boundary"
            raise ModelError(
                code or "summary_failed", "Context capacity insufficient; history preserved"
            )
        return None

    def preparation_report(self, check):
        if self._task is not None and self._task.done() and not self._task.cancelled():
            self._task.result()
        status = (
            self._result.status
            if self._result is not None
            else "generating"
            if self._task is not None
            else "idle"
        )
        protected = (
            self._source.source_entry_ids
            if self._source is not None and status in {"ready", "generating"}
            else ()
        )
        return SummaryReport(check.tokens, protected_entry_ids=protected, status=status)

    async def adopt(self):
        snapshot = await self._history.snapshot()
        projection = self._projector.transform_context(snapshot)
        source, result = self._source, self._result
        if source is None or result is None:
            raise ModelError("summary_not_ready", "No completed summary is available to adopt")
        identifier = "compaction_" + source.job_id
        data = {
            "summary": result.text,
            "first_kept_entry_id": source.first_kept_entry_id,
            # Taken at adoption: changes made while the summary ran are already in this state.
            "checkpoint": self.checkpoint(projection),
            "summary_job_id": source.job_id,
            "prefix_hash": source.prefix_hash,
            "config_hash": source.config_hash,
            "usage": [plain_data(item) for item in result.usage],
            # Pi: CompactionEntry.tokensBefore and details{readFiles, modifiedFiles}
            "tokens_before": source.tokens_before,
            "details": {
                "read_files": list(source.read_files),
                "modified_files": list(source.modified_files),
            },
        }
        entry = SessionEntry(identifier, snapshot.leaf_id, "compaction", "summary", data=data)
        candidate = replace(snapshot, entries=(*snapshot.entries, entry), leaf_id=identifier)
        after = self._projector.transform_context(candidate)
        tokens = self.estimate(candidate, after)
        if tokens >= self._settings.budget.input_limit:
            await self._objects.invalidate(source.job_id, "tail_outgrew_retention")
            self._source = self._result = self._task = None
            raise ModelError(
                "compaction_capacity",
                "Summary and recent tail still exceed capacity; history preserved",
            )
        await self._history.append_entry(EntryDraft("compaction", id=identifier, data=data))
        if self._on_compacted is not None:
            self._on_compacted(await self._history.snapshot())
        await self._events.emit(
            "compaction", {"job_id": source.job_id, "status": "applied", "context_tokens": tokens}
        )
        await self._objects.invalidate(source.job_id, "adopted")
        self._source = self._result = self._task = None
        return SummaryReport(tokens, source.job_id, status="applied")

    async def aclose(self):
        if self._closed:
            return
        await self.collect(cancel=True)
        self._closed = True

    async def cancel_background(self):
        await self.collect(cancel=True)
        if self._source is not None:
            loaded = await self._objects.load(self._source.job_id)
            if loaded is not None and loaded[1] is not None and loaded[1].status == "ready":
                self._result = loaded[1]
                self._task = None
                return
        self._source = self._result = self._task = None
