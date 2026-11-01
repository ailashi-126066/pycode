"""Immutable contributions to the model's history projection."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pycode.kernel import ProjectedEntry, freeze_value


@dataclass(frozen=True)
class ProjectionContribution:
    entries: tuple[ProjectedEntry, ...] = ()
    checkpoint_updates: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_removals: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "entries", freeze_value(self.entries))
        object.__setattr__(self, "checkpoint_updates", freeze_value(self.checkpoint_updates))
        object.__setattr__(self, "checkpoint_removals", frozenset(self.checkpoint_removals))
