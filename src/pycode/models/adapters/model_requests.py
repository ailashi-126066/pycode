"""Per-attempt accounting and retries around the public LangChain model handler."""

import asyncio
from dataclasses import dataclass, replace
from uuid import uuid4

from langchain.agents.middleware import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from pycode.kernel import EventStream, ModelError, ModelSelection, Purpose, SystemTiming
from pycode.models.adapters.messages import normalize_usage
from pycode.models.domain.requests import ModelFailure, RequestRecord, RetryPolicy
from pycode.models.ports.models import ModelFailureClassifier, ModelObservation, RequestStore


@dataclass(frozen=True)
class ModelMessages:
    messages: list[BaseMessage]


def model_request_step(requests, handler, *, logical_id):
    """Expose the existing retry/accounting boundary as one LCEL step."""

    async def invoke(request):
        return await requests.invoke(request, handler, logical_id=logical_id)

    return RunnableLambda(invoke, name="pycode_model_request")


class ModelRequestRunner:
    def __init__(
        self,
        store: RequestStore,
        classifier: ModelFailureClassifier,
        timing: SystemTiming,
        policy: RetryPolicy,
        events: EventStream,
        selection: ModelSelection,
        session_id: str,
        run_id: str,
        *,
        agent_id: str = "lead",
        purpose: Purpose = "agent",
        observation: ModelObservation | None = None,
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
        self._observation = observation
        self.active_request_id: str | None = None
        self.active_attempt = 0
        self._invocation_lock = asyncio.Lock()

    @property
    def selection(self):
        return self._selection

    async def invoke(self, request, handler, *, logical_id: str | None = None):
        # A Runner and its SDK observation belong to one request at a time.
        # Native components may issue concurrent queries within the same job.
        async with self._invocation_lock:
            return await self._invoke(request, handler, logical_id=logical_id)

    async def _invoke(self, request, handler, *, logical_id: str | None = None):
        offset = 0
        if logical_id is not None:
            previous = [
                record
                for record in await self._store.list_requests(self._session_id)
                if record.logical_id == logical_id
                and record.run_id == self._run_id
                and record.provider == self._selection.provider
                and record.model_id == self._selection.model_id
            ]
            if previous:
                offset = max(record.attempt for record in previous) + 1
        attempts = 0

        async def metered_attempt(current):
            nonlocal attempts
            index = attempts
            attempts += 1
            return await self.invoke_attempt(current, handler, logical_id, offset + index)

        total = self._policy.max_retries + 1
        for index in range(total):
            try:
                return await metered_attempt(request)
            except ModelError as error:
                if not error.retryable or index >= total - 1:
                    raise
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
        if self._observation is not None:
            self._observation.begin()
            reasoning = tuple(
                message.additional_kwargs.get("reasoning_content")
                for message in request.messages
                if isinstance(message, AIMessage)
            )
            self._observation.prepare_reasoning(reasoning)
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
            response_metadata = dict(assistant.response_metadata)
            if self._observation is not None:
                response_metadata.update(self._observation.snapshot())
            failure = self._classifier.response_failure(response_metadata)
        except asyncio.CancelledError:
            await self.settle_failure(
                record,
                ModelFailure("model_interrupted", "Model request cancelled", False),
                status="interrupted",
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
            if self._observation is not None:
                metadata.update(self._observation.snapshot())
            assistant = assistant.model_copy(update={"response_metadata": metadata}, deep=True)
            usage = normalize_usage(assistant, self._selection, record.session_id, record.run_id)
            completed = replace(
                record,
                status="completed",
                settled_at=self._timing.now(),
                usage=usage,
                provider_request_id=metadata.get("provider_response_id") or assistant.id,
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

    async def settle_failure(
        self, record: RequestRecord, failure: ModelFailure, *, status="failed"
    ) -> None:
        metadata = {
            **failure.metadata,
            "pycode_request_id": record.request_id,
            "pycode_request_purpose": record.purpose,
            "pycode_agent_id": record.agent_id,
            "pycode_origin_purpose": self._purpose,
        }
        if self._observation is not None:
            observed = self._observation.snapshot()
            if status == "interrupted":
                observed["pycode_usage_complete"] = False
            metadata.update(observed)
            if isinstance(failure.metadata.get("usage"), dict):
                metadata["pycode_provider_usage"] = failure.metadata["usage"]
                metadata["pycode_usage_complete"] = True
        usage = normalize_usage(
            AIMessage(content="", response_metadata=metadata),
            self._selection,
            record.session_id,
            record.run_id,
        )
        usage = replace(usage, error=failure.code)
        settled = replace(
            record,
            status=status,
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
