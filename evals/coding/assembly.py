"""Assemble coding evaluations from pycode's public constructors."""

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path

from evals.coding.provenance import installed_dependencies, package_manifest
from evals.coding.runner import EvaluationRunner
from evals.coding.solver import ConfiguredEvaluationSolver
from evals.coding.workspaces import EvaluationWorkspaces
from evals.quality.langfuse import LangfuseEvaluationSummary
from pycode.app import compose_application
from pycode.kernel import StorageError, data_hash
from pycode.sandbox import SandboxSettings


def build_evaluation_runner(config, providers, manifest, cache, work, results, *, langfuse=False):
    evaluation_root = (config.workspace / "evals").resolve()
    runtime_root = (config.workspace / "tests" / ".runtime-tests").resolve()
    for path in (manifest, cache, work, results):
        allowed = (evaluation_root,) if path == manifest else (evaluation_root, runtime_root)
        if not any(Path(path).resolve().is_relative_to(root) for root in allowed):
            raise StorageError(
                "evaluation_path_escape",
                "Manifest must stay under evals; artifacts under evals or tests/.runtime-tests",
            )
    settings = SandboxSettings("on" if sys.platform == "darwin" else "off", False)
    config = replace(config, sandbox=settings)
    workspaces = EvaluationWorkspaces(
        manifest,
        cache,
        work,
        results,
        sandbox=compose_application(config, providers).sandbox_backend(),
        sandbox_settings=settings,
    )
    solver = build_evaluation_solver(config, providers)
    telemetry = None
    if langfuse:
        public = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret = os.environ.get("LANGFUSE_SECRET_KEY")
        if public and secret:
            telemetry = LangfuseEvaluationSummary(
                os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"), public, secret
            )
    return EvaluationRunner(
        workspaces,
        solver.solve,
        Path(sys.executable),
        model_id=providers[0].model_id,
        telemetry=telemetry,
        solver_handles_timeout=True,
    )


def build_evaluation_solver(config, providers):
    def identity():
        versions = installed_dependencies()
        lock = config.workspace / "requirements-dev.lock"
        return {
            "source_manifest": package_manifest(),
            "dependencies": versions,
            "dependency_hash": data_hash(versions),
            "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        }

    def trace_path(selected, session_id):
        return str(compose_application(selected).session_trace_path(session_id))

    async def open_session(selected, roster, session_id, **options):
        return await compose_application(selected, roster).open_session(session_id, **options)

    return ConfiguredEvaluationSolver(config, providers, open_session, identity, trace_path)
