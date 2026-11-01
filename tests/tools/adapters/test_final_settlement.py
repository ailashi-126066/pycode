"""Framework-owned tool execution and pycode's narrow repeated-failure guard."""

import pytest
from langchain_core.messages import AIMessage

from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.messages import Message, ToolResult
from tests.agent.adapters.test_agent import assemble


def response(identifier, value="same", name="echo"):
    return AIMessage(
        id="assistant-" + identifier,
        content="",
        tool_calls=[{"id": identifier, "name": name, "args": {"value": value}}],
    )


async def test_fourth_and_later_identical_failure_is_paired_but_never_executed():
    responses = [response(str(index)) for index in range(6)]
    responses.append(AIMessage(id="final", content="changed approach"))
    runtime, context, _, tool, journal, _ = assemble(responses)

    class AlwaysFails:
        spec = tool.spec
        executed = 0

        async def execute(self, tool_context, arguments):
            self.executed += 1
            raise ToolError("stable_failure", "bad input")

    failing = AlwaysFails()
    context.registry.replace_source("builtin", (failing,))
    result = await runtime.run((Message("u1", "user", "work"),), context)
    assert result.outcome == "completed"
    assert failing.executed == 3
    results = [message.tool_result for message in journal.messages if message.tool_result]
    assert [result.error_code for result in results] == ["stable_failure"] * 3 + [
        "repeated_tool_failure"
    ] * 3
    assert all(result.details["executed"] is False for result in results[3:])
    assert [result.call_id for result in results] == [str(index) for index in range(6)]


async def test_parallel_identical_calls_stop_at_three_actual_failures():
    replies = [
        response("prior"),
        AIMessage(
            id="batch",
            content="",
            tool_calls=[
                {"id": str(index), "name": "echo", "args": {"value": "same"}} for index in range(5)
            ],
        ),
        AIMessage(id="done", content="choose another approach"),
    ]
    runtime, context, _, tool, journal, _ = assemble(replies)

    class Failure:
        spec = tool.spec
        count = 0

        async def execute(self, current, arguments):
            self.count += 1
            raise ToolError("stable_failure", "bad input")

    failing = Failure()
    context.registry.replace_source("builtin", (failing,))
    await runtime.run((Message("u", "user", "work"),), context)
    assert failing.count == 3
    results = [message.tool_result for message in journal.messages if message.tool_result]
    assert [result.error_code for result in results] == ["stable_failure"] * 3 + [
        "repeated_tool_failure"
    ] * 3


@pytest.mark.parametrize("change", ["arguments", "error", "success"])
async def test_real_changes_reset_the_narrow_failure_counter(change):
    responses = [response(str(index)) for index in range(4)]
    if change == "arguments":
        responses[-1] = response("3", "new input")
    responses.append(AIMessage(id="final", content="done"))
    runtime, context, _, tool, journal, _ = assemble(responses)

    class SometimesFails:
        spec = tool.spec
        executed = 0

        async def execute(self, tool_context, arguments):
            self.executed += 1
            if self.executed == 3 and change == "success":
                return ToolResult("", "", "success")
            code = (
                "changed_failure" if self.executed == 3 and change == "error" else "stable_failure"
            )
            raise ToolError(code, "failed")

    failing = SometimesFails()
    context.registry.replace_source("builtin", (failing,))
    await runtime.run((Message("u1", "user", "work"),), context)
    assert failing.executed == 4
    assert not any(
        message.tool_result.error_code == "repeated_tool_failure"
        for message in journal.messages
        if message.tool_result
    )


@pytest.mark.parametrize("case", ["unknown", "malformed"])
async def test_unknown_and_malformed_calls_have_one_error_pair_and_no_effect(case):
    reply = response("c1", name="unknown" if case == "unknown" else "echo")
    if case == "malformed":
        reply = AIMessage(
            id="bad",
            content="",
            invalid_tool_calls=[
                {"id": "c1", "name": "echo", "args": "{bad", "error": "Malformed JSON"}
            ],
        )
    runtime, context, _, tool, journal, _ = assemble([reply, AIMessage(id="done", content="fixed")])
    result = await runtime.run((Message("u1", "user", "work"),), context)
    assert result.outcome == "completed" and result.final_text == "fixed"
    assert tool.executed == []
    results = [message.tool_result for message in journal.messages if message.tool_result]
    assert len(results) == 1 and results[0].call_id == "c1"
    assert results[0].error_code == ("unknown_tool" if case == "unknown" else "invalid_arguments")


