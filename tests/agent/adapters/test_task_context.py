"""Task updates share the native model/tool turn and the durable assistant record."""

import json

import pytest
from langchain_core.messages import AIMessage

from pycode.context import TaskOutput, apply_task_change, empty_tasks, validate_task_action
from pycode.kernel import Message
from tests.agent.adapters.test_agent import assemble, tool_response


def reply(action, message="继续处理"):
    return json.dumps({"task_action": action, "message": message}, ensure_ascii=False)


def new_task(goal="修复登录超时"):
    return {
        "relation": "new",
        "task_id": None,
        "update": {
            "goal": goal,
            "constraints": [{"text": "不改变 API", "source": "user"}],
            "acceptance": [{"text": "登录回归测试通过", "source": "agent"}],
            "current_step": "s1",
            "steps": [{"id": "s1", "title": "定位并验证", "status": "in_progress"}],
        },
    }


def validate(state, action, evidence=()):
    proposal = TaskOutput.model_validate_json(reply(action)).task_action
    assert proposal is not None
    return validate_task_action(state, proposal, set(evidence))


def test_switch_preserves_unfinished_task_and_continue_keeps_constraints():
    state = empty_tasks()
    first = validate(state, new_task())
    state = apply_task_change(state, first)
    second = validate(state, new_task("整理部署脚本"))
    state = apply_task_change(state, second)
    assert state["tasks"][first["task_id"]]["status"] == "paused"
    state = apply_task_change(
        state,
        validate(state, {"relation": "switch", "task_id": first["task_id"], "update": None}),
    )
    assert state["active_task_id"] == first["task_id"]
    assert state["tasks"][first["task_id"]]["current_step"] == "s1"
    assert state["tasks"][first["task_id"]]["constraints"][0]["text"] == "不改变 API"
    assert state["tasks"][second["task_id"]]["status"] == "paused"
    with pytest.raises(ValueError, match="Use switch"):
        validate(state, {"relation": "continue", "task_id": second["task_id"], "update": None})


def test_completion_requires_branch_evidence_and_goal_cannot_be_replaced():
    state = empty_tasks()
    change = validate(state, new_task())
    state = apply_task_change(state, change)
    action = {
        "relation": "continue",
        "task_id": change["task_id"],
        "update": {
            "status": "completed",
            "current_step": None,
            "steps": [
                {
                    "id": "s1",
                    "title": "定位并验证",
                    "status": "completed",
                    "conclusion": "修改连接池后测试通过",
                    "evidence_refs": ["test-result"],
                }
            ],
        },
    }
    with pytest.raises(ValueError, match="current branch"):
        validate(state, action)
    completed = validate(state, action, {"test-result"})
    assert completed["task"]["status"] == "completed"
    with pytest.raises(ValueError, match="replacing"):
        validate(
            state,
            {"relation": "continue", "task_id": change["task_id"], "update": {"goal": "别的事"}},
        )


async def test_task_update_and_native_tool_call_share_one_response(disclosed_tools):
    first = tool_response().model_copy(update={"content": reply(new_task(), "先定位问题")})
    runtime, context, model, _, journal, recorder = assemble([first, AIMessage(content="已定位")])
    result = await runtime.run((Message("u1", "user", "修复登录超时，不改变 API"),), context)
    assert result.outcome == "completed"
    assert result.final_text == "已定位"
    assert model.call_index == 2
    assistant = next(m for m in journal.messages if m.role == "assistant")
    assert assistant.text == "先定位问题"
    assert assistant.tool_calls[0].name == "echo"
    assert assistant.provider_metadata["pycode_task_change"]["task"]["goal"] == "修复登录超时"
    second_request = str(model.requests[1])
    assert "current_task" in second_request and "定位并验证" in second_request
    assert "recent_execution_observations" in second_request and "tool-1" in second_request
    displayed = "".join(
        event.data["text"] for event in recorder.events if event.kind == "message_delta"
    )
    assert displayed == "先定位问题已定位"
    assert "task_action" not in displayed
    assert "write_todos" not in second_request


@pytest.mark.parametrize("text", ["没有按格式输出", '{"task_action":null,"message":"x","extra":1}'])
async def test_invalid_envelope_cannot_execute_its_tool_calls(text, disclosed_tools):
    runtime, context, model, tool, journal, recorder = assemble(
        [tool_response().model_copy(update={"content": text})]
    )
    model.task_output = False
    result = await runtime.run((Message("u1", "user", "执行"),), context)
    assert result.outcome == "error"
    assert not tool.executed
    assert len(journal.messages) == 1
    assert (
        next(e for e in recorder.events if e.kind == "error").data["code"] == "invalid_task_output"
    )


async def test_truncated_json_does_not_apply_task_until_complete_continuation():
    raw = reply(new_task(), "已经找到原因，继续修改")
    partial = raw[: raw.index("继续修改")]
    runtime, context, model, _, journal, _ = assemble(
        [
            AIMessage(content=partial, response_metadata={"finish_reason": "length"}),
            AIMessage(content=reply(new_task(), "继续修改")),
        ]
    )
    result = await runtime.run((Message("u1", "user", "修复登录超时"),), context)
    assert result.outcome == "completed"
    assistants = [m for m in journal.messages if m.role == "assistant"]
    assert assistants[0].text == "已经找到原因，"
    assert assistants[0].provider_metadata.get("pycode_task_change") is None
    assert assistants[1].provider_metadata["pycode_task_change"]["task"]["goal"] == "修复登录超时"
    assert "new complete JSON" in str(model.requests[1])
