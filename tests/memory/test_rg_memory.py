"""A giant matching line has a bounded Python preview rather than a JSON submatch expansion."""

import tracemalloc

import pytest

from pycode.tools.adapters.backend import PycodeBackend

pytestmark = pytest.mark.requires_rg


async def test_rg_large_line_is_bounded_even_when_regex_has_millions_of_matches(tmp_path):
    (tmp_path / "large.txt").write_bytes(b"x" * (8 * 1024 * 1024) + b"\n")
    backend = PycodeBackend(tmp_path)
    tracemalloc.start()
    try:
        result = await backend.agrep("x", max_count=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.error is None and result.truncated
    assert len(result.matches) == 1
    assert len(result.matches[0]["text"].encode()) <= 50 * 1024
    assert peak < 4 * 1024 * 1024
