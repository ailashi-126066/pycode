"""Real child-process crash probes; production code has no injected failure hooks."""

import asyncio
import os
import sys
from pathlib import Path

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.messages import Message
from pycode.models.domain.models import ProviderConfig
from pycode.tools.adapters.output_store import TempOutputStore
from tests.support.recovery_tools import WritingEcho


class CrashConsumer:
    def __init__(self, boundary):
        self._boundary = boundary

    async def consume(self, event):
        fatal = (
            (
                self._boundary == "model_saved"
                and event.kind == "message_end"
                and event.data["message"]["role"] == "assistant"
            )
            or (
                self._boundary in {"output_ready", "question_answered"}
                and event.kind == "tool_execution_end"
            )
            or (
                self._boundary == "history_saved"
                and event.kind == "message_end"
                and event.data["message"]["role"] == "tool_result"
            )
            or (self._boundary == "model_stream" and event.kind == "message_update")
            or (
                self._boundary == "model_request_settled"
                and event.kind == "model_request_end"
                and event.data.get("status") == "completed"
            )
            or (
                self._boundary == "queued_delivery"
                and event.kind == "notification"
                and event.data.get("kind") == "input_delivered"
            )
            or (
                self._boundary == "queue_after_model"
                and event.kind == "message_end"
                and event.data["message"]["provider_metadata"].get("pycode_turn") == 2
            )
        )
        if fatal:
            os._exit(73)


class CrashTool(WritingEcho):
    def __init__(self, boundary):
        super().__init__()
        self._boundary = boundary

    async def execute(self, context, arguments):
        result = await super().execute(context, arguments)
        if self._boundary == "effect_written":
            os._exit(73)
        if self._boundary in {"output_published", "stream_output_published"}:
            outputs = TempOutputStore(context.session_id)
            if self._boundary == "output_published":
                await outputs.prepare(context, result, "head")
            else:
                writer = await outputs.begin(context, "head")
                await writer.append(result.text.encode())
                await writer.finish(result)
            os._exit(73)
        return result


async def main():
    from pycode.tools.adapters.discovery import ToolDiscovery

    async def disclosed(self, request):
        return request

    ToolDiscovery.disclose = disclosed

    workspace, url, boundary = sys.argv[1:]
    workspace = Path(workspace)
    configured = ProviderConfig(
        "test",
        "openai",
        "wire-model",
        url + "/v1",
        "PYCODE_RECOVERY_TEST_KEY",
        32000,
        4096,
        openai_api="chat_completions",
        supports_tools=True,
    )
    session = await compose_application(
        AppConfig(workspace, workspace / "data", memory_mode="off"), (configured,)
    ).open_session(
        "s1",
        create=True,
        tools=(CrashTool(boundary),),
        consumers=(CrashConsumer(boundary),),
        refresh_models=False,
    )
    waiting = await session.run(Message("u1", "user", "write"))
    if boundary == "question_wait":
        assert waiting.pending[0]["kind"] == "ask_user"
        os._exit(73)
    if boundary == "question_answered":
        await session.resume(
            {
                "operation_id": waiting.pending[0]["operation_id"],
                "answers": {"language": "Python"},
            }
        )
    if boundary in {"queue_saved", "queued_delivery", "queue_after_model"}:
        await session.submit_input(Message("steer-input", "user", "durable steering"), "steer")
        await session.submit_input(Message("later-input", "user", "durable follow up"), "follow_up")
        if boundary == "queue_saved":
            os._exit(73)
    if waiting.outcome == "needs_input":
        await session.resume(
            {
                "operation_id": waiting.pending[0]["operation_id"],
                "decision": "approve",
            }
        )
    await session.aclose()
    raise AssertionError("Crash boundary was not reached")


if __name__ == "__main__":
    asyncio.run(main())
