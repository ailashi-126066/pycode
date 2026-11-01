# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/bash.ts
# 参照 MewCode local mewcode/tools/bash.py; SHA-256:
# 24e05e37c804c4fc9bec2a50e95090b33fd4f1f89d4d7c7baeddeae79d9b84b3
# Source: MewCode local mewcode/processes.py; SHA-256:
# a8049900efb1977eb4866bd871d3bcd101abaffe35d70452c75da69da45096b4
# Adaptation: explicit shell/output dependencies, streamed output and owned native process groups.
"""Shell tool with bounded streaming output; execution goes through ToolExecutor."""

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pycode.kernel import ToolContext, ToolError, ToolResult, ToolSpec
from pycode.sandbox.process import run_process, subprocess_environment, validate_passthrough
from pycode.tools import TempOutputStore

if TYPE_CHECKING:
    from pycode.sandbox.sandbox import Sandbox


def find_bash() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            bundled = Path(git).parent.parent / "bin" / "bash.exe"
            if bundled.is_file():
                return str(bundled)
    executable = shutil.which("bash")
    if not executable:
        raise ToolError("shell_unavailable", "Bash executable is unavailable")
    return executable


# Pi: bash.ts. Parallel by default like Pi; the output store keeps the tail and the complete output.
class Bash:
    spec = ToolSpec(
        name="bash",
        description=(
            "Execute a bash command in the current working directory. Returns stdout and stderr. "
            "Output is truncated to last 2000 lines or 50KB (whichever is hit first). If "
            "truncated, full output is saved to a temp file. Optionally provide a timeout in "
            "seconds. "
            # pycode (11.1 section 40), not in Pi: repeated polling trips the progress guard.
            "To wait for a service, build or CI, wait inside one command with a loop, sleep and "
            "a timeout (for example `timeout 300 sh -c 'until curl -sf URL; do sleep 5; done'`) "
            "instead of running the same command again and again."
        ),
        category="command",
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 600,
                    "default": 120,
                    "description": "Timeout in seconds",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        exposure="direct",
        prompt_snippet="Execute bash commands (ls, grep, find, etc.)",
    )

    def __init__(
        self,
        executable: str,
        outputs: TempOutputStore,
        sandbox: "Sandbox",
        environment=None,
        *,
        passthrough=(),
    ) -> None:
        self._executable = executable
        self._outputs = outputs
        self._sandbox = sandbox
        # Source environment, filtered at execution time; never an already trusted environment.
        self._environment = environment
        self._passthrough = validate_passthrough(passthrough)

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        argv = await self._sandbox.launch(
            (self._executable, "-c", arguments["command"]), context.workspace
        )
        writer = await self._outputs.begin(context, "tail")
        return await run_process(
            argv,
            context.workspace,
            writer,
            ToolResult(context.call_id, self.spec.name, ""),
            limit_seconds=arguments.get("timeout", 120),
            progress=context.progress,
            # Never inherit provider keys or other credentials from the parent process.
            environment=subprocess_environment(
                os.environ if self._environment is None else self._environment,
                passthrough=self._passthrough,
            ),
        )
