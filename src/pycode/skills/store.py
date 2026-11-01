# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/skills.ts
# (frontmatter validation, first-found collisions, disable-model-invocation).
# 参照 MewCode local skills/parser.py; SHA-256:
# eba23547c0b392cd13211b38d1f45bc71a86c7de21a05b9998a3cd227ca3a11d
# Adaptation: native immutable data, line-delimited frontmatter and strict metadata types.
# 参照 MewCode local skills/loader.py; SHA-256:
# 5888d912116f3a08cca7d75e54234299d6d2e8aa2789ca42c017d804390cc9b3
# Adaptation: ordered trusted roots, recursive discovery, hashes and no resource imports.
"""Skill files on disk: parsing, discovery across trusted roots and local authoring."""

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import yaml

from pycode.kernel import FileLease, ToolError, atomic_write, owned_io
from pycode.skills.types import SkillDocument, SkillEntry, SkillIndex


def parse_skill(raw: str, path: str, source: str, *, content_hash=None) -> SkillDocument:
    lines = raw.lstrip().splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ToolError("invalid_skill", "Skill requires YAML frontmatter")
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        raise ToolError("invalid_skill", "Skill frontmatter is not closed")
    try:
        meta = yaml.safe_load("".join(lines[1:closing]))
    except yaml.YAMLError as error:
        raise ToolError("invalid_skill", "Skill frontmatter is invalid YAML") from error
    if not isinstance(meta, dict):
        raise ToolError("invalid_skill", "Skill frontmatter must be a mapping")
    description = meta.get("description")
    # Pi: a missing description is the only frontmatter problem that skips the skill.
    if not isinstance(description, str) or not description.strip():
        raise ToolError("invalid_skill", "description is required")
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        # Pi: fall back to the parent directory name.
        name = Path(path).parent.name
    warnings = validate_name(name)
    if len(description) > MAX_DESCRIPTION_LENGTH:
        warnings.append(
            f"description exceeds {MAX_DESCRIPTION_LENGTH} characters ({len(description)})"
        )
    mode, context = meta.get("mode", "inline"), meta.get("context", "full")
    if mode == "fork":
        raise ToolError(
            "unsupported_skill_mode", "Skill fork is no longer supported; use an inline Skill"
        )
    if (
        not isinstance(mode, str)
        or not isinstance(context, str)
        or mode != "inline"
        or context not in {"full", "recent", "none"}
    ):
        raise ToolError("invalid_skill", "Skill mode or context is invalid")
    if "allowedTools" in meta and "allowed-tools" in meta:
        raise ToolError("invalid_skill", "Use a single allowed-tools declaration")
    allowed = meta.get("allowedTools", meta.get("allowed-tools", []))
    if not isinstance(allowed, list) or any(
        not isinstance(item, str) or not item for item in allowed
    ):
        raise ToolError("invalid_skill", "allowed-tools must be a list of tool names")
    model = meta.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ToolError("invalid_skill", "Skill model must be a nonempty string")
    digest = content_hash or hashlib.sha256(raw.encode()).hexdigest()
    entry = SkillEntry(
        name,
        description,
        source,
        path,
        digest,
        "inline",
        tuple(allowed),
        cast(Literal["full", "recent", "none"], context),
        model,
        disable_model_invocation=meta.get("disable-model-invocation") is True,
    )
    body = "".join(lines[closing + 1 :]).lstrip("\n")
    return SkillDocument(entry, raw, body, tuple(warnings))


MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024


# Pi: validateName. Problems are warnings; the skill still loads.
def validate_name(name: str) -> list[str]:
    errors = []
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({len(name)})")
    if re.fullmatch(r"[a-z0-9-]+", name) is None:
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