async def test_native_tool_retry_retries_only_declared_temporary_read_errors():
    runtime, context, _, tool, journal, _ = assemble(
        [response("c1"), AIMessage(id="done", content="fixed")]
    )

    class TemporaryRead:
        spec = tool.spec
        attempts = 0

        async def execute(self, tool_context, arguments):
            self.attempts += 1
            if self.attempts == 1:
                raise ToolError("temporary_read_failure", "temporary", retryable=True)
            return ToolResult("", "", "read completed")

    reader = TemporaryRead()
    context.registry.replace_source("builtin", (reader,))
    result = await runtime.run((Message("u1", "user", "read"),), context)
    assert result.outcome == "completed" and reader.attempts == 2
    results = [message.tool_result for message in journal.messages if message.tool_result]
    assert len(results) == 1 and results[0].text == "read completed"


@pytest.mark.parametrize("category", ["read", "write", "external"])
# 11.1 section 33.1 #2: an undeclared name is rejected before validation, even invalid arguments.
@pytest.mark.parametrize("value", ["same", 3])
async def test_known_tool_hidden_from_final_request_never_prepares_approves_or_executes(
    category, value
):
    from dataclasses import replace

    runtime, context, _, tool, journal, _ = assemble(
        [response("hidden", value=value), AIMessage(id="done", content="choose an available tool")]
    )
    tool.spec = replace(tool.spec, category=category)
    context.registry.replace_source("builtin", (tool,))

    class Gate:
        async def authorize(self, current, spec, call):
            raise AssertionError("Undeclared tool must not request approval")

    async def hide(request):
        return request.override(tools=[item for item in request.tools if item.name != "echo"])

    class Extension:
        prepared = 0
        validated = 0

        def lookup(self, name, *, state):
            return None

        async def validate_call(self, current, call):
            self.validated += 1

        async def before_call(self, current, call):
            self.prepared += 1

        def prepare_result(self, call, result):
            return result

        async def after_persist(self, current, call, result, entry_id):
            return None

    extension = Extension()
    original_factory = runtime.bindings.settlement_middleware_factory

    def factory(current):
        middleware = original_factory(current)
        middleware.tool_extensions = (extension,)
        return middleware

    context.executor.gate = Gate()
    runtime.bindings = replace(
        runtime.bindings, model_filters=(hide,), settlement_middleware_factory=factory
    )
    result = await runtime.run((Message("u1", "user", "work"),), context)
    assert result.outcome == "completed" and tool.executed == []
    outputs = [message.require_tool_result() for message in journal.messages if message.tool_result]
    assert len(outputs) == 1 and outputs[0].error_code == "tool_not_declared"
    assert outputs[0].details["executed"] is False
    assert extension.prepared == extension.validated == 0


async def test_schema_error_is_redacted_in_result_history_and_complete_output(tmp_path):
    from dataclasses import replace

    from pycode.kernel import SessionHeader, json_dumps
    from pycode.session.adapters.storage.session import SqliteSessionStore
    from tests.support.requests import TestTiming

    secret = "api_key=private-secret-key sk-abcdefghijklmnop12345678 "
    runtime, context, _, tool, _, _ = assemble(
        [
            response("hidden-secret", value=secret + "x" * 4000),
            AIMessage(id="done", content="fixed"),
        ]
    )
    tool.spec = replace(
        tool.spec,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )
    context.registry.replace_source("builtin", (tool,))
    sessions = tmp_path / "sessions"
    journal = SqliteSessionStore(
        sessions / context.session_id,
        SessionHeader(context.session_id, context.workspace, "created"),
        TestTiming(),
    )
    context.journal = journal
    try:
        result = await runtime.run((Message("u1", "user", "work"),), context)
        assert result.outcome == "completed" and tool.executed == []
        saved = [
            entry.require_message().require_tool_result()
            for entry in (await journal.snapshot()).entries
            if entry.message is not None and entry.message.tool_result is not None
        ]
        assert len(saved) == 1 and saved[0].error_code == "invalid_arguments"
        assert saved[0].details["executed"] is False and len(saved[0].text) == 1600
        assert "[redacted]" in saved[0].text
        assert "private-secret-key" not in json_dumps(saved[0])
        assert "sk-abcdefghijklmnop12345678" not in json_dumps(saved[0])
    finally:
        await journal.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
