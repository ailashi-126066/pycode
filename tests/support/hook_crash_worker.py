"""Inject real process exit right after a configured command Hook finished."""

import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import TypeAdapter

from pycode.app.api import compose_application
from pycode.app.config import AppConfig
from pycode.kernel import Message
from pycode.models import ProviderConfig


async def main():
    from pycode.tools import ToolDiscovery

    async def disclosed(self, request):
        return request

    ToolDiscovery.disclose = disclosed

    request = json.load(sys.stdin)

    class CrashConsumer:
        async def on_event(self, event):
            if event.kind == "notification" and event.data.get("hook_id") == "mark":
                os._exit(81)

    config = AppConfig(Path(request["workspace"]), Path(request["data"]), memory_mode="off")
    session = await compose_application(
        config,
        (TypeAdapter(ProviderConfig).validate_json(json.dumps(request["provider"]), strict=True),),
    ).open_session("s1", create=True, refresh_models=False, consumers=(CrashConsumer(),))
    try:
        result = await session.run(Message("u1", "user", "Run parent after Hook"))
        if result.outcome == "needs_input":
            await session.resume(
                {"operation_id": result.pending[0]["operation_id"], "decision": "approve"}
            )
    finally:
        await session.aclose()


if __name__ == "__main__":
    asyncio.run(main())
