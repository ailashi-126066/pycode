"""Long-term memory: what pycode remembers about the user, across sessions and projects.

~/.pycode/memory/ holds one Markdown file per memory, named after it:

    ---
    type: feedback
    description: 回答要短，用例子讲
    modified: 2026-11-08 14:03:27
    ---
    用户在准备面试，不喜欢在函数之间跳来跳去的代码……

Three types: user (who the user is, what they know), feedback (how they want pycode to work,
and why), reference (where things outside the project are). Facts about one project belong in
that project's AGENTS.md. MEMORY.md is the index; the code rewrites it after every change.
The newest 50 entries go into the system prompt, read once per session.

Two ways in:
- the model calls memory_write / memory_read / memory_delete; no approval, the user sees a
  notice for each change;
- a background LangMem pass reads only what the user said and adds, updates or deletes
  memories: after every 5 user messages, and when compaction starts summarizing messages it
  has not read yet (they are about to leave the context).
Text that looks like a key or password is never saved.
"""

import asyncio
import contextvars
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langmem import create_memory_manager
from pydantic import BaseModel, Field

from ..compaction.summarize import is_skill, is_summary
from ..context import Context

TYPES = ("user", "feedback", "reference")
INDEX_FILE = "MEMORY.md"
IN_PROMPT = 50  # index entries put into the system prompt, newest first
EVERY = 5  # user messages between two background passes
NAME = re.compile(r"\w[\w-]{0,59}")  # letters (Chinese too), digits, _ and -
SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"  # OpenAI, DeepSeek, Anthropic style API keys
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"  # Slack tokens
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}"
)

PROMPT = """## 长期记忆

你有跨会话、跨项目的长期记忆，下面是索引（最新的 {shown} 条，共 {total} 条）：
<memory_index>
{index}
</memory_index>

需要某条的内容时用 memory_read 读；不带名字读到完整索引。
用户让你记住什么，或者你发现了以后其他会话也用得上的事，用 memory_write 记下，分三类：
- user：用户是谁，背景，熟悉什么
- feedback：用户对你工作方式的要求、纠正和偏好，写上原因
- reference：项目之外的资料在哪里（文档、看板、仪表盘的地址）
不要记：只和某个项目有关的事实（那属于项目的 AGENTS.md）、代码或 git 里查得到的、只和当前任务\
有关的、密钥和密码。已有一条讲同一件事时更新它，不要新建。
记忆可能过时：和用户现在说的冲突时以用户为准，并更新或删除那条。"""

CONSOLIDATE = """你在整理一个编码助手关于用户的长期记忆。输入只有用户说过的话，以及已有的记忆。
只记以后在其他会话、其他项目里仍然有用的东西，分三类：
- user：用户是谁，背景，熟悉什么
- feedback：用户对助手工作方式的要求、纠正和偏好，写上原因
- reference：项目之外的资料在哪里
不记：只和某一个项目有关的事实、代码或 git 里查得到的、只和当前任务有关的、密钥和密码、猜测。
已有记忆讲同一件事时更新那条，不要新建重复的；被用户否定或已经过时的删除。
没有值得记的就什么也不做。"""


class Note(BaseModel):
    """One long-term memory about the user."""

    name: str = Field(description="简短的名字，作文件名：小写英文或拼音加连字符，如 answer-style")
    type: Literal["user", "feedback", "reference"]
    description: str = Field(description="一行，说明这条记忆讲什么，用来判断要不要读它")
    body: str = Field(description="记忆的内容")


@dataclass(frozen=True)
class Entry:
    name: str
    type: str
    description: str
    body: str
    modified: str  # "YYYY-MM-DD HH:MM:SS"; the newest come first


class MemoryState(AgentState):
    memory_index: NotRequired[str]  # the index as this session's system prompt shows it
    memory_seen: NotRequired[list[str]]  # ids of user messages a background pass has read


def user_words(messages: list[BaseMessage]) -> list[BaseMessage]:
    """What the user said: typed turns and steering messages, not summaries or pinned skills."""
    return [
        m for m in messages if isinstance(m, HumanMessage) and not is_summary(m) and not is_skill(m)
    ]


