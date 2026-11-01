from pycode.kernel.domain.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    UsageRecord,
    convert_to_llm,
)


def test_ordered_content_derives_text_thinking_and_tool_calls():
    usage = UsageRecord(provider="deepseek", model="configured-model", request_id="req-1")
    message = Message(
        id="assistant-1",
        role="assistant",
        content=(
            ThinkingBlock(text="Reasoning", signature="provider-signature"),
            TextBlock("Checking "),
            ToolCall(id="call-1", name="Read", arguments={"path": "example.py"}),
            TextBlock("done"),
        ),
        stop_reason="tool_use",
        usage=usage,
    )
    assert message.text == "Checking done"
    assert message.thinking[0].signature == "provider-signature"
    assert message.tool_calls[0].id == "call-1"
    assert message.usage.input_tokens is None
    assert Message("u1", "user", "hi").text == "hi"
    assert Message("u1", "user", "hi").tool_calls == ()


def test_error_result_preserves_call_pairing_and_output_reference():
    result = ToolResult(
        call_id="call-1",
        name="Bash",
        text="failed",
        is_error=True,
        error_code="command_failed",
        exit_code=1,
        output_id="output-1",
    )
    message = Message(id="result-1", role="tool_result", tool_result=result)
    assert message.tool_result.call_id == "call-1"
    assert message.tool_result.exit_code == 1
    assert message.tool_result.output_id == "output-1"


def test_convert_to_llm_turns_custom_roles_into_user_text():
    messages = (
        Message("s", "system", "rules"),
        Message("c", "compaction_summary", "summary"),
        Message("x", "custom", "note"),
        Message("a", "assistant", "reply"),
    )
    assert [m.role for m in convert_to_llm(messages)] == ["system", "user", "user", "assistant"]
    assert convert_to_llm(messages)[1].text == "summary"
