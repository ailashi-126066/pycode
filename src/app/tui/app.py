"""The pycode window: conversation, notices, input and status line, in the original layout."""

import asyncio
from contextlib import AsyncExitStack
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static
from textual.worker import WorkerState

from ... import __version__
from ...agent import open_agent
from ...compaction.budget import context_tokens
from ...compaction.summarize import count_turns, is_skill, is_steer, is_summary, is_user_turn
from ...context import Context
from ...middleware.subagents import Subagent, notification
from ...middleware.task_board import DONE, render
from ...sessions.branches import full_history, settle_cancelled
from ...sessions.catalog import get_session, save_session
from ...tools.files import Files
from ..stream import (
    Approval,
    PlanReview,
    Text,
    ToolResult,
    events,
    message_events,
)
from . import commands
from .theme import themes
from .widgets import ApprovalPanel, AssistantTurn, ChatInput, PlanPanel

BOARD_TOOLS = {"ExitPlanMode", "TaskWrite", "TaskUpdate"}


def subagent_message(text: str) -> HumanMessage:
    """Finished subagents, as a message to the main agent; marked so history draws it apart."""
    return HumanMessage(text, additional_kwargs={"pycode_subagent": True})


class PycodeApp(App):
    TITLE = "pycode"
    CSS = """
    Screen {background: $background; color: $foreground;}
    #brand {height: 3; padding: 0 3; color: $primary; text-style: bold;}
    #conversation {height: 1fr; padding: 0 3; scrollbar-size-vertical: 1;
      scrollbar-color: $text-muted; scrollbar-background: $background;}
    #transcript {height: auto;}
    #transcript Static {height: auto;}
    #transcript Markdown {margin-bottom: 1;}
    #notice {display: none; height: auto; max-height: 6; padding: 0 3;}
    #notice.error {color: $error;}
    #notice.success {color: $success;}
    #notice.warning {color: $warning;}
    #tasks {display: none; height: auto; max-height: 14; padding: 0 1; margin: 0 3;
      border: round $primary-muted; color: $text-muted;}
    #entry {height: auto; padding: 0 3;}
    #completion {display: none; height: auto; max-height: 8; color: $text-muted;}
    #chat-input {height: 3; min-height: 3; max-height: 10; border: none; background: $surface;}
    #chat-input:focus {border: none;}
    #status {height: 1; padding: 0 3; color: $text-muted;}
    .user-message {color: $primary; text-style: bold; margin-top: 1;}
    .assistant-label {color: $primary; text-style: bold; margin-top: 1;}
    .message-usage {color: $text-muted; margin-bottom: 1;}
    .message-thinking {color: $text-muted; text-style: italic;}
    .thinking-block {height: auto; margin-bottom: 1; color: $text-muted;}
    .tool-block {height: auto; margin-bottom: 1; color: $text-muted;}
    .live-turn {height: auto; margin-top: 1;}
    .tool-failed > CollapsibleTitle {color: $error;}
    .tool-ok > CollapsibleTitle {color: $success;}
    .notice-line {color: $success; margin-bottom: 1;}
    .marker {color: $warning; margin: 1 0;}
    .info {color: $text-muted;}
    """
    BINDINGS = [
        Binding("ctrl+c", "stop", "Stop", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("escape", "dismiss_notice", show=False),
        Binding("f6", "toggle_theme", "Theme", priority=True),
        Binding("f7", "toggle_tasks", "Tasks", priority=True),
    ]

    def __init__(self, settings, session_id: str | None = None):
        super().__init__()
        self.settings = settings
        self.session_id = session_id or uuid4().hex[:12]
        self.open_session_on_start = session_id is not None
        self.opened = None  # agent.Opened while the agent is open
        self.stack = AsyncExitStack()
        self.blocks: dict = {}  # tool call id → ToolBlock, see AssistantTurn
        self.pending: ApprovalPanel | PlanPanel | None = None
        self.tasks_hidden = False  # F7
        self.asking = (
            asyncio.Lock()
        )  # one approval panel at a time, the main agent's or a subagent's
        self.notices: list[str] = []  # finished subagents waiting to be handed to the main agent
        self.next_turn: str | None = None  # sent once the run in progress has ended

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.session_id}}

    def compose(self) -> ComposeResult:
        yield Static(f"PYCODE\nv{__version__}", id="brand", markup=False)
        with VerticalScroll(id="conversation"):
            yield Vertical(id="transcript")
        yield Static("", id="notice", markup=False)
        yield Static("", id="tasks", markup=False)
        with Vertical(id="entry"):
            yield Static("", id="completion", markup=False)
            yield ChatInput(commands.COMMANDS, id="chat-input")
        yield Static("", id="status", markup=False)

    async def on_mount(self) -> None:
        for theme in themes():
            self.register_theme(theme)
        self.theme = "pycode-dark"
        self.query_one("#conversation", VerticalScroll).anchor()  # follow new content
        try:
            await self.reopen_agent()
        except ValueError as error:  # bad provider file or missing API key
            self.show_notice(str(error), "error")
            self.query_one(ChatInput).disabled = True
            return
        if self.open_session_on_start:
            await commands.run(self, f"/open {self.session_id}")
        await self.refresh_status()
        self.query_one(ChatInput).focus()
        problems = []
        if self.settings.sandbox and not self.opened.sandboxed:
            problems.append("沙箱不可用：bash 命令不受系统限制，仍然需要审批")
        if failed := [server.name for server in self.opened.servers if server.error]:
            problems.append(f"MCP 服务器连接失败：{', '.join(failed)}，用 /mcp 查看")
        if problems:
            self.show_notice("\n".join(problems), "warning")

    async def reopen_agent(self) -> None:
        """Open (or, after /model, reopen) the agent and its storage."""
        await self.stack.aclose()
        self.stack = AsyncExitStack()
        self.opened = await self.stack.enter_async_context(open_agent(self.settings))
        self.opened.subagents.notify = self.subagent_finished
        self.opened.subagents.ask = self.ask_for_subagent

    async def on_unmount(self) -> None:
        await self.stack.aclose()

    # ---- input ----

    def on_text_area_changed(self, event) -> None:
        box = self.query_one(ChatInput)
        found = box.matches()
        completion = self.query_one("#completion", Static)
        completion.update("\n".join(f"{name}  {commands.COMMANDS[name]}" for name in found))
        completion.display = bool(found)

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        self.query_one("#completion", Static).display = False
        if event.text.startswith("/"):
            await commands.run(self, event.text)
        elif self.running():
            # Steering: the agent reads it before its next step (middleware/steering.py).
            self.opened.steering.put(self.session_id, event.text)
            await self.write_user(event.text, steer=True)
            self.show_notice(
                "已插话：模型下一步会看到。Esc 停止并立即发送，Ctrl+C 只停止", "success"
            )
        else:
            self.run_turn(event.text)

    async def on_chat_input_interrupted(self, event: ChatInput.Interrupted) -> None:
        """Esc: with words while the model runs, stop this run and send them as the next turn."""
        if not (event.text and self.running()):
            self.action_dismiss_notice()
            return
        self.query_one(ChatInput).clear()
        await self.write_user(event.text)
        self.next_turn = event.text
        self.action_stop()

    def running(self) -> bool:
        return any(worker.group == "agent" and worker.is_running for worker in self.workers)

    # ---- running the agent ----

    @work(exclusive=True, group="agent")
    async def run_turn(
        self, text: str, skill: str | None = None, plan: bool = False, echo: bool = True
    ) -> None:
        """One user turn; with `skill`, that skill's SKILL.md is pinned in after the message;
        with `plan`, the turn starts plan mode (see middleware/plan_mode.py)."""
        session = await get_session(self.opened.store, self.session_id)
        title = session.title if session else text
        await save_session(self.opened.store, self.session_id, self.settings.workspace, title)
        if echo:  # steering words that became this turn are on screen already
            await self.write_user(text)
        request = {"messages": [{"role": "user", "content": text}]}
        if skill is not None:
            await self.write_info(f"已加载 Skill：{skill}")
            request["pinned_skills"] = [skill]
            # Rescan the skill directories first: /skill checked the name against the disk,
            # and the list this session cached on its first run may not have it yet.
            request["skills_metadata"] = None
        if plan:
            await self.write_info("进入 Plan 模式：只读规划，提交计划后由你审批")
            request["plan_mode"] = True
        await self.drive(request)

    @work(exclusive=True, group="agent")
    async def answer_pending(self, approvals: list[Approval]) -> None:
        """A reopened session that was waiting for approval: ask now, then continue."""
        await self.drive(await self.resume_command(approvals))

    async def drive(self, request) -> None:
        """Stream the run into one assistant turn, asking whenever it pauses for approval."""
        skill_dirs = list(self.settings.skill_dirs.values())
        context = Context(
            files=Files(self.settings.workspace, read_only=skill_dirs),
            sandboxed=self.opened.sandboxed,
        )
        turn = AssistantTurn(self.blocks)
        await self.transcript.mount(turn)
        try:
            while request is not None:
                approvals = []
                async for event in events(self.opened.agent, request, self.config, context):
                    if isinstance(event, Approval | PlanReview):
                        approvals.append(event)
                        continue
                    await turn.handle(event)
                    if isinstance(event, ToolResult) and event.name in BOARD_TOOLS:
                        await self.refresh_tasks()
                await turn.end_text()
                request = await self.resume_command(approvals) if approvals else None
        except asyncio.CancelledError:
            await turn.end_text()
            await settle_cancelled(self.opened.agent, self.config)
            self.show_notice("已停止", "warning")
            raise
        finally:
            await turn.finish()
            await self.refresh_status()
            # Words that came after its last model call: they start the next turn.
            if waiting := self.opened.steering.take(self.session_id):
                self.next_turn = "\n\n".join([*waiting, *filter(None, [self.next_turn])])

    async def resume_command(self, approvals: list[Approval | PlanReview]) -> Command:
        await self.refresh_status()  # the run is paused: show its mode and board while asking
        async with self.asking:
            return await self.ask_all(approvals)

    async def ask_all(self, approvals: list[Approval | PlanReview]) -> Command:
        answers = {}
        for approval in approvals:
            if isinstance(approval, PlanReview):
                self.pending = PlanPanel(approval)
                await self.transcript.mount(self.pending)
                answers[approval.interrupt_id] = await self.pending.answer
                continue
            decisions = []
            for action in approval.actions:
                self.pending = ApprovalPanel(action)
                await self.transcript.mount(self.pending)
                allowed = await self.pending.answer
                decisions.append(
                    {"type": "approve"}
                    if allowed
                    else {"type": "reject", "message": "用户拒绝执行"}
                )
            answers[approval.interrupt_id] = {"decisions": decisions}
        self.pending = None
        self.query_one(ChatInput).focus()
        return Command(resume=answers)

    # ---- subagents ----

    async def ask_for_subagent(self, agent: Subagent, value: dict) -> dict:
        """A subagent paused for approval: ask here, in the main window."""
        async with self.asking:
            decisions = []
            try:
                for action in value["action_requests"]:
                    # A background subagent asks at any time: do not take the keys from
                    # someone typing, or their Enter would answer the panel.
                    typing = bool(self.query_one(ChatInput).text)
                    who = f"子 agent {agent.name}"
                    self.pending = ApprovalPanel(action, who=who, take_focus=not typing)
                    await self.transcript.mount(self.pending)
                    allowed = await self.pending.answer
                    decisions.append(
                        {"type": "approve"}
                        if allowed
                        else {"type": "reject", "message": "用户拒绝执行"}
                    )
            except asyncio.CancelledError:
                # StopAgent stopped the subagent while its panel was open (asyncio has
                # cancelled the panel's answer along with it).
                if self.pending is not None:
                    self.pending.cancel_panel()
                raise
            finally:
                self.pending = None
                self.query_one(ChatInput).focus()  # back to typing, as after the main agent's
            return {"decisions": decisions}

    async def subagent_finished(self, agent: Subagent) -> None:
        """Hand the result to the main agent of its session: now if it is idle, else after."""
        text = notification(agent)
        if agent.parent != self.session_id:  # the user opened another session meanwhile
            config = {"configurable": {"thread_id": agent.parent}}
            await self.opened.agent.aupdate_state(config, {"messages": [subagent_message(text)]})
            self.show_notice(
                f"会话 {agent.parent} 的子 agent {agent.name} 已结束，结果已存入那个会话"
            )
            return
        self.notices.append(text)
        if not self.running():
            self.wake()

    @work(exclusive=True, group="agent")
    async def wake(self) -> None:
        texts, self.notices = self.notices, []
        await self.write_info("── 子 agent 已结束，结果交给主 agent ──", "marker")
        await self.drive({"messages": [subagent_message("\n\n---\n\n".join(texts))]})

    def on_worker_state_changed(self, event) -> None:
        """A main run ended: words typed meanwhile, then finished subagents, go on now."""
        finished = event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED)
        if event.worker.group != "agent" or not finished or self.running():
            return
        if self.next_turn is not None:
            text, self.next_turn = self.next_turn, None
            self.run_turn(text, echo=False)
        elif self.notices:
            self.wake()

    # ---- drawing ----

    @property
    def transcript(self) -> Vertical:
        return self.query_one("#transcript", Vertical)

    async def write_user(self, text: str, steer: bool = False) -> None:
        label = "你（插话）" if steer else "你"
        await self.transcript.mount(Static(label, classes="user-message"))
        await self.transcript.mount(Static(text, markup=False))

    async def write_info(self, text: str, classes: str = "info") -> None:
        await self.transcript.mount(Static(text, classes=classes, markup=False))

    async def clear_transcript(self) -> None:
        if isinstance(self.pending, ApprovalPanel) and not self.pending.answer.done():
            self.pending.action_decide(False)  # its panel is about to go: reject that call
        await self.transcript.remove_children()
        self.blocks.clear()

    async def show_history(self) -> None:
        """The whole branch, including messages compaction replaced, with a marker for it."""
        turn = None
        for message in await full_history(self.opened.agent, self.config):
            if is_summary(message):
                turns = count_turns([message])
                text = f"── 上下文已压缩：以上 {turns} 轮在模型的上下文里换成了摘要 ──"
                await self.write_info(text, "marker")
                continue
            if is_skill(message):
                await self.write_info(f"已加载 Skill：{message.additional_kwargs['skill']['name']}")
                continue
            if is_steer(message):  # stays inside the assistant's turn, like when it was typed
                await self.write_user(message.text, steer=True)
                continue
            if message.additional_kwargs.get("pycode_subagent"):
                await self.write_info("── 子 agent 已结束，结果交给主 agent ──", "marker")
                turn = None
                continue
            if is_user_turn(message):
                if turn is not None:
                    await turn.finish()
                turn = None
                await self.write_user(message.text)
                continue
            if turn is None:
                turn = AssistantTurn(self.blocks)
                await self.transcript.mount(turn)
            if isinstance(message, AIMessage) and message.text:
                await turn.handle(Text(message.text))
            for event in message_events("model", message):
                await turn.handle(event)
        if turn is not None:
            await turn.finish()

    def show_notice(self, text: str, kind: str = "success") -> None:
        notice = self.query_one("#notice", Static)
        notice.update(text)
        notice.set_classes(kind)
        notice.display = True

    async def refresh_status(self) -> None:
        provider = self.opened.provider
        state = await self.opened.agent.aget_state(self.config)
        tokens = context_tokens(state.values.get("messages", []), self.opened.compaction.overhead)
        share = tokens * 100 // provider.context_window
        sandbox = "开" if self.opened.sandboxed else "关"
        mode = " · 规划中" if state.values.get("plan_mode") else ""
        mode += " · 自动执行计划" if state.values.get("auto_approve") else ""
        self.query_one("#status", Static).update(
            f"模型：{provider.name} / {provider.model_id} · "
            f"上下文：{tokens:,} / {provider.context_window:,}（{share}%）· "
            f"沙箱：{sandbox}{mode} · 会话 {self.session_id}"
        )
        await self.refresh_tasks()

    async def refresh_tasks(self) -> None:
        """The board above the input while it has open steps, unless hidden with F7."""
        state = await self.opened.agent.aget_state(self.config)
        board = state.values.get("task_board")
        panel = self.query_one("#tasks", Static)
        if board:
            panel.update(render(board))
        unfinished = board and any(s["status"] not in DONE for s in board["steps"])
        panel.display = bool(unfinished) and not self.tasks_hidden

    # ---- keys ----

    def action_stop(self) -> None:
        if isinstance(self.pending, PlanPanel) and not self.pending.answer.done():
            self.pending.answer.set_result({"decision": "cancel"})  # stopping cancels the plan
        elif self.pending is not None and not self.pending.answer.done():
            self.pending.action_decide(False)  # stopping at an approval rejects that call
        else:
            self.workers.cancel_group(self, "agent")

    def action_dismiss_notice(self) -> None:
        self.query_one("#notice", Static).display = False

    async def action_toggle_tasks(self) -> None:
        self.tasks_hidden = not self.tasks_hidden
        await self.refresh_tasks()

    def action_toggle_theme(self) -> None:
        self.theme = "pycode-light" if self.theme == "pycode-dark" else "pycode-dark"
