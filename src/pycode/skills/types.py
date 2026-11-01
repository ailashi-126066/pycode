"""Skills types: component-owned implementation."""

from dataclasses import dataclass
from typing import Literal

from pycode.kernel import ToolError


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    source: str
    path: str
    content_hash: str
    mode: Literal["inline"] = "inline"
    allowed_tools: tuple[str, ...] = ()
    context: Literal["full", "recent", "none"] = "full"
    model: str | None = None
    resource_hash: str | None = None
    # Pi: disable-model-invocation; only the /skill command loads such a skill.
    disable_model_invocation: bool = False


@dataclass(frozen=True)
class SkillDocument:
    entry: SkillEntry
    raw: str
    body: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillIndex:
    entries: tuple[SkillEntry, ...]
    issues: tuple[str, ...] = ()
    # Later skills whose name was already taken.
    shadowed: tuple[SkillEntry, ...] = ()


def skill_entry(data):
    return SkillEntry(
        data["name"],
        data["description"],
        data["source"],
        data["path"],
        data["content_hash"],
        data["mode"],
        tuple(data["allowed_tools"]),
        data["context"],
        data["model"],
        data.get("resource_hash"),
    )


# 11.1 section 33.1 #8: a skill is loaded by a successful read of its SKILL.md; the result
# details carry the entry (Pi treats it as an ordinary read result).
def active_skills(path):
    active = {}
    for entry in path:
        if entry.type == "fork_context":
            active = {}
        result = entry.message.tool_result if entry.message is not None else None
        if result is not None and not result.is_error and "skill" in result.details:
            active[result.details["skill"]["name"]] = result.details["skill"]
    return tuple(active[name] for name in sorted(active))


def skill_scope(path):
    allowed = None
    for item in active_skills(path):
        declared = item.get("allowed_tools", ())
        if declared:
            allowed = set(declared) if allowed is None else allowed.intersection(declared)
    return allowed


@dataclass
class SkillSelection:
    dispatch: bool
    allowed_paths: tuple[str, ...]
    paths: tuple[str, ...] = ()
    revision: int = 0
    # SKILL.md paths kept out of SkillsMiddleware's listing.
    hidden: tuple[str, ...] = ()

    def select(self, paths):
        selected = []
        for path in paths:
            if path not in self.allowed_paths:
                raise ToolError(
                    "skill_catalog_changed", "Start a new run to use newly installed Skills"
                )
            if path not in selected:
                selected.append(path)
        selected = selected[:20]
        current = tuple(selected)
        if current != self.paths:
            self.paths = current
            self.revision += 1

    def visible(self, directory):
        prefix = directory.rstrip("/") + "/"
        if prefix + "SKILL.md" in self.hidden:
            return False
        if not self.dispatch:
            return True
        for path in self.paths:
            if path.startswith(prefix):
                return True
        return False
