"""A recall batch belongs to a task, not a model turn or an in-memory session object."""

from pycode.app.bootstrap import compose_application
from pycode.context import ContextProjector, apply_task_change, empty_tasks
from pycode.kernel import Message
from pycode.memory.services.projection import MemoryProjection
from tests.agent.adapters.test_task_context import new_task, validate
from tests.memory.services.test_memory import FakeUpdater, service_at, text


async def test_recall_reused_switched_released_and_restored(tmp_path):
    updater = FakeUpdater(chosen=[("user", "login")])
    config, history, memory = await service_at(tmp_path, updater)
    await memory.store.aput(memory.user_namespace, "login", text("Keep the login API unchanged"))
    await memory.store.aput(memory.user_namespace, "deploy", text("Deploy via staging first"))
    state = empty_tasks()

    async def change(action, identifier):
        nonlocal state
        normalized = validate(state, action, {"u2"})
        state = apply_task_change(state, normalized)
        await history.append_message(
            Message(
                identifier,
                "assistant",
                "working",
                provider_metadata={
                    "pycode_task_action": action,
                    "pycode_task_change": normalized,
                },
            )
        )
        if normalized["task"]["status"] in {"completed", "cancelled"}:
            before_release = ContextProjector((MemoryProjection(),)).transform_context(
                await history.snapshot()
            )
            assert "memories" not in before_release.checkpoint
        await memory.prepare_task(settled=True)
        return normalized["task_id"]

    try:
        await history.append_message(Message("u2", "user", "修复登录超时"))
        await memory.prepare_task()
        assert len(updater.selections) == 1
        first = await change(new_task(), "task-first")
        assert len(updater.selections) == 1  # Bind the initial batch without another model call.
        await history.append_message(Message("u3", "user", "继续"))
        await memory.prepare_task()
        assert len(updater.selections) == 1
        await memory.aclose()
        await history.aclose()
        history = await compose_application(config).session_repository().open("s")
        memory = compose_application(config).memory_service(history)
        memory.updater = updater
        await memory.initialize()
        await memory.prepare_task()
        assert len(updater.selections) == 1
        updater.chosen = [("user", "deploy")]
        await change(new_task("整理部署脚本"), "task-second")
        assert len(updater.selections) == 2
        assert "整理部署脚本" in updater.selections[-1][0]
        assert "Current step: 定位并验证" in updater.selections[-1][0]
        updater.chosen = [("user", "login")]
        await change({"relation": "switch", "task_id": first, "update": None}, "task-return")
        assert len(updater.selections) == 3
        await change(
            {
                "relation": "continue",
                "task_id": first,
                "update": {
                    "status": "completed",
                    "current_step": None,
                    "steps": [
                        {
                            "id": "s1",
                            "title": "定位并验证",
                            "status": "completed",
                            "conclusion": "已确认结果",
                            "evidence_refs": ["u2"],
                        }
                    ],
                },
            },
            "task-done",
        )
        assert len(updater.selections) == 3
        snapshot = await history.snapshot()
        loads = [entry for entry in snapshot.entries if entry.type == "memory_load"]
        assert not loads[-1].data["records"]
        assert loads[-1].data["task_id"] == first
        projection = ContextProjector((MemoryProjection(),)).transform_context(snapshot)
        assert "memories" not in projection.checkpoint
        assert len((await memory.list())["records"]) == 2
    finally:
        await memory.aclose()
        await history.aclose()


async def test_provisional_memory_survives_truncation_and_leaves_after_simple_reply(tmp_path):
    updater = FakeUpdater(chosen=[("user", "preference")])
    _, history, memory = await service_at(tmp_path, updater)
    try:
        await memory.store.aput(memory.user_namespace, "preference", text("Use a small example"))
        await history.append_message(Message("question", "user", "解释一下连接池"))
        await memory.prepare_task()
        await history.append_message(
            Message("partial", "assistant", "连接池", stop_reason="length")
        )
        await memory.prepare_task(settled=True)
        projection = ContextProjector((MemoryProjection(),)).transform_context(
            await history.snapshot()
        )
        assert len(projection.checkpoint["memories"]) == 1
        await history.append_message(Message("final", "assistant", "解释完毕", stop_reason="stop"))
        await memory.prepare_task(settled=True)
        projection = ContextProjector((MemoryProjection(),)).transform_context(
            await history.snapshot()
        )
        assert "memories" not in projection.checkpoint
        assert len(updater.selections) == 1
        assert len((await memory.list())["records"]) == 1
    finally:
        await memory.aclose()
        await history.aclose()