MAX_SKILL_BYTES = 256 * 1024


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class FileSkillCatalog:
    def __init__(self, roots: tuple[tuple[str, Path], ...]):
        self._roots = roots

    def virtual_path(self, entry):
        for source, root in self._roots:
            if source == entry.source:
                relative = Path(entry.path).relative_to(root.resolve()).as_posix()
                return "/skills/" + source + "/" + relative
        raise ToolError("skill_not_accessible", "Skill has no trusted virtual mount")

    async def write(self, name, content, *, scope="user", replace=False):
        """Create (or with replace, overwrite) one definition without running its resources."""
        if scope not in {"user", "project"}:
            raise ToolError("invalid_skill_scope", "Choose user or project scope")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ToolError("invalid_skill", "Skill must be bounded UTF-8 text")
        document = parse_skill(content, "SKILL.md", scope)
        # The name becomes a directory, so authoring accepts no frontmatter warnings.
        if document.warnings:
            raise ToolError("invalid_skill", "; ".join(document.warnings))
        if name != document.entry.name:
            raise ToolError("invalid_skill", "Skill name must match its frontmatter")
        root = next((root for source, root in self._roots if source == scope), None)
        if root is None:
            raise ToolError(
                "skill_not_accessible", "Trust the project before writing project Skills"
            )

        def write():
            if scope == "project" and not root.resolve().is_relative_to(
                root.parent.parent.resolve()
            ):
                raise ToolError("skill_not_accessible", "Project Skill root escapes the workspace")
            if root.exists() and linked(root):
                raise ToolError("skill_not_accessible", "Linked Skill roots are not writable")
            root.mkdir(parents=True, exist_ok=True)
            lease = FileLease(root / ".catalog.lock")
            lease.acquire()
            try:
                directory = root / name
                if directory.is_symlink():
                    raise ToolError(
                        "skill_not_accessible", "Linked Skill directories are not writable"
                    )
                if not replace:
                    directory.mkdir(exist_ok=True)
                target = directory / "SKILL.md"
                if target.is_symlink():
                    raise ToolError(
                        "skill_not_accessible", "Linked Skill documents are not writable"
                    )
                if replace:
                    if not target.is_file():
                        raise ToolError("unknown_skill", "Skill does not exist; create it first")
                elif target.exists():
                    raise ToolError("skill_exists", "Skill exists; update it instead")
                atomic_write(target, content.encode("utf-8"))
                return self.document(target, scope).entry
            finally:
                lease.close()

        return await self.io(write)

    async def entry_at_virtual_path(self, virtual_path):
        if not virtual_path.endswith("/SKILL.md"):
            return None
        parts = PurePosixPath(virtual_path).parts
        if ".." in parts or "\\" in virtual_path:
            raise ToolError("skill_not_accessible", "Skill path escapes its source")
        for source, root in self._roots:
            prefix = "/skills/" + source + "/"
            if not virtual_path.startswith(prefix):
                continue
            relative = virtual_path.removeprefix(prefix)
            path = root.resolve() / relative

            def selected_document(path=path, source=source):
                return self.document(self.checked(str(path)), source).entry

            return await self.io(selected_document)
        return None

    async def io(self, action):
        try:
            return await owned_io(action)
        except OSError as error:
            raise ToolError("skill_io_error", "Cannot read Skill storage") from error

    def document(self, path, source):
        if linked(path) or not path.is_file() or path.stat().st_size > MAX_SKILL_BYTES:
            raise ToolError("invalid_skill", "Skill must be a bounded regular file")
        with path.open("rb") as stream:
            data = stream.read(MAX_SKILL_BYTES + 1)
        if len(data) > MAX_SKILL_BYTES:
            raise ToolError("invalid_skill", "Skill exceeds document size limit")
        try:
            raw = data.decode("utf-8-sig")
        except UnicodeError as error:
            raise ToolError("invalid_skill", "Skill must be UTF-8") from error
        document = parse_skill(
            raw, str(path.resolve()), source, content_hash=hashlib.sha256(data).hexdigest()
        )
        return document

    async def scan(self):
        def scan():
            entries, issues, shadowed = {}, [], []
            for source, root in self._roots:
                if not root.exists():
                    continue
                if linked(root):
                    issues.append(source + ": linked root skipped")
                    continue
                for directory, names, files in os.walk(root, followlinks=False):
                    parent = Path(directory)
                    names[:] = sorted(
                        name
                        for name in names
                        if not name.startswith(".") and not linked(parent / name)
                    )
                    if "SKILL.md" not in files:
                        continue
                    path = parent / "SKILL.md"
                    location = source + ": " + str(path.relative_to(root))
                    try:
                        document = self.document(path, source)
                    except ToolError as error:
                        issues.append(location + ": " + error.code)
                        continue
                    issues.extend(location + ": " + warning for warning in document.warnings)
                    name = document.entry.name
                    if name in entries:
                        # Pi: the first skill found keeps the name.
                        issues.append(location + f': name "{name}" collision')
                        shadowed.append(document.entry)
                        continue
                    entries[name] = document.entry
            return SkillIndex(
                tuple(entries[name] for name in sorted(entries)), tuple(issues), tuple(shadowed)
            )

        return await self.io(scan)

    async def read(self, entry):
        def read():
            path = self.checked(entry.path)
            document = self.document(path, entry.source)
            if document.entry != entry:
                raise ToolError(
                    "skill_changed", "Skill changed since catalog refresh; reload explicitly"
                )
            return document

        return await self.io(read)

    def checked(self, value):
        path = Path(value)
        if not any(path.is_relative_to(root.resolve()) for _, root in self._roots):
            raise ToolError("skill_not_accessible", "Skill is outside the active trusted roots")
        root = next(
            root.resolve() for _, root in self._roots if path.is_relative_to(root.resolve())
        )
        for parent in (path, *path.parents):
            if parent == root:
                break
            if linked(parent):
                raise ToolError("skill_not_accessible", "Linked Skill paths are not accessible")
        if not path.resolve().is_relative_to(root):
            raise ToolError("skill_not_accessible", "Skill path escapes its trusted root")
        return path

    async def resource(self, entry, relative_path):
        def read():
            relative = Path(relative_path)
            if (
                relative.is_absolute()
                or relative.drive
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[0] not in {"references", "scripts", "assets"}
            ):
                raise ToolError(
                    "invalid_skill_resource",
                    "Resource must be inside references, scripts or assets",
                )
            source = self.checked(entry.path)
            path = self.checked(str(source.parent / relative))
            if not path.is_file() or path.stat().st_size > MAX_SKILL_BYTES:
                raise ToolError(
                    "invalid_skill_resource", "Resource is missing or exceeds the read limit"
                )
            try:
                with path.open("rb") as stream:
                    data = stream.read(MAX_SKILL_BYTES + 1)
                if len(data) > MAX_SKILL_BYTES:
                    raise ToolError("invalid_skill_resource", "Resource exceeds the read limit")
                return data.decode("utf-8")
            except UnicodeError as error:
                raise ToolError("invalid_skill_resource", "Resource is not UTF-8 text") from error

        return await self.io(read)
