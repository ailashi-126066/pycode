"""Refresh a stable provider/model catalog and resolve explicit selections."""

import asyncio
import hashlib
from dataclasses import replace

from pycode.kernel import (
    ModelCapabilities,
    ModelError,
    SystemTiming,
    clone_value,
    json_dumps,
    plain_data,
)
from pycode.models.domain.models import (
    CatalogSnapshot,
    DiscoveredModel,
    ModelDescriptor,
    ProviderConfig,
)
from pycode.models.ports.models import CatalogStore, ModelDirectory


class ModelCatalog:
    def __init__(
        self,
        providers: tuple[ProviderConfig, ...],
        directory: ModelDirectory,
        store: CatalogStore,
        timing: SystemTiming,
    ) -> None:
        self._providers = {provider.name: clone_value(provider) for provider in providers}
        self._directory = directory
        self._store = store
        self._timing = timing
        definitions = [
            plain_data(provider) for provider in sorted(providers, key=lambda item: item.name)
        ]
        config_hash = hashlib.sha256(json_dumps(definitions, sort_keys=True).encode()).hexdigest()
        self._snapshot = CatalogSnapshot(config_hash=config_hash, refreshed_at=0)
        self._lock = asyncio.Lock()

    async def initialize(self) -> CatalogSnapshot:
        cached = await self._store.load()
        if cached is not None and cached.config_hash == self._snapshot.config_hash:
            models = tuple(
                replace(model, source="cache", availability="unknown") for model in cached.models
            )
            self._snapshot = replace(cached, models=models)
        else:
            models = self.descriptors({})
            self._snapshot = replace(self._snapshot, models=models)
        return clone_value(self._snapshot)

    async def refresh(self) -> CatalogSnapshot:
        async with self._lock:
            results = {}
            errors = {}
            slots = asyncio.Semaphore(4)

            async def fetch(provider):
                async with slots:
                    try:
                        results[provider.name] = await self._directory.list_models(provider)
                    except ModelError as error:
                        errors[provider.name] = error.code

            async with asyncio.TaskGroup() as group:
                for provider in self._providers.values():
                    group.create_task(fetch(provider))
            snapshot = replace(
                self._snapshot,
                refreshed_at=self._timing.now(),
                models=self.descriptors(results),
                errors=dict(sorted(errors.items())),
            )
            await self._store.save(snapshot)
            self._snapshot = snapshot
            return clone_value(snapshot)

    def descriptors(
        self, results: dict[str, tuple[DiscoveredModel, ...]]
    ) -> tuple[ModelDescriptor, ...]:
        models = []
        for provider in self._providers.values():
            refreshed = provider.name in results
            discovered = {model.model_id: model for model in results.get(provider.name, ())}
            if not refreshed:
                for old in self._snapshot.models:
                    if old.provider == provider.name:
                        discovered[old.model_id] = DiscoveredModel(old.model_id, old.capabilities)
            identifiers = {provider.model_id, *discovered, *provider.model_overrides}
            for model_id in sorted(identifiers):
                known = discovered.get(model_id)
                capabilities = known.capabilities if known else ModelCapabilities()
                source = "server" if refreshed and known else "cache" if known else "configured"
                if model_id == provider.model_id:
                    capabilities = ModelCapabilities(
                        provider.context_window,
                        provider.max_output_tokens,
                        provider.supports_tools,
                        provider.supports_thinking,
                        provider.supports_images,
                    )
                override = provider.model_overrides.get(model_id)
                if override is not None:
                    changed = {
                        key: value
                        for key, value in plain_data(override).items()
                        if value is not None
                    }
                    capabilities = replace(capabilities, **changed)
                    source = "override"
                availability = (
                    "available"
                    if refreshed and known
                    else "unavailable"
                    if refreshed
                    else "unknown"
                )
                models.append(
                    ModelDescriptor(
                        provider.name,
                        model_id,
                        provider.implementation,
                        capabilities,
                        availability,
                        source,
                    )
                )
        return tuple(sorted(models, key=lambda item: (item.provider, item.model_id)))

    def snapshot(self) -> CatalogSnapshot:
        return clone_value(self._snapshot)

    def provider_hash(self, provider_name: str) -> str:
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ModelError("unknown_provider", f"Provider is not configured: {provider_name}")
        fields = plain_data(provider)
        fields["timeout_seconds"] = float(provider.timeout_seconds)
        encoded = json_dumps(fields, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def resolve(self, provider_name: str, model_id: str) -> ProviderConfig:
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ModelError("unknown_provider", f"Provider is not configured: {provider_name}")
        match = next(
            (
                model
                for model in self._snapshot.models
                if model.provider == provider_name and model.model_id == model_id
            ),
            None,
        )
        if match is None or match.availability == "unavailable":
            raise ModelError(
                "model_unavailable",
                f"Model is unavailable; select another model: {provider_name}/{model_id}",
            )
        capabilities = match.capabilities
        window = capabilities.context_window or provider.context_window
        output = min(
            capabilities.max_output_tokens or provider.max_output_tokens, provider.max_output_tokens
        )
        if window <= output:
            raise ModelError("invalid_model_capacity", "Selected model leaves no input capacity")
        return clone_value(
            replace(
                provider,
                model_id=model_id,
                context_window=window,
                max_output_tokens=output,
                supports_tools=capabilities.supports_tools,
                supports_thinking=capabilities.supports_thinking,
                supports_images=capabilities.supports_images,
                thinking=provider.thinking if capabilities.supports_thinking else {},
            )
        )
