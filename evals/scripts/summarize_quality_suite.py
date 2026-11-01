"""Bind every required quality case to completed evidence; retain superseded failures."""

import argparse
import hashlib
import json
from pathlib import Path

try:
    from evals.scripts.source_manifest import manifest
except ModuleNotFoundError:  # run as a file path, where evals/scripts is sys.path[0]
    from source_manifest import manifest


def load(path):
    return json.loads(path.read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_files(report):
    source = report.get("runtime_manifest", report.get("source_manifest"))
    return [file for file in source["files"] if file["path"].startswith("src/")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--memory-recovery", type=Path, required=True)
    parser.add_argument("--trajectories", type=Path, required=True)
    parser.add_argument("--selector-recovery", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    for path in vars(args).values():
        if not path.resolve().is_relative_to((root / "tests/.runtime-tests").resolve()):
            raise ValueError("Quality evidence must remain under tests/.runtime-tests")
    if args.output.exists():
        raise ValueError("Use a fresh aggregate report")
    dataset = root / "evals/data/quality-cases.json"
    definition = load(dataset)
    reports = {
        key: load(getattr(args, key))
        for key in ("outputs", "memory_recovery", "trajectories", "selector_recovery", "rules")
    }
    current = [file for file in manifest(root)["files"] if file["path"].startswith("src/")]
    if any(runtime_files(report) != current for report in reports.values()):
        raise ValueError("Runtime code changed relative to quality evidence")
    for report in reports.values():
        if (
            "cases" in report
            and report is not reports["rules"]
            and report["dataset_sha256"] != digest(dataset)
        ):
            raise ValueError("Grading dataset differs from the frozen evidence")
    outputs = {case["id"]: case for case in reports["outputs"]["cases"]}
    recovered = reports["memory_recovery"]["cases"]
    if len(recovered) != 1 or recovered[0]["id"] != "memory-preference":
        raise ValueError("Memory recovery must replace only its unfinished case")
    if (
        recovered[0]["produced"]["session_id"]
        != outputs["memory-preference"]["produced"]["session_id"]
    ):
        raise ValueError("Memory recovery belongs to another source")
    outputs["memory-preference"] = recovered[0]
    trajectories = {case["id"]: case for case in reports["trajectories"]["cases"]}
    recovered = reports["selector_recovery"]["cases"]
    if len(recovered) != 1 or recovered[0]["id"] != "tool-selection":
        raise ValueError("Selector recovery must replace only its incomplete judge case")
    if Path(recovered[0]["reused_trajectory_from"]).resolve() != args.trajectories.resolve():
        raise ValueError("Selector recovery belongs to another observed trajectory")
    trajectories["tool-selection"] = recovered[0]
    if set(outputs) != {case["id"] for case in definition["output_cases"]} or set(trajectories) != {
        case["id"] for case in definition["trajectory_cases"]
    }:
        raise ValueError("Quality suite is missing required cases")
    required = list(outputs.values()) + list(trajectories.values())
    if (
        not all(case["status"] == "passed" for case in required)
        or reports["rules"]["status"] != "passed"
    ):
        raise ValueError("Some required quality case or judge calibration remains incomplete")
    evidence = {
        key: {"path": str(getattr(args, key)), "sha256": digest(getattr(args, key))}
        for key in reports
    }
    payload = {
        "status": "passed",
        "dataset_sha256": digest(dataset),
        "grading_policy": definition["grading_policy"],
        "current_runtime_files": current,
        "optional_metadata_declaration": manifest(root)["sha256"],
        "evidence": evidence,
        "output_cases": outputs,
        "trajectory_cases": trajectories,
        "rule_comparison": reports["rules"],
        "limitations": [
            "Fixed small dataset; not a statistical reliability bound.",
            "Trajectories use controlled loopback failures; not real remote service acceptance.",
            "Scores never replace deterministic tests or version-bound rule adoption permissions.",
            "Failures remain on disk; prescribed recovery replaces only unfinished case results.",
        ],
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "outputs": len(outputs),
                "trajectories": len(trajectories),
                "rule_cases": len(reports["rules"]["cases"]),
                "report": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
