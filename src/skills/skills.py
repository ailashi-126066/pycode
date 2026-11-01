# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/skills.ts
# Adaptation: the catalog is a branch record; skills load through Deep Agents SkillsMiddleware and
# the read tool; allowed-tools narrows the tool list and the gate; read-only /skills/ mounts.
"""SkillsSession: catalog service, /skills/ mounts, SkillsMiddleware and the allowed-tools scope."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import FileDownloadResponse, ReadResult
from deepagents.middleware.skills import SkillsMiddleware, SkillsState

from ..kernel import (
    EntryDraft,
    Event,
    RunScope,
    SessionScope,
    SessionTree,
    ToolError,
    data_hash,
    estimate_text,
    file_sha256,
    plain_data,
)
from ..tools import PycodeBackend, ScopedBackend
from .install import GitHubSkillInstaller
from .store import FileSkillCatalog
from .tools import InstallSkill, SaveSkill, SkillSearch
from .types import SkillSelection, active_skills, skill_entry, skill_scope

if TYPE_CHECKING:
    from ..session import SqliteSessionStore


class Skills:
    """The session's Skill catalog record, search, request preparation, authoring and install."""

    def __init__(
        self,
        history: "SqliteSessionStore",
        catalog: FileSkillCatalog,
        installer: GitHubSkillInstaller,
        *,
        index_budget=2000,
    ):
        self._history = history
        self._catalog = catalog
        self._installer = installer
        self._budget = index_budget
        self._lock = asyncio.Lock()

    def catalog_data(self, index):
        entries = [
            {**plain_data(entry), "allowed_tools": list(entry.allowed_tools)}
            for entry in index.entries
        ]
        listing = "\n".join(
            f"- {entry.name}: {entry.description} ({entry.source}; {entry.mode})"
            for entry in index.entries
        )
        return {
            "entries": entries,
            "hash": data_hash(entries),
            "text": "",
            "dispatch": estimate_text(listing) > self._budget,
            "issues": list(index.issues),
        }

    async def refresh(self, index=None):
        """Record the catalog on the branch when it differs from the last record."""
        async with self._lock:
            if index is None:
                index = await self._catalog.scan()
            data = self.catalog_data(index)
            snapshot = await self._history.snapshot()
            path = SessionTree(snapshot.index).path(snapshot.leaf_id)
            previous = next(
                (entry for entry in reversed(path) if entry.type == "skill_catalog"), None
            )
            if previous is not None and plain_data(previous.data) == data:
                return previous.data
            saved = await self._history.append_entry(EntryDraft("skill_catalog", data=data))
            return saved.data

    async def index(self):
        """The branch's catalog record; before the first run records one, a fresh scan."""
        snapshot = await self._history.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        entry = next((item for item in reversed(path) if item.type == "skill_catalog"), None)
        if entry is None:
            return self.catalog_data(await self._catalog.scan())
        return entry.data

    async def search(self, query, limit=10):
        if not isinstance(query, str) or not isinstance(limit, int) or isinstance(limit, bool):
            raise ToolError("invalid_skill_search", "Search requires text and an integer limit")
        if not 1 <= limit <= 20:
            raise ToolError("invalid_skill_search", "Skill search limit must be between 1 and 20")
        terms = query.casefold().split()
        found = []
        for entry in (await self.index())["entries"]:
            if entry["disable_model_invocation"]:
                continue
            text = (entry["name"] + " " + entry["description"]).casefold()
            score = sum(term in text for term in terms)
            if not terms or score:
                found.append((score, entry))
        found.sort(key=lambda item: (-item[0], item[1]["name"]))
        results = []
        for _, entry in found[:limit]:
            result = {}
            for key in ("name", "description", "source", "mode"):
                result[key] = entry[key]
            result["path"] = self._catalog.virtual_path(skill_entry(entry))
            results.append(result)
        return tuple(results)

    async def list(self):
        return (await self._catalog.scan()).entries

    async def read(self, name):
        for entry in await self.list():
            if entry.name == name:
                return await self._catalog.read(entry)
        raise ToolError("unknown_skill", "Skill is outside the trusted catalog")

    async def load(self, name, args=""):
        """Prepare a request only; the caller explicitly starts the Agent run."""
        if not isinstance(name, str) or not isinstance(args, str) or len(args) > 16000:
            raise ToolError("invalid_skill_arguments", "Skill arguments must be bounded text")
        index = await self._catalog.scan()
        for entry in index.entries:
            if entry.name == name:
                return {
                    "name": name,
                    "path": self._catalog.virtual_path(entry),
                    "arguments": args,
                    "mode": entry.mode,
                    "context": entry.context,
                }
        raise ToolError("unknown_skill", "Skill is outside the trusted catalog")

    async def create(self, name, content, *, scope="user"):
        return await self._write(name, content, scope=scope, replace=False)

    async def update(self, name, content, *, scope="user"):
        return await self._write(name, content, scope=scope, replace=True)

    async def _write(self, name, content, *, scope, replace):
        entry = await self._catalog.write(name, content, scope=scope, replace=replace)
        catalog = await self.refresh()
        report = {
            "name": entry.name,
            "path": entry.path,
            "scope": scope,
            "revision": entry.content_hash,
            "catalog_hash": catalog["hash"],
        }
        await self._history.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "skill_updated" if replace else "skill_created", **report},
            )
        )
        return report

    async def install(self, url, *, ref=None, subpath=None):
        report = await self._installer.install(url, ref=ref, subpath=subpath)
        catalog = await self.refresh()
        saved = await self._history.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "skill_install", **report, "catalog_hash": catalog["hash"]},
            )
        )
        return saved.data


