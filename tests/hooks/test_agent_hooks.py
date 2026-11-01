"""Removed Hook modes fail before actions or model clients are created."""

import pytest

from pycode.hooks.config import parse_hooks
from pycode.kernel import DomainError


@pytest.mark.parametrize("event", ["startup", "pre_send", "post_receive"])
def test_agent_hook_configuration_is_rejected(event):
    with pytest.raises(DomainError, match="Hook action"):
        parse_hooks([{"event": event, "action": {"type": "agent", "prompt": "review"}}])


@pytest.mark.parametrize("event", ["startup", "pre_tool_use", "post_receive"])
@pytest.mark.parametrize("flag", [True, False])
def test_background_hook_configuration_is_rejected(event, flag):
    with pytest.raises(DomainError, match="Background Hooks"):
        parse_hooks(
            [{"event": event, "async": flag, "action": {"type": "command", "command": "echo no"}}]
        )
