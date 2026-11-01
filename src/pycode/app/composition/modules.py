"""An explicit, application-scoped set of feature assembly providers.

No module is discovered dynamically. Optional runtime features can be replaced or
set to None; historical projection providers are configured independently.
"""

import sys
from dataclasses import dataclass, replace
from functools import partial

from pycode.agent.module import AgentModule
from pycode.app.configuration.credentials import credential_environment
from pycode.app.configuration.settings import AppConfig
from pycode.context import ProjectionContributor
from pycode.context.module import ContextModule
from pycode.hooks.module import HooksModule
from pycode.kernel.adapters.timing import SystemTiming
from pycode.mcp import MCPProjection
from pycode.mcp.module import MCPModule
from pycode.memory.module import MemoryModule
from pycode.models.module import ModelsModule
from pycode.plans.module import PlansModule
from pycode.sandbox.module import SandboxModule
from pycode.session.module import SessionsModule
from pycode.skills import SkillsProjection
from pycode.skills.module import SkillsModule
from pycode.snapshot.module import SnapshotModule
from pycode.team.module import TeamModule
from pycode.tools.module import ToolsModule
from pycode.ui.module import UIModule


@dataclass(frozen=True)
class ApplicationModules:
    sandbox: SandboxModule
    snapshots: SnapshotModule
    models: ModelsModule
    tools: ToolsModule
    hooks: HooksModule
    plans: PlansModule
    sessions: SessionsModule
    context: ContextModule
    memory: MemoryModule
    agent: AgentModule
    team: TeamModule
    ui: UIModule
    skills: SkillsModule | None
    mcp: MCPModule | None
    history_contributors: tuple[ProjectionContributor, ...]

    def projector(self):
        return self.context.projector(self.history_contributors)

    def scoped(self, config: AppConfig):
        """Rebind selected providers; never fall back to a newly created default module set."""
        sessions = self.sessions.scoped(config.workspace, config.user_data)
        return replace(
            self,
            sessions=sessions,
            sandbox=self.sandbox.scoped(
                config.sandbox,
                isolated_worker=config.isolated_worker,
                passthrough=config.subprocess_passthrough,
                path_prefix=config.bash_path_prefix,
            ),
            snapshots=self.snapshots.scoped(
                config.workspace, sessions.layout.project / "snapshots"
            ),
            models=self.models.scoped(config.request_database or sessions.layout.runtime_database),
            hooks=self.hooks.scoped(
                config.workspace,
                config.user_data,
                trusted=config.project_trusted,
                worker=config.agent_id != "lead",
            ),
            plans=self.plans.scoped(config.workspace / ".pycode/plans"),
            memory=self.memory.scoped(
                config.workspace,
                config.user_data,
                sessions.layout.project.name,
                trusted=config.project_trusted,
                mode=config.memory_mode,
            ),
            mcp=self.mcp if config.agent_id == "lead" else None,
        )


def default_modules(config: AppConfig) -> ApplicationModules:
    timing = SystemTiming()
    sessions = SessionsModule(config.workspace, config.user_data, timing)
    sandbox = SandboxModule(
        config.sandbox,
        isolated_worker=config.isolated_worker,
        passthrough=config.subprocess_passthrough,
        path_prefix=config.bash_path_prefix,
    )
    tools = ToolsModule()
    models = ModelsModule(
        config.user_data / "model-catalog.json",
        config.request_database or sessions.layout.runtime_database,
        partial(credential_environment, config),
        timing=timing,
    )
    context = ContextModule(request_step=models.model_request_step, stop_reason=models.stop_reason)
    memory = MemoryModule(
        config.workspace,
        config.user_data,
        sessions.layout.project.name,
        timing,
        trusted=config.project_trusted,
        mode=config.memory_mode,
    )
    plans = PlansModule(config.workspace / ".pycode/plans", backend_factory=tools.backend)
    skills = SkillsModule(
        backend_factory=tools.backend,
    )
    return ApplicationModules(
        sandbox=sandbox,
        snapshots=SnapshotModule(
            config.workspace,
            sessions.layout.project / "snapshots",
            excluded_roots=(config.user_data,),
        ),
        models=models,
        tools=tools,
        hooks=HooksModule(
            config.workspace,
            config.user_data,
            trusted=config.project_trusted,
            worker=config.agent_id != "lead",
        ),
        plans=plans,
        sessions=sessions,
        context=context,
        memory=memory,
        agent=AgentModule(),
        team=TeamModule(),
        ui=UIModule(sys.stdout),
        skills=skills,
        mcp=MCPModule(schema=tools.validator()),
        history_contributors=(
            plans.projection(),
            context.prompt_projection(),
            MCPProjection(),
            SkillsProjection(),
        ),
    )
