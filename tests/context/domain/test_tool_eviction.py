import json
from dataclasses import replace
from pathlib import Path

import pytest

from pycode.context.domain.tool_eviction import EvictionBudget, select_old_results
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel.domain.history import SessionEntry, SessionHeader, SessionSnapshot
from pycode.kernel.domain.messages import Message, ToolCall, ToolResult

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/tool-output-baselines.json").read_text()
)


def snapshot_for(results):
    entries = [SessionEntry("u1", None, "message", "t", message=Message("u1", "user", "first"))]
    previous = "u1"
    for index, (name, tokens) in enumerate(results):
        identifier = f"r{index}"
        call_entry = f"a{index}"
        entries.append(
            SessionEntry(
                call_entry,
                previous,
                "message",
                "t",
                message=Message(
                    call_entry, "assistant", content=(ToolCall(f"c{index}", name, {}),)
                ),
            )
        )
        previous = call_entry
        entries.append(
            SessionEntry(
                identifier,
                previous,
                "message",
                "t",
                message=Message(
                    identifier,
                    "tool_result",
                    tool_result=ToolResult(f"c{index}", name, "x" * (tokens * 4)),
                ),
            )
        )
        previous = identifier
    for index in range(2, 4):
        identifier = f"u{index}"
        entries.append(
            SessionEntry(
                identifier, previous, "message", "t", message=Message(identifier, "user", "recent")
            )
        )
        previous = identifier
    return SessionSnapshot(SessionHeader("s1", "/project", "t"), tuple(entries), previous, 0, 0)


@pytest.mark.parametrize("case", VECTORS["eviction"], ids=lambda case: case["name"])
def test_upstream_tool_pruning_vectors(case):
    snapshot = snapshot_for([(case["tool_name"], case["tokens"])])
    projection = ContextProjector().transform_context(snapshot)
    selection = select_old_results(snapshot, projection, EvictionBudget(200000))
    assert bool(selection.entry_ids) == case["expected_selected"]


def test_40k_protection_20k_threshold_and_whole_boundary_selection():
    snapshot = snapshot_for([("Bash", 25000), ("Bash", 40000)])
    selection = select_old_results(
        snapshot, ContextProjector().transform_context(snapshot), EvictionBudget(200000)
    )
    assert selection.entry_ids == ("r0",)
    assert selection.tokens == 25000
    smaller = snapshot_for([("Bash", 19999), ("Bash", 40000)])
    assert (
        select_old_results(
            smaller, ContextProjector().transform_context(smaller), EvictionBudget(200000)
        ).entry_ids
        == ()
    )
    exact = snapshot_for([("Bash", 20000), ("Bash", 40000)])
    assert select_old_results(
        exact, ContextProjector().transform_context(exact), EvictionBudget(200000)
    ).entry_ids == ("r0",)


def test_window_proportions_and_recent_region_are_respected():
    assert EvictionBudget(32000).limits() == (8000, 4000)
    snapshot = snapshot_for([("Bash", 50000)])
    shortened = SessionSnapshot(snapshot.header, snapshot.entries[:-1], "u2", 0, 0)
    assert (
        select_old_results(
            shortened, ContextProjector().transform_context(shortened), EvictionBudget(200000)
        ).entry_ids
        == ()
    )


def test_orphan_results_removed_from_model_projection_do_not_consume_eviction_budget():
    snapshot = snapshot_for([("Bash", 50000)])
    entries = tuple(
        replace(entry, parent_id="u1") if entry.id == "r0" else entry
        for entry in snapshot.entries
        if entry.id != "a0"
    )
    snapshot = replace(snapshot, entries=entries)
    selection = select_old_results(
        snapshot, ContextProjector().transform_context(snapshot), EvictionBudget(200000)
    )
    assert selection.entry_ids == () and selection.tokens == 0
