"""Skill-owned catalog, routes, resources, framework loading and permission assembly."""

from dataclasses import dataclass
from pathlib import Path

from pycode.kernel import SessionTree
from pycode.skills.api import Skills
from pycode.skills.catalog import FileSkillCatalog
from pycode.skills.framework import (
    SkillBackend,
    SkillsFrameworkExtension,
    SkillsScopeConstraint,
    SkillsToolExtension,
)
from pycode.skills.install import GitHubSkillInstaller
from pycode.skills.tools import InstallSkill, SaveSkill, SkillSearch
from pycode.skills.types import SkillSelection


@dataclass(frozen=True)
class SkillsSettings:
    workspace: Path
    user_data: Path
    project_trusted: bool = False
    index_budget: int = 2000


@dataclass(frozen=True)
class SkillsRun:
    tools: tuple
    routes: dict
    extension: SkillsFrameworkExtension
    tool_extension: SkillsToolExtension | None
    permission_constraint: SkillsScopeConstraint | None
    script_roots: tuple[Path, ...] = ()


class SkillsSession:
    def __init__(self, module, settings, history, catalog_write_guard):
        self.module, self.settings, self.history = module, settings, history
        self.roots = module._roots(settings)
        self.catalog = FileSkillCatalog(self.roots)
        self.service = Skills(
            history,
            self.catalog,
            catalog_write_guard=catalog_write_guard,
            index_budget=settings.index_budget,
            installer=GitHubSkillInstaller(settings.user_data / "skills"),
        )

    async def initialize(self):
        return await self.service.index()

    async def prepare_run(self, *, read_only=False) -> SkillsRun:
        snapshot = await self.history.snapshot()
        index = await self.catalog.scan()
        indexed = await self.service.refresh(index)
        paths = tuple(self.catalog.virtual_path(entry) for entry in index.entries)
        invocable_by_command = (entry for entry in index.entries if entry.disable_model_invocation)
        hidden = tuple(
            self.catalog.virtual_path(entry) for entry in (*invocable_by_command, *index.shadowed)
        )
        selection = SkillSelection(indexed["dispatch"], paths, hidden=hidden)
        for saved in SessionTree(snapshot.index).path(snapshot.leaf_id):
            if saved.message is not None and saved.message.role == "user":
                selected_path = saved.message.provider_metadata.get("skill_request", {}).get("path")
                if selected_path in paths:
                    selection.select((selected_path,))
        routes = self.module.routes(self.settings, selection=selection)
        constraint = SkillsScopeConstraint(self.history)
        tools = self.module.session_tools(self, read_only=read_only)
        return SkillsRun(
            tools,
            routes,
            SkillsFrameworkExtension(selection, tuple(routes), constraint),
            SkillsToolExtension(self.catalog, selection, constraint),
            constraint,
            tuple(Path(entry.path).parent for entry in index.entries),
        )


class SkillsModule:
    def __init__(self, *, backend_factory):
        self.backend_factory = backend_factory

    def open_session(
        self, settings: SkillsSettings, history, *, catalog_write_guard
    ) -> SkillsSession:
        return SkillsSession(self, settings, history, catalog_write_guard)

    def routes(self, settings: SkillsSettings, *, selection=None) -> dict:
        routes = {}
        for source, root in self._roots(settings):
            if root.is_dir():
                prefix = "/skills/" + source + "/"
                # The generic read-only mount, plus Skill version checks and listing filters.
                routes[prefix] = SkillBackend(
                    self.backend_factory(root, read_only=True), prefix=prefix, selection=selection
                )
        return routes

    def standalone(self, settings: SkillsSettings) -> SkillsRun:
        routes = self.routes(settings)
        return SkillsRun(
            (), routes, SkillsFrameworkExtension(None, tuple(routes), None), None, None
        )

    @staticmethod
    def _roots(settings: SkillsSettings):
        roots = [
            ("builtin", Path(__file__).parent / "resources"),
            ("user", settings.user_data / "skills"),
        ]
        if settings.project_trusted:
            roots.extend(
                (
                    ("project-agents", settings.workspace / ".agents" / "skills"),
                    ("project", settings.workspace / ".pycode" / "skills"),
                )
            )
        return tuple(roots)

    @staticmethod
    def session_tools(session: SkillsSession, *, read_only=False) -> tuple:
        return (
            (SkillSearch(session.service),)
            if read_only
            else (
                SkillSearch(session.service),
                InstallSkill(session.service),
                SaveSkill(session.service),
            )
        )
