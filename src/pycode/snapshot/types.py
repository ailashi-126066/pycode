# Pi 没有，pycode 新增
"""Content-addressed file versions and reviewable selective restore plans."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FileVersion:
    object_id: str
    mode: str
    size: int


@dataclass(frozen=True)
class Snapshot:
    tree_id: str
    workspace: str
    files: dict[str, FileVersion]
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileChange:
    path: str
    before: FileVersion | None
    after: FileVersion | None
    patch: str = ""
    binary: bool = False
