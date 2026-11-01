# Pi 没有，pycode 新增；参照 MewCode local tools/load_skill.py
# SHA-256: 2eb89e486d11167ceae3a18b973accecf12658bcb60c593ea7ea47641c682f52
# Adaptation: the Skills service instead of mutable Agent/loader fields and private dispatch.
"""SkillSearch, InstallSkill and SaveSkill."""

from typing import TYPE_CHECKING

from pycode.kernel import ToolResult, ToolSpec, json_dumps

if TYPE_CHECKING:
    from pycode.skills.skills import Skills


class SkillSearch:
    spec = ToolSpec(
        "SkillSearch",
        "Search Skill names and descriptions without loading their bodies.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        category="read",
        execution_mode="parallel",
    )

    def __init__(self, skills: "Skills"):
        self._skills = skills

    async def execute(self, context, arguments):
        found = await self._skills.search(arguments["query"], arguments.get("limit", 10))
        return ToolResult(context.call_id, self.spec.name, json_dumps(found, ensure_ascii=False))


class InstallSkill:
    spec = ToolSpec(
        "InstallSkill",
        "Install a GitHub Skill and its resources, keeping previous versions; "
        "does not activate it.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "maxLength": 4096},
                "ref": {"type": "string", "maxLength": 255},
                "subpath": {"type": "string", "maxLength": 1024},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        category="external",
        execution_mode="sequential",
    )

    def __init__(self, skills: "Skills"):
        self._skills = skills

    async def execute(self, context, arguments):
        report = await self._skills.install(
            arguments["url"], ref=arguments.get("ref"), subpath=arguments.get("subpath")
        )
        return ToolResult(context.call_id, self.spec.name, json_dumps(report, ensure_ascii=False))


class SaveSkill:
    """Author local definitions through the same approval boundary as installation."""

    spec = ToolSpec(
        "SaveSkill",
        "Create a local SKILL.md, or overwrite an existing one with replace. "
        "Content must include name and description YAML frontmatter. Does not run or activate it.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 64},
                "content": {"type": "string", "maxLength": 262144},
                "scope": {"type": "string", "enum": ["user", "project"], "default": "user"},
                "replace": {"type": "boolean", "default": False},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
        category="external",
        execution_mode="sequential",
    )

    def __init__(self, skills: "Skills"):
        self._skills = skills

    async def execute(self, context, arguments):
        name, content = arguments["name"], arguments["content"]
        scope = arguments.get("scope", "user")
        if arguments.get("replace", False):
            report = await self._skills.update(name, content, scope=scope)
        else:
            report = await self._skills.create(name, content, scope=scope)
        return ToolResult(context.call_id, self.spec.name, json_dumps(report, ensure_ascii=False))
