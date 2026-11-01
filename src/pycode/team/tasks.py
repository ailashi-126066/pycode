"""Task ownership, dependencies and explicit submission/acceptance."""

import asyncio
from pathlib import PurePosixPath

from pycode.kernel import ToolError, json_dumps
from pycode.team.types import bounded_text, claim_error, validate_dependencies


def checked_scope(values):
    for value in values:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or any(c in value for c in "*?[]")
        ):
            raise ToolError(
                "invalid_task_scope", "Scope uses workspace-relative files or directories"
            )
    return list(values)


TRANSITIONS = {
    "open": {"open", "cancelled"},
    "in_progress": {"submitted", "failed", "cancelled"},
    "submitted": {"done", "open", "cancelled"},
    "failed": {"open", "cancelled"},
    "done": set(),
    "cancelled": set(),
}


class TeamManager:
    def __init__(self, team):
        self.team = team
        self.lock = asyncio.Lock()

    @property
    def records(self):
        return self.team.state["tasks"]

    async def tasks(self):
        return {
            key: {**value, "blocked_by": self.blocked_by(value)}
            for key, value in self.records.items()
        }

    def blocked_by(self, task):
        blocked = []
        for identifier in task["dependencies"]:
            dependency = self.records[identifier]
            owner = self.team.records.get(dependency.get("done_by"), {})
            if dependency["status"] != "done" or (
                owner.get("mode") == "worker" and owner["status"] != "merged"
            ):
                blocked.append(identifier)
        return blocked

    async def create_task(self, title, description="", depends_on=(), acceptance=(), scope=()):
        async with self.lock:
            if len(self.records) >= 200:
                raise ToolError("task_limit_reached", "Task board is full")
            identifier = "t" + str(max((int(key[1:]) for key in self.records), default=0) + 1)
            validate_dependencies(identifier, depends_on, self.records)
            task = {
                "task_id": identifier,
                "title": bounded_text(title, "title", 200),
                "description": bounded_text(description, "description", 4000, empty=True),
                "dependencies": list(depends_on),
                "acceptance": [bounded_text(item, "criterion", 500) for item in acceptance],
                "scope": checked_scope([bounded_text(item, "scope", 500) for item in scope]),
                "status": "open",
                "assignee": None,
                "generation": 0,
                "result": "",
                "artifact_refs": [],
                "checks": [],
                "source_fingerprint": None,
                "evidence": {},
                "done_by": None,
                "last_assignee": None,
                "interrupted": False,
            }
            await self.team.commit(tasks=[task])
            return task

    def claimed(self, task_id, agent_id):
        error = claim_error(task_id, agent_id, self.records)
        if error:
            raise ToolError(error, "Cannot claim task: " + error)
        task = self.records[task_id]
        if self.blocked_by(task):
            raise ToolError(
                "dependency_not_integrated", "Dependencies must be accepted and worker code merged"
            )
        generation = (
            self.team.records[agent_id]["generation"] if agent_id in self.team.records else 1
        )
        return {**task, "status": "in_progress", "assignee": agent_id, "generation": generation}

    async def claim_task(self, task_id, agent_id):
        async with self.lock:
            task = self.claimed(task_id, agent_id)
            await self.team.commit(tasks=[task])
            return task

    async def add_dependencies(self, task_id, dependencies):
        return await self.update_task(task_id, actor="lead", depends_on=dependencies)

    async def update_task(self, task_id, *, actor, generation=None, **updates):
        async with self.lock:
            if task_id not in self.records:
                raise ToolError("unknown_task", "Task does not exist")
            task = dict(self.records[task_id])
            if actor != "lead":
                if task["assignee"] != actor:
                    raise ToolError("task_not_owned", "Only the owner can update this task")
                if generation is not None and generation != task["generation"]:
                    raise ToolError("stale_generation", "Task belongs to a newer execution")
                if set(updates) - {"status", "result", "artifact_refs", "checks"}:
                    raise ToolError(
                        "invalid_task_update", "Child may only submit results and evidence"
                    )
                if updates.get("status") not in {"in_progress", "submitted", "failed"}:
                    raise ToolError(
                        "invalid_task_update", "Only lead can accept or reassign a task"
                    )
            status = updates.get("status", task["status"])
            if (
                status == "cancelled"
                and task["assignee"] is not None
                and self.team.records[task["assignee"]]["status"]
                in {"starting", "running", "needs_approval", "stopping", "stop_unconfirmed"}
            ):
                raise ToolError("agent_still_running", "Stop the member before cancelling its task")
            if status != task["status"] and status not in TRANSITIONS[task["status"]]:
                raise ToolError(
                    "invalid_task_transition", f"Cannot move {task['status']} to {status}"
                )
            if task["status"] != "open" and set(updates) & {
                "title",
                "description",
                "depends_on",
                "scope",
                "acceptance",
            }:
                raise ToolError(
                    "task_in_use", "Return the task to open before changing its contract"
                )
            for field, limit in (("title", 200), ("description", 4000), ("result", 4000)):
                if field in updates:
                    task[field] = bounded_text(updates[field], field, limit, empty=field != "title")
            if "depends_on" in updates:
                validate_dependencies(task_id, updates["depends_on"], self.records)
                task["dependencies"] = list(updates["depends_on"])
            for field in ("artifact_refs", "checks", "acceptance", "scope"):
                if field in updates:
                    task[field] = (
                        checked_scope(updates[field]) if field == "scope" else updates[field]
                    )
            if status in {"submitted", "done"}:
                await self.team.check_evidence(task, accepting=status == "done")
            task["status"] = status
            if status == "done":
                task["done_by"] = task["assignee"]
            if status == "open" and self.records[task_id]["status"] != "open":
                task.update(
                    assignee=None,
                    done_by=None,
                    result="",
                    checks=[],
                    artifact_refs=[],
                    source_fingerprint=None,
                )
            await self.team.commit(tasks=[task])
            return task

    def settled(self, agent_id, outcome, text):
        tasks = []
        for original in self.records.values():
            if original["assignee"] != agent_id or original["status"] != "in_progress":
                continue
            task = dict(original)
            if outcome == "completed":
                task.update(status="submitted", result=text[:4000])
            elif outcome == "failed":
                task.update(status="failed", result=text[:4000])
            else:
                task.update(status="open", assignee=None, last_assignee=agent_id, interrupted=True)
            tasks.append(task)
        return tasks

    def assignment(self, task_id, instruction):
        if task_id is None:
            return instruction
        task = self.records[task_id]
        dependencies = [
            {
                "task_id": key,
                "owner": self.records[key]["done_by"],
                "result": self.records[key]["result"],
                "artifacts": self.records[key]["artifact_refs"],
            }
            for key in task["dependencies"]
        ]
        return (
            instruction
            + "\n\n任务契约（由看板生成）：\n"
            + json_dumps(
                {
                    "task_id": task_id,
                    "goal": task["description"],
                    "acceptance": task["acceptance"],
                    "scope": task["scope"],
                    "dependencies": dependencies,
                    "delivery": "提交 submitted，附验证记录引用；由 lead 验收，不自行标记 done。",
                    "evidence": "list_tasks.evidence 提供本任务工具调用的 call_id，不要编造。",
                },
                ensure_ascii=False,
            )
        )
