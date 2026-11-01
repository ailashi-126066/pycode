"""Classify public SDK exceptions without storing keys, headers or error bodies."""

import math
from email.utils import parsedate_to_datetime
from typing import Any

import anthropic
import httpx
import httpx2
import openai

from pycode.kernel import SystemTiming, clone_value
from pycode.models.domain.requests import ModelFailure


class ProviderFailureClassifier:
    def __init__(self, timing: SystemTiming) -> None:
        self._timing = timing

    def classify(self, error: Exception) -> ModelFailure | None:
        if isinstance(error, openai.LengthFinishReasonError):
            completion = error.completion
            metadata: dict[str, Any] = {"provider_request_id": completion.id}
            if completion.usage is not None:
                metadata["usage"] = completion.usage.model_dump(mode="json")
            return ModelFailure(
                "provider_output_limit",
                "Provider output reached its limit",
                False,
                metadata=metadata,
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
            metadata = {"status_code": status}
            usage = body.get("usage") or body.get("token_usage")
            if isinstance(usage, dict):
                metadata["usage"] = clone_value(usage)
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
        if metadata.get("pycode_stream_terminal") is False:
            return ModelFailure(
                "provider_connection",
                "Provider stream ended before completion",
                True,
                metadata=clone_value(metadata),
            )
        if metadata.get("pycode_provider_status") == "failed":
            return ModelFailure(
                "provider_response_error",
                "Provider response failed",
                False,
                metadata=clone_value(metadata),
            )
        reason = metadata.get("finish_reason") or metadata.get("stop_reason")
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
