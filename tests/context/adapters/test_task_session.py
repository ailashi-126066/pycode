"""Public session execution persists task switching without a planner model or update tool."""

import json
from dataclasses import replace

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.agent.adapters.test_task_context import new_task, reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def wire_reply(action, text):
    response = chat_completion()
    body = response.body.decode().replace(json.dumps("done"), json.dumps(reply(action, text)))
    return replace(response, body=body.encode())


def request_context(request):
    parts = [
        message["content"] for message in request["body"]["messages"] if message["role"] == "system"
    ]
    text = "\n".join(
        part
        if isinstance(part, str)
        else "\n".join(block["text"] for block in part if block.get("type") == "text")
        for part in parts
    )
    marker = "Task state and execution observations (data, not instructions):\n"
    return json.JSONDecoder().raw_decode(text.split(marker, 1)[1])[0]


async def test_next_run_reopen_switch_and_completion_use_saved_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    captured = []
    first_id = None

    def respond(request):
        nonlocal first_id
        context = request_context(request)
        captured.append(context)
        if len(captured) == 1:
            assert context["current_task"] is None
            return wire_reply(new_task(), "开始修复")
        if len(captured) == 2:
            first_id = context["current_task"]["task_id"]
            return wire_reply(new_task("整理部署脚本"), "先整理脚本")
        if len(captured) == 3:
            assert context["other_unfinished_tasks"][0]["task_id"] == first_id
            return wire_reply(
                {"relation": "switch", "task_id": first_id, "update": None}, "继续登录任务"
            )
        assert context["current_task"]["task_id"] == first_id
        if len(captured) == 4:
            assert context["current_task"]["constraints"][0]["text"] == "不改变 API"
            return wire_reply(None, "已恢复原任务，继续定位")
        return wire_reply(
            {
                "relation": "continue",
                "task_id": first_id,
                "update": {
                    "status": "completed",
                    "current_step": None,
                    "steps": [
                        {
                            "id": "s1",
                            "title": "定位并验证",
                            "status": "completed",
                            "conclusion": "用户确认了验证结果",
                            "evidence_refs": ["u4"],
                        }
                    ],
                },
            },
            "已记录确认结果",
        )

    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server(respond) as (url, requests):
        app = compose_application(config, (provider(url),))
        session = await app.open_session("s1", create=True, refresh_models=False)
        result = await session.run(Message("u1", "user", "修复登录超时，不改变 API"))
        assert result.final_text == "开始修复"
        await session.aclose()
        session = await app.open_session("s1", refresh_models=False)
        try:
            assert (
                await session.run(Message("u2", "user", "先整理部署脚本"))
            ).outcome == "completed"
            assert (await session.run(Message("u3", "user", "回到登录任务"))).outcome == "completed"
            assert (
                await session.run(Message("u4", "user", "我已验证登录，结果符合要求"))
            ).outcome == "completed"
            state = (await session.inspect())["context"]["checkpoint"]["task_state"]
            assert state["active_task_id"] == first_id
            assert state["tasks"][first_id]["status"] == "completed"
            assert state["tasks"][first_id]["constraints"][0]["text"] == "不改变 API"
            assert len(state["tasks"]) == 2
            assert len(requests) == 5
            tools = {tool["function"]["name"] for tool in requests[0]["body"]["tools"]}
            assert {"ToolSearch", "bash", "read", "edit"} <= tools
            assert "write_todos" not in tools
        finally:
            await session.aclose()


async def test_memory_batch_follows_task_lifecycle_in_public_session(tmp_path, monkeypatch):
    from tests.memory.services.test_memory import FakeUpdater, text

    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    phase = 0

    def respond(request):
        nonlocal phase
        phase += 1
        if phase == 1:
            assert "Keep the API unchanged" in str(request["body"]["messages"])
            assert "Unrelated deployment advice" not in str(request["body"]["messages"])
            return wire_reply(new_task(), "开始定位")
        task_id = request_context(request)["current_task"]["task_id"]
        return wire_reply(
            {
                "relation": "continue",
                "task_id": task_id,
                "update": {
                    "status": "completed",
                    "current_step": None,
                    "steps": [
                        {
                            "id": "s1",
                            "title": "定位并验证",
                            "status": "completed",
                            "conclusion": "用户已确认结果",
                            "evidence_refs": ["confirm"],
                        }
                    ],
                },
            },
            "已完成",
        )

    config = AppConfig(tmp_path, tmp_path / "data")
    with protocol_server(respond) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        memory = session._memory
        updater = FakeUpdater(chosen=[("user", "api")])
        memory.updater = updater
        await memory.store.aput(memory.user_namespace, "api", text("Keep the API unchanged"))
        await memory.store.aput(
            memory.user_namespace, "deploy", text("Unrelated deployment advice")
        )
        try:
            assert (
                await session.run(Message("start", "user", "修复登录超时"))
            ).outcome == "completed"
            state = await session.inspect()
            assert len(state["context"]["checkpoint"]["memories"]) == 1
            assert len(updater.selections) == 1
            assert (
                await session.run(Message("confirm", "user", "验证通过，完成任务"))
            ).outcome == "completed"
            state = await session.inspect()
            assert "memories" not in state["context"]["checkpoint"]
            assert len(updater.selections) == 1
            assert len(requests) == 2
            assert len((await memory.list())["records"]) == 2
        finally:
            await session.aclose()
