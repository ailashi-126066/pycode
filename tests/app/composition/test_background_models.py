"""Background memory models always run with thinking off and one owned model lifetime."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.support.execution import provider

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.memory.adapters.background import ConfiguredMemoryUpdater
from pycode.models.module import ModelsModule


def metered_facade(owner, prefix, **options):
    return ModelsModule.metered(owner, logical_prefix=prefix, **options)


async def unused_owner(*args):
    raise AssertionError("This configuration check must not create a model")


def test_memory_updater_forces_thinking_off_for_forced_tool_choice():
    configured = replace(
        provider("http://127.0.0.1:9"),
        implementation="deepseek",
        supports_thinking=True,
        thinking={"type": "enabled"},
        reasoning_effort="high",
        max_output_tokens=8192,
    )
    updater = ConfiguredMemoryUpdater(configured, unused_owner, metered_facade)
    assert updater.provider.thinking == {} and updater.provider.reasoning_effort is None
    assert updater.provider.max_output_tokens == 4096
    owner = SimpleNamespace(model=object(), requests=object())
    model = updater.facade(owner, "memory_manager")
    assert model.logical_prefix == "memory_manager"


async def test_memory_model_factory_disables_request_retries(tmp_path, monkeypatch):
    policies = []

    async def factory(*args, **kwargs):
        policies.append(kwargs["retry_policy"])
        return None

    application = compose_application(AppConfig(tmp_path, tmp_path / "data"))
    monkeypatch.setattr(application._modules.models, "owner", factory)
    updater = application.memory_updater(provider("http://127.0.0.1:9"))
    await updater.model_factory(updater.provider, "s", "r", "memory_extract")
    assert len(policies) == 1 and policies[0].max_retries == 0


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