class Memory(AgentMiddleware):
    state_schema = MemoryState

    def __init__(self, directory: Path, model):
        super().__init__()
        self.directory = directory
        self.manager = create_memory_manager(
            model, schemas=[Note], instructions=CONSOLIDATE, enable_deletes=True
        )
        self.lock = asyncio.Lock()  # one background pass at a time
        self.tasks: set[asyncio.Task] = set()
        # User messages already given to a background pass. Saved in each session's state at
        # the end of a run, and read back at the start of the next.
        self.seen: set[str] = set()
        self.notify = None  # set by the interface: (text) -> None, for background changes
        self.tools = self.make_tools()

    # ---- the files ----

    def entries(self) -> list[Entry]:
        """Every memory, newest first; a file that cannot be read is skipped."""
        found = []
        for path in self.directory.glob("*.md"):
            if path.name == INDEX_FILE:
                continue
            try:
                found.append(parse(path.stem, path.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(found, key=lambda e: e.modified, reverse=True)

    def get(self, name: str) -> Entry | None:
        return next((e for e in self.entries() if e.name == name), None)

    def save(self, name: str, type_: str, description: str, body: str) -> Entry:
        """Write one memory and the index. Raises ValueError for a bad name, type or a secret."""
        if not NAME.fullmatch(name) or name == INDEX_FILE.removesuffix(".md"):
            raise ValueError(f"名字 {name!r} 不能用：只用字母、数字、_ 和 -，最多 60 个字符")
        if type_ not in TYPES:
            raise ValueError(f"类型 {type_!r} 不对，只能是 {'、'.join(TYPES)}")
        description = " ".join(description.split())
        if SECRET.search(description) or SECRET.search(body):
            raise ValueError("内容像是密钥或密码，不能存进记忆")
        entry = Entry(
            name, type_, description, body.strip(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{name}.md").write_text(
            f"---\ntype: {entry.type}\ndescription: {entry.description}\n"
            f"modified: {entry.modified}\n---\n{entry.body}\n"
        )
        self.write_index()
        return entry

    def delete(self, name: str) -> bool:
        path = self.directory / f"{name}.md"
        if not NAME.fullmatch(name) or not path.exists():
            return False
        path.unlink()
        self.write_index()
        return True

    def write_index(self) -> None:
        text = "# 长期记忆索引\n\npycode 生成，每次记忆变化后重写，不要手改。\n\n"
        (self.directory / INDEX_FILE).write_text(text + index_lines(self.entries()) + "\n")

    # ---- the model's tools ----

    def make_tools(self):
        memory = self

        @tool(parse_docstring=True)
        def memory_read(name: str = "") -> str:
            """Read one long-term memory, or the full index when no name is given.

            Args:
                name: The memory's name as the index shows it; leave empty for the index.
            """
            if not name:
                return index_lines(memory.entries()) or "还没有记忆。"
            entry = memory.get(name)
            if entry is None:
                return f"Error: 没有叫 {name} 的记忆"
            return f"[{entry.type}] {entry.description}（{entry.modified}）\n\n{entry.body}"

        @tool(parse_docstring=True)
        def memory_write(
            name: str,
            type: Literal["user", "feedback", "reference"],  # noqa: A002
            description: str,
            content: str,
            runtime: ToolRuntime[Context],
        ) -> str:
            """Save a long-term memory, or replace the one with this name.

            Args:
                name: Short file name, e.g. answer-style. The same name replaces that memory.
                type: user (who the user is), feedback (how they want you to work, and why)
                    or reference (where things outside the project are).
                description: One line saying what the memory is about.
                content: The memory itself.
            """
            try:
                entry = memory.save(name, type, description, content)
            except ValueError as error:
                return f"Error: {error}"
            runtime.stream_writer({"memory": f"记下了：{entry.name}（{entry.description}）"})
            return f"Saved memory {entry.name}."

        @tool(parse_docstring=True)
        def memory_delete(name: str, runtime: ToolRuntime[Context]) -> str:
            """Delete a long-term memory that is wrong or out of date.

            Args:
                name: The memory's name as the index shows it.
            """
            if not memory.delete(name):
                return f"Error: 没有叫 {name} 的记忆"
            runtime.stream_writer({"memory": f"删除了记忆：{name}"})
            return f"Deleted memory {name}."

        return [memory_read, memory_write, memory_delete]

    # ---- around the agent ----

    async def abefore_agent(self, state, runtime):
        self.seen |= set(state.get("memory_seen", []))
        if "memory_index" in state:
            return None  # read once per session: the prompt stays the same, and so its cache
        entries = self.entries()
        shown = entries[:IN_PROMPT]
        index = PROMPT.format(
            shown=len(shown), total=len(entries), index=index_lines(shown) or "（还没有记忆）"
        )
        return {"memory_index": index}

    async def awrap_model_call(self, request, handler):
        system = request.system_message.text if request.system_message else ""
        section = request.state.get("memory_index", "")
        return await handler(
            request.override(system_message=SystemMessage(f"{system}\n\n{section}"))
        )

    async def aafter_agent(self, state, runtime):
        said = user_words(state["messages"])
        fresh = [m for m in said if m.id not in self.seen]
        if len(fresh) >= EVERY:
            self.consolidate_later(fresh)
        # Only ids still in the conversation: the rest can no longer come back as fresh.
        return {"memory_seen": [m.id for m in said if m.id in self.seen]}

    def before_compaction(self, messages: list[BaseMessage]) -> None:
        """Compaction is about to summarize these: read what the user said in them first."""
        fresh = [m for m in user_words(messages) if m.id not in self.seen]
        if fresh:
            self.consolidate_later(fresh)

    # ---- the background pass ----

    def consolidate_later(self, messages: list[BaseMessage]) -> None:
        self.seen |= {m.id for m in messages}
        # An empty context keeps the pass out of the run's stream, as compaction's summary.
        task = asyncio.create_task(self.consolidate(messages), context=contextvars.Context())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def consolidate(self, messages: list[BaseMessage]) -> None:
        async with self.lock:
            try:
                changes = await self.apply(messages)
            except Exception as error:  # a failed pass loses nothing already saved
                changes = [f"后台整理记忆失败（{type(error).__name__}）"]
        if changes and self.notify is not None:
            self.notify("\n".join(changes))

    async def apply(self, messages: list[BaseMessage]) -> list[str]:
        """One LangMem pass over the user's words; returns what changed, for the notice."""
        before = {e.name: e for e in self.entries()}
        existing = [
            (e.name, Note(name=e.name, type=e.type, description=e.description, body=e.body))
            for e in before.values()
        ]
        said = [HumanMessage(m.text) for m in messages]
        results = await self.manager.ainvoke({"messages": said, "existing": existing})
        changes = []
        for result in results:
            note, old = result.content, before.get(result.id)
            if type(note).__name__ == "RemoveDoc":
                if old is not None and self.delete(old.name):
                    changes.append(f"删除了记忆：{old.name}")
                continue
            if not isinstance(note, Note):
                continue
            if old is not None:
                same = (note.type, note.description, note.body.strip())
                if same == (old.type, old.description, old.body):
                    continue  # LangMem returns the unchanged ones too
                current = self.get(old.name)
                if current is None or current.modified != old.modified:
                    continue  # the model changed or deleted it meanwhile: its version wins
                name, verb = old.name, "更新了记忆"
            else:
                name, verb = self.free_name(note.name), "记下了"
            try:
                entry = self.save(name, note.type, note.description, note.body)
            except ValueError:
                continue  # a bad name or a secret: not saved
            changes.append(f"{verb}：{entry.name}（{entry.description}）")
        return changes

    def free_name(self, wanted: str) -> str:
        """A valid file name for a new memory that does not replace an existing one."""
        base = "-".join(re.findall(r"[\w]+", wanted.lower()))[:50] or "memory"
        name, n = base, 2
        while (self.directory / f"{name}.md").exists():
            name, n = f"{base}-{n}", n + 1
        return name

    async def aclose(self) -> None:
        """On exit: a pass still running is stopped; its messages are read again next time."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def parse(name: str, text: str) -> Entry:
    """A memory file: front matter between two --- lines, then the content."""
    if not text.startswith("---\n") or "\n---\n" not in text[3:]:
        raise ValueError("no front matter")
    head, body = text[4:].split("\n---\n", 1)
    fields = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
    if fields.get("type") not in TYPES:
        raise ValueError("unknown type")
    return Entry(
        name,
        fields["type"],
        fields.get("description", ""),
        body.strip(),
        fields.get("modified", ""),
    )


def index_lines(entries: list[Entry]) -> str:
    return "\n".join(f"- [{e.name}]({e.name}.md) {e.type}：{e.description}" for e in entries)
