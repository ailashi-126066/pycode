"""Skills install: component-owned implementation."""

# Source: MewCode local skills/install.py; SHA-256:
# 0393811fad87caf067df6c006cc652298ebb151c86ee9883087d557bc85178b0
# Adaptation: repository roots, explicit ref/path overrides and portable path validation.
# Adaptation: immutable commit downloads, owned I/O, provenance, lease and rollback.
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import httpx

from pycode import __version__
from pycode.kernel import FileLease, StorageError, ToolError, json_dumps, owned_io, plain_data
from pycode.skills.catalog import MAX_SKILL_BYTES, linked, parse_skill


@dataclass(frozen=True)
class GitHubSkillSource:
    owner: str
    repo: str
    ref: str | None
    subpath: str
    directory_name: str
    original: str


def safe_component(value):
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(ord(char) < 32 or char in '/\\:*?"<>|' for char in value)
        or value.endswith((".", " "))
        or value.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{index}" for index in range(1, 10)],
            *[f"LPT{index}" for index in range(1, 10)],
        }
    ):
        raise ToolError("invalid_skill_source", "Skill source contains an unsafe path component")
    return value


def safe_subpath(value):
    if not isinstance(value, str) or value.startswith("/") or value.endswith("/"):
        raise ToolError("invalid_skill_source", "Skill source subpath must be relative")
    if not value:
        return ""
    return "/".join(safe_component(part) for part in value.split("/"))


def parse_skill_url(raw, *, ref=None, subpath=None):
    try:
        url = urlsplit(raw.strip())
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.port not in {None, 443}
            or url.query
            or url.fragment
        ):
            raise ValueError("Use HTTPS without credentials, query or fragment")
        parts = [safe_component(unquote(part)) for part in url.path.strip("/").split("/")]
        if len(parts) < 2:
            raise ValueError("Missing GitHub owner/repository")
        owner, repo = parts[:2]
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in (owner, repo)):
            raise ValueError("Invalid GitHub repository")
        repo = repo.removesuffix(".git")
        safe_component(repo)
        source_ref, source_path = None, ""
        if url.hostname in {"skills.sh", "www.skills.sh"}:
            if len(parts) < 3:
                raise ValueError("skills.sh requires a Skill name")
            source_ref, source_path = "main", "skills/" + "/".join(parts[2:])
        elif url.hostname == "github.com":
            if len(parts) != 2:
                if len(parts) < 4 or parts[2] != "tree":
                    raise ValueError("Use a GitHub repository or tree URL")
                source_ref, source_path = parts[3], "/".join(parts[4:])
        elif url.hostname == "raw.githubusercontent.com":
            if len(parts) < 4 or parts[-1] != "SKILL.md":
                raise ValueError("Raw URL must identify SKILL.md")
            source_ref, source_path = parts[2], "/".join(parts[3:-1])
        else:
            raise ValueError("Use GitHub, raw.githubusercontent.com or skills.sh")
        selected_ref = ref if ref is not None else source_ref
        if selected_ref is not None:
            if not isinstance(selected_ref, str) or not selected_ref or len(selected_ref) > 255:
                raise ValueError("Invalid Git ref")
            safe_subpath(selected_ref)
        selected_path = safe_subpath(subpath if subpath is not None else source_path)
        directory_name = selected_path.split("/")[-1] if selected_path else repo
        if directory_name.startswith("."):
            raise ValueError("Skill destination must be visible to the catalog")
        return GitHubSkillSource(
            owner, repo, selected_ref, selected_path, directory_name, raw.strip()
        )
    except (ValueError, AttributeError) as error:
        raise ToolError("invalid_skill_source", "Cannot parse GitHub Skill URL") from error


MAX_FILE_SIZE = 1 << 20


MAX_TOTAL_SIZE = 8 << 20


MAX_FILE_COUNT = 64


MAX_RECURSION_DEPTH = 4


MAX_TREE_ENTRIES = 128


API = "https://api.github.com"


