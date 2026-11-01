# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/compaction.ts
# Source: MewCode local mewcode/context/summary_cache.py; SHA-256:
# e291f27c5264240e6a535b4ec997e0958cec665f5be1e36f0f9f5f7b08621db0
# Adaptation: canonical projected entry IDs, prefix/config hashes and paired native cuts.
"""Capture and validate a fixed summary prefix independently of appended tail messages."""

from pycode.context.domain.compaction import find_cut_point
from pycode.context.domain.file_ops import (
    FileOperations,
    compute_file_lists,
    extract_file_ops_from_message,
)
from pycode.context.domain.history_boundaries import valid_kept_boundary
from pycode.context.domain.summaries import SummarySource
from pycode.kernel import (
    ModelSelection,
    ProjectedContext,
    SessionSnapshot,
    SessionTree,
    clone_value,
    data_hash,
    plain_data,
)


# Pi: prepareCompaction
def capture_source(
    snapshot: SessionSnapshot,
    projection: ProjectedContext,
    *,
    job_id: str,
    selection: ModelSelection,
    config_hash: str,
    keep_recent_tokens: int,
    tokens_before: int,
) -> SummarySource | None:
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    by_id = {entry.id: entry for entry in path}
    entries = tuple(by_id[item.entry_id] for item in projection.entries)
    previous_index = next(
        (
            index
            for index, item in enumerate(projection.entries)
            if entries[index].type == "compaction" and item.messages
        ),
        -1,
    )
    start = previous_index + 1
    cut = find_cut_point(entries, projection, start, keep_recent_tokens)
    index = cut.first_kept_index
    while index > start and not valid_kept_boundary(path, entries[index].id):
        index -= 1
    if index <= start:
        return None
    split = cut.split_turn and start <= cut.turn_start_index < index
    history_end = cut.turn_start_index if split else index

    def summarized(begin, end):
        return tuple(
            message
            for item, entry in zip(projection.entries[begin:end], entries[begin:end], strict=True)
            if entry.type != "compaction"
            for message in item.messages
            if message.role != "system"
        )

    messages = summarized(start, history_end)
    turn_prefix = summarized(history_end, index) if split else ()
    if not messages and not turn_prefix:
        return None
    file_ops = FileOperations()
    previous_summary = None
    if previous_index >= 0:
        previous = entries[previous_index]
        previous_summary = previous.data["summary"]
        # Compactions written before file tracking have no details.
        details = previous.data.get("details")
        if details is not None:
            file_ops.read.update(details["read_files"])
            file_ops.edited.update(details["modified_files"])
    for message in (*messages, *turn_prefix):
        extract_file_ops_from_message(message, file_ops)
    read_files, modified_files = compute_file_lists(file_ops)
    included = projection.entries[:index]
    visible = tuple(item for item in included if any(m.role != "system" for m in item.messages))
    assert snapshot.leaf_id is not None
    return SummarySource(
        job_id,
        snapshot.header.session_id,
        snapshot.leaf_id,
        tuple(item.entry_id for item in visible),
        entries[index].id,
        entries[previous_index].id if previous_index >= 0 else None,
        data_hash([plain_data(item) for item in visible]),
        config_hash,
        previous_summary,
        clone_value(messages),
        clone_value(turn_prefix),
        tuple(read_files),
        tuple(modified_files),
        tokens_before,
        clone_value(selection),
    )


def valid_source(
    source: SummarySource, snapshot: SessionSnapshot, projection: ProjectedContext, config_hash: str
) -> bool:
    if source.session_id != snapshot.header.session_id or source.config_hash != config_hash:
        return False
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    previous = next((entry.id for entry in reversed(path) if entry.type == "compaction"), None)
    if previous != source.previous_compaction_id or source.first_kept_entry_id not in {
        e.id for e in path
    }:
        return False
    visible = tuple(
        item for item in projection.entries if any(m.role != "system" for m in item.messages)
    )
    prefix = visible[: len(source.source_entry_ids)]
    if tuple(item.entry_id for item in prefix) != source.source_entry_ids:
        return False
    return data_hash([plain_data(item) for item in prefix]) == source.prefix_hash
