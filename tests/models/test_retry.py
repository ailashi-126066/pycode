import json
from pathlib import Path

import pytest

from pycode.models.types import RetryPolicy

VECTORS = json.loads((Path(__file__).parents[1] / "fixtures/pi-retry.json").read_text())


def test_upstream_capped_delay_vector():
    case = VECTORS["cases"][2]
    policy = RetryPolicy(base_delay=case["base_delay"], max_delay=case["max_delay"])
    assert [policy.delay(i) for i in range(1, 5)] == case["expected_delays"]


def test_server_retry_after_is_respected_even_above_backoff_cap():
    assert RetryPolicy(base_delay=1, max_delay=8).delay(1, retry_after=12) == 12


@pytest.mark.parametrize("retries", [-1, 4, 99])
def test_at_most_three_retries_are_permitted(retries):
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=retries)
