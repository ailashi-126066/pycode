"""Feature-owned, read-only reconstruction of model context from durable facts."""

from collections.abc import Mapping
from typing import Any, Protocol

from pycode.kernel import ProjectedEntry, ProjectionContribution, SessionEntry, SessionSnapshot


class ProjectionContributor(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def incremental_safe(self) -> bool: ...
    @property
    def checkpoint_keys(self) -> frozenset[str]: ...

    def contribute(
        self,
        snapshot: SessionSnapshot,
        path: tuple[SessionEntry, ...],
        entries: tuple[ProjectedEntry, ...],
        checkpoint: Mapping[str, Any],
    ) -> ProjectionContribution: ...
