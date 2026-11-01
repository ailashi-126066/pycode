# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/path-utils.ts
"""Resolve tool paths onto the CompositeBackend mounts instead of the host filesystem."""

import os
import posixpath
from pathlib import Path
from typing import Protocol, cast

from deepagents.backends import CompositeBackend

from pycode.kernel import ToolError

WORKSPACE = "/workspace/"
# Only these mounts accept writes; their backends map virtual paths to real files.
WRITABLE = ("/workspace/", "/plans/")


class ResolvingBackend(Protocol):
    def resolve(self, value: str) -> Path: ...


# Pi: resolveToCwd. The working directory is the /workspace/ mount.
def resolve_to_cwd(backend: CompositeBackend, path: str) -> str:
    absolute = posixpath.normpath(path if path.startswith("/") else WORKSPACE + path)
    prefixes = [prefix for prefix, _ in backend.sorted_routes]
    if any(absolute == prefix.rstrip("/") or absolute.startswith(prefix) for prefix in prefixes):
        return absolute
    raise ToolError(
        "outside_workspace",
        "Use a path relative to the workspace or under " + ", ".join(sorted(prefixes)),
    )


def real_path(backend: CompositeBackend, path: str) -> str:
    """The file a writable virtual path stands for; the file mutation queue key."""
    for prefix, target in backend.sorted_routes:
        if prefix in WRITABLE and path.startswith(prefix):
            resolved = cast(ResolvingBackend, target).resolve("/" + path.removeprefix(prefix))
            return os.path.realpath(resolved)
    raise ToolError("read_only_path", "Only workspace and Plan files can be changed")


# Pi: formatPath in grep.ts and relativizeFindResultPath in find.ts.
def relative_to(path: str, base: str) -> str:
    prefix = base.rstrip("/") + "/"
    return path.removeprefix(prefix) if path.startswith(prefix) else posixpath.basename(path)


def backend_error(error: str) -> ToolError:
    """Backends report failures as "code: message" text."""
    code, _, message = error.partition(": ")
    return ToolError(code, message or code)
