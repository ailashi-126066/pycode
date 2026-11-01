# Pi 没有，pycode 新增
"""Task board shared by simple tasks and Plan mode (11.1 section 9), on a minimal state machine.

The model decides the steps and their dependencies; this module only checks
the graph and applies status transitions. Derived state is computed, never stored.
StateMachine callers own the effects of a transition and run them in a fixed order:
write the SessionEntry, emit the event, then fire the Hook.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from graphlib import CycleError, TopologicalSorter
from types import MappingProxyType
from typing import Generic, Literal, TypeVar

from pycode.kernel.errors import DomainError

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


StepStatus = Literal["pending", "in_progress", "completed", "blocked", "failed", "skipped"]
BoardStatus = Literal["active", "blocked", "completed", "failed", "abandoned"]
Derived = Literal["ready", "waiting", "dep_blocked"]

DONE = frozenset({"completed", "skipped"})
STUCK = frozenset({"blocked", "failed"})
MAX_DECISIONS = 10


@dataclass(frozen=True)
class Acceptance:
    text: str
    # A bash command; completing the step needs a successful run of it after the step started.
    check: str | None = None


@dataclass(frozen=True)
class Evidence:
    """A record on the current branch; the tool fields are set when it is a tool result."""

    ref: str
    position: int
    tool: str | None = None
    command: str | None = None
    exit_code: int | None = None
    is_error: bool = False


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    depends_on: tuple[str, ...]
    status: StepStatus = "pending"
    acceptance: tuple[Acceptance, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    note: str = ""
    # The TaskUpdate call id that moved the step to in_progress.
    started_by: str = ""


@dataclass(frozen=True)
class Board:
    id: str
    goal: str
    done_when: tuple[str, ...]
    steps: tuple[Step, ...]
    status: BoardStatus = "active"
    evidence: tuple[Evidence, ...] = ()
    outcome: str = ""
    decisions: tuple[str, ...] = ()
    revision: int = 0
    plan_id: str | None = None


def dependencies_done(board, step, **_) -> bool:
    status = {item.id: item.status for item in board.steps}
    return all(status[dependency] in DONE for dependency in step.depends_on)


def acceptance_proven(step, evidence, **_) -> bool:
    return not step.acceptance or bool(evidence)


def has_note(note, **_) -> bool:
    return bool(note)


def done_when_proven(board, evidence, **_) -> bool:
    # evidence[i] proves done_when[i].
    return all(step.status in DONE for step in board.steps) and len(evidence) == len(
        board.done_when
    )


# completed and skipped have no outgoing transitions, so finished steps cannot move back.
STEP_TRANSITIONS = StateMachine[StepStatus, str](
    {
        ("pending", "start"): ("in_progress", dependencies_done),
        ("pending", "block"): ("blocked", has_note),
        ("pending", "skip"): ("skipped", None),
        ("in_progress", "complete"): ("completed", acceptance_proven),
        ("in_progress", "fail"): ("failed", has_note),
        ("in_progress", "block"): ("blocked", has_note),
        ("in_progress", "skip"): ("skipped", None),
        ("failed", "retry"): ("pending", None),
        ("failed", "skip"): ("skipped", None),
        ("blocked", "retry"): ("pending", None),
        ("blocked", "skip"): ("skipped", None),
    }
)

BOARD_TRANSITIONS = StateMachine[BoardStatus, str](
    {
        ("active", "complete"): ("completed", done_when_proven),
        ("active", "fail"): ("failed", has_note),
        ("active", "block"): ("blocked", has_note),
        ("active", "abandon"): ("abandoned", None),
        ("blocked", "resume"): ("active", None),
        ("blocked", "abandon"): ("abandoned", None),
    }
)


def succeeded(evidence: Evidence) -> bool:
    return not evidence.is_error and evidence.exit_code in (None, 0)


def step_evidence(
    step: Step, cited: tuple[Evidence, ...], branch: Mapping[str, Evidence]
) -> tuple[Evidence, ...]:
    """Cited evidence plus the run of each check; all must succeed after the step started."""
    checks = [item.check.strip() for item in step.acceptance if item.check is not None]
    if not cited and not checks:
        return ()
    start = branch[step.started_by].position
    found = list(cited)
    for check in checks:
        run = next(
            (
                item
                for item in branch.values()
                if item.command is not None
                and item.command.strip() == check
                and item.position > start
                and succeeded(item)
            ),
            None,
        )
        if run is None:
            raise DomainError("check_not_run", f"`{check}` has not run successfully since start")
        found.append(run)
    for item in found:
        if item.position <= start:
            raise DomainError("stale_evidence", f"{item.ref} was recorded before {step.id} started")
        if not succeeded(item):
            raise DomainError("failed_evidence", f"{item.ref} did not succeed")
    return tuple(found)


def evidence_text(evidence: Evidence) -> str:
    if evidence.tool is None:
        return evidence.ref
    command = f" `{evidence.command}`" if evidence.command is not None else ""
    exit_code = f" exit {evidence.exit_code}" if evidence.exit_code is not None else ""
    return f"{evidence.ref} {evidence.tool}{command}{exit_code}"


def graph(board: Board) -> dict[str, tuple[str, ...]]:
    return {step.id: step.depends_on for step in board.steps}


def validate(board: Board) -> None:
    ids = [step.id for step in board.steps]
    if len(set(ids)) != len(ids):
        raise DomainError("invalid_board", "Step ids must be unique")
    for step in board.steps:
        missing = set(step.depends_on) - set(ids)
        if missing:
            raise DomainError(
                "invalid_board", f"{step.id} depends on unknown steps: {', '.join(sorted(missing))}"
            )
    try:
        TopologicalSorter(graph(board)).prepare()
    except CycleError as error:
        raise DomainError(
            "invalid_board", "Dependency cycle: " + " -> ".join(error.args[1])
        ) from error
    if sum(step.status == "in_progress" for step in board.steps) > 1:
        raise DomainError("invalid_board", "Only one step can be in_progress")


def apply(
    board: Board,
    target: str,
    trigger: str,
    *,
    note: str = "",
    evidence: tuple[Evidence, ...] = (),
    call_id: str = "",
    branch: Mapping[str, Evidence] = MappingProxyType({}),
    decision: str = "",
) -> Board:
    """Move the board (target="board") or one step; the result is validated.

    call_id is the TaskUpdate call making the change; branch indexes the current branch's
    records by id, so step completion can check where and how its evidence was produced.
    A decision is appended to the board; only the last MAX_DECISIONS are kept.
    """
    if target == "board":
        status = BOARD_TRANSITIONS.apply(
            board.status, trigger, board=board, note=note, evidence=evidence
        )
        updated = replace(board, status=status, outcome=note, evidence=evidence)
    else:
        steps = {step.id: step for step in board.steps}
        if target not in steps:
            raise DomainError("unknown_step", f"No step {target}")
        step = steps[target]
        if trigger == "complete":
            evidence = step_evidence(step, evidence, branch)
        status = STEP_TRANSITIONS.apply(
            step.status, trigger, board=board, step=step, note=note, evidence=evidence
        )
        moved = replace(step, status=status, note=note, evidence=step.evidence + evidence)
        if trigger == "start":
            moved = replace(moved, started_by=call_id)
        updated = replace(
            board, steps=tuple(moved if item.id == target else item for item in board.steps)
        )
    decisions = (*board.decisions, decision)[-MAX_DECISIONS:] if decision else board.decisions
    updated = replace(updated, revision=board.revision + 1, decisions=decisions)
    validate(updated)
    return updated


def derived(board: Board) -> dict[str, Derived]:
    """ready / waiting / dep_blocked for pending steps; blocked or failed upstream propagates."""
    steps = {step.id: step for step in board.steps}
    dependencies = graph(board)
    stuck: dict[str, bool] = {}
    for identifier in TopologicalSorter(dependencies).static_order():
        stuck[identifier] = steps[identifier].status in STUCK or any(
            stuck[dependency] for dependency in dependencies[identifier]
        )
    result: dict[str, Derived] = {}
    for step in board.steps:
        if step.status != "pending":
            continue
        if any(stuck[dependency] for dependency in step.depends_on):
            result[step.id] = "dep_blocked"
        elif all(steps[dependency].status in DONE for dependency in step.depends_on):
            result[step.id] = "ready"
        else:
            result[step.id] = "waiting"
    return result


def layers(board: Board) -> tuple[tuple[str, ...], ...]:
    """Steps grouped so that each layer only depends on earlier layers."""
    order = {step.id: index for index, step in enumerate(board.steps)}
    sorter = TopologicalSorter(graph(board))
    sorter.prepare()
    result = []
    while sorter.is_active():
        ready = sorter.get_ready()
        result.append(tuple(sorted(ready, key=order.__getitem__)))
        sorter.done(*ready)
    return tuple(result)


def critical_path(board: Board) -> tuple[str, ...]:
    """Longest dependency chain, counted in steps."""
    dependencies = graph(board)
    longest: dict[str, tuple[str, ...]] = {}
    for identifier in TopologicalSorter(dependencies).static_order():
        upstream = max((longest[item] for item in dependencies[identifier]), key=len, default=())
        longest[identifier] = (*upstream, identifier)
    return max(longest.values(), key=len, default=())


def view(board: Board) -> str:
    """Compact view injected every turn while the board is active."""
    states = derived(board)
    lines = [f"Task board {board.id} r{board.revision} [{board.status}]: {board.goal}"]
    for step in board.steps:
        line = f"- {step.id} [{states.get(step.id, step.status)}] {step.title}"
        lines.append(line + (f" ({step.note})" if step.note else ""))
    return "\n".join(lines)


def full_view(board: Board) -> str:
    """Complete view with criteria, evidence and decisions, injected once after compaction."""
    states = derived(board)
    lines = [f"Task board {board.id} r{board.revision} [{board.status}]: {board.goal}"]
    if board.outcome:
        lines.append("Outcome: " + board.outcome)
    lines.append("Done when:")
    lines.extend(f"- {criterion}" for criterion in board.done_when)
    lines.extend(f"  evidence: {evidence_text(item)}" for item in board.evidence)
    lines.append("Steps:")
    for step in board.steps:
        lines.append(f"- {step.id} [{states.get(step.id, step.status)}] {step.title}")
        if step.depends_on:
            lines.append("  depends on: " + ", ".join(step.depends_on))
        for item in step.acceptance:
            check = f" (check: {item.check})" if item.check is not None else ""
            lines.append(f"  acceptance: {item.text}{check}")
        lines.extend(f"  evidence: {evidence_text(item)}" for item in step.evidence)
        if step.note:
            lines.append("  note: " + step.note)
    lines.append("Critical path: " + " -> ".join(critical_path(board)))
    if board.decisions:
        lines.append("Decisions:")
        lines.extend(f"- {decision}" for decision in board.decisions)
    return "\n".join(lines)
