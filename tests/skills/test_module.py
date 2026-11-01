"""Optional Skill assembly keeps discovery and approval policy within the feature."""

from types import SimpleNamespace

import pytest

from pycode.kernel import ToolCall, ToolContext, ToolError, ToolResult, ToolSpec
from pycode.skills.framework import SkillsScopeConstraint, SkillsToolExtension
from pycode.skills.module import SkillsModule, SkillsSettings


def test_standalone_mounts_are_readonly_and_project_trust_is_explicit(tmp_path):
    user = tmp_path / "user"
    workspace = tmp_path / "project"
    (user / "skills").mkdir(parents=True)
    (workspace / ".pycode" / "skills").mkdir(parents=True)
    seen = []

    def backend(root, **options):
        seen.append((root, options))
        return SimpleNamespace(receipts={})

    module = SkillsModule(backend_factory=backend)
    settings = SkillsSettings(workspace, user)
    routes = module.routes(settings)
    assert "/skills/user/" in routes
    assert "/skills/project/" not in routes
    assert all(options == {"read_only": True} for _, options in seen)
    assert all(route.read_only for route in routes.values())


def test_skill_extension_never_registers_python_functions():
    extension = SkillsToolExtension(None, None)
    assert not hasattr(extension, "lookup")


@pytest.mark.asyncio
async def test_successful_skill_read_carries_the_skill_in_its_details():
    from pycode.skills.types import SkillEntry

    entry = SkillEntry("review", "Review code", "user", "/x/review/SKILL.md", "hash")

    class Catalog:
        async def entry_at_virtual_path(self, path):
            return entry

    extension = SkillsToolExtension(Catalog(), SimpleNamespace(dispatch=False))
    call = ToolCall("c", "read", {"path": "/skills/user/review/SKILL.md"})
    await extension.before_call(ToolContext("s", "r", "o", "/workspace"), call)
    override = extension.after_tool_call(call, ToolResult("c", "read", "body"))
    assert override is not None
    assert override["details"]["skill"]["name"] == "review"
    assert override["details"]["protected_content"] is True


@pytest.mark.asyncio
async def test_scope_preserves_instruction_reads_but_denies_external_tools(monkeypatch):
    constraint = SkillsScopeConstraint(None)

    async def allowed():
        return {"grep", "SkillSearch"}

    monkeypatch.setattr(constraint, "allowed", allowed)
    context = ToolContext("s", "r", "o", "/workspace")
    await constraint.authorize(
        context,
        ToolSpec("read", "read", {}),
        ToolCall("c", "read", {"path": "/skills/user/x/SKILL.md"}),
    )
    with pytest.raises(ToolError, match="Active Skill scope"):
        await constraint.authorize(
            context, ToolSpec("bash", "shell", {}), ToolCall("b", "bash", {})
        )
