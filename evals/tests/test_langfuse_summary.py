"""The actual SDK sends datasets, experiments, observations and deterministic scores."""

from uuid import uuid4

from evals.quality.langfuse import LangfuseEvaluationSummary
from evals.tests.langfuse_server import langfuse_server


async def test_langfuse_ingestion_contains_only_aggregate_evaluation_fields():
    result = {
        "task": "affine-cipher",
        "run_id": "fixed-run",
        "outcome": "passed",
        "model": "fixed",
        "source_commit": "fixed-commit",
        "source_sha256": "fixed-source",
        "elapsed_seconds": 1.25,
        "platform": "Windows",
        "agent": {
            "usage": {"session": {"request_count": 3, "tokens": {"total_tokens": 42}}},
            "prompt": "PRIVATE PROMPT",
            "tools": [
                {
                    "name": "read_file",
                    "operation_id": "tool-1",
                    "arguments": "PRIVATE TOOL RESULT",
                    "is_error": False,
                }
            ],
            "model_requests": [
                {
                    "request_id": "req-1",
                    "status": "completed",
                    "purpose": "agent",
                    "usage": {"input_tokens": 20, "output_tokens": 22},
                    "prompt": "PRIVATE PROMPT",
                }
            ],
        },
        "changes": [{"after": "PRIVATE SOURCE CODE"}],
    }
    with langfuse_server() as (url, requests, spans):
        report = await LangfuseEvaluationSummary(
            url, "pk-lf-" + uuid4().hex, "local-placeholder"
        ).publish((result,))
        assert report["experiment"] == "fixed-run"
        assert len(report["traces"]) == 1 and report["traces"][0]["trace_id"]
        assert any(request["path"] == "/api/public/v2/datasets" for request in requests)
        item = next(
            request["body"]
            for request in requests
            if request["path"] == "/api/public/dataset-items"
        )
        assert item["metadata"]["request_count"] == 3
        assert item["metadata"]["tokens"] == {"total_tokens": 42}
        assert all(b"PRIVATE" not in request["raw"] for request in requests)
        assert {"model request", "read_file"} <= {span.name for span in spans}
        scores = []
        for request in requests:
            if request["path"] == "/api/public/ingestion":
                scores.extend(
                    event["body"]
                    for event in request["body"]["batch"]
                    if event["type"] == "score-create"
                )
        assert {score["dataType"] for score in scores} == {"BOOLEAN", "CATEGORICAL", "NUMERIC"}
        assert all(score["traceId"] == report["traces"][0]["trace_id"] for score in scores)