def skill_roots(workspace: Path, user_data: Path, *, trusted: bool):
    roots = [("builtin", Path(__file__).parent / "resources"), ("user", user_data / "skills")]
    if trusted:
        roots.extend(
            (
                ("project-agents", workspace / ".agents" / "skills"),
                ("project", workspace / ".pycode" / "skills"),
            )
        )
    return tuple(roots)


class SkillBackend(ScopedBackend):
    """One read-only Skill root: version receipts, SKILL.md script hints and listing filters."""

    def __init__(self, root: Path, *, selection: SkillSelection | None = None, prefix: str = ""):
        self.files = PycodeBackend(root, read_only=True)
        super().__init__(self.files, root, read_only=True)
        self.receipts = self.files.receipts
        self.selection = selection
        self.prefix = prefix

    def ls(self, path):
        result = self.files.ls(path)
        if self.selection is None or result.error:
            return result
        filtered = []
        for entry in result.entries or ():
            if self.selection.visible(self.prefix.rstrip("/") + entry["path"]):
                filtered.append(entry)
        result.entries = filtered
        return result

    def verify_version(self, value):
        path = self.files.resolve(value)
        expected = self.receipts.get(str(path))
        if expected is not None and file_sha256(path) != expected:
            raise ToolError(
                "skill_changed",
                "Skill changed during this run; start a new run to use the new version",
            )
        return path

    def script_instructions(self, file_path):
        return (
            "\n\nSkill source directory: "
            + str(self.files.resolve(file_path).parent)
            + "\nRead supporting files through the Skill mount. Run scripts with Bash using "
            "their physical paths in this directory; normal command permissions apply."
        )

    def read(self, file_path, offset=0, limit=2000):
        try:
            self.verify_version(file_path)
        except (OSError, ToolError) as error:
            return ReadResult(error=self.files.error(error))
        result = self.files.read(file_path, offset, limit)
        if not result.error and result.file_data is not None and Path(file_path).name == "SKILL.md":
            result.file_data["content"] += self.script_instructions(file_path)
        return result

    def download_files(self, paths):
        results = []
        for value in paths:
            try:
                path = self.verify_version(value)
                with path.open("rb") as source:
                    data = source.read(256 * 1024 + 1)
                if len(data) > 256 * 1024:
                    raise ToolError("skill_too_large", "Skill file exceeds 256 KiB")
                self.receipts[str(path)] = hashlib.sha256(data).hexdigest()
                if path.name == "SKILL.md":
                    data += self.script_instructions(value).encode("utf-8")
                results.append(FileDownloadResponse(path=value, content=data))
            except (OSError, ToolError, UnicodeError, ValueError, KeyError):
                results.append(FileDownloadResponse(path=value, error="permission_denied"))
        return results


class PycodeSkillsMiddleware(SkillsMiddleware):
    """Reloads the listing when SkillSearch changes the selection during a run."""

    def __init__(self, *, selection: SkillSelection, **options):
        super().__init__(**options)
        self.selection = selection
        self.loaded_revision = selection.revision

    @property
    def name(self):
        # Replace the harness's SkillsMiddleware slot; there is one loader.
        return "SkillsMiddleware"

    async def abefore_model(self, state, runtime):
        updates = {}
        if self.selection.revision != self.loaded_revision:
            loaded = await super().abefore_agent({**state, "skills_metadata": None}, runtime, {})
            updates.update(loaded or {})
            self.loaded_revision = self.selection.revision
        pinned = await super().abefore_model(cast(SkillsState, {**state, **updates}), runtime)
        updates.update(pinned or {})
        return updates or None


# Claude-style allowed-tools names mapped to Pi's builtin tools.
ALIASES = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Grep": "grep",
    "Glob": "find",
    "Bash": "bash",
}


def instruction_read(name, arguments):
    return name == "read" and arguments["path"].startswith("/skills/")


