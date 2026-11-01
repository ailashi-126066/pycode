# Source: MewCode local app.py and tests/test_tui_framework.py; hashes in docs/source-map.md.
# Adaptation: only public application commands and native projections; owned UI tasks and Events.
# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""Interactive terminal presentation shares AgentSession with non-interactive CLI."""

import asyncio
import logging
import shlex
from typing import Any
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message as UIMessage
from textual.widgets import Button, Collapsible, Markdown, Static

from pycode import __version__
from pycode.kernel import DomainError, Message, RunResult, StorageError, json_dumps
from pycode.ui.adapters.agents import AgentsPanel
from pycode.ui.adapters.approval import ApprovalPanel
from pycode.ui.adapters.chat_input import ChatInput
from pycode.ui.adapters.commands import CommandRegistry
from pycode.ui.adapters.completion import CompletionPopup
from pycode.ui.adapters.memory import MemoryPanel
from pycode.ui.adapters.model_picker import ModelPicker
from pycode.ui.adapters.plans import PlanPanel
from pycode.ui.adapters.questions import QuestionPanel
from pycode.ui.adapters.recovery import RevertPanel
from pycode.ui.adapters.theme import themes
from pycode.ui.adapters.tool_output import ToolOutputCard
from pycode.ui.adapters.usage import UsageScreen
from pycode.ui.ports.ui import InteractiveSession, InteractiveSessions


