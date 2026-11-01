"""Content-object GC is separate from worktree cleanup and conversation eviction."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StoredObject:
    object_id: str
    kind: str
    files: tuple[str, ...]
    size: int
    references: tuple[str, ...] = ()
    protected: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StorageInventory:
    objects: tuple[StoredObject, ...]
    roots: tuple[str, ...]


def mark_objects(inventory):
    by_id = {}
    for item in inventory.objects:
        by_id[item.object_id] = item
    pending = list(inventory.roots)
    marked = set()
    while pending:
        identifier = pending.pop()
        if identifier in marked:
            continue
        marked.add(identifier)
        item = by_id.get(identifier)
        if item is not None:
            pending.extend(item.references)
    return marked
