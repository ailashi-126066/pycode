# Pi 没有，pycode 新增
"""Record effective tools and system-prompt budget; never compact or rebuild messages."""

from ..kernel import (
    EntryDraft,
    SessionTree,
    ToolSpec,
    data_hash,
    estimate_text,
    plain_data,
)
from .context import RunContext


def model_tool_specs(tools, context: RunContext) -> tuple[ToolSpec, ...]:
    specs = []
    for tool in tools:
        if isinstance(tool, dict):
            function = tool.get("function", tool)
            specs.append(
                ToolSpec(
                    function.get("name", tool.get("type", "provider_tool")),
                    function.get("description", "Provider tool"),
                    function.get("parameters", {}),
                    source="provider",
                )
            )
            continue
        # Request preparation already removed unregistered framework tools.
        specs.append(context.registry.get_spec(tool.name))
    specs.sort(key=lambda spec: spec.name)
    return tuple(specs)


async def calibrate_model_request(request):
    context: RunContext = request.runtime.context
    actual_tools = model_tool_specs(request.tools, context)
    # Only fully disclosed definitions authorize execution; catalog names never count.
    context.disclosed_tool_names = tuple(spec.name for spec in actual_tools)
    system = request.system_message.content if request.system_message is not None else ""
    system_hash = data_hash(system)
    if actual_tools != context.model_tools or system_hash != context.system_hash:
        context.model_tools = actual_tools
        context.system_hash = system_hash
        history = context.history
        snapshot = await history.snapshot()
        previous = None
        for entry in reversed(SessionTree(snapshot.index).path(snapshot.leaf_id)):
            if entry.type == "tool_set_change" and entry.data.get("source") == "effective_model":
                previous = entry
                break
        digest = data_hash({"tools": plain_data(actual_tools), "system_hash": system_hash})
        if previous is None or previous.data["hash"] != digest:
            await history.append_entry(
                EntryDraft(
                    "tool_set_change",
                    data={
                        "source": "effective_model",
                        "hash": digest,
                        "tools": plain_data(actual_tools),
                        "harness_system_hash": system_hash,
                        "harness_system_tokens": estimate_text(
                            system if isinstance(system, str) else str(system)
                        ),
                    },
                    run_id=context.run_id,
                )
            )
    return request
