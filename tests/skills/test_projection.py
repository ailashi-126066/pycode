"""Pure historical contributors remain usable without runtime module activation."""

from types import SimpleNamespace

from pycode.mcp.history import MCPProjection


def test_empty_history_removes_only_owned_checkpoint():
    snapshot = SimpleNamespace(index={})
    mcp = MCPProjection().contribute(snapshot, (), (), {"mcp": {}})
    assert mcp.checkpoint_removals == frozenset({"mcp"}) and not mcp.entries


def test_mcp_projection_uses_persisted_catalog_only():
    catalog = {"hash": "hash", "tools": [], "text": "catalog"}
    entry = SimpleNamespace(id="catalog-entry", type="mcp_catalog", data=catalog)
    projected = MCPProjection().contribute(SimpleNamespace(), (entry,), (), {})
    assert projected.entries[0].entry_id == "catalog-entry"
    assert projected.entries[0].messages[0].text == "catalog"
    assert projected.checkpoint_updates["mcp"]["hash"] == "hash"
