"""The session list: open the SQLite storage and keep one record per conversation."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

NAMESPACE = ("sessions",)


@dataclass(frozen=True)
class Session:
    id: str  # the LangGraph thread_id
    workspace: str
    title: str
    updated_at: datetime


@asynccontextmanager
async def open_storage(database: Path):
    """The checkpointer (conversations) and the store (session records), in one file."""
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with (
        AsyncSqliteSaver.from_conn_string(str(database)) as checkpointer,
        AsyncSqliteStore.from_conn_string(str(database)) as store,
    ):
        await store.setup()
        yield checkpointer, store


async def save_session(store, session_id: str, workspace: Path, title: str) -> None:
    """Create the record, or refresh its updated_at when the session is used again."""
    await store.aput(NAMESPACE, session_id, {"workspace": str(workspace), "title": title[:60]})


async def get_session(store, session_id: str) -> Session | None:
    item = await store.aget(NAMESPACE, session_id)
    if item is None:
        return None
    return Session(item.key, item.value["workspace"], item.value["title"], item.updated_at)


async def list_sessions(store, workspace: Path) -> list[Session]:
    items = await store.asearch(NAMESPACE, filter={"workspace": str(workspace)}, limit=1000)
    sessions = [Session(i.key, i.value["workspace"], i.value["title"], i.updated_at) for i in items]
    return sorted(sessions, key=lambda session: session.updated_at, reverse=True)


async def delete_session(checkpointer, store, session_id: str) -> None:
    await checkpointer.adelete_thread(session_id)
    await store.adelete(NAMESPACE, session_id)
