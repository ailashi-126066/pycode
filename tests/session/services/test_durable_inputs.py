import pytest

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import EntryDraft, Message, SessionHeader, StorageError
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.session.services.durable_inputs import DurableInputs
from tests.support.requests import TestTiming


async def setup(tmp_path):
    store = SqliteSessionStore(tmp_path / "s1", SessionHeader("s1", "/project", "t"), TestTiming())
    await store.append_message(Message("u1", "user", "task"))
    await store.append_entry(
        EntryDraft(
            "custom",
            data={"kind": "run_start", "input_id": "u1", "execution_session_id": "s1"},
            run_id="r1",
        )
    )
    return store, DurableInputs(store)


async def test_queued_input_is_durable_invisible_and_idempotent_until_real_model_consumption(
    tmp_path,
):
    store, queue = await setup(tmp_path)
    user = Message("steer-1", "user", "correct the task")
    assert await queue.submit(user, "steer", "r1")
    assert not await queue.submit(user, "steer", "r1")
    assert [
        message.id
        for message in ContextProjector().transform_context(await store.snapshot()).messages
    ] == ["u1"]
    with pytest.raises(StorageError):
        await queue.submit(Message("steer-1", "user", "different"), "steer", "r1")
    with pytest.raises(StorageError):
        await queue.submit(user, "follow_up", "r1")
    await store.aclose()
    store = SqliteSessionStore(tmp_path / "s1", SessionHeader("s1", "/project", "t"), TestTiming())
    queue = DurableInputs(store)
    selected = await queue.pending("r1")
    assert selected.take("steer") == (user,)
    source = await store.append_message(user)
    assert (await queue.pending("r1")).has("steer")
    await store.append_message(
        Message(
            "a1",
            "assistant",
            "done",
            provider_metadata={
                "pycode_run_id": "r1",
                "pycode_turn": 2,
                "pycode_source_leaf_id": source,
            },
        )
    )
    assert not (await queue.pending("r1")).has("steer")
    assert not await queue.submit(user, "steer", "r1")
    await store.aclose()


async def test_older_response_does_not_consume_input_arriving_during_that_request(tmp_path):
    store, queue = await setup(tmp_path)
    source = (await store.snapshot()).leaf_id
    await queue.submit(Message("later", "user", "follow up"), "follow_up", "r1")
    await store.append_message(Message("later", "user", "follow up"))
    await store.append_message(
        Message(
            "a1",
            "assistant",
            "old response",
            provider_metadata={
                "pycode_run_id": "r1",
                "pycode_source_leaf_id": source,
            },
        )
    )
    assert (await queue.pending("r1")).has("follow_up")
    await store.aclose()


async def test_cross_branch_input_id_and_finished_run_are_rejected(tmp_path):
    store, queue = await setup(tmp_path)
    before = (await store.snapshot()).leaf_id
    user = Message("q1", "user", "queued")
    await queue.submit(user, "steer", "r1")
    await store.branch(before)
    with pytest.raises(StorageError) as error:
        await queue.submit(user, "steer", "r1")
    assert error.value.code == "input_branch_conflict"
    await store.append_entry(EntryDraft("custom", data={"kind": "run_end"}, run_id="r1"))
    with pytest.raises(StorageError) as error:
        await queue.submit(Message("q2", "user", "too late"), "steer", "r1")
    assert error.value.code == "run_settled"
    await store.aclose()
