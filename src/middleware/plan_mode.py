"""Plan mode: read-only planning, then the user approves the plan and it becomes the task board.

/plan sets the state field `plan_mode`. While it is set, code (not the prompt) keeps the model
read-only: before each model request only the read tools, bash and ExitPlanMode are sent, and
before each tool call anything else is refused, as is any bash command that is not in the
allow list (sandbox.py). The model submits the plan with ExitPlanMode(plan, goal, steps); the
run pauses and the user picks one of:

- auto: execute; for the rest of this run write, edit, bash and the task board changes run
  without asking (dangerous commands are still refused and the sandbox still applies);
- review: execute, asking as usual;
- feedback: the model gets the user's comments, stays in plan mode and submits again;
- cancel: leave plan mode and do nothing.

Approving turns goal and steps into the task board (task_board.py) and keeps the plan text in
the state field `plan`.
"""

from typing import NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command, interrupt

from ..context import Context
from ..sandbox import allowed_command
from ..tools.files import ToolError
from .task_board import StepInput, layers, new_board, render


class PlanState(AgentState):
    plan_mode: NotRequired[bool]  # planning now
    plan: NotRequired[str | None]  # the approved plan
    auto_approve: NotRequired[bool]  # this run executes an approved plan without asking


PLANNING_TOOLS = {"read", "ls", "grep", "find", "bash", "ExitPlanMode"}

PROMPT = """## Plan 模式

现在是规划阶段，项目是只读的：只能用 read、ls、grep、find，bash 只能运行几条只读的 git 命令。\
先弄清现状，再调用 ExitPlanMode 提交计划，等用户批准后才能改动。

ExitPlanMode 的参数：
- plan：Markdown 计划正文，写清现状、要改什么、为什么、风险；
- goal：一句话目标；
- steps：一般 3 到 8 步，最多 10 步。每一步是一块能单独验证的工作，不要把"读某个文件"\
"运行某条命令"单独列成一步。depends_on 只写必须先完成的步骤。
- acceptance：每一步完成时要成立的条件。只有确定某条命令在这个项目里能运行时（例如在 README、\
pyproject.toml、package.json 里看到过），才写 check；不确定就只写 text，执行时知道了再用 \
TaskWrite 补上。写了 check 的条件由系统自动验收，没写的由用户确认。"""

FEEDBACK = (
    "用户的修改意见：\n{feedback}\n\n"
    "仍在规划阶段。按意见修改后，重新调用 ExitPlanMode 提交完整的计划。"
)
CANCELLED = "用户取消了这个计划，已退出 Plan 模式。不要执行计划里的任何改动，简短回复后等待用户。"
APPROVED = (
    "用户批准了计划（{how}），已退出 Plan 模式。按任务板执行：开始一步前用 TaskUpdate 标为 "
    "in_progress，做完后标为 completed。知道验证命令后用 TaskWrite 补上 check。\n\n{board}"
)


@tool
def ExitPlanMode(  # noqa: N802 - the tool name the model sees
    plan: str, goal: str, steps: list[StepInput], runtime: ToolRuntime[Context]
):
    """Submit the plan for the user's approval. Only in plan mode.

    Args:
        plan: The plan in Markdown: current state, changes, reasons, risks.
        goal: The goal in one sentence.
        steps: 3 to 8 steps (at most 10), each a piece of work that can be checked on its own.
    """
    if not runtime.state.get("plan_mode"):
        raise ToolError("Not in plan mode")
    board = new_board(goal, steps)  # refuses too many steps, unknown dependencies, cycles
    # Pauses the run; on resume the tool runs again from the top and this returns the answer.
    answer = interrupt({"kind": "plan", "plan": plan, "board": board, "layers": layers(board)})
    decision, call_id = answer["decision"], runtime.tool_call_id

    def reply(text: str) -> ToolMessage:
        return ToolMessage(text, tool_call_id=call_id, name="ExitPlanMode")

    if decision == "feedback":
        return reply(FEEDBACK.format(feedback=answer.get("feedback", "")))
    if decision == "cancel":
        return Command(update={"plan_mode": False, "messages": [reply(CANCELLED)]})
    how = "自动执行，不再逐项审批" if decision == "auto" else "执行时逐项审批"
    update = {"plan_mode": False, "plan": plan, "task_board": board}
    update |= {"auto_approve": decision == "auto"}
    update["messages"] = [reply(APPROVED.format(how=how, board=render(board)))]
    return Command(update=update)


def approval(when=None):
    """Wrap a HumanInTheLoopMiddleware `when`: never ask while planning (whatever is not
    allowed then is refused anyway) or while an approved plan runs automatically."""

    def needs_approval(request) -> bool:
        if request.state.get("plan_mode") or request.state.get("auto_approve"):
            return False
        return when(request) if when else True

    return needs_approval


def not_planning(request) -> bool:
    """`when` for tools an automatic plan does not cover (MCP): ask, except while planning."""
    return not request.state.get("plan_mode")


class PlanModeMiddleware(AgentMiddleware):
    state_schema = PlanState

    def __init__(self, allowed_commands: list[str]):
        super().__init__()
        self.allowed_commands = allowed_commands
        self.tools = [ExitPlanMode]

    async def awrap_model_call(self, request, handler):
        if not request.state.get("plan_mode"):
            tools = [t for t in request.tools if getattr(t, "name", None) != "ExitPlanMode"]
            return await handler(request.override(tools=tools))
        tools = [t for t in request.tools if getattr(t, "name", None) in PLANNING_TOOLS]
        system = request.system_message.text if request.system_message else ""
        return await handler(
            request.override(tools=tools, system_message=SystemMessage(f"{system}\n\n{PROMPT}"))
        )

    async def awrap_tool_call(self, request, handler):
        """The read-only rule itself: a tool the model should not have had is refused too."""
        if not request.state.get("plan_mode"):
            return await handler(request)
        call = request.tool_call
        if call["name"] not in PLANNING_TOOLS:
            reason = f"{call['name']} is not available in plan mode; the project is read only"
        elif call["name"] == "bash" and not allowed_command(
            call["args"].get("command", ""), self.allowed_commands
        ):
            reason = "In plan mode bash only runs these commands: " + ", ".join(
                self.allowed_commands
            )
        else:
            return await handler(request)
        return ToolMessage(
            f"Error: {reason}", tool_call_id=call["id"], name=call["name"], status="error"
        )

    async def aafter_agent(self, state, runtime):
        """Automatic execution covers only the run in which the plan was approved."""
        if state.get("auto_approve"):
            return {"auto_approve": False}
        return None
