"""PromptService joins feature sections in list order and snapshots only changed prompts."""

import pytest

from pycode.context.prompts import PromptService
from pycode.kernel import EventStream, Feature, SessionHeader, SessionScope, StorageError
from pycode.session import SqliteSessionStore
from tests.support.requests import TestTiming


def scope(tmp_path, history=None):
    return SessionScope(
        "s1", tmp_path, tmp_path / "data", history, EventStream("s1", "r1", ()), False, "lead"
    )


class Section(Feature):
    def __init__(self, name, *texts):
        self.name = name
        self.texts = texts
        self.calls = 0

    def prompt_sections(self, session):
        self.calls += 1
        return self.texts


async def test_missing_project_rules_and_bounded_utf8_errors(tmp_path):
    prompts = PromptService(scope(tmp_path), ())
    assert "工作目录：" in (await prompts.read())["text"]
    (tmp_path / "agent.md").write_bytes(b"x" * 65537)
    with pytest.raises(StorageError, match="64 KiB"):
        await prompts.read()
    (tmp_path / "agent.md").write_bytes(b"\xff")
    with pytest.raises(StorageError, match="UTF-8"):
        await prompts.read()


async def test_feature_sections_follow_the_list_and_refresh_appends_only_changes(tmp_path):
    history = SqliteSessionStore(
        tmp_path / "s1", SessionHeader("s1", str(tmp_path), "t"), TestTiming()
    )
    first, second = Section("memory", "MEMORY_INDEX"), Section("skills", "", "SKILL_INDEX")
    prompts = PromptService(scope(tmp_path, history), (first, second), suffix="SUFFIX")
    try:
        text = (await prompts.refresh())["text"]
        assert text.index("SUFFIX") < text.index("MEMORY_INDEX") < text.index("SKILL_INDEX")
        assert "\n\n\n\n" not in text
        await prompts.refresh()
        first.texts = ("NEW_MEMORY_INDEX",)
        assert "NEW_MEMORY_INDEX" in (await prompts.refresh())["text"]
        snapshot = await history.snapshot()
        assert [entry.type for entry in snapshot.entries].count("system_prompt") == 2
        assert first.calls == second.calls == 3
    finally:
        await history.aclose()
