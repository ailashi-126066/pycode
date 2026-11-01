"""Crash a real independently metered summary job at an authoritative boundary."""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.messages import Message
from tests.support.execution import provider


class Crash:
    def __init__(self, boundary):
        self._boundary = boundary

    async def consume(self, event):
        if event.kind == "compaction" and event.data.get("status") == self._boundary:
            os._exit(74)
        if (
            self._boundary == "request_started"
            and event.kind == "model_request_start"
            and event.data.get("purpose") == "summary"
        ):
            os._exit(74)


async def main():
    from pycode.tools.adapters.discovery import ToolDiscovery

    async def disclosed(self, request):
        return request

    ToolDiscovery.disclose = disclosed

    workspace, url, boundary = sys.argv[1:]
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
