"""Private graph state owned by the agent runtime."""

from deepagents.graph import DeepAgentState


class PycodeState(DeepAgentState):
    session_id: str
    run_id: str
    branch_leaf_id: str | None
    turn: int
    outcome: str
    final_text: str
    output_continuations: int
    model_tools: list[dict]
    disclosed_tool_names: list[str] | None
    task_state: dict
