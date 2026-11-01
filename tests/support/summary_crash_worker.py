"""Crash a real independently metered summary job at an authoritative boundary."""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

from pycode.app.api import compose_application
from pycode.app.config import AppConfig
from pycode.kernel import Message
from pycode.models import SQLiteRequestStore
from tests.support.execution import provider


class Crash:
    def __init__(self, boundary):
        self._boundary = boundary

    async def on_event(self, event):
        if event.kind == "compaction" and event.data.get("status") == self._boundary:
            os._exit(74)


def crash_after_summary_request_start():
    """No event marks a request start (11.1 section 3.1), so the request record is the boundary."""
    start = SQLiteRequestStore.start

    async def started(self, record):
        await start(self, record)
        if record.purpose == "summary":
            os._exit(74)

    SQLiteRequestStore.start = started


async def main():
    from pycode.tools import ToolDiscovery

    async def disclosed(self, request):
        return request

    ToolDiscovery.disclose = disclosed

    workspace, url, boundary = sys.argv[1:]
    if boundary == "request_started":
        crash_after_summary_request_start()
    root = Path(workspace)
    config = AppConfig(root, root / "data", summary_keep_recent_tokens=20, memory_mode="off")
    selected = replace(provider(url), context_window=20000, max_output_tokens=1000)
    session = await compose_application(config, (selected,)).open_session(
        "s1", refresh_models=False, consumers=(Crash(boundary),)
    )
    await session.run(Message("initial", "user", "continue"))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
