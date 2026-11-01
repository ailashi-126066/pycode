import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.cli.commands import command
from pycode.app.cli.main import main
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import RunResult


def test_diagnostics_use_shared_application(tmp_path, capsys):
    assert main(["--workspace", str(tmp_path), "doctor", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["name"] == "pycode"
    assert result["workspace"]["state_path"] == str(tmp_path / ".pycode")
    assert not result["workspace"]["is_git"]
    assert not (tmp_path / ".git").exists()


def test_doctor_builds_one_application(tmp_path, capsys, monkeypatch):
    import pycode.app.cli.main as cli

    built = []

    def compose(*args):
        built.append(args)
        return compose_application(*args)

    monkeypatch.setattr(cli, "compose_application", compose)
    assert main(["--workspace", str(tmp_path), "doctor", "--json"]) == 0
    assert len(built) == 1


def test_doctor_reports_external_tools_with_install_hints(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert main(["--workspace", str(tmp_path), "doctor", "--json"]) == 0
    tools = {tool["name"]: tool for tool in json.loads(capsys.readouterr().out)["tools"]}
    assert {"rg", "git", "bash"} <= tools.keys()
    assert tools["rg"]["path"] is None and "ripgrep" in tools["rg"]["install"]
    assert main(["--workspace", str(tmp_path), "doctor"]) == 0
    assert "rg: missing (glob and grep search tools)" in capsys.readouterr().out


def test_missing_workspace_is_a_reported_failure(tmp_path, capsys):
    assert main(["--workspace", str(tmp_path / "missing"), "doctor", "--json"]) == 1
    assert not json.loads(capsys.readouterr().out)["workspace"]["exists"]


def test_module_entry_point(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "pycode", "--version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "pycode 0.1.0"


async def test_cli_waits_for_background_subagents_and_runs_their_results(tmp_path, monkeypatch):
    session = SimpleNamespace(
        run=AsyncMock(return_value=RunResult("completed", "lead finished", ())),
        wait_subagents=AsyncMock(),
        wake_pending=Mock(side_effect=[True, False]),
        wake=AsyncMock(return_value=RunResult("completed", "subagent result handled", ())),
        publish_result=AsyncMock(),
        aclose=AsyncMock(),
    )
    config = AppConfig(tmp_path, tmp_path / "data")
    application = compose_application(config)
    selected = SimpleNamespace(
        console=application.console, open_session=AsyncMock(return_value=session)
    )
    monkeypatch.setattr("pycode.app.cli.commands.compose_application", lambda *args: selected)
    monkeypatch.setattr("pycode.app.cli.commands.read_providers", lambda path: ())
    args = SimpleNamespace(
        command="run",
        output_format="json",
        providers="unused",
        session="lead",
        create=False,
        cached=True,
        prompt="task",
        input_id="u1",
    )
    assert await command(args, config) == 0
    session.wake.assert_awaited_once()
    assert session.wait_subagents.await_count == 2
    # Nobody can answer a subagent's approval in a non-interactive run.
    assert selected.open_session.await_args.kwargs["interactive"] is False
    session.aclose.assert_awaited_once()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
