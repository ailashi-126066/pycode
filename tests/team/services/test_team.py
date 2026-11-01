"""Real durable team transitions with controllable child sessions and no models."""

import asyncio
from types import SimpleNamespace

import pytest

from pycode.kernel import EntryDraft
from pycode.kernel.domain.completion import RunResult
from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.messages import Message, ToolResult
from pycode.session.services.durable_inputs import DurableInputs
from pycode.team.api import AgentTeam
from tests.models.services.test_model_control import store_for


class Events:
    def __init__(self):
        self.values = []

    async def emit(self, kind, data):
        self.values.append((kind, data))


class Child:
    def __init__(self):
        self.release = asyncio.Event()
        self.inputs = []
        self.answers = []
        self.result = RunResult("completed", "child result", ())
        self.closed = False

    async def run(self, message):
        self.inputs.append(message)
        await self.release.wait()
        return self.result

    async def resume(self, answers):
        self.answers.append(answers)
        return RunResult("completed", "approved child", ())

    async def record_team_parent(self, data):
        self.parent = data

    def prepare_handoff(self):
        self.handoff_prepared = True

    async def continue_handoff(self):
        return self.result

    async def task_evidence(self):
        return SimpleNamespace(entries=())

    async def cancel(self):
        return False

    async def aclose(self):
        self.closed = True


class Children:
    workspace = "/project"

    def __init__(self):
        self.values = {}
        self.open_started = asyncio.Event()
        self.open_release = None

    async def open(self, record, member, **options):
        self.open_started.set()
        if self.open_release is not None:
            await self.open_release.wait()
        return self.values.setdefault(record["agent_id"], Child())

    async def evidence(self, record):
        return await self.values[record["agent_id"]].task_evidence()

    def reassign(self, session, record, member):
        self.values[record["agent_id"]] = session

    async def input_accepted(self, record, input_id):
        return any(message.id == input_id for message in self.values[record["agent_id"]].inputs)


def team_at(tmp_path, *, limit=4):
    ledger, lead = store_for(tmp_path / "ledger"), store_for(tmp_path / "lead")
    return AgentTeam(ledger, lead, Children(), None, Events(), max_agents=limit)


async def close(team):
    await team.aclose()
    await team.lead_history.aclose()


async def spawn(team, **kwargs):
    return await team.spawn(
        "readonly", "reviewer", "inspect", SimpleNamespace(name="test", model_id="model"), **kwargs
    )


async def test_atomic_task_claim_dependencies_and_explicit_failure_are_preserved(tmp_path):
    team = team_at(tmp_path)
    try:
        first = await team.board.create_task("first")
        second = await team.board.create_task("second", depends_on=[first["task_id"]])
        with pytest.raises(ToolError) as error:
            await team.board.claim_task(second["task_id"], "b")
        assert error.value.code == "dependencies_pending"
        claims = await asyncio.gather(
            team.board.claim_task(first["task_id"], "a"),
            team.board.claim_task(first["task_id"], "b"),
            return_exceptions=True,
        )
        assert sum(isinstance(value, dict) for value in claims) == 1
        owner = team.board.records[first["task_id"]]["assignee"]
        await team.board.update_task(
            first["task_id"], actor=owner, status="failed", result="test failed"
        )
        assert team.board.settled(owner, "completed", "model says success") == []
        assert team.board.records[first["task_id"]]["result"] == "test failed"
    finally:
        await close(team)


async def test_spawn_limits_cancel_starting_and_parent_cancel_do_not_revive_children(tmp_path):
    team = team_at(tmp_path, limit=1)
    try:
        record = await spawn(team)
        with pytest.raises(ToolError) as error:
            await spawn(team)
        assert error.value.code == "agent_limit_reached"
        await team.inbox.send("lead", record["agent_id"], "queued correction", "run")
        await team.cancel_all()
        assert team.active_count() == 0
        assert team.records[record["agent_id"]]["status"] == "cancelled"
        await team.schedule_wakes()
        assert team.active_count() == 0
        assert team.inbox.pending(record["agent_id"]) == []
    finally:
        await close(team)


async def test_startup_cancellation_waits_for_owned_opener(tmp_path):
    team = team_at(tmp_path)
    team.children.open_release = asyncio.Event()
    task = asyncio.create_task(spawn(team))
    try:
        await team.children.open_started.wait()
        identifier = next(iter(team.records))
        await team.cancel(identifier)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert team.active_count() == 0
        assert not team.sessions
    finally:
        await close(team)


