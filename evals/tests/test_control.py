"""Fixed wire-model control run uses the actual harness and independent corpus tests."""

import asyncio
import json
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import HTTPResponse, chat_completion, protocol_server

from evals.coding.assembly import build_evaluation_solver
from evals.coding.runner import EvaluationRunner
from evals.coding.workspaces import EvaluationWorkspaces
from evals.quality.langfuse import LangfuseEvaluationSummary
from evals.tests.corpus import polyglot_cache
from pycode.app.configuration.settings import AppConfig


@pytest.mark.parametrize("verify,telemetry_fault", [(True, False), (False, False), (True, True)])
async def test_fixed_model_three_task_smoke_is_real_local_protocol_control(
    tmp_path, monkeypatch, verify, telemetry_fault
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    root = Path(__file__).parents[2]
    definition = root / "evals/data/manifest.json"
    cache = polyglot_cache()
    workspaces = EvaluationWorkspaces(definition, cache, tmp_path / "work", tmp_path / "results")
    replies = []
    for task in workspaces.tasks:
        if not task.smoke:
            continue
        canonical = cache / task.source_path / ".meta/example.py"
        solution = await asyncio.to_thread(canonical.read_text, encoding="utf-8")
        replies.append(
            skill_reply(
                "ToolSearch",
                {"query": "read write bash", "limit": 3},
                task.task_id + "-search",
            )
        )
        filename = task.solution_files[0]
        replies.append(skill_reply("read", {"path": filename}, task.task_id + "-read"))
        replies.append(
            skill_reply("write", {"path": filename, "content": solution}, task.task_id + "-write")
        )
        if verify:
            replies.append(
                skill_reply("bash", {"command": "python -m pytest -q"}, task.task_id + "-verify")
            )
        replies.append(chat_completion())
    telemetry_server = (
        protocol_server(
            [
                HTTPResponse(b'{"message":"offline"}', status=503, content_type="application/json")
                for _ in range(10)
            ]
        )
        if telemetry_fault
        else nullcontext(None)
    )
    with protocol_server(replies) as (url, requests), telemetry_server as telemetry_endpoint:
        config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
        solver = build_evaluation_solver(config, (provider(url),))
        runner = EvaluationRunner(
            workspaces,
            solver.solve,
            Path(sys.executable),
            model_id="wire-model deterministic control",
            telemetry=LangfuseEvaluationSummary(
                telemetry_endpoint[0], "pk-lf-control", "local-placeholder"
            )
            if telemetry_fault
            else None,
        )
        results = await runner.run(smoke=True, cleanup=True)
        expected_requests = 15 if verify else 12
        expected_outcome = "passed" if verify else "no_verification"
        assert len(results) == 3 and len(requests) == expected_requests
        for result in results:
            assert result["baseline"]["exit_code"] != 0
            assert result["outcome"] == expected_outcome, result["independent_tests"]
            assert result["independent_tests"]["exit_code"] == 0
            assert result["agent"]["usage"]["session"]["request_count"] == (5 if verify else 4)
            assert result["agent"]["verified"] is verify
            assert result["source_sha256"]
            assert result["independent_tests"]["junit"]["tests"] > 0

        def verify_artifacts():
            directory = tmp_path / "results"
            for result in results:
                assert (directory / (result["task"] + ".json")).exists()
                assert (directory / result["artifacts"]["patch"]).is_file()
                assert result["artifacts"]["trace"]["complete"]
            assert (directory / "results.jsonl").is_file()
            summary = json.loads((directory / "summary.json").read_bytes())
            assert summary["outcomes"] == {expected_outcome: 3}
            assert summary["telemetry"]["status"] == ("failed" if telemetry_fault else "disabled")
            assert not list((tmp_path / "work").iterdir())
            # Keep a truthful control report separate from model-quality scores.
            destination = tmp_path / "evaluation-control.json"
            destination.write_text(
                json.dumps(
                    {
                        "kind": "deterministic_protocol_control",
                        "benchmark_quality": "unverified",
                        "source_commit": workspaces.definition["commit"],
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        await asyncio.to_thread(verify_artifacts)
