import asyncio

import pytest

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel.domain.completion import RunResult
from pycode.kernel.domain.errors import StorageError
from pycode.kernel.domain.history import EntryDraft
from pycode.kernel.domain.messages import Message
from pycode.session.domain.execution_recovery import pending_run_entry
from pycode.session.services.agent_session import AgentSession
from tests.models.adapters.test_factory import configuration
from tests.models.services.test_model_control import controller, store_for


class Launcher:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = None

    async def run(self, provider, store, inputs, history):
        self.calls.append((provider, inputs, history))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        reply = await store.append_message(
            Message("assistant-" + inputs[0].id, "assistant", "done")
        )
        # The runtime writes run_end when its RunState enters finished.
        start = pending_run_entry(await store.snapshot())
        await store.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "run_end",
                    "input_id": inputs[0].id,
                    "input_ids": [inputs[0].id],
                    "reason": "completed",
                    "final_entry_id": reply,
                },
                run_id=start.run_id,
            )
        )
        return RunResult("completed", "done", ())

    async def aclose(self):
        return None


async def assemble(tmp_path):
    provider = configuration("deepseek")
    store = store_for(tmp_path)
    models = controller(tmp_path, (provider,), store)
    await models.list_models(refresh=False)
    launcher = Launcher()
    session = AgentSession(store, ContextProjector(), models, launcher, provider)
    return session, store, launcher


async def test_settled_input_retries_return_result_without_relaunching_model_or_tools(tmp_path):
    session, store, launcher = await assemble(tmp_path)
    user = Message("input-1", "user", "request")
    assert (await session.run(user)).final_text == "done"
    assert (await session.run(user)).final_text == "done"
    assert len(launcher.calls) == 1
    assert (
        len([entry for entry in (await store.snapshot()).entries if entry.type == "message"]) == 2
    )
    await session.aclose()


async def test_session_has_one_active_executor_and_cannot_switch_branch_mid_run(tmp_path):
    session, _, launcher = await assemble(tmp_path)
    launcher.release = asyncio.Event()
    running = asyncio.create_task(session.run(Message("input-1", "user", "request")))
    await launcher.started.wait()
    with pytest.raises(StorageError, match="active executor"):
        await session.run(Message("input-2", "user", "other"))
    with pytest.raises(StorageError, match="settle"):
        await session.branch(None)
    launcher.release.set()
    await running
    await session.aclose()


async def test_interrupted_execution_with_history_is_not_blindly_reexecuted(tmp_path):
    session, store, launcher = await assemble(tmp_path)
    user = Message("input-1", "user", "request")
    await store.append_message(user)
    await store.append_message(Message("partial-1", "assistant", "tool requested"))
    with pytest.raises(StorageError) as error:
        await session.run(user)
    assert error.value.code == "execution_recovery_required"
    assert launcher.calls == []
    await session.aclose()


async def test_failure_outside_the_runtime_still_ends_the_run_once(tmp_path):
    session, store, launcher = await assemble(tmp_path)

    async def broken(provider, store, inputs, history):
        raise RuntimeError("launcher broke")

    launcher.run = broken
    with pytest.raises(RuntimeError):
        await session.run(Message("input-1", "user", "request"))
    entries = (await store.snapshot()).entries
    assert [e.data["reason"] for e in entries if e.data.get("kind") == "run_end"] == ["error"]
    await session.aclose()
