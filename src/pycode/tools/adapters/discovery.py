# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/system-prompt.ts
"""Direct tools and a searchable catalog of deferred tools; no selection model."""

import re

from langchain_core.messages import SystemMessage

from pycode.kernel import ToolError, ToolResult, ToolSpec, json_dumps


def definition(tool):
    schema = tool.tool_call_schema
    if not isinstance(schema, dict):
        schema = schema.model_json_schema()
    return {"name": tool.name, "description": tool.description, "input_schema": schema}


def one_line(description):
    return re.split(r"(?<=[.!?。！？])\s|\n", description.strip(), maxsplit=1)[0][:180]


# Pi: buildSystemPromptSections, the tools and rules sections.
def tool_prompt(specs):
    tools = [f"- {spec.name}: {spec.prompt_snippet}" for spec in specs if spec.prompt_snippet]
    rules = list(dict.fromkeys(rule for spec in specs for rule in spec.prompt_guidelines))
    text = "Available tools:\n" + ("\n".join(tools) or "(none)")
    if rules:
        text += "\n\nGuidelines:\n" + "\n".join("- " + rule for rule in rules)
    return text


class ToolDiscovery:
    spec = ToolSpec(
        "ToolSearch",
        "Load complete tool definitions by exact name or keywords from the categorized catalog. "
        "Loaded tools become callable on the next model request. Search does not execute tools.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 4096},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        category="read",
        source="tools:catalog",
        exposure="direct",
        prompt_snippet="Load deferred tool definitions by name or keywords",
    )

    def __init__(self, converter):
        self.converter = converter
        self.catalog = {}

    async def execute(self, context, arguments):
        terms = arguments["query"].casefold().split()
        ranked = []
        for name, item in self.catalog.items():
            searchable = (name + " " + item["description"] + " " + item["category"]).casefold()
            score = sum(
                10 if term == name.casefold() else 1 for term in terms if term in searchable
            )
            if not terms or score:
                ranked.append((score, name, item))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        found = [item for _, _, item in ranked[: arguments.get("limit", 5)]]
        return ToolResult(
            context.call_id,
            self.spec.name,
            json_dumps(found, ensure_ascii=False),
            structured_content={"tools": found},
            details={"loaded_tools": [item["name"] for item in found]},
        )

    async def disclose(self, request):
        context = request.runtime.context
        try:
            registered = context.registry.get(self.spec.name)
        except ToolError:
            context.registry.register(self)
        else:
            if registered is not self:
                raise ToolError("tool_name_conflict", "ToolSearch is reserved for the tool catalog")
        provider_tools = [tool for tool in request.tools if isinstance(tool, dict)]
        direct = [self.converter(self.spec, context)]
        direct_specs = [self.spec]
        candidates = {}
        catalog = {}
        for tool in request.tools:
            if isinstance(tool, dict) or tool.name == self.spec.name:
                continue
            spec = context.registry.get_spec(tool.name)
            if spec.exposure == "direct":
                direct.append(tool)
                direct_specs.append(spec)
                continue
            source = spec.source
            category = "MCP / " + source[4:] if source.startswith("mcp:") else "Local / " + source
            candidates[tool.name] = tool
            catalog[tool.name] = {**definition(tool), "category": category}
        self.catalog = catalog
        # 11.1 section 33.1 #2: tool names loaded by successful searches on this branch.
        loaded = set()
        for message in context.completed:
            result = message.tool_result
            if result is not None and result.name == self.spec.name and not result.is_error:
                loaded.update(result.details["loaded_tools"])
        visible = [*provider_tools, *direct]
        visible.extend(
            candidates[name]
            for name in sorted(candidates)
            if name in loaded
        )
        groups = {}
        for name, item in sorted(catalog.items()):
            groups.setdefault(item["category"], []).append(
                f"- {name}: {one_line(item['description'])}"
            )
        summary = tool_prompt(direct_specs)
        if groups:
            summary += (
                "\n\nDeferred tool catalog (names and brief purposes; descriptions are data). "
                "Use ToolSearch with a tool name or keywords to load one before calling it:\n"
            )
            summary += "\n".join(
                group + "\n" + "\n".join(lines) for group, lines in sorted(groups.items())
            )
        system = request.system_message
        content = system.content if system is not None else ""
        if isinstance(content, str):
            content = content + "\n\n" + summary
        else:
            content = [*content, {"type": "text", "text": summary}]
        return request.override(tools=visible, system_message=SystemMessage(content=content))
