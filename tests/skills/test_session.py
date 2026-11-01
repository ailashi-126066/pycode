"""SkillsSession keeps discovery, mounts and the allowed-tools scope inside the feature."""

import json
from types import SimpleNamespace

import pytest

from pycode.kernel import Event, ToolCall, ToolError, ToolResult, ToolSpec
from pycode.skills import SkillsSession
from pycode.skills.types import SkillEntry, SkillSelection
from tests.skills.test_skills import scope, store_for, write_skill


async def opened(tmp_path, *, trusted=False, read_only=False):
    store = store_for(tmp_path)
    feature = SkillsSession(trusted=trusted, index_budget=2000)
    await feature.open(scope(tmp_path / "project", tmp_path / "user", store, read_only=read_only))
    return feature, store


async def test_mounts_are_read_only_and_project_trust_is_explicit(tmp_path):
    (tmp_path / "user" / "skills").mkdir(parents=True)
    (tmp_path / "project" / ".pycode" / "skills").mkdir(parents=True)
    for trusted in (False, True):
        feature, store = await opened(tmp_path, trusted=trusted)
        try:
            await feature.prepare_run(None)
            mounts = feature.mounts(None)
            assert "/skills/user/" in mounts
            assert ("/skills/project/" in mounts) is trusted
            assert all(route.read_only for route in mounts.values())
            written = mounts["/skills/user/"].write("/x/SKILL.md", "x")
            assert written.error is not None
            assert (await store.snapshot()).entries[-1].type == "skill_catalog"
        finally:
            await store.aclose()


async def test_read_only_sessions_only_get_skill_search(tmp_path):
    feature, store = await opened(tmp_path, read_only=True)
    try:
        run = SimpleNamespace(session=SimpleNamespace(read_only=True))
        assert [tool.spec.name for tool in feature.tools(run)] == ["SkillSearch"]
        run.session.read_only = False
        assert [tool.spec.name for tool in feature.tools(run)] == [
            "SkillSearch",
            "InstallSkill",
            "SaveSkill",
        ]
    finally:
        await store.aclose()


async def test_successful_skill_read_carries_the_skill_and_narrows_the_scope(tmp_path):
    feature, store = await opened(tmp_path)
    entry = SkillEntry(
        "review", "Review code", "user", "/x/review/SKILL.md", "hash", allowed_tools=("Grep",)
    )

    class Catalog:
        async def entry_at_virtual_path(self, path):
            return entry

    feature.catalog = Catalog()
    try:
        call = ToolCall("c", "read", {"path": "/skills/user/review/SKILL.md"})
        assert await feature.before_tool_call(None, ToolSpec("read", "read", {}), call) is None
        override = await feature.after_tool_call(None, call, ToolResult("c", "read", "body"))
        assert override["details"]["skill"]["name"] == "review"
        assert override["details"]["protected_content"] is True
        (gate,) = feature.gates(None)
        # Instruction reads stay possible; tools outside allowed-tools are denied.
        await gate.authorize(None, ToolSpec("read", "read", {}), call)
        await gate.authorize(None, ToolSpec("grep", "grep", {}), ToolCall("g", "grep", {}))
        with pytest.raises(ToolError, match="Active Skill scope"):
            await gate.authorize(None, ToolSpec("bash", "shell", {}), ToolCall("b", "bash", {}))
        failed = ToolResult("d", "read", "missing", is_error=True)
        again = ToolCall("d", "read", {"path": "/skills/user/review/SKILL.md"})
        await feature.before_tool_call(None, ToolSpec("read", "read", {}), again)
        assert await feature.after_tool_call(None, again, failed) is None
        other = ToolCall("e", "read", {"path": "/workspace/a.py"})
        assert await feature.after_tool_call(None, other, ToolResult("e", "read", "a")) is None
    finally:
        await store.aclose()


async def test_skill_search_end_selects_the_found_skills_when_dispatching(tmp_path):
    await write_skill(tmp_path / "user" / "skills", "example")
    feature, store = await opened(tmp_path)
    try:
        await feature.prepare_run(None)
        path = "/skills/user/example/SKILL.md"
        feature.selection = SkillSelection(True, (path,))
        found = json.dumps([{"name": "example", "path": path}])
        event = Event(
            1,
            "s1",
            "r1",
            "tool_execution_end",
            {
                "tool_call_id": "c",
                "tool_name": "SkillSearch",
                "result": {"text": found},
                "is_error": False,
            },
        )
        await feature.on_event(event)
        assert feature.selection.paths == (path,) and feature.selection.revision == 1
    finally:
        await store.aclose()
