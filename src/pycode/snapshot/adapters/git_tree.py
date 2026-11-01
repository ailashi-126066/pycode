# Source: https://github.com/anomalyco/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/snapshot/index.ts
# Source: https://github.com/anomalyco/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/core/src/snapshot.ts
# Adaptation: owned git-dir/index, no filters/hooks, checked records and selective restores.
"""Git tree snapshots never use the project's index, refs, commits or stash as storage."""

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import TypeAdapter

from pycode.kernel import (
    FileLease,
    SnapshotError,
    StorageError,
    atomic_write,
    json_dumps,
    owned_io,
    plain_data,
)
from pycode.snapshot.domain.snapshots import (
    FileChange,
    FileVersion,
    RestoreFile,
    RestorePlan,
    RestoreRecord,
    Snapshot,
)

MAX_UNTRACKED_BYTES = 2 * 1024 * 1024


class GitSnapshotStore:
    def __init__(self, workspace: Path, directory: Path, *, excluded_roots: tuple[Path, ...] = ()):
        self._workspace = workspace.resolve()
        self._root: Path | None = None
        self._directory = directory.resolve()
        self._gitdir = self._directory / "repository.git"
        self._index = self._directory / "snapshot.index"
        excluded = []
        for value in (*excluded_roots, self._directory, self._workspace / ".pycode"):
            path = Path(value).resolve()
            if path.is_relative_to(self._workspace):
                excluded.append(path)
        self._excluded = tuple(excluded)
        self._lock = asyncio.Lock()
        self._lease = FileLease(self._directory / "snapshot.lock")

    def environment(self):
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_OPTIONAL_LOCKS="0",
            GIT_LITERAL_PATHSPECS="1",
        )
        return environment

    def read_blob(self, object_id: str) -> bytes:
        """Read one captured blob through the independent snapshot object store."""
        return self.git(["cat-file", "blob", object_id], own=True)[0]

    def git(self, arguments, *, own=False, data=None, source=None, allowed=(0,)):
        environment = self.environment()
        if arguments and arguments[0] == "check-ignore":
            environment.pop("GIT_LITERAL_PATHSPECS")
        command = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.symlinks=true",
            "-c",
            "core.longpaths=true",
            "-c",
            "core.quotepath=false",
            "-c",
            "core.excludesFile=",
        ]
        if own:
            environment["GIT_INDEX_FILE"] = str(self._index)
            command.extend(
                [
                    "-c",
                    f"core.hooksPath={self._directory / 'disabled-hooks'}",
                    f"--git-dir={self._gitdir}",
                    f"--work-tree={self._root or self._workspace}",
                ]
            )
        else:
            command.extend(["-C", str(self._root or self._workspace)])
        command.extend(arguments)
        result = subprocess.run(
            command,
            input=data,
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode not in allowed:
            raise SnapshotError("snapshot_git_error", "Git snapshot operation failed")
        return result.stdout, result.returncode

    def discover(self) -> Path | None:
        if not self._workspace.is_dir():
            raise SnapshotError("workspace_missing", "Snapshot workspace no longer exists")
        output, code = self.git(["rev-parse", "--show-toplevel"], allowed=(0, 128))
        self._root = Path(os.fsdecode(output.rstrip(b"\r\n"))).resolve() if code == 0 else None
        return self._root

    @contextmanager
    def leased(self):
        try:
            self._lease.acquire()
        except StorageError as error:
            raise SnapshotError(
                "snapshot_busy", "Another snapshot writer owns this workspace"
            ) from error
        try:
            yield
        finally:
            self._lease.close()

    async def run_io(self, operation):
        async with self._lock:
            try:
                return await owned_io(operation)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
                raise SnapshotError(
                    "snapshot_io_error", "Snapshot filesystem operation failed"
                ) from error

    def initialize(self):
        if (self._gitdir / "HEAD").is_file():
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        self.git(["init", "--bare", "--quiet", str(self._gitdir)])
        (self._directory / "disabled-hooks").mkdir(exist_ok=True)

    def checked_path(self, root: Path, name: str) -> Path:
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or ".git" in relative.parts
            or ".pycode" in relative.parts
            or "\x00" in name
            or "\\" in name
            or (os.name == "nt" and ":" in name)
        ):
            raise SnapshotError(
                "unsafe_snapshot_path", "Snapshot path is outside the permitted scope"
            )
        path = root.joinpath(*relative.parts)
        if not path.parent.resolve().is_relative_to(self._workspace):
            raise SnapshotError("unsafe_snapshot_path", "Snapshot path escapes the workspace")
        parent = path.parent
        while parent != root:
            if parent.exists():
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise SnapshotError(
                        "unsafe_snapshot_path", "Snapshot path crosses a directory link"
                    )
            parent = parent.parent
        return path

    def registry_path(self, tree_id: str):
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", tree_id):
            raise SnapshotError("invalid_snapshot_id", "Snapshot requires a registered Git tree ID")
        return self._directory / "trees" / f"{tree_id}.json"

    def write_json(self, path: Path, value, *, immutable=False):
        encoded_value = json_dumps(plain_data(value), sort_keys=True, separators=(",", ":"))
        envelope = json_dumps(
            {
                "value": json.loads(encoded_value),
                "sha256": hashlib.sha256(encoded_value.encode()).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if immutable and path.exists():
            if path.read_text(encoding="utf-8") != envelope:
                raise SnapshotError("snapshot_identity_conflict", "Snapshot record changed")
            return
        atomic_write(path, envelope.encode("utf-8"))

    def read_json(self, path: Path, kind):
        envelope = json.loads(path.read_bytes())
        encoded = json_dumps(envelope["value"], sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(encoded.encode()).hexdigest() != envelope["sha256"]:
            raise SnapshotError("snapshot_corrupt", "Snapshot record checksum verification failed")
        return TypeAdapter(kind).validate_json(encoded, strict=True)

    def get_sync(self, tree_id):
        path = self.registry_path(tree_id)
        if not path.is_file():
            raise SnapshotError("snapshot_missing", "Snapshot tree is not registered")
        snapshot = self.read_json(path, Snapshot)
        kind, _ = self.git(["cat-file", "-t", tree_id], own=True)
        if (
            snapshot.tree_id != tree_id
            or snapshot.workspace != str(self._workspace)
            or kind.strip() != b"tree"
        ):
            raise SnapshotError("snapshot_corrupt", "Snapshot ownership or tree type is invalid")
        return snapshot

    def version(self, path: Path, *, write=False, original_mode=None):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        command = ["hash-object", "--no-filters"]
        if write:
            command.append("-w")
        command.append("--stdin")
        if stat.S_ISLNK(info.st_mode):
            data = os.fsencode(os.readlink(path))
            output, _ = self.git(command, own=True, data=data)
            return FileVersion(output.strip().decode(), "120000", len(data))
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError(
                "unsupported_snapshot_file", "Snapshot supports regular files and file symlinks"
            )
        mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
        if os.name == "nt" and original_mode in {"100644", "100755"}:
            mode = original_mode
        with path.open("rb") as source:
            output, _ = self.git(command, own=True, source=source)
        after = path.lstat()
        if (info.st_mtime_ns, info.st_size, info.st_mode) != (
            after.st_mtime_ns,
            after.st_size,
            after.st_mode,
        ):
            raise SnapshotError("snapshot_file_changed", "File changed while it was being captured")
        return FileVersion(output.strip().decode(), mode, info.st_size)

    def capture_sync(self, root):
        self.initialize()
        tracked_data, _ = self.git(["ls-files", "--full-name", "--stage", "-z"])
        tracked = {}
        for record in tracked_data.split(b"\x00"):
            if not record:
                continue
            header, name = record.split(b"\t", 1)
            mode, _, stage = header.split()
            if stage != b"0":
                raise SnapshotError(
                    "snapshot_unmerged_index",
                    "Resolve the project's unmerged index before snapshot capture",
                )
            tracked[os.fsdecode(name)] = mode.decode()
        listed, _ = self.git(
            ["ls-files", "--full-name", "--cached", "--others", "--exclude-standard", "-z"]
        )
        names = sorted({os.fsdecode(name) for name in listed.split(b"\x00") if name})
        ignored = set()
        if names:
            payload = b"\x00".join(os.fsencode("./" + name) for name in names) + b"\x00"
            data, _ = self.git(
                ["check-ignore", "--no-index", "--stdin", "-z"], data=payload, allowed=(0, 1)
            )
            ignored = {os.fsdecode(item).removeprefix("./") for item in data.split(b"\x00") if item}
        files, skipped = {}, []
        for name in names:
            candidate = root / name
            if not candidate.parent.resolve().is_relative_to(self._workspace):
                continue
            if re.fullmatch(r"\..+\.pycode-restore-[a-f0-9]{32}\.partial", candidate.name):
                skipped.append(name)
                continue
            if name in ignored or any(
                candidate.is_relative_to(excluded.resolve()) for excluded in self._excluded
            ):
                skipped.append(name)
                continue
            path = self.checked_path(root, name)
            if not path.exists() and not path.is_symlink():
                continue
            if name not in tracked and path.lstat().st_size > MAX_UNTRACKED_BYTES:
                skipped.append(name)
                continue
            version = self.version(path, write=True, original_mode=tracked.get(name))
            files[name] = version
        self.git(["read-tree", "--empty"], own=True)
        records = b"".join(
            f"{version.mode} {version.object_id}\t".encode() + os.fsencode(name) + b"\x00"
            for name, version in files.items()
        )
        if records:
            self.git(["update-index", "-z", "--index-info"], own=True, data=records)
        output, _ = self.git(["write-tree"], own=True)
        tree_id = output.strip().decode()
        self.git(["update-ref", f"refs/pycode/snapshots/{tree_id}", tree_id], own=True)
        snapshot = Snapshot(tree_id, str(self._workspace), files, tuple(skipped))
        self.write_json(self.registry_path(tree_id), replace(snapshot, skipped=()), immutable=True)
        return snapshot

    async def capture(self):
        def capture():
            root = self.discover()
            if root is None:
                return None
            with self.leased():
                return self.capture_sync(root)

        return await self.run_io(capture)

    async def get(self, tree_id):
        return await self.run_io(lambda: self.get_sync(tree_id))

    def patch(self, before, after, name):
        output, _ = self.git(
            ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", before, after, "--", name],
            own=True,
        )
        text = output.decode("utf-8", errors="replace")
        return text, "Binary files " in text or "GIT binary patch" in text

    async def diff(self, before, after):
        def diff():
            first, second = self.get_sync(before), self.get_sync(after)
            changes = []
            for name in sorted(set(first.files) | set(second.files)):
                if first.files.get(name) == second.files.get(name):
                    continue
                patch, binary = self.patch(before, after, name)
                changes.append(
                    FileChange(name, first.files.get(name), second.files.get(name), patch, binary)
                )
            return tuple(changes)

        return await self.run_io(diff)

    async def preview(self, targets, *, expected_tree_id=None, expected_trees=None):
        def preview():
            root = self.discover()
            if root is None:
                raise SnapshotError("snapshot_unavailable", "File snapshots require a Git project")
            with self.leased():
                for name in targets:
                    self.checked_path(root, name)
                current = self.capture_sync(root)
                expected = self.get_sync(expected_tree_id) if expected_tree_id else current
                files, conflicts = [], []
                for name, tree_id in sorted(targets.items()):
                    target = self.get_sync(tree_id)
                    if expected_trees is not None and name in expected_trees:
                        expected = self.get_sync(expected_trees[name])
                    physical = self.version(
                        self.checked_path(root, name),
                        original_mode=expected.files[name].mode if name in expected.files else None,
                    )
                    if physical != expected.files.get(name):
                        conflicts.append(name)
                    patch, binary = self.patch(current.tree_id, tree_id, name)
                    files.append(
                        RestoreFile(
                            name,
                            tree_id,
                            expected.files.get(name),
                            target.files.get(name),
                            patch,
                            binary,
                        )
                    )
                return RestorePlan(
                    uuid4().hex,
                    str(self._workspace),
                    current.tree_id,
                    tuple(files),
                    tuple(conflicts),
                )

        return await self.run_io(preview)

    def restore_path(self, restore_id):
        if not re.fullmatch(r"[a-f0-9]{32}", restore_id):
            raise SnapshotError("invalid_restore_id", "Invalid restore transaction ID")
        return self._directory / "restores" / f"{restore_id}.json"

    def apply(self, plan, *, recovering=False):
        if plan.workspace != str(self._workspace) or plan.conflicts:
            raise SnapshotError(
                "restore_conflict", "Restore conflicts must be resolved before modifying files"
            )
        root = self.discover()
        if root is None:
            raise SnapshotError("snapshot_unavailable", "File snapshots require a Git project")
        self.get_sync(plan.before_tree_id)
        completed, pending = [], []
        for item in plan.files:
            target = self.get_sync(item.target_tree_id).files.get(item.path)
            if target != item.target:
                raise SnapshotError(
                    "restore_plan_changed",
                    "Restore target no longer matches its registered snapshot",
                )
            path = self.checked_path(root, item.path)
            current = self.version(
                path, original_mode=item.expected.mode if item.expected else None
            )
            if recovering and current == item.target:
                completed.append(item.path)
            elif current != item.expected:
                raise SnapshotError(
                    "restore_conflict",
                    "Workspace contains external changes; no files were restored",
                )
            else:
                pending.append(item)
        record_path = self.restore_path(plan.restore_id)
        record = RestoreRecord(plan, "prepared", tuple(completed))
        self.write_json(record_path, record)
        staged = []
        cleanup = []
        try:
            for item in pending:
                path = self.checked_path(root, item.path)
                temporary = None
                if item.target is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(
                        f".{path.name}.pycode-restore-{plan.restore_id}.partial"
                    )
                    data, _ = self.git(["cat-file", "blob", item.target.object_id], own=True)
                    actual_id, _ = self.git(
                        ["hash-object", "--no-filters", "--stdin"], own=True, data=data
                    )
                    if (
                        len(data) != item.target.size
                        or actual_id.strip().decode() != item.target.object_id
                    ):
                        raise SnapshotError(
                            "snapshot_corrupt", "Snapshot blob size verification failed"
                        )
                    if temporary.exists() or temporary.is_symlink():
                        if (
                            not recovering
                            or self.version(temporary, original_mode=item.target.mode)
                            != item.target
                        ):
                            raise SnapshotError(
                                "restore_staging_conflict",
                                "Restore staging file changed externally",
                            )
                        cleanup.append(temporary)
                        staged.append((item, path, temporary))
                        continue
                    if item.target.mode == "120000":
                        os.symlink(os.fsdecode(data), temporary)
                        cleanup.append(temporary)
                    else:
                        with temporary.open("xb") as output:
                            cleanup.append(temporary)
                            output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                        if os.name == "posix":
                            temporary.chmod(0o755 if item.target.mode == "100755" else 0o644)
                staged.append((item, path, temporary))
            record = replace(record, status="restoring")
            self.write_json(record_path, record)
            for item, path, temporary in staged:
                self.checked_path(root, item.path)
                current = self.version(
                    path, original_mode=item.expected.mode if item.expected else None
                )
                if current != item.expected:
                    raise SnapshotError(
                        "restore_conflict",
                        "File changed during restoration; inspect the recovery record",
                    )
                if temporary is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(temporary, path)
                completed.append(item.path)
                record = replace(record, completed_paths=tuple(completed))
                self.write_json(record_path, record)
            record = replace(record, status="completed")
            self.write_json(record_path, record)
            return record
        except BaseException as error:
            record = replace(
                record,
                status="failed",
                completed_paths=tuple(completed),
                error_code=error.code if isinstance(error, SnapshotError) else "restore_io_error",
            )
            self.write_json(record_path, record)
            raise
        finally:
            for temporary in cleanup:
                if temporary.parent.resolve().is_relative_to(self._workspace):
                    temporary.unlink(missing_ok=True)

    async def restore(self, plan):
        def restore():
            with self.leased():
                record_path = self.restore_path(plan.restore_id)
                if record_path.exists():
                    record = self.read_json(record_path, RestoreRecord)
                    if record.plan != plan:
                        raise SnapshotError("restore_plan_changed", "Restore identity changed")
                    if record.status in {"completed", "compensated"}:
                        return record
                    if record.compensation_id is not None:
                        raise SnapshotError(
                            "restore_compensation_pending", "Finish the pending compensation"
                        )
                    return self.apply(plan, recovering=True)
                return self.apply(plan)

        return await self.run_io(restore)

    async def recover(self, restore_id, *, compensate=False, plan=None):
        fallback_plan = plan

        def recover():
            with self.leased():
                record_path = self.restore_path(restore_id)
                if not record_path.exists() and fallback_plan is not None:
                    if fallback_plan.restore_id != restore_id:
                        raise SnapshotError("restore_plan_changed", "Restore identity changed")
                    self.write_json(record_path, RestoreRecord(fallback_plan, "prepared"))
                record = self.read_json(record_path, RestoreRecord)
                if fallback_plan is not None and record.plan != fallback_plan:
                    raise SnapshotError("restore_plan_changed", "Restore identity changed")
                if record.status == "compensated" or (
                    not compensate and record.status == "completed"
                ):
                    return record
                if record.compensation_id is not None and not compensate:
                    raise SnapshotError(
                        "restore_compensation_pending", "Finish the pending compensation"
                    )
                plan = record.plan
                reverse = None
                if compensate:
                    if record.compensation_id is not None:
                        reverse = self.read_json(
                            self.restore_path(record.compensation_id), RestoreRecord
                        )
                        plan = reverse.plan
                    else:
                        before = self.get_sync(plan.before_tree_id)
                        files = tuple(
                            RestoreFile(
                                item.path,
                                before.tree_id,
                                item.target,
                                before.files.get(item.path),
                                "",
                                item.binary,
                            )
                            for item in plan.files
                        )
                        plan = replace(plan, restore_id=uuid4().hex, files=files)
                        reverse = RestoreRecord(plan, "prepared")
                        self.write_json(self.restore_path(plan.restore_id), reverse)
                        record = replace(
                            record, status="compensating", compensation_id=plan.restore_id
                        )
                        self.write_json(record_path, record)
                restored = (
                    reverse
                    if reverse is not None and reverse.status == "completed"
                    else self.apply(plan, recovering=True)
                )
                if compensate:
                    root = self.discover()
                    if root is None:
                        raise StorageError(
                            "snapshot_unavailable", "Snapshot repository disappeared"
                        )
                    for item in record.plan.files:
                        path = self.checked_path(root, item.path)
                        temporary = path.with_name(
                            f".{path.name}.pycode-restore-{record.plan.restore_id}.partial"
                        )
                        if (
                            temporary.exists() or temporary.is_symlink()
                        ) and item.target is not None:
                            if (
                                self.version(temporary, original_mode=item.target.mode)
                                == item.target
                            ):
                                temporary.unlink()
                    self.write_json(
                        self.restore_path(restore_id),
                        replace(
                            record, status="compensated", compensation_id=restored.plan.restore_id
                        ),
                    )
                return restored

        return await self.run_io(recover)
