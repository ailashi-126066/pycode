"""Session persistence and application services assembled from explicit local paths."""

from copy import copy
from pathlib import Path

from pycode.kernel import Timing
from pycode.session.adapters.storage.history_backend import ConversationHistoryBackend
from pycode.session.adapters.storage.layout import StorageLayout
from pycode.session.adapters.storage.objects import FileStorageObjects
from pycode.session.adapters.storage.session_repository import SqliteSessionRepository
from pycode.session.adapters.storage.workspace import FileWorkspaceInspector
from pycode.session.services.application import ApplicationService
from pycode.session.services.ownership import SessionCleanup
from pycode.session.services.sessions import SessionService
from pycode.session.services.storage_gc import StorageGC


class SessionsModule:
    """Application-scoped paths; repositories and views own their session resources."""

    def scoped(self, workspace, user_data):
        scoped = copy(self)
        scoped.workspace, scoped.user_data = workspace, user_data
        scoped.layout = StorageLayout(workspace, user_data)
        return scoped

    def __init__(self, workspace: Path, user_data: Path, timing: Timing):
        self.workspace = workspace
        self.user_data = user_data
        self.timing = timing
        self.layout = StorageLayout(workspace, user_data)

    @staticmethod
    def cleanup():
        return SessionCleanup()

    def repository(self):
        return SqliteSessionRepository(self.layout.sessions, str(self.workspace), self.timing)

    def service(self):
        return SessionService(self.repository())

    def application(self, version: str):
        return ApplicationService(workspace=FileWorkspaceInspector(self.workspace), version=version)

    def storage(self, *, owned_session_id=None):
        objects = FileStorageObjects(
            self.user_data, self.workspace, owned_session_id=owned_session_id
        )
        return StorageGC(objects)

    def history_backend(self, history):
        return ConversationHistoryBackend(history)
