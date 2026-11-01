# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/ai/src/utils/retry.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/ai/src/utils/provider-retry.ts
# Adaptation: SDK exception classification, one durable SQLite record per attempt,
# retries on the same model only.
"""Per-attempt accounting and retries around the public LangChain model handler."""

import asyncio
import json
import math
import sqlite3
import time
from dataclasses import dataclass, fields, replace
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import anthropic
import httpx
import httpx2
import openai
from langchain.agents.middleware import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from pycode.kernel import (
    EventStream,
    ModelError,
    ModelSelection,
    Purpose,
    StorageError,
    SystemTiming,
    UsageRecord,
    clone_value,
    json_dumps,
    owned_io,
    plain_data,
)
from pycode.models.messages import normalize_usage
from pycode.models.types import ModelFailure, RequestRecord, RetryPolicy


class ProviderFailureClassifier:
    def __init__(self, timing: SystemTiming) -> None:
        self._timing = timing

    def classify(self, error: Exception) -> ModelFailure | None:
        if isinstance(error, openai.LengthFinishReasonError):
            return ModelFailure(
                "provider_output_limit",
                "Provider output reached its limit",
                False,
                metadata={"provider_request_id": error.completion.id},
            )
        if isinstance(error, openai.ContentFilterFinishReasonError):
            return ModelFailure("provider_content_filter", "Provider output was filtered", False)
        if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
            status = error.status_code
            retryable = status == 429 or status in {500, 502, 503, 504, 529}
            body = error.body if isinstance(error.body, dict) else {}
            nested_error = body.get("error")
            native_error = nested_error if isinstance(nested_error, dict) else body
            native_code = native_error.get("code") or native_error.get("type")
            native_message = native_error.get("message")
            too_long = (
                status == 400
                and isinstance(native_message, str)
                and any(
                    phrase in native_message.lower()
                    for phrase in (
                        "prompt is too long",
                        "input is too long",
                        "maximum context length",
                    )
                )
            )
            if too_long or native_code in {
                "context_length_exceeded",
                "context_window_exceeded",
                "prompt_too_long",
            }:
                code = "context_overflow"
            else:
                code = "provider_rate_limit" if status == 429 else f"provider_http_{status}"
            metadata: dict[str, Any] = {"status_code": status}
            native_id = error.response.headers.get("x-request-id")
            if native_id:
                metadata["provider_request_id"] = native_id
            return ModelFailure(
                code=code,
                message=f"Provider request failed (HTTP {status})",
                retryable=retryable,
                retry_after=self.retry_after(error.response.headers.get("retry-after")),
                metadata=metadata,
            )
        if isinstance(
            error,
            (
                openai.APITimeoutError,
                anthropic.APITimeoutError,
                httpx.TimeoutException,
                httpx2.TimeoutException,
            ),
        ):
            return ModelFailure("provider_timeout", "Provider request timed out", True)
        if isinstance(
            error,
            (
                openai.APIConnectionError,
                anthropic.APIConnectionError,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                httpx2.NetworkError,
                httpx2.RemoteProtocolError,
            ),
        ):
            return ModelFailure("provider_connection", "Provider connection was interrupted", True)
        if isinstance(error, (openai.APIError, anthropic.APIError)):
            body = error.body if isinstance(error.body, dict) else {}
            nested_error = body.get("error")
            error_info = nested_error if isinstance(nested_error, dict) else body
            error_type = error_info.get("type") or error_info.get("code")
            retryable = error_type in {"overloaded_error", "server_error", "network_error"}
            return ModelFailure("provider_stream_error", "Provider stream failed", retryable)
        return None

    def retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self._timing.now()
            except (ValueError, TypeError, OverflowError):
                return None
        if not math.isfinite(seconds):
            return None
        return max(0, seconds)

    def response_failure(self, metadata: dict[str, Any]) -> ModelFailure | None:
        reason = metadata.get("finish_reason") or metadata.get("stop_reason")
        if (
            metadata.get("model_provider") in {"openai", "deepseek", "anthropic"}
            and reason is None
            and metadata.get("status") not in {"completed", "incomplete", "failed", "cancelled"}
        ):
            return ModelFailure(
                "provider_connection",
                "Provider stream ended before its final response",
                True,
                metadata={**metadata, "stream_interrupted": True},
            )
        if reason in {"network_error", "server_error", "overloaded_error"}:
            return ModelFailure(
                "provider_stream_error",
                "Provider stream did not finish",
                True,
                metadata=clone_value(metadata),
            )
        if reason in {"error", "aborted"}:
            return ModelFailure(
                "provider_response_error",
                "Provider response failed",
                False,
                metadata=clone_value(metadata),
            )
        return None


