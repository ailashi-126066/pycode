"""Own model catalogs, SDK conversion, request accounting and model lifetimes."""

import asyncio
from collections.abc import Callable, Mapping
from copy import copy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel

from pycode.kernel import (
    EventConsumer,
    EventStream,
    MemoryEventJournal,
    Purpose,
    SystemTiming,
    estimate_text,
)
from pycode.models.adapters.calibration import calibrate_model_request
from pycode.models.adapters.directory import SDKModelDirectory
from pycode.models.adapters.factory import ModelFactory
from pycode.models.adapters.failures import ProviderFailureClassifier
from pycode.models.adapters.messages import (
    from_assistant,
    stop_reason,
    to_framework,
    to_framework_messages,
)
from pycode.models.adapters.metered_model import MeteredChatModel
from pycode.models.adapters.model_requests import ModelRequestRunner, model_request_step
from pycode.models.adapters.observation import ProviderObservation
from pycode.models.adapters.storage.catalog import FileCatalogStore
from pycode.models.adapters.storage.requests import SQLiteRequestStore
from pycode.models.domain.models import ProviderConfig
from pycode.models.domain.requests import RetryPolicy
from pycode.models.ports.context import Projector
from pycode.models.services.model_catalog import ModelCatalog
from pycode.models.services.model_control import ModelControl

if TYPE_CHECKING:
    from pycode.session import SqliteSessionStore


async def _close_resources(
    factory: ModelFactory,
    records: SQLiteRequestStore | None,
    events: MemoryEventJournal | None,
) -> None:
    try:
        await factory.aclose()
    finally:
        try:
            if records is not None:
                await records.aclose()
        finally:
            if events is not None:
                await events.aclose()


async def _finish_owned_close(
    factory: ModelFactory,
    records: SQLiteRequestStore | None,
    events: MemoryEventJournal | None,
) -> None:
    # This task always has an awaited owner, including when cleanup is cancelled.
    closing = asyncio.create_task(_close_resources(factory, records, events))
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError:
        await closing
        raise


@dataclass
class MeteredModelOwner:
    model: BaseChatModel
    requests: ModelRequestRunner
    factory: ModelFactory
    records: SQLiteRequestStore
    events: MemoryEventJournal | None

    async def aclose(self) -> None:
        await _finish_owned_close(self.factory, self.records, self.events)


class ModelsModule:
    """Immutable configuration; each owner opens an independent accounting lifetime."""

    to_framework = staticmethod(to_framework)
    to_framework_messages = staticmethod(to_framework_messages)
    from_assistant = staticmethod(from_assistant)
    stop_reason = staticmethod(stop_reason)
    model_request_step = staticmethod(model_request_step)
    calibrate_model_request = staticmethod(
        partial(
            calibrate_model_request, converter=to_framework_messages, estimate_text=estimate_text
        )
    )

    def scoped(self, request_database):
        scoped = copy(self)
        scoped.request_database = request_database
        return scoped

    def __init__(
        self,
        catalog_path: Path,
        request_database: Path,
        environment: Mapping[str, str] | Callable[[tuple[ProviderConfig, ...]], Mapping[str, str]],
        *,
        timing: SystemTiming | None = None,
    ) -> None:
        self.catalog_path = catalog_path
        self.request_database = request_database
        self.environment = environment
        self.timing = timing or SystemTiming()

    def credentials(self, providers: tuple[ProviderConfig, ...]) -> dict[str, str]:
        source = self.environment(providers) if callable(self.environment) else self.environment
        return dict(source)

    def catalog(self, providers: tuple[ProviderConfig, ...]) -> ModelCatalog:
        directory = SDKModelDirectory(
            self.credentials(providers), ProviderFailureClassifier(self.timing)
        )
        return ModelCatalog(providers, directory, FileCatalogStore(self.catalog_path), self.timing)

    @staticmethod
    def control(
        catalog: ModelCatalog,
        projector: Projector,
        store: "SqliteSessionStore | None" = None,
        events: EventStream | None = None,
    ) -> ModelControl:
        return ModelControl(catalog, projector, store, events)

    def records(self) -> SQLiteRequestStore:
        return SQLiteRequestStore(self.request_database)

    async def owner(
        self,
        provider: ProviderConfig,
        consumers: tuple[EventConsumer, ...],
        session_id: str,
        run_id: str,
        purpose: Purpose,
        *,
        retry_policy: RetryPolicy | None = None,
        agent_id: str = "lead",
        events: EventStream | None = None,
    ) -> MeteredModelOwner:
        observation = ProviderObservation(provider.implementation)
        factory = ModelFactory(self.credentials((provider,)), observation)
        records: SQLiteRequestStore | None = None
        journal: MemoryEventJournal | None = None
        try:
            model = factory.create(provider)
            records = self.records()
            journal = MemoryEventJournal() if events is None else None
            sink = (
                events
                if events is not None
                else EventStream(session_id, run_id, consumers, journal=journal)
            )
            requests = ModelRequestRunner(
                records,
                ProviderFailureClassifier(self.timing),
                self.timing,
                retry_policy or RetryPolicy(),
                sink,
                factory.selection(provider),
                session_id,
                run_id,
                purpose=purpose,
                observation=observation,
                agent_id=agent_id,
            )
            return MeteredModelOwner(model, requests, factory, records, journal)
        except BaseException:
            # Construction has not transferred ownership to the caller. Complete
            # each owned close before propagating failure or cancellation.
            await _finish_owned_close(factory, records, journal)
            raise

    @staticmethod
    def metered(owner: MeteredModelOwner, **options: Any) -> MeteredChatModel:
        """Options are confined to the framework/Pydantic model boundary."""
        return MeteredChatModel(model=owner.model, requests=owner.requests, **options)
