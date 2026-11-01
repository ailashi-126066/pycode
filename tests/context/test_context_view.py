import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_context_view_matches_runtime_anchor_and_remains_available_without_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            await session.run(Message("u1", "user", "hello"))
            report = (await session.inspect())["context_usage"]
            assert report["tokens"] == 25 and report["source"] == "provider_usage"
            assert report["window"] == 32000 and len(requests) == 1
        finally:
            await session.aclose()
    monkeypatch.delenv("PYCODE_RECOVERY_TEST_KEY")
    view = await compose_application(config).open_view("s1")
    try:
        assert (await view.inspect())["context_usage"] == report
        assert (await view.usage())["context"] == report
    finally:
        await view.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
