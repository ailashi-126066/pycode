"""The local study checkout omits the three automatic content middleware layers."""

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.pii_redaction_config import PiiRedactionConfig

REMOVED = {"InputSanitizationMiddleware", "ToolResultSanitizationMiddleware", "PiiRedactionMiddleware"}


@pytest.mark.parametrize("include_uploads", [False, True], ids=["subagent-base", "lead-base"])
@pytest.mark.parametrize("pii_enabled", [False, True])
def test_shared_runtime_does_not_mount_removed_content_layers(include_uploads, pii_enabled):
    from deerflow.agents.middlewares.tool_error_handling_middleware import _build_runtime_middlewares

    config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}, "pii_redaction": {"enabled": pii_enabled, "token_secret": "synthetic-test-secret-not-a-real-key"}})
    chain = _build_runtime_middlewares(app_config=config, include_uploads=include_uploads, include_dangling_tool_call_patch=True)
    names = {type(m).__name__ for m in chain}
    assert not names & REMOVED
    assert {"KnowledgeScopeMiddleware", "ToolOutputBudgetMiddleware", "ThreadDataMiddleware", "SandboxMiddleware", "LLMErrorHandlingMiddleware"} <= names


@pytest.mark.parametrize("pii_enabled", [False, True])
def test_sdk_feature_builder_does_not_mount_pii_layer(pii_enabled):
    from deerflow.agents.factory import _assemble_from_features
    from deerflow.agents.features import RuntimeFeatures

    chain, _ = _assemble_from_features(RuntimeFeatures(), pii_redaction_config=PiiRedactionConfig(enabled=pii_enabled, token_secret="synthetic-test-secret-not-a-real-key"))
    names = {type(m).__name__ for m in chain}
    assert not names & REMOVED
    assert {"ToolErrorHandlingMiddleware", "DurableContextMiddleware", "ClarificationMiddleware"} <= names
