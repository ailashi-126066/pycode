import asyncio
from dataclasses import replace

import pytest

from pycode.app.configuration.settings import AppConfig
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.history import EntryDraft
from pycode.kernel.domain.messages import Message, ToolCall
from pycode.kernel.domain.tools import ToolSpec
from pycode.session import SessionCatalogWriteGuard
from pycode.skills import SkillsProjection
from pycode.skills.api import Skills
from pycode.skills.catalog import FileSkillCatalog
from pycode.skills.framework import SkillBackend, SkillsScopeConstraint
from pycode.skills.install import GitHubSkillInstaller
from pycode.skills.module import SkillsModule, SkillsSettings
from pycode.skills.types import active_skills
from pycode.tools.adapters.backend import PycodeBackend
from pycode.tools.module import ToolsModule
from tests.models.services.test_model_control import store_for
from tests.skills.adapters.test_install import URL, download_transport
from tests.support.skill_loading import read_skill


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
        index_budget=budget,
        catalog_write_guard=SessionCatalogWriteGuard(),
    )
    await service.index()
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
            index_budget=1,
            catalog_write_guard=SessionCatalogWriteGuard(),
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
        installer=GitHubSkillInstaller(root, transport=download_transport()),
        catalog_write_guard=SessionCatalogWriteGuard(),
    )
    try:
        await service.index()
        report = await service.install(URL)
        assert report["skill_name"] == "vendor-sample"
        assert (await service.search("sample"))[0]["name"] == "vendor-sample"
        projection = ContextProjector((SkillsProjection(),)).transform_context(
            await store.snapshot()
        )
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
    assert (
        len([entry for entry in (await store.snapshot()).entries if entry.type == "skill_load"])
        == 1
    )
    await write_skill(path.parent.parent, body="New instructions $ARGUMENTS")
    await store.aclose()
    restored = store_for(tmp_path)
    service = Skills(
        restored,
        FileSkillCatalog((("user", path.parent.parent),)),
        catalog_write_guard=SessionCatalogWriteGuard(),
    )
    try:
        assert (await service.index())["entries"][0]["content_hash"] == original["content_hash"]
        projected = ContextProjector((SkillsProjection(),)).transform_context(
            await restored.snapshot()
        )
        assert any(content(message) == original["text"] for message in projected.messages)
        assert not any(
            message.text.startswith("New instructions") for message in projected.messages
        )
        await service.refresh()
        updated = await read_skill(restored, catalog, "example")
        assert "New instructions $ARGUMENTS" in updated["text"]
        assert updated["content_hash"] != original["content_hash"]
        projected = ContextProjector((SkillsProjection(),)).transform_context(
            await restored.snapshot()
        )
        assert sum(content(message) == updated["text"] for message in projected.messages) == 1
        assert any(content(message) == original["text"] for message in projected.messages)
        assert (
            active_skills((await restored.snapshot()).entries)[-1]["content_hash"]
            == updated["content_hash"]
        )
    finally:
        await restored.aclose()


async def test_branch_and_compaction_preserve_only_current_activations(tmp_path):
    service, store, path = await prepare(tmp_path)
    try:
        before = (await store.snapshot()).leaf_id
        loaded = await read_skill(
            store,
            FileSkillCatalog((("user", path.parent.parent),)),
            "example",
        )
        await store.append_message(Message("u2", "user", "Keep this"))
        kept = (await store.snapshot()).leaf_id
        projected = ContextProjector((SkillsProjection(),)).transform_context(
            await store.snapshot()
        )
        await store.append_entry(
            EntryDraft(
                "compaction",
                data={
                    "summary": "Old task",
                    "first_kept_entry_id": kept,
                    "checkpoint": projected.checkpoint,
                },
            )
        )
        snapshot = await store.snapshot()
        # Checkpoint alone is sufficient even when the earlier Skill entry is outside context.
        reduced = replace(
            snapshot,
            entries=tuple(entry for entry in snapshot.entries if entry.type != "skill_load"),
        )
        # Keep the full tree for parent integrity; projection excludes the old entry itself.
        assert active_skills(reduced.entries)[0]["content_hash"] == loaded["content_hash"]
        projection = ContextProjector((SkillsProjection(),)).transform_context(snapshot)
        assert sum(content(message) == loaded["text"] for message in projection.messages) == 1
        await store.branch(before)
        projection = ContextProjector((SkillsProjection(),)).transform_context(
            await store.snapshot()
        )
        assert "skills" not in projection.checkpoint
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
        backend = SkillBackend(PycodeBackend(path.parent.parent, read_only=True))
        await read_skill(
            store,
            FileSkillCatalog((("user", path.parent.parent),)),
            "example",
        )
        projection = ContextProjector((SkillsProjection(),)).transform_context(
            await store.snapshot()
        )
        assert not any("Resource only" in message.text for message in projection.messages)
        result = await backend.aread("/example/references/note.txt")
        assert result.error is None and "Resource only" in result.file_data["content"]
        for relative in ("/../secret", "/example/references/../../secret", "\\secret"):
            result = await backend.aread(relative)
            assert result.error.startswith("outside_workspace")
    finally:
        await store.aclose()


class ParentGate:
    def __init__(self):
        self.calls = []

    async def authorize(self, context, spec, call):
        self.calls.append(spec.name)
        if spec.name == "Bash":
            raise ToolError("approval_required", "Parent policy still applies")


async def test_allowed_tools_intersect_and_cannot_bypass_parent_gate(tmp_path):
    service, store, path = await prepare(tmp_path)
    await write_skill(path.parent.parent, "second", allowed="Read, Bash")
    await service.refresh()
    parent = ParentGate()
    gate = ToolsModule.gate(None, None, custom=parent, scope=SkillsScopeConstraint(store))
    try:
        catalog = FileSkillCatalog((("user", path.parent.parent),))
        await read_skill(store, catalog, "second")
        with pytest.raises(ToolError) as error:
            await gate.authorize(None, ToolSpec("Bash", "command", {}), ToolCall("b1", "Bash", {}))
        assert error.value.code == "approval_required"
        await read_skill(store, catalog, "example")
        with pytest.raises(ToolError) as error:
            await gate.authorize(None, ToolSpec("Bash", "command", {}), ToolCall("b2", "Bash", {}))
        assert error.value.code == "skill_tool_not_allowed" and parent.calls == ["Bash"]
        await gate.authorize(
            None,
            ToolSpec("read_file", "read", {}),
            ToolCall("r1", "read_file", {"file_path": "/workspace/test"}),
        )
        assert parent.calls == ["Bash", "read_file"]
    finally:
        await store.aclose()


def configured_skill_service(config, store):
    module = SkillsModule(backend_factory=ToolsModule.backend)
    settings = SkillsSettings(
        config.workspace, config.user_data, config.project_trusted, config.skill_index_budget
    )
    return module.open_session(
        settings, store, catalog_write_guard=SessionCatalogWriteGuard()
    ).service


async def test_project_sources_require_explicit_trust_and_request_does_not_activate(tmp_path):
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await write_skill(config.workspace / ".agents/skills", "private")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "task"))
    try:
        service = configured_skill_service(config, store)
        assert not any(item["name"] == "private" for item in (await service.index())["entries"])
        trusted = configured_skill_service(replace(config, project_trusted=True), store)
        await trusted.refresh()
        requested = await trusted.load("private")
        assert requested["path"].startswith("/skills/project-agents/")
        requested_review = await trusted.load("review")
        assert requested_review["path"].startswith("/skills/builtin/")
        assert all(
            item["name"] != "review" for item in active_skills((await store.snapshot()).entries)
        )
    finally:
        await store.aclose()
