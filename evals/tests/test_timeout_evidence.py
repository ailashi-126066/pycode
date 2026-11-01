"""An actual cancelled model request remains in coding evaluation evidence."""

import asyncio
import gzip
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server

from evals.coding.assembly import build_evaluation_solver
from evals.coding.runner import EvaluationRunner
from evals.coding.workspaces import EvaluationWorkspaces
from evals.tests.corpus import polyglot_cache
from pycode.app import AppConfig


async def test_timeout_preserves_settled_request_usage_trace_and_independent_tests(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "local-test-key")
    root = Path(__file__).parents[2]
    workspaces = EvaluationWorkspaces(
        root / "evals/data/manifest.json",
        polyglot_cache(),
        tmp_path / "work",
        tmp_path / "results",
    )
    task = replace(workspaces.tasks[0], timeout_seconds=1)
    workspaces.tasks = [task]
    response = chat_completion()
    response.started = threading.Event()
    response.release = threading.Event()
    with protocol_server([response]) as (url, requests):
        solver = build_evaluation_solver(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        )
        runner = EvaluationRunner(
            workspaces,
            solver.solve,
            Path(sys.executable),
            model_id="wire-model",
            solver_handles_timeout=True,
        )
        running = asyncio.create_task(runner.run(smoke=False))
        try:
            assert await asyncio.to_thread(response.started.wait, 8)
            results = await asyncio.wait_for(asyncio.shield(running), 8)
        finally:
            response.release.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        assert len(requests) == len(results) == 1
        result = results[0]
        assert result["outcome"] == "agent_timeout"
        assert result["agent"]["outcome"] == "timeout"
        assert result["agent"]["usage"]["session"]["request_count"] == 1
        assert len(result["agent"]["model_requests"]) == 1
        assert result["agent"]["model_requests"][0]["status"] != "pending"
        assert result["agent"]["provider"]["model_id"] == "wire-model"
        assert result["baseline"]["junit"]["tests"] > 0
        assert result["independent_tests"]["junit"]["tests"] > 0
        assert result["artifacts"]["trace"]["complete"]
        trace = tmp_path / "results" / result["artifacts"]["trace"]["path"]
        from pycode.session import read_records

        exported = tmp_path / "exported-session.sqlite"
        with gzip.open(trace, "rb") as stream:
            exported.write_bytes(stream.read())
        metadata, entries, _ = read_records(exported)
        assert metadata["header"]["session_id"] == "evaluation"
        assert any(entry.type == "message" for entry in entries)
        summary = json.loads((tmp_path / "results/summary.json").read_bytes())
        assert summary["outcomes"] == {"agent_timeout": 1}
        assert not list((tmp_path / "work").iterdir())


async def test_unavailable_baseline_preserves_failure_and_never_calls_model(tmp_path, monkeypatch):
    root = Path(__file__).parents[2]
    workspaces = EvaluationWorkspaces(
        root / "evals/data/manifest.json",
        polyglot_cache(),
        tmp_path / "work",
        tmp_path / "results",
    )
    workspaces.tasks = workspaces.tasks[:1]

    async def unavailable(*args):
        return {"exit_code": 3, "junit": None, "timeout": False, "stdout": "pytest startup failed"}

    async def forbidden(*args):
        raise AssertionError("An invalid baseline must not consume model requests")

    monkeypatch.setattr(workspaces, "test", unavailable)
    runner = EvaluationRunner(workspaces, forbidden, Path(sys.executable), model_id="not-called")
    results = await runner.run(smoke=False)
    assert len(results) == 1 and results[0]["outcome"] == "setup_failed"
    assert results[0]["baseline"]["exit_code"] == 3
    assert results[0]["agent"]["error_type"] == "StorageError"
    assert "Baseline tests did not start normally" in results[0]["agent"]["error"]
    saved = json.loads((tmp_path / "results/affine-cipher.json").read_bytes())
    assert saved["outcome"] == "setup_failed" and saved["independent_tests"] is None
