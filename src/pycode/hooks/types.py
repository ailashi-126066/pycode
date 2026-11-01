"""Hooks types: component-owned implementation."""

# Source: MewCode local hooks/conditions.py and models.py; fixed SHA in docs/source-map.md.
# SHA-256 conditions: d11485198df8f4ebf76a009c4b13eaf51883ae0a1bbc14f734779d684817f870
# SHA-256 models: 0a07d8f977875f523398ca5972eb11c1be0a074df7045711e758f92bfb416e19
# Adaptation: immutable native records; conditions compile and evaluate in process.
import fnmatch
import re
from dataclasses import dataclass, field

from pycode.kernel import DomainError

HOOK_EVENTS = frozenset(
    {
        "startup",
        "shutdown",
        "run_start",
        "run_end",
        "turn_start",
        "turn_end",
        "pre_send",
        "post_receive",
        "pre_tool_use",
        "post_tool_use",
        "compact",
        "permission_request",
        "file_change",
        "error",
    }
)
# 11.1 section 21 #8: old names are configuration errors that name the replacement.
RENAMED_EVENTS = {
    "session_start": "run_start",
    "session_end": "run_end",
    "command_execute": 'pre_tool_use with "if": "tool == \\"bash\\""',
}
MAX_PATTERN_LENGTH = 256


@dataclass(frozen=True)
class HookCondition:
    field: str
    operator: str
    value: str
    # Compiled once when the configuration loads; =~ conditions only.
    pattern: re.Pattern[str] | None = field(
        default=None, compare=False, metadata={"serialize": False}
    )

    def matches(self, fields: dict[str, str]) -> bool:
        value = fields.get(self.field, "")
        if self.operator == "==":
            return value == self.value
        if self.operator == "!=":
            return value != self.value
        if self.operator == "~=":
            return fnmatch.fnmatch(value, self.value)
        if self.pattern is None:
            raise DomainError("invalid_hook_condition", "Regex condition was not compiled")
        return self.pattern.search(value) is not None


@dataclass(frozen=True)
class HookConditionGroup:
    conditions: tuple[HookCondition, ...]
    logic: str = "and"

    def matches(self, fields: dict[str, str]) -> bool:
        results = (condition.matches(fields) for condition in self.conditions)
        return all(results) if self.logic == "and" else any(results)


def compile_pattern(value):
    if value.startswith("/") and value.endswith("/"):
        value = value[1:-1]
    # Regexes run in process; a length limit keeps a hostile pattern small.
    if len(value) > MAX_PATTERN_LENGTH:
        raise DomainError("invalid_hook_condition", "Regex condition exceeds 256 characters")
    try:
        return re.compile(value)
    except re.error as error:
        raise DomainError("invalid_hook_condition", "Invalid regex condition") from error


def parse_condition(expression):
    if not expression.strip():
        return None
    if "&&" in expression and "||" in expression:
        raise DomainError("invalid_hook_condition", "Cannot mix && and || in a condition")
    logic = "or" if "||" in expression else "and"
    parts = expression.split("||" if logic == "or" else "&&")
    conditions = []
    for part in parts:
        for operator in ("==", "!=", "=~", "~="):
            if operator not in part:
                continue
            key, value = (item.strip() for item in part.split(operator, 1))
            if not key or not value:
                raise DomainError("invalid_hook_condition", "Condition needs a field and value")
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            pattern = compile_pattern(value) if operator == "=~" else None
            conditions.append(HookCondition(key, operator, value, pattern))
            break
        else:
            raise DomainError("invalid_hook_condition", "No valid operator in condition")
    return HookConditionGroup(tuple(conditions), logic)


@dataclass(frozen=True)
class HookAction:
    type: str
    command: str = ""
    message: str = ""
    timeout: float = 30


@dataclass(frozen=True)
class Hook:
    id: str
    event: str
    action: HookAction
    condition: HookConditionGroup | None = None
    reject: bool = False
    once: bool = False
    source: str = "user"


@dataclass(frozen=True)
class HookContext:
    session_id: str
    run_id: str
    workspace: str
    event_name: str
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""

    def fields(self):
        return {
            "tool": self.tool_name,
            "event": self.event_name,
            "file": self.file_path,
            "message": self.message,
            "error": self.error,
            **{"args." + key: str(value) if value else "" for key, value in self.tool_args.items()},
        }

    def expand(self, template):
        values = {
            "EVENT": self.event_name,
            "TOOL_NAME": self.tool_name,
            "FILE_PATH": self.file_path,
            "MESSAGE": self.message,
            "ERROR": self.error,
            **{"TOOL_ARGS." + key: str(value) for key, value in self.tool_args.items()},
        }
        pattern = r"\$(TOOL_ARGS\.[A-Za-z0-9_-]+|EVENT|TOOL_NAME|FILE_PATH|MESSAGE|ERROR)\b"
        return re.sub(pattern, lambda match: values.get(match.group(1), match.group(0)), template)
