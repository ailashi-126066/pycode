"""Upstream vectors adapted to actual rg semantics, scope and cancellation."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from pycode.tools.adapters.backend import PycodeBackend

pytestmark = pytest.mark.requires_rg
VECTORS = json.loads((Path(__file__).parents[2] / "fixtures/search-baselines.json").read_bytes())


def prepare(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name, text in VECTORS["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize("case", VECTORS["grep"])
async def test_source_grep_vectors_use_rg_regex_and_sorted_line_numbers(tmp_path, case):
    root = prepare(tmp_path)
    result = await PycodeBackend(root).agrep(case["pattern"], glob=case.get("include") or None)
    assert result.error is None
    lines = []
    for match in result.matches:
        lines.append(f"{match['path'].lstrip('/')}:{match['line']}:{match['text']}")
    actual = "\n".join(lines) if lines else "No matches found."
    expected = case["expected"]
    if case.get("include") == "unicode.txt":
        # rg counts LF source lines; Python str.splitlines treated U+2028 as a newline.
        expected = "unicode.txt:1:first\u2028needle unicode"
    assert actual == expected


@pytest.mark.parametrize("case", VECTORS["glob"])
async def test_source_glob_vectors_use_real_rg_patterns(tmp_path, case):
    root = prepare(tmp_path)
    result = await PycodeBackend(root).aglob(case["pattern"])
    assert result.error is None
    actual = []
    for match in result.matches:
        actual.append(match["path"].lstrip("/"))
    expected = case["expected"].splitlines()
    if case["pattern"] == "*.py":
        expected = ["a.py", "nested/b.py"]
    elif case["pattern"] == "**":
        expected = ["a.py", "empty.txt", "nested/b.py", "nested/你好.txt", "unicode.txt"]
    elif case["expected"] == "No files matched the pattern.":
        expected = []
    assert actual == expected


async def test_search_rejects_escape_invalid_regex_and_prunes_state_and_owned_data(tmp_path):
    root = prepare(tmp_path)
    for name in (".git", ".pycode", "data"):
        directory = root / name
        directory.mkdir()
        (directory / "private.py").write_text("needle private")
    backend = PycodeBackend(root, excluded_roots=(root / "data",))
    assert (await backend.agrep("private")).matches == []
    assert (await backend.agrep("needle", path="/../outside")).error.startswith("outside_workspace")
    malformed = await backend.agrep("[")
    assert malformed.error and malformed.truncated and malformed.matches == []
    assert (await backend.aglob("../*")).error


async def test_rg_avoids_python_regex_backtracking_and_is_cancellable(tmp_path):
    root = prepare(tmp_path)
    target = root / "slow.txt"
    target.write_text("a" * 200000 + "!")
    backend = PycodeBackend(root)
    result = await asyncio.wait_for(backend.agrep("(a+)+$", glob="slow.txt"), 5)
    assert result.error is None and result.matches == []
    pending = asyncio.create_task(backend.agrep("a", glob="slow.txt"))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_worktree_git_pointer_hidden_default_and_explicit_hidden_directory(tmp_path):
    root = prepare(tmp_path)
    pointer = root / ".git"
    pointer.write_text("needle internal worktree pointer")
    backend = PycodeBackend(root)
    assert (await backend.agrep("internal")).matches == []
    assert not any(".venv" in item["path"] for item in (await backend.aglob("**/*")).matches)
    explicit = await backend.aglob("**/*", path="/.venv")
    assert [item["path"] for item in explicit.matches] == ["/.venv/private.py"]


async def test_outside_directory_link_uses_real_link_without_reading_target(tmp_path):
    root = prepare(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("private marker")
    linked = root / "linked"
    if os.name == "nt":
        process = await asyncio.create_subprocess_exec(
            "cmd.exe",
            "/c",
            "mklink",
            "/J",
            str(linked),
            str(outside),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        assert process.returncode == 0, (output, error)
    else:
        linked.symlink_to(outside, target_is_directory=True)
    backend = PycodeBackend(root)
    assert (await backend.aglob("linked/*.txt")).matches == []
    assert (await backend.agrep("marker", path="/linked")).error.startswith("outside_workspace")


pytestmark = pytest.mark.usefixtures("disclosed_tools")