async def test_message_injected_before_model_and_wait_delivery_is_acknowledged_after_settlement(
    tmp_path,
):
    team = team_at(tmp_path)
    try:
        record = await spawn(team)
        await team.inbox.send("lead", record["agent_id"], "new constraint", "run")
        child_store = store_for(tmp_path / "child")
        await child_store.append_message(Message("child-input", "user", "inspect"))
        await child_store.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "run_start",
                    "input_id": "child-input",
                    "execution_session_id": (await child_store.snapshot()).header.session_id,
                },
                run_id="child-run",
            )
        )
        context = SimpleNamespace(durable_inputs=DurableInputs(child_store), run_id="child-run")
        await team.member(record["agent_id"]).flush_inbox(context)
        pending = await context.durable_inputs.pending("child-run")
        assert pending.has("steer")
        await team.inbox.send(record["agent_id"], "lead", "question", "child-run")
        result = await team.inbox.wait(operation_id="op")
        assert result["messages"][0]["status"] == "delivering"
        await team.member("lead").settle_tool("op", accepted=False)
        assert team.inbox.pending("lead")
        await team.inbox.wait(operation_id="op2")
        await team.member("lead").settle_tool(
            ToolResult(
                "wait",
                "wait_agents",
                "",
                operation_id="op2",
                details={
                    "team_message_ids": [
                        m["id"] for m in team.inbox.records.values() if m["recipient"] == "lead"
                    ]
                },
            ),
            accepted=True,
        )
        assert not team.inbox.pending("lead")
        await child_store.aclose()
    finally:
        await close(team)


async def test_child_approval_forwarding_and_resume_are_operation_bound(tmp_path):
    team = team_at(tmp_path)
    try:
        record = await spawn(team)
        child = team.children.values[record["agent_id"]]
        question = {"kind": "permission", "operation_id": "write-op", "interrupt_id": "i1"}
        child.result = RunResult("needs_input", "", (), (question,))
        child.release.set()
        await team.drivers[record["agent_id"]]
        assert team.records[record["agent_id"]]["status"] == "needs_approval"
        assert any(data["type"] == "approval" for _, data in team.events.values)
        with pytest.raises(Exception, match="identify"):
            await team.answer(record["agent_id"], {"operation_id": "wrong", "decision": "approve"})
        await team.answer(record["agent_id"], {"operation_id": "write-op", "decision": "approve"})
        await team.drivers[record["agent_id"]]
        assert team.records[record["agent_id"]]["status"] == "completed"
    finally:
        await close(team)


async def test_completed_child_wakes_for_direct_message_and_new_process_does_not_auto_run(tmp_path):
    team = team_at(tmp_path)
    try:
        record = await spawn(team)
        child = team.children.values[record["agent_id"]]
        child.release.set()
        await team.drivers[record["agent_id"]]
        await team.inbox.send(
            "lead", record["agent_id"], "resolve conflict in file only", "lead-run"
        )
        await team.drivers[record["agent_id"]]
        assert len(child.inputs) == 2
        assert "resolve conflict" in child.inputs[-1].text
        persisted = {**team.records[record["agent_id"]], "status": "running"}
        await team.commit(agents=[persisted])
        restored = AgentTeam(
            team.ledger,
            team.lead_history,
            Children(),
            None,
            Events(),
        )
        await restored.initialize()
        assert restored.records[record["agent_id"]]["status"] == "interrupted"
        assert restored.children.values == {} and not restored.tasks
    finally:
        await close(team)


class Worktrees:
    def __init__(self):
        self.record = {
            "id": "a" * 32,
            "path": "/worker",
            "branch": "pycode/team/" + "a" * 32,
            "base": "b" * 40,
            "worker_stopped": False,
        }
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.result = {"merged": False, "error_code": "merge_conflict", "conflicts": ["file.py"]}

    async def create(self, **options):
        self.started.set()
        await self.release.wait()
        return self.record

    async def inspect(self, identifier):
        return self.record

    async def update(self, record):
        self.record = record

    async def changes(self, identifier):
        return {
            "fingerprint": "tree",
            "diff": "change",
            "source_tree": "tree",
            "files": ["file.py"],
        }

    async def merge_preview(self, identifier, fingerprint, action):
        return {"fingerprint": fingerprint, "target_head": "head", "preview_id": "preview"}

    async def merge_into_workspace(self, identifier, preview, action):
        return self.result

    async def complete_team_merge(self, identifier, commit):
        return {"removed": True}


