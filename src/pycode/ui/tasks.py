# Pi 没有，pycode 新增
"""/tasks text: the board, evidence of completed steps, layers when steps can run in parallel."""

from pycode.kernel import Board, work_graph


def tasks_text(board: Board) -> str:
    lines = [work_graph.view(board)]
    for step in board.steps:
        if step.status == "completed":
            lines.extend(
                f"  {step.id} evidence: {work_graph.evidence_text(item)}" for item in step.evidence
            )
    layers = work_graph.layers(board)
    if any(len(layer) > 1 for layer in layers):
        lines.append("Layers:")
        lines.extend(
            f"  {index}) {', '.join(layer)}" for index, layer in enumerate(layers, start=1)
        )
        lines.append("Critical path: " + " -> ".join(work_graph.critical_path(board)))
    return "\n".join(lines)
