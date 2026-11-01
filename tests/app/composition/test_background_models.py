"""Memory's model configuration and its owned lifetime use explicit functions."""

from dataclasses import replace
from types import SimpleNamespace

from tests.support.execution import provider
from tests.support.model import ScriptedChatModel

from pycode.app import AppConfig
from pycode.app.composition import background_model
from pycode.models import RetryPolicy


async def test_memory_background_model_disables_thinking_and_closes_owner(tmp_path, monkeypatch):
    configured = replace(
        provider("http://127.0.0.1:9"),
        implementation="deepseek",
        supports_thinking=True,
        thinking={"type": "enabled"},
        reasoning_effort="high",
        max_output_tokens=8192,
    )
    seen, closed = [], []

    async def close():
        closed.append(True)

    async def open_model(config, selected, *args, **kwargs):
        seen.append((selected, kwargs["retry_policy"]))
        return SimpleNamespace(
            model=ScriptedChatModel(responses=[]), requests=object(), aclose=close
        )

    monkeypatch.setattr("pycode.app.composition.open_model", open_model)
    policy = RetryPolicy(max_retries=0)
    async with background_model(
        AppConfig(tmp_path, tmp_path / "data"),
        configured,
        session_id="s",
        run_id="r",
        purpose="memory_extract",
        logical_prefix="memory_manager",
        retry_policy=policy,
    ) as model:
        assert model.logical_prefix == "memory_manager"
        assert seen[0][0].thinking == {} and seen[0][0].reasoning_effort is None
        assert seen[0][0].max_output_tokens == 4096
        assert seen[0][1] is policy
    assert closed == [True]