async def test_conflict_retains_completed_worker_then_can_wake_to_resolve(tmp_path):
    team = team_at(tmp_path)
    team.worktrees = Worktrees()
    try:
        record = await team.spawn(
            "worker", "engineer", "change", SimpleNamespace(name="test", model_id="model")
        )
        child = team.children.values[record["agent_id"]]
        child.release.set()
        await team.drivers[record["agent_id"]]
        await team.review(record["agent_id"])
        await team.board.update_task(record["task_id"], actor="lead", status="done")
        preview = await team.merge_preview(record["agent_id"])
        result = await team.merge(record["agent_id"], preview)
        assert result["error_code"] == "merge_conflict"
        assert team.records[record["agent_id"]]["status"] == "completed"
        assert not child.closed
        await team.inbox.send("lead", record["agent_id"], "resolve the conflicting file", "run")
        await team.drivers[record["agent_id"]]
        assert len(child.inputs) == 2
    finally:
        await close(team)


async def test_cancellation_during_worktree_create_retains_late_owned_record(tmp_path):
    team = team_at(tmp_path)
    team.worktrees = Worktrees()
    team.worktrees.release.clear()
    task = asyncio.create_task(
        team.spawn("worker", "engineer", "change", SimpleNamespace(name="test", model_id="model"))
    )
    try:
        await team.worktrees.started.wait()
        identifier = next(iter(team.records))
        task.cancel()
        team.worktrees.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert team.records[identifier]["worktree_id"] == "a" * 32
        assert team.records[identifier]["status"] == "cancelled"
        assert team.worktrees.record["worker_stopped"]
    finally:
        await close(team)


async def test_corrupt_atomic_transition_does_not_partially_apply(tmp_path):
    team = team_at(tmp_path)
    try:
        with pytest.raises(TypeError):
            team.apply(
                {
                    "agents": None,
                    "tasks": [
                        {"task_id": "t1", "status": "open", "title": "bad", "dependencies": []}
                    ],
                }
            )
        assert not team.records and not team.board.records
        from pycode.kernel import StorageError

        with pytest.raises(StorageError):
            team.apply({"agents": [{"agent_id": "12345678", "status": []}], "messages": []})
        assert not team.records
    finally:
        await close(team)


async def test_synthetic_interrupted_wait_result_does_not_ack_undelivered_messages(tmp_path):
    team = team_at(tmp_path)
    try:
        record = await spawn(team)
        await team.inbox.send(record["agent_id"], "lead", "important result", "child-run")
        result = await team.inbox.wait(operation_id="interrupted-wait")
        await team.lead_history.append_message(
            Message(
                "tool-result",
                "tool_result",
                tool_result=ToolResult(
                    "wait",
                    "wait_agents",
                    "interrupted",
                    is_error=True,
                    error_code="tool_interrupted",
                    operation_id="interrupted-wait",
                ),
            )
        )
        restored = AgentTeam(
            team.ledger,
            team.lead_history,
            Children(),
            None,
            Events(),
        )
        await restored.initialize()
        assert result["messages"][0]["id"] in {m["id"] for m in restored.inbox.pending("lead")}
        assert not restored.tasks
    finally:
        await close(team)


