"""Actual process death after a confirmed MCP effect and before history publication."""

import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import TypeAdapter

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.messages import Message
from pycode.models.domain.models import ProviderConfig


class CrashAfterFact:
    async def consume(self, event):
        if event.kind == "tool_execution_end" and event.data["tool_name"] == "mcp__srv__mutate":
            os._exit(85)


async def main():
    from pycode.tools.adapters.discovery import ToolDiscovery

    async def disclosed(self, request):
        return request

    ToolDiscovery.disclose = disclosed

    request = json.load(sys.stdin)
    config = AppConfig(Path(request["workspace"]), Path(request["data"]), memory_mode="off")
    session = await compose_application(
        config,
        (TypeAdapter(ProviderConfig).validate_json(json.dumps(request["provider"]), strict=True),),
    ).open_session("s1", create=True, refresh_models=False, consumers=(CrashAfterFact(),))
    try:
        waiting = await session.run(Message("u1", "user", "mutate"))
        await session.resume(
            {"operation_id": waiting.pending[0]["operation_id"], "decision": "approve"}
        )
    finally:
        await session.aclose()


if __name__ == "__main__":
    asyncio.run(main())
