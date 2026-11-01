"""Pinned baseline/workspace pairs and bounded, independently verified evaluation artifacts."""

import difflib
import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from evals.coding.tasks import EvaluationTask
from pycode.kernel import StorageError, ToolResult, atomic_write, file_sha256, json_dumps, owned_io
from pycode.sandbox.adapters.tools.process_runner import run_process

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_TRACE_BYTES = 8 * 1024 * 1024
MAX_PATCH_BYTES = 2 * 1024 * 1024


class TestLog:
    def __init__(self, path):
        self.path = path
        self.stream = path.open("xb")
        self.seen = 0
        self.saved = 0

    async def append(self, data):
        self.seen += len(data)
        remaining = max(0, MAX_LOG_BYTES - self.saved)
        clipped = data[:remaining]
        if clipped:
            self.stream.write(clipped)
            self.saved += len(clipped)

    async def finish(self, result):
        self.close()
        return result

    async def abort(self):
        self.close()

    def close(self):
        if not self.stream.closed:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()


class EvaluationWorkspaces:
    def __init__(
        self, manifest, cache, directory, results=None, *, sandbox=None, sandbox_settings=None
    ):
        self.manifest_path = Path(manifest).resolve()
        self.cache = Path(cache).resolve()
        self.directory = Path(directory).resolve()
        self.results = Path(results).resolve() if results else self.directory.parent / "results"
        if self.results.exists() and any(self.results.iterdir()):
            raise StorageError(
                "eval_results_exist", "Use a fresh result directory; previous evidence is retained"
            )
        self.run_id = uuid4().hex
        self.definition = json.loads(self.manifest_path.read_bytes())
        self.manifest_sha256 = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        self.sandbox = sandbox
        self.sandbox_settings = sandbox_settings
        self.test_counts = {}
        self.tasks = []
        for item in self.definition["tasks"]:
            self.tasks.append(
                EvaluationTask(
                    item["id"],
                    item["source_path"],
                    tuple(item["solution_files"]),
                    tuple(item["test_files"]),
                    item["files_sha256"],
                    tuple(item["test_command"]),
                    item["timeout_seconds"],
                    item["smoke"],
                )
            )

    def owned(self, path):
        resolved = Path(path).resolve()
        if resolved == self.directory or not resolved.is_relative_to(self.directory):
            raise StorageError(
                "eval_path_escape", "Evaluation path is outside its owned work directory"
            )
        return resolved

    def result_path(self, task_id, suffix):
        if task_id not in {task.task_id for task in self.tasks}:
            raise StorageError("eval_task_missing", "Unknown task artifact")
        path = (self.results / (task_id + suffix)).resolve()
        if path.parent != self.results:
            raise StorageError(
                "eval_path_escape", "Evaluation artifact escapes its result directory"
            )
        self.results.mkdir(parents=True, exist_ok=True)
        return path

    def remove_owned(self, container):
        target = self.owned(container)
        if target.parent != self.directory or Path(container).absolute() != target:
            raise StorageError("eval_path_escape", "Unsafe evaluation cleanup target")
        io_target = target
        if os.name == "nt":
            name = str(target)
            name = "\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name
            io_target = Path(name)

        def remove_readonly(function, filename, info):
            error = info[1]
            path = Path(filename)
            name = str(path)
            if name.startswith("\\\\?\\UNC\\"):
                name = "\\\\" + name[8:]
            elif name.startswith("\\\\?\\"):
                name = name[4:]
            if not Path(name).resolve().is_relative_to(target):
                raise StorageError(
                    "eval_path_escape", "Cleanup encountered a link outside its owned task"
                )
            if not isinstance(error, PermissionError):
                raise error
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            function(filename)

        shutil.rmtree(io_target, onerror=remove_readonly)

    async def prepare(self, task):
        return await owned_io(self.prepare_sync, task)

    def prepare_sync(self, task):
        source = (self.cache / task.source_path).resolve()
        if not source.is_relative_to(self.cache):
            raise StorageError("eval_source_escape", "Task source escapes the pinned cache")
        container = self.owned(self.directory / task.task_id)
        if container.exists():
            raise StorageError("eval_workspace_exists", "Use a fresh evaluation run directory")
        verified = []
        for filename, digest in task.files_sha256.items():
            original = (source / filename).resolve()
            if (
                not original.is_relative_to(source)
                or original.stat().st_size > MAX_FILE_BYTES
                or file_sha256(original) != digest
            ):
                raise StorageError("eval_source_changed", "Pinned task file changed: " + filename)
            for name in ("baseline", "workspace"):
                self.owned(container / name / filename)
            verified.append((filename, original))
        try:
            for filename, original in verified:
                for name in ("baseline", "workspace"):
                    target = self.owned(container / name / filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
            workspace = container / "workspace"
            for arguments in (["init", "--quiet"], ["add", "-A"]):
                result = subprocess.run(
                    ["git", "-C", str(workspace), *arguments],
                    capture_output=True,
                    timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode:
                    raise StorageError(
                        "eval_git_failed", "Cannot prepare the owned snapshot repository"
                    )
            return workspace
        except BaseException:
            if container.exists() and container.parent == self.directory:
                self.remove_owned(container)
            raise

    async def test(self, task, workspace, python):
        workspace = self.owned(workspace)
        number = self.test_counts.get(task.task_id, 0)
        self.test_counts[task.task_id] = number + 1
        stage = "baseline" if number == 0 else "independent"
        current = workspace.parent / "baseline" if number == 0 else workspace
        report = current / "independent-tests.xml"
        log_path = self.result_path(task.task_id, "." + stage + ".log")
        log = TestLog(log_path)
        argv = (str(python), *task.test_command, "--junitxml", str(report))
        isolation = {"enabled": False, "reason": "OS isolation unavailable on this platform"}
        try:
            if self.sandbox is not None:
                status = await self.sandbox.status(self.sandbox_settings)
                isolation = {"enabled": status.enabled, "reason": status.reason}
                argv = await self.sandbox.launch(argv, current, self.sandbox_settings)
            result = await run_process(
                argv,
                str(current),
                log,
                ToolResult("", "independent_tests", ""),
                limit_seconds=task.timeout_seconds,
                prefix="evaluation_tests",
            )
        finally:
            log.close()
        stdout = log_path.read_bytes().decode("utf-8", "replace")
        counts = None
        if report.exists():
            report = self.owned(report)
            if report.is_symlink() or report.stat().st_size > MAX_LOG_BYTES:
                raise StorageError(
                    "eval_invalid_junit", "Independent test report is not a bounded regular file"
                )
            root = ET.parse(report).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
            counts = {
                name: sum(int(suite.get(name, "0")) for suite in suites)
                for name in ("tests", "failures", "errors", "skipped")
            }
            shutil.copyfile(report, self.result_path(task.task_id, "." + stage + ".xml"))
        if result.exit_code == 0 and (
            counts is None or counts["tests"] == 0 or counts["failures"] or counts["errors"]
        ):
            raise StorageError(
                "eval_invalid_junit",
                "Exit zero did not produce successful independent test evidence",
            )
        return {
            "exit_code": result.exit_code,
            "timeout": result.details["timed_out"],
            "stdout": stdout,
            "stderr": "",
            "output_truncated": log.seen > log.saved,
            "output_bytes": log.seen,
            "junit": counts,
            "isolation": isolation,
            "command": list(argv),
        }

    async def forbidden_changes(self, task, workspace):
        def inspect():
            problems = []
            for filename, digest in task.files_sha256.items():
                if filename in task.solution_files:
                    continue
                path = self.owned(workspace / filename)
                if not path.is_file() or file_sha256(path) != digest:
                    problems.append(filename)
            allowed = set(task.files_sha256) | {"independent-tests.xml"}
            for directory, folders, files in os.walk(workspace, followlinks=False):
                folders[:] = [
                    name for name in folders if name not in {".git", "__pycache__", ".pytest_cache"}
                ]
                for name in folders:
                    child = Path(directory) / name
                    if child.is_symlink():
                        problems.append(child.relative_to(workspace).as_posix())
                for name in files:
                    path = Path(directory) / name
                    relative = path.relative_to(workspace).as_posix()
                    if relative not in allowed or path.is_symlink():
                        problems.append(relative)
            return sorted(set(problems))

        return await owned_io(inspect)

    async def unchanged_tests(self, task, workspace):
        return not await self.forbidden_changes(task, workspace)

    async def changes(self, task, workspace):
        def read():
            changes = []
            baseline = workspace.parent / "baseline"
            patch = bytearray()
            truncated = False
            for filename in task.solution_files:
                original = self.owned(baseline / filename)
                current = self.owned(workspace / filename)
                after_digest = file_sha256(current) if current.is_file() else None
                if after_digest == task.files_sha256[filename]:
                    continue
                before = original.read_text(encoding="utf-8")
                if current.exists() and current.stat().st_size > MAX_FILE_BYTES:
                    after = "[solution exceeds evaluation size limit]"
                    truncated = True
                else:
                    after = current.read_text(encoding="utf-8") if current.exists() else ""
                for line in difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile="a/" + filename,
                    tofile="b/" + filename,
                ):
                    data = line.encode()
                    remaining = max(0, MAX_PATCH_BYTES - len(patch))
                    if len(data) > remaining:
                        truncated = True
                    patch.extend(data[:remaining])
                changes.append(
                    {
                        "path": filename,
                        "before_sha256": task.files_sha256[filename],
                        "after_sha256": after_digest,
                        "patch_truncated": truncated,
                    }
                )
            atomic_write(self.result_path(task.task_id, ".patch"), bytes(patch))
            return changes

        return await owned_io(read)

    async def save_result(self, workspace, result):
        def write():
            self.results.mkdir(parents=True, exist_ok=True)
            task_id = result["task"]
            trace_path = result["agent"].pop("trace_file", None)
            trace_info = None
            if trace_path is not None:
                source = self.owned(trace_path)
                target = self.result_path(task_id, ".session.sqlite.gz")
                digest = hashlib.sha256()
                saved = 0
                total = 0
                with source.open("rb") as stream, gzip.open(target, "xb") as output:
                    while True:
                        line = stream.read(MAX_FILE_BYTES)
                        if not line:
                            break
                        digest.update(line)
                        total += len(line)
                        if len(line) <= MAX_FILE_BYTES and saved + len(line) <= MAX_TRACE_BYTES:
                            output.write(line)
                            saved += len(line)
                trace_info = {
                    "path": target.name,
                    "raw_sha256": digest.hexdigest(),
                    "raw_bytes": total,
                    "saved_bytes": saved,
                    "complete": saved == total,
                    "output_objects_retained": False,
                }
            result["artifacts"] = {
                "trace": trace_info,
                "patch": task_id + ".patch",
                "independent_junit": task_id + ".independent.xml",
            }
            manifest = result["agent"].pop("source_manifest", None)
            versions = result["agent"].pop("dependencies", None)
            if manifest is not None:
                source_id = manifest["sha256"]
                source_file = "source-" + source_id + ".json"
                destination = self.results / source_file
                if not destination.exists():
                    atomic_write(
                        destination,
                        (
                            json_dumps(
                                {"source_manifest": manifest, "dependencies": versions},
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n"
                        ).encode(),
                    )
                result["source_sha256"] = source_id
                result["artifacts"]["source_manifest"] = source_file
            destination = self.result_path(task_id, ".json")
            atomic_write(
                destination, (json_dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
            )
            with (self.results / "results.jsonl").open("ab") as stream:
                stream.write((json_dumps(result, ensure_ascii=False) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())

        await owned_io(write)

    async def summarize(self, results, telemetry):
        counts = {}
        for result in results:
            counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
        payload = {
            "run_id": self.run_id,
            "manifest_sha256": self.manifest_sha256,
            "tasks": len(results),
            "outcomes": counts,
            "telemetry": telemetry,
        }
        await owned_io(
            atomic_write,
            self.results / "summary.json",
            (json_dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        return payload

    async def cleanup(self, workspace):
        def remove():
            target = self.owned(workspace).parent
            if target.parent != self.directory or target == self.directory:
                raise StorageError("eval_path_escape", "Unsafe evaluation cleanup target")
            self.remove_owned(target)

        await owned_io(remove)
