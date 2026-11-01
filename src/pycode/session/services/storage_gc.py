"""Mark true references before any cleanup; unreferenced objects are deleted directly."""

from pycode.session.domain.storage import mark_objects
from pycode.session.ports.storage import StorageObjects


class StorageGC:
    def __init__(self, objects: StorageObjects):
        self.objects = objects

    async def status(self):
        inventory = await self.objects.inventory()
        counts = {}
        bytes_by_type = {}
        for item in inventory.objects:
            counts[item.kind] = counts.get(item.kind, 0) + 1
            bytes_by_type[item.kind] = bytes_by_type.get(item.kind, 0) + item.size
        return {"objects": counts, "bytes": bytes_by_type}

    async def collect(self, *, dry_run=False):
        inventory = await self.objects.inventory()
        marked = mark_objects(inventory)
        decisions = []
        for item in inventory.objects:
            if item.protected:
                action = "keep_protected"
            elif item.object_id in marked:
                action = "keep_referenced"
            else:
                action = "delete"
            decisions.append(
                {
                    "object_id": item.object_id,
                    "type": item.kind,
                    "bytes": item.size,
                    "last_reference": item.metadata.get("last_reference"),
                    "action": action,
                }
            )
            if action == "delete" and not dry_run:
                await self.objects.delete(item)
        return {"dry_run": dry_run, "decisions": decisions}

    async def delete_session(self, session_id):
        await self.objects.delete_session(session_id)
