import pytest
from textual.widgets import Static

from pycode.app import AppConfig, compose_application
from pycode.ui.usage import UsageScreen
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server
from tests.ui.test_tui_http import settled


async def test_tui_usage_is_readable_has_unknown_cache_values_and_closes_with_escape(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion()]) as (url, requests):
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).tui()
        async with app.run_test(size=(120, 50)) as pilot:
            await app.ready.wait()
            await app.send_text("usage")
            await settled(app)
            await app.command("/usage")
            await pilot.pause()
            assert isinstance(app.screen, UsageScreen)
            text = "\n".join(str(item.render()) for item in app.screen.query(Static))
            assert "total_tokens: 25" in text and "cache_miss_tokens: unavailable" in text
            assert "Purpose: agent" in text and "Agent: lead" in text
            assert "Context: 25 / 32000" in text and "provider_usage" in text
            await pilot.press("escape")
            assert not isinstance(app.screen, UsageScreen) and len(requests) == 1


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
