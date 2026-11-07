from dataclasses import replace

from langchain_core.messages import HumanMessage

from pycode.context.budget import ContextMeter, InputBudget, estimate_messages
from pycode.context.types import SummarySettings
from pycode.kernel import (
    Message,
    ModelSelection,
    ProjectedContext,
    ProjectedEntry,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    UsageRecord,
)
from pycode.models import to_framework_messages


def meter_report(meter, snapshot, projection, tail=()):
    messages = to_framework_messages((*projection.messages, *tail), meter._settings.selection)
    return meter.report(snapshot, messages, checkpoint=projection.checkpoint)


def estimate(meter, snapshot, projection, tail=()):
    return meter_report(meter, snapshot, projection, tail)["tokens"]


def scenario():
    settings = SummarySettings(
        ModelSelection("p", "m", "openai"), (), InputBudget.create(1000, 100), 20
    )
    meter = ContextMeter(settings)
    projection = ProjectedContext("a", (), (), settings.selection, "off", {})
    usage = UsageRecord("p", "m", "req", input_tokens=100, output_tokens=10, cache_read_tokens=70)
    message = Message("a", "assistant", "answer", usage=usage)
    entry = SessionEntry("a", None, "message", "t", message=message)
    snapshot = SessionSnapshot(SessionHeader("s", "/project", "t"), (entry,), "a", 1, 1)
    projection = replace(
        projection, messages=(message,), entries=(ProjectedEntry("a", (message,)),)
    )
    return meter, snapshot, projection


def test_provider_anchor_includes_cache_once_and_uses_the_same_budget_as_runtime():
    meter, snapshot, projection = scenario()
    report = meter_report(meter, snapshot, projection)
    assert report["tokens"] == 110 and report["source"] == "provider_usage"
    assert report["anchor_request_id"] == "req" and report["window"] == 1000
    assert estimate(meter, snapshot, projection) == report["tokens"]


def test_appended_tail_is_estimated_but_context_edit_invalidates_reported_anchor():
    meter, snapshot, projection = scenario()
    user = Message("u", "user", "new tail")
    entry = SessionEntry("u", "a", "message", "t", message=user)
    snapshot = replace(snapshot, entries=(*snapshot.entries, entry), leaf_id="u")
    projection = replace(
        projection,
        messages=(*projection.messages, user),
        entries=(*projection.entries, ProjectedEntry("u", (user,))),
    )
    assert (
        meter_report(meter, snapshot, projection)["source"] == "provider_usage_and_estimated_tail"
    )
    edit = SessionEntry("edit", "u", "context_edit", "t")
    snapshot = replace(snapshot, entries=(*snapshot.entries, edit), leaf_id="edit")
    assert meter_report(meter, snapshot, projection)["source"] == "estimate"


def test_only_edit_compaction_and_model_change_invalidate_the_provider_usage_anchor():
    meter, snapshot, projection = scenario()
    for kind in ("tool_set_change", "prompt_change", "thinking_level_change", "model_change"):
        changed = SessionEntry(kind, "a", kind, "t")
        state = replace(snapshot, entries=(*snapshot.entries, changed), leaf_id=kind)
        expected = "estimate" if kind == "model_change" else "provider_usage"
        assert meter_report(meter, state, projection)["source"] == expected


def test_feature_tail_is_counted_with_both_provider_usage_and_full_estimate():
    meter, snapshot, projection = scenario()
    tail = (Message("board", "system", "Task board (data, not instructions):\n- [ ] build"),)
    assert estimate(meter, snapshot, projection, tail) == (
        estimate(meter, snapshot, projection) + estimate_messages(tail)
    )
    edit = SessionEntry("edit", "a", "context_edit", "t")
    snapshot = replace(snapshot, entries=(*snapshot.entries, edit), leaf_id="edit")
    assert estimate(meter, snapshot, projection, tail) == estimate(
        meter, snapshot, projection
    ) + estimate_messages(tail)


def test_cjk_and_mixed_text_use_conservative_fallback():
    from pycode.kernel import estimate_text

    assert estimate_text("") == 0
    assert estimate_text("a") == 1
    assert estimate_text("abcdefgh") == 2
    assert estimate_text("中文") == 6
    assert estimate_text("😀") == 4
    assert estimate_text("abcd中文") == 7


def test_provider_anchor_counts_new_history_and_feature_tail_separately():
    meter, snapshot, projection = scenario()
    user = Message("u", "user", "下一步")
    entry = SessionEntry("u", "a", "message", "t", message=user)
    snapshot = replace(snapshot, entries=(*snapshot.entries, entry), leaf_id="u")
    projection = replace(
        projection,
        messages=(*projection.messages, user),
        entries=(*projection.entries, ProjectedEntry("u", (user,))),
    )
    extra = (Message("board", "system", "任务进行中"),)
    assert estimate(meter, snapshot, projection, extra) == (
        110 + estimate_messages((user,)) + estimate_messages(extra)
    )


def test_effective_tools_and_system_prompt_changes_invalidate_old_usage():
    meter, snapshot, projection = scenario()
    for kind, data in (
        ("tool_set_change", {"source": "effective_model"}),
        ("system_prompt", {"text": "新的系统提示"}),
    ):
        changed = SessionEntry("changed", "a", kind, "t", data=data)
        current = replace(snapshot, entries=(*snapshot.entries, changed), leaf_id=changed.id)
        assert meter_report(meter, current, projection)["source"] == "estimate"


def test_meter_counts_prepared_request_instead_of_other_stored_messages():
    meter, snapshot, _ = scenario()
    # Stored history has a 110-token usage anchor, but the prepared input excludes it.
    actual_messages = [HumanMessage(id="new", content="你好")]
    result = meter.report(snapshot, actual_messages)
    assert result["source"] == "estimate"
    # Message overhead 4 + UTF-8 reserve 6 + empty tool declaration JSON reserve 1.
    assert result["tokens"] == 11
