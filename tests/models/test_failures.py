import anthropic
import httpx2
import openai
import pytest

from pycode.models.requests import ProviderFailureClassifier
from tests.support.requests import TestTiming


def status_error(status, *, sdk=openai, headers=None, body=None):
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://example.invalid"), headers=headers
    )
    return sdk.APIStatusError("error body must not leak fake-secret", response=response, body=body)


@pytest.mark.parametrize("sdk", [openai, anthropic])
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_transient_public_sdk_statuses_are_retryable(sdk, status):
    failure = ProviderFailureClassifier(TestTiming()).classify(status_error(status, sdk=sdk))
    assert failure.retryable
    assert "fake-secret" not in failure.message


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501])
def test_deterministic_statuses_are_not_retried(status):
    assert not ProviderFailureClassifier(TestTiming()).classify(status_error(status)).retryable


@pytest.mark.parametrize(
    "sdk,body",
    [
        (openai, {"error": {"code": "context_length_exceeded", "message": "too long"}}),
        (
            anthropic,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 130000 tokens",
                }
            },
        ),
    ],
)
def test_native_overflow_signals_are_classified_without_persisting_error_body(sdk, body):
    failure = ProviderFailureClassifier(TestTiming()).classify(
        status_error(400, sdk=sdk, body=body)
    )
    assert failure.code == "context_overflow" and not failure.retryable
    assert "130000" not in failure.message and "message" not in failure.metadata


def test_retry_after_seconds_dates_and_invalid_values():
    classifier = ProviderFailureClassifier(TestTiming())
    assert classifier.retry_after("12") == 12
    assert classifier.retry_after("Thu, 01 Jan 1970 00:16:52 GMT") == 12
    assert classifier.retry_after("invalid") is None
    assert classifier.retry_after("nan") is None
    assert classifier.retry_after("-3") == 0


def test_failure_request_id_is_preserved_without_authentication_headers():
    error = status_error(
        429,
        headers={"retry-after": "12", "x-request-id": "provider-1", "Authorization": "fake-secret"},
        body={"message": "fake-secret", "usage": {"input_tokens": 5}},
    )
    failure = ProviderFailureClassifier(TestTiming()).classify(error)
    assert failure.metadata["provider_request_id"] == "provider-1"
    assert "fake-secret" not in repr(failure)
    assert failure.retry_after == 12


def test_unknown_implementation_defects_are_not_classified_as_network_failures():
    assert (
        ProviderFailureClassifier(TestTiming()).classify(ValueError("implementation defect"))
        is None
    )


def test_native_structured_length_failure_keeps_the_request_id_without_response_body():
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion(
        id="completion-1",
        object="chat.completion",
        created=1,
        model="wire",
        choices=[],
        usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    )
    error = openai.LengthFinishReasonError(completion=completion)
    failure = ProviderFailureClassifier(TestTiming()).classify(error)
    assert failure.code == "provider_output_limit" and not failure.retryable
    assert failure.metadata["provider_request_id"] == "completion-1"
    assert failure.metadata == {"provider_request_id": "completion-1"}


def test_native_structured_content_filter_is_terminal_not_a_network_retry():
    failure = ProviderFailureClassifier(TestTiming()).classify(
        openai.ContentFilterFinishReasonError()
    )
    assert failure.code == "provider_content_filter" and not failure.retryable
