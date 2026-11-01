"""Exercise the real application root with disabled, replaced and failed feature products."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.support.execution import provider
from tests.support.skill_loading import read_skill

from pycode.app.api import RunOptions, SessionResources
from pycode.app.bootstrap import compose_application, default_modules
from pycode.app.composition.subagents import ConfiguredSubagentSessions
from pycode.app.configuration.settings import AppConfig
from pycode.app.lifecycle.session import ConfiguredRuntimeLauncher
from pycode.kernel import Message, ToolCall, ToolContext, ToolResult, ToolSpec
from pycode.models.module import ModelsModule
from pycode.session import SessionCatalogWriteGuard
from pycode.session.module import SessionsModule
from pycode.skills.module import SkillsSettings
from pycode.subagents import Subagent
from pycode.tools import ReadOnlyToolGate


def configuration(tmp_path):
    return AppConfig(tmp_path, tmp_path / "state", memory_mode="off", allow_delegation=False)


async def test_providerless_root_and_disabled_modules_do_not_open_feature_configuration(
    tmp_path, monkeypatch
):
    config = configuration(tmp_path)
    config.user_data.mkdir()
    (config.user_data / "mcp.json").write_text("not valid JSON")
    selected = replace(default_modules(config), skills=None, mcp=None)
    application = compose_application(config, modules=selected)
    assert application.status().workspace.exists
    assert application.session_repository() is not None
    configured = provider("http://127.0.0.1:9")
    monkeypatch.setenv(configured.api_key_env, "local-test-key")
    session = await compose_application(config, (configured,), modules=selected).open_session(
        "disabled",
        create=True,
        refresh_models=False,
        startup_hooks=False,
    )
    await session.aclose()


async def test_selected_modules_and_credential_source_survive_child_scope(tmp_path):
    config = configuration(tmp_path)
    selected = default_modules(config)
    repository_workspaces = []

    class SelectedSessions(SessionsModule):
        def repository(self):
            repository_workspaces.append(self.workspace)
            return SimpleNamespace()

    sessions = SelectedSessions(config.workspace, config.user_data, selected.sessions.timing)
    credentials = {"INDEPENDENT_PROVIDER_KEY": "test-only"}
    models = ModelsModule(tmp_path / "catalog.json", tmp_path / "custom.sqlite", credentials)
    selected = replace(selected, sessions=sessions, models=models, skills=None, mcp=None)
    roster = provider("http://127.0.0.1:9")
    observed = {}
    recorded = []

    async def open_child(config, providers, session_id, **options):
        observed.update(options, config=config, providers=providers)
        options["modules"].sessions.repository()
        return SimpleNamespace()

    async def restore(default):
        return roster

    async def append_entry(draft):
        recorded.append(draft)

    children = ConfiguredSubagentSessions(
        config,
        SimpleNamespace(append_entry=append_entry),
        SimpleNamespace(restore=restore),
        roster,
        interactive=False,
        session_opener=open_child,
        modules=selected,
    )
    child_workspace = tmp_path / "child"
    agent = Subagent(
        "a1", "agent-1", "review", "readonly", False, 5, "call", "run", str(child_workspace)
    )
    await children.open(agent, SimpleNamespace())
    derived = observed["modules"]
    assert isinstance(derived.sessions, SelectedSessions)
    assert derived.sessions.workspace == child_workspace
    assert repository_workspaces == [child_workspace]
    assert derived.models.environment is credentials
    assert derived.skills is None and derived.mcp is None
    assert derived.tools is selected.tools and derived.context is selected.context
    child = observed["config"]
    assert child.read_only and not child.allow_delegation and child.max_turns == 5
    # Non-interactive: nobody can answer a subagent's approval.
    assert isinstance(child.tool_gate, ReadOnlyToolGate)
    assert observed["providers"] == (roster,)
    assert recorded[0].data["kind"] == "subagent_start" and recorded[0].run_id == "run"


async def test_replaced_mcp_owns_exactly_the_session_it_created(tmp_path):
    config = configuration(tmp_path)
    selected = default_modules(config)
    closed = []
    calls = []

    class ReplacedMCP:
        async def open_session(self, settings, credentials, **options):
            calls.append(settings)

            async def close():
                closed.append("custom")

            return SimpleNamespace(connections=None, aclose=close)

    config.user_data.mkdir()
    (config.user_data / "mcp.json").write_text("default implementation must not read this")
    selected = replace(selected, skills=None, mcp=ReplacedMCP())
    session = await compose_application(
        config, (provider("http://127.0.0.1:9"),), modules=selected
    ).open_session(
        "custom",
        create=True,
        refresh_models=False,
        startup_hooks=False,
    )
    await session.aclose()
    await session.aclose()
    assert len(calls) == 1 and closed == ["custom"]


async def test_public_runtime_with_history_keeps_active_skill_scope(tmp_path, monkeypatch):
    class Approved:
        async def authorize(self, *args):
            return None

    class Probe:
        spec = ToolSpec(
            "ForbiddenProbe", "must remain blocked", {"type": "object"}, category="read"
        )
        executed = False

        async def execute(self, context, arguments):
            self.executed = True
            return ToolResult(context.call_id, self.spec.name, "executed")

    config = replace(configuration(tmp_path), tool_gate=Approved())
    configured = provider("http://127.0.0.1:9")
    monkeypatch.setenv(configured.api_key_env, "local-test-key")
    document = config.user_data / "skills/locked/SKILL.md"
    document.parent.mkdir(parents=True)
    document.write_text(
        "---\nname: locked\ndescription: Read-only helper\nallowed-tools: [Read]\n---\nRead only.\n"
    )
    application = compose_application(config, (configured,))
    history = await application.session_repository().open("scope", create=True)
    runtime = None
    try:
        await history.append_message(Message("u", "user", "follow the Skill"))
        skills = application._modules.skills.open_session(
            SkillsSettings(config.workspace, config.user_data),
            history,
            catalog_write_guard=SessionCatalogWriteGuard(),
        )
        await skills.initialize()
        await read_skill(history, skills.catalog, "locked")
        tool = Probe()
        runtime = await application.runtime(
            SessionResources(
                session_id="scope", journal=history, history_store=history, tools=(tool,)
            ),
            RunOptions(provider=configured, run_id="r"),
        )
        result = await runtime.context.executor.execute(
            ToolContext("scope", "r", "probe", str(tmp_path)),
            ToolCall("probe", tool.spec.name, {}),
            "tool_use",
        )
        assert result.error_code == "skill_tool_not_allowed" and not tool.executed
    finally:
        if runtime is not None:
            await runtime.aclose()
        await history.aclose()


async def test_launcher_attempts_every_closer_after_an_earlier_failure(tmp_path):
    order = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def aclose(self):
            order.append(self.name)
            if self.fail:
                raise RuntimeError(self.name)

    launcher = ConfiguredRuntimeLauncher(
        configuration(tmp_path),
        (),
        (),
        None,
        resource_owners=(Resource("first"), Resource("second", True)),
    )
    launcher._active_container = Resource("active", True)
    launcher.summary = Resource("summary", True)
    with pytest.raises(RuntimeError, match="active"):
        await launcher.aclose()
    with pytest.raises(RuntimeError, match="active"):
        await launcher.aclose()
    assert order == ["active", "summary", "second", "first"]


async def test_launcher_cancellation_waits_for_owned_cleanup(tmp_path):
    started, finish = asyncio.Event(), asyncio.Event()
    closed = []

    class Resource:
        async def aclose(self):
            started.set()
            await finish.wait()
            closed.append(True)

    launcher = ConfiguredRuntimeLauncher(
        configuration(tmp_path), (), (), None, resource_owners=(Resource(),)
    )
    waiting = asyncio.create_task(launcher.aclose())
    async with asyncio.timeout(2):
        await started.wait()
        waiting.cancel()
        await asyncio.sleep(0)
        waiting.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert closed == [True]
    await launcher.aclose()


async def test_view_factory_failure_closes_records_before_returning(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    selected = replace(default_modules(config), skills=None, mcp=None)
    application = compose_application(config, modules=selected)
    store = await application.session_repository().open("view", create=True)
    await store.append_message(Message("u", "user", "persist view"))
    await store.aclose()
    closed = []

    class Records:
        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(selected.models, "records", lambda: Records())

    def broken_view(*args, **options):
        raise RuntimeError("view construction failed")

    monkeypatch.setattr("pycode.app.composition.session.SessionView", broken_view)
    with pytest.raises(RuntimeError, match="view construction failed"):
        await application.open_view("view")
    assert closed == [True]


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
