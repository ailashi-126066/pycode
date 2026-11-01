"""Hooks actions: component-owned implementation."""

# Source: MewCode local hooks/executors.py; SHA-256:
# 2b16275a172f0c998ee24da7b693c3aa0bb248e374bff66e7b027c15672806f3
# Adaptation: sandboxed commands with bounded output; prompts never expand secrets.
# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

import os
import re

from pycode.hooks.types import HookAction, HookContext
from pycode.kernel import ToolContext, ToolError, ToolResult
from pycode.sandbox import ProcessRunner, subprocess_environment, validate_passthrough

TOKENS = (
    r"\$ENV\.[A-Z_][A-Z0-9_]*|\$TOOL_ARGS\.[A-Za-z0-9_-]+|"
    r"\$(?:EVENT|TOOL_NAME|FILE_PATH|MESSAGE|ERROR)\b"
)


class ConfiguredHookActions:
    def __init__(
        self,
        outputs,
        shell,
        sandbox,
        settings,
        environment,
        *,
        process_runner: ProcessRunner,
        passthrough=(),
    ):
        self._outputs = outputs
        self._run_process = process_runner
        self._shell = shell
        self._sandbox = sandbox
        self._settings = settings
        self._environment = environment
        self._passthrough = validate_passthrough(passthrough)

    def token(self, token, context):
        if not token.startswith("$ENV."):
            return context.expand(token)
        name = token[len("$ENV.") :]
        if name not in self._environment:
            raise ToolError("hook_environment_missing", "Hook environment reference is unavailable")
        return self._environment[name]

    def command(self, template, context):
        # Only explicit $ENV.NAME references reach the command; nothing else is inherited.
        environment = subprocess_environment(os.environ, passthrough=self._passthrough)
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
            # Environment secrets are not expanded into model context.
            if "$ENV." in action.message:
                raise ToolError(
                    "invalid_hook_prompt", "Prompt actions cannot inject environment secrets"
                )
            return ToolResult(tool.call_id, tool.tool_name, context.expand(action.message))
        if action.type == "command":
            command, environment = self.command(action.command, context)
            argv = (self._shell, "-c", command)
            argv = await self._sandbox.launch(argv, tool.workspace, self._settings)
            writer = await self._outputs.begin(tool, "head")
            return await self._run_process(
                argv,
                tool.workspace,
                writer,
                ToolResult(tool.call_id, tool.tool_name, ""),
                limit_seconds=action.timeout,
                prefix="hook_command",
                environment=environment,
            )
        raise ToolError("unknown_hook_action", "Unknown Hook action")
