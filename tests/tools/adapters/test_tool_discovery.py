"""Unified discovery must disclose schemas only after a successful search."""

from dataclasses import replace
from types import SimpleNamespace

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

from pycode.kernel import Message, ToolContext
from pycode.tools.adapters.discovery import ToolDiscovery
from pycode.tools.adapters.framework_tool import make_framework_tool
from tests.agent.adapters.test_agent import assemble, tool_response
from tests.support.model import ScriptedChatModel


def setup_request():
    _, context, _, tool, _, _ = assemble([])
    discovery = ToolDiscovery(make_framework_tool)
    native = make_framework_tool(tool.spec, context)
    request = ModelRequest(
        model=ScriptedChatModel(responses=[]),
        messages=[HumanMessage(content="inspect")],
        tools=[native],
        state={"turn": 1},
        runtime=SimpleNamespace(context=context),
    )
    return discovery, request, context, tool


async def test_catalog_contains_brief_names_but_no_parameter_schema_then_loads():
    discovery, request, context, tool = setup_request()
    first = await discovery.disclose(request)
    assert [t.name for t in first.tools] == ["ToolSearch"]
    assert "echo:" in first.system_message.content
    assert '"properties"' not in first.system_message.content
    result = await discovery.execute(ToolContext("s", "r", "op", "/workspace"), {"query": "echo"})
    assert (
        result.structured_content["tools"][0]["input_schema"]["properties"]
        == tool.spec.input_schema["properties"]
    )
    # Executing search alone is insufficient: publication must complete.
    assert [t.name for t in (await discovery.disclose(request)).tools] == ["ToolSearch"]
    context.completed.append(Message("result", "tool", tool_result=result))
    assert [t.name for t in (await discovery.disclose(request)).tools] == ["ToolSearch", "echo"]


async def test_stale_definition_and_permission_removed_tool_are_not_disclosed():
    discovery, request, context, _ = setup_request()
    await discovery.disclose(request)
    result = await discovery.execute(ToolContext("s", "r", "op", "/workspace"), {"query": "echo"})
    context.completed.append(Message("result", "tool", tool_result=result))
    changed = request.tools[0].model_copy(update={"description": "changed definition"})
    assert len((await discovery.disclose(request.override(tools=[changed]))).tools) == 1
    assert len((await discovery.disclose(request.override(tools=[]))).tools) == 1
    assert discovery.catalog == {}


async def test_local_and_mcp_share_search_and_empty_results_do_not_unlock_tools():
    discovery, request, context, tool = setup_request()
    from tests.support.probe import Probe

    remote = Probe("mcp__database__query")
    remote.spec = replace(remote.spec, source="mcp:database", description="Query database")
    context.registry.register(remote)
    request = request.override(tools=[*request.tools, make_framework_tool(remote.spec, context)])
    first = await discovery.disclose(request)
    assert "MCP / database" in first.system_message.content
    assert "Local / builtin" in first.system_message.content
    result = await discovery.execute(
        ToolContext("s", "r", "op", "/workspace"), {"query": remote.spec.name}
    )
    assert [item["name"] for item in result.structured_content["tools"]] == [remote.spec.name]
    empty = await discovery.execute(
        ToolContext("s", "r", "op", "/workspace"), {"query": "nonexistent"}
    )
    assert empty.structured_content == {"tools": []}


async def test_real_agent_search_then_execute_without_selection_model():
    search = AIMessage(
        content="", tool_calls=[{"id": "search", "name": "ToolSearch", "args": {"query": "echo"}}]
    )
    runtime, context, model, tool, journal, _ = assemble(
        [search, tool_response(), AIMessage(content="done")]
    )
    result = await runtime.run((Message("u", "user", "inspect"),), context)
    assert result.outcome == "completed"
    assert tool.executed
    results = [m.tool_result for m in journal.messages if m.tool_result]
    assert [r.name for r in results] == ["ToolSearch", "echo"]
    assert not any(r.is_error for r in results)
    assert model.call_index == 3


async def test_call_without_search_is_rejected_even_if_name_is_in_catalog():
    runtime, context, _, tool, journal, _ = assemble([tool_response(), AIMessage(content="done")])
    await runtime.run((Message("u", "user", "inspect"),), context)
    assert tool.executed == []
    results = [m.tool_result for m in journal.messages if m.tool_result]
    assert results[0].error_code == "tool_not_declared"


async def test_failed_search_record_does_not_load_and_same_batch_cannot_call_target():
    discovery, request, context, _ = setup_request()
    await discovery.disclose(request)
    found = await discovery.execute(ToolContext("s", "r", "op", "/workspace"), {"query": "echo"})
    context.completed.append(Message("failed", "tool", tool_result=replace(found, is_error=True)))
    assert [tool.name for tool in (await discovery.disclose(request)).tools] == ["ToolSearch"]
    batch = tool_response()
    batch.tool_calls.insert(0, {"id": "search", "name": "ToolSearch", "args": {"query": "echo"}})
    runtime, context, _, tool, journal, _ = assemble([batch, AIMessage(content="done")])
    await runtime.run((Message("u", "user", "inspect"),), context)
    assert tool.executed == []
    results = [m.tool_result for m in journal.messages if m.tool_result]
    assert next(r for r in results if r.name == "echo").error_code == "tool_not_declared"


async def test_readonly_catalog_filters_write_tools_before_search():
    from pycode.agent.adapters.boundaries import filter_model_tools
    from tests.support.probe import Probe

    discovery, request, context, _ = setup_request()
    write = Probe("write_external")
    write.spec = replace(write.spec, category="external")
    context.registry.register(write)
    context.read_only = True
    request = request.override(tools=[*request.tools, make_framework_tool(write.spec, context)])
    first = await discovery.disclose(await filter_model_tools(request))
    assert "write_external" not in first.system_message.content
    result = await discovery.execute(
        ToolContext("s", "r", "op", "/workspace"), {"query": "write_external"}
    )
    assert result.structured_content == {"tools": []}


async def test_common_tools_are_preloaded_but_permissions_still_filter_them():
    from pycode.agent.adapters.boundaries import filter_model_tools
    from tests.support.probe import Probe

    discovery, request, context, _ = setup_request()
    bash = Probe("Bash")
    bash.spec = replace(bash.spec, category="external", description="Execute a shell command")
    read = Probe("read_file")
    for tool in (bash, read):
        context.registry.register(tool)
    request = request.override(
        tools=[*request.tools, *(make_framework_tool(t.spec, context) for t in (bash, read))]
    )
    disclosed = await discovery.disclose(request)
    assert {tool.name for tool in disclosed.tools} == {"ToolSearch", "Bash", "read_file"}
    assert "echo" in disclosed.system_message.content
    assert "echo" not in {tool.name for tool in disclosed.tools}
    context.read_only = True
    disclosed = await discovery.disclose(await filter_model_tools(request))
    assert {tool.name for tool in disclosed.tools} == {"ToolSearch", "read_file"}
    assert "Bash" not in disclosed.system_message.content
