"""Managed-object inventory and direct deletion of unreferenced objects."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from pycode.kernel import FileLease, StorageError, owned_io, plain_data
from pycode.session.adapters.storage.layout import StorageLayout
from pycode.session.adapters.storage.session import read_header, read_records
from pycode.session.domain.storage import StorageInventory, StoredObject, mark_objects


def string_values(value):
    values = []
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return values


@dataclass
class _InventoryScan:
    """References collected during one inventory; no state survives the scan."""

    objects: dict[str, StoredObject] = field(default_factory=dict)
    tokens_by_object: dict[str, list[str]] = field(default_factory=dict)
    root_tokens: list[str] = field(default_factory=list)
    reference_details: dict[str, dict[str, str]] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    uncertain_projects: set[str] = field(default_factory=set)
    active_projects: set[str] = field(default_factory=set)
    known_workspaces: set[str] = field(default_factory=set)


class FileStorageObjects:
    def __init__(self, user_data: Path, workspace: Path, *, owned_session_id=None):
        self.root = user_data.resolve()
        self.workspace = workspace.resolve()
        self.project = StorageLayout(workspace, user_data).project.resolve()
        self.allowed_roots = [self.root, self.workspace / ".pycode" / "plans"]
        # Memory (<id>.md files, settings.json) is never a managed object.
        self.memory_roots = [self.root / "memory", self.workspace / ".pycode" / "memory"]
        self.lease = FileLease(self.root / "storage.lock")
        self.owned_session_id = owned_session_id

    def checked(self, value):
        path = Path(value)
        resolved = path.resolve()
        for root in self.memory_roots:
            if resolved.is_relative_to(root.resolve()):
                raise StorageError("gc_memory_excluded", "Memory files are not managed objects")
        for root in self.allowed_roots:
            if resolved.is_relative_to(root.resolve()) and resolved != root.resolve():
                return resolved
        raise StorageError("gc_path_escape", "Content operation escapes managed storage")

    def read_json(self, path):
        try:
            return json.loads(self.checked(path).read_bytes())
        except (OSError, ValueError) as error:
            raise StorageError(
                "gc_unreadable_record", "Cannot verify managed storage references"
            ) from error

    def descriptor(self, object_id, kind, files, *, protected=False, metadata=None):
        owned = []
        size = 0
        for path in files:
            checked = self.checked(path)
            if checked.exists():
                if checked.is_symlink() or not checked.is_file():
                    raise StorageError("gc_invalid_object", "Managed objects must be regular files")
                owned.append(str(checked))
                size += checked.stat().st_size
        return StoredObject(
            object_id, kind, tuple(owned), size, protected=protected, metadata=metadata or {}
        )

    def inventory_sync(self):
        scan = _InventoryScan(known_workspaces={str(self.workspace)})
        projects = self.root / "projects"
        if projects.exists():
            for project in sorted(projects.iterdir()):
                if (
                    not re.fullmatch(r"[a-f0-9]{24}", project.name)
                    or not project.is_dir()
                    or project.is_symlink()
                ):
                    continue
                self.inventory_project(project, scan)
        self.inventory_memory_roots(scan)
        return self.resolve_inventory(scan)

    def inventory_project(self, project, scan):
        sessions = project / "sessions"
        workspaces = set()
        if sessions.exists():
            for session in sorted(sessions.iterdir()):
                if not session.is_dir() or session.is_symlink():
                    continue
                self.inventory_session_log(project, session, workspaces, scan)
        self.inventory_summaries(project, scan)
        self.inventory_snapshots(project, scan)
        self.inventory_restore_references(project, scan)
        self.inventory_plans(project, workspaces, scan)

    def inventory_session_log(self, project, session, workspaces, scan):
        log = session / "session.sqlite"
        if log.exists():
            writer_lock = session / "writer.lock"
            owned = project.resolve() == self.project and session.name == self.owned_session_id
            if writer_lock.exists() and not owned:
                probe = FileLease(writer_lock)
                try:
                    probe.acquire()
                except StorageError:
                    scan.active_projects.add(project.name)
                finally:
                    probe.close()
            header = read_header(log)
            _, entries, _ = read_records(log)
            if (
                header.session_id != session.name
                or StorageLayout(Path(header.workspace), self.root).project.name != project.name
            ):
                raise StorageError("gc_session_identity_conflict", "Session identity differs")
            workspaces.add(header.workspace)
            scan.known_workspaces.add(header.workspace)
            for entry in entries:
                tokens = string_values(plain_data(entry))
                scan.root_tokens.extend(tokens)
                for token in tokens:
                    previous = scan.reference_details.get(token)
                    if previous is None or entry.timestamp >= previous["timestamp"]:
                        scan.reference_details[token] = {
                            "session_id": session.name,
                            "entry_id": entry.id,
                            "timestamp": entry.timestamp,
                        }

    def inventory_summaries(self, project, scan):
        summaries = project / "summaries"
        if summaries.exists():
            grouped = {}
            for path in sorted(summaries.glob("*.json")):
                job_id = path.name.split(".", 1)[0]
                if re.fullmatch(r"[a-f0-9]{32}", job_id):
                    grouped.setdefault(job_id, []).append(path)
            for job_id, files in grouped.items():
                identifier = f"summary:{project.name}:{job_id}"
                scan.objects[identifier] = self.descriptor(
                    identifier,
                    "summary",
                    files,
                    protected=not any(
                        path.name.endswith((".result.json", ".invalidated.json")) for path in files
                    ),
                    metadata={"project_id": project.name},
                )
                scan.aliases.setdefault(job_id, []).append(identifier)
                tokens = []
                for path in files:
                    tokens.extend(string_values(self.read_json(path)))
                scan.tokens_by_object[identifier] = tokens

    def inventory_snapshots(self, project, scan):
        trees = project / "snapshots" / "trees"
        if trees.exists():
            for path in sorted(trees.glob("*.json")):
                if not re.fullmatch(r"[a-f0-9]{40}", path.stem):
                    continue
                identifier = f"snapshot:{project.name}:{path.stem}"
                scan.objects[identifier] = self.descriptor(
                    identifier,
                    "snapshot",
                    [path],
                    metadata={
                        "project_id": project.name,
                        "tree_id": path.stem,
                        "gitdir": str(project / "snapshots" / "repository.git"),
                    },
                )
                scan.aliases.setdefault(path.stem, []).append(identifier)

    def inventory_restore_references(self, project, scan):
        restores = project / "snapshots" / "restores"
        if restores.exists():
            for path in restores.glob("*.json"):
                scan.root_tokens.extend(string_values(self.read_json(path)))

    def inventory_plans(self, project, workspaces, scan):
        for workspace in workspaces:
            plans = Path(workspace).resolve() / ".pycode" / "plans"
            if plans not in self.allowed_roots:
                self.allowed_roots.append(plans)
            if not plans.exists():
                continue
            for path in sorted(plans.glob("*/*.md")):
                if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                    continue
                identifier = f"plan:{project.name}:{path.parent.name}:{path.stem}"
                scan.objects[identifier] = self.descriptor(
                    identifier, "plan", [path], metadata={"project_id": project.name}
                )
                scan.aliases.setdefault(str(path.resolve()), []).append(identifier)
                scan.aliases.setdefault(path.stem, []).append(identifier)

    def inventory_memory_roots(self, scan):
        for workspace in scan.known_workspaces:
            root = Path(workspace).resolve() / ".pycode" / "memory"
            if root not in self.memory_roots:
                self.memory_roots.append(root)

    def resolve_inventory(self, scan):
        roots = set()
        last_references = {}
        for token in scan.root_tokens:
            identifiers = scan.aliases.get(token, ())
            roots.update(identifiers)
            detail = scan.reference_details.get(token)
            if detail is not None:
                for identifier in identifiers:
                    previous = last_references.get(identifier)
                    if previous is None or detail["timestamp"] >= previous["timestamp"]:
                        last_references[identifier] = detail
        for identifier, item in list(scan.objects.items()):
            references = set()
            for token in scan.tokens_by_object.get(identifier, ()):
                references.update(scan.aliases.get(token, ()))
            protected = (
                item.protected
                or item.metadata.get("project_id") in scan.uncertain_projects
                or item.metadata.get("project_id") in scan.active_projects
            )
            scan.objects[identifier] = replace(
                item,
                references=tuple(sorted(references)),
                protected=protected,
                metadata={
                    **item.metadata,
                    "last_reference": last_references.get(
                        identifier, item.metadata.get("last_reference")
                    ),
                },
            )
        return StorageInventory(tuple(scan.objects.values()), tuple(sorted(roots)))

    async def inventory(self):
        return await owned_io(self.inventory_sync)

    def purge_snapshot_ref(self, item):
        gitdir = self.checked(item.metadata["gitdir"])
        expected = (
            self.root / "projects" / item.metadata["project_id"] / "snapshots" / "repository.git"
        )
        if gitdir != expected.resolve():
            raise StorageError(
                "gc_git_scope", "Git GC must use the independent snapshot repository"
            )
        command = ["git", "--git-dir=" + str(gitdir)]
        environment = {}
        for key, value in os.environ.items():
            if not key.startswith("GIT_"):
                environment[key] = value
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        bare = subprocess.run(
            [*command, "rev-parse", "--is-bare-repository"],
            capture_output=True,
            check=True,
            env=environment,
        )
        if bare.stdout.strip() != b"true":
            raise StorageError("gc_git_scope", "Snapshot repository is not bare")
        subprocess.run(
            [*command, "update-ref", "-d", "refs/pycode/snapshots/" + item.metadata["tree_id"]],
            capture_output=True,
            check=True,
            env=environment,
        )
        subprocess.run(
            [*command, "gc", "--prune=now"], capture_output=True, check=True, env=environment
        )

    async def delete(self, item):
        def delete():
            self.lease.acquire()
            snapshot_lease = None
            try:
                if item.kind == "snapshot":
                    snapshot_lease = FileLease(
                        Path(item.metadata["gitdir"]).parent / "snapshot.lock"
                    )
                    snapshot_lease.acquire()
                current = self.inventory_sync()
                if item.object_id in mark_objects(current) or any(
                    candidate.object_id == item.object_id and candidate.protected
                    for candidate in current.objects
                ):
                    raise StorageError(
                        "gc_reference_changed", "Object gained a reference during collection"
                    )
                files = [self.checked(path) for path in item.files]
                if item.kind == "snapshot":
                    self.purge_snapshot_ref(item)
                for path in files:
                    path.unlink(missing_ok=True)
            finally:
                if snapshot_lease is not None:
                    snapshot_lease.close()
                self.lease.close()

        await owned_io(delete)

    async def delete_session(self, session_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
            raise StorageError("invalid_session_id", "Session ID must be an opaque identity")
        directory = self.project / "sessions" / session_id

        def delete():
            if not (directory / "session.sqlite").exists():
                raise StorageError("session_not_found", f"Session not found: {session_id}")
            self.lease.acquire()
            writer = FileLease(directory / "writer.lock")
            try:
                # An open session holds its writer lease; deleting it then fails.
                writer.acquire()
                writer.close()
                shutil.rmtree(directory)
            finally:
                writer.close()
                self.lease.close()

        await owned_io(delete)