def write_bytes(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class GitHubSkillInstaller:
    def __init__(self, root: Path, *, transport=None):
        self._root = root
        self._transport = transport

    async def request(self, client, url, *, params=None):
        async with client.stream("GET", url, params=params) as response:
            if response.status_code != 200:
                raise ToolError(
                    "skill_download_failed", f"GitHub returned HTTP {response.status_code}"
                )
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_TOTAL_SIZE:
                    raise ToolError("skill_download_limit", "GitHub response exceeds size limit")
            return bytes(content)

    async def metadata(self, client, url, *, params=None):
        try:
            return json.loads(await self.request(client, url, params=params))
        except (ValueError, UnicodeError) as error:
            raise ToolError("skill_download_failed", "GitHub returned invalid JSON") from error

    async def download(self, client, source, commit, directory, staging, report, *, depth=0):
        if depth > MAX_RECURSION_DEPTH:
            raise ToolError("skill_download_limit", "Skill directory nesting exceeds limit")
        repo_url = f"{API}/repos/{quote(source.owner, safe='')}/{quote(source.repo, safe='')}"
        entries = await self.metadata(
            client, repo_url + "/contents/" + quote(directory, safe="/"), params={"ref": commit}
        )
        if not isinstance(entries, list):
            raise ToolError("invalid_skill_download", "Skill source must be a directory")
        names = set()
        for entry in entries:
            report["entry_count"] += 1
            if report["entry_count"] > MAX_TREE_ENTRIES:
                raise ToolError("skill_download_limit", "Skill entry count exceeds limit")
            if not isinstance(entry, dict):
                raise ToolError("invalid_skill_download", "Invalid GitHub content entry")
            name = safe_component(entry.get("name"))
            if name.casefold() in names or name in {".git", ".pycode-source.json"}:
                raise ToolError("invalid_skill_download", "Duplicate or reserved Skill resource")
            names.add(name.casefold())
            remote = directory + "/" + name if directory else name
            if entry.get("path") != remote:
                raise ToolError(
                    "invalid_skill_download", "GitHub entry escapes requested directory"
                )
            relative = remote[len(source.subpath) + 1 :] if source.subpath else remote
            target = staging.joinpath(*relative.split("/"))
            if entry.get("type") == "dir":
                await owned_io(target.mkdir)
                await self.download(
                    client, source, commit, remote, staging, report, depth=depth + 1
                )
            elif entry.get("type") == "file":
                if entry.get("submodule_git_url") or entry.get("target"):
                    raise ToolError("invalid_skill_download", "Linked resources are not installed")
                size = entry.get("size")
                if (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or not 0 <= size <= MAX_FILE_SIZE
                ):
                    raise ToolError("skill_download_limit", "Skill file exceeds size limit")
                if report["file_count"] >= MAX_FILE_COUNT:
                    raise ToolError("skill_download_limit", "Skill file count exceeds limit")
                data = await self.blob(client, source, commit, remote, entry)
                if len(data) > MAX_FILE_SIZE or report["total_bytes"] + len(data) > MAX_TOTAL_SIZE:
                    raise ToolError("skill_download_limit", "Skill resource exceeds size limit")
                await owned_io(partial(write_bytes, target, data))
                report["file_count"] += 1
                report["total_bytes"] += len(data)
                report["files"][relative] = hashlib.sha256(data).hexdigest()
            else:
                raise ToolError(
                    "invalid_skill_download", "Symlinks and submodules are not installed"
                )

    async def blob(self, client, source, commit, remote, entry):
        content = entry.get("content")
        if entry.get("encoding") == "base64" and isinstance(content, str):
            if len(content) > MAX_FILE_SIZE * 2:
                raise ToolError("skill_download_limit", "Encoded Skill resource exceeds size limit")
            try:
                return base64.b64decode("".join(content.split()), validate=True)
            except (ValueError, binascii.Error) as error:
                raise ToolError(
                    "invalid_skill_download", "Invalid base64 Skill resource"
                ) from error
        url = entry.get("download_url")
        if not isinstance(url, str):
            raise ToolError("invalid_skill_download", "GitHub file has no download URL")
        try:
            parsed = urlsplit(url)
            valid_port = parsed.port in {None, 443}
        except ValueError as error:
            raise ToolError("invalid_skill_download", "Malformed GitHub download URL") from error
        expected = f"/{source.owner}/{source.repo}/{commit}/{remote}"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "raw.githubusercontent.com"
            or parsed.username
            or parsed.password
            or not valid_port
            or parsed.query
            or parsed.fragment
            or parsed.path != quote(expected, safe="/")
        ):
            raise ToolError(
                "invalid_skill_download", "Download URL differs from pinned GitHub source"
            )
        return await self.request(client, url)

    def begin(self, source, staging, lease):
        if self._root.exists() and linked(self._root):
            raise ToolError("invalid_skill_destination", "Linked Skill root is not writable")
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            lease.acquire()
        except StorageError as error:
            raise ToolError(
                "skill_install_busy", "Another Skill installation owns the directory"
            ) from error
        staging.mkdir()

    def publish(self, source, staging, report):
        manifest = staging / "SKILL.md"
        if not manifest.is_file() or manifest.stat().st_size > MAX_SKILL_BYTES:
            raise ToolError("invalid_skill", "Downloaded tree needs a bounded SKILL.md")
        try:
            raw = manifest.read_text(encoding="utf-8-sig")
        except UnicodeError as error:
            raise ToolError("invalid_skill", "Downloaded Skill must use UTF-8") from error
        skill = parse_skill(raw, str(manifest), "user")
        final = self._root / source.directory_name
        if (final.exists() or final.is_symlink()) and (linked(final) or not final.is_dir()):
            raise ToolError(
                "invalid_skill_destination", "Skill destination is not a regular directory"
            )
        report.update(skill_name=skill.entry.name, target_dir=str(final))
        write_bytes(staging / ".pycode-source.json", json_dumps(report, sort_keys=True).encode())
        backup = self._root / (".previous-" + uuid4().hex)
        previous = final.exists()
        if previous:
            os.replace(final, backup)
        try:
            os.replace(staging, final)
        except BaseException:
            if previous:
                os.replace(backup, final)
            raise
        # Reinstalling is the recovery path; the replaced version is not retained.
        if previous:
            shutil.rmtree(backup, ignore_errors=True)
        return report

    def cleanup(self, staging, lease):
        try:
            if staging.exists():
                if staging.parent.resolve() != self._root.resolve() or linked(staging):
                    raise ToolError(
                        "invalid_skill_destination", "Cannot clean an escaped staging path"
                    )
                shutil.rmtree(staging)
        finally:
            lease.close()

    async def install(self, url, *, ref=None, subpath=None):
        source = parse_skill_url(url, ref=ref, subpath=subpath)
        staging = self._root / (".install-" + uuid4().hex)
        lease = FileLease(self._root / ".install.lock")
        report = {
            "source": plain_data(source),
            "entry_count": 0,
            "file_count": 0,
            "total_bytes": 0,
            "files": {},
        }
        try:
            await owned_io(lambda: self.begin(source, staging, lease))
            async with httpx.AsyncClient(
                timeout=30,
                transport=self._transport,
                trust_env=False,
                headers={
                    "User-Agent": f"pycode/{__version__}",
                    "Accept": "application/vnd.github+json",
                },
            ) as client:
                repo_url = (
                    f"{API}/repos/{quote(source.owner, safe='')}/{quote(source.repo, safe='')}"
                )
                selected_ref = source.ref
                if selected_ref is None:
                    repository = await self.metadata(client, repo_url)
                    selected_ref = (
                        repository.get("default_branch") if isinstance(repository, dict) else None
                    )
                    if not isinstance(selected_ref, str) or not selected_ref:
                        raise ToolError(
                            "invalid_skill_download", "Repository has no default branch"
                        )
                resolved = await self.metadata(
                    client, repo_url + "/commits/" + quote(selected_ref, safe="")
                )
                commit = resolved.get("sha") if isinstance(resolved, dict) else None
                if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
                    raise ToolError(
                        "invalid_skill_download", "Cannot resolve immutable GitHub commit"
                    )
                report["commit"] = commit
                await self.download(client, source, commit, source.subpath, staging, report)
            return await owned_io(lambda: self.publish(source, staging, report))
        except httpx.HTTPError as error:
            raise ToolError("skill_download_failed", "GitHub request failed") from error
        except OSError as error:
            raise ToolError(
                "skill_install_io_error", "Cannot publish Skill installation"
            ) from error
        finally:
            await owned_io(lambda: self.cleanup(staging, lease))
