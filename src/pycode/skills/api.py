"""Skills api: component-owned implementation."""

import asyncio

from pycode.kernel import EntryDraft, SessionTree, ToolError, data_hash, estimate_text, plain_data
from pycode.kernel import HistoryJournal as SessionStore
from pycode.skills.contracts import CatalogWriteGuard, SkillCatalog, SkillInstaller
from pycode.skills.types import skill_entry

CONTROL_TOOLS = frozenset({"SkillSearch"})


class Skills:
    def __init__(
        self,
        history: SessionStore,
        catalog: SkillCatalog,
        *,
        catalog_write_guard: CatalogWriteGuard,
        index_budget=2000,
        installer: SkillInstaller | None = None,
    ):
        self._history = history
        self._catalog_write_guard = catalog_write_guard
        self._catalog = catalog
        self._budget = index_budget
        self._installer = installer
        self._lock = asyncio.Lock()

    async def refresh(self, index=None):
        async with self._lock:
            if index is None:
                index = await self._catalog.scan()
            entries = [
                {**plain_data(entry), "allowed_tools": list(entry.allowed_tools)}
                for entry in index.entries
            ]
            digest = data_hash(entries)
            snapshot = await self._history.snapshot()
            path = SessionTree(snapshot.index).path(snapshot.leaf_id)
            previous = next(
                (entry for entry in reversed(path) if entry.type == "skill_catalog"), None
            )
            lines = [
                f"- {entry.name}: {entry.description} ({entry.source}; {entry.mode})"
                for entry in index.entries
            ]
            listing = "\n".join(lines)
            dispatch = estimate_text(listing) > self._budget
            data = {
                "entries": entries,
                "hash": digest,
                "text": "",
                "dispatch": dispatch,
                "issues": list(index.issues),
            }
            if previous is not None and plain_data(previous.data) == data:
                return previous.data
            saved = await self._history.append_entry(EntryDraft("skill_catalog", data=data))
            return saved.data

    async def index(self):
        snapshot = await self._history.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        entry = next((item for item in reversed(path) if item.type == "skill_catalog"), None)
        if entry is None:
            if not self._catalog_write_guard.can_initialize(snapshot):
                raise ToolError(
                    "skill_catalog_unavailable", "Recover execution before creating a catalog"
                )
            return await self.refresh()
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
            result["revision"] = entry["content_hash"]
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
        return await self._write(name, content, scope=scope)

    async def update(self, name, content, *, expected_revision, scope="user"):
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ToolError("skill_conflict", "Updating a Skill requires its current revision")
        return await self._write(name, content, scope=scope, expected_revision=expected_revision)

    async def _write(self, name, content, *, scope, expected_revision=None):
        entry = await self._catalog.write(
            name, content, scope=scope, expected_revision=expected_revision
        )
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
                data={
                    "kind": "skill_updated" if expected_revision is not None else "skill_created",
                    **report,
                },
            )
        )
        return report

    async def install(self, url, *, ref=None, subpath=None):
        if self._installer is None:
            raise ToolError("skill_install_unavailable", "This session has no Skill installer")
        report = await self._installer.install(url, ref=ref, subpath=subpath)
        catalog = await self.refresh()
        saved = await self._history.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "skill_install", **report, "catalog_hash": catalog["hash"]},
            )
        )
        return saved.data
