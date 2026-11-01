"""Check every pinned coding baseline under the actual OS test sandbox, without a model."""

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path

from pycode.app import AppConfig, compose_application
from pycode.sandbox import SandboxSettings


async def verify(args, root):
    from evals.coding.workspaces import EvaluationWorkspaces

    spaces = EvaluationWorkspaces(
        root / "evals/data/manifest.json",
        args.cache,
        args.work,
        args.output.parent / "logs",
        sandbox=compose_application(AppConfig(root, root / ".pycode")).sandbox_backend(),
        sandbox_settings=SandboxSettings("on" if sys.platform == "darwin" else "off"),
    )
    report = {
        "model_requests": 0,
        "platform": platform.platform(),
        "tasks": [],
        "status": "running",
    }
    for task in spaces.tasks:
        workspace = await spaces.prepare(task)
        try:
            result = await spaces.test(task, workspace, Path(sys.executable))
            checked = (
                result["exit_code"] in (0, 1) and result["junit"] and result["junit"]["tests"] > 0
            )
            report["tasks"].append(
                {"task": task.task_id, "ready": bool(checked), "baseline": result}
            )
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(task.task_id + ": " + ("ready" if checked else "unavailable"), flush=True)
        finally:
            await spaces.cleanup(workspace)
    report["status"] = "passed" if all(item["ready"] for item in report["tasks"]) else "failed"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=root / "evals/cache/polyglot")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runtime = (root / "tests/.runtime-tests").resolve()
    cache_roots = ((root / "evals/cache").resolve(), runtime)
    if not any(args.cache.resolve().is_relative_to(path) for path in cache_roots):
        raise ValueError("The corpus cache must stay under evals/cache or tests/.runtime-tests")
    for path in (args.work, args.output):
        if not path.resolve().is_relative_to(runtime):
            raise ValueError("Baseline verification artifacts must stay under tests/.runtime-tests")
    if args.output.exists():
        raise ValueError("Use a fresh report path; retain previous evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    return asyncio.run(verify(args, root))


if __name__ == "__main__":
    raise SystemExit(main())
