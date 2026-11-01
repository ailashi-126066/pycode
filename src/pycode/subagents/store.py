# Pi 没有，pycode 新增
"""Temporary git worktrees for worktree-mode subagents (Claude Code's worktree isolation).

A worktree without changes is removed with its branch when the subagent ends; one with
changes is kept, and the main agent merges its branch with an approved bash command.
"""

import asyncio
import os
import subprocess
from pathlib import Path

from pycode.kernel import ToolError
from pycode.subagents.types import Worktree


class GitWorktrees:
    def __init__(self, workspace: Path, directory: Path):
        self.workspace = workspace
        self.directory = directory

    def git(self, arguments, *, cwd=None, allowed=(0,)) -> tuple[str, int]:
        # Repository hooks and the caller's GIT_* overrides never run inside pycode's git calls.
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=" + str(self.directory / "disabled-hooks"),
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(cwd or self.workspace),
                    *arguments,
                ],
                capture_output=True,
                env=environment,
                timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            raise ToolError(
                "worktree_git_timeout", "Git operation exceeded its time limit"
            ) from error
        if result.returncode not in allowed:
            raise ToolError("worktree_git_error", result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8", "replace").strip(), result.returncode

    async def create(self, agent_id: str) -> Worktree:
        return await asyncio.to_thread(self._create, agent_id)

    def _create(self, agent_id):
        _, status = self.git(["rev-parse", "--show-toplevel"], allowed=(0, 128))
        if status != 0:
            raise ToolError("worktree_requires_git", "Worktree mode requires a Git repository")
        base, _ = self.git(["rev-parse", "--verify", "HEAD^{commit}"])
        path = self.directory / agent_id
        branch = "pycode/agent/" + agent_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.git(["worktree", "add", "-b", branch, str(path), base])
        return Worktree(str(path), branch, base)

    async def changed(self, worktree: Worktree) -> bool:
        return await asyncio.to_thread(self._changed, worktree)

    def _changed(self, worktree):
        status, _ = self.git(["status", "--porcelain"], cwd=worktree.path)
        head, _ = self.git(["rev-parse", "HEAD"], cwd=worktree.path)
        return bool(status) or head != worktree.base

    async def remove(self, worktree: Worktree) -> None:
        await asyncio.to_thread(self._remove, worktree)

    def _remove(self, worktree):
        self.git(["worktree", "remove", "--force", worktree.path])
        self.git(["branch", "-D", worktree.branch])

    async def commit(self, worktree: Worktree, message: str) -> None:
        await asyncio.to_thread(self._commit, worktree, message)

    def _commit(self, worktree, message):
        self.git(["add", "-A"], cwd=worktree.path)
        _, staged = self.git(["diff", "--cached", "--quiet"], cwd=worktree.path, allowed=(0, 1))
        if not staged:
            return
        options = ["-c", "commit.gpgsign=false"]
        for key, fallback in (("user.name", "pycode agent"), ("user.email", "agent@pycode.local")):
            _, missing = self.git(["config", "--get", key], cwd=worktree.path, allowed=(0, 1))
            if missing:
                options += ["-c", f"{key}={fallback}"]
        self.git([*options, "commit", "-m", message], cwd=worktree.path)
