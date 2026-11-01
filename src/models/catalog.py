# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/model-registry.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/models-store.ts
# Adaptation: discovery through the public SDK model-list APIs, an atomic cache file
# holding identities and capabilities (never keys), explicit selection only.
"""The provider/model catalog: SDK discovery, the cache file and selection resolution."""

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, replace
from pathlib import Path

import anthropic
import openai
from pydantic import TypeAdapter

from ..kernel import (
    ModelCapabilities,
    ModelError,
    StorageError,
    SystemTiming,
    atomic_write,
    freeze_value,
    json_dumps,
    owned_io,
    plain_data,
)
from .requests import ProviderFailureClassifier
from .types import CatalogSnapshot, DiscoveredModel, ModelDescriptor, ProviderConfig


class SDKModelDirectory:
    def __init__(
        self, credentials: Mapping[str, str], classifier: ProviderFailureClassifier
    ) -> None:
        self._credentials = credentials
        self._classifier = classifier

    async def list_models(self, config: ProviderConfig) -> tuple[DiscoveredModel, ...]:
        key = self._credentials.get(config.api_key_env)
        if not key:
            raise ModelError("missing_api_key", f"API key reference is unset: {config.api_key_env}")
        options = {
            "api_key": key,
            "base_url": config.base_url,
            "timeout": config.timeout_seconds,
            "max_retries": 0,
            "default_headers": {"User-Agent": "pycode/0.1.0"},
        }
        client = (
            anthropic.AsyncAnthropic(**options)
            if config.implementation == "anthropic"
            else openai.AsyncOpenAI(**options)
        )
        try:
            async with client:
                page = await client.models.list()
                discovered = []
                async for model in page:
                    data = model.model_dump()
                    window = self.positive_integer(data.get("context_window"))
                    output = self.positive_integer(data.get("max_output_tokens"))
                    capabilities = ModelCapabilities(
                        context_window=window,
                        max_output_tokens=output,
                        supports_tools=data.get("supports_tools") is True,
                        supports_thinking=data.get("supports_thinking") is True,
                        supports_images=data.get("supports_images") is True,
                    )
                    discovered.append(DiscoveredModel(model.id, capabilities))
                return tuple(sorted(discovered, key=lambda item: item.model_id))
        except Exception as error:
            failure = self._classifier.classify(error)
            if failure is None:
                raise
            raise ModelError(failure.code, failure.message, retryable=failure.retryable) from error

    def positive_integer(self, value) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None


class FileCatalogStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def run_io(self, operation):
        async with self._lock:
            try:
                return await owned_io(operation)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise StorageError(
                    "invalid_catalog_cache", "Cannot read or publish model catalog"
                ) from error

    async def load(self) -> CatalogSnapshot | None:
        def read():
            if not self._path.exists():
                return None
            payload = json.loads(self._path.read_bytes())
            if (
                not isinstance(payload, dict)
                or type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1
            ):
                raise ValueError("Unsupported catalog schema")
            payload.pop("schema_version")
            if set(payload) - {field.name for field in fields(CatalogSnapshot)}:
                raise ValueError("Unsupported catalog fields")
            return TypeAdapter(CatalogSnapshot).validate_json(json_dumps(payload), strict=True)

        return await self.run_io(read)

    async def save(self, snapshot: CatalogSnapshot) -> None:
        def write():
            payload = {"schema_version": 1, **plain_data(snapshot)}
            encoded = json_dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            atomic_write(self._path, encoded)

        await self.run_io(write)


class ModelCatalog:
    def __init__(
        self,
        providers: tuple[ProviderConfig, ...],
        directory: SDKModelDirectory,
        store: FileCatalogStore,
        timing: SystemTiming,
    ) -> None:
        self._providers = {provider.name: freeze_value(provider) for provider in providers}
        self._directory = directory
        self._store = store
        self._timing = timing
        definitions = [
            plain_data(provider) for provider in sorted(providers, key=lambda item: item.name)
        ]
        config_hash = hashlib.sha256(json_dumps(definitions, sort_keys=True).encode()).hexdigest()
        self._snapshot = freeze_value(CatalogSnapshot(config_hash=config_hash, refreshed_at=0))
        self._lock = asyncio.Lock()

    async def initialize(self) -> CatalogSnapshot:
        cached = await self._store.load()
        if cached is not None and cached.config_hash == self._snapshot.config_hash:
            models = tuple(
                replace(model, source="cache", availability="unknown") for model in cached.models
            )
            self._snapshot = freeze_value(replace(cached, models=models))
        else:
            models = self.descriptors({})
            self._snapshot = freeze_value(replace(self._snapshot, models=models))
        return self._snapshot

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
            snapshot = freeze_value(
                replace(
                    self._snapshot,
                    refreshed_at=self._timing.now(),
                    models=self.descriptors(results),
                    errors=dict(sorted(errors.items())),
                )
            )
            await self._store.save(snapshot)
            self._snapshot = snapshot
            return snapshot

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
        return self._snapshot

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
        return freeze_value(
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
