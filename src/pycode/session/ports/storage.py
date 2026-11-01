"""Owned content storage: inventory and direct deletion."""

from typing import Protocol

from pycode.session.domain.storage import StorageInventory, StoredObject


class StorageObjects(Protocol):
    async def inventory(self) -> StorageInventory: ...
    async def delete(self, item: StoredObject) -> None: ...
    async def delete_session(self, session_id: str) -> None: ...
