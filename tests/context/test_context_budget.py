import json
from pathlib import Path

import pytest

from pycode.context.budget import InputBudget

VECTORS = json.loads((Path(__file__).parents[1] / "fixtures/compaction-baselines.json").read_text())


@pytest.mark.parametrize("case", VECTORS["budgets"], ids=lambda case: case["name"])
def test_source_background_trigger_vectors(case):
    if case.get("invalid"):
        with pytest.raises(ValueError):
            InputBudget.create(case["window"], case["reserve"])
        return
    budget = InputBudget.create(case["window"], case["reserve"])
    # Preserve the original source vector; the local product policy changes only
    # its 85% case to the final product's exact 80% threshold.
    expected = case["expected_trigger"]
    if case["name"] == "85 percent cache trigger":
        expected = 160000
    assert budget.cache_trigger == expected
    if "expected_limit" in case:
        assert budget.input_limit == case["expected_limit"]
        assert budget.cache_trigger < budget.input_limit < case["window"] * 0.85
