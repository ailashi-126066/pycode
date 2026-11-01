from pycode.kernel import UsageRecord
from pycode.models.domain.requests import RequestRecord
from pycode.models.domain.usage import aggregate_requests


def record(identifier, purpose="agent", usage=None, status="completed"):
    return RequestRecord(
        identifier, "s", "r", "provider", "model", purpose, 0, status, 1, usage=usage
    )


def test_partial_usage_is_not_a_fabricated_complete_total_and_failure_is_counted():
    measured = UsageRecord("p", "m", "first", input_tokens=10, output_tokens=2, total_tokens=12)
    second = UsageRecord("p", "m", "second", input_tokens=None, output_tokens=0)
    group = aggregate_requests(
        (record("a", usage=measured), record("b", "retry", second, "failed"))
    )
    assert group.request_count == 2 and group.settled_requests == 2
    assert group.tokens["input_tokens"] is None
    assert group.known_tokens["input_tokens"] == 10
    assert group.reported["input_tokens"] == 1
    assert group.tokens["output_tokens"] == 2 and group.reported["output_tokens"] == 2
    assert group.tokens["cache_read_tokens"] is None
    assert group.known_tokens["cache_read_tokens"] is None


def test_started_requests_and_empty_group_have_no_formal_token_measurement():
    group = aggregate_requests((record("a", status="started"),))
    assert group.pending_requests == 1 and group.settled_requests == 0
    assert all(value is None for value in group.tokens.values())
    assert all(value is None for value in aggregate_requests(()).tokens.values())