def abbreviated_tokens(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def context_text(report):
    if report["tokens"] is None or report["window"] is None:
        return "上下文：等待首轮请求"
    return (
        "上下文："
        f"{report['fraction'] * 100:.1f}% "
        f"({abbreviated_tokens(report['tokens'])} / {abbreviated_tokens(report['window'])})"
    )


def content_text(message, kind):
    """Join the plain message content of one block kind ("text" or "thinking")."""
    content = message["content"]
    if isinstance(content, str):
        return content if kind == "text" else ""
    separator = "" if kind == "text" else "\n\n"
    return separator.join(block["text"] for block in content if block["type"] == kind)


def reply_metadata(message):
    metadata = message.get("provider_metadata", {})
    turn = metadata.get("pycode_turn")
    parts = [f"第 {turn} 轮"] if turn is not None else []
    usage = message.get("usage")
    if usage is None:
        return " · ".join(parts)
    fields = (("输入", "input_tokens"), ("输出", "output_tokens"), ("推理", "reasoning_tokens"))
    for label, field in fields:
        value = usage.get(field)
        if value is not None:
            parts.append(f"{label} {value}")
    total = usage.get("total_tokens")
    if total is not None:
        parts.append(f"共 {total}")
    cache_hit = usage.get("cache_read_tokens")
    input_tokens = usage.get("input_tokens")
    if cache_hit is not None and input_tokens not in {None, 0}:
        parts.append(f"缓存命中 {cache_hit / input_tokens * 100:.1f}%")
    return " · ".join(parts)


def plan_status(plan):
    if plan is None:
        return ""
    return {
        "planning": "计划：规划中（只读）",
        "review": "计划：等待确认",
        "auto": "计划：已批准执行",
    }.get(plan["mode"], "")


def thinking_preview(text):
    lines = text.splitlines()
    return ("…\n" if len(lines) > 3 else "") + "\n".join(lines[-3:])


def tool_preview(text):
    lines = text.splitlines()
    return ("…\n" if len(lines) > 3 else "") + "\n".join(lines[-3:])


def tool_title(result):
    state = "失败" if result.get("is_error") else "已完成"
    return result["name"] + " · " + state


class PyApp(App):
    TITLE = "pycode"
    CSS = """
    Screen {background: $background; color: $foreground;}
    #brand {height: 3; padding: 0 3; color: $primary; text-style: bold;}
    #main {height: 1fr;}
    #conversation {height: 1fr; padding: 0 3; scrollbar-size-vertical: 1;
      scrollbar-color: $text-muted; scrollbar-color-hover: $text-muted;
      scrollbar-color-active: $text-muted; scrollbar-background: $background;}
    #back-to-bottom {display: none; height: 1; color: $primary; background: $background;
      border: none;}
    #conversation Static {height: auto;}
    #conversation Markdown {margin-bottom: 1;}
    #transcript, #pending {height: auto;}
    .approval-block {height: auto; margin-bottom: 1; color: $text-muted;}
    .approval-block > CollapsibleTitle {color: $text-muted;}
    #entry {height: auto; padding: 0 3;}
    #chat-input {height: 3; min-height: 3; max-height: 10; border: none;}
    #chat-input:focus {border: none;}
    #status {height: 1; padding: 0 3; color: $text-muted;}
    #notice {display: none; height: auto; max-height: 6; padding: 0 3;}
    #notice.error {color: $error;}
    #notice.success {color: $success;}
    #notice.warning {color: $warning;}
    .user-message {color: $primary; text-style: bold; margin-top: 1;}
    .assistant-label {color: $primary; text-style: bold; margin-top: 1;}
    .message-usage {color: $text-muted; margin-bottom: 1;}
    .message-thinking {color: $text-muted; text-style: italic;}
    .thinking-block {height: auto; margin-bottom: 1; color: $text-muted;}
    .thinking-block > CollapsibleTitle {color: $text-muted;}
    .tool-block {height: auto; margin-bottom: 1; color: $text-muted;}
    .tool-block > CollapsibleTitle {color: $text-muted;}
    .live-turn {height: auto; margin-top: 1;}
    .live-phase {color: $primary; text-style: bold;}
    .live-thinking {color: $text-muted; text-style: italic; display: none;}
    .live-text {height: auto;}
    .tool-failed {color: $error;}
    .tool-ok {color: $success;}
    """
    BINDINGS = [
        Binding("ctrl+c", "stop", "Stop", priority=True),
        Binding("ctrl+q", "close", "Quit", priority=True),
        Binding("escape", "dismiss_notice", show=False),
        Binding("f6", "toggle_theme", "Theme"),
    ]

    class RuntimeEvent(UIMessage):
        def __init__(self, event):
            super().__init__()
            self.event = event

    def __init__(
        self,
        sessions: InteractiveSessions,
        *,
        session_id=None,
        sandbox_status="OS 沙箱不可用；使用权限审批",
    ):
        super().__init__()
        self._sessions = sessions
        self._initial_session_id = session_id
        self._sandbox_status = sandbox_status
        self.session: InteractiveSession | None = None
        self._render_failed = False
        self.session_id = None
        self.state: dict[str, Any] | None = None
        self.ready = asyncio.Event()
        self.last_error = None
        self._memory_report = None
        self._show_agents = False
        self._team_tasks = set()
        self._operation = None
        self._closed = False
        self._live_text = ""
        self._live_thinking = ""
        self._live_turn = None
        self._live_phase = None
        self._live_thinking_widget = None
        self._live_text_widget = None
        self._pending_plan = None
        self._displayed_entries = []
        self._displayed_session = None
        self._running_tools = {}
        self.commands = CommandRegistry()
        self.scroll_sensitivity_y = 1.0
        for theme in themes():
            self.register_theme(theme)
        self.theme = "pycode-dark"

    @property
    def active_session(self) -> InteractiveSession:
        session = self.session
        if session is None:
            raise StorageError("session_unavailable", "Open a session before this operation")
        return session

    @property
    def view_state(self) -> dict[str, Any]:
        state = self.state
        if state is None:
            raise StorageError("session_unavailable", "Session state is not ready")
        return state

    @property
    def busy(self):
        return self._operation is not None and not self._operation.done()

    def compose(self) -> ComposeResult:
        yield Static(f"PYCODE\nv{__version__}", id="brand", markup=False)
        with Vertical(id="main"):
            with VerticalScroll(id="conversation"):
                yield Vertical(id="transcript")
                yield Vertical(id="pending")
            yield Button("回到最新消息", id="back-to-bottom")
        yield Static("", id="notice", markup=False)
        with Vertical(id="entry"):
            yield CompletionPopup(id="command-completion")
            yield ModelPicker(id="model-picker")
            yield ChatInput(id="chat-input", disabled=True)
        yield Static("模型：等待首轮请求 · 上下文：等待首轮请求", id="status", markup=False)

    async def on_mount(self):
        try:
            entry = self.query_one("#chat-input", ChatInput)
            entry.registry = self.commands
            entry.popup = self.query_one("#command-completion", CompletionPopup)
            entry.model_picker = self.query_one("#model-picker", ModelPicker)
            entry.model_selected = self.select_model_from_picker
            entry.dismiss_transient = self.action_dismiss_notice
            self._sandbox_status = (await self._sessions.sandbox_status()).reason
            await self.switch_session(self._initial_session_id)
        except DomainError as error:
            self.last_error = error
            self.notice(str(error), "error")
        finally:
            self.ready.set()

    async def consume(self, event):
        # Summary tasks intentionally run in a clean Context. UI updates occur in
        # the App's message pump, never inside model/summary execution callbacks.
        self.post_message(self.RuntimeEvent(event))

    async def on_py_app_runtime_event(self, message):
        if self._render_failed:
            return
        try:
            await self.render_runtime_event(message)
        except Exception as error:
            self.render_failed(error)

    def render_failed(self, error):
        self._render_failed = True
        logging.getLogger("pycode.ui").error(
            "Disabled failed UI renderer: %s", error, exc_info=True
        )
        try:
            self.notice(
                "Display failed; saved history remains available. Reopen the session to refresh.",
                "error",
            )
        except Exception:
            logging.getLogger("pycode.ui").exception("Could not display renderer failure")

    async def render_runtime_event(self, message):
        event = message.event
        if self._closed or event.session_id != self.session_id:
            return
        if event.kind == "subagent":
            await self.refresh_state()
            if event.data["type"] == "wake":
                self.maybe_wake_team()
        elif event.kind == "message_start" and event.data["message"]["role"] == "assistant":
            self._live_text = self._live_thinking = ""
            await self.start_live_reply()
        elif event.kind == "message_update" and event.data["kind"] == "text":
            self._live_text += event.data["delta"]
            if self._live_phase is not None:
                self._live_phase.update("PYCODE")
            if self._live_text_widget is not None:
                self._live_text_widget.update(self._live_text)
            self.follow_conversation()
        elif event.kind == "message_update":
            self._live_thinking += event.data["delta"]
            if self._live_thinking_widget is not None:
                self._live_thinking_widget.display = True
                self._live_thinking_widget.update(
                    "思考中：\n" + thinking_preview(self._live_thinking)
                )
            self.follow_conversation()
        elif event.kind == "message_end":
            role = event.data["message"]["role"]
            if role == "assistant":
                await self.clear_live_reply()
            await self.render_history()
            if role == "user":
                self.follow_conversation(force=True)
        elif event.kind == "tool_execution_start":
            # One card per call: mounted at start, removed at the matching end.
            body = Static("正在运行…", markup=False, classes="message-usage")
            card = Collapsible(
                body,
                title=event.data["tool_name"] + " · 运行中",
                collapsed=True,
                classes="tool-block",
            )
            await self.transcript().mount(card)
            self._running_tools[event.data["tool_call_id"]] = (card, body)
            self.follow_conversation()
        elif event.kind == "tool_execution_update" and "delta" in event.data:
            # A call approved after reopening the session ends without a start here.
            running = self._running_tools.get(event.data["tool_call_id"])
            if running is not None:
                running[1].update("最近输出：\n" + tool_preview(event.data["delta"]))
        elif event.kind == "tool_execution_end":
            running = self._running_tools.pop(event.data["tool_call_id"], None)
            if running is not None:
                await running[0].remove()
        elif event.kind == "agent_end" and "error" in event.data:
            self.notice(event.data["error"], "error")
        elif event.kind == "compaction":
            self.notice("上下文整理：" + event.data["status"])
        elif event.kind == "notification" and event.data["kind"] == "hook":
            self.notice(event.data["message"], None if event.data["success"] else "error")
        elif event.kind == "result":
            self.notice("")

    def notice(self, text, severity=None):
        widget = self.query_one("#notice", Static)
        widget.set_classes(severity or "")
        widget.update(text)
        widget.display = bool(text)

    def action_dismiss_notice(self):
        self.notice("")

    def follow_conversation(self, *, force=False):
        area = self.query_one("#conversation", VerticalScroll)
        if force or area.is_vertical_scroll_end:
            self.call_after_refresh(area.scroll_end, animate=False)
            self.query_one("#back-to-bottom", Button).display = False
        else:
            self.query_one("#back-to-bottom", Button).display = True

    def transcript(self):
        return self.query_one("#transcript", Vertical)

    def collapse_parent(self, widget):
        for parent in widget.ancestors:
            if isinstance(parent, Collapsible):
                parent.collapsed = True
                return

    async def start_live_reply(self):
        await self.clear_live_reply()
        turn = Vertical(classes="live-turn")
        await self.transcript().mount(turn)
        phase = Static("PYCODE 正在思考…", markup=False, classes="live-phase")
        thinking = Static("", markup=False, classes="live-thinking")
        text = Static("", markup=False, classes="live-text")
        await turn.mount(phase, thinking, text)
        self._live_turn = turn
        self._live_phase = phase
        self._live_thinking_widget = thinking
        self._live_text_widget = text
        self.follow_conversation(force=True)

    async def clear_live_reply(self):
        if self._live_turn is not None:
            await self._live_turn.remove()
        self._live_turn = None
        self._live_phase = None
        self._live_thinking_widget = None
        self._live_text_widget = None

    async def switch_session(self, session_id=None):
        await self.stop_active()
        target = session_id or uuid4().hex
        opened = await self._sessions.open(target, create=session_id is None, consumer=self)
        try:
            if self.session is not None:
                await self.active_session.aclose()
        except BaseException:
            await opened.aclose()
            raise
        identity = (await opened.inspect())["snapshot"]["header"]["session_id"]
        self.session, self.session_id = opened, identity
        self._render_failed = False
        self._memory_report = None
        self._pending_plan = None
        self._running_tools = {}
        self._live_text = self._live_thinking = ""
        await self.clear_live_reply()
        await self.refresh_state()

    async def render_history(self):
        if self.session is None or self._closed:
            return
        state = await self.active_session.inspect(presentation=True)
        area = self.query_one("#conversation", VerticalScroll)
        transcript = self.transcript()
        history = state["history"]
        identifiers = [item["entry_id"] for item in history]
        at_bottom = area.is_vertical_scroll_end
        added = len(identifiers) > len(self._displayed_entries)
        prefix = identifiers[: len(self._displayed_entries)] == self._displayed_entries
        if self._displayed_session != self.session_id or not prefix:
            await transcript.remove_children()
            self._displayed_entries = []
            self._displayed_session = self.session_id
        for item in history[len(self._displayed_entries) :]:
            entry_id = item["entry_id"]
            if item["kind"] == "compaction":
                await transcript.mount(Static(item["text"], markup=False, classes="message-usage"))
                self._displayed_entries.append(entry_id)
                continue
            message = item["message"]
            if message["tool_result"] is not None:
                await transcript.mount(
                    Collapsible(
                        ToolOutputCard(entry_id, message["tool_result"]),
                        title=tool_title(message["tool_result"]),
                        collapsed=True,
                        classes="tool-block",
                    )
                )
            elif message["role"] == "assistant":
                await transcript.mount(Static("PYCODE", classes="assistant-label", markup=False))
                thinking = content_text(message, "thinking")
                if thinking:
                    await transcript.mount(
                        Collapsible(
                            Static(thinking, markup=False, classes="message-thinking"),
                            title="思考过程",
                            collapsed=True,
                            classes="thinking-block",
                        )
                    )
                await transcript.mount(
                    Markdown(content_text(message, "text"), id="message-" + entry_id)
                )
                metadata = reply_metadata(message)
                if metadata:
                    await transcript.mount(Static(metadata, markup=False, classes="message-usage"))
            else:
                await transcript.mount(
                    Static(
                        "› " + content_text(message, "text"), markup=False, classes="user-message"
                    )
                )
            self._displayed_entries.append(entry_id)
        if at_bottom:
            self.follow_conversation()
        elif added:
            self.query_one("#back-to-bottom", Button).display = True

    async def refresh_state(self):
        if self.session is None or self._closed:
            return
        self.state = await self.active_session.inspect(presentation=True)
        if self._render_failed:
            return
        try:
            await self.refresh_widgets()
        except Exception as error:
            self.render_failed(error)

    async def refresh_widgets(self):
        self.refresh_commands()
        await self.render_history()
        await self.query_one("#pending", Vertical).remove_children()
        pending = self.view_state["pending"]
        child_source = None
        team = self.view_state.get("team") or {}
        if team.get("degraded"):
            self.notice("Team persistence failed; reopen the session to recover", "error")
        if not pending and team.get("approvals"):
            child_source = team["approvals"][0]
            pending = child_source["pending"]
        if pending:
            payload = pending[0]
            if payload["kind"] == "permission":
                panel = ApprovalPanel(
                    payload["operation_id"],
                    payload["call"]["name"],
                    json_dumps(
                        {
                            "arguments": payload["call"]["arguments"],
                            "details": payload.get("approval_details", {}),
                            "sandbox": payload.get("sandbox"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    agent_id=child_source["agent_id"] if child_source else None,
                    source=(child_source["agent_id"] + " · " + child_source["role"][:80])
                    if child_source
                    else "",
                )
                await self.query_one("#pending", Vertical).mount(
                    Collapsible(
                        panel,
                        title=payload["call"]["name"] + " · 等待批准",
                        collapsed=False,
                        classes="approval-block",
                    )
                )
            elif payload["kind"] == "plan":
                await self.query_one("#pending", Vertical).mount(PlanPanel(payload))
            elif payload["kind"] == "ask_user":
                await self.query_one("#pending", Vertical).mount(QuestionPanel(payload))
        if self.view_state["pending_revert_id"]:
            self.notice(
                "Revert recovery required: " + self.view_state["pending_revert_id"], "warning"
            )
            await self.query_one("#pending", Vertical).mount(
                RevertPanel(transaction_id=self.view_state["pending_revert_id"])
            )
        elif self._pending_plan is not None:
            await self.query_one("#pending", Vertical).mount(RevertPanel(self._pending_plan))
        elif not pending and self._show_agents:
            await self.query_one("#pending", Vertical).mount(
                AgentsPanel(self.view_state.get("team"))
            )
        elif not pending and self._memory_report is not None:
            await self.query_one("#pending", Vertical).mount(MemoryPanel(self._memory_report))
        self.query_one("#chat-input", ChatInput).disabled = bool(
            self.view_state["pending_run_id"] or self.view_state["pending_revert_id"]
        )
        self.query_one("#chat-input", ChatInput).update_completion()
        selected = self.view_state["context"]["selection"]
        label = "模型：" + selected["model_id"] if selected else "模型：等待首轮请求"
        pieces = [
            plan_status(self.view_state.get("plan")),
            label,
            context_text(self.view_state["context_usage"]),
        ]
        self.query_one("#status", Static).update(" · ".join(piece for piece in pieces if piece))
        if not pending and not self.query_one("#chat-input", ChatInput).disabled:
            self.query_one("#chat-input", ChatInput).focus()

    def start_operation(self, function, *args):
        if self.busy:
            self.notice("Wait for the current command or stop it first", "warning")
            return
        self.last_error = None
        self._operation = asyncio.create_task(
            self.execute_operation(function, args), name="pycode-ui-operation"
        )

    async def execute_operation(self, function, args):
        try:
            value = await function(*args)
            if isinstance(value, RunResult):
                await self.active_session.publish_result(value)
        except asyncio.CancelledError:
            if not self._closed:
                self.notice("Stopped; execution remains recoverable", "warning")
        except DomainError as error:
            self.last_error = error
            self.notice(str(error), "error")
        finally:
            if not self._closed:
                await self.refresh_state()
                self.call_later(self.maybe_wake_team)

    def maybe_wake_team(self):
        if (
            self.session is not None
            and not self._closed
            and not self.busy
            and not (self.state or {}).get("pending_run_id")
            and getattr(self.session, "team_wake_pending", lambda: False)()
        ):
            self.start_operation(self.active_session.wake_team)

    async def wait_idle(self):
        if self._operation is not None:
            await asyncio.shield(self._operation)

    async def send_text(self, text):
        if self.session is None:
            return
        if text.startswith("/"):
            await self.command(text)
        elif self.busy:
            await self.active_session.submit_input(Message(uuid4().hex, "user", text), "steer")
            self.notice("已追加到当前任务")
        else:
            self.start_operation(self.active_session.run, Message(uuid4().hex, "user", text))

    async def on_chat_input_submitted(self, event):
        try:
            await self.send_text(event.text)
        except DomainError as error:
            self.notice(str(error), "error")

    def select_model_from_picker(self, provider, model_id):
        self.start_operation(self.active_session.select_model, provider, model_id)

    def refresh_commands(self):
        snapshot = self.view_state["snapshot"]
        entries = {entry["id"]: entry for entry in snapshot["entries"]}
        current = snapshot["leaf_id"]
        catalog = ()
        while current is not None:
            entry = entries[current]
            if entry["type"] == "skill_catalog":
                catalog = entry["data"]["entries"]
                break
            current = entry["parent_id"]
        self.commands.set_skills(catalog)
        self.query_one("#chat-input", ChatInput).update_completion()

    def on_option_list_option_selected(self, event):
        if isinstance(event.option_list, CompletionPopup):
            event.stop()
            popup = event.option_list
            if popup.display and event.option_index < len(popup.commands):
                self.query_one("#chat-input", ChatInput).complete_command(
                    popup.commands[event.option_index]
                )
        elif isinstance(event.option_list, ModelPicker):
            event.stop()
            selected = event.option_list.selected
            if selected is not None:
                event.option_list.hide()
                self.query_one("#chat-input", ChatInput).clear()
                self.select_model_from_picker(selected.provider, selected.model_id)

    def on_approval_panel_responded(self, event):
        event.stop()
        panel = self.query_one("#perm-inline", ApprovalPanel)
        panel.disabled = True
        self.collapse_parent(panel)
        if event.agent_id is not None:
            task = asyncio.create_task(self.answer_child(event.agent_id, event.answer))
            self._team_tasks.add(task)
            task.add_done_callback(self._team_tasks.discard)
        else:
            self.respond(event.answer)

    async def answer_child(self, agent_id, answer):
        try:
            await self.active_session.answer_agent(agent_id, answer)
        except DomainError as error:
            self.notice(str(error), "error")
        finally:
            if not self._closed:
                await self.refresh_state()

    def respond(self, answer):
        self.start_operation(self.active_session.resume, answer)

    def on_plan_panel_responded(self, event):
        self.respond(event.answer)

    def on_question_panel_responded(self, event):
        event.stop()
        self.query_one("#askuser-inline", QuestionPanel).disabled = True
        self.respond(event.answer)

    def on_revert_panel_responded(self, event):
        event.stop()
        if event.action == "revert-discard":
            self._pending_plan = None
            self.start_operation(self.refresh_state)
        elif event.action == "revert-apply":
            self.start_operation(self.apply_revert)
        elif event.action in {"restore-continue", "restore-compensate"}:
            self.start_operation(self.recover_revert, event.action == "restore-compensate")

    async def preview_revert(self, target, mode):
        self._pending_plan = await self.active_session.preview_revert(target, mode)

    async def apply_revert(self):
        await self.active_session.revert(self._pending_plan)
        self._pending_plan = None

    async def recover_revert(self, compensate):
        await self.active_session.recover_revert(
            self.view_state["pending_revert_id"], compensate=compensate
        )
        self._pending_plan = None

    async def stop_active(self):
        if self.busy:
            await self.active_session.cancel()
            if self.busy:
                assert self._operation is not None
                self._operation.cancel()
            await self.wait_idle()
        elif self.session is not None:
            await self.active_session.cancel()

    async def action_stop(self):
        await self.stop_active()

    async def action_close(self):
        await self.shutdown()
        self.exit()

    def action_toggle_theme(self):
        self.theme = "pycode-light" if self.theme == "pycode-dark" else "pycode-dark"

    async def shutdown(self):
        if self._closed:
            return
        self._closed = True
        await self.stop_active()
        for task in tuple(self._team_tasks):
            task.cancel()
        await asyncio.gather(*self._team_tasks, return_exceptions=True)
        if self.session is not None:
            await self.active_session.aclose()

    async def on_unmount(self):
        await self.shutdown()

    async def on_button_pressed(self, event):
        if event.button.id == "back-to-bottom":
            self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)
            event.button.display = False
        return

    async def command(self, text):
        first, _, raw = text.partition(" ")
        spec = self.commands.find(first)
        if spec is not None and spec.raw_arguments:
            await getattr(self, spec.handler)(first[len("/skill:") :], raw)
            return
        try:
            parts = shlex.split(text)
        except ValueError as error:
            raise StorageError("invalid_command", str(error)) from error
        spec = self.commands.find(parts[0]) if parts else None
        if spec is None:
            self.notice("未识别的命令；输入 / 选择命令，Esc 关闭提示。", "warning")
        elif spec.raw_arguments or await getattr(self, spec.handler)(parts[1:]) is False:
            usage = spec.name + (" " + spec.usage if spec.usage else "")
            self.notice("用法：" + usage, "warning")

    async def command_new(self, args):
        await self.switch_session()

    async def command_session(self, args):
        if args == ["new"]:
            await self.switch_session()
        elif len(args) == 2 and args[0] == "open":
            await self.switch_session(args[1])
        elif args == ["list"]:
            self.notice(json_dumps(await self._sessions.list_sessions(), ensure_ascii=False))
        elif len(args) >= 2 and args[0] == "name":
            self.start_operation(self.active_session.name, " ".join(args[1:]))
        elif len(args) == 2 and args[0] == "fork":
            await self._sessions.fork(
                self.session_id, self.view_state["snapshot"]["leaf_id"], args[1]
            )
            await self.switch_session(args[1])
        else:
            return False

    async def command_resume(self, args):
        if args:
            return False
        self.start_operation(self.active_session.resume)

    async def command_model(self, args):
        if not args:
            catalog = await self.active_session.model_list()
            self.notice("")
            self.query_one("#model-picker", ModelPicker).show_models(catalog.models)
            self.query_one("#chat-input", ChatInput).focus()
        elif len(args) == 2:
            self.start_operation(self.active_session.select_model, *args)
        else:
            return False

    async def command_memory(self, args):
        scope = None
        if len(args) >= 3 and args[-2] == "--scope":
            if args[0] not in {"edit", "remove", "delete"} or args[-1] not in {"user", "project"}:
                return False
            scope, args = args[-1], args[:-2]
        if not args:
            self.start_operation(self.show_memory)
        elif len(args) == 2 and args[0] == "mode":
            self.start_operation(self.active_session.memory_mode, args[1])
        elif len(args) >= 3 and args[0] == "edit":
            self.start_operation(self.edit_memory, args[1], " ".join(args[2:]), scope)
        elif len(args) == 2 and args[0] in {"remove", "delete"}:
            self.start_operation(self.remove_memory, args[1], scope)
        elif args == ["undo"]:
            self.start_operation(self.undo_memory)
        elif args == ["clear"]:
            self.start_operation(self.active_session.memory_clear)
        elif args == ["extract"]:
            self.start_operation(self.active_session.memory_extract)
        else:
            return False

    async def command_storage(self, args):
        if args == ["status"]:
            self.start_operation(self.show_storage)
        elif args in (["gc"], ["gc", "--dry-run"]):
            self.start_operation(self.collect_storage, "--dry-run" in args)
        else:
            return False

    async def command_plan(self, args):
        self.start_operation(self.enter_plan, " ".join(args))

    async def command_agents(self, args):
        if not args:
            self._show_agents = not self._show_agents
            await self.refresh_state()
            return True
        if len(args) == 2 and args[0] == "cancel":
            await self.active_session.cancel_agent(args[1])
            await self.refresh_state()
            return True
        return False

    async def command_usage(self, args):
        self.push_screen(UsageScreen(await self.active_session.usage()))

    async def command_compact(self, args):
        if args:
            return False
        self.start_operation(self.active_session.compact)

    async def command_reload(self, args):
        if args:
            return False
        self.start_operation(self.reload_skills)

    async def command_skills(self, args):
        self.start_operation(self.search_skills, " ".join(args))

    async def command_skill(self, args):
        if len(args) == 2 and args[0] == "install":
            self.start_operation(self.install_skill, args[1])
        elif len(args) >= 3 and args[0] == "create":
            self.start_operation(self.create_skill, args[1], " ".join(args[2:]))
        elif len(args) == 2 and args[0] == "read":
            self.start_operation(self.read_skill, args[1])
        else:
            return False

    async def create_skill(self, name, description):
        content = (
            "---\nname: "
            + json_dumps(name, ensure_ascii=False)
            + "\ndescription: "
            + json_dumps(description, ensure_ascii=False)
            + "\n---\n\n"
            + description
            + "\n"
        )
        report = await self.active_session.save_skill(name, content)
        self.notice("Skill 已创建：" + report["path"])
        await self.refresh_state()

    async def read_skill(self, name):
        document = await self.active_session.read_skill(name)
        self.notice(document.raw + "\nRevision: " + document.entry.content_hash)

    async def command_load_skill(self, name, arguments):
        self.start_operation(self.load_skill, name, arguments)

    async def command_branch(self, args):
        if len(args) != 1:
            return False
        self.start_operation(self.active_session.branch, None if args[0] == "root" else args[0])

    async def command_theme(self, args):
        self.action_toggle_theme()

    async def command_revert(self, args):
        if not 1 <= len(args) <= 2:
            return False
        self.start_operation(
            self.preview_revert,
            None if args[0] == "root" else args[0],
            args[1] if len(args) == 2 else "dialogue",
        )

    async def command_quit(self, args):
        await self.action_close()

    async def show_memory(self):
        self._memory_report = await self.active_session.memory_list()

    async def edit_memory(self, memory_id, content, scope=None):
        await self.active_session.memory_edit(memory_id, content, scope=scope)
        self.notice("Memory updated: " + memory_id)
        await self.show_memory()

    async def remove_memory(self, memory_id, scope=None):
        await self.active_session.memory_remove(memory_id, scope=scope)
        self.notice("Memory deleted: " + memory_id)
        await self.show_memory()

    async def undo_memory(self):
        result = await self.active_session.memory_undo()
        self.notice("Undid automatic memory update: " + ", ".join(result["memory_ids"]))
        await self.show_memory()

    async def show_storage(self):
        report = await self.active_session.storage_status()
        self.notice(json_dumps(report, ensure_ascii=False))

    async def collect_storage(self, dry_run):
        report = await self.active_session.storage_gc(dry_run=dry_run)
        self.notice(json_dumps(report, ensure_ascii=False))

    async def enter_plan(self, task):
        result = await self.active_session.enter_plan(task)
        self.notice("Plan mode entered")
        return result

    async def load_skill(self, name, arguments):
        result = await self.active_session.load_skill(name, arguments)
        self.notice("Skill task: " + name)
        return result

    async def reload_skills(self):
        catalog = await self.active_session.reload_skills()
        self.notice(f"Skill catalog refreshed: {len(catalog['entries'])} entries")

    async def search_skills(self, query):
        found = await self.active_session.skill_search(query)
        self.notice("\n".join(item["name"] + ": " + item["description"] for item in found))

    async def install_skill(self, url):
        report = await self.active_session.install_skill(url)
        self.notice("Installed Skill " + report["skill_name"] + " at " + report["target_dir"])
