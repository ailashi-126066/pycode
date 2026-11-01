"""/tasks text: the board with evidence of completed steps, or its dependency graph."""

from pycode.kernel import Board, work_graph


def tasks_text(board: Board) -> str:
    lines = [work_graph.view(board)]
    for step in board.steps:
        if step.status == "completed":
            lines.extend(
                f"  {step.id} evidence: {work_graph.evidence_text(item)}" for item in step.evidence
            )
    return "\n".join(lines)


def graph_text(board: Board) -> str:
    lines = [f"Task board {board.id} r{board.revision} [{board.status}]: {board.goal}", "Layers:"]
    lines.extend(
        f"  {index}) {', '.join(layer)}"
        for index, layer in enumerate(work_graph.layers(board), start=1)
    )
    lines.append("Critical path: " + " -> ".join(work_graph.critical_path(board)))
    lines.append("Depends on:")
    lines.extend(
        f"  {step.id} <- {', '.join(step.depends_on) if step.depends_on else '-'}"
        for step in board.steps
    )
    return "\n".join(lines)
