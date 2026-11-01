"""Hook review commands were removed; records do not expose a replay action."""

import pytest

from pycode.app.cli.parser import build_parser
from pycode.session import AgentSession
from pycode.ui.commands import COMMANDS


def test_hook_review_is_not_a_cli_or_ui_command():
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["hooks", "old-operation", "retry"])
    assert error.value.code == 2
    assert "/hooks" not in {command.name for command in COMMANDS}
    assert not hasattr(AgentSession, "reconcile_hook")
