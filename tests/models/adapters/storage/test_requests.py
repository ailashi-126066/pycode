from dataclasses import replace

import pytest

from pycode.kernel import StorageError, UsageRecord
from pycode.models.adapters.storage.requests import SQLiteRequestStore
from pycode.models.domain.requests import RequestRecord


def initial_record():
    return RequestRecord(
        "request-1", "session-1", "run-1", "provider", "model", "agent", 0, "started", 1000
    )


async def test_request_attempt_and_usage_survive_close_and_reopen(tmp_path):
    path = tmp_path / "runtime.sqlite"
    store = SQLiteRequestStore(path)
    record = initial_record()
    await store.start(record)
    usage = UsageRecord("provider", "model", "request-1", input_tokens=12, output_tokens=3)
    completed = replace(record, status="completed", settled_at=1001, usage=usage)
    await store.settle(completed)
    await store.settle(completed)
    await store.aclose()
    reopened = SQLiteRequestStore(path)
    try:
        assert await reopened.list_requests("session-1") == (completed,)
        assert await reopened.list_requests("other-session") == ()
        with pytest.raises(StorageError, match="different settled"):
            await reopened.settle(replace(completed, error_code="different"))
    finally:
        await reopened.aclose()


async def test_settlement_cannot_create_a_phantom_request(tmp_path):
    store = SQLiteRequestStore(tmp_path / "runtime.sqlite")
    try:
        with pytest.raises(StorageError, match="not recorded"):
            await store.settle(replace(initial_record(), status="failed"))
    finally:
        await store.aclose()


async def test_unsettled_attempt_remains_explicit_for_later_recovery(tmp_path):
    path = tmp_path / "runtime.sqlite"
    store = SQLiteRequestStore(path)
    await store.start(initial_record())
    await store.aclose()
    reopened = SQLiteRequestStore(path)
    try:
        assert (await reopened.list_requests())[0].status == "started"
        assert (await reopened.list_requests())[0].usage is None
    finally:
        await reopened.aclose()


@pytest.mark.parametrize(
    "failed_statement", ["PRAGMA journal_mode", "PRAGMA synchronous", "CREATE TABLE", "COMMIT"]
)
def test_constructor_failure_closes_connection_before_returning(
    tmp_path, monkeypatch, failed_statement
):
    import sqlite3

    from pycode.models.adapters.storage import requests as request_module

    original_connect = sqlite3.connect
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            if statement.startswith(failed_statement):
                raise sqlite3.OperationalError("injected initialization failure")
            return super().execute(statement, parameters)

        def commit(self):
            if failed_statement == "COMMIT":
                raise sqlite3.OperationalError("injected initialization failure")
            return super().commit()

    def connect(path, **options):
        connection = original_connect(path, factory=FailingConnection, **options)
        opened.append(connection)
        return connection

    monkeypatch.setattr(request_module.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="injected initialization failure"):
        SQLiteRequestStore(tmp_path / "failed.sqlite3")
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


async def test_removed_candidate_record_is_rejected_without_rewriting_payload(tmp_path):
    import json
    import sqlite3
    from dataclasses import asdict

    path = tmp_path / "runtime.sqlite"
    store = SQLiteRequestStore(path)
    await store.start(initial_record())
    await store.aclose()
    old = {**asdict(initial_record()), "candidate_index": 1}
    payload = json.dumps(old)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_requests SET payload=?", (payload,))
    store = SQLiteRequestStore(path)
    try:
        with pytest.raises(StorageError) as error:
            await store.list_requests()
        assert error.value.code == "invalid_request_record"
    finally:
        await store.aclose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT payload FROM model_requests").fetchone()[0] == payload
