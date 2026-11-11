"""Runtime options have one carrier; checkpoint selectors stay configurable."""

from copy import deepcopy

import pytest

from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config


@pytest.fixture(autouse=True)
def application_config():
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    yield
    reset_app_config()


def test_http_options_reach_runtime_without_entering_checkpoint_config():
    from app.gateway.services import build_run_config, merge_run_context_overrides
    from deerflow.runtime.runs.worker import _build_runtime_context

    options = {"model_name": "chosen", "thinking_enabled": False, "is_plan_mode": True, "subagent_enabled": False, "reasoning_effort": "medium"}
    config = build_run_config("thread-1", None, None)
    merge_run_context_overrides(config, options)
    runtime = _build_runtime_context("thread-1", "run-1", config["context"])
    assert all(runtime[key] == value for key, value in options.items())
    assert config["configurable"] == {"thread_id": "thread-1"}


def test_configurable_cannot_supply_legacy_runtime_options():
    from app.gateway.services import build_run_config
    from deerflow.agents.lead_agent.agent import _get_runtime_config

    request = {"configurable": {"model_name": "legacy", "agent_name": "legacy-agent", "is_plan_mode": True, "checkpoint_ns": "", "checkpoint_id": "cp-1"}}
    original = deepcopy(request)
    config = build_run_config("thread-1", request, None, assistant_id="current-agent")
    assert config["context"]["agent_name"] == "current-agent"
    assert "model_name" not in config["context"]
    assert config["configurable"] == {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "cp-1"}
    assert _get_runtime_config({"configurable": {"model_name": "legacy", "is_plan_mode": True}}) == {}
    assert request == original


def test_runtime_context_and_checkpoint_selectors_can_coexist():
    from app.gateway.services import build_run_config, merge_run_context_overrides

    config = build_run_config("thread-1", {"configurable": {"checkpoint_id": "cp-1"}, "context": {"model_name": "chosen"}}, None)
    merge_run_context_overrides(config, {"model_name": "other", "is_plan_mode": False})
    assert config["configurable"] == {"checkpoint_id": "cp-1", "thread_id": "thread-1"}
    assert config["context"]["model_name"] == "chosen"
    assert config["context"]["is_plan_mode"] is False


@pytest.mark.parametrize("internal", [False, True])
def test_internal_flags_remain_gated_and_never_enter_configurable(internal):
    from app.gateway.services import merge_run_context_overrides

    config = {"configurable": {"thread_id": "thread-1"}}
    merge_run_context_overrides(config, {"non_interactive": True, "github_token": "synthetic-token"}, internal=internal)
    assert config["configurable"] == {"thread_id": "thread-1"}
    assert (config["context"].get("non_interactive") is True) is internal
    assert (config["context"].get("github_token") == "synthetic-token") is internal


def test_interaction_policy_does_not_read_legacy_options():
    from deerflow.agents.interaction_policy import resolve_run_interaction_policy

    policy = resolve_run_interaction_policy({"configurable": {"non_interactive": True}})
    assert policy == resolve_run_interaction_policy({"context": {}})


def test_run_name_and_knowledge_identity_ignore_legacy_agent_name():
    from deerflow.knowledge.tools import _execution_agent_name
    from deerflow.runtime.runs.naming import resolve_root_run_name

    config = {"configurable": {"agent_name": "legacy"}, "context": {"agent_name": "current"}}
    assert resolve_root_run_name(config, "fallback") == "current"
    assert resolve_root_run_name({"configurable": {"agent_name": "legacy"}}, "fallback") == "fallback"
    assert _execution_agent_name({}, config) == "lead_agent"
    assert _execution_agent_name(config["context"], config) == "current"


def test_embedded_client_builds_options_in_context():
    from deerflow.client import DeerFlowClient

    client = DeerFlowClient.__new__(DeerFlowClient)
    client._model_name = "default"
    client._thinking_enabled = True
    client._plan_mode = False
    client._subagent_enabled = True
    config = client._get_runnable_config("thread-1", model_name="chosen", thinking_enabled=False, plan_mode=True, subagent_enabled=False)
    assert config["configurable"] == {"thread_id": "thread-1"}
    assert config["context"] == {"model_name": "chosen", "thinking_enabled": False, "is_plan_mode": True, "subagent_enabled": False}


def test_real_graph_tools_receive_context_and_checkpoints_keep_framework_selectors():
    from langchain.tools import ToolRuntime, tool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver

    from app.gateway.services import build_run_config, merge_run_context_overrides
    from deerflow.agents.factory import create_deerflow_agent

    observed = []

    @tool
    def inspect_options(runtime: ToolRuntime) -> str:
        """Return this run's selected model and Plan setting."""
        observed.append(dict(runtime.context))
        return f"{runtime.context['model_name']}:{runtime.context['is_plan_mode']}"

    class RecordingModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "offline-context-options"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if isinstance(messages[-1], ToolMessage):
                reply = AIMessage(content=messages[-1].content)
            else:
                reply = AIMessage(content="", tool_calls=[{"name": "inspect_options", "args": {}, "id": "call-1"}])
            return ChatResult(generations=[ChatGeneration(message=reply)])

    config = build_run_config("thread-graph", None, None)
    merge_run_context_overrides(config, {"model_name": "chosen", "is_plan_mode": True})
    graph = create_deerflow_agent(RecordingModel(), [inspect_options], middleware=[], checkpointer=InMemorySaver())
    result = graph.invoke({"messages": [HumanMessage(content="Inspect this run")]}, config=config, context=config["context"])
    assert result["messages"][-1].content == "chosen:True"
    assert observed == [config["context"]]
    checkpoint_config = graph.get_state(config).config["configurable"]
    assert checkpoint_config["thread_id"] == "thread-graph"
    assert "checkpoint_id" in checkpoint_config
    assert not {"model_name", "is_plan_mode"} & checkpoint_config.keys()