class SkillScope:
    """allowed-tools of the active skills: it narrows the tool list and denies other calls."""

    def __init__(self):
        self.active: dict[str, dict] = {}

    def allowed(self):
        scope = skill_scope(tuple(self.active.values()))
        if scope is None:
            return None
        return {ALIASES.get(name, name) for name in scope} | {"SkillSearch", "ToolSearch"}

    async def authorize(self, run, spec, call):
        allowed = self.allowed()
        if (
            allowed is not None
            and call.name not in allowed
            and not instruction_read(call.name, call.arguments)
        ):
            raise ToolError("skill_tool_not_allowed", "Active Skill scope excludes this tool")
        return None


class SkillsSession:
    name = "skills"
    service: Skills
    catalog: FileSkillCatalog
    selection: SkillSelection
    # Directories of the indexed skills, for the sandbox to grant script reads.
    script_roots: tuple[Path, ...] = ()

    def __init__(self, *, trusted: bool, index_budget: int):
        self._trusted = trusted
        self._budget = index_budget
        self._scope = SkillScope()
        self._routes: dict[str, SkillBackend] = {}
        # SKILL.md entries of read calls, from before_tool_call to after_tool_call.
        self._reading: dict[str, dict] = {}

    async def open(self, session: SessionScope) -> None:
        self._roots = skill_roots(session.workspace, session.user_data, trusted=self._trusted)
        self._history = session.history
        self.catalog = FileSkillCatalog(self._roots)
        self.service = Skills(
            session.history,
            self.catalog,
            GitHubSkillInstaller(session.user_data / "skills"),
            index_budget=self._budget,
        )

    async def prepare_run(self, run: RunScope) -> None:
        index = await self.catalog.scan()
        indexed = await self.service.refresh(index)
        paths = tuple(self.catalog.virtual_path(entry) for entry in index.entries)
        by_command = (entry for entry in index.entries if entry.disable_model_invocation)
        hidden = tuple(self.catalog.virtual_path(entry) for entry in (*by_command, *index.shadowed))
        self.selection = SkillSelection(indexed["dispatch"], paths, hidden=hidden)
        snapshot = await self._history.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        for saved in path:
            if saved.message is not None and saved.message.role == "user":
                selected = saved.message.provider_metadata.get("skill_request", {}).get("path")
                if selected in paths:
                    self.selection.select((selected,))
        self._scope.active = {skill["name"]: skill for skill in active_skills(path)}
        self.script_roots = tuple(Path(entry.path).parent for entry in index.entries)
        self._routes = {}
        for source, root in self._roots:
            if root.is_dir():
                prefix = "/skills/" + source + "/"
                self._routes[prefix] = SkillBackend(root, selection=self.selection, prefix=prefix)

    def tools(self, run: RunScope):
        if run.session.read_only:
            return (SkillSearch(self.service),)
        return (SkillSearch(self.service), InstallSkill(self.service), SaveSkill(self.service))

    def mounts(self, run: RunScope):
        return self._routes

    def middleware(self, run: RunScope):
        return (
            PycodeSkillsMiddleware(
                selection=self.selection,
                backend=CompositeBackend(StateBackend(), dict(self._routes)),
                sources=list(self._routes),
            ),
        )

    def gates(self, run: RunScope):
        return (self._scope,)

    def select_tools(self, run: RunScope, specs):
        allowed = self._scope.allowed()
        if allowed is None:
            return specs
        # Loading instructions stays possible when the scope excludes ordinary reads; the gate
        # still permits only the Skill-path exception.
        return tuple(spec for spec in specs if spec.name in allowed or spec.name == "read")

    async def before_tool_call(self, run: RunScope, spec, call):
        if instruction_read(call.name, call.arguments):
            entry = await self.catalog.entry_at_virtual_path(call.arguments["path"])
            if entry is not None:
                self._reading[call.id] = {
                    "name": entry.name,
                    "path": call.arguments["path"],
                    "content_hash": entry.content_hash,
                    "allowed_tools": list(entry.allowed_tools),
                }
        return None

    async def after_tool_call(self, run: RunScope, call, result):
        if not instruction_read(call.name, call.arguments):
            return None
        skill = self._reading.pop(call.id, None)
        if result.is_error:
            return None
        # 11.1 section 33.1 #8: a successful SKILL.md read loads the skill.
        details = {**result.details, "protected_content": True}
        if skill is not None:
            details["skill"] = skill
            self._scope.active[skill["name"]] = skill
        return {"details": details}

    async def on_event(self, event: Event) -> None:
        data = event.data
        if (
            event.kind == "tool_execution_end"
            and data["tool_name"] == "SkillSearch"
            and not data["is_error"]
            and self.selection.dispatch
        ):
            self.selection.select([item["path"] for item in json.loads(data["result"]["text"])])
