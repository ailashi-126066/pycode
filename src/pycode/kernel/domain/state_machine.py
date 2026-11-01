"""Minimal transition table shared by every explicit state machine.

Callers own the effects of a transition and run them in a fixed order:
write the SessionEntry, emit the event, then fire the Hook.
"""

from collections.abc import Callable, Mapping
from typing import Generic, TypeVar

from pycode.kernel.domain.errors import DomainError

Guard = Callable[..., bool]
S = TypeVar("S", bound=str)
T = TypeVar("T", bound=str)


class StateMachine(Generic[S, T]):
    def __init__(self, table: Mapping[tuple[S, T], tuple[S, Guard | None]]) -> None:
        for (state, trigger), (target, _) in table.items():
            if target == state:
                # Borrowed from pi-durable: every transition must make progress.
                raise DomainError(
                    "invalid_transition", f"{trigger} keeps {state} unchanged (idle transition)"
                )
        self.table = dict(table)

    def apply(self, state: S, trigger: T, **facts) -> S:
        """Return the target state, or raise invalid_transition."""
        if (state, trigger) not in self.table:
            raise DomainError("invalid_transition", f"Cannot {trigger} from {state}")
        target, guard = self.table[state, trigger]
        if guard is not None and not guard(**facts):
            raise DomainError(
                "invalid_transition", f"Cannot {trigger} from {state}: {guard.__name__} failed"
            )
        return target
