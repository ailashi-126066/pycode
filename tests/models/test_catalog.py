from dataclasses import replace

import pytest

from pycode.kernel import ModelCapabilities, ModelError
from pycode.models.catalog import FileCatalogStore, ModelCatalog
from pycode.models.types import CapabilityOverride, DiscoveredModel
from tests.models.test_factory import configuration
from tests.support.requests import TestTiming


class FixedDirectory:
    def __init__(self, results):
        self.results = results

    async def list_models(self, config):
        value = self.results[config.name]
        if isinstance(value, Exception):
            raise value
        return value


def assemble_catalog(tmp_path, providers, results):
    directory = FixedDirectory(results)
    store = FileCatalogStore(tmp_path / "catalog.json")
    return ModelCatalog(providers, directory, store, TestTiming()), directory


async def test_unknown_server_models_are_selectable_without_inferred_features(tmp_path):
    provider = configuration("deepseek")
    catalog, _ = assemble_catalog(
        tmp_path, (provider,), {provider.name: (DiscoveredModel("new-model"),)}
    )
    await catalog.initialize()
    snapshot = await catalog.refresh()
    assert [model.model_id for model in snapshot.models] == ["configured-model", "new-model"]
    selected = catalog.resolve(provider.name, "new-model")
    assert not selected.supports_tools
    assert not selected.supports_thinking
    assert selected.thinking == {}
    with pytest.raises(ModelError, match="select another"):
        catalog.resolve(provider.name, "configured-model")


async def test_partial_override_preserves_other_detected_capabilities(tmp_path):
    provider = replace(
        configuration("deepseek"),
        model_overrides={
            "new-model": CapabilityOverride(context_window=64000),
        },
    )
    discovered = DiscoveredModel("new-model", ModelCapabilities(supports_tools=True))
    catalog, _ = assemble_catalog(tmp_path, (provider,), {provider.name: (discovered,)})
    await catalog.initialize()
    await catalog.refresh()
    selected = catalog.resolve(provider.name, "new-model")
    assert selected.supports_tools
    assert selected.context_window == 64000


async def test_explicit_false_override_disables_a_reported_capability(tmp_path):
    provider = replace(
        configuration("deepseek"),
        model_overrides={
            "new-model": CapabilityOverride(supports_tools=False),
        },
    )
    model = DiscoveredModel("new-model", ModelCapabilities(supports_tools=True))
    catalog, _ = assemble_catalog(tmp_path, (provider,), {provider.name: (model,)})
    await catalog.refresh()
    assert not catalog.resolve(provider.name, "new-model").supports_tools


async def test_refresh_failure_keeps_cached_models_but_exposes_stale_availability(tmp_path):
    provider = configuration("deepseek")
    catalog, directory = assemble_catalog(
        tmp_path, (provider,), {provider.name: (DiscoveredModel("new-model"),)}
    )
    await catalog.refresh()
    directory.results[provider.name] = ModelError("provider_http_401", "Authentication failed")
    snapshot = await catalog.refresh()
    assert snapshot.errors == {provider.name: "provider_http_401"}
    cached = next(model for model in snapshot.models if model.model_id == "new-model")
    assert cached.availability == "unknown"
    assert cached.source == "cache"


async def test_cache_reloads_without_leaking_to_another_configuration(tmp_path):
    provider = configuration("deepseek")
    results = {provider.name: (DiscoveredModel("new-model"),)}
    catalog, _ = assemble_catalog(tmp_path, (provider,), results)
    await catalog.refresh()
    restored, _ = assemble_catalog(tmp_path, (provider,), results)
    snapshot = await restored.initialize()
    assert all(model.availability == "unknown" for model in snapshot.models)
    changed = replace(provider, base_url="https://different.invalid")
    other, _ = assemble_catalog(tmp_path, (changed,), results)
    snapshot = await other.initialize()
    assert [model.model_id for model in snapshot.models] == ["configured-model"]


async def test_all_providers_have_stable_grouped_model_order(tmp_path):
    providers = (configuration("openai"), configuration("deepseek"), configuration("anthropic"))
    results = {
        provider.name: (DiscoveredModel("z-new"), DiscoveredModel("a-new"))
        for provider in providers
    }
    catalog, _ = assemble_catalog(tmp_path, providers, results)
    snapshot = await catalog.refresh()
    identities = [(model.provider, model.model_id) for model in snapshot.models]
    assert identities == sorted(identities)
    assert catalog.snapshot() is snapshot
    with pytest.raises(TypeError):
        snapshot.errors["injected"] = "changed"
    assert catalog.snapshot().errors == {}


async def test_registered_provider_configuration_is_immutable_and_detached(tmp_path):
    thinking = {"type": "enabled", "options": {"budget": 1024}}
    overrides = {"new-model": CapabilityOverride(supports_thinking=True)}
    provider = replace(configuration("deepseek"), thinking=thinking, model_overrides=overrides)
    catalog, _ = assemble_catalog(
        tmp_path, (provider,), {provider.name: (DiscoveredModel("new-model"),)}
    )
    thinking["options"]["budget"] = 1
    overrides.clear()
    await catalog.initialize()
    selected = catalog.resolve(provider.name, "new-model")
    assert selected.thinking["options"]["budget"] == 1024
    with pytest.raises(TypeError):
        selected.thinking["options"]["budget"] = 2
    assert catalog.resolve(provider.name, "new-model").thinking is selected.thinking
