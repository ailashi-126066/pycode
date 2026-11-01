# Pi 没有，pycode 新增
"""Inline Plan review sends one operation-bound decision to AgentSession."""

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Markdown, Static


class PlanPanel(Vertical):
    DEFAULT_CSS = """
    PlanPanel { height: auto; padding: 1; border: solid $primary; background: $surface; }
    PlanPanel VerticalScroll { height: 12; }
    PlanPanel Button { width: 1fr; }
    """

    class Responded(Message):
        def __init__(self, answer):
            super().__init__()
            self.answer = answer

    def __init__(self, payload):
        super().__init__(id="plan-inline")
        self.payload = payload

    def compose(self) -> ComposeResult:
        yield Static("计划审批 · " + self.payload["virtual_path"], markup=False)
        with VerticalScroll():
            yield Markdown(self.payload["content"])
        layers = "  ".join(
            f"{index}) {', '.join(layer)}" for index, layer in enumerate(self.payload["layers"], 1)
        )
        yield Static("分层：" + layers, id="plan-layers", markup=False)
        yield Static(
            "关键路径：" + " → ".join(self.payload["critical_path"]),
            id="plan-critical-path",
            markup=False,
        )
        yield Button("自动批准执行", id="plan-auto", variant="primary")
        yield Button("执行但逐项审批", id="plan-review")
        yield Input(placeholder="反馈：继续修改当前计划", id="plan-feedback-text")
        yield Button("返回反馈修改计划", id="plan-feedback")
        yield Static("", id="plan-review-status", markup=False)

    def on_button_pressed(self, event):
        choices = {"plan-auto": "auto", "plan-review": "review", "plan-feedback": "feedback"}
        decision = choices.get(event.button.id)
        if decision is None:
            return
        event.stop()
        feedback = self.query_one("#plan-feedback-text", Input).value.strip()
        if decision == "feedback" and not feedback:
            self.query_one("#plan-review-status", Static).update("请填写反馈后提交")
            return
        answer = {
            "operation_id": self.payload["operation_id"],
            "plan_id": self.payload["plan_id"],
            "decision": decision,
        }
        if decision == "feedback":
            answer["feedback"] = feedback
        self.post_message(self.Responded(answer))
