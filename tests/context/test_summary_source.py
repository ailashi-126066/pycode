from pycode.context.compaction import find_cut_point
from pycode.context.projector import ContextProjector
from pycode.context.summary import (
    SUMMARIZATION_PROMPT,
    UPDATE_SUMMARIZATION_PROMPT,
    capture_source,
    history_prompt,
    serialize_conversation,
)
from pycode.kernel import (
    Attachment,
    Message,
    ModelSelection,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
)


def snapshot(*entries):
    return SessionSnapshot(
        SessionHeader("s1", "/project", "t"), entries, entries[-1].id, len(entries), len(entries)
    )


def chain(*items, parent=None):
    entries = []
    for item in items:
        if isinstance(item, Message):
            item = SessionEntry(item.id, parent, "message", "t", message=item)
        else:
            item = SessionEntry(item.id, parent, item.type, "t", data=item.data)
        entries.append(item)
        parent = item.id
    return entries


def call(identifier, name, path):
    return Message(identifier, "assistant", (ToolCall("c-" + identifier, name, {"path": path}),))


def result(identifier, call_id, name):
    return Message(identifier, "tool_result", tool_result=ToolResult(call_id, name, "ok"))


def capture(entries, keep_recent_tokens=1):
    state = snapshot(*entries)
    return capture_source(
        state,
        ContextProjector().transform_context(state),
        job_id="a" * 32,
        selection=ModelSelection("test", "model", "openai"),
        config_hash="config",
        keep_recent_tokens=keep_recent_tokens,
        tokens_before=123,
    )


def test_first_summary_uses_the_initial_template():
    entries = chain(
        Message("u0", "user", "start"),
        call("a0", "read", "src/app.py"),
        result("t0", "c-a0", "read"),
        Message("u1", "user", "next"),
        Message("a1", "assistant", "done"),
    )
    source = capture(entries)
    assert source.previous_summary is None and source.tokens_before == 123
    assert source.read_files == ("src/app.py",) and source.modified_files == ()
    prompt = history_prompt(source)
    assert prompt.startswith("<conversation>\n[User]: start")
    assert "<previous-summary>" not in prompt
    assert prompt.endswith(SUMMARIZATION_PROMPT)


def test_update_uses_previous_summary_and_accumulates_files_across_compactions():
    head = chain(
        Message("u0", "user", "old"),
        call("a0", "read", "old.py"),
        result("t0", "c-a0", "read"),
        Message("u1", "user", "kept"),
        call("a1", "edit", "new.py"),
        result("t1", "c-a1", "edit"),
    )
    compaction = SessionEntry(
        "c",
        "t1",
        "compaction",
        "t",
        data={
            "summary": "previous summary",
            "first_kept_entry_id": "u1",
            "details": {"read_files": ["old.py"], "modified_files": ["gone.py"]},
        },
    )
    tail = chain(
        Message("u2", "user", "more"),
        Message(
            "a2",
            "assistant",
            (
                ToolCall("c-a2", "read", {"path": "new.py"}),
                ToolCall("c-w2", "write", {"path": "w.py"}),
            ),
        ),
        result("t2", "c-a2", "read"),
        result("w2", "c-w2", "write"),
        Message("u3", "user", "latest"),
        Message("a3", "assistant", "done"),
        parent="c",
    )
    source = capture([*head, compaction, *tail])
    assert source.previous_summary == "previous summary"
    assert source.previous_compaction_id == "c"
    assert [message.id for message in source.messages][0] == "u1"
    assert source.read_files == ("old.py",)
    assert source.modified_files == ("gone.py", "new.py", "w.py")
    prompt = history_prompt(source)
    assert "<previous-summary>\nprevious summary\n</previous-summary>\n\n" in prompt
    assert "previous summary" not in prompt.split("</conversation>")[0]
    assert prompt.endswith(UPDATE_SUMMARIZATION_PROMPT)


def test_split_turn_separates_the_turn_prefix_from_history():
    entries = chain(
        Message("u0", "user", "old " * 200),
        Message("a0", "assistant", "old answer " * 200),
        Message("u1", "user", "current request"),
        Message("a1", "assistant", "progress " * 100),
        Message("a2", "assistant", "recent " * 100),
    )
    source = capture(entries, keep_recent_tokens=50)
    assert source.first_kept_entry_id == "a2"
    assert [message.id for message in source.messages] == ["u0", "a0"]
    assert [message.id for message in source.turn_prefix_messages] == ["u1", "a1"]


def test_cut_point_search_starts_after_the_previous_compaction():
    entries = chain(
        Message("u0", "user", "x" * 4000),
        Message("a0", "assistant", "x" * 4000),
        Message("u1", "user", "small"),
        Message("a1", "assistant", "small"),
    )
    projection = ContextProjector().transform_context(snapshot(*entries))
    assert find_cut_point(tuple(entries), projection, 0, 100000).first_kept_index == 0
    assert find_cut_point(tuple(entries), projection, 2, 100000).first_kept_index == 2
    # Tokens before start_index never count toward the retained budget.
    assert find_cut_point(tuple(entries), projection, 2, 1500).first_kept_index == 2


def test_serialization_omits_thinking_and_attachments_and_truncates_tool_results():
    text = serialize_conversation(
        (
            Message(
                "u",
                "user",
                "look",
                attachments=(Attachment("image/png", "blob:1"),),
            ),
            Message(
                "a",
                "assistant",
                (
                    ThinkingBlock("private reasoning"),
                    TextBlock("reading"),
                    ToolCall("c1", "read", {"path": "a.py", "offset": 2}),
                ),
            ),
            Message(
                "t",
                "tool_result",
                tool_result=ToolResult(
                    "c1", "read", "y" * 2500, details={"full_output_path": "/tmp/pycode/s/c1.log"}
                ),
            ),
            Message("s", "compaction_summary", "older summary"),
        )
    )
    assert "private reasoning" not in text and "blob:1" not in text
    parts = text.split("\n\n")
    assert parts[0] == "[User]: look"
    assert parts[1] == "[Assistant]: reading"
    assert parts[2] == '[Assistant tool calls]: read(path="a.py", offset=2)'
    assert parts[3] == "[Tool result full_output=/tmp/pycode/s/c1.log]: " + "y" * 2000
    assert parts[4] == "[... 500 more characters truncated]"
    assert parts[5].startswith("[User]: The conversation history before this point")


def test_loaded_skills_line_lists_read_skill_files():
    from pycode.context.compaction import format_loaded_skills

    line = format_loaded_skills(["/workspace/a.py", "/skills/user/review/SKILL.md"])
    assert line == "\n\nLoaded skills: review (/skills/user/review/SKILL.md)"
    assert format_loaded_skills(["/workspace/a.py"]) == ""
