import asyncio

import pytest

from pycode.context import ContextProjector
from pycode.kernel import (
    EventStream,
    Message,
    SessionHeader,
    SessionScope,
    SessionTree,
    ToolCall,
    ToolError,
    ToolSpec,
)
from pycode.session import SqliteSessionStore
from pycode.skills import FileSkillCatalog, GitHubSkillInstaller, Skills, SkillsSession
from pycode.skills.skills import SkillBackend
from pycode.skills.types import active_skills
from tests.skills.test_install import URL, download_transport
from tests.support.requests import TestTiming
from tests.support.skill_loading import read_skill


def store_for(tmp_path):
    return SqliteSessionStore(
        tmp_path / "session", SessionHeader("s1", "/project", "created"), TestTiming()
    )


def scope(workspace, user_data, history, *, read_only=False):
    return SessionScope(
        "s1", workspace, user_data, history, EventStream("s1", "r1", ()), read_only, "lead"
    )


def content(message):
    if message.tool_result is not None:
        return message.tool_result.text
    return message.text


async def write_skill(root, name="example", body="Instructions $ARGUMENTS", allowed="Read"):
    path = root / name / "SKILL.md"

    def write():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: Example task\nallowed-tools: [{allowed}]\n"
            f"---\n{body}",
            encoding="utf-8",
        )

    await asyncio.to_thread(write)
    return path


async def prepare(tmp_path, *, budget=2000):
    root = tmp_path / "skills"
    path = await write_skill(root)
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "Task"))
    service = Skills(
        store,
        FileSkillCatalog((("user", root),)),
        GitHubSkillInstaller(root),
        index_budget=budget,
    )
    await service.refresh()
    return service, store, path


async def test_catalog_stays_metadata_only_and_dispatch_search_is_bounded(tmp_path):
    service, store, _ = await prepare(tmp_path, budget=1)
    try:
        catalog = await service.index()
        assert catalog["dispatch"] and catalog["text"] == ""
        assert "Instructions" not in catalog["text"]
        assert await service.search("missing") == ()
        assert (await service.search("Example", 1))[0]["name"] == "example"
        with pytest.raises(ToolError, match="between 1 and 20"):
            await service.search("", 0)
        assert not active_skills((await store.snapshot()).entries)
    finally:
        await store.aclose()


async def test_reload_keeps_identical_prefix_and_honors_changed_index_budget(tmp_path):
    service, store, path = await prepare(tmp_path)
    try:
        before = await store.snapshot()
        catalog = await service.refresh()
        assert await store.snapshot() == before
        assert not catalog["dispatch"] and "Instructions" not in catalog["text"]
        bounded = Skills(
            store,
            FileSkillCatalog((("user", path.parent.parent),)),
            GitHubSkillInstaller(path.parent.parent),
            index_budget=1,
        )
        changed = await bounded.refresh()
        assert changed["dispatch"] and changed["hash"] == catalog["hash"]
        assert (await store.snapshot()).leaf_id != before.leaf_id
    finally:
        await store.aclose()


async def test_install_refreshes_catalog_but_never_activates_downloaded_skill(tmp_path):
    root = tmp_path / "skills"
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "install"))
    service = Skills(
        store,
        FileSkillCatalog((("user", root),)),
        GitHubSkillInstaller(root, transport=download_transport()),
    )
    try:
        await service.refresh()
        report = await service.install(URL)
        assert report["skill_name"] == "vendor-sample"
        assert (await service.search("sample"))[0]["name"] == "vendor-sample"
        projection = ContextProjector().transform_context(await store.snapshot())
        assert not any(message.text == "Sample SOP" for message in projection.messages)
        assert not active_skills((await store.snapshot()).entries)
        loaded = await read_skill(
            store,
            FileSkillCatalog((("user", root),)),
            "vendor-sample",
        )
        assert "Sample SOP" in loaded["text"]
    finally:
        await store.aclose()


async def test_exact_snapshot_survives_disk_change_restart_and_explicit_reload(tmp_path):
    service, store, path = await prepare(tmp_path)
    catalog = FileSkillCatalog((("user", path.parent.parent),))
    requested = await service.load("example", 'keep "quotes"')
    assert requested["arguments"] == 'keep "quotes"'
    original = await read_skill(store, catalog, "example")
    assert "Instructions $ARGUMENTS" in original["text"]
    await read_skill(store, catalog, "example")
    await write_skill(path.parent.parent, body="New instructions $ARGUMENTS")
    await store.aclose()
    restored = store_for(tmp_path)
    service = Skills(
        restored,
        FileSkillCatalog((("user", path.parent.parent),)),
        GitHubSkillInstaller(path.parent.parent),
    )
    try:
        assert (await service.index())["entries"][0]["content_hash"] == original["content_hash"]
        projected = ContextProjector().transform_context(await restored.snapshot())
        assert any(content(message) == original["text"] for message in projected.messages)
        assert not any(
            message.text.startswith("New instructions") for message in projected.messages
        )
        await service.refresh()
        updated = await read_skill(restored, catalog, "example")
        assert "New instructions $ARGUMENTS" in updated["text"]
        assert updated["content_hash"] != original["content_hash"]
        projected = ContextProjector().transform_context(await restored.snapshot())
        assert sum(content(message) == updated["text"] for message in projected.messages) == 1
        assert any(content(message) == original["text"] for message in projected.messages)
        assert (
            active_skills((await restored.snapshot()).entries)[-1]["content_hash"]
            == updated["content_hash"]
        )
    finally:
        await restored.aclose()


