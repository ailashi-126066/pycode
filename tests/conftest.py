import shutil

import pytest

RG_SKIP_REASON = "ripgrep (rg) is not on PATH; install it (e.g. `brew install ripgrep`) to run"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "requires_rg: test runs the real ripgrep binary and is skipped without rg"
    )


def pytest_collection_modifyitems(config, items):
    if shutil.which("rg") is not None:
        return
    skip = pytest.mark.skip(reason=RG_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("requires_rg") is not None:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_tempdir(tmp_path_factory, monkeypatch):
    """Large tool outputs go to $TMPDIR/pycode/<session>/; each test gets its own TMPDIR."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("tmpdir")))


@pytest.fixture
def disclosed_tools(monkeypatch):
    """Execution-only regressions start with definitions already available.

    Discovery integration tests do not use this fixture. Permission filters and
    calibration still run, so a disclosed definition is never execution approval.
    """
    from pycode.tools.adapters.discovery import ToolDiscovery

    async def disclose(self, request):
        return request

    monkeypatch.setattr(ToolDiscovery, "disclose", disclose)
