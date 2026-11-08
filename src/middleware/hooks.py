"""Hooks: commands from ~/.pycode/hooks.json that run at fixed moments, without the model.

    {"hooks": {
        "post_tool_use": [{"matcher": "edit|write", "command": "ruff format \\"$PYCODE_FILE\\""}],
        "pre_tool_use":  [{"matcher": "bash", "command": "~/.pycode/block-push.sh"}],
        "stop":          [{"command": "osascript -e 'display notification \\"done\\"'"}]}}

Moments: session_start, session_end, pre_tool_use, post_tool_use, pre_compact, stop. `matcher`
is a regular expression for the tool name (tool moments only; left out, it matches every tool).
Only the user's own file is read: a project file would run its commands on opening the repo.

A command runs like the bash tool's: bash -c, in the workspace, with credentials left out of
the environment, under Seatbelt when that is on, for at most 60 seconds. It reads a JSON
description of the moment on stdin; PYCODE_EVENT, PYCODE_SESSION, PYCODE_TOOL and PYCODE_FILE
(the path a tool works on, when it has one) are set too. Exit code 2 means:
- pre_tool_use: refuse the call; what the command printed is the reason the model reads;
- post_tool_use: what it printed is added to the tool's result for the model.
Any other failure is only shown to the user.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.config import get_config

from ..sandbox import sandboxed
from ..tools.bash import command_environment

EVENTS = {"session_start", "session_end", "pre_tool_use", "post_tool_use", "pre_compact", "stop"}
TOOL_EVENTS = {"pre_tool_use", "post_tool_use"}
SECONDS = 60
OUTPUT_LIMIT = 4000  # characters of a hook's output passed on
BLOCK = 2  # the exit code that refuses a tool call, or feeds the output to the model


@dataclass(frozen=True)
class Hook:
    event: str
    command: str
    matcher: re.Pattern | None = None


@dataclass(frozen=True)
class Ran:
    hook: Hook
    code: int | None  # None: it could not start, or ran out of time
    output: str


def load_hooks(path: Path) -> list[Hook]:
    """The configured hooks; a mistake in the file stops pycode with a message about it."""
    if not path.exists():
        return []
    try:
        configured = json.loads(path.read_text()).get("hooks", {})
        hooks = []
        for event, entries in configured.items():
            if event not in EVENTS:
                raise ValueError(f"unknown moment {event}; use one of {', '.join(sorted(EVENTS))}")
            for entry in entries:
                matcher = entry.get("matcher")
                if matcher and event not in TOOL_EVENTS:
                    raise ValueError(f"{event} has no tool to match")
                pattern = re.compile(matcher) if matcher else None
                hooks.append(Hook(event, entry["command"], pattern))
        return hooks
    except (ValueError, KeyError, TypeError, AttributeError, re.error) as error:
        raise ValueError(f"{path}: {error}") from error


class Hooks(AgentMiddleware):
    """Runs the tool hooks around each tool call and the stop hooks when a run ends; the
    interface fires the session hooks and compaction the pre_compact ones."""

    def __init__(self, hooks: list[Hook], workspace: Path, sandbox: bool, *, stop: bool = True):
        super().__init__()
        self.hooks = hooks
        self.workspace = workspace.resolve()
        self.sandbox = sandbox
        self.stop = stop  # subagents run tool hooks only

    def matching(self, event: str, tool: str | None = None) -> list[Hook]:
        return [
            hook
            for hook in self.hooks
            if hook.event == event and (hook.matcher is None or hook.matcher.search(tool or ""))
        ]

    async def fire(
        self, event: str, payload: dict, *, cwd: Path | None = None, tool=None, writer=None
    ) -> list[Ran]:
        """Run every hook of this moment in order; tell the user about failures."""
        ran = []
        for hook in self.matching(event, tool):
            result = await self.run(hook, {"event": event, **payload}, cwd or self.workspace)
            ran.append(result)
            if result.code not in (0, BLOCK) and writer is not None:
                why = "没有运行完" if result.code is None else f"退出码 {result.code}"
                writer({"hook": f"hook {event} 失败（{why}）：{hook.command}\n{result.output}"})
        return ran

    async def run(self, hook: Hook, payload: dict, cwd: Path) -> Ran:
        shell = shutil.which("bash") or "/bin/bash"
        argv = [shell, "-c", hook.command]
        if self.sandbox:
            argv = sandboxed(argv, cwd)
        environment = command_environment()
        environment |= {"PYCODE_EVENT": payload["event"]}
        environment |= {"PYCODE_SESSION": payload.get("session_id", "")}
        environment |= {"PYCODE_TOOL": payload.get("tool", "")}
        if isinstance(path := payload.get("args", {}).get("path"), str):
            environment["PYCODE_FILE"] = str((cwd / path).resolve())
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own process group, stopped as a whole
            )
        except OSError as error:
            return Ran(hook, None, str(error))
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode()
            out, _ = await asyncio.wait_for(process.communicate(data), SECONDS)
            return Ran(hook, process.returncode, out.decode("utf-8", "replace")[:OUTPUT_LIMIT])
        except TimeoutError:
            return Ran(hook, None, f"stopped after {SECONDS} seconds")
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    # ---- around the agent ----

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        cwd = request.runtime.context.files.workspace
        writer = request.runtime.stream_writer
        payload = {"session_id": thread_id(), "tool": call["name"], "args": call["args"]}
        for ran in await self.fire(
            "pre_tool_use", payload, cwd=cwd, tool=call["name"], writer=writer
        ):
            if ran.code == BLOCK:
                reason = ran.output.strip() or "a hook refused this call"
                return ToolMessage(
                    f"Refused by a hook: {reason}",
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error",
                )
        result = await handler(request)
        if not isinstance(result, ToolMessage) or not self.matching("post_tool_use", call["name"]):
            return result  # a Command (e.g. TaskUpdate) has no single result to add to
        payload |= {"result": result.text[:OUTPUT_LIMIT], "error": result.status == "error"}
        notes = [
            ran.output.strip()
            for ran in await self.fire(
                "post_tool_use", payload, cwd=cwd, tool=call["name"], writer=writer
            )
            if ran.code == BLOCK and ran.output.strip()
        ]
        if notes:
            text = result.text + "\n\n[From a hook]\n" + "\n".join(notes)
            result = result.model_copy(update={"content": text})
        return result

    async def aafter_agent(self, state, runtime):
        if self.stop:
            await self.fire("stop", {"session_id": thread_id()}, writer=runtime.stream_writer)
        return None


def thread_id() -> str:
    return get_config()["configurable"]["thread_id"]
