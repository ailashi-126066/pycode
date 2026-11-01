"""Agent, SendMessage and StopAgent: Claude Code's subagent tools."""

from pycode.kernel import ToolContext, ToolResult, ToolSpec, plain_data
from pycode.subagents.manager import SubagentManager, result_text
from pycode.subagents.types import Subagent

# Slug: lowercase letters, digits and hyphens, at most 64 characters.
_NAME = {"type": "string", "pattern": "^[a-z0-9-]{1,64}$"}
# The name shown in results; the 8-character agent id also works.
_AGENT = {**_NAME, "description": "The subagent's name (or its agent id)"}


def schema(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def spec(name, description, properties, required, snippet):
    # Starting or stopping a subagent needs no approval; the subagent's own tool calls do.
    # These calls have effects, so a restart never reruns them.
    return ToolSpec(
        name,
        description,
        schema(properties, required),
        category="read",
        source="subagents",
        replay="never",
        exposure="direct",
        prompt_snippet=snippet,
    )


def subagent_result(context: ToolContext, name: str, agent: Subagent) -> ToolResult:
    return ToolResult(
        context.call_id,
        name,
        result_text(agent),
        is_error=agent.status in {"failed", "cancelled"},
        error_code=agent.error_code,
        details={"subagent": plain_data(agent)},
    )


class AgentTool:
    def __init__(self, manager: SubagentManager):
        self.manager = manager
        self.spec = spec(
            "Agent",
            "Start a subagent with its own context; only its final result comes back. "
            "mode readonly may only read; mode worktree edits in a temporary git worktree, "
            "which is removed when unchanged; otherwise merge its branch with `git merge "
            "<branch>` and then run the relevant tests or type checks. On conflicts or "
            "failures (including semantic breakage git does not report), fix them yourself "
            "or use SendMessage to have that subagent fix its own branch before merging "
            "again; if that fails, undo the merge (`git merge --abort` / `git reset`) and "
            "tell the user which files conflict. Give parallel subagents different files. "
            "background=true returns at once and you are notified when it finishes. "
            "At max_turns it returns a partial result that SendMessage can continue. "
            "In non-interactive runs (pycode run / -p) subagent calls that need approval "
            "fail with approval_required. name is unique in this session; for a task board "
            "step use the step id (for example n3); without it the system names it a1, a2, ...",
            {
                "name": _NAME,
                "description": {"type": "string", "maxLength": 200},
                "prompt": {"type": "string", "maxLength": 20000},
                "mode": {"enum": ["readonly", "worktree"]},
                "background": {"type": "boolean"},
                "max_turns": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            ("description", "prompt", "mode"),
            "Agent: delegate a task to a subagent (readonly or worktree)",
        )

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        agent = await self.manager.start(
            arguments["description"],
            arguments["prompt"],
            arguments["mode"],
            # Optional in the schema.
            background=arguments.get("background", False),
            max_turns=arguments.get("max_turns"),
            parent_call_id=context.call_id,
            run_id=context.run_id,
            name=arguments.get("name"),
        )
        if not agent.background:
            agent = await self.manager.wait(agent.agent_id)
        return subagent_result(context, self.spec.name, agent)


class SendMessageTool:
    def __init__(self, manager: SubagentManager):
        self.manager = manager
        self.spec = spec(
            "SendMessage",
            "Continue a finished or partial subagent with a follow-up message, in its own "
            "history. It runs in the foreground or background as it was started.",
            {"agent_id": _AGENT, "message": {"type": "string", "maxLength": 20000}},
            ("agent_id", "message"),
            "SendMessage: continue or ask a subagent again",
        )

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        agent = await self.manager.send(
            arguments["agent_id"],
            arguments["message"],
            parent_call_id=context.call_id,
            run_id=context.run_id,
        )
        if not agent.background:
            agent = await self.manager.wait(agent.agent_id)
        return subagent_result(context, self.spec.name, agent)


class StopAgentTool:
    def __init__(self, manager: SubagentManager):
        self.manager = manager
        self.spec = spec(
            "StopAgent",
            "Stop a running subagent; it ends as cancelled.",
            {"agent_id": _AGENT},
            ("agent_id",),
            "StopAgent: stop a running subagent",
        )

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        agent = await self.manager.stop(arguments["agent_id"])
        return subagent_result(context, self.spec.name, agent)


def subagent_tools(manager: SubagentManager):
    return AgentTool(manager), SendMessageTool(manager), StopAgentTool(manager)
