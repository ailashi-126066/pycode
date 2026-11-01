"""Local authoring has explicit scope, create/update intent and no implicit model execution."""

import pytest

from pycode.kernel import ToolError
from tests.skills.test_skills import prepare


def definition(body="First version"):
    return "---\nname: authored\ndescription: A local Skill\n---\n" + body + "\n"


async def test_create_read_prepare_and_update(tmp_path):
    skills, store, _ = await prepare(tmp_path)
    try:
        created = await skills.create("authored", definition())
        assert (tmp_path / "skills/authored/SKILL.md").read_text() == definition()
        document = await skills.read("authored")
        assert document.entry.content_hash == created["revision"]
        before = await store.snapshot()
        request = await skills.load("authored", "review this")
        assert request["arguments"] == "review this"
        assert (await store.snapshot()).leaf_id == before.leaf_id
        updated = await skills.update("authored", definition("Second version"))
        assert updated["revision"] != created["revision"]
        with pytest.raises(ToolError) as error:
            await skills.update("missing", definition().replace("authored", "missing"))
        assert error.value.code == "unknown_skill"
        with pytest.raises(ToolError, match="exists"):
            await skills.create("authored", definition())
        assert "Second version" in (await skills.read("authored")).body
    finally:
        await store.aclose()


async def test_authoring_cannot_escape_or_write_builtin_sources(tmp_path):
    skills, store, _ = await prepare(tmp_path)
    try:
        with pytest.raises(ToolError):
            await skills.create("../escape", definition())
        with pytest.raises(ToolError):
            await skills.create("authored", definition(), scope="builtin")
        external = tmp_path / "external"
        external.mkdir()
        (tmp_path / "skills/authored").symlink_to(external, target_is_directory=True)
        with pytest.raises(ToolError, match="Linked"):
            await skills.create("authored", definition())
        assert not (external / "SKILL.md").exists()
    finally:
        await store.aclose()
