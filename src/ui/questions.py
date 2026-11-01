# Pi 没有，pycode 新增
# Source: MewCode local askuser_dialog.py.
# SHA-256: aa6315c63c290b3e5afccb9d3319016c3830d9e56e7fc010543dd300bd8d96cc
# Adaptation: named typed answers, Textual tabs/controls and review; no memory Future.
"""Question forms submit decisions for persisted LangGraph interrupts."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, Select, Static, TabbedContent, TabPane

from ..kernel import StorageError, clone_value, json_dumps
from ..tools import validate_question_answer


class QuestionPanel(Vertical):
    DEFAULT_CSS = """
    QuestionPanel {height: auto; border: solid $primary; background: $surface; padding: 1;}
    QuestionPanel TabPane {height: auto;}
    QuestionPanel Static {height: auto;}
    QuestionPanel Horizontal {height: auto;}
    QuestionPanel .question-description {color: $text-muted;}
    QuestionPanel #question-error {color: $error;}
    """

    class Responded(Message):
        def __init__(self, answer, agent_id=None):
            super().__init__()
            self.agent_id = agent_id
            self.answer = answer

    def __init__(self, payload, *, agent_id=None):
        super().__init__(id="askuser-inline")
        self._payload = clone_value(payload)
        self._agent_id = agent_id
        self._reviewed = None

    def compose(self) -> ComposeResult:
        with TabbedContent(id="question-tabs"):
            for index, question in enumerate(self._payload["questions"]):
                with TabPane(question["title"], id=f"question-{index}"):
                    yield Static(question["message"], markup=False)
                    if question["type"] == "text":
                        yield Input(placeholder="Answer", id=f"answer-{index}")
                    elif question["type"] in {"radio", "select"}:
                        yield Select(
                            [(option["label"], option["value"]) for option in question["options"]],
                            prompt="Choose an option",
                            id=f"answer-{index}",
                        )
                    else:
                        for option_index, option in enumerate(question["options"]):
                            yield Checkbox(option["label"], id=f"choice-{index}-{option_index}")
                    if question["type"] != "text":
                        for option in question["options"]:
                            if option["description"]:
                                yield Static(
                                    option["label"] + ": " + option["description"],
                                    markup=False,
                                    classes="question-description",
                                )
                        yield Input(
                            placeholder="Other — custom answer (optional)", id=f"other-{index}"
                        )
        yield Static("", id="question-error", markup=False)
        yield Static("", id="question-review", markup=False)
        with Horizontal():
            yield Button("Review answers", id="question-check", variant="primary")
            yield Button("Submit answers", id="question-submit", disabled=True, variant="primary")
            yield Button("Cancel question", id="question-cancel", variant="error")

    def answers(self):
        answers = {}
        for index, question in enumerate(self._payload["questions"]):
            if question["type"] == "text":
                value = self.query_one(f"#answer-{index}", Input).value
            else:
                other = self.query_one(f"#other-{index}", Input).value
                if question["type"] == "checkbox":
                    value = [
                        option["value"]
                        for option_index, option in enumerate(question["options"])
                        if self.query_one(f"#choice-{index}-{option_index}", Checkbox).value
                    ]
                    if other:
                        value.append({"other": other})
                elif other:
                    value = {"other": other}
                else:
                    value = self.query_one(f"#answer-{index}", Select).value
            answers[question["name"]] = value
        return {"operation_id": self._payload["operation_id"], "answers": answers}

    def reset_review(self):
        self._reviewed = None
        self.query_one("#question-submit", Button).disabled = True

    def on_input_changed(self, event):
        self.reset_review()

    def on_select_changed(self, event):
        self.reset_review()

    def on_checkbox_changed(self, event):
        self.reset_review()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "question-cancel":
            self.post_message(
                self.Responded(
                    {"operation_id": self._payload["operation_id"], "decision": "cancel"},
                    self._agent_id,
                )
            )
        elif event.button.id == "question-check":
            try:
                answer = self.answers()
                validate_question_answer(self._payload, answer)
            except StorageError as error:
                self.query_one("#question-error", Static).update(str(error))
                return
            self._reviewed = clone_value(answer)
            self.query_one("#question-error", Static).update("")
            self.query_one("#question-review", Static).update(
                json_dumps(answer["answers"], ensure_ascii=False)
            )
            self.query_one("#question-submit", Button).disabled = False
        elif event.button.id == "question-submit" and self._reviewed is not None:
            self.post_message(self.Responded(clone_value(self._reviewed), self._agent_id))
