"""Shared Provider and session fixtures for actual loopback framework tests."""

from pycode.app.api import compose_application
from pycode.models import ProviderConfig


def provider(url):
    return ProviderConfig(
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


async def session_at(config, configured, tool, *, create=False):
    return await compose_application(config, (configured,)).open_session(
        "s1", create=create, tools=(tool,), refresh_models=False
    )
