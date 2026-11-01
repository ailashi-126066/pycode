"""Serial coding evaluations preserve local evidence independently of telemetry."""

import asyncio
import logging
import platform
import time
from datetime import datetime, timedelta, timezone

from evals.coding.tasks import evaluation_outcome
from pycode.kernel import StorageError


class EvaluationRunner:
    def __init__(
        self, workspaces, solver, python, *, model_id, telemetry=None, solver_handles_timeout=False
    ):
        self.workspaces = workspaces
        self.solver = solver
        self.python = python
        self.model_id = model_id
        self.telemetry = telemetry
        self.solver_handles_timeout = solver_handles_timeout

    async def run(self, *, smoke=True, cleanup=True):
        results = []
        for task in self.workspaces.tasks:
            if smoke and not task.smoke:
                continue
            started = datetime.now(timezone(timedelta(hours=8))).isoformat()
            clock = time.monotonic()
            workspace = None
            baseline = None
            tested = None
            solved = {"outcome": "setup_failed"}
            changes = []
            forbidden = []
            try:
                workspace = await self.workspaces.prepare(task)
                baseline = await self.workspaces.test(task, workspace, self.python)
                if (
                    baseline["exit_code"] not in (0, 1)
                    or baseline.get("junit") is None
                    or not baseline["junit"]["tests"]
                ):
                    raise StorageError(
                        "eval_baseline_unavailable",
                        "Baseline tests did not start normally; skip model execution",
                    )
                try:
                    if self.solver_handles_timeout:
                        solved = await self.solver(task, workspace)
                    else:
                        async with asyncio.timeout(task.timeout_seconds * 3):
                            solved = await self.solver(task, workspace)
                except TimeoutError:
                    solved = {"outcome": "timeout"}
                except Exception as error:
                    solved = {
                        "outcome": "agent_error",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                unchanged = await self.workspaces.unchanged_tests(task, workspace)
                tested = await self.workspaces.test(task, workspace, self.python)
                unchanged = unchanged and await self.workspaces.unchanged_tests(task, workspace)
                changes = await self.workspaces.changes(task, workspace)
                if not unchanged:
                    forbidden = await self.workspaces.forbidden_changes(task, workspace)
                outcome = evaluation_outcome(
                    solved["outcome"],
                    tested["exit_code"],
                    tampered=not unchanged,
                    timeout=tested["timeout"] or solved["outcome"] == "timeout",
                    changed=bool(changes),
                    verified=solved.get("verified", False),
                    final_text=solved.get("final_text", ""),
                    failure_codes=solved.get("failure_codes", ()),
                )
            except Exception as error:
                outcome = "setup_failed"
                solved = {**solved, "error_type": type(error).__name__, "error": str(error)}
            result = {
                "schema_version": 2,
                "run_id": self.workspaces.run_id,
                "task": task.task_id,
                "source_path": task.source_path,
                "outcome": outcome,
                "model": self.model_id,
                "platform": platform.platform(),
                "started_at": started,
                "finished_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "timezone": "Asia/Shanghai",
                "elapsed_seconds": time.monotonic() - clock,
                "source_commit": self.workspaces.definition["commit"],
                "manifest_sha256": self.workspaces.manifest_sha256,
                "agent_timeout_seconds": task.timeout_seconds * 3,
                "baseline": baseline,
                "independent_tests": tested,
                "agent": solved,
                "changes": changes,
                "forbidden_changes": forbidden,
            }
            try:
                await self.workspaces.save_result(workspace, result)
                results.append(result)
            finally:
                if cleanup and workspace is not None:
                    await self.workspaces.cleanup(workspace)
        telemetry = {"status": "disabled"}
        if self.telemetry is not None:
            try:
                published = await self.telemetry.publish(results)
                telemetry = {"status": "submitted", "report": published}
            except Exception as error:
                telemetry = {"status": "failed", "error_type": type(error).__name__}
                logging.getLogger("pycode.evaluation").warning(
                    "Optional telemetry failed: %s; local results are unchanged",
                    type(error).__name__,
                )
        await self.workspaces.summarize(results, telemetry)
        return tuple(results)
