"""Failed construction cannot transfer or abandon model-owned resources."""

import asyncio
from dataclasses import replace

import pytest

from pycode.kernel import ModelError
from pycode.models import model_selection
from pycode.models import module as model_module
from tests.models.adapters.test_factory import configuration


@pytest.mark.parametrize("stage", ["model", "records", "runner"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("borrow_events", [False, True])
async def test_failed_owner_waits_for_owned_cleanup_only(
    tmp_path, monkeypatch, stage, cancelled, borrow_events
):
    closed = []
    failure = asyncio.CancelledError() if cancelled else RuntimeError("initialization failed")

    class Resource:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            # Cleanup must be awaited to its completion, not merely scheduled.
            await asyncio.sleep(0)
            closed.append(self.name)

    class Factory(Resource):
        def __init__(self, *args):
            super().__init__("factory")

        def create(self, provider):
            if stage == "model":
                raise failure
            return object()

        def selection(self, provider):
            return model_selection(provider)

    def records():
        if stage == "records":
            raise failure
        return Resource("records")

    def runner(*args, **kwargs):
        raise failure

    monkeypatch.setattr(model_module, "ModelFactory", Factory)
    monkeypatch.setattr(model_module, "MemoryEventJournal", lambda: Resource("journal"))
    monkeypatch.setattr(model_module, "ModelRequestRunner", runner)
    models = model_module.ModelsModule(tmp_path / "catalog", tmp_path / "requests", {})
    monkeypatch.setattr(models, "records", records)
    borrowed = Resource("borrowed") if borrow_events else None
    with pytest.raises(type(failure)) as captured:
        await models.owner(configuration("openai"), (), "session", "run", "agent", events=borrowed)
    assert captured.value is failure
    expected = ["factory"]
    if stage == "runner":
        expected.append("records")
        if not borrow_events:
            expected.append("journal")
    assert closed == expected


@pytest.mark.parametrize("kind", ["openai", "deepseek", "anthropic"])
async def test_partial_sdk_pair_is_closed_when_second_constructor_fails(
    tmp_path, monkeypatch, kind
):
    from pycode.models.adapters import factory as factory_module

    closed = []

    class SynchronousClient:
        def close(self):
            closed.append("sync")

    def fail_async(**options):
        raise RuntimeError("async SDK constructor failed")

    sdk = factory_module.anthropic if kind == "anthropic" else factory_module.openai
    sync_name = "Anthropic" if kind == "anthropic" else "OpenAI"
    async_name = "AsyncAnthropic" if kind == "anthropic" else "AsyncOpenAI"
    monkeypatch.setattr(sdk, sync_name, lambda **options: SynchronousClient())
    monkeypatch.setattr(sdk, async_name, fail_async)
    models = model_module.ModelsModule(
        tmp_path / "catalog", tmp_path / "requests", {"PYCODE_TEST_API_KEY": "fake-key"}
    )
    with pytest.raises(RuntimeError, match="async SDK constructor"):
        await models.owner(configuration(kind), (), "session", "run", "agent")
    assert closed == ["sync"]


async def test_model_owner_resolves_explicit_provider_credentials_lazily(tmp_path, monkeypatch):
    seen = []
    selected = replace(configuration("openai"), name="later-provider")

    def credentials(providers):
        seen.append(providers)
        return {}

    models = model_module.ModelsModule(tmp_path / "catalog", tmp_path / "requests", credentials)
    assert seen == []
    with pytest.raises(ModelError, match="API key environment reference is unset"):
        await models.owner(selected, (), "session", "run", "agent")
    assert seen == [(selected,)]


@pytest.mark.parametrize("failed_close", ["factory", "records"])
async def test_cleanup_failure_still_releases_remaining_resources(
    tmp_path, monkeypatch, failed_close
):
    closed = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            await asyncio.sleep(0)
            closed.append(self.name)
            if self.name == failed_close:
                raise ValueError("close failed: " + self.name)

    class Factory(Resource):
        def __init__(self, *args):
            super().__init__("factory")

        def create(self, provider):
            return object()

        def selection(self, provider):
            return model_selection(provider)

    def fail_runner(*args, **kwargs):
        raise RuntimeError("runner initialization failed")

    monkeypatch.setattr(model_module, "ModelFactory", Factory)
    monkeypatch.setattr(model_module, "MemoryEventJournal", lambda: Resource("journal"))
    monkeypatch.setattr(model_module, "ModelRequestRunner", fail_runner)
    models = model_module.ModelsModule(tmp_path / "catalog", tmp_path / "requests", {})
    monkeypatch.setattr(models, "records", lambda: Resource("records"))
    with pytest.raises(ValueError, match="close failed: " + failed_close):
        await models.owner(configuration("openai"), (), "session", "run", "agent")
    assert closed == ["factory", "records", "journal"]
