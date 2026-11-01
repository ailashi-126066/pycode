# Pi 没有，pycode 新增；参照 MewCode hooks/engine.py and executors.py
# SHA-256 engine: 91e9d3162556ea54e9c1d211feb3ecf4126a302f50f7eb3e5f584194be9e9b29
# SHA-256 executors: 2b16275a172f0c998ee24da7b693c3aa0bb248e374bff66e7b027c15672806f3
"""Configured synchronous hooks with an explicitly injected sandbox command runner."""

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import uuid4

from pycode.hooks.config import load_hooks
from pycode.hooks.types import Hook, HookAction, HookContext
from pycode.kernel import (
    CommandRunner,
    DomainError,
    EntryDraft,
    EventStream,
    RunScope,
    SessionScope,
    ToolCall,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    clone_value,
    owned_io,
)

if TYPE_CHECKING:
    from pycode.session import SqliteSessionStore

TOKENS = (
    r"\$ENV\.[A-Z_][A-Z0-9_]*|\$TOOL_ARGS\.[A-Za-z0-9_-]+|"
    r"\$(?:EVENT|TOOL_NAME|FILE_PATH|MESSAGE|ERROR)\b"
)


class ConfiguredHookActions:
    def __init__(self, run_command: CommandRunner, shell: str, environment: Mapping[str, str]):
        self._run_command = run_command
        self._shell = shell
        self._environment = environment

    def token(self, token, context):
        if not token.startswith("$ENV."):
            return context.expand(token)
        name = token[len("$ENV.") :]
        if name not in self._environment:
            raise ToolError("hook_environment_missing", "Hook environment reference is unavailable")
        return self._environment[name]

    def command(self, template, context):
        # Only explicit substitutions are added; the sandbox runner supplies its safe base env.
        environment = {}
        index = 0

        def bind(match):
            nonlocal index
            name = "PYCODE_HOOK_VALUE_" + str(index)
            index += 1
            environment[name] = self.token(match.group(0), context)
            return "${" + name + "}"

        return re.sub(TOKENS, bind, template), environment

    async def execute(self, action: HookAction, context: HookContext, tool: ToolContext):
        if action.type == "prompt":
            # Config parsing already rejects invalid action types. Secrets never enter prompts.
            if "$ENV." in action.message:
                raise ToolError(
                    "invalid_hook_prompt", "Prompt actions cannot inject environment secrets"
                )
            return ToolResult(tool.call_id, tool.tool_name, context.expand(action.message))
        command, environment = self.command(action.command, context)
        return await self._run_command(
            (self._shell, "-c", command), tool.workspace, environment, action.timeout
        )


class HookEngine:
    def __init__(
        self,
        hooks: tuple[Hook, ...],
        history: "SqliteSessionStore",
        actions: ConfiguredHookActions,
        events: EventStream,
    ) -> None:
        self._hooks = hooks
        self._history = history
        self._actions = actions
        self._events = events

    async def fire(self, context: HookContext) -> str | None:
        """Run matching Hooks in order; a pre_tool_use rejection is returned as text."""
        for hook in self._hooks:
            if hook.event != context.event_name:
                continue
            if hook.condition is not None and not hook.condition.matches(context.fields()):
                continue
            if hook.once and await self.fired_before(hook):
                continue
            result = await self.run(hook, context)
            if context.event_name != "pre_tool_use":
                continue
            # 11.1 section 26 #3: fail-closed; only a successful run lets the tool proceed.
            if result.is_error:
                return "Hook check did not complete successfully: " + result.text
            if hook.reject:
                return result.text or "Configured Hook rejected this tool"
        return None

    async def fired_before(self, hook: Hook) -> bool:
        snapshot = await self._history.snapshot()
        return any(
            entry.type == "hook_context" and entry.data["hook_id"] == hook.id
            for entry in snapshot.entries
        )

    async def run(self, hook: Hook, context: HookContext) -> ToolResult:
        call_id = f"hook_{hook.id}_{uuid4().hex[:8]}"
        tool = ToolContext(
            context.session_id,
            context.run_id,
            call_id,
            context.workspace,
            call_id=call_id,
            tool_name="hook__" + hook.id,
            agent_id="hook",
        )
        try:
            result = await self._actions.execute(hook.action, context, tool)
        except Exception as error:
            # A Hook that cannot run is a failed Hook; pre_tool_use then blocks the tool.
            result = ToolResult(
                call_id,
                tool.tool_name,
                str(error) or type(error).__name__,
                is_error=True,
                error_code=error.code if isinstance(error, DomainError) else "hook_failed",
            )
        # Only output that enters the model context is recorded and announced.
        if hook.action.type == "command" or not result.is_error:
            await self._history.append_entry(
                EntryDraft(
                    "hook_context",
                    data={
                        "text": result.text
                        if hook.action.type == "prompt"
                        else (
                            f"Hook [{hook.id}] {context.event_name}: "
                            + ("failed" if result.is_error else "completed")
                            + "\n"
                            + result.text
                        ),
                        "hook_id": hook.id,
                    },
                    run_id=context.run_id,
                )
            )
            await self._events.emit(
                "notification",
                {
                    "kind": "hook",
                    "hook_id": hook.id,
                    "event": context.event_name,
                    "success": not result.is_error,
                    "message": result.text,
                    "full_output_path": result.details.get("full_output_path"),
                },
            )
        return result


