"""Skills tools: component-owned implementation."""

# Source: MewCode local tools/load_skill.py; SHA-256:
# 2eb89e486d11167ceae3a18b973accecf12658bcb60c593ea7ea47641c682f52
# Adaptation: application port instead of mutable Agent/loader fields and private dispatch.

from pycode.kernel import ToolResult, ToolSpec, json_dumps
from pycode.skills.contracts import SkillCatalogCommands


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

    def __init__(self, skills: SkillCatalogCommands):
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

    def __init__(self, skills: SkillCatalogCommands):
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
        "Create a local SKILL.md, or update it with the SHA-256 revision from SkillSearch. "
        "Content must include name and description YAML frontmatter. Does not run or activate it.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 64},
                "content": {"type": "string", "maxLength": 262144},
                "scope": {"type": "string", "enum": ["user", "project"], "default": "user"},
                "expected_revision": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
        category="external",
        execution_mode="sequential",
    )

    def __init__(self, skills: SkillCatalogCommands):
        self._skills = skills

    async def execute(self, context, arguments):
        name, content = arguments["name"], arguments["content"]
        scope = arguments.get("scope", "user")
        if "expected_revision" in arguments:
            report = await self._skills.update(
                name, content, scope=scope, expected_revision=arguments["expected_revision"]
            )
        else:
            report = await self._skills.create(name, content, scope=scope)
        return ToolResult(context.call_id, self.spec.name, json_dumps(report, ensure_ascii=False))
