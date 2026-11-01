# Pi 没有，pycode 新增
"""Stable errors at public I/O and execution boundaries."""

import asyncio


class OutputClosed(asyncio.CancelledError):
    """A closed CLI consumer requests cancellation and exit code 141."""


class DomainError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ToolError(DomainError):
    pass


class ModelError(DomainError):
    def __init__(self, code, message, *, retryable=False, retry_after=None, request_id=None):
        super().__init__(code, message, retryable=retryable)
        self.retry_after = retry_after
        self.request_id = request_id


class StorageError(DomainError):
    pass


class SnapshotError(DomainError):
    pass
