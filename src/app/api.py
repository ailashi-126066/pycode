# Pi 没有，pycode 新增
"""Public application entry points backed by explicit concrete services."""

import sys
from dataclasses import dataclass

from .. import __version__
from ..agent import RunOptions
from ..context import SummaryWorkflow
from ..kernel import EventStream, SessionScope
from ..memory import MemoryService
from ..models import ModelControl, ProviderConfig, model_request_step, stop_reason
from ..sandbox import Sandbox
from ..session import (
    ApplicationService,
    FileStorageObjects,
    FileWorkspaceInspector,
    SessionService,
    StorageGC,
)
from . import composition
from .composition import SessionResources
from .config import AppConfig


@dataclass(frozen=True)
class Application:
    config: AppConfig
    providers: tuple[ProviderConfig, ...] = ()

    def status(self):
        return ApplicationService(
            workspace=FileWorkspaceInspector(self.config.workspace), version=__version__
        ).status()

    async def sandbox_status(self):
        return await self.sandbox_backend().status()

    def sandbox_backend(self):
        return Sandbox(self.config.sandbox, required=self.config.isolated_worker)

    def session_trace_path(self, session_id):
        return composition.layout(self.config).sessions / session_id / "session.sqlite"

    def session_service(self):
        return SessionService(self.session_repository())

    def session_repository(self):
        return composition.repository(self.config)

    async def open_session(self, session_id, **options):
        return await composition.open_agent_session(
            self.config, self.providers, session_id, **options
        )

    async def open_view(self, session_id):
        return await composition.open_session_view(self.config, session_id)

    def model_catalog(self):
        return composition.model_catalog(self.config, self.providers)

    def model_control(self, store, *, catalog=None):
        return ModelControl(
            self.model_catalog() if catalog is None else catalog,
            composition.projector(),
            store,
            EventStream("catalog", "", ()),
        )

    async def memory_service(self, history, provider=None, consumers=()):
        from contextlib import asynccontextmanager
        from functools import partial

        from ..kernel import ModelError

        @asynccontextmanager
        async def unavailable(**options):
            raise ModelError("providers_required", "Memory extraction requires a provider")
            yield

        memory = MemoryService(
            trusted=self.config.project_trusted,
            mode=self.config.memory_mode,
            model=unavailable
            if provider is None
            else partial(composition.background_model, self.config, provider, consumers),
        )
        identifier = (await history.snapshot()).header.session_id
        await memory.open(
            SessionScope(
                identifier,
                self.config.workspace,
                self.config.user_data,
                history,
                EventStream(identifier, "", consumers),
                self.config.read_only,
                self.config.agent_id,
            )
        )
        return memory

    def storage(self, owned_session_id=None):
        return StorageGC(
            FileStorageObjects(
                self.config.user_data, self.config.workspace, owned_session_id=owned_session_id
            )
        )

    def console(self, output_format):
        from ..ui import ConsoleOutput

        return ConsoleOutput(output_format, sys.stdout)

    def tui(self, session_id=None, tools=()):
        from ..ui import PyApp

        return PyApp(ConfiguredInteractiveSessions(self, tools), session_id=session_id)

    async def runtime(self, session: SessionResources, options: RunOptions):
        return await composition.embedded_runtime(self.config, session, options)

    async def background_model(
        self, provider, consumers, session_id, run_id, purpose, **request_options
    ):
        return await composition.open_model(
            self.config, provider, consumers, session_id, run_id, purpose, **request_options
        )

    def summary_workflow(self, model):
        return SummaryWorkflow(model, request_step=model_request_step, stop_reason=stop_reason)


class ConfiguredInteractiveSessions:
    def __init__(self, application, tools):
        self.application, self.tools = application, tools

    async def open(self, session_id, *, create, consumer):
        return await self.application.open_session(
            session_id, create=create, consumers=(consumer,), tools=self.tools, refresh_models=False
        )

    async def list_sessions(self):
        return await self.application.session_service().list_sessions()

    async def sandbox_status(self):
        return await self.application.sandbox_status()

    async def fork(self, source_id, leaf_id, target_id):
        return await self.application.session_service().fork(source_id, leaf_id, target_id)


def compose_application(
    config: AppConfig, providers: tuple[ProviderConfig, ...] = ()
) -> Application:
    return Application(config, providers)


__all__ = ["Application", "RunOptions", "SessionResources", "compose_application"]
