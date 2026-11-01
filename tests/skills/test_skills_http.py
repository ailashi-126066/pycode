import json

import pytest

from pycode.app import AppConfig, compose_application
from pycode.kernel import Message
from tests.context.test_summary_http import seed
from tests.skills.test_skills import write_skill
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


def skill_reply(name, arguments, call_id):
    reply = chat_completion(tool=True)
    reply.body = (
        reply.body.decode()
        .replace('"name": "echo"', json.dumps("name") + ": " + json.dumps(name))
        .replace(json.dumps('{"value":"wire"}'), json.dumps(json.dumps(arguments)))
        .replace('"call-1"', json.dumps(call_id))
        .encode()
    )
    return reply


async def test_model_loads_instructions_at_next_boundary_and_all_tools_keep_common_ledger(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await write_skill(config.user_data / "skills", body="Private instructions $ARGUMENTS")
    replies = [
        skill_reply("read", {"path": "/skills/user/example/SKILL.md"}, "load-1"),
        skill_reply("bash", {"command": "echo forbidden"}, "bash-1"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            result = await session.run(Message("u1", "user", "Use example"))
            assert result.outcome == "completed" and len(requests) == 3
            before = requests[0]["body"]["messages"]
            assert any(
                "example" in str(item["content"]) and "Example task" in str(item["content"])
                for item in before
            )
            assert not any("Private instructions" in str(item["content"]) for item in before)
            after = requests[1]["body"]["messages"]
            assert (
                sum("Private instructions $ARGUMENTS" in str(item["content"]) for item in after)
                == 1
            )
            state = await session.inspect()
            entries = state["snapshot"]["entries"]
            loaded = [
                entry["message"]["tool_result"]["details"]["skill"]["name"]
                for entry in entries
                if entry["message"]
                and entry["message"]["role"] == "tool_result"
                and "skill" in entry["message"]["tool_result"]["details"]
            ]
            assert loaded == ["example"]
            results = [
                entry["message"]["tool_result"]
                for entry in entries
                if entry["message"] and entry["message"]["role"] == "tool_result"
            ]
            assert len(results) == 2
            # The skill scope removed bash from the request, so the name check rejects it.
            assert results[1]["error_code"] == "tool_not_declared"
            assert all(item["operation_id"] and item["run_id"] for item in results)
        finally:
            await session.aclose()


async def test_actual_compaction_and_restart_keep_exact_skill_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=1500, memory_mode="off"
    )
    await write_skill(config.user_data / "skills", body="Pinned Skill $ARGUMENTS")
    await seed(config)
    with protocol_server(
        [
            skill_reply("read", {"path": "/skills/user/example/SKILL.md"}, "load"),
            chat_completion(),
            chat_completion(),
            chat_completion(),
        ]
    ) as (url, requests):
        selected = provider(url)
        session = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        try:
            await run_skill(session, "example", "exact")
            report = await session.compact()
            assert report.applied_job_id is not None
            state = await session.inspect()
            compacted = next(
                entry for entry in state["snapshot"]["entries"] if entry["type"] == "compaction"
            )
            assert "Pinned Skill" not in compacted["data"]["checkpoint"]["system_prompt"]
        finally:
            await session.aclose()
        session = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        try:
            assert (await session.run(Message("new", "user", "continue"))).outcome == "completed"
            assert len(requests) == 4
            assert (
                sum(
                    "Pinned Skill $ARGUMENTS" in str(item["content"])
                    for item in requests[-1]["body"]["messages"]
                )
                == 1
            )
        finally:
            await session.aclose()


async def test_root_branch_recreates_catalog_without_restoring_old_activation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await write_skill(config.user_data / "skills", body="Instructions $ARGUMENTS")
    with protocol_server(
        [
            skill_reply("read", {"path": "/skills/user/example/SKILL.md"}, "load-old"),
            chat_completion(),
            chat_completion(),
            skill_reply("read", {"path": "/skills/user/example/SKILL.md"}, "load-new"),
            chat_completion(),
        ]
    ) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            await run_skill(session, "example", "old")
            assert (await session.run(Message("old", "user", "task"))).outcome == "completed"
            await session.branch(None)
            assert (await session.inspect())["snapshot"]["leaf_id"] is None
            assert (await session.run(Message("new", "user", "new task"))).outcome == "completed"
            assert len(requests) == 5
            before = requests[3]["body"]["messages"]
            assert any(
                "example" in str(item["content"]) and "Example task" in str(item["content"])
                for item in before
            )
            assert not any("Instructions $ARGUMENTS" in str(item["content"]) for item in before)
            after = requests[4]["body"]["messages"]
            assert sum("Instructions $ARGUMENTS" in str(item["content"]) for item in after) == 1
        finally:
            await session.aclose()


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_model_install_needs_persisted_approval_and_restart_settles_same_operation(
    tmp_path, monkeypatch, decision
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    replies = [
        skill_reply("InstallSkill", {"url": "file:///invalid"}, "install-1"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            waiting = await session.run(Message("u1", "user", "install"))
            assert waiting.outcome == "needs_input" and len(requests) == 1
            pending = waiting.pending[0]
            assert pending["kind"] == "permission" and pending["call"]["name"] == "InstallSkill"
            assert not (config.user_data / "skills").exists()
            assert (await session.resume()).pending == waiting.pending
            result = await session.resume(
                {"operation_id": pending["operation_id"], "decision": decision}
            )
            assert result.outcome == "completed" and len(requests) == 2
            state = await session.inspect()
            results = [
                entry["message"]["tool_result"]
                for entry in state["snapshot"]["entries"]
                if entry["message"] and entry["message"]["role"] == "tool_result"
            ]
            assert len(results) == 1 and results[0]["is_error"]
            assert results[0]["operation_id"] == pending["operation_id"]
            assert results[0]["error_code"] == (
                "invalid_skill_source" if decision == "approve" else "permission_denied"
            )
            assert not (config.user_data / "skills").exists()
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
