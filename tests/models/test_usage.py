from dataclasses import replace

from pycode.kernel import EntryDraft, Message, UsageRecord
from pycode.models.requests import SQLiteRequestStore
from pycode.models.types import RequestRecord
from pycode.models.usage import UsageService
from tests.models.test_control import store_for


async def test_usage_keeps_summary_retry_worker_and_old_branch_billing_separate(tmp_path):
    history = store_for(tmp_path)
    records = SQLiteRequestStore(tmp_path / "runtime.sqlite")
    try:
        user = await history.append_message(Message("u1", "user", "task"))
        await history.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "run_start",
                    "input_id": "u1",
                    "execution_session_id": "s1",
                },
                run_id="r1",
            )
        )
        await history.append_entry(
            EntryDraft("custom", data={"kind": "summary_job", "job_id": "summary1"})
        )
        inputs = (
            ("agent1", "r1", "agent", "lead", 10, 2, "agent"),
            ("sum1", "summary1", "summary", "lead", 3, 1, "summary"),
            ("sum2", "summary1", "retry", "lead", 4, 1, "summary"),
            ("worker1", "r1", "subagent", "worker", 14, 5, "subagent"),
        )
        for identifier, run_id, purpose, agent, input_tokens, output_tokens, origin in inputs:
            record = RequestRecord(
                identifier, "s1", run_id, "p", "m", purpose, 0, "started", 1, agent_id=agent
            )
            await records.start(record)
            usage = UsageRecord(
                "p",
                "m",
                identifier,
                session_id="s1",
                run_id=run_id,
                agent_id=agent,
                purpose=purpose,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                raw={"response_metadata": {"pycode_origin_purpose": origin}},
            )
            await records.settle(replace(record, status="completed", usage=usage))
        service = UsageService(history, records)
        report = await service.report("r1")
        assert report["main_turn"]["tokens"]["total_tokens"] == 12
        assert report["turn"]["tokens"]["total_tokens"] == 40
        assert report["by_origin_purpose"]["summary"]["tokens"]["total_tokens"] == 9
        assert report["by_purpose"]["retry"]["tokens"]["total_tokens"] == 5
        assert report["by_agent"]["worker"]["tokens"]["total_tokens"] == 19
        await history.branch(user)
        assert (await service.report())["session"] == report["session"]
        assert (await service.report("r1"))["turn"] == report["turn"]
    finally:
        await history.aclose()
        await records.aclose()
