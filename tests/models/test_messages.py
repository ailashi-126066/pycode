from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from pycode.kernel import ModelError, ModelSelection
from pycode.models.messages import from_assistant, normalize_usage, to_framework


# Fixed AIMessage samples in the shape each LangChain integration produces.
@pytest.mark.parametrize(
    ("implementation", "usage_metadata", "expected"),
    [
        (
            "deepseek",
            {
                "input_tokens": 20,
                "output_tokens": 5,
                "total_tokens": 25,
                "input_token_details": {"cache_read": 12},
                "output_token_details": {"reasoning": 3},
            },
            (20, 5, 12, None, 3, 25),
        ),
        (
            "openai",
            {
                "input_tokens": 20,
                "output_tokens": 5,
                "total_tokens": 25,
                "input_token_details": {"cache_read": 0},
                "output_token_details": {"reasoning": 3},
            },
            (20, 5, 0, None, 3, 25),
        ),
        (
            "anthropic",
            {
                "input_tokens": 34,
                "output_tokens": 5,
                "total_tokens": 39,
                "input_token_details": {"cache_read": 12, "cache_creation": 2},
            },
            (34, 5, 12, 2, None, 39),
        ),
        ("openai", None, (None, None, None, None, None, None)),
    ],
)
def test_usage_comes_only_from_usage_metadata_without_fabricated_zeros(
    implementation, usage_metadata, expected
):
    selection = ModelSelection("configured-provider", "configured-model", implementation)
    metadata = {"model_name": "configured-model", "finish_reason": "stop"}
    response = AIMessage(
        id="req-1", content="text", usage_metadata=usage_metadata, response_metadata=metadata
    )
    usage = normalize_usage(response, selection, "session-1", "run-1")
    values = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
    )
    assert values == expected
    assert usage.cache_miss_tokens is None
    assert usage.raw["response_metadata"] == metadata


def test_deepseek_reasoning_and_tool_calls_round_trip_at_framework_boundary():
    selection = ModelSelection("local", "model", "deepseek")
    response = AIMessage(
        id="a1",
        content="reply",
        additional_kwargs={"reasoning_content": "think"},
        tool_calls=[{"id": "call-1", "name": "echo", "args": {"value": "hello"}}],
    )
    normalized = from_assistant(response, selection, "s1", "r1")
    restored = to_framework(normalized, selection)
    assert restored.additional_kwargs["reasoning_content"] == "think"
    assert restored.tool_calls[0]["id"] == "call-1"
    assert normalized.usage.cache_read_tokens is None


def test_anthropic_thinking_signature_and_redaction_are_preserved():
    selection = ModelSelection("local", "model", "anthropic")
    response = AIMessage(
        id="a1",
        content=[
            {"type": "thinking", "thinking": "think", "signature": "signed"},
            {"type": "redacted_thinking", "data": "redacted"},
            {"type": "text", "text": "reply"},
        ],
    )
    normalized = from_assistant(response, selection, "s1", "r1")
    restored = to_framework(normalized, selection)
    assert normalized.thinking[0].signature == "signed"
    assert normalized.thinking[1].redacted_data == "redacted"
    assert restored.content == response.content


def test_malformed_tool_arguments_remain_invalid_even_with_an_empty_schema():
    response = AIMessage(
        id="a1",
        content="",
        invalid_tool_calls=[
            {"id": "call-1", "name": "echo", "args": "{broken", "error": "not valid JSON"},
        ],
    )
    normalized = from_assistant(response, ModelSelection("local", "model", "openai"), "s1", "r1")
    assert normalized.tool_calls[0].parse_error == "not valid JSON"
    assert normalized.tool_calls[0].raw_arguments == "{broken"


@pytest.mark.parametrize("call_id", [None, "", " \t"])
def test_missing_provider_call_id_is_rejected_before_creating_unpairable_history(call_id):
    response = AIMessage(
        content="", tool_calls=[{"id": call_id, "name": "write_file", "args": {"path": "code.py"}}]
    )
    with pytest.raises(ModelError) as failed:
        from_assistant(response, ModelSelection("local", "model", "openai"), "s1", "r1")
    assert failed.value.code == "invalid_tool_call"


def test_openai_responses_reasoning_summary_is_not_lost():
    block = {
        "type": "reasoning",
        "id": "rs-1",
        "summary": [
            {"type": "summary_text", "text": "reasoning summary"},
        ],
    }
    response = AIMessage(id="a1", content=[block, {"type": "text", "text": "reply"}])
    normalized = from_assistant(response, ModelSelection("local", "model", "openai"), "s1", "r1")
    assert normalized.thinking[0].text == "reasoning summary"
    assert normalized.thinking[0].provider_data == block
    restored = to_framework(
        normalized, ModelSelection("local", "model", "openai", api_mode="responses")
    )
    assert restored.content[0] == block


