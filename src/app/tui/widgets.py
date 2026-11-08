"""The input box, the assistant's turn, a tool call block and the approval panel."""

import asyncio
import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Collapsible, Input, Markdown, Static, TextArea

from ...middleware.task_board import render
from ..stream import Notice, Text, Thinking, ToolCall, ToolResult, Usage


class ChatInput(TextArea):
    """Enter sends, Shift+Enter or Ctrl+J starts a new line, Tab completes a slash command."""

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        Binding("shift+enter,ctrl+j", "newline", "Newline", priority=True),
        Binding("tab", "complete", "Complete", priority=True, show=False),
        Binding("escape", "interrupt", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    class Interrupted(Message):
        """Esc with text in the box: stop the run and send this instead."""

        def __init__(self, text: str):
            super().__init__()
            self.text = text

    def __init__(self, commands: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self.commands = commands  # name → help text, for completion

    def matches(self) -> list[str]:
        text = self.text
        if not text.startswith("/") or " " in text:
            return []
        return [name for name in self.commands if name.startswith(text)]

    def action_submit(self) -> None:
        if self.text.strip():
            self.post_message(self.Submitted(self.text.strip()))
            self.clear()

    def action_interrupt(self) -> None:
        self.post_message(self.Interrupted(self.text.strip()))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        if found := self.matches():
            self.load_text(found[0] + " ")
            self.move_cursor((0, len(found[0]) + 1))


class ToolBlock(Collapsible):
    """One tool call; its result is added when it arrives. Collapsed by default."""

    def __init__(self, name: str, args: dict):
        self.label = f"{name} {summarize_args(args)}"
        super().__init__(
            Static(json.dumps(args, ensure_ascii=False, indent=2), markup=False),
            title=self.label,
            collapsed=True,
            classes="tool-block",
        )

    def finish(self, text: str, error: bool) -> None:
        self.title = ("✗ " if error else "✓ ") + self.label
        self.add_class("tool-failed" if error else "tool-ok")
        shown = text if len(text) <= 4000 else text[:4000] + f"\n… ({len(text) - 4000} more)"
        self.query_one(Collapsible.Contents).mount(Static(shown, markup=False))


def summarize_args(args: dict) -> str:
    """The argument a person most wants to see: path, command or pattern."""
    for key in ("path", "command", "pattern"):
        if isinstance(args.get(key), str):
            return args[key][:80]
    return json.dumps(args, ensure_ascii=False)[:80]


# HumanInTheLoopMiddleware's own description starts like this; then the arguments are shown.
DEFAULT_DESCRIPTION = "Tool execution requires approval"


class ApprovalPanel(Vertical, can_focus=True):
    """Ask about one tool call; `answer` resolves to True (allow) or False (reject)."""

    DEFAULT_CSS = """
    ApprovalPanel {height: auto; padding: 1 2; margin-bottom: 1;
      background: $surface; border: solid $primary-muted;}
    ApprovalPanel:focus {border: solid $primary;}
    ApprovalPanel Static {height: auto;}
    ApprovalPanel .approval-title {color: $primary; text-style: bold;}
    ApprovalPanel .approval-args {color: $text-muted;}
    """
    BINDINGS = [
        Binding("y", "decide(True)", "Allow", priority=True),
        Binding("n,escape", "decide(False)", "Reject", priority=True),
        Binding("up,down,left,right", "toggle", show=False, priority=True),
        Binding("enter", "choose", "Choose", priority=True),
    ]

    def __init__(self, action: dict, who: str = "", take_focus: bool = True):
        super().__init__()
        self.action = action
        self.who = who  # e.g. "子 agent a1" when a subagent asks
        self.take_focus = take_focus
        self.allow = True  # the highlighted option
        self.answer: asyncio.Future = asyncio.get_running_loop().create_future()

    def compose(self) -> ComposeResult:
        who = f"{self.who} " if self.who else ""
        title = f"{who}需要审批：{self.action['name']}"
        yield Static(title, classes="approval-title", markup=False)
        description = self.action.get("description", "")
        if description and not description.startswith(DEFAULT_DESCRIPTION):
            yield Static(description, markup=False)  # written for this tool, e.g. a step to confirm
        else:
            args = json.dumps(self.action["args"], ensure_ascii=False, indent=2)
            yield Static(args[:2000], classes="approval-args", markup=False)
        yield Static(self.options(), id="approval-options", markup=False)

    def options(self) -> str:
        allow, reject = ("[允许 y]", " 拒绝 n ") if self.allow else (" 允许 y ", "[拒绝 n]")
        return f"{allow}   {reject}"

    def on_mount(self) -> None:
        if self.take_focus:
            self.focus()

    def action_toggle(self) -> None:
        self.allow = not self.allow
        self.query_one("#approval-options", Static).update(self.options())

    def action_choose(self) -> None:
        self.action_decide(self.allow)

    def action_decide(self, allow: bool) -> None:
        if not self.answer.done():
            self.answer.set_result(allow)
            label = "已允许" if allow else "已拒绝"
            self.query_one("#approval-options", Static).update(label)

    def cancel_panel(self) -> None:
        """Whoever asked no longer waits (a stopped subagent): the panel stops taking keys."""
        self.answer.cancel()  # does nothing when already cancelled or answered
        self.query_one("#approval-options", Static).update("已取消")


class PlanPanel(Vertical, can_focus=True):
    """Review a submitted plan; `answer` resolves to the ExitPlanMode resume value."""

    DEFAULT_CSS = """
    PlanPanel {height: auto; padding: 1 2; margin-bottom: 1;
      background: $surface; border: solid $primary;}
    PlanPanel Static {height: auto;}
    PlanPanel .approval-title {color: $primary; text-style: bold;}
    PlanPanel #plan-feedback {display: none;}
    """
    BINDINGS = [
        # Not priority bindings: while the feedback box has focus, letters go into it.
        Binding("a", "decide('auto')", "自动批准执行"),
        Binding("r", "decide('review')", "执行但逐项审批"),
        Binding("f", "feedback", "提修改意见"),
        Binding("c", "decide('cancel')", "取消"),
    ]
    LABELS = {"auto": "已批准：自动执行", "review": "已批准：逐项审批", "cancel": "已取消"}

    def __init__(self, review):
        super().__init__()
        self.review = review  # stream.PlanReview
        self.answer: asyncio.Future = asyncio.get_running_loop().create_future()

    def compose(self) -> ComposeResult:
        yield Static("计划审批", classes="approval-title")
        yield Markdown(self.review.plan)
        yield Static(render(self.review.board), markup=False)
        order = " → ".join("、".join(layer) for layer in self.review.layers)
        yield Static(f"分层（同一层可以同时做）：{order}", markup=False)
        yield Static(
            "[a] 自动批准执行   [r] 执行但逐项审批   [f] 提修改意见   [c] 取消",
            id="plan-options",
            markup=False,
        )
        yield Input(placeholder="修改意见，回车发送", id="plan-feedback")

    def on_mount(self) -> None:
        self.focus()

    def action_feedback(self) -> None:
        box = self.query_one("#plan-feedback", Input)
        box.display = True
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() and not self.answer.done():
            self.answer.set_result({"decision": "feedback", "feedback": event.value.strip()})
            event.input.display = False
            self.query_one("#plan-options", Static).update("已提交修改意见")

    def action_decide(self, decision: str) -> None:
        if self.query_one("#plan-feedback", Input).has_focus:
            return  # typing feedback: these letters belong to the text
        if not self.answer.done():
            self.answer.set_result({"decision": decision})
            self.query_one("#plan-options", Static).update(self.LABELS[decision])


class AssistantTurn(Vertical):
    """Draws the events of one turn in order: reasoning, text, tool calls and their results.

    `blocks` maps tool call ids to their ToolBlock; it is shared by every turn on screen,
    because a result can arrive later (a rejection after approval, a reopened session)."""

    def __init__(self, blocks: dict):
        super().__init__(classes="live-turn")
        self.blocks = blocks
        self.stream = None  # the Markdown being written, until a tool call interrupts it
        self.thinking: Static | None = None
        self.thinking_text = ""
        self.usage: Usage | None = None

    def compose(self) -> ComposeResult:
        yield Static("助手", classes="assistant-label")

    async def handle(self, event) -> None:
        if isinstance(event, Text):
            if self.stream is None:
                markdown = Markdown()
                await self.mount(markdown)
                self.stream = Markdown.get_stream(markdown)
            await self.stream.write(event.text)
        elif isinstance(event, Thinking):
            if self.thinking is None:
                self.thinking = Static("", classes="message-thinking", markup=False)
                block = Collapsible(self.thinking, title="思考过程", classes="thinking-block")
                await self.mount(block)
            self.thinking_text += event.text
            self.thinking.update(self.thinking_text)
        elif isinstance(event, ToolCall):
            await self.end_text()
            block = ToolBlock(event.name, event.args)
            self.blocks[event.id] = block
            await self.mount(block)
        elif isinstance(event, ToolResult):
            block = self.blocks.get(event.call_id)
            if block is not None:
                block.finish(event.text, event.error)
        elif isinstance(event, Usage):
            self.usage = event
        elif isinstance(event, Notice):
            await self.end_text()
            await self.mount(Static(event.text, classes="notice-line", markup=False))

    async def end_text(self) -> None:
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None

    async def finish(self) -> None:
        await self.end_text()
        if self.usage is not None:
            usage = self.usage
            hit = usage.cached_tokens / usage.input_tokens if usage.input_tokens else 0
            line = (
                f"输入 {usage.input_tokens:,}（缓存命中 {usage.cached_tokens:,}，{hit:.0%}）"
                f" · 输出 {usage.output_tokens:,} tokens"
            )
            await self.mount(Static(line, classes="message-usage", markup=False))
