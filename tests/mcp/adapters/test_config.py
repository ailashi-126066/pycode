import json
from dataclasses import asdict
from pathlib import Path

import pytest

from pycode.kernel import DomainError
from pycode.mcp.config import parse_servers
from pycode.mcp.types import mcp_spec

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/mcp-baselines.json").read_text(encoding="utf-8")
)


def test_source_loading_threshold_vectors_and_native_metadata(tmp_path):
    configured = parse_servers(
        {"s": {"command": "python", "env": {"TOKEN": "$ENV.TEST_TOKEN"}}}, tmp_path
    )[0]
    assert asdict(configured)["env"] == {"TOKEN": "$ENV.TEST_TOKEN"}
    declaration = {
        "name": "mcp__s__search",
        "server": "s",
        "description": "search",
        "input_schema": VECTORS["schema"]["inputSchema"],
        "trusted": True,
        "annotations": {"readOnlyHint": True},
        "config_hash": "config",
    }
    assert mcp_spec(declaration).category == "read"
    assert mcp_spec({**declaration, "trusted": False}).category == "external"


@pytest.mark.parametrize(
    "configuration",
    [
        {"url": "file:///tmp/server"},
        {"command": "x", "env": {"TOKEN": "secret"}},
        {"command": "x", "timeout": True},
        {"command": "x", "args": "bad"},
        {"url": "https://user:secret@example.com/mcp"},
        {"command": "x", "trusted": "yes"},
    ],
)
def test_invalid_sources_and_literal_credentials_fail_at_configuration_boundary(
    tmp_path, configuration
):
    with pytest.raises(DomainError):
        parse_servers({"s": configuration}, tmp_path)


@pytest.mark.parametrize("field,value", [("loading", "direct"), ("tool_modes", {})])
def test_removed_loading_modes_are_rejected(tmp_path, field, value):
    with pytest.raises(DomainError, match="unknown fields"):
        parse_servers({"s": {"command": "python", field: value}}, tmp_path)


@pytest.mark.parametrize("disabled", ["echo", ["echo", "echo"], [1], [""]])
def test_disabled_tools_requires_unique_names(tmp_path, disabled):
    with pytest.raises(DomainError):
        parse_servers({"s": {"command": "python", "disabled_tools": disabled}}, tmp_path)