async def test_fifo_wakes_follow_first_new_message_instead_of_agent_creation_order(tmp_path):
    team = team_at(tmp_path, limit=1)
    try:
        first = await spawn(team)
        team.children.values[first["agent_id"]].release.set()
        await team.drivers[first["agent_id"]]
        second = await spawn(team)
        team.children.values[second["agent_id"]].release.set()
        await team.drivers[second["agent_id"]]
        # Each second run has its own gate. The initial completed runs left the
        # gates set, which otherwise lets a FIFO wake finish before cancellation
        # of the blocker returns and makes an active-driver assertion race.
        first_child = team.children.values[first["agent_id"]]
        second_child = team.children.values[second["agent_id"]]
        first_child.release.clear()
        second_child.release.clear()
        blocker = await spawn(team)
        await team.inbox.send("lead", second["agent_id"], "second gets first slot", "run")
        await team.inbox.send("lead", first["agent_id"], "first gets later slot", "run")
        assert list(team.wake_eligible)[:2] == [second["agent_id"], first["agent_id"]]
        await team.cancel(blocker["agent_id"])
        await team.schedule_wakes()
        assert second["agent_id"] in team.drivers
        assert first["agent_id"] not in team.drivers
        second_driver = team.drivers[second["agent_id"]]
        second_child.release.set()
        await second_driver
        assert first["agent_id"] in team.drivers
        first_driver = team.drivers[first["agent_id"]]
        first_child.release.set()
        await first_driver
        assert len(first_child.inputs) == len(second_child.inputs) == 2
        assert not team.inbox.pending(first["agent_id"])
        assert not team.inbox.pending(second["agent_id"])
        assert all(
            message["status"] == "delivered"
            for message in team.inbox.records.values()
            if message["recipient"] in {first["agent_id"], second["agent_id"]}
        )
    finally:
        await close(team)


async def test_degraded_team_waits_fail_explicitly_instead_of_hanging(tmp_path):
    team = team_at(tmp_path)
    try:
        await spawn(team)
        team.degraded = True
        with pytest.raises(Exception, match="persistence"):
            await team.wait_lead_input()
        with pytest.raises(ToolError) as error:
            await team.inbox.wait()
        assert error.value.code == "team_storage_error"
    finally:
        await close(team)


async def test_failed_startup_without_jsonl_cannot_queue_unrecoverable_wake(tmp_path):
    team = team_at(tmp_path)

    async def failed_open(*args, **kwargs):
        raise ToolError("session_not_found", "No child was created")

    team.children.open = failed_open
    try:
        with pytest.raises(ToolError) as error:
            await spawn(team)
        assert error.value.code == "agent_start_failed"
        identifier = next(iter(team.records))
        with pytest.raises(ToolError) as error:
            await team.inbox.send("lead", identifier, "retry startup", "run")
        assert error.value.code == "agent_closed"
        assert not team.inbox.pending(identifier)
        restored = AgentTeam(
            team.ledger,
            team.lead_history,
            Children(),
            None,
            Events(),
        )
        await restored.initialize()
        assert restored.records[identifier]["status"] == "failed"
        assert not restored.tasks
    finally:
        await close(team)


async def test_recoverable_child_open_failure_does_not_degrade_other_spawns(tmp_path):
    team = team_at(tmp_path)
    opener = team.children.open

    async def failed_open(*args, **kwargs):
        raise ToolError("model_unavailable", "Child failed before opening")

    try:
        team.children.open = failed_open
        with pytest.raises(ToolError) as error:
            await spawn(team)
        assert error.value.code == "agent_start_failed"
        await asyncio.sleep(0)
        assert not team.degraded
        team.children.open = opener
        record = await spawn(team)
        assert record["status"] == "running" and team.active_count() == 1
    finally:
        await close(team)


@pytest.mark.parametrize("has_ended_run", [False, True])
async def test_inbox_flush_releases_messages_rejected_by_native_run_ownership(
    tmp_path, has_ended_run
):
    team = team_at(tmp_path)
    child_store = store_for(tmp_path / "child")
    try:
        record = await spawn(team)
        sent = await team.inbox.send("lead", record["agent_id"], "late correction", "lead-run")
        await child_store.append_message(Message("child-input", "user", "inspect"))
        if has_ended_run:
            await child_store.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": "run_start",
                        "input_id": "child-input",
                        "execution_session_id": (await child_store.snapshot()).header.session_id,
                    },
                    run_id="child-run",
                )
            )
            await child_store.append_entry(
                EntryDraft("custom", data={"kind": "run_end"}, run_id="child-run")
            )
        context = SimpleNamespace(durable_inputs=DurableInputs(child_store), run_id="child-run")
        await team.member(record["agent_id"]).flush_inbox(context)
        message = team.inbox.records[sent["message_ids"][0]]
        assert message["status"] == "pending" and message["delivery"] is None
        assert not any(
            entry.type == "input_queued" for entry in (await child_store.snapshot()).entries
        )
        assert not (await context.durable_inputs.pending("child-run")).has("steer")
    finally:
        await child_store.aclose()
        await close(team)
