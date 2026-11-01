"""Skills disclose instructions; scripts run only through ordinary Bash approval."""

import asyncio
import json
import shlex
import sys

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message
from tests.skills.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def run_skill(session, name, arguments=""):
    from uuid import uuid4

    selected = await session.skills.load(name, arguments)
    return await session.run(
        Message(
            uuid4().hex,
            "user",
            f"Use the {selected['name']} Skill. Read {selected['path']} for its instructions.\n"
            f"Task arguments: {selected['arguments']}",
            provider_metadata={"skill_request": selected},
        )
    )


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_skill_script_uses_bash_and_never_registers_python_tools(
    tmp_path, monkeypatch, decision
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    skill = config.user_data / "skills" / "check"

    def write():
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: check\ndescription: Check the workspace\n---\n"
            "Read scripts/check.py, then run it with Bash.\n",
            encoding="utf-8",
        )
        (skill / "scripts/check.py").write_text(
            "from pathlib import Path\nPath('ran.txt').write_text('once')\nprint('CHECK_OK')\n",
            encoding="utf-8",
        )
        # Old manifests are inert files; even a broken declaration cannot register/import code.
        (skill / "tool.json").write_text('{"name": "legacy_python_tool"}', encoding="utf-8")

    await asyncio.to_thread(write)
    command = shlex.join([sys.executable, str(skill / "scripts/check.py")])
    replies = [
        skill_reply("read", {"path": "/skills/user/check/SKILL.md"}, "load"),
        skill_reply("read", {"path": "/skills/user/check/scripts/check.py"}, "source"),
        skill_reply("bash", {"command": command}, "run"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            waiting = await run_skill(session, "check")
            assert waiting.outcome == "needs_input"
            assert waiting.pending[0]["call"]["name"] == "bash"
            assert not (tmp_path / "ran.txt").exists()
            result = await session.resume(
                {
                    "operation_id": waiting.pending[0]["operation_id"],
                    "decision": decision,
                }
            )
            assert result.outcome == "completed"
            assert (tmp_path / "ran.txt").exists() == (decision == "approve")
            assert str(skill) in str(requests[2]["body"]["messages"])
            for request in requests:
                names = {item["function"]["name"] for item in request["body"]["tools"]}
                assert "legacy_python_tool" not in names
                assert "bash" in names
            entries = (await session.inspect())["snapshot"]["entries"]
            results = [
                entry["message"]["tool_result"]
                for entry in entries
                if entry["message"]
                and entry["message"]["role"] == "tool_result"
                and entry["message"]["tool_result"]["call_id"] == "run"
            ]
            assert len(results) == 1
            assert results[0]["is_error"] == (decision == "deny")
            if decision == "approve":
                assert "CHECK_OK" in results[0]["text"]
        finally:
            await session.aclose()


async def test_save_skill_tool_requires_approval_and_does_not_start_a_second_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    content = "---\nname: local-check\ndescription: Local check\n---\nRead the project.\n"
    replies = [
        skill_reply("SaveSkill", {"name": "local-check", "content": content}, "save"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            waiting = await session.run(Message("u1", "user", "Create a Skill"))
            target = config.user_data / "skills/local-check/SKILL.md"
            assert waiting.outcome == "needs_input" and not target.exists()
            assert (
                await session.resume(
                    {
                        "operation_id": waiting.pending[0]["operation_id"],
                        "decision": "approve",
                    }
                )
            ).outcome == "completed"
            assert target.read_text() == content and len(requests) == 2
            entries = (await session.inspect())["snapshot"]["entries"]
            assert not any(
                "skill" in item["message"]["tool_result"]["details"]
                for item in entries
                if item["message"] and item["message"]["tool_result"]
            )
            assert json.dumps(entries)
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