def test_thinking_is_replayed_only_for_the_model_that_produced_it():
    from pycode.kernel import Message, TextBlock, ThinkingBlock
    from pycode.models.messages import transform_messages

    current = ModelSelection("p", "m1", "anthropic")
    reply = Message(
        "a1",
        "assistant",
        (ThinkingBlock("think", signature="s"), TextBlock("reply")),
        model_selection=current,
    )
    assert transform_messages([reply], current)[0].thinking
    other_model = ModelSelection("p", "m2", "anthropic")
    dropped = transform_messages([reply], other_model)[0]
    assert dropped.thinking == () and dropped.text == "reply"
    other_api = ModelSelection("p", "m1", "anthropic", api_mode="responses")
    assert transform_messages([reply], other_api)[0].thinking == ()


def test_failed_reply_and_its_tool_results_are_not_replayed():
    from pycode.kernel import Message, TextBlock, ToolCall, ToolResult
    from pycode.models.messages import to_framework_messages

    selection = ModelSelection("p", "m", "openai")
    messages = [
        Message("u1", "user", "go"),
        Message(
            "a1",
            "assistant",
            (TextBlock("partial"), ToolCall("c1", "echo", {})),
            stop_reason="error",
            error="provider_http_503",
        ),
        Message("t1", "tool_result", tool_result=ToolResult("c1", "echo", "unknown")),
        Message("s1", "compaction_summary", "summary"),
    ]
    replay = to_framework_messages(messages, selection)
    assert [type(message).__name__ for message in replay] == ["HumanMessage", "HumanMessage"]
    # convert_to_llm wraps the summary as Pi does; only the failed reply was skipped.
    assert "<summary>\nsummary\n</summary>" in replay[1].content


def test_interleaved_provider_blocks_keep_their_order():
    selection = ModelSelection("local", "model", "anthropic")
    response = AIMessage(
        id="a1",
        content=[
            {"type": "thinking", "thinking": "first", "signature": "s1"},
            {"type": "text", "text": "look "},
            {"type": "tool_use", "id": "call-1", "name": "echo", "input": {}},
            {"type": "text", "text": "then"},
        ],
        tool_calls=[{"id": "call-1", "name": "echo", "args": {}}],
    )
    normalized = from_assistant(response, selection, "s1", "r1")
    assert [block.type for block in normalized.content] == [
        "thinking",
        "text",
        "tool_call",
        "text",
    ]
    assert normalized.text == "look then"
    restored = to_framework(normalized, selection)
    assert restored.content == response.content
    from langchain_anthropic.chat_models import _format_messages

    _, wire = _format_messages([restored], model=selection.model_id)
    assert wire[0]["content"] == response.content
    assert normalized.stop_reason == "tool_use"


def test_content_filter_maps_to_error_with_its_code():
    response = AIMessage(content="", response_metadata={"finish_reason": "content_filter"})
    normalized = from_assistant(response, ModelSelection("local", "model", "openai"), "s", "r")
    assert (normalized.stop_reason, normalized.error) == ("error", "provider_content_filter")


def test_other_model_tool_call_ids_are_normalized_with_their_results():
    from pycode.kernel import Message, TextBlock, ToolCall, ToolResult
    from pycode.models.messages import transform_messages

    source = ModelSelection("p", "gpt", "openai", api_mode="responses")
    call_id = "call_1|fc_" + "x" * 300
    messages = [
        Message("a1", "assistant", (TextBlock("run"), ToolCall(call_id, "echo", {}))),
        Message("t1", "tool_result", tool_result=ToolResult(call_id, "echo", "ok")),
    ]
    messages[0] = replace(messages[0], model_selection=source)
    sent = transform_messages(messages, ModelSelection("p", "claude", "anthropic"))
    normalized = sent[0].tool_calls[0].id
    assert len(normalized) <= 64 and "|" not in normalized
    assert sent[1].tool_result.call_id == normalized
    assert messages[0].tool_calls[0].id == call_id
    # The producing model gets its own IDs back unchanged.
    assert transform_messages(messages, source)[0].tool_calls[0].id == call_id


def test_excluded_messages_are_not_sent_and_late_system_text_suits_anthropic():
    from pycode.kernel import Message
    from pycode.models.messages import to_framework_messages

    messages = [
        Message("s0", "system", "rules"),
        Message("u1", "user", "hi"),
        Message("x1", "custom", "local note", exclude_from_context=True),
        Message("s1", "system", "The available tools changed."),
    ]
    anthropic = to_framework_messages(messages, ModelSelection("p", "m", "anthropic"))
    assert [type(m).__name__ for m in anthropic] == [
        "SystemMessage",
        "HumanMessage",
        "HumanMessage",
    ]
    openai = to_framework_messages(messages, ModelSelection("p", "m", "openai"))
    assert [type(m).__name__ for m in openai] == ["SystemMessage", "HumanMessage", "SystemMessage"]
