# Pi 没有，pycode 新增
# Source: code-python/mewcode/commands/registry.py and parser.py.
# Adaptation: pycode's existing commands and application entry points only.
"""One registry for existing terminal command dispatch, help and completion."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    handler: str
    usage: str = ""
    needs_arguments: bool = False
    raw_arguments: bool = False

    @property
    def completion(self):
        return self.name + (" " if self.needs_arguments else "")


COMMANDS = (
    Command(
        "/session", "Manage sessions", "command_session", "new|list|open ID|name NAME|fork ID", True
    ),
    Command("/new", "Start a new session", "command_new"),
    Command("/clear", "Start a new session", "command_new"),
    Command("/resume", "Resume pending work", "command_resume"),
    Command("/model", "List or select a provider model", "command_model", "[PROVIDER MODEL]"),
    Command(
        "/memory",
        "View and manage memory",
        "command_memory",
        "[mode|edit|delete|clear|extract]",
    ),
    Command(
        "/storage",
        "Inspect or collect stored artifacts",
        "command_storage",
        "status|gc [--dry-run]",
        True,
    ),
    Command("/plan", "Enter plan mode", "command_plan", "[TASK]", True),
    Command("/tasks", "Show or clear the task board", "command_tasks", "[clear]"),
    Command("/agents", "Inspect or cancel local agents", "command_agents", "[cancel ID]"),
    Command("/usage", "Show token usage", "command_usage"),
    Command("/compact", "Compact conversation context", "command_compact"),
    Command("/reload", "Refresh the skill catalog", "command_reload"),
    Command("/skills", "Search available skills", "command_skills", "[QUERY]", True),
    Command(
        "/skill",
        "Create, read or install a skill",
        "command_skill",
        "install URL | create NAME DESCRIPTION | read NAME",
        True,
    ),
    Command("/branch", "Branch from an entry", "command_branch", "ENTRY|root", True),
    Command("/theme", "Switch color theme", "command_theme"),
    Command(
        "/revert",
        "Revert conversation or files after confirmation",
        "command_revert",
        "ENTRY|root [dialogue|code|both]",
        True,
    ),
    Command("/quit", "Close pycode", "command_quit"),
    Command("/exit", "Close pycode", "command_quit"),
)


class CommandRegistry:
    def __init__(self):
        self._commands = {command.name: command for command in COMMANDS}
        self._skills = {}
        self.hidden = set()

    def set_skills(self, entries):
        self._skills = {
            "/skill:" + entry["name"]: Command(
                "/skill:" + entry["name"],
                " ".join(entry["description"].split())[:80],
                "command_load_skill",
                "[ARGUMENTS]",
                True,
                True,
            )
            for entry in entries
        }

    def find(self, name):
        if name in self.hidden or (name.startswith("/skill:") and "/skill" in self.hidden):
            return None
        command = self._commands.get(name) or self._skills.get(name)
        if command is None and name.startswith("/skill:"):
            # Preserve the existing trusted-catalog validation and error path.
            return Command(name, "Run a skill", "command_load_skill", raw_arguments=True)
        return command

    def matches(self, text):
        if not text.startswith("/") or any(character.isspace() for character in text):
            return ()
        return tuple(
            command
            for name, command in sorted((self._commands | self._skills).items())
            if name.startswith(text) and name not in self.hidden
        )
