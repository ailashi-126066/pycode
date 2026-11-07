"""The pycode window: conversation, notices, input and status line, in the original layout."""

import asyncio
from contextlib import AsyncExitStack
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.types import Command
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ... import __version__
from ...agent import open_agent
from ...compaction.budget import context_tokens
from ...compaction.summarize import count_turns, is_skill, is_summary, is_user_turn
from ...context import Context
from ...sessions.branches import full_history
from ...sessions.catalog import get_session, save_session
from ...tools.files import Files
from ..stream import Approval, Text, events, message_events, settle_cancelled
from . import commands
from .theme import themes
from .widgets import ApprovalPanel, AssistantTurn, ChatInput


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
        Binding("f6", "toggle_theme", "Theme"),
    ]

    def __init__(self, settings, session_id: str | None = None):
        super().__init__()
        self.settings = settings
        self.session_id = session_id or uuid4().hex[:12]
        self.open_session_on_start = session_id is not None
        self.opened = None  # agent.Opened while the agent is open
        self.stack = AsyncExitStack()
        self.blocks: dict = {}  # tool call id → ToolBlock, see AssistantTurn
        self.pending: ApprovalPanel | None = None

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.session_id}}

    def compose(self) -> ComposeResult:
        yield Static(f"PYCODE\nv{__version__}", id="brand", markup=False)
        with VerticalScroll(id="conversation"):
            yield Vertical(id="transcript")
        yield Static("", id="notice", markup=False)
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
        if failed := [server.name for server in self.opened.servers if server.error]:
            self.show_notice(f"MCP 服务器连接失败：{', '.join(failed)}，用 /mcp 查看", "error")

    async def reopen_agent(self) -> None:
        """Open (or, after /model, reopen) the agent and its storage."""
        await self.stack.aclose()
        self.stack = AsyncExitStack()
        self.opened = await self.stack.enter_async_context(open_agent(self.settings))

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
            self.show_notice("模型还在运行，按 Ctrl+C 停止", "warning")
        else:
            self.run_turn(event.text)

    def running(self) -> bool:
        return any(worker.group == "agent" and worker.is_running for worker in self.workers)

    # ---- running the agent ----

    @work(exclusive=True, group="agent")
    async def run_turn(self, text: str, skill: str | None = None) -> None:
        """One user turn; with `skill`, that skill's SKILL.md is pinned in after the message."""
        session = await get_session(self.opened.store, self.session_id)
        title = session.title if session else text
        await save_session(self.opened.store, self.session_id, self.settings.workspace, title)
        await self.write_user(text)
        request = {"messages": [{"role": "user", "content": text}]}
        if skill is not None:
            await self.write_info(f"已加载 Skill：{skill}")
            request["pinned_skills"] = [skill]
            # Rescan the skill directories first: /skill checked the name against the disk,
            # and the list this session cached on its first run may not have it yet.
            request["skills_metadata"] = None
        await self.drive(request)

    @work(exclusive=True, group="agent")
    async def answer_pending(self, approvals: list[Approval]) -> None:
        """A reopened session that was waiting for approval: ask now, then continue."""
        await self.drive(await self.resume_command(approvals))

    async def drive(self, request) -> None:
        """Stream the run into one assistant turn, asking whenever it pauses for approval."""
        skill_dirs = list(self.settings.skill_dirs.values())
        context = Context(files=Files(self.settings.workspace, read_only=skill_dirs))
        turn = AssistantTurn(self.blocks)
        await self.transcript.mount(turn)
        try:
            while request is not None:
                approvals = []
                async for event in events(self.opened.agent, request, self.config, context):
                    if isinstance(event, Approval):
                        approvals.append(event)
                    else:
                        await turn.handle(event)
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

    async def resume_command(self, approvals: list[Approval]) -> Command:
        answers = {}
        for approval in approvals:
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

    # ---- drawing ----

    @property
    def transcript(self) -> Vertical:
        return self.query_one("#transcript", Vertical)

    async def write_user(self, text: str) -> None:
        await self.transcript.mount(Static("你", classes="user-message"))
        await self.transcript.mount(Static(text, markup=False))

    async def write_info(self, text: str, classes: str = "info") -> None:
        await self.transcript.mount(Static(text, classes=classes, markup=False))

    async def clear_transcript(self) -> None:
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
        self.query_one("#status", Static).update(
            f"模型：{provider.name} / {provider.model_id} · "
            f"上下文：{tokens:,} / {provider.context_window:,}（{share}%）· 会话 {self.session_id}"
        )

    # ---- keys ----

    def action_stop(self) -> None:
        if self.pending is not None and not self.pending.answer.done():
            self.pending.action_decide(False)  # stopping at an approval rejects that call
        else:
            self.workers.cancel_group(self, "agent")

    def action_dismiss_notice(self) -> None:
        self.query_one("#notice", Static).display = False

    def action_toggle_theme(self) -> None:
        self.theme = "pycode-light" if self.theme == "pycode-dark" else "pycode-dark"
