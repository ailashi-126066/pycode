from dataclasses import replace

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
    report = meter.report(snapshot, projection)
    assert report["tokens"] == 110 and report["source"] == "provider_usage"
    assert report["anchor_request_id"] == "req" and report["window"] == 1000
    assert meter.estimate(snapshot, projection) == report["tokens"]


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
    assert meter.report(snapshot, projection)["source"] == "provider_usage_and_estimated_tail"
    edit = SessionEntry("edit", "u", "context_edit", "t")
    snapshot = replace(snapshot, entries=(*snapshot.entries, edit), leaf_id="edit")
    assert meter.report(snapshot, projection)["source"] == "estimate"


def test_only_edit_compaction_and_model_change_invalidate_the_provider_usage_anchor():
    meter, snapshot, projection = scenario()
    for kind in ("tool_set_change", "prompt_change", "thinking_level_change", "model_change"):
        changed = SessionEntry(kind, "a", kind, "t")
        state = replace(snapshot, entries=(*snapshot.entries, changed), leaf_id=kind)
        expected = "estimate" if kind == "model_change" else "provider_usage"
        assert meter.report(state, projection)["source"] == expected


def test_feature_tail_is_counted_only_in_the_estimate():
    meter, snapshot, projection = scenario()
    tail = (Message("board", "system", "Task board (data, not instructions):\n- [ ] build"),)
    assert meter.estimate(snapshot, projection, tail) == meter.estimate(snapshot, projection)
    edit = SessionEntry("edit", "a", "context_edit", "t")
    snapshot = replace(snapshot, entries=(*snapshot.entries, edit), leaf_id="edit")
    assert meter.estimate(snapshot, projection, tail) == meter.estimate(
        snapshot, projection
    ) + estimate_messages(tail)