def decode_request(payload):
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {
            field.name for field in fields(RequestRecord)
        }:
            raise ValueError("Unsupported request record fields")
        if data["usage"] is not None:
            data["usage"] = UsageRecord(**data["usage"])
        return RequestRecord(**data)
    except (ValueError, TypeError, KeyError) as error:
        raise StorageError(
            "invalid_request_record", "Invalid or unsupported request record format"
        ) from error


class SQLiteRequestStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS model_requests ("
                "request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "status TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self._connection.commit()
            self._lock = asyncio.Lock()
        except BaseException:
            # No owner receives this store when setup fails; release the handle
            # here rather than relying on a later asynchronous close.
            self._connection.close()
            raise

    async def run_io(self, operation):
        async with self._lock:
            try:
                return await owned_io(operation)
            except sqlite3.Error as error:
                raise StorageError(
                    "request_store_error", "Request database operation failed"
                ) from error

    async def start(self, record: RequestRecord) -> None:
        if record.status != "started" or record.usage is not None:
            raise ValueError("New attempt must be started with no settled usage")
        await self.run_io(lambda: self.write(record, initial=True))

    async def settle(self, record: RequestRecord) -> None:
        if record.status == "started":
            raise ValueError("Settled attempt requires a terminal status")
        await self.run_io(lambda: self.write(record, initial=False))

    def write(self, record: RequestRecord, *, initial: bool) -> None:
        payload = json_dumps(
            plain_data(record), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        previous = self._connection.execute(
            "SELECT status, payload FROM model_requests WHERE request_id = ?", (record.request_id,)
        ).fetchone()
        if previous is None and not initial:
            raise StorageError("missing_request", "Cannot settle an attempt that was not recorded")
        if previous is not None:
            if previous[1] == payload:
                return
            if initial or previous[0] != "started":
                raise StorageError(
                    "request_id_conflict", "Request ID already has different settled content"
                )
        with self._connection:
            self._connection.execute(
                "INSERT INTO model_requests VALUES (?, ?, ?, ?) "
                "ON CONFLICT(request_id) DO UPDATE SET "
                "status=excluded.status, payload=excluded.payload",
                (record.request_id, record.session_id, record.status, payload),
            )

    async def list_requests(self, session_id: str | None = None) -> tuple[RequestRecord, ...]:
        def read():
            query = "SELECT payload FROM model_requests"
            arguments = ()
            if session_id is not None:
                query += " WHERE session_id = ?"
                arguments = (session_id,)
            query += " ORDER BY rowid"
            output = []
            for (payload,) in self._connection.execute(query, arguments):
                output.append(decode_request(payload))
            return tuple(output)

        return await self.run_io(read)

    async def aclose(self) -> None:
        await self.run_io(self._connection.close)

    async def interrupt_unsettled(self, session_id: str, run_id: str) -> None:
        for record in await self.list_requests(session_id):
            if record.run_id == run_id and record.status == "started":
                await self.settle(
                    replace(
                        record,
                        status="interrupted",
                        error_code="process_interrupted",
                        settled_at=time.time(),
                    )
                )


@dataclass(frozen=True)
class ModelMessages:
    messages: list[BaseMessage]


def model_request_step(requests, handler, *, logical_id, on_retry=None):
    """Expose the existing retry/accounting boundary as one LCEL step."""

    attempt_offset = 0

    async def invoke(request):
        nonlocal attempt_offset
        try:
            return await requests.invoke(
                request,
                handler,
                logical_id=logical_id,
                attempt_offset=attempt_offset,
                on_retry=on_retry,
            )
        finally:
            attempt_offset = requests.active_attempt + 1

    return RunnableLambda(invoke, name="pycode_model_request")


class ModelRequestRunner:
    def __init__(
        self,
        store: SQLiteRequestStore,
        classifier: ProviderFailureClassifier,
        timing: SystemTiming,
        policy: RetryPolicy,
        events: EventStream,
        selection: ModelSelection,
        session_id: str,
        run_id: str,
        *,
        agent_id: str = "lead",
        purpose: Purpose = "agent",
    ) -> None:
        self._store = store
        self._classifier = classifier
        self._timing = timing
        self._policy = policy
        self._events = events
        self._selection = selection
        self._session_id = session_id
        self._run_id = run_id
        self._agent_id = agent_id
        self._purpose: Purpose = purpose
        self.active_request_id: str | None = None
        self.active_attempt = 0
        self._invocation_lock = asyncio.Lock()

    @property
    def selection(self):
        return self._selection

    async def invoke(
        self,
        request,
        handler,
        *,
        logical_id: str | None = None,
        attempt_offset: int = 0,
        on_retry=None,
    ):
        # active_request_id names one request at a time; native components may
        # issue concurrent queries within the same job.
        async with self._invocation_lock:
            return await self._invoke(
                request,
                handler,
                logical_id=logical_id,
                attempt_offset=attempt_offset,
                on_retry=on_retry,
            )

    async def _invoke(
        self,
        request,
        handler,
        *,
        logical_id: str | None = None,
        attempt_offset: int = 0,
        on_retry=None,
    ):
        total = self._policy.max_retries + 1
        for index in range(total):
            try:
                return await self.invoke_attempt(
                    request, handler, logical_id, index + attempt_offset
                )
            except ModelError as error:
                if not error.retryable or index >= total - 1:
                    raise
                if on_retry is not None:
                    await on_retry(error)
                # Accounting owns the header-aware delay so Retry-After is honored.
                delay = self._policy.delay(index + 1, error.retry_after)
                await self._events.emit(
                    "retry",
                    {
                        "request_id": error.request_id,
                        "attempt": index + 1,
                        "max_retries": self._policy.max_retries,
                        "delay_seconds": delay,
                        "error_code": error.code,
                    },
                )
                await self._timing.sleep(delay)

    async def invoke_attempt(self, request, handler, logical_id, attempt):
        record = RequestRecord(
            request_id=str(uuid4()),
            session_id=self._session_id,
            run_id=self._run_id,
            provider=self._selection.provider,
            model_id=self._selection.model_id,
            purpose=self._purpose if attempt == 0 else "retry",
            attempt=attempt,
            status="started",
            started_at=self._timing.now(),
            agent_id=self._agent_id,
            logical_id=logical_id,
        )
        await self._store.start(record)
        self.active_request_id = record.request_id
        self.active_attempt = attempt
        failure = None
        try:
            result = await handler(request)
            response = (
                result.model_response if isinstance(result, ExtendedModelResponse) else result
            )
            if not isinstance(response, ModelResponse) or not isinstance(
                response.result[-1], AIMessage
            ):
                raise TypeError("Model handler did not return a completed assistant response")
            assistant = response.result[-1]
            failure = self._classifier.response_failure(assistant.response_metadata)
        except asyncio.CancelledError:
            # Usage is unknown when the stream stops before its final chunk.
            await self._store.settle(
                replace(
                    record,
                    status="interrupted",
                    settled_at=self._timing.now(),
                    error_code="model_interrupted",
                )
            )
            raise
        except Exception as error:
            failure = self._classifier.classify(error)
            if failure is None:
                await self.settle_failure(
                    record,
                    ModelFailure(
                        "model_implementation_error",
                        "Model implementation failed",
                        False,
                    ),
                )
                raise
        finally:
            self.active_request_id = None
        if failure is None:
            metadata = {
                **assistant.response_metadata,
                "pycode_request_id": record.request_id,
                "pycode_request_purpose": record.purpose,
                "pycode_agent_id": record.agent_id,
                "pycode_origin_purpose": self._purpose,
            }
            assistant = assistant.model_copy(update={"response_metadata": metadata}, deep=True)
            usage = normalize_usage(assistant, self._selection, record.session_id, record.run_id)
            completed = replace(
                record,
                status="completed",
                settled_at=self._timing.now(),
                usage=usage,
                provider_request_id=metadata.get("id") or assistant.id,
            )
            # Usage reaches the UI in the assistant's message_end; the record serves /usage.
            await self._store.settle(completed)
            updated = replace(response, result=[*response.result[:-1], assistant])
            return (
                replace(result, model_response=updated)
                if isinstance(result, ExtendedModelResponse)
                else updated
            )
        await self.settle_failure(record, failure)
        raise ModelError(
            failure.code,
            failure.message,
            retryable=failure.retryable,
            retry_after=failure.retry_after,
            request_id=record.request_id,
        )

    async def settle_failure(self, record: RequestRecord, failure: ModelFailure) -> None:
        if failure.metadata.get("stream_interrupted"):
            await self._store.settle(
                replace(
                    record,
                    status="interrupted",
                    settled_at=self._timing.now(),
                    error_code=failure.code,
                )
            )
            return
        metadata = {
            **failure.metadata,
            "pycode_request_id": record.request_id,
            "pycode_request_purpose": record.purpose,
            "pycode_agent_id": record.agent_id,
            "pycode_origin_purpose": self._purpose,
        }
        usage = normalize_usage(
            AIMessage(content="", response_metadata=metadata),
            self._selection,
            record.session_id,
            record.run_id,
        )
        usage = replace(usage, error=failure.code)
        settled = replace(
            record,
            status="failed",
            settled_at=self._timing.now(),
            error_code=failure.code,
            usage=usage,
            provider_request_id=metadata.get("provider_request_id"),
        )
        await self._store.settle(settled)

    async def list_attempts(self):
        return tuple(
            record
            for record in await self._store.list_requests(self._session_id)
            if record.run_id == self._run_id
        )
