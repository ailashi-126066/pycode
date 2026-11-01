"""Coding evaluation assembly confines manifests and artifacts to owned roots."""

import json

import pytest

from evals.coding.assembly import build_evaluation_runner
from pycode.app import AppConfig
from pycode.kernel import StorageError
from pycode.models import ProviderConfig


def test_evaluation_rejects_work_directories_outside_its_owned_root(tmp_path):
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    root = tmp_path / "evals"
    with pytest.raises(StorageError, match="under evals"):
        build_evaluation_runner(
            config,
            (),
            root / "manifest.json",
            root / "cache",
            tmp_path / "unrelated",
            root / "results",
        )


def test_evaluation_accepts_test_owned_runtime_artifacts_but_not_an_outside_manifest(tmp_path):
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    definition = tmp_path / "evals/data/manifest.json"
    definition.parent.mkdir(parents=True)
    definition.write_text(json.dumps({"tasks": [], "commit": "fixed"}))
    runtime = tmp_path / "tests/.runtime-tests"
    providers = (
        ProviderConfig(
            "test", "openai", "test-model", "https://example.com", "TEST_KEY", 32000, 4096
        ),
    )
    runner = build_evaluation_runner(
        config, providers, definition, runtime / "cache", runtime / "work", runtime / "results"
    )
    assert runner.workspaces.directory == runtime / "work"
    assert runner.workspaces.results == runtime / "results"
    assert runner.solver_handles_timeout
    with pytest.raises(StorageError, match="Manifest"):
        build_evaluation_runner(
            config,
            providers,
            runtime / "manifest.json",
            runtime / "cache",
            runtime / "work",
            runtime / "results",
        )


def test_evaluation_rejects_runtime_artifact_symlink_escape(tmp_path):
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    root = tmp_path / "evals"
    runtime = tmp_path / "tests/.runtime-tests"
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime / "work").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError, match="tests/.runtime-tests"):
        build_evaluation_runner(
            config,
            (),
            root / "manifest.json",
            runtime / "cache",
            runtime / "work",
            runtime / "results",
        )
