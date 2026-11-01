"""Team tools: component-owned implementation."""

from pycode.kernel import StorageError, ToolError, ToolResult, ToolSpec, json_dumps

_TEXT = {"type": "string"}


_AGENT = {"type": "string", "pattern": "^[0-9a-f]{8}$"}


_TASK = {"type": "string", "pattern": "^t[0-9]{1,4}$"}


_DEPS = {"type": "array", "items": _TASK, "maxItems": 20}
_TEXTS = {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 30}
_CHECKS = {
    "type": "array",
    "maxItems": 30,
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 500},
            "status": {"enum": ["passed", "failed", "not_run"]},
            "call_id": {"type": "string"},
        },
        "required": ["name", "status"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"status": {"enum": ["passed", "failed"]}}},
                "then": {"required": ["call_id"]},
            }
        ],
    },
}


def schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class TeamTool:
    def __init__(self, member, name: str, properties=None, required=(), *, provider=None):
        self.member, self.provider = member, provider
        self.spec = ToolSpec(
            name,
            {
                "list_tasks": "List tasks, dependencies and saved output IDs.",
                "update_task": "Submit evidence; only lead can accept submitted tasks.",
                "handoff_task": "Transfer an unfinished task after stopping its current member.",
            }.get(name, "Local team: " + name.replace("_", " ")),
            schema(properties or {}, required),
            category="write" if name == "merge_agent" else "read",
            source="team",
            execution_mode="parallel"
            if name in {"list_agents", "list_tasks", "review_agent"}
            else "sequential",
        )

    async def execute(self, context, arguments):
        self.member.check_current()
        team, actor, name = self.member.team, self.member.agent_id, self.spec.name
        try:
            result = await self.dispatch(team, actor, name, context, arguments)
        except StorageError as error:
            raise ToolError("team_storage_error", str(error)) from error
        error_code = result.get("error_code")
        if error_code is not None and not isinstance(error_code, str):
            raise TypeError("Team error_code must be a string")
        return ToolResult(
            context.call_id,
            name,
            json_dumps(result, ensure_ascii=False),
            structured_content=result,
            is_error=bool(result.get("error_code")),
            error_code=error_code,
            details={"team_message_ids": [m["id"] for m in result.get("messages", ())]}
            if name == "wait_agents"
            else {},
        )

    async def dispatch(self, team, actor, name, context, args):
        if name == "spawn_agent":
            return await team.spawn(**args, provider=self.provider, run_id=context.run_id)
        if name == "send_message":
            return await team.inbox.send(
                actor, run_id=context.run_id, generation=self.member.generation, **args
            )
        if name == "wait_agents":
            return await team.inbox.wait(**args, operation_id=context.operation_id)
        if name == "handoff_task":
            return await team.handoff_task(args["task_id"])
        if name == "cancel_agent":
            return await team.cancel(args["id"])
        if name == "list_agents":
            return team.agents_view()
        if name == "create_task":
            return await team.board.create_task(**args)
        if name == "update_task":
            return await team.board.update_task(
                actor=actor, generation=self.member.generation, **args
            )
        if name == "list_tasks":
            return {"tasks": list((await team.board.tasks()).values())}
        if name == "review_agent":
            return await team.review(args["id"])
        if name == "merge_agent":
            preview = team.previews.pop(context.operation_id)
            return await team.merge(args["id"], preview, args.get("action", "merge"))
        raise AssertionError("Unregistered team tool")


def child_tools(member):
    return (
        TeamTool(
            member,
            "send_message",
            {
                "to": _TEXT,
                "text": _TEXT,
                "task_id": _TASK,
                "kind": {"enum": ["message", "progress", "blocked", "delivery"]},
                "artifact_refs": _TEXTS,
            },
            ("to", "text"),
        ),
        TeamTool(member, "list_agents"),
        TeamTool(member, "list_tasks"),
        TeamTool(
            member,
            "update_task",
            {
                "task_id": _TASK,
                "status": {"enum": ["in_progress", "submitted", "failed"]},
                "result": _TEXT,
                "artifact_refs": _TEXTS,
                "checks": _CHECKS,
            },
            ("task_id", "status"),
        ),
    )


def lead_tools(member, provider):
    basic = tuple(tool for tool in child_tools(member) if tool.spec.name != "update_task")
    return (
        *basic,
        TeamTool(
            member,
            "spawn_agent",
            {
                "mode": {"enum": ["readonly", "worker"]},
                "role": _TEXT,
                "task": _TEXT,
                "task_id": _TASK,
            },
            ("mode", "role", "task"),
            provider=provider,
        ),
        TeamTool(
            member,
            "wait_agents",
            {
                "ids": {"type": "array", "items": _AGENT, "maxItems": 64},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
            },
        ),
        TeamTool(member, "cancel_agent", {"id": _AGENT}, ("id",)),
        TeamTool(member, "handoff_task", {"task_id": _TASK}, ("task_id",)),
        TeamTool(
            member,
            "create_task",
            {
                "title": _TEXT,
                "description": _TEXT,
                "depends_on": _DEPS,
                "acceptance": _TEXTS,
                "scope": _TEXTS,
            },
            ("title",),
        ),
        TeamTool(
            member,
            "update_task",
            {
                "task_id": _TASK,
                "title": _TEXT,
                "description": _TEXT,
                "status": {
                    "enum": ["open", "in_progress", "submitted", "done", "failed", "cancelled"]
                },
                "result": _TEXT,
                "artifact_refs": _TEXTS,
                "checks": _CHECKS,
                "depends_on": _DEPS,
                "acceptance": _TEXTS,
                "scope": _TEXTS,
            },
            ("task_id",),
        ),
        TeamTool(member, "review_agent", {"id": _AGENT}, ("id",)),
        TeamTool(
            member,
            "merge_agent",
            {"id": _AGENT, "action": {"enum": ["merge", "prepare_resolution"]}},
            ("id",),
        ),
    )
