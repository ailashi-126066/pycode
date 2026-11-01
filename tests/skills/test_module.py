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
async def test_skill_receipts_follow_successful_result_persistence():
    recorded = []

    class Recorder:
        async def record(self, path, result, entry_id):
            recorded.append((path, entry_id))

    extension = SkillsToolExtension(Recorder(), SimpleNamespace(dispatch=False))
    call = ToolCall("c", "read", {"path": "/skills/user/review/SKILL.md"})
    context = ToolContext("s", "r", "o", "/workspace", agent_id="lead")
    await extension.before_call(context, call)
    override = extension.after_tool_call(call, ToolResult("c", "read", "body"))
    assert override == {"details": {"protected_content": True}}
    result = ToolResult("c", "read", "body", details=override["details"])
    assert recorded == []
    await extension.after_persist(context, call, result, "entry")
    assert recorded == [(call.arguments["path"], "entry")]


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
