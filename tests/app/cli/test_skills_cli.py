import asyncio

import pytest

from pycode.app.cli.main import main
from tests.app.cli.test_execution_cli import setup_cli
from tests.skills.test_skills import write_skill
from tests.skills.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def test_cli_load_search_reload_use_public_session_and_make_no_model_requests(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    asyncio.run(write_skill(tmp_path / "data/skills"))
    with protocol_server(
        [
            chat_completion(),
            skill_reply("read", {"path": "/skills/user/example/SKILL.md"}, "load"),
            chat_completion(),
        ]
    ) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        assert main([*common, "run", "task", *execution, "--create"]) == 0
        capsys.readouterr()
        assert main([*common, "skill", *execution, "load", "example", "arguments"]) == 0
        assert "completed" in capsys.readouterr().out
        assert main([*common, "skill", *execution, "search", "Example", "--limit", "1"]) == 0
        assert "Example task" in capsys.readouterr().out
        assert main([*common, "skill", *execution, "reload"]) == 0
        capsys.readouterr()
        assert main([*common, "skill", *execution, "search", "", "--limit", "0"]) == 1
        assert "invalid_skill_search" in capsys.readouterr().out
        assert len(requests) == 3
        assert "Task arguments: arguments" in str(requests[1]["body"]["messages"])
        assert "Instructions $ARGUMENTS" in str(requests[2]["body"]["messages"])


def test_cli_skill_authoring_does_not_call_model(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    source = tmp_path / "draft.md"
    source.write_text("---\nname: authored\ndescription: Local instructions\n---\nFirst\n")
    with protocol_server([chat_completion()]) as (url, requests):
        common, execution = setup_cli(tmp_path, provider(url))
        assert main([*common, "run", "task", *execution, "--create"]) == 0
        capsys.readouterr()
        assert (
            main([*common, "skill", *execution, "create", "authored", "--file", str(source)]) == 0
        )
        created = json.loads(capsys.readouterr().out)
        assert created["revision"]
        assert main([*common, "skill", *execution, "read", "authored"]) == 0
        assert "First" in capsys.readouterr().out
        source.write_text(source.read_text().replace("First", "Second"))
        assert (
            main(
                [
                    *common,
                    "skill",
                    *execution,
                    "update",
                    "authored",
                    "--file",
                    str(source),
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert (tmp_path / "data/skills/authored/SKILL.md").read_text() == source.read_text()
        assert (
            main(
                [
                    *common,
                    "skill",
                    *execution,
                    "update",
                    "authored",
                    "--file",
                    str(source),
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert len(requests) == 1


pytestmark = pytest.mark.usefixtures("disclosed_tools")
