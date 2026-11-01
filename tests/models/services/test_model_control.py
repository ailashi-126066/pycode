from dataclasses import replace

import pytest

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import EntryDraft, EventStream, Message, ModelError, SessionHeader
from pycode.models.adapters.storage.catalog import FileCatalogStore
from pycode.models.services.model_catalog import ModelCatalog
from pycode.models.services.model_control import ModelControl
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.models.adapters.test_factory import configuration
from tests.models.services.test_model_catalog import FixedDirectory
from tests.support.requests import TestTiming


class Recorder:
    def __init__(self):
        self.events = []

    async def on_event(self, event):
        self.events.append(event)


def controller(tmp_path, providers, store, recorder=None):
    catalog = ModelCatalog(
        providers, FixedDirectory({}), FileCatalogStore(tmp_path / "catalog.json"), TestTiming()
    )
    events = EventStream("s1", "r1", (recorder or Recorder(),))
    return ModelControl(catalog, ContextProjector(), store, events)


def store_for(tmp_path):
    return SqliteSessionStore(
        tmp_path / "session", SessionHeader("s1", "/project", "created"), TestTiming()
    )


async def test_model_changes_and_branch_restoration_are_durable(tmp_path):
    providers = (configuration("deepseek"), configuration("openai"))
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "hello"))
    control = controller(tmp_path, providers, store)
    await control.list_models(refresh=False)
    first = await control.select(providers[0].name, providers[0].model_id)
    first_leaf = (await store.snapshot()).leaf_id
    second = await control.select(providers[1].name, providers[1].model_id)
    assert first != second
    raw = (await store.snapshot()).entries
    await store.aclose()
    restored = store_for(tmp_path)
    control = controller(tmp_path, providers, restored)
    await control.list_models(refresh=False)
    assert (await control.restore()).implementation == "openai"
    await restored.branch(first_leaf)
    assert (await control.restore()).implementation == "deepseek"
    assert (await restored.snapshot()).entries == raw
    await restored.aclose()


async def test_invalid_selection_does_not_change_history_or_fall_back(tmp_path):
    provider = configuration("deepseek")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "hello"))
    control = controller(tmp_path, (provider,), store)
    await control.list_models(refresh=False)
    before = await store.snapshot()
    with pytest.raises(ModelError) as error:
        await control.select(provider.name, "missing-model")
    assert error.value.code == "model_unavailable"
    assert await store.snapshot() == before
    await store.aclose()


async def test_reselecting_identical_model_does_not_append_duplicate_change(tmp_path):
    provider = configuration("openai")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "hello"))
    recorder = Recorder()
    control = controller(tmp_path, (provider,), store, recorder)
    await control.list_models(refresh=False)
    await control.select(provider.name, provider.model_id)
    before = await store.snapshot()
    await control.select(provider.name, provider.model_id)
    assert await store.snapshot() == before
    [event] = recorder.events
    assert event.kind == "model_change"
    assert event.data["entry_id"] == before.leaf_id
    assert event.data["selection"]["model_id"] == provider.model_id
    await store.aclose()


async def test_restore_uses_the_current_provider_configuration(tmp_path):
    provider = configuration("openai")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "hello"))
    control = controller(tmp_path, (provider,), store)
    await control.list_models(refresh=False)
    await control.select(provider.name, provider.model_id)
    changed = replace(provider, base_url="https://other.invalid")
    other = controller(tmp_path, (changed,), store)
    await other.list_models(refresh=False)
    assert (await other.restore()).base_url == changed.base_url
    await store.aclose()


async def test_thinking_level_restores_native_provider_effort(tmp_path):
    provider = replace(
        configuration("deepseek"), supports_thinking=True, thinking={"type": "enabled"}
    )
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "hello"))
    control = controller(tmp_path, (provider,), store)
    await control.list_models(refresh=False)
    await control.select(provider.name, provider.model_id)
    await store.append_entry(EntryDraft("thinking_level_change", data={"thinking": "high"}))
    restored = await control.restore()
    assert restored.thinking == {"type": "enabled"}
    assert restored.reasoning_effort == "high"
    await store.append_entry(EntryDraft("thinking_level_change", data={"thinking": "off"}))
    assert (await control.restore()).reasoning_effort is None
    await store.aclose()


async def test_selecting_a_provider_does_not_restore_an_incompatible_old_level(tmp_path):
    first = replace(configuration("deepseek"), supports_thinking=True, thinking={"type": "enabled"})
    second = configuration("openai")
    store = store_for(tmp_path)
    await store.append_message(Message("u1", "user", "hello"))
    control = controller(tmp_path, (first, second), store)
    await control.list_models(refresh=False)
    await control.select(first.name, first.model_id)
    await store.append_entry(EntryDraft("thinking_level_change", data={"thinking": "high"}))
    await control.select(second.name, second.model_id)
    assert (await control.restore()).thinking == {}
    await store.aclose()
