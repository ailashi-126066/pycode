from dataclasses import replace

import pytest

from pycode.app.api import RunOptions, SessionResources
from pycode.app.api import compose_application
from pycode.app.config import AppConfig
from pycode.context import ContextProjector
from pycode.kernel import Message
from pycode.models import ProviderConfig
from tests.support.http_provider import chat_completion, protocol_server


async def test_selected_provider_and_branch_context_drive_real_framework_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_SWITCH_API_KEY", "fake-switch-key")
    responses = [chat_completion(deepseek=True), chat_completion(), chat_completion(deepseek=True)]
    with protocol_server(responses) as (url, received):
        base = ProviderConfig(
            "first", "deepseek", "wire-model", url + "/v1", "PYCODE_SWITCH_API_KEY", 32000, 4096
        )
        second = replace(
            base, name="second", implementation="openai", openai_api="chat_completions"
        )
        config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
        repository = compose_application(config).session_repository()
        store = await repository.open("s1", create=True)
        catalog = compose_application(config, (base, second)).model_catalog()
        control = compose_application(config).model_control(store, catalog=catalog)
        await control.list_models(refresh=False)
        await control.select(base.name, base.model_id)

        async def run(input_id, text):
            provider = await control.restore()
            history = ContextProjector().transform_context(await store.snapshot()).messages
            container = await compose_application(config).runtime(
                SessionResources(session_id="s1", history_store=store, tools=(), consumers=()),
                RunOptions(provider=provider, run_id=input_id),
            )
            try:
                return await container.runtime.run(
                    (Message(input_id, "user", text),), container.context, history=history
                )
            finally:
                await container.aclose()

        assert (await run("u1", "first turn")).outcome == "completed"
        first_branch = (await store.snapshot()).leaf_id
        await control.select(second.name, second.model_id)
        assert (await run("u2", "second provider")).outcome == "completed"
        await store.aclose()
        store = await repository.open("s1")
        control = compose_application(config).model_control(store, catalog=catalog)
        assert (await control.restore()).name == "second"
        await store.branch(first_branch)
        assert (await control.restore()).name == "first"
        assert (await run("u3", "new branch")).outcome == "completed"
        final = await store.snapshot()
        assert len([entry for entry in final.entries if entry.type == "message"]) == 6
        contexts = received[-1]["body"]["messages"]
        assert any(message.get("content") == "first turn" for message in contexts)
        assert not any(message.get("content") == "second provider" for message in contexts)
        last_reply = next(
            entry.message
            for entry in reversed(final.entries)
            if entry.message is not None and entry.message.role == "assistant"
        )
        assert last_reply.model_selection.provider == "first"
        await store.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
