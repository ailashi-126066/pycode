"""Model list, selection and restoration share the same append-only branch history."""

from dataclasses import replace

from pycode.kernel import (
    EntryDraft,
    EventSink,
    ModelError,
    ModelSelection,
    SessionTree,
    clone_value,
    plain_data,
)
from pycode.kernel import HistoryJournal as SessionStore
from pycode.models.domain.models import CatalogSnapshot, ProviderConfig, model_selection
from pycode.models.ports.context import Projector
from pycode.models.services.model_catalog import ModelCatalog


class ModelControl:
    def __init__(
        self,
        catalog: ModelCatalog,
        projector: Projector,
        store: SessionStore | None = None,
        events: EventSink | None = None,
    ) -> None:
        self._catalog = catalog
        self._projector = projector
        self._store = store
        self._events = events

    async def list_models(self, *, refresh: bool = True) -> CatalogSnapshot:
        await self._catalog.initialize()
        if refresh:
            return await self._catalog.refresh()
        return self._catalog.snapshot()

    async def current(self) -> ModelSelection | None:
        if self._store is None:
            return None
        snapshot = await self._store.snapshot()
        projection = self._projector.transform_context(snapshot)
        path = SessionTree(snapshot.index).path(projection.leaf_id)
        selection = projection.selection
        for entry in reversed(path):
            if entry.type == "model_change":
                selection = entry.selection
                break
        if selection is not None:
            if any(entry.type == "thinking_level_change" for entry in path):
                selection = replace(selection, thinking=projection.thinking)
        return selection

    async def select(self, provider: str, model_id: str) -> ModelSelection:
        if self._store is None:
            raise ModelError("session_required", "Selecting a model requires an active session")
        configured = self._catalog.resolve(provider, model_id)
        selected = model_selection(configured)
        snapshot = await self._store.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        recorded_hash = None
        for previous in reversed(path):
            if (
                previous.type == "model_change"
                and previous.selection is not None
                and previous.selection.provider == provider
            ):
                recorded_hash = previous.data.get("provider_hash")
                break
        if await self.current() != selected or recorded_hash != self._catalog.provider_hash(
            provider
        ):
            had_level = any(item.type == "thinking_level_change" for item in path)
            entry = await self._store.append_entry(
                EntryDraft(
                    "model_change",
                    selection=selected,
                    data={
                        "catalog_hash": self._catalog.snapshot().config_hash,
                        "provider_hash": self._catalog.provider_hash(provider),
                    },
                )
            )
            if self._events is not None:
                await self._events.emit(
                    "model_change", {"entry_id": entry.id, "selection": plain_data(selected)}
                )
            if had_level:
                await self._store.append_entry(
                    EntryDraft("thinking_level_change", data={"thinking": selected.thinking})
                )
        return selected

    async def restore(self, default: ProviderConfig | None = None) -> ProviderConfig:
        selected = await self.current()
        if selected is None:
            if default is None:
                raise ModelError("model_not_selected", "Choose a provider and model before running")
            return self._catalog.resolve(default.name, default.model_id)
        configured = self._catalog.resolve(selected.provider, selected.model_id)
        store = self._store
        if store is None:
            raise ModelError("session_required", "Restoring a selected model requires its session")
        snapshot = await store.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        for entry in reversed(path):
            if (
                entry.type == "model_change"
                and entry.selection is not None
                and entry.selection.provider == selected.provider
            ):
                fingerprint = entry.data.get("provider_hash")
                if fingerprint is not None and fingerprint != self._catalog.provider_hash(
                    selected.provider
                ):
                    raise ModelError(
                        "provider_configuration_changed",
                        "Provider configuration changed; select a model again",
                    )
                break
        if configured.implementation != selected.implementation:
            raise ModelError(
                "provider_configuration_changed",
                "Provider implementation changed; select a model again",
            )
        if selected.thinking != "off" and not configured.supports_thinking:
            raise ModelError(
                "model_capability_changed", "Thinking capability changed; select a model again"
            )
        parameters = clone_value(selected.thinking_parameters)
        if selected.thinking == "off":
            parameters = {}
        elif not parameters:
            parameters = clone_value(configured.thinking)
        level = selected.thinking
        effort = selected.reasoning_effort
        if selected.thinking == "off":
            effort = None
        if level in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            if configured.implementation == "openai":
                parameters = {"effort": level}
            elif configured.implementation == "deepseek":
                parameters = {"type": "enabled"}
                effort = level
            else:
                parameters = parameters or {"type": "adaptive"}
                effort = level
        return replace(configured, thinking=parameters, reasoning_effort=effort)