class HooksService:
    """Own loaded configuration; app connects actual lifecycle and tool callbacks explicitly."""

    def __init__(
        self,
        *,
        trusted: bool,
        worker: bool,
        run_command: CommandRunner,
        shell: str,
        environment: Mapping[str, str],
        enabled: bool = True,
    ):
        self.trusted = trusted
        self.worker = worker
        self.enabled = enabled
        self.actions = ConfiguredHookActions(run_command, shell, environment)

    async def open(self, session: SessionScope) -> None:
        self.session = session
        roots = [("user", session.user_data / "hooks.json")]
        if self.trusted:
            roots.append(("project", session.workspace / ".pycode" / "hooks.json"))
        hooks = await owned_io(load_hooks, tuple(roots)) if self.enabled else ()
        if self.worker or session.read_only:
            hooks = tuple(hook for hook in hooks if hook.action.type == "prompt")
        self.engine = HookEngine(hooks, session.history, self.actions, session.events)
        await self.engine.fire(
            HookContext(session.session_id, "startup", str(session.workspace), "startup")
        )

    async def run_hook(
        self, run: RunScope, event: str, *, message: str = "", error: str = ""
    ) -> None:
        await self.engine.fire(
            HookContext(
                run.session.session_id,
                run.run_id,
                str(run.session.workspace),
                event,
                message=message,
                error=error,
            )
        )

    async def before_tool_call(self, run: RunScope, spec: ToolSpec, call: ToolCall):
        rejection = await self.tool_hook(run, "pre_tool_use", call)
        if rejection is not None:
            return {"block": True, "reason": rejection}
        return None

    async def after_tool_call(self, run: RunScope, call: ToolCall, result: ToolResult):
        await self.tool_hook(run, "post_tool_use", call, result)
        snapshot = result.details.get("file_snapshot")
        if snapshot is not None and snapshot["paths"]:
            await self.tool_hook(run, "file_change", call, result)
        if result.is_error:
            await self.tool_hook(run, "error", call, result)
        return None

    async def permission_request(self, run: RunScope, call: ToolCall) -> None:
        await self.tool_hook(run, "permission_request", call)

    async def tool_hook(
        self, run: RunScope, name: str, call: ToolCall, result: ToolResult | None = None
    ):
        return await self.engine.fire(
            HookContext(
                run.session.session_id,
                run.run_id,
                str(run.session.workspace),
                name,
                call.name,
                clone_value(call.arguments),
                str(call.arguments.get("path", "")),
                result.text if result is not None else "",
                (result.error_code or "") if result is not None else "",
            )
        )

    async def aclose(self) -> None:
        await self.engine.fire(
            HookContext(
                self.session.session_id, "shutdown", str(self.session.workspace), "shutdown"
            )
        )
