import pytest

from pycode.app.api import compose_application
from pycode.app.config import AppConfig


def test_public_application_composition_has_no_filesystem_write(tmp_path):
    config = AppConfig(workspace=tmp_path, user_data=tmp_path / "user-data", memory_mode="off")
    application = compose_application(config)
    status = application.status()
    assert status.name == "pycode"
    assert status.workspace.exists
    assert not status.workspace.snapshot_available
    assert list(tmp_path.iterdir()) == []


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
