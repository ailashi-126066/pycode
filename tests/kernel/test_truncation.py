import json
from pathlib import Path

import pytest

from pycode.kernel import truncate

VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures/tool-output-baselines.json").read_text()
)


@pytest.mark.parametrize("case", VECTORS["truncation"], ids=lambda case: case["name"])
def test_source_truncation_vectors(case):
    content = "\n".join(
        case["prefix"] + str(i).zfill(case.get("pad", 0)) for i in range(1, case["count"] + 1)
    )
    if case.get("trailing_newline"):
        content += "\n"
    result = truncate(content, case["mode"])
    assert result.truncated
    assert result.total_lines == case["total_lines"]
    assert result.output_lines == case["output_lines"]
    assert result.text.splitlines()[0] == case["expected_first"]
    assert result.text.splitlines()[-1] == case["expected_last"]


def test_utf8_byte_limit_does_not_mean_character_limit():
    result = truncate("€" * 30000, "tail")
    assert result.total_bytes == 90000
    assert result.output_bytes <= 50 * 1024
    assert "�" not in result.text
    assert result.partial_line


def test_head_never_returns_a_partial_first_line():
    result = truncate("x" * 60000, "head")
    assert result.text == ""
    assert result.first_line_exceeds_limit


@pytest.mark.parametrize("text", ["", "hello", "hello\n", "\n"])
def test_small_output_is_unchanged(text):
    assert truncate(text, "head").text == text
    assert not truncate(text, "tail").truncated
