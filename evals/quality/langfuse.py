"""Optional SDK experiments upload only whitelisted, measured summaries."""

import hashlib

from pycode.kernel import StorageError, json_dumps, owned_io


class LangfuseEvaluationSummary:
    def __init__(self, host, public_key, secret_key):
        self.host = host.rstrip("/")
        self.public_key = public_key
        self.secret_key = secret_key

    async def publish(self, results):
        return await owned_io(self.publish_sync, results)

    def summary(self, result):
        fields = (
            "task",
            "run_id",
            "outcome",
            "model",
            "source_commit",
            "source_sha256",
            "source_path",
            "manifest_sha256",
            "elapsed_seconds",
        )
        summary = {name: result[name] for name in fields if name in result}
        agent = result["agent"]
        usage = agent.get("usage", {}).get("session", {})
        summary["request_count"] = usage.get("request_count")
        summary["tokens"] = usage.get("tokens")
        for name in ("configuration_hash", "system_prompt_hash", "dependency_hash"):
            if name in agent:
                summary[name] = agent[name]
        return summary

    def publish_sync(self, results):
        try:
            from langfuse import Evaluation, Langfuse
        except ImportError as error:
            raise StorageError(
                "evaluation_extra_required", "Install pycode[eval] to enable Langfuse"
            ) from error
        if not results:
            return {"items": 0}
        client = Langfuse(
            host=self.host,
            public_key=self.public_key,
            secret_key=self.secret_key,
            timeout=3,
            flush_at=1,
            flush_interval=0.1,
        )
        dataset_name = "pycode-python-" + results[0]["source_commit"][:12]
        run_name = (
            results[0].get("run_id")
            or "evaluation-"
            + hashlib.sha256(
                json_dumps([self.summary(result) for result in results], sort_keys=True).encode()
            ).hexdigest()[:16]
        )
        selected = {result["task"]: result for result in results}
        try:
            client.create_dataset(
                name=dataset_name,
                metadata={
                    "source_commit": results[0]["source_commit"],
                    "kind": "deterministic coding evaluation",
                },
            )
            items = []
            for result in results:
                item_id = hashlib.sha256(
                    (result["source_commit"] + ":" + result["task"]).encode()
                ).hexdigest()
                items.append(
                    client.create_dataset_item(
                        dataset_name=dataset_name,
                        id=item_id,
                        input={"task": result["task"], "source_commit": result["source_commit"]},
                        expected_output={"outcome": "passed"},
                        metadata=self.summary(result),
                    )
                )

            def task(*, item, **kwargs):
                result = selected[item.input["task"]]
                for request in result["agent"].get("model_requests", ()):
                    usage = request.get("usage") or {}
                    token_details = {}
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "cache_read_tokens",
                        "cache_miss_tokens",
                        "reasoning_tokens",
                    ):
                        if isinstance(usage.get(key), int):
                            token_details[key] = usage[key]
                    metadata = {
                        key: request[key]
                        for key in (
                            "request_id",
                            "status",
                            "purpose",
                            "attempt",
                            "started_at",
                            "settled_at",
                            "error_code",
                        )
                        if key in request
                    }
                    with client.start_as_current_observation(
                        name="model request",
                        as_type="generation",
                        model=result["model"],
                        metadata=metadata,
                        usage_details=token_details,
                    ):
                        pass
                for invocation in result["agent"].get("tools", ()):
                    metadata = {
                        key: invocation[key]
                        for key in ("operation_id", "is_error", "error_code", "exit_code")
                        if key in invocation
                    }
                    with client.start_as_current_observation(
                        name=invocation["name"], as_type="tool", metadata=metadata
                    ):
                        pass
                return self.summary(result)

            def scores(*, output, **kwargs):
                scored = [
                    Evaluation(
                        name="task_passed", value=output["outcome"] == "passed", data_type="BOOLEAN"
                    ),
                    Evaluation(name="outcome", value=output["outcome"], data_type="CATEGORICAL"),
                ]
                if isinstance(output.get("elapsed_seconds"), (int, float)):
                    scored.append(
                        Evaluation(
                            name="elapsed_seconds",
                            value=output["elapsed_seconds"],
                            data_type="NUMERIC",
                        )
                    )
                return scored

            experiment = client.run_experiment(
                name=dataset_name,
                run_name=run_name,
                data=items,
                task=task,
                evaluators=[scores],
                max_concurrency=1,
                metadata={"run_id": run_name},
            )
            traces = []
            for item in experiment.item_results:
                traces.append(
                    {
                        "task": item.item.input["task"],
                        "trace_id": item.trace_id,
                        "dataset_run_id": item.dataset_run_id,
                    }
                )
            return {"experiment": run_name, "dataset": dataset_name, "traces": traces}
        finally:
            try:
                client.flush()
            finally:
                client.shutdown()