async def test_branch_drops_skills_loaded_on_the_other_branch(tmp_path):
    service, store, path = await prepare(tmp_path)
    try:
        before = (await store.snapshot()).leaf_id
        loaded = await read_skill(
            store,
            FileSkillCatalog((("user", path.parent.parent),)),
            "example",
        )
        assert active_skills((await store.snapshot()).entries)[0]["name"] == "example"
        await store.branch(before)
        snapshot = await store.snapshot()
        assert not active_skills(SessionTree(snapshot.index).path(snapshot.leaf_id))
        projection = ContextProjector().transform_context(snapshot)
        assert not any(message.text == loaded["text"] for message in projection.messages)
    finally:
        await store.aclose()


async def test_resources_are_on_demand_and_scope_rejects_escape(tmp_path):
    service, store, path = await prepare(tmp_path)

    def resource():
        references = path.parent / "references"
        references.mkdir()
        (references / "note.txt").write_text("Resource only", encoding="utf-8")
        (references / "never.py").write_text("raise RuntimeError('never import')", encoding="utf-8")

    await asyncio.to_thread(resource)
    try:
        backend = SkillBackend(path.parent.parent)
        await read_skill(
            store,
            FileSkillCatalog((("user", path.parent.parent),)),
            "example",
        )
        projection = ContextProjector().transform_context(await store.snapshot())
        assert not any("Resource only" in message.text for message in projection.messages)
        result = await backend.aread("/example/references/note.txt")
        assert result.error is None and "Resource only" in result.file_data["content"]
        for relative in ("/../secret", "/example/references/../../secret", "\\secret"):
            result = await backend.aread(relative)
            assert result.error.startswith("outside_workspace")
    finally:
        await store.aclose()


async def test_allowed_tools_intersect_and_are_enforced_by_the_feature_gate(tmp_path):
    root = tmp_path / "data" / "skills"
    await write_skill(root, "example")
    await write_skill(root, "second", allowed="Read, Bash")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "Task"))
    feature = SkillsSession(trusted=False, index_budget=2000)
    await feature.open(scope(tmp_path, tmp_path / "data", store))
    try:
        catalog = FileSkillCatalog((("user", root),))
        await read_skill(store, catalog, "second")
        await feature.prepare_run(None)
        (gate,) = feature.gates(None)
        bash = ToolSpec("bash", "command", {})
        await gate.authorize(None, bash, ToolCall("b1", "bash", {}))
        await read_skill(store, catalog, "example")
        await feature.prepare_run(None)
        with pytest.raises(ToolError) as error:
            await gate.authorize(None, bash, ToolCall("b2", "bash", {}))
        assert error.value.code == "skill_tool_not_allowed"
        read = ToolSpec("read", "read", {}, category="read")
        await gate.authorize(None, read, ToolCall("r1", "read", {"path": "/workspace/test"}))
        names = [spec.name for spec in feature.select_tools(None, (bash, read))]
        assert names == ["read"]
    finally:
        await store.aclose()


async def test_project_sources_require_explicit_trust_and_request_does_not_activate(tmp_path):
    await write_skill(tmp_path / ".agents/skills", "private")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "task"))
    try:
        untrusted = SkillsSession(trusted=False, index_budget=2000)
        await untrusted.open(scope(tmp_path, tmp_path / "data", store))
        entries = (await untrusted.service.index())["entries"]
        assert not any(item["name"] == "private" for item in entries)
        trusted = SkillsSession(trusted=True, index_budget=2000)
        await trusted.open(scope(tmp_path, tmp_path / "data", store))
        await trusted.service.refresh()
        requested = await trusted.service.load("private")
        assert requested["path"].startswith("/skills/project-agents/")
        requested_review = await trusted.service.load("review")
        assert requested_review["path"].startswith("/skills/builtin/")
        assert all(
            item["name"] != "review" for item in active_skills((await store.snapshot()).entries)
        )
    finally:
        await store.aclose()


async def test_disable_model_invocation_skill_is_only_reachable_by_command(tmp_path):
    hidden = tmp_path / "skills" / "manual" / "SKILL.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text(
        "---\nname: manual\ndescription: Manual task\ndisable-model-invocation: true\n---\nBody"
    )
    await write_skill(tmp_path / "skills")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "Task"))
    feature = SkillsSession(trusted=False, index_budget=2000)
    await feature.open(scope(tmp_path, tmp_path, store))
    try:
        await feature.prepare_run(None)
        listing = feature.mounts(None)["/skills/user/"].ls("/")
        assert [entry["path"] for entry in listing.entries] == ["/example"]
        assert [item["name"] for item in await feature.service.search("task")] == ["example"]
        assert (await feature.service.load("manual"))["path"] == "/skills/user/manual/SKILL.md"
    finally:
        await store.aclose()
