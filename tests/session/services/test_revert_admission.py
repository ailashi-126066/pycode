from dataclasses import asdict

import pytest

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import EntryDraft, Message, SessionHeader, SessionSnapshot, StorageError
from pycode.session.domain.revert import RevertPlan
from pycode.session.domain.revert_state import pending_revert
from pycode.session.services.agent_session import AgentSession
from pycode.session.services.durable_inputs import DurableInputs
from tests.models.adapters.test_factory import configuration
from tests.models.services.test_model_control import controller, store_for
from tests.session.services.test_agent_session import Launcher


async def test_pending_revert_blocks_all_mutation_commands_and_input_delivery(tmp_path):
    provider = configuration("deepseek")
    store = store_for(tmp_path)
    launcher = Launcher()
    models = controller(tmp_path, (provider,), store)
    await models.list_models(refresh=False)
    session = AgentSession(
        store, ContextProjector(), models, launcher, provider, DurableInputs(store)
    )
    plan = RevertPlan("s1", None, None, "dialogue", transaction_id="tx")
    await store.append_entry(
        EntryDraft(
            "custom",
            data={
                "kind": "revert_started",
                "session_id": "s1",
                "transaction_id": "tx",
                "plan": asdict(plan),
            },
        )
    )
    before = await store.snapshot()
    calls = [
        lambda: session.run(Message("u1", "user", "new")),
        lambda: session.resume(),
        lambda: session.branch(None),
        lambda: session.compact(),
        lambda: session.preview_revert(None),
        lambda: session.revert(plan),
        lambda: session.select_model(provider.name, provider.model_id),
        lambda: session.submit_input(Message("u2", "user", "queued"), "steer"),
    ]
    try:
        for call in calls:
            with pytest.raises(StorageError) as error:
                await call()
            assert error.value.code == "revert_recovery_required"
        assert launcher.calls == []
        assert await store.snapshot() == before
        await store.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "revert_completed",
                    "session_id": "s1",
                    "transaction_id": "tx",
                },
            )
        )
        assert (await session.run(Message("u1", "user", "new"))).outcome == "completed"
    finally:
        await session.aclose()


async def test_fork_does_not_inherit_parent_revert_executor(tmp_path):
    store = store_for(tmp_path)
    try:
        await store.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "revert_started",
                    "session_id": "s1",
                    "transaction_id": "tx",
                },
            )
        )
        parent = await store.snapshot()
        child = SessionSnapshot(
            SessionHeader("child", "/project", "t", parent_session_id="s1"),
            parent.entries,
            parent.leaf_id,
            parent.revision,
            parent.record_count,
        )
        assert pending_revert(parent) is not None
        assert pending_revert(child) is None
    finally:
        await store.aclose()


async def test_pending_approval_blocks_compaction_and_revert_preview(tmp_path):
    provider = configuration("deepseek")
    store = store_for(tmp_path)
    models = controller(tmp_path, (provider,), store)
    session = AgentSession(store, ContextProjector(), models, Launcher(), provider)
    await store.append_entry(
        EntryDraft(
            "custom",
            data={
                "kind": "run_start",
                "execution_session_id": "s1",
                "input_id": "u1",
            },
            run_id="run",
        )
    )
    try:
        for call in (session.compact, lambda: session.preview_revert(None)):
            with pytest.raises(StorageError) as error:
                await call()
            assert error.value.code == "session_needs_input"
    finally:
        await session.aclose()
