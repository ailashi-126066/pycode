"""The application's sole public composition root.

Feature modules own their adapters and local lifetimes. This facade selects a module
set and connects application/session/run scopes without exposing their construction.
"""

from dataclasses import dataclass, replace

from pycode import __version__
from pycode.app.composition import factories
from pycode.app.composition import runtime as runtime_scope
from pycode.app.composition import session as session_scope
from pycode.app.composition import ui as ui_scope
from pycode.app.composition.inputs import RunOptions, SessionResources
from pycode.app.composition.modules import ApplicationModules
from pycode.app.configuration.settings import AppConfig
from pycode.models import ProviderConfig
from pycode.session import SessionCatalogWriteGuard


@dataclass(frozen=True)
class Application:
    config: AppConfig
    providers: tuple[ProviderConfig, ...]
    _modules: ApplicationModules

    def status(self):
        return self._modules.sessions.application(__version__).status()

    async def sandbox_status(self):
        return await self._modules.sandbox.backend().status(self.config.sandbox)

    def sandbox_backend(self):
        return self._modules.sandbox.backend()

    def session_trace_path(self, session_id):
        return self._modules.sessions.layout.sessions / session_id / "session.sqlite"

    def session_service(self):
        return self._modules.sessions.service()

    def session_repository(self):
        return self._modules.sessions.repository()

    async def open_session(self, session_id, **options):
        return await session_scope.open_agent_session(
            self.config, self.providers, session_id, modules=self._modules, **options
        )

    async def open_view(self, session_id):
        return await session_scope.open_session_view(self.config, session_id, modules=self._modules)

    def model_catalog(self):
        return self._modules.models.catalog(self.providers)

    def model_control(self, store=None, *, catalog=None):
        selected = self.model_catalog() if catalog is None else catalog
        return self._modules.models.control(selected, self._modules.projector(), store)

    def memory_service(self, history, provider=None, consumers=()):
        return factories.build_memory_service(
            self.config, history, provider, consumers, modules=self._modules
        )

    def storage(self, owned_session_id=None):
        return self._modules.sessions.storage(owned_session_id=owned_session_id)

    def console(self, output_format):
        return self._modules.ui.console(output_format)

    def tui(self, session_id=None, tools=()):
        return ui_scope.build_tui(
            self.config, self.providers, session_id=session_id, tools=tools, modules=self._modules
        )

    async def runtime(self, session: SessionResources, options: RunOptions):
        if options.skills_run is None and self._modules.skills is not None:
            settings = factories.skill_settings(self.config)
            if session.history_store is not None:
                skills = self._modules.skills.open_session(
                    settings, session.history_store, catalog_write_guard=SessionCatalogWriteGuard()
                )
                options = replace(
                    options, skills_run=await skills.prepare_run(read_only=self.config.read_only)
                )
            else:
                options = replace(options, skills_run=self._modules.skills.standalone(settings))
        return await runtime_scope.build_runtime(
            self.config, modules=self._modules, session=session, options=options
        )

    async def background_model(
        self, provider, consumers, session_id, run_id, purpose, **request_options
    ):
        return await self._modules.models.owner(
            provider,
            consumers,
            session_id,
            run_id,
            purpose,
            **request_options,
        )

    def memory_updater(self, provider, consumers=()):
        return factories.build_memory_updater(
            self.config, provider, consumers, modules=self._modules
        )

    def summary_workflow(self, model):
        return self._modules.context.workflow(model)
