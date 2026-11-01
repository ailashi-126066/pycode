"""Subagent worktrees in a temporary git repository."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from pycode.kernel import ToolError
from pycode.subagents.store import GitWorktrees
from tests.snapshot.test_git_tree import git, repository


def committed_repository(tmp_path):
    project = repository(tmp_path)
    (project / "code.py").write_text("base\n")
    git(project, "add", ".")
    git(project, "commit", "-qm", "base")
    return project


async def test_worktree_is_isolated_and_reports_changes(tmp_path):
    project = committed_repository(tmp_path)
    (project / "code.py").write_text("user uncommitted edit\n")
    worktrees = GitWorktrees(project, tmp_path / "worktrees")
    worktree = await worktrees.create("a1")
    assert worktree.branch == "pycode/agent/a1"
    assert (Path(worktree.path) / "code.py").read_text() == "base\n"
    assert not await worktrees.changed(worktree)
    (Path(worktree.path) / "code.py").write_text("agent edit\n")
    assert await worktrees.changed(worktree)
    assert (project / "code.py").read_text() == "user uncommitted edit\n"


async def test_unchanged_worktree_and_branch_are_removed(tmp_path):
    project = committed_repository(tmp_path)
    worktrees = GitWorktrees(project, tmp_path / "worktrees")
    worktree = await worktrees.create("a1")
    await worktrees.remove(worktree)
    assert not await asyncio.to_thread(Path(worktree.path).exists)
    assert git(project, "branch", "--list", worktree.branch).strip() == b""


async def test_commit_records_a_step_and_counts_as_a_change(tmp_path):
    project = committed_repository(tmp_path)
    worktrees = GitWorktrees(project, tmp_path / "worktrees")
    worktree = await worktrees.create("a1")
    await worktrees.commit(worktree, "nothing staged")
    assert git(Path(worktree.path), "rev-parse", "HEAD").decode().strip() == worktree.base
    (Path(worktree.path) / "new.py").write_text("x\n")
    await worktrees.commit(worktree, "subagent a1: complete step s1")
    log = git(Path(worktree.path), "log", "-1", "--format=%s").decode().strip()
    assert log == "subagent a1: complete step s1"
    assert await worktrees.changed(worktree)
    # The main agent merges the kept branch with an ordinary git merge.
    git(project, "merge", "-q", "--ff-only", worktree.branch)
    assert (project / "new.py").read_text() == "x\n"


async def test_non_git_workspace_is_rejected(tmp_path):
    with pytest.raises(ToolError) as error:
        await GitWorktrees(tmp_path, tmp_path / "worktrees").create("a1")
    assert error.value.code == "worktree_requires_git"


def test_git_timeout_has_a_stable_error_code(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 60)

    monkeypatch.setattr("pycode.subagents.store.subprocess.run", timeout)
    with pytest.raises(ToolError) as error:
        GitWorktrees(tmp_path, tmp_path / "worktrees").git(["status"])
    assert error.value.code == "worktree_git_timeout"
