import pytest
from deepagents.backends import CompositeBackend

from pycode.kernel import Message
from pycode.session.history_backend import ConversationHistoryBackend
from pycode.tools import PycodeBackend
from tests.session.test_session import open_store as store_for

pytestmark = pytest.mark.requires_rg


async def test_root_discovery_merges_workspace_and_active_history_without_abstract_method_error(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("needle = 1\n")
    history = store_for(tmp_path)
    try:
        await history.append_message(Message("u1", "user", "needle current"))
        leaf = (await history.snapshot()).leaf_id
        empty = tmp_path / "empty-default"
        empty.mkdir()
        backend = CompositeBackend(
            default=PycodeBackend(empty, read_only=True),
            routes={
                "/workspace/": PycodeBackend(workspace),
                "/conversation_history/": ConversationHistoryBackend(history),
            },
        )
        found = await backend.aglob("**/*.py", "/")
        assert found.error is None
        assert [item["path"] for item in found.matches] == ["/workspace/main.py"]
        found = await backend.agrep("needle", "/")
        assert found.error is None and not found.truncated
        assert {item["path"] for item in found.matches} == {
            "/workspace/main.py",
            "/conversation_history/" + leaf + ".json",
        }
    finally:
        await history.aclose()


async def test_history_search_scopes_globs_caps_and_invalid_arguments(tmp_path, monkeypatch):
    history = store_for(tmp_path)
    try:
        await history.append_message(Message("u1", "user", "needle visible"))
        visible = (await history.snapshot()).leaf_id
        await history.append_message(Message("u2", "user", "needle secret"))
        hidden = (await history.snapshot()).leaf_id
        await history.branch(visible)
        backend = ConversationHistoryBackend(history)
        glob = await backend.aglob("**/*.json")
        assert [item["path"] for item in glob.matches] == ["/" + visible + ".json"]
        result = await backend.agrep("needle", glob="**/*.json")
        assert len(result.matches) == 1 and "visible" in result.matches[0]["text"]
        assert not result.truncated
        assert (await backend.agrep("needle", glob="*.py")).matches == []
        assert (
            "history_not_accessible"
            in (await backend.agrep("needle", "/" + hidden + ".json")).error
        )
        assert "invalid_regex" in (await backend.agrep("[")).error
        assert "invalid_glob" in (await backend.aglob("../*.json")).error
        assert (await backend.agrep("needle", max_count=0)).truncated
        assert (await backend.agrep("needle", max_count=1)).truncated
        monkeypatch.setattr("pycode.session.history_backend.SEARCH_BYTES", 16)
        assert (await backend.agrep("needle")).truncated
        assert backend.grep("needle").error.startswith("async_backend_required")
        assert backend.glob("*.json").error.startswith("async_backend_required")
    finally:
        await history.aclose()
