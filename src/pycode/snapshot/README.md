# Snapshot

`GitSnapshotStore(workspace, directory, excluded_roots=...)` owns an isolated Git object store and index. It captures, reads and compares trees and writes a selected path without changing the project index, refs or stash.

`SnapshotService(history, store)` records tool before/after boundaries in result details. The application explicitly connects its async tool callbacks. `changed_paths(target, mode)` is the confirmation input; `revert(target, mode)` rechecks current state and executes once.

Ordinary targets exclude themselves. A latest custom revert target undoes that record using its own snapshot and previous dialogue leaf. Non-leaf undo is rejected. Partial writes are recorded when storage remains available; if recording fails, the error makes no recovery promise. There are no preview/apply/recover transactions.
