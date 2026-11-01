"""Verify two complete fixed coding runs and report every result and measured variation."""

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

from pycode.kernel import data_hash, json_dumps


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_run(folder, tasks):
    results = [json.loads(line) for line in (folder / "results.jsonl").read_text().splitlines()]
    if [item["task"] for item in results] != tasks:
        raise ValueError("Run does not contain every manifest task exactly once, in order")
    for result in results:
        name = result["task"]
        if json.loads((folder / (name + ".json")).read_bytes()) != result:
            raise ValueError("Per-task JSON differs from the append-only result: " + name)
        trace = result["artifacts"]["trace"]
        if not trace or not trace["complete"]:
            raise ValueError("Missing or incomplete session evidence: " + name)
        with gzip.open(folder / trace["path"], "rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) != trace["raw_bytes"] or hashlib.sha256(raw).hexdigest() != trace["raw_sha256"]:
            raise ValueError("Session evidence differs from its recorded hash: " + name)
        entries = [json.loads(line) for line in raw.splitlines()]
        workspace = entries[0]["workspace"]
        snapshot = next(entry["data"] for entry in entries if entry.get("type") == "system_prompt")
        if (
            hashlib.sha256(snapshot["text"].encode()).hexdigest()
            != result["agent"]["system_prompt_hash"]
        ):
            raise ValueError("System prompt differs from its recorded hash: " + name)
        normalized = snapshot["text"].replace(workspace, "<isolated-workspace>")
        result["normalized_system_prompt_hash"] = hashlib.sha256(normalized.encode()).hexdigest()
        user = next(
            entry["message"]["text"]
            for entry in entries
            if entry.get("type") == "message" and entry["message"]["role"] == "user"
        )
        result["task_prompt_hash"] = hashlib.sha256(user.encode()).hexdigest()
        files = [
            name + suffix
            for suffix in (
                ".json",
                ".patch",
                ".baseline.log",
                ".baseline.xml",
                ".independent.log",
                ".independent.xml",
            )
        ] + [trace["path"], result["artifacts"]["source_manifest"]]
        result["verified_artifact_hashes"] = {file: digest(folder / file) for file in files}
        provenance = json.loads((folder / result["artifacts"]["source_manifest"]).read_bytes())
        runtime = provenance["source_manifest"]
        if (
            runtime["sha256"] != result["source_sha256"]
            or hashlib.sha256(
                json_dumps(runtime["files"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            != runtime["sha256"]
            or data_hash(provenance["dependencies"]) != result["agent"]["dependency_hash"]
        ):
            raise ValueError(
                "Recorded runtime/dependency identity differs from its manifest: " + name
            )
        requests = result["agent"]["model_requests"]
        if len(requests) != result["agent"]["usage"]["session"]["request_count"]:
            raise ValueError("Request records disagree with usage: " + name)
    summary = json.loads((folder / "summary.json").read_bytes())
    if summary["tasks"] != len(results) or summary["outcomes"] != dict(
        Counter(r["outcome"] for r in results)
    ):
        raise ValueError("Summary differs from the per-task results")
    return results


def metrics(result):
    usage = result["agent"]["usage"]["session"]
    return {
        "outcome": result["outcome"],
        "elapsed_seconds": result["elapsed_seconds"],
        "requests": usage["request_count"],
        "tokens": usage["tokens"]["total_tokens"],
        "independent_junit": result["independent_tests"]["junit"],
        "verified_artifact_hashes": result["verified_artifact_hashes"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.first.resolve() == args.second.resolve():
        raise ValueError("Compare two distinct runs; one run is not two observations")
    root = Path(__file__).resolve().parents[2]
    for path in (args.first, args.second, args.output):
        if not path.resolve().is_relative_to((root / "tests/.runtime-tests").resolve()):
            raise ValueError("Evaluation comparison must remain under tests/.runtime-tests")
    if args.output.exists():
        raise ValueError("Use a fresh comparison output; retain previous reports")
    manifest = root / "evals/data/manifest.json"
    tasks = [item["id"] for item in json.loads(manifest.read_bytes())["tasks"]]
    first, second = load_run(args.first, tasks), load_run(args.second, tasks)
    identities = {
        (
            r["source_sha256"],
            r["agent"]["configuration_hash"],
            r["agent"]["dependency_hash"],
            r["agent"]["lock_sha256"],
            r["normalized_system_prompt_hash"],
            r["manifest_sha256"],
            r["agent_timeout_seconds"],
        )
        for r in first + second
    }
    if len(identities) != 1 or first[0]["manifest_sha256"] != digest(manifest):
        raise ValueError("Runs differ in runtime, model, prompts, dependencies, manifest or budget")
    rows = []
    for left, right in zip(first, second, strict=True):
        if left["task_prompt_hash"] != right["task_prompt_hash"]:
            raise ValueError("Task prompts differ between rounds: " + left["task"])
        a, b = metrics(left), metrics(right)
        rows.append(
            {
                "task": left["task"],
                "first": a,
                "second": b,
                "variation": {
                    "seconds": b["elapsed_seconds"] - a["elapsed_seconds"],
                    "requests": b["requests"] - a["requests"],
                    "tokens": b["tokens"] - a["tokens"]
                    if a["tokens"] is not None and b["tokens"] is not None
                    else None,
                    "outcome_changed": a["outcome"] != b["outcome"],
                },
            }
        )
    report = {
        "status": "complete",
        "rounds": 2,
        "tasks_per_round": len(tasks),
        "limitations": (
            "Two observations per task; this does not establish a statistical reliability bound."
        ),
        "provider": first[0]["agent"]["provider"],
        "runtime_source_sha256": first[0]["source_sha256"],
        "identity": dict(
            zip(
                (
                    "runtime_source_sha256",
                    "configuration_sha256",
                    "dependency_sha256",
                    "lock_sha256",
                    "normalized_system_prompt_sha256",
                    "manifest_sha256",
                    "agent_timeout_seconds",
                ),
                next(iter(identities)),
                strict=True,
            )
        ),
        "first": {
            "directory": str(args.first),
            "outcomes": dict(Counter(r["outcome"] for r in first)),
        },
        "second": {
            "directory": str(args.second),
            "outcomes": dict(Counter(r["outcome"] for r in second)),
        },
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("status", "rounds", "tasks_per_round", "first", "second")
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
