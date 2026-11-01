"""Persistent restart recovery ends lost runs without executing their old calls.

Recovery only writes records: this process never emitted a start for those calls.
"""

from dataclasses import replace

from pycode.kernel import EntryDraft, Message, SessionTree
from pycode.session.domain.inputs import pending_input_entries
from pycode.tools import abandoned_result, interrupted_result


async def recover_conversation(
    store,
    *,
    reason="interrupted",
    started_only=False,
    finish_runs=True,
):
    snapshot = await store.snapshot()
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    paired = set()
    ended = set()
    starts = []
    expired_inputs = set()
    for entry in snapshot.entries:
        if entry.data.get("kind") == "run_end":
            ended.add(entry.run_id)
        if entry.type == "input_expired":
            expired_inputs.add(entry.data["input_entry_id"])
    started_operations = {
        entry.operation_id: entry.data["replay"]
        for entry in path
        if entry.data.get("kind") == "tool_started"
    }
    for entry in path:
        if entry.message is not None and entry.message.tool_result is not None:
            result = entry.message.tool_result
            paired.add((result.details["source_assistant_id"], result.call_id))
        if entry.data.get("kind") == "run_start":
            starts.append(entry)
    for entry in path:
        message = entry.message
        if message is None or message.role != "assistant":
            continue
        for call in message.tool_calls:
            run_id = entry.run_id or message.provider_metadata.get("pycode_run_id", "")
            key = (message.id, call.id)
            if key in paired:
                continue
            operation_id = f"{run_id}:{message.id}:{call.id}"
            replay = started_operations.get(operation_id)
            # Continuing a run reruns calls that never started and replay-safe calls.
            if started_only and replay in {None, "safe"}:
                continue
            if operation_id in started_operations:
                result = interrupted_result(
                    call, "the previous process ended without a persistent result"
                )
            else:
                result = abandoned_result(call)
            result = replace(
                result,
                session_id=snapshot.header.session_id,
                run_id=run_id,
                operation_id=operation_id,
                details={**result.details, "source_assistant_id": message.id},
            )
            await store.append_message(
                Message(f"interrupted:{message.id}:{call.id}", "tool_result", tool_result=result)
            )
            paired.add(key)
    if not finish_runs:
        return
    for start in starts:
        if (
            start.run_id in ended
            or start.data.get("execution_session_id", snapshot.header.session_id)
            != snapshot.header.session_id
        ):
            continue
        current = await store.snapshot()
        for queued in pending_input_entries(current, start.run_id):
            if queued.id in expired_inputs:
                continue
            await store.append_entry(
                EntryDraft(
                    "input_expired",
                    data={"input_entry_id": queued.id, "queue_kind": queued.data["queue_kind"]},
                    run_id=start.run_id,
                )
            )
        await store.append_entry(
            EntryDraft(
                "custom",
                run_id=start.run_id,
                data={
                    "kind": "run_end",
                    "input_id": start.data["input_id"],
                    "reason": reason,
                    "final_entry_id": None,
                },
            )
        )
    return await store.snapshot()
