"""Team tool schemas, mandatory merge approval, and inherited policy boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pycode.app.composition.team import child_config
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.messages import ToolCall
from pycode.kernel.domain.tools import ToolContext, ToolSpec
from pycode.sandbox.adapters.seatbelt import RequiredSandbox, UnavailableSandbox
from pycode.sandbox.domain.sandbox import SandboxSettings
from pycode.team.tools import child_tools, lead_tools
from pycode.tools.adapters.permissions import TeamToolGate
from pycode.tools.adapters.schema import JsonSchemaValidator
from pycode.tools.services.permissions import NoOpToolGate, WorkerBoundaryGate


def test_team_tools_have_valid_schemas_and_separate_child_capabilities():
    member = SimpleNamespace(agent_id="lead", team=None)
    validator = JsonSchemaValidator()
    lead = lead_tools(member, None)
    child = child_tools(member)
    for tool in (*lead, *child):
        validator.check_schema(tool.spec.input_schema)
    assert {tool.spec.name for tool in child} == {
        "send_message",
        "list_agents",
        "list_tasks",
        "update_task",
    }
    assert next(tool for tool in lead if tool.spec.name == "merge_agent").spec.category == "write"
    assert all(
        tool.spec.execution_mode == "sequential"
        for tool in lead
        if tool.spec.name in {"spawn_agent", "wait_agents"}
    )


async def test_every_merge_has_mandatory_interrupt_even_with_auto_plan_and_custom_grant(
    monkeypatch,
):
    answers = []

    async def permission(question):
        answers.append(question)
        return {"operation_id": question["operation_id"], "decision": "approve"}

    monkeypatch.setattr("pycode.tools.adapters.permissions.permission_answer", permission)
    preview = {"preview_id": "tree-and-target", "fingerprint": "tree"}
    team = SimpleNamespace(previews={}, merge_preview=AsyncMock(return_value=preview))
    parent = AsyncMock()
    constraints = NoOpToolGate()
    gate = TeamToolGate(
        team, parent, constraints, SimpleNamespace(guard=AsyncMock(return_value=True))
    )
    spec = next(tool.spec for tool in lead_tools(None, None) if tool.spec.name == "merge_agent")
    call = ToolCall("merge", "merge_agent", {"id": "12345678"})
    for operation in ("merge-1", "merge-2"):
        await gate.authorize(ToolContext("lead", "run", operation, "/project"), spec, call)
    assert len(answers) == 2
    assert all(answer["approval_details"]["merge"] == preview for answer in answers)
    parent.authorize.assert_not_called()


async def test_custom_denial_and_changed_preview_prevent_merge_before_side_effect(monkeypatch):
    mandatory = AsyncMock()
    monkeypatch.setattr("pycode.tools.adapters.permissions.permission_answer", mandatory)
    team = SimpleNamespace(
        previews={}, merge_preview=AsyncMock(return_value={"fingerprint": "new"})
    )
    deny = SimpleNamespace(authorize=AsyncMock(side_effect=ToolError("custom_denied", "denied")))
    gate = TeamToolGate(team, NoOpToolGate(), deny)
    context = ToolContext("lead", "run", "op", "/project")
    spec = ToolSpec("merge_agent", "merge", {}, source="team", category="write")
    call = ToolCall("merge", "merge_agent", {"id": "12345678"})
    with pytest.raises(ToolError, match="denied"):
        await gate.authorize(context, spec, call)
    mandatory.assert_not_called()
    gate.constraints = NoOpToolGate()
    team.previews["op"] = {"fingerprint": "old"}
    with pytest.raises(ToolError) as error:
        await gate.authorize(context, spec, call)
    assert error.value.code == "merge_preview_stale"
    mandatory.assert_not_called()


async def test_worker_boundary_keeps_custom_denials_and_requires_command_approval():
    context = ToolContext("child", "run", "op", "/worker")
    deny = SimpleNamespace(authorize=AsyncMock(side_effect=ToolError("custom_denied", "denied")))
    mandatory = SimpleNamespace(authorize=AsyncMock())
    write = ToolSpec("write", "write", {}, category="write")
    call = ToolCall("write", "write", {})
    with pytest.raises(ToolError, match="denied"):
        await WorkerBoundaryGate(deny, mandatory).authorize(context, write, call)
    command = ToolSpec("bash", "command", {}, category="command")
    await WorkerBoundaryGate(NoOpToolGate(), mandatory).authorize(
        context, command, ToolCall("bash", "bash", {})
    )
    mandatory.authorize.assert_awaited_once()
    with pytest.raises(ToolError):
        await WorkerBoundaryGate(NoOpToolGate(), mandatory, readonly=True).authorize(
            context, write, call
        )
    with pytest.raises(ToolError):
        await WorkerBoundaryGate(NoOpToolGate(), mandatory).authorize(
            context, ToolSpec("remote", "external", {}, source="mcp:test"), call
        )


async def test_required_sandbox_never_uses_unavailable_backend_fallback(tmp_path):
    with pytest.raises(ToolError) as error:
        await RequiredSandbox(UnavailableSandbox()).launch(
            ("bash", "-c", "touch /tmp/escaped"), tmp_path, SandboxSettings("on")
        )
    assert error.value.code == "worker_sandbox_unavailable"


def test_child_config_forces_worker_isolation_preserves_parent_policy_and_validates_limits(
    tmp_path,
):
    policy = object()
    config = AppConfig(
        tmp_path, tmp_path / "data", sandbox=SandboxSettings("off", True), tool_gate=policy
    )
    child = child_config(
        config, {"agent_id": "12345678", "mode": "worker", "workspace": str(tmp_path / "worker")}
    )
    assert child.sandbox.mode == "on" and child.sandbox.network_enabled
    assert child.tool_gate is policy and not child.allow_delegation
    for value in (True, 0, 5):
        with pytest.raises(ValueError):
            AppConfig(tmp_path, tmp_path / "data", team_max_agents=value)


def test_user_skill_read_grant_is_narrow_and_keeps_dotenv_and_write_denials(tmp_path):
    from pycode.sandbox.adapters.seatbelt import build_profile

    home = tmp_path / "home"
    workspace = tmp_path / "worker"
    skill = home / ".pycode" / "skills" / "reviewer"
    profile = build_profile(workspace, False, home=home, skill_read_roots=(skill,))
    grant = f'(allow file-read* (subpath "{skill}"))'
    assert grant in profile
    assert profile.index(grant) < profile.index("(deny file-read-data")
    assert f'(allow file-write* (subpath "{workspace}"))' in profile
    assert f'(allow file-write* (subpath "{skill}"))' not in profile
    assert '(allow file-read* (subpath "' + str(home / ".pycode") + '"))' not in profile
