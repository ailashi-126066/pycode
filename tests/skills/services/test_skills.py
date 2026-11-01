import asyncio
from dataclasses import replace

import pytest

from pycode.app.configuration.settings import AppConfig
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import SessionTree
from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.messages import Message, ToolCall
from pycode.kernel.domain.tools import ToolSpec
from pycode.session import SessionCatalogWriteGuard
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
        projection = ContextProjector(()).transform_context(await store.snapshot())
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
        catalog_write_guard=SessionCatalogWriteGuard(),
    )
    try:
        assert (await service.index())["entries"][0]["content_hash"] == original["content_hash"]
        projected = ContextProjector(()).transform_context(await restored.snapshot())
        assert any(content(message) == original["text"] for message in projected.messages)
        assert not any(
            message.text.startswith("New instructions") for message in projected.messages
        )
        await service.refresh()
        updated = await read_skill(restored, catalog, "example")
        assert "New instructions $ARGUMENTS" in updated["text"]
        assert updated["content_hash"] != original["content_hash"]
        projected = ContextProjector(()).transform_context(await restored.snapshot())
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
        projection = ContextProjector(()).transform_context(snapshot)
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
        projection = ContextProjector(()).transform_context(await store.snapshot())
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
        if spec.name == "bash":
            raise ToolError("approval_required", "Parent policy still applies")


async def test_allowed_tools_intersect_and_cannot_bypass_parent_gate(tmp_path):
    service, store, path = await prepare(tmp_path)
    await write_skill(path.parent.parent, "second", allowed="Read, Bash")
    await service.refresh()
    parent = ParentGate()
    # The default chain still runs after the custom gate; read_only keeps it sandbox-free.
    gate = ToolsModule.gate(
        None, None, read_only=True, custom=parent, scope=SkillsScopeConstraint(store)
    )
    try:
        catalog = FileSkillCatalog((("user", path.parent.parent),))
        await read_skill(store, catalog, "second")
        with pytest.raises(ToolError) as error:
            await gate.authorize(None, ToolSpec("bash", "command", {}), ToolCall("b1", "bash", {}))
        assert error.value.code == "approval_required"
        await read_skill(store, catalog, "example")
        with pytest.raises(ToolError) as error:
            await gate.authorize(None, ToolSpec("bash", "command", {}), ToolCall("b2", "bash", {}))
        assert error.value.code == "skill_tool_not_allowed" and parent.calls == ["bash"]
        await gate.authorize(
            None,
            ToolSpec("read", "read", {}, category="read"),
            ToolCall("r1", "read", {"path": "/workspace/test"}),
        )
        assert parent.calls == ["bash", "read"]
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


async def test_disable_model_invocation_skill_is_only_reachable_by_command(tmp_path):
    hidden = tmp_path / "skills" / "manual" / "SKILL.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text(
        "---\nname: manual\ndescription: Manual task\ndisable-model-invocation: true\n---\nBody"
    )
    await write_skill(tmp_path / "skills")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "Task"))
    module = SkillsModule(backend_factory=ToolsModule.backend)
    session = module.open_session(
        SkillsSettings(tmp_path, tmp_path), store, catalog_write_guard=SessionCatalogWriteGuard()
    )
    try:
        run = await session.prepare_run()
        listing = run.routes["/skills/user/"].ls("/")
        assert [entry["path"] for entry in listing.entries] == ["/example"]
        assert [item["name"] for item in await session.service.search("task")] == ["example"]
        assert (await session.service.load("manual"))["path"] == "/skills/user/manual/SKILL.md"
    finally:
        await store.aclose()
