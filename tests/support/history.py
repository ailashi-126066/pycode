"""Real temporary history for graph tests, collected by the test lifetime fixture."""

from pathlib import Path
from tempfile import TemporaryDirectory

from pycode.kernel import SessionHeader
from pycode.session import SqliteSessionStore
from tests.support.requests import TestTiming

OWNED_HISTORIES = []


class Journal(SqliteSessionStore):
    def __init__(self):
        directory = TemporaryDirectory(prefix="pycode-graph-")
        super().__init__(
            Path(directory.name) / "session",
            SessionHeader("session-1", "/workspace", "t"),
            TestTiming(),
        )
        self.messages = []
        OWNED_HISTORIES.append((self, directory))

    async def append_message(self, message):
        identifier = await super().append_message(message)
        self.messages.append(message)
        return identifier


class QueuedInputs:
    """Graph tests inject in-memory inputs; durable admission has its own integration suite."""

    def __init__(self, queue):
        self.queue = queue

    async def pending(self, run_id):
        return self.queue

    async def finish_boundary(self, run_id):
        return self.queue
